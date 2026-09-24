"""Offline tests for resumable Feishu publishing; no cloud calls are made."""

import hashlib
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = (Path(__file__).resolve().parents[1] / "xiaowei-douyin-to-feishu" /
          "scripts" / "publish_feishu.py")
SPEC = importlib.util.spec_from_file_location("publish_feishu", SCRIPT)
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)


def cli_response(arguments, payload, returncode=0):
    return subprocess.CompletedProcess(arguments, returncode, json.dumps(payload, ensure_ascii=False), "")


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.transcript = "你好。你好。\n[原话] *强调* $99，不能漏字。"
        (self.base / "transcript.txt").write_text(self.transcript, encoding="utf-8")
        self.path = self.base / "run.json"
        self.run = {
            "video_id": "123456", "source_url_canonical": "https://www.douyin.com/video/123456",
            "title": "测试视频", "author": "作者", "published_at": "2026-09-24",
            "created_at": "2026-09-24T12:00:00+08:00", "transcript_file": "transcript.txt",
            "transcript_status": "complete", "method": "本地测试转写",
            "transcript_characters": len(self.transcript),
            "transcript_sha256": hashlib.sha256(self.transcript.encode()).hexdigest(),
            "unrelated_field": "必须保留",
        }
        self.write_run()

    def tearDown(self):
        self.temporary.cleanup()

    def write_run(self):
        self.path.write_text(json.dumps(self.run, ensure_ascii=False), encoding="utf-8")

    def saved(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def fetched_content(self):
        return (self.base / "document.md").read_text(encoding="utf-8")

    def existing_content(self, transcript=None):
        if transcript is None:
            transcript = self.transcript
        return (f"来源：[抖音原视频]({self.run['source_url_canonical']})\n"
                f"视频 ID：{self.run['video_id']}\n\n### 逐字稿\n\n{transcript}")

    def test_create_then_full_readback_saves_response_and_verifies(self):
        calls = []

        def fake_run(arguments, **kwargs):
            calls.append(arguments)
            if "+create" in arguments:
                # The durable attempt marker must precede the external write.
                self.assertEqual(self.saved()["feishu_write_status"], "create_result_unknown")
                self.assertTrue((self.base / "document.md").exists())
                return cli_response(arguments, {"ok": True, "data": {"document": {
                    "url": "https://example.feishu.cn/docx/real", "document_id": "real"}}})
            # The returned URL and whole response must be saved before fetch.
            self.assertEqual(self.saved()["feishu_doc"], "https://example.feishu.cn/docx/real")
            self.assertIn("feishu_create_response", self.saved())
            return cli_response(arguments, {"ok": True, "data": {"document": {
                "content": self.fetched_content()}}})

        with patch.object(publisher.subprocess, "run", side_effect=fake_run):
            result = publisher.publish(self.path)
        self.assertTrue(result["ok"])
        self.assertEqual(self.saved()["feishu_write_status"], "verified_full_text")
        self.assertTrue(self.saved()["feishu_readback_matches_transcript"])
        self.assertEqual(self.saved()["unrelated_field"], "必须保留")
        self.assertEqual(["+create", "+fetch"], [a[2] for a in calls])
        self.assertEqual(calls[1][calls[1].index("--doc") + 1], "https://example.feishu.cn/docx/real")

    def test_existing_document_fetches_without_creating(self):
        self.run["feishu_doc"] = "https://example.feishu.cn/docx/existing"
        self.write_run()

        def fake_run(arguments, **kwargs):
            self.assertIn("+fetch", arguments)
            return cli_response(arguments, {"ok": True, "data": {"document": {
                "content": self.existing_content()}}})

        with patch.object(publisher.subprocess, "run", side_effect=fake_run) as command:
            publisher.publish(self.path)
        command.assert_called_once()
        self.assertEqual(self.saved()["feishu_write_status"], "verified_full_text")
        self.assertFalse((self.base / "document.md").exists())

    def test_uncertain_create_never_retries_without_reconciliation(self):
        with patch.object(publisher.subprocess, "run", side_effect=OSError("connection lost")) as command:
            with self.assertRaisesRegex(publisher.PublishError, "创建结果不确定"):
                publisher.publish(self.path)
        command.assert_called_once()
        self.assertEqual(self.saved()["feishu_write_status"], "create_result_unknown")
        with patch.object(publisher.subprocess, "run") as command:
            with self.assertRaisesRegex(publisher.PublishError, "先前创建结果不确定"):
                publisher.publish(self.path)
        command.assert_not_called()

    def test_created_document_is_reused_after_fetch_error(self):
        def first_attempt(arguments, **kwargs):
            if "+create" in arguments:
                return cli_response(arguments, {"ok": True, "data": {"document": {
                    "url": "https://example.feishu.cn/docx/real"}}})
            raise OSError("fetch unavailable")

        with patch.object(publisher.subprocess, "run", side_effect=first_attempt):
            with self.assertRaisesRegex(publisher.PublishError, "回读待完成"):
                publisher.publish(self.path)
        self.assertEqual(self.saved()["feishu_doc"], "https://example.feishu.cn/docx/real")
        self.assertEqual(self.saved()["feishu_write_status"], "verification_pending")

        def second_attempt(arguments, **kwargs):
            self.assertIn("+fetch", arguments)
            return cli_response(arguments, {"ok": True, "data": {"document": {
                "content": self.fetched_content()}}})

        with patch.object(publisher.subprocess, "run", side_effect=second_attempt) as command:
            publisher.publish(self.path)
        command.assert_called_once()
        self.assertEqual(self.saved()["feishu_write_status"], "verified_full_text")

    def test_readback_mismatch_keeps_document_for_repair(self):
        self.run["feishu_doc"] = "https://example.feishu.cn/docx/existing"
        self.write_run()
        with patch.object(publisher.subprocess, "run", side_effect=lambda args, **kw: cli_response(
                args, {"ok": True, "data": {"document": {"content": self.existing_content("只有一半")}}})):
            with self.assertRaisesRegex(publisher.PublishError, "不一致"):
                publisher.publish(self.path)
        self.assertEqual(self.saved()["feishu_doc"], "https://example.feishu.cn/docx/existing")
        self.assertEqual(self.saved()["feishu_write_status"], "verification_failed")

    def test_readback_wrong_video_identity_stops_verification(self):
        self.run["feishu_doc"] = "https://example.feishu.cn/docx/other-video"
        self.write_run()
        content = self.existing_content().replace("视频 ID：123456", "视频 ID：654321")
        with patch.object(publisher.subprocess, "run", side_effect=lambda args, **kw: cli_response(
                args, {"ok": True, "data": {"document": {"content": content}}})):
            with self.assertRaisesRegex(publisher.PublishError, "视频 ID 或来源链接"):
                publisher.publish(self.path)
        self.assertEqual(self.saved()["feishu_write_status"], "verification_pending")

    def test_changed_local_source_stops_before_any_cloud_call(self):
        (self.base / "transcript.txt").write_text("别人修改过", encoding="utf-8")
        with patch.object(publisher.subprocess, "run") as command:
            with self.assertRaisesRegex(publisher.PublishError, "SHA-256 不一致"):
                publisher.publish(self.path)
        command.assert_not_called()

    def test_reconcile_unknown_create_with_confirmed_doc(self):
        self.run.update(feishu_create_attempted_at="2026-09-24T00:00:00+00:00",
                        feishu_write_status="create_result_unknown")
        self.write_run()
        with patch.object(publisher.subprocess, "run", side_effect=lambda args, **kw: cli_response(
                args, {"ok": True, "data": {"document": {
                    "content": self.existing_content()}}})) as command:
            publisher.publish(self.path, existing_doc="https://example.feishu.cn/docx/found")
        command.assert_called_once()
        self.assertEqual(self.saved()["feishu_doc"], "https://example.feishu.cn/docx/found")
        self.assertEqual(self.saved()["feishu_write_status"], "verified_full_text")


if __name__ == "__main__":
    unittest.main()
