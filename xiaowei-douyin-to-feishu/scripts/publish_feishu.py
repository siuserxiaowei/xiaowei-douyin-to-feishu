#!/usr/bin/env python3
"""Resume one saved transcript and publish it to a Feishu document with lark-cli.

The run record is the source of truth. A create attempt is recorded before the
network call, and a returned document is saved before readback. An uncertain
create is never retried automatically, because it may already have succeeded.
"""

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class PublishError(Exception):
    """A publishing step needs attention or cannot be verified."""


def load_renderer():
    path = Path(__file__).with_name("build_document.py")
    spec = importlib.util.spec_from_file_location("xiaowei_build_document", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def now_utc():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def locked_run(path):
    path = Path(path).resolve()
    with path.with_name(path.name + ".lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield path


def save_run(path, run):
    """Replace only the run record, atomically, preserving unrelated fields."""
    encoded = (json.dumps(run, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=".run-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_source(path, run):
    if run.get("transcript_status") != "complete":
        raise PublishError("只发布全文已取回的运行记录")
    value = run.get("transcript_file")
    if not isinstance(value, str) or not value:
        raise PublishError("run.json 缺少 transcript_file")
    transcript_path = (path.parent / value).resolve()
    transcript = transcript_path.read_bytes().decode("utf-8")
    if not transcript.strip():
        raise PublishError("逐字稿为空")
    expected = run.get("transcript_sha256")
    actual = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
    if expected and expected != actual:
        raise PublishError("逐字稿与 run.json 的 SHA-256 不一致，停止写入")
    if run.get("transcript_characters") is not None and run["transcript_characters"] != len(transcript):
        raise PublishError("逐字稿字符数与 run.json 不一致，停止写入")
    return transcript_path, transcript


def make_document(path, run, transcript_path):
    """Use the bundled renderer and keep the exact submitted Markdown locally."""
    video_id = str(run.get("video_id") or "")
    title = run.get("title") or f"视频 {video_id}"
    batch = {
        "title": f"抖音逐字稿｜{title}",
        "collected_at": run.get("created_at"),
        "videos": [{
            "video_id": video_id,
            "source_url": run.get("source_url_canonical"),
            "title": title,
            "author": run.get("author"),
            "published_at": run.get("published_at"),
            "method": run.get("method") or "未记录",
            "status": "complete",
            "summary": [],
            "transcript_file": str(transcript_path),
        }],
    }
    body, manifest = load_renderer().render(batch, path.parent)
    document_path = path.with_name("document.md")
    if document_path.exists():
        if document_path.read_text(encoding="utf-8") != body:
            raise PublishError("现有 document.md 与逐字稿不一致，停止写入")
    else:
        try:
            with document_path.open("x", encoding="utf-8") as stream:
                stream.write(body)
        except FileExistsError:
            if document_path.read_text(encoding="utf-8") != body:
                raise PublishError("现有 document.md 与逐字稿不一致，停止写入")
    return document_path, manifest


def cli_json(arguments):
    result = subprocess.run(arguments, capture_output=True, text=True, check=False)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PublishError(f"lark-cli 未返回可解析的 JSON（退出码 {result.returncode}）") from exc
    if result.returncode != 0 or payload.get("ok") is not True:
        detail = payload.get("error") or payload.get("message") or f"退出码 {result.returncode}"
        raise PublishError(f"lark-cli 调用失败：{detail}")
    return payload


def create(path, run, document_path, title, parent_token=None):
    run["feishu_write_status"] = "create_result_unknown"
    run["feishu_create_attempted_at"] = now_utc()
    save_run(path, run)
    args = ["lark-cli", "docs", "+create", "--as", "user", "--doc-format", "markdown",
            "--title", title, "--content", "@" + str(document_path)]
    if parent_token:
        args.extend(["--parent-token", parent_token])
    try:
        payload = cli_json(args)
    except (OSError, PublishError):
        # A timeout, crash, or lost response can occur after Feishu creates the
        # document. The next run must reconcile the existing document first.
        raise PublishError("创建结果不确定；先在飞书确认文档，再用 --existing-doc 续跑")
    run["feishu_create_response"] = payload
    document = payload.get("data", {}).get("document") or {}
    url = document.get("url")
    document_id = document.get("document_id")
    if isinstance(url, str) and url:
        run["feishu_doc"] = url
    if isinstance(document_id, str) and document_id:
        run["feishu_doc_id"] = document_id
    run["feishu_write_status"] = "created_pending_verification" if (url or document_id) else "create_result_unknown"
    save_run(path, run)  # Save the real returned location before any fetch.
    if not (url or document_id):
        raise PublishError("创建响应没有文档 URL 或 ID；已保存响应，需先定位文档")


ESCAPE_PATTERN = re.compile(r"\\([\\`*_\[\]$~<>#+\-=.!|()&])")
TRANSCRIPT_HEADING = re.compile(r"(?m)^### 逐字稿[ \t]*\r?\n(?:[ \t]*\r?\n)")


def readback_transcript(content):
    if not isinstance(content, str):
        raise PublishError("飞书回读响应缺少 Markdown 正文")
    headings = list(TRANSCRIPT_HEADING.finditer(content))
    if len(headings) != 1:
        raise PublishError("飞书回读中无法唯一定位逐字稿章节")
    return content[headings[0].end():].replace("\r\n", "\n").rstrip("\n")


def verify_readback_identity(content, run):
    """Ensure the fetched section belongs to the requested video."""
    heading = TRANSCRIPT_HEADING.search(content)
    metadata = content[:heading.start()] if heading else content
    video_id = str(run["video_id"])
    source = run.get("source_url_canonical")
    id_line = rf"(?m)^视频 ID：{re.escape(video_id)}[ \t]*\r?$"
    if not re.search(id_line, metadata) or not source or source not in metadata:
        raise PublishError("飞书回读的视频 ID 或来源链接与运行记录不一致")


def same_text(readback, original):
    """Allow Markdown escaping and layout whitespace, never altered wording."""
    original = original.replace("\r\n", "\n").rstrip("\n")
    candidates = (readback, ESCAPE_PATTERN.sub(r"\1", readback))
    return any(re.sub(r"\s+", "", value) == re.sub(r"\s+", "", original) for value in candidates)


def verify(path, run, transcript):
    doc = run.get("feishu_doc") or run.get("feishu_doc_id")
    if not doc:
        raise PublishError("没有已确认的飞书文档 URL 或 ID")
    try:
        payload = cli_json(["lark-cli", "docs", "+fetch", "--as", "user", "--doc", doc,
                            "--doc-format", "markdown", "--detail", "with-ids", "--scope", "full"])
        content = payload.get("data", {}).get("document", {}).get("content")
        readback = readback_transcript(content)
        verify_readback_identity(content, run)
    except (OSError, PublishError) as exc:
        run["feishu_write_status"] = "verification_pending"
        save_run(path, run)
        raise PublishError(f"文档已定位，但全文回读待完成：{exc}") from exc
    matches = same_text(readback, transcript)
    run["feishu_readback_characters"] = len(readback)
    run["feishu_readback_matches_transcript"] = matches
    run["feishu_write_status"] = "verified_full_text" if matches else "verification_failed"
    if matches:
        run["feishu_verified_at"] = now_utc()
    save_run(path, run)
    if not matches:
        raise PublishError("飞书回读逐字稿与本地原稿不一致；保留原文档供修补")
    return {"ok": True, "feishu_doc": doc, "feishu_write_status": run["feishu_write_status"],
            "video_id": str(run["video_id"]), "transcript_characters": len(transcript)}


def publish(run_path, existing_doc=None, parent_token=None):
    with locked_run(run_path) as path:
        run = json.loads(path.read_text(encoding="utf-8"))
        transcript_path, transcript = read_source(path, run)
        if existing_doc:
            current = run.get("feishu_doc") or run.get("feishu_doc_id")
            if current and current != existing_doc:
                raise PublishError("--existing-doc 与运行记录中的文档不一致")
            run["feishu_doc"] = existing_doc
            run["feishu_write_status"] = "created_pending_verification"
            save_run(path, run)
        if not (run.get("feishu_doc") or run.get("feishu_doc_id")):
            if run.get("feishu_create_attempted_at"):
                raise PublishError("先前创建结果不确定；先找到已有文档并用 --existing-doc 续跑")
            document_path, manifest = make_document(path, run, transcript_path)
            create(path, run, document_path, manifest["title"], parent_token)
        return verify(path, run, transcript)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_json", type=Path)
    parser.add_argument("--existing-doc", help="已核实存在的飞书文档 URL 或 ID，用于恢复不确定的创建")
    parser.add_argument("--parent-token", help="仅新建文档时使用已核实的父文件夹或知识库节点 token")
    args = parser.parse_args(argv)
    try:
        result = publish(args.run_json, args.existing_doc, args.parent_token)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError, PublishError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
