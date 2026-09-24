"""Offline regression tests. These do not prove availability of Douyin or Feishu."""

import copy
import hashlib
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "xiaowei-douyin-to-feishu"


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


audio = module("audio_fallback")
document = module("build_document")


class Response(io.BytesIO):
    def __init__(self, body, length=None):
        super().__init__(body)
        self.headers = {} if length is None else {"Content-Length": str(length)}


class AudioTests(unittest.TestCase):
    def test_share_text_and_direct_variants(self):
        self.assertEqual(audio.source_url("复制打开抖音 https://v.douyin.com/abc/ 看视频"), "https://v.douyin.com/abc/")
        for url in ["https://www.douyin.com/video/123456", "https://www.iesdouyin.com/share/video/123456/", "https://www.douyin.com/?modal_id=123456"]:
            self.assertEqual(audio.video_id_from_url(url), "123456")

    def test_reject_ambiguous_or_foreign_sources(self):
        for text in ["https://evil.example/video/123", "https://douyin.com.evil.example/video/123",
                     "https://v.douyin.com/a/ https://v.douyin.com/b/", "https://user:pass@douyin.com/video/123"]:
            with self.assertRaises(ValueError):
                audio.source_url(text)

    def test_final_redirect_id_used(self):
        class RedirectResponse(Response):
            def geturl(self):
                return "https://www.iesdouyin.com/share/video/98765/"
        with patch.object(audio, "open_public", return_value=RedirectResponse(b"")):
            self.assertEqual(audio.resolve_video_id("https://v.douyin.com/example/"), "98765")

    def test_semicolon_and_script_suffix_parse_without_execution(self):
        payload = {"loaderData": {"video_(id)/page": {"videoInfoRes": {"item_list": [
            {"desc": "重复也保留", "video": {"play_addr": {"url_list": ["https://cdn.example/playwm/?token=abc"]}}}
        ]}}}}
        html = "<script>window._ROUTER_DATA = " + json.dumps(payload) + ";window.other=1;</script>"
        parsed = audio.parse_share_page(html)
        self.assertEqual(parsed["title"], "重复也保留")
        self.assertEqual(parsed["video_url"], "https://cdn.example/playwm/?token=abc")

    def test_invalid_share_data_reports_failure(self):
        for html in ["login", "window._ROUTER_DATA = {};", "window._ROUTER_DATA = invalid"]:
            with self.assertRaises(ValueError):
                audio.parse_share_page(html)

    def test_private_destination_rejected(self):
        with patch.object(audio.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(ValueError):
                audio.validate_public_url("https://example.test/file")

    def test_redirect_private_destination_rejected(self):
        with patch.object(audio.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("192.168.1.1", 443))]):
            with self.assertRaises(ValueError):
                audio.PublicRedirects().redirect_request(None, None, 302, "Found", {}, "https://example.test/file")

    def test_stream_size_and_empty_result(self):
        with tempfile.TemporaryDirectory() as temp:
            for i, data in enumerate([b"", b"123456"]):
                with self.assertRaises(ValueError):
                    audio.copy_limited(Response(data), Path(temp) / str(i), 5)

    def test_content_length_limit_prevents_file_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "video"
            with self.assertRaises(ValueError):
                audio.copy_limited(Response(b"123456", 6), destination, 5)
            self.assertFalse(destination.exists())

    def test_failure_is_json_and_is_saved(self):
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()) as output:
            status = audio.main(["transcribe", str(Path(temp) / "missing.wav"), "--output-dir", temp])
            result = json.loads(output.getvalue())
            self.assertEqual(status, 1)
            self.assertFalse(result["ok"])
            self.assertEqual(json.loads((Path(result["run_dir"]) / "result.json").read_text()), result)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg unavailable")
    def test_real_ffmpeg_converts_synthetic_audio(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "synthetic.wav"
            destination = Path(temp) / "audio.wav"
            with wave.open(str(source), "wb") as stream:
                stream.setnchannels(2)
                stream.setsampwidth(2)
                stream.setframerate(44100)
                stream.writeframes(b"\0" * 44100 * 4)
            audio.extract_audio(source, destination)
            with wave.open(str(destination), "rb") as stream:
                self.assertEqual(stream.getframerate(), 16000)
                self.assertEqual(stream.getnchannels(), 1)
                self.assertGreater(stream.getnframes(), 0)


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.text = "获取数据。获取数据。\n短口播没有句号\n<img href='https://example.test'> &amp; [文字](链接) *强调* $99 C:\\test\n1. 真的编号"
        (self.base / "transcript.txt").write_text(self.text, encoding="utf-8")
        self.batch = {"title": "测试文档", "collected_at": "2026-09-24T12:00:00+08:00", "videos": [
            {"video_id": "123456", "source_url": "https://www.douyin.com/video/123456",
             "title": "测试", "method": "离线测试素材", "status": "complete", "summary": [],
             "transcript_file": "transcript.txt"}
        ]}

    def tearDown(self):
        self.temporary.cleanup()

    def test_original_text_hash_and_roundtrip_preserved(self):
        body, manifest = document.render(self.batch, self.base)
        segment = body.split("### 逐字稿\n\n", 1)[1].strip()
        unescaped = re.sub(r"\\([\\`*_\[\]$~<>#+\-=.!|()&])", r"\1", segment)
        self.assertEqual(unescaped, self.text)
        self.assertEqual(manifest["videos"][0]["sha256"], hashlib.sha256(self.text.encode()).hexdigest())
        self.assertEqual(manifest["feishu_write_status"], "not_started")

    def test_short_transcript_is_allowed(self):
        (self.base / "transcript.txt").write_text("今天先到这里", encoding="utf-8")
        body, _ = document.render(self.batch, self.base)
        self.assertIn("今天先到这里", body)

    def test_reject_empty_success(self):
        (self.base / "transcript.txt").write_text(" \n", encoding="utf-8")
        with self.assertRaises(ValueError):
            document.render(self.batch, self.base)

    def test_reject_duplicates(self):
        self.batch["videos"].append(copy.deepcopy(self.batch["videos"][0]))
        with self.assertRaises(ValueError):
            document.render(self.batch, self.base)

    def test_reject_wrong_video_source(self):
        self.batch["videos"][0]["source_url"] = "https://www.douyin.com/video/99999"
        with self.assertRaises(ValueError):
            document.render(self.batch, self.base)

    def test_partial_requires_and_displays_reason(self):
        item = self.batch["videos"][0]
        item["status"] = "partial"
        with self.assertRaises(ValueError):
            document.render(self.batch, self.base)
        item["note"] = "后半段未取回"
        body, _ = document.render(self.batch, self.base)
        self.assertIn("已取得的逐字稿（不完整）", body)
        self.assertIn("后半段未取回", body)

    def test_failure_cannot_include_transcript_or_summary(self):
        item = self.batch["videos"][0]
        item.update(status="failed", note="登录限制")
        with self.assertRaises(ValueError):
            document.render(self.batch, self.base)
        item.pop("transcript_file")
        item["summary"] = ["不应该出现的内容概括"]
        with self.assertRaises(ValueError):
            document.render(self.batch, self.base)
        item["summary"] = []
        body, _ = document.render(self.batch, self.base)
        self.assertNotIn("### 逐字稿", body)

    def test_no_speech_requires_evidence(self):
        item = self.batch["videos"][0]
        item.update(status="no_speech")
        item.pop("transcript_file")
        with self.assertRaises(ValueError):
            document.render(self.batch, self.base)
        item["note"] = "测试素材：已检查完整音轨，无人声"
        body, _ = document.render(self.batch, self.base)
        self.assertIn("确认无口播", body)

    def test_existing_output_preserved(self):
        metadata = self.base / "batch.json"
        metadata.write_text(json.dumps(self.batch), encoding="utf-8")
        output = self.base / "existing.md"
        output.write_text("用户原内容", encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            code = document.main([str(metadata), "--output", str(output)])
        self.assertEqual(code, 1)
        self.assertEqual(output.read_text(), "用户原内容")

    def test_cli_artifacts_generated(self):
        metadata = self.base / "batch.json"
        metadata.write_text(json.dumps(self.batch), encoding="utf-8")
        output = self.base / "document.md"
        result = subprocess.run([sys.executable, str(ROOT / "scripts/build_document.py"), str(metadata), "--output", str(output)], capture_output=True, text=True, check=True)
        self.assertTrue(json.loads(result.stdout)["ok"])
        manifest = json.loads(output.with_suffix(".md.manifest.json").read_text())
        self.assertEqual(manifest["document_sha256"], hashlib.sha256(output.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
