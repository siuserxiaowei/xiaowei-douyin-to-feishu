#!/usr/bin/env python3
"""Assemble a completed mediakit subtitle result into a traceable Douyin run.

This is an offline bridge. It never calls mediakit or Feishu. An ASR task marked
"completed" is not by itself proof that every part of the video was transcribed:
the subtitle timeline is checked against the independently captured video length.
"""

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


MAX_EDGE_GAP_SECONDS = 3.0
MAX_INTERNAL_GAP_SECONDS = 3.0
MAX_DURATION_OVERRUN_SECONDS = 3.0
MAX_SEGMENT_DURATION_SECONDS = 30.0


class AssembleError(ValueError):
    """Input or output does not satisfy the run-record contract."""


def positive_seconds(value, field):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise AssembleError(f"{field} 必须是正数秒")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise AssembleError(f"{field} 必须是正数秒")
    return number


def video_identity(metadata):
    video_id = metadata.get("video_id")
    source = metadata.get("source_url_canonical")
    if video_id is not None:
        video_id = str(video_id)
        if not video_id.isdigit():
            raise AssembleError("video_id 必须是数字")
    if source:
        parsed = urlsplit(source)
        path = parsed.path.rstrip("/")
        parts = path.split("/")
        if (parsed.scheme != "https" or parsed.hostname not in ("www.douyin.com", "douyin.com")
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or len(parts) != 3 or parts[:2] != ["", "video"]
                or not parts[2].isdigit() or parsed.query or parsed.fragment):
            raise AssembleError("source_url_canonical 必须是规范抖音 HTTPS 视频链接")
        if video_id and video_id != parts[2]:
            raise AssembleError("video_id 与 source_url_canonical 不一致")
        video_id = parts[2]
    if not video_id:
        raise AssembleError("需要 video_id 或 source_url_canonical")
    return video_id, f"https://www.douyin.com/video/{video_id}"


def subtitle_timeline(subtitles, duration_seconds):
    if not isinstance(subtitles, list):
        raise AssembleError("completed ASR 缺少 subtitles 数组")
    if not subtitles:
        return "", [], ["ASR 返回空字幕，无法确认视频是否有口播"]

    texts = []
    intervals = []
    issues = []
    previous_start = previous_end = None
    for number, subtitle in enumerate(subtitles, start=1):
        if not isinstance(subtitle, dict):
            raise AssembleError(f"第 {number} 段字幕不是对象")
        start = subtitle.get("start_time")
        end = subtitle.get("end_time")
        if (isinstance(start, bool) or isinstance(end, bool)
                or not isinstance(start, (int, float)) or not isinstance(end, (int, float))
                or not math.isfinite(start) or not math.isfinite(end)
                or start < 0 or end <= start):
            raise AssembleError(f"第 {number} 段字幕时间无效")
        if previous_start is not None and (start < previous_start or start < previous_end - 0.05):
            raise AssembleError(f"第 {number} 段字幕乱序或与上一段明显重叠")
        value = subtitle.get("subtitle_text")
        if not isinstance(value, str):
            raise AssembleError(f"第 {number} 段缺少 subtitle_text 字符串")
        if not value.strip():
            issues.append(f"第 {number} 段字幕为空")
        if end - start > MAX_SEGMENT_DURATION_SECONDS:
            issues.append(f"第 {number} 段持续 {end - start:.2f} 秒，需核实是否合并或漏段")
        if previous_end is not None and start - previous_end > MAX_INTERNAL_GAP_SECONDS:
            issues.append(f"第 {number - 1}–{number} 段之间有 {start - previous_end:.2f} 秒未覆盖")
        texts.append(value)
        intervals.append((float(start), float(end)))
        previous_start, previous_end = start, end

    if intervals[0][0] > MAX_EDGE_GAP_SECONDS:
        issues.append(f"开头 {intervals[0][0]:.2f} 秒未覆盖")
    if duration_seconds is None:
        issues.append("缺少独立的视频时长，无法核实结尾覆盖")
    else:
        tail_gap = duration_seconds - intervals[-1][1]
        if tail_gap > MAX_EDGE_GAP_SECONDS:
            issues.append(f"结尾 {tail_gap:.2f} 秒未覆盖")
        if tail_gap < -MAX_DURATION_OVERRUN_SECONDS:
            issues.append(f"字幕末端超出视频时长 {-tail_gap:.2f} 秒，需核实元数据")
    return "".join(texts), intervals, issues


