#!/usr/bin/env python3
"""Optional Douyin audio extraction / local ASR. No installs or cloud uploads.

The old share-page parser and SenseVoice configuration adapted from chubbyguan/chubbyskills.
Copyright (c) 2026 Chubby; MIT notice: ../licenses/chubbyskills-MIT.txt.
"""

import argparse
import hashlib
import importlib.util
import ipaddress
import json
import math
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import wave
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)
MODEL = "iic/SenseVoiceSmall"


def douyin_host(host):
    return any(host == d or host.endswith("." + d) for d in ("douyin.com", "iesdouyin.com"))


def source_url(text):
    urls = re.findall(r"https?://[^\s<>\"'，。！？；、）】]+", text)
    urls = list(dict.fromkeys(u.rstrip(".,;!?)］]") for u in urls))
    if len(urls) != 1:
        raise ValueError("每次请提供一个抖音链接，或只含一个链接的分享文案")
    parsed = urlsplit(urls[0])
    if not douyin_host(parsed.hostname or "") or parsed.username or parsed.password:
        raise ValueError("来源必须是 douyin.com 或 iesdouyin.com 的公开链接")
    return urls[0]


def video_id_from_url(url):
    parsed = urlsplit(url)
    if not douyin_host(parsed.hostname or ""):
        raise ValueError("跳转结果不是抖音页面")
    match = re.search(r"/(?:share/)?video/(\d+)(?:/|$)", parsed.path)
    if match:
        return match.group(1)
    for key in ("modal_id", "item_id"):
        value = parse_qs(parsed.query).get(key, [""])[0]
        if value.isdigit():
            return value
    raise ValueError("页面链接中没有视频 ID，可能是主页、失效链接或登录页")


def validate_public_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("只支持公开 HTTP(S) 资源")
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("拒绝访问本机或内网资源")


class PublicRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_public(url, *, referer=False):
    validate_public_url(url)
    headers = {"User-Agent": MOBILE_UA}
    if referer:
        headers["Referer"] = "https://www.douyin.com/"
    response = build_opener(PublicRedirects()).open(Request(url, headers=headers), timeout=30)
    try:
        validate_public_url(response.geturl())
    except Exception:
        response.close()
        raise
    return response


def resolve_video_id(url):
    try:
        return video_id_from_url(url)
    except ValueError:
        with open_public(url) as response:
            return video_id_from_url(response.geturl())


def parse_share_page(html):
    match = re.search(r"window\._ROUTER_DATA\s*=\s*", html)
    if not match:
        raise ValueError("分享页没有 _ROUTER_DATA；可能被风控、链接失效或页面已变化")
    try:
        # raw_decode accepts whitespace and a trailing semicolon without executing JS.
        data, _ = json.JSONDecoder().raw_decode(html[match.end():].lstrip())
        item = data["loaderData"]["video_(id)/page"]["videoInfoRes"]["item_list"][0]
        addresses = item["video"]["play_addr"]["url_list"]
        address = next(u for u in addresses if isinstance(u, str) and urlsplit(u).scheme in ("http", "https"))
        title = item.get("desc") or ""
        description = title if isinstance(title, str) else ""
        return {"title": description, "description": description, "video_url": address}
    except (KeyError, TypeError, IndexError, ValueError, StopIteration) as exc:
        raise ValueError("分享页结构不匹配或没有播放地址，请改用已有视频提取工具") from exc


