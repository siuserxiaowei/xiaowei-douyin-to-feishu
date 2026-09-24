#!/usr/bin/env python3
"""Render transcript text and factual metadata to escaped Markdown for Feishu import."""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

STATUS = {
    "complete": "全文已取回，未逐字校对",
    "partial": "部分内容，缺段待补",
    "failed": "未取得逐字稿",
    "no_speech": "确认无口播",
}


def escape_text(text):
    """Preserve literal text, including source text that looks like XML/Markdown."""
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    # Escaping Markdown punctuation everywhere is valid CommonMark; it also
    # prevents ordered lists, XML extensions, autolinks, and table delimiters.
    return re.sub(r"([\\`*_\[\]$~<>#+\-=.!|()&])", r"\\\1", text)


def text_value(value, default="未获取"):
    return escape_text(value if value not in (None, "") else default)


def render(batch, base):
    title = batch.get("title")
    collected = batch.get("collected_at")
    videos = batch.get("videos")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("需要非空文档 title")
    if not isinstance(collected, str) or not collected.strip():
        raise ValueError("需要真实 collected_at")
    if not isinstance(videos, list) or not videos:
        raise ValueError("videos 必须是非空数组")
    seen = set()
    sections = []
    records = []
    for item in videos:
        video_id = str(item.get("video_id", ""))
        if not video_id.isdigit() or video_id in seen:
            raise ValueError("视频 ID 必须为数字且不可重复")
        seen.add(video_id)
        source = item.get("source_url", "")
        parsed = urlsplit(source)
        if (parsed.scheme != "https" or parsed.hostname not in ("www.douyin.com", "douyin.com")
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or parsed.path.rstrip("/") != f"/video/{video_id}" or parsed.query or parsed.fragment):
            raise ValueError("source_url 应为与 video_id 一致的规范抖音 HTTPS 视频链接")
        status = item.get("status")
        if status not in STATUS:
            raise ValueError("未知的逐字稿状态")
        note = item.get("note") or ""
        method = item.get("method")
        if not isinstance(method, str) or not method.strip():
            raise ValueError("需要记录实际获取方式 method")
        if status != "complete" and (not isinstance(note, str) or not note.strip()):
            raise ValueError("部分、失败或无口播状态需要 note 说明依据")
        summary = item.get("summary", [])
        if not isinstance(summary, list) or len(summary) > 3 or any(not isinstance(v, str) or not v.strip() for v in summary):
            raise ValueError("summary 应为 0 到 3 个非空字符串")
        description = item.get("description")
        if description is not None and not isinstance(description, str):
            raise ValueError("description 应为抖音页面发布文案字符串")
        description = description if description and description.strip() else ""
        transcript = ""
        transcript_path = None
        if status in ("complete", "partial"):
            if not item.get("transcript_file"):
                raise ValueError("成功或部分状态必须提供 transcript_file")
            transcript_path = (base / item["transcript_file"]).resolve()
            transcript = transcript_path.read_bytes().decode("utf-8")
            if not transcript.strip():
                raise ValueError("成功或部分状态不能使用空逐字稿")
        elif item.get("transcript_file") or summary:
            raise ValueError("失败或无口播状态不能携带逐字稿或 AI 摘要")
        heading = escape_text(item.get("title") or f"视频 {video_id}")
        lines = [f"## {heading}", "", f"来源：[抖音原视频]({source})",
                 f"视频 ID：{video_id}", f"作者：{text_value(item.get('author'))}",
                 f"发布时间：{text_value(item.get('published_at'))}",
                 f"采集时间：{text_value(collected)}", "",
                 f"全文状态：{STATUS[status]}", f"获取方式：{escape_text(method)}", ""]
        if note:
            lines.extend(["说明：" + escape_text(note), ""])
        if description:
            lines.extend(["### 发布文案（抖音页面）", "", escape_text(description), ""])
        if summary:
            lines.extend(["### 内容速览（AI 提炼）", ""])
            lines.extend("- " + escape_text(value) for value in summary)
            lines.append("")
        if transcript:
            label = "已取得的逐字稿（不完整）" if status == "partial" else "逐字稿"
            lines.extend([f"### {label}", "", escape_text(transcript), ""])
        sections.append("\n".join(lines))
        record = {"video_id": video_id, "status": status,
                        "source_url": source, "transcript_file": str(transcript_path) if transcript_path else None,
                        "characters": len(transcript),
                        "sha256": hashlib.sha256(transcript.encode("utf-8")).hexdigest() if transcript else None}
        if description:
            record["description_characters"] = len(description)
            record["description_sha256"] = hashlib.sha256(description.encode("utf-8")).hexdigest()
        records.append(record)
    counts = {key: sum(item["status"] == key for item in records) for key in STATUS}
    overview = (f"本批共 {len(videos)} 条视频：全文已取回 {counts['complete']} 条，"
                f"部分内容 {counts['partial']} 条，未取得逐字稿 {counts['failed']} 条，"
                f"确认无口播 {counts['no_speech']} 条。自动提取或转写结果未逐字校对。")
    body = overview + "\n\n" + "\n\n".join(sections) + "\n"
    manifest = {"title": title, "collected_at": collected, "videos": records, "counts": counts,
                "document_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "feishu_write_status": "not_started"}
    return body, manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    manifest_path = args.output.with_suffix(args.output.suffix + ".manifest.json")
    try:
        if args.output.resolve() == args.metadata.resolve():
            raise ValueError("输出不能覆盖元数据文件")
        if args.output.exists() or manifest_path.exists():
            raise ValueError("输出文件已存在，请使用新的路径")
        batch = json.loads(args.metadata.read_text(encoding="utf-8"))
        body, manifest = render(batch, args.metadata.resolve().parent)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as output:
            output.write(body)
        with manifest_path.open("x", encoding="utf-8") as output:
            json.dump(manifest, output, ensure_ascii=False, indent=2)
        print(json.dumps({"ok": True, "title": manifest["title"], "document": str(args.output.resolve()),
                          "manifest": str(manifest_path.resolve())}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
