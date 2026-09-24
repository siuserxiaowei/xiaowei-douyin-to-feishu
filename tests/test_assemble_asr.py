"""Offline contract tests for the real mediakit ASR response schema."""

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = (Path(__file__).resolve().parents[1] / "xiaowei-douyin-to-feishu" /
          "scripts" / "assemble_asr.py")
SPEC = importlib.util.spec_from_file_location("assemble_asr", SCRIPT)
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


def asr_sample():
    # Same field names and status as a real mediakit `video asr-subtitles` result.
    return {
        "status": "completed", "task_id": "amk-tool-asr-subtitles-example",
        "request_id": "request-example", "task_type": "video_asr_subtitles",
        "duration": 7.1,  # ASR duration can differ from video/subtitle end.
        "subtitles": [
            {"start_time": 0.3, "end_time": 1.0, "subtitle_text": "第一句"},
            {"start_time": 1.0, "end_time": 1.8, "subtitle_text": "第二句"},
            {"start_time": 1.8, "end_time": 5.0, "subtitle_text": "第三句"},
            {"start_time": 5.0, "end_time": 8.45, "subtitle_text": "最后一句"},
        ],
    }


def metadata():
    return {
        "video_id": "123456789", "source_url_canonical": "https://www.douyin.com/video/123456789",
        "source_url_share": "https://v.douyin.com/example/", "title": "测试片段",
        "author": "测试作者", "published_at": "2026-09-24", "duration_seconds": 8.3,
        "description": "这是抖音页面中的原始文案 #话题",
        "method": "yt-dlp + mediakit Cloud ASR",
    }


class AssembleTests(unittest.TestCase):
    def test_completed_timeline_preserves_every_word_and_publisher_contract(self):
        run, transcript = bridge.assemble(asr_sample(), metadata())
        self.assertEqual(transcript, "第一句第二句第三句最后一句")
        self.assertEqual(run["transcript_status"], "complete")
        self.assertEqual(run["asr_coverage_status"], "timeline_covered")
        self.assertEqual(run["asr_segments"], 4)
        self.assertEqual(run["asr_task_id"], "amk-tool-asr-subtitles-example")
        self.assertEqual(run["description"], "这是抖音页面中的原始文案 #话题")
        self.assertEqual(run["asr_duration"], 7.1)
        self.assertFalse(run["human_verified"])
        self.assertEqual(run["transcript_file"], "transcript.txt")
        self.assertEqual(run["transcript_characters"], len(transcript))
        self.assertEqual(run["transcript_sha256"], hashlib.sha256(transcript.encode()).hexdigest())
        with tempfile.TemporaryDirectory() as temporary:
            path = bridge.write_outputs(Path(temporary) / "video-run", run, transcript)
            self.assertEqual(path.name, "run.json")
            self.assertEqual((path.parent / "transcript.txt").read_text(encoding="utf-8"), transcript)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), run)
            with self.assertRaisesRegex(bridge.AssembleError, "已有"):
                bridge.write_outputs(path.parent, run, transcript)

    def test_missing_middle_segment_is_partial_not_complete(self):
        result = asr_sample()
        del result["subtitles"][2]  # Leaves a 3.2-second gap in the timed words.
        run, transcript = bridge.assemble(result, metadata())
        self.assertEqual(transcript, "第一句第二句最后一句")
        self.assertEqual(run["transcript_status"], "partial")
        self.assertEqual(run["asr_coverage_status"], "unverified")
        self.assertIn("未覆盖", run["asr_coverage_issues"][0])

    def test_audio_download_metadata_records_composite_method_and_caption(self):
        details = {
            "stage": "audio_ready", "video_id": "123456789",
            "source_url_canonical": "https://www.douyin.com/video/123456789",
            "source_url_input": "https://v.douyin.com/example/",
            "title": "音频下载结果", "description": "原视频发布文案，不是口播逐字稿",
            "duration_seconds": 8.3, "method": "yt-dlp+ffmpeg",
            "transcript_status": "not_started",
        }
        run, transcript = bridge.assemble(asr_sample(), details)
        self.assertEqual(run["transcript_status"], "complete")
        self.assertEqual(run["audio_method"], "yt-dlp+ffmpeg")
        self.assertEqual(run["asr_method"], "mediakit-cli video asr-subtitles")
        self.assertIn("mediakit-cli video asr-subtitles", run["method"])
        self.assertIn("yt-dlp+ffmpeg", run["method"])
        self.assertEqual(run["description"], details["description"])
        self.assertEqual(run["source_url_input"], details["source_url_input"])
        self.assertNotEqual(run["description"], transcript)

    def test_unverified_video_duration_is_partial(self):
        details = metadata()
        del details["duration_seconds"]
        run, transcript = bridge.assemble(asr_sample(), details)
        self.assertIsNotNone(transcript)
        self.assertEqual(run["transcript_status"], "partial")
        self.assertIn("视频时长", run["asr_coverage_issues"][0])

    def test_failed_asr_records_failure_without_transcript(self):
        result = asr_sample()
        result["status"] = "failed"
        result["subtitles"] = []
        run, transcript = bridge.assemble(result, metadata())
        self.assertIsNone(transcript)
        self.assertEqual(run["transcript_status"], "failed")
        self.assertEqual(run["asr_status"], "failed")
        self.assertIsNone(run["transcript_file"])
        with tempfile.TemporaryDirectory() as temporary:
            path = bridge.write_outputs(temporary, run, transcript)
            self.assertTrue(path.exists())
            self.assertFalse((Path(temporary) / "transcript.txt").exists())

    def test_completed_but_empty_is_not_claimed_as_no_speech(self):
        result = asr_sample()
        result["subtitles"] = []
        run, transcript = bridge.assemble(result, metadata())
        self.assertIsNone(transcript)
        self.assertEqual(run["transcript_status"], "failed")
        self.assertIn("不能推断无口播", run["note"])

    def test_missing_task_id_is_unverified(self):
        result = asr_sample()
        del result["task_id"]
        run, transcript = bridge.assemble(result, metadata())
        self.assertIsNotNone(transcript)
        self.assertEqual(run["transcript_status"], "partial")
        self.assertIn("task_id", run["asr_coverage_issues"][0])

    def test_implausibly_long_single_segment_is_unverified(self):
        result = asr_sample()
        result["subtitles"] = [{"start_time": 0.2, "end_time": 40.1, "subtitle_text": "短句"}]
        details = metadata()
        details["duration_seconds"] = 40.0
        run, _ = bridge.assemble(result, details)
        self.assertEqual(run["transcript_status"], "partial")
        self.assertIn("漏段", run["asr_coverage_issues"][0])

    def test_unordered_subtitles_are_rejected(self):
        result = asr_sample()
        result["subtitles"][2], result["subtitles"][3] = result["subtitles"][3], result["subtitles"][2]
        with self.assertRaisesRegex(bridge.AssembleError, "乱序"):
            bridge.assemble(result, metadata())

    def test_video_identity_mismatch_is_rejected(self):
        details = metadata()
        details["video_id"] = "222"
        with self.assertRaisesRegex(bridge.AssembleError, "不一致"):
            bridge.assemble(asr_sample(), details)


if __name__ == "__main__":
    unittest.main()