def assemble(asr, metadata):
    if not isinstance(asr, dict) or not isinstance(metadata, dict):
        raise AssembleError("ASR 与视频元数据必须是 JSON 对象")
    video_id, canonical_url = video_identity(metadata)
    duration = metadata.get("duration_seconds")
    if duration is not None:
        duration = positive_seconds(duration, "duration_seconds")
    asr_status = asr.get("status")
    if not isinstance(asr_status, str) or not asr_status.strip():
        raise AssembleError("ASR 结果缺少 status")
    asr_duration = asr.get("duration")
    if asr_duration is not None:
        asr_duration = positive_seconds(asr_duration, "ASR duration")
    asr_method = "mediakit-cli video asr-subtitles"
    input_method = metadata.get("method")
    if input_method is not None and (not isinstance(input_method, str) or not input_method.strip()):
        raise AssembleError("method 必须是非空字符串")
    if input_method and "mediakit" in input_method.casefold():
        combined_method = input_method
    elif input_method:
        combined_method = f"{input_method} + {asr_method}"
    else:
        combined_method = asr_method
    now = datetime.now(timezone.utc).isoformat()
    run = {
        "video_id": video_id,
        "source_url_canonical": canonical_url,
        "title": metadata.get("title") or f"视频 {video_id}",
        "author": metadata.get("author"),
        "created_at": now,
        "duration_seconds": duration,
        "method": combined_method,
        "asr_method": asr_method,
        "asr_status": asr_status,
        "asr_task_id": asr.get("task_id"),
        "asr_request_id": asr.get("request_id"),
        "asr_duration": asr_duration,
        "asr_segments": 0,
        "transcript_status": "failed",
        "transcript_file": None,
        "transcript_characters": 0,
        "transcript_sha256": None,
        "human_verified": False,
    }
    for key in ("source_url_share", "source_url_input", "author_id", "published_at", "description"):
        if metadata.get(key) is not None:
            run[key] = metadata[key]
    if metadata.get("stage") == "audio_ready" and input_method:
        run["audio_method"] = input_method

    if asr_status != "completed":
        run["transcript_status"] = "failed" if asr_status in {"failed", "error", "cancelled"} else "pending"
        run["asr_coverage_status"] = "not_assessable"
        run["note"] = f"ASR 状态为 {asr_status}，未取得可核实的逐字稿；不可作为全文发布。"
        return run, None

    transcript, intervals, issues = subtitle_timeline(asr.get("subtitles"), duration)
    if not isinstance(run["asr_task_id"], str) or not run["asr_task_id"].strip():
        issues.append("缺少 ASR task_id，无法核对转写任务来源")
    run["asr_segments"] = len(intervals)
    if intervals:
        run["asr_first_start_seconds"] = intervals[0][0]
        run["asr_last_end_seconds"] = intervals[-1][1]
    if not transcript.strip():
        run["asr_coverage_status"] = "unverified"
        run["asr_coverage_issues"] = issues
        run["note"] = "ASR 已完成但没有有效口播文字；不能推断无口播，需人工核实。"
        return run, None

    run["transcript_file"] = "transcript.txt"
    run["transcript_characters"] = len(transcript)
    run["transcript_sha256"] = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
    run["asr_coverage_issues"] = issues
    if issues:
        run["transcript_status"] = "partial"
        run["asr_coverage_status"] = "unverified"
        run["note"] = "ASR 自动转写，未人工逐字校对；时间轴存在未核实的覆盖问题，不能宣称全文已取回。"
    else:
        run["transcript_status"] = "complete"
        run["asr_coverage_status"] = "timeline_covered"
        run["note"] = "字幕时间轴覆盖视频首尾，ASR 自动转写未人工逐字校对；识别错字仍需人工核对。"
    return run, transcript


def _exclusive_write(path, content):
    """Publish one complete file without overwriting an existing result."""
    descriptor, temporary = tempfile.mkstemp(prefix=".assemble-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # Fails if another invocation already published.
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_outputs(output_dir, run, transcript):
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    run_path = output_dir / "run.json"
    transcript_path = output_dir / "transcript.txt"
    if run_path.exists() or transcript_path.exists():
        raise AssembleError("输出目录已有 run.json 或 transcript.txt；请使用新的输出目录，避免覆盖原始结果")
    if transcript is not None:
        _exclusive_write(transcript_path, transcript.encode("utf-8"))
    encoded = (json.dumps(run, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _exclusive_write(run_path, encoded)
    return run_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("asr_json", type=Path, help="mediakit 返回的完整 ASR JSON")
    parser.add_argument("--metadata", type=Path, help="视频元数据 JSON，可使用尚未发布的 run.json")
    parser.add_argument("--output-dir", required=True, type=Path, help="新建或空的运行目录")
    parser.add_argument("--video-id")
    parser.add_argument("--source-url-canonical")
    parser.add_argument("--title")
    parser.add_argument("--author")
    parser.add_argument("--duration-seconds", type=float,
                        help="独立核实的视频时长；缺少时只能标记部分/待核实")
    args = parser.parse_args(argv)
    try:
        asr = json.loads(args.asr_json.read_text(encoding="utf-8"))
        metadata = json.loads(args.metadata.read_text(encoding="utf-8")) if args.metadata else {}
        if not isinstance(metadata, dict):
            raise AssembleError("视频元数据必须是 JSON 对象")
        for key in ("video_id", "source_url_canonical", "title", "author", "duration_seconds"):
            value = getattr(args, key)
            if value is not None:
                metadata[key] = value
        run, transcript = assemble(asr, metadata)
        path = write_outputs(args.output_dir, run, transcript)
        ok = run["transcript_status"] == "complete"
        print(json.dumps({"ok": ok, "run_json": str(path), "transcript_status": run["transcript_status"],
                          "asr_coverage_issues": run.get("asr_coverage_issues", [])}, ensure_ascii=False))
        return 0 if ok else 2
    except (OSError, ValueError, TypeError, AssembleError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