def copy_limited(response, destination, limit):
    length = response.headers.get("Content-Length", "")
    if length.isdigit() and int(length) > limit:
        raise ValueError("视频超过本次下载大小上限")
    total = 0
    with destination.open("xb") as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ValueError("视频超过本次下载大小上限")
            output.write(chunk)
    if not total:
        raise ValueError("下载结果为空")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def valid_duration_seconds(value):
    """Only independently reported, finite, positive media lengths are usable."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        seconds = float(value)
    except OverflowError:
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def extracted_wav_duration_seconds(path):
    """Measure the extracted media itself, independently of ASR timestamps."""
    try:
        with wave.open(str(path), "rb") as stream:
            if (stream.getframerate() != 16000 or stream.getnchannels() != 1
                    or stream.getsampwidth() != 2 or stream.getcomptype() != "NONE"):
                return None
            return valid_duration_seconds(stream.getnframes() / stream.getframerate())
    except (OSError, EOFError, wave.Error):
        return None


def safe_input_url(url):
    """Retain the supplied share-link path, dropping query/fragment tracking data."""
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.hostname or "", parsed.path, "", ""))


def extract_audio(video, audio):
    binary = shutil.which("ffmpeg")
    if not binary:
        raise RuntimeError("缺少 FFmpeg；可改用豆包工作已有的音频提取能力")
    subprocess.run(
        [binary, "-nostdin", "-v", "error", "-n", "-i", str(video),
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio)],
        capture_output=True, check=True, timeout=300,
    )
    if not audio.exists() or audio.stat().st_size <= 44:
        raise RuntimeError("未提取到有效音轨")


def download_ytdlp(url, run_dir, max_mb, cookies_from_browser=None):
    """Prefer yt-dlp's maintained extractor over Douyin's changing share HTML."""
    binary = shutil.which("yt-dlp")
    if not binary:
        raise RuntimeError("缺少 yt-dlp")
    common = [binary, "--no-playlist", "--no-warnings", "--no-progress"]
    if cookies_from_browser:
        common.extend(["--cookies-from-browser", cookies_from_browser])
    metadata = subprocess.run(
        common + ["--dump-single-json", "--skip-download", url],
        capture_output=True, text=True, timeout=90,
    )
    if metadata.returncode:
        raise RuntimeError("yt-dlp 未能解析此抖音链接；检查链接、登录态或浏览器会话")
    try:
        info = json.loads(metadata.stdout)
        video_id = str(info["id"])
        if not video_id.isdigit():
            raise ValueError("invalid video ID")
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError("yt-dlp 未返回有效的视频 ID") from exc
    title = info.get("title") if isinstance(info.get("title"), str) else ""
    # A video's page description is the author's published caption, not the
    # spoken transcript. Keep the original value separate for downstream use.
    description = info.get("description") if isinstance(info.get("description"), str) else ""
    duration_seconds = valid_duration_seconds(info.get("duration"))
    with tempfile.TemporaryDirectory(prefix="video-", dir=run_dir) as temporary:
        video_root = Path(temporary)
        download_result = subprocess.run(
            common + ["--max-filesize", str(int(max_mb * 1024 * 1024)),
                      "-f", "bestaudio/best", "-o", str(video_root / "media.%(ext)s"), url],
            capture_output=True, text=True, timeout=300,
        )
        if download_result.returncode:
            raise RuntimeError("yt-dlp 未取得媒体；检查浏览器登录态或视频访问限制")
        media = [p for p in video_root.glob("media.*") if p.is_file() and p.suffix not in (".part", ".ytdl")]
        if len(media) != 1 or media[0].stat().st_size > int(max_mb * 1024 * 1024):
            raise RuntimeError("下载结果不存在、数量异常或超过大小上限")
        audio_path = run_dir / "audio.wav"
        extract_audio(media[0], audio_path)
    duration_source = "yt-dlp" if duration_seconds is not None else None
    if duration_seconds is None:
        duration_seconds = extracted_wav_duration_seconds(audio_path)
        if duration_seconds is not None:
            duration_source = "extracted_audio"
    canonical_url = f"https://www.douyin.com/video/{video_id}"
    result = {
        "stage": "audio_ready", "video_id": video_id, "title": title,
        "source_url": canonical_url, "source_url_canonical": canonical_url,
        "source_url_input": safe_input_url(url),
        "audio_path": str(audio_path.resolve()), "audio_sha256": sha256(audio_path),
        "method": "yt-dlp+ffmpeg", "transcript_status": "not_started",
    }
    if description.strip():
        result["description"] = description
    if duration_seconds is not None:
        result["duration_seconds"] = duration_seconds
        result["duration_source"] = duration_source
    return result


def download_mobile_share(url, run_dir, max_mb):
    """Legacy fallback: this page layout was observed broken on 2026-09-24."""
    video_id = resolve_video_id(url)
    share = f"https://www.iesdouyin.com/share/video/{video_id}"
    with open_public(share) as response:
        html_bytes = response.read(4 * 1024 * 1024 + 1)
    if len(html_bytes) > 4 * 1024 * 1024:
        raise ValueError("分享页面异常过大，已停止解析")
    info = parse_share_page(html_bytes.decode("utf-8", errors="replace"))
    audio_path = run_dir / "audio.wav"
    with tempfile.TemporaryDirectory(prefix="video-", dir=run_dir) as temporary:
        video_path = Path(temporary) / "video.mp4"
        with open_public(info["video_url"], referer=True) as response:
            copy_limited(response, video_path, int(max_mb * 1024 * 1024))
        extract_audio(video_path, audio_path)
    canonical_url = f"https://www.douyin.com/video/{video_id}"
    result = {
        "stage": "audio_ready", "video_id": video_id, "title": info["title"],
        "source_url": canonical_url, "source_url_canonical": canonical_url,
        "source_url_input": safe_input_url(url),
        "audio_path": str(audio_path.resolve()), "audio_sha256": sha256(audio_path),
        "method": "legacy_mobile_share_page+ffmpeg", "transcript_status": "not_started",
    }
    if info["description"].strip():
        result["description"] = info["description"]
    return result


def download(source, run_dir, max_mb, cookies_from_browser=None):
    if not shutil.which("ffmpeg"):
        raise RuntimeError("缺少 FFmpeg，未开始下载")
    url = source_url(source)
    if shutil.which("yt-dlp"):
        return download_ytdlp(url, run_dir, max_mb, cookies_from_browser)
    if cookies_from_browser:
        raise RuntimeError("指定了浏览器登录态，但未安装 yt-dlp")
    return download_mobile_share(url, run_dir, max_mb)


def transcribe(audio_path, run_dir):
    if not audio_path.is_file():
        raise ValueError("音频文件不存在")
    required = ("funasr", "modelscope", "torch", "torchaudio")
    missing = [name for name in required if importlib.util.find_spec(name) is None]
    if missing:
        raise RuntimeError("缺少本地转写依赖：" + ", ".join(missing) + "；请优先使用已有转写服务")
    # Keep stdout a single JSON result even if the underlying libraries print logs.
    with redirect_stdout(sys.stderr):
        from funasr import AutoModel
        from funasr.utils.postprocess_utils import rich_transcription_postprocess
        model = AutoModel(model=MODEL, trust_remote_code=True, vad_model="fsmn-vad",
                          vad_kwargs={"max_single_segment_time": 30000}, device="cpu")
        result = model.generate(input=str(audio_path.resolve()), language="zh", use_itn=True, batch_size_s=60)
        parts = [rich_transcription_postprocess(item["text"]) for item in (result or []) if item.get("text")]
    text = "\n\n".join(part for part in parts if part.strip()).strip()
    if not text:
        raise RuntimeError("模型没有返回文字；不能据此确认视频无口播")
    output = run_dir / "transcript.txt"
    output.write_text(text, encoding="utf-8")
    return {"stage": "transcribed", "model": MODEL, "transcript_file": str(output.resolve()),
            "characters": len(text), "sha256": sha256(output), "human_verified": False,
            "note": "自动语音识别结果；已启用 ITN，未逐字校对"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("download", help="公开抖音链接转为本地音频，不转写")
    fetch.add_argument("source")
    fetch.add_argument("--output-dir", type=Path, required=True)
    fetch.add_argument("--max-mb", type=float, default=256)
    fetch.add_argument("--cookies-from-browser", help="仅在当前浏览器会话可访问时传给 yt-dlp，例如 chrome")
    asr = commands.add_parser("transcribe", help="已有依赖时使用本地 SenseVoice 转写")
    asr.add_argument("audio", type=Path)
    asr.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "download" and not (0 < args.max_mb <= 8192):
        parser.error("--max-mb 必须在 0 到 8192 之间")
    run_dir = None
    result = {"created_at": datetime.now(timezone.utc).isoformat(), "operation": args.command}
    try:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        run_dir = Path(tempfile.mkdtemp(prefix=args.command + "-", dir=args.output_dir)).resolve()
        result["run_dir"] = str(run_dir)
        data = download(args.source, run_dir, args.max_mb, args.cookies_from_browser) if args.command == "download" else transcribe(args.audio, run_dir)
        result.update(data, ok=True)
    except Exception as exc:
        # Do not expose signed CDN URLs from HTTP exceptions in durable records.
        if isinstance(exc, subprocess.CalledProcessError):
            error = "FFmpeg 未能读取媒体或提取音轨"
        elif isinstance(exc, (ValueError, RuntimeError)):
            error = str(exc)
        else:
            error = f"{type(exc).__name__}：处理失败，保留本次记录后检查网络和环境"
        result.update(ok=False, error=error)
    if run_dir:
        (run_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
