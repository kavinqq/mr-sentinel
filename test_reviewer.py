"""Tests for reviewer pure helpers."""
import unittest
from unittest import mock

import reviewer


class TestCompletionText(unittest.TestCase):
    FINDINGS = [{"severity": "high"}, {"severity": "high"}, {"severity": "low"}]

    def test_english_default(self):
        text = reviewer.completion_text("group/backend-app", 45, "https://gl/mr/45",
                                        self.FINDINGS, 3, language="en")
        self.assertIn("AI review complete", text)
        self.assertIn("group/backend-app", text)
        self.assertIn("!45", text)
        self.assertIn("🔴2", text)
        self.assertIn("🟠0", text)
        self.assertIn("🟡1", text)
        self.assertIn("https://gl/mr/45", text)

    def test_chinese_when_configured(self):
        text = reviewer.completion_text("group/backend-app", 45, None,
                                        self.FINDINGS, 3, language="zh-TW")
        self.assertIn("AI Review 完成", text)

    def test_signature_includes_engine_label(self):
        sig = reviewer.build_signature("scanned by X, vetted by Y")
        self.assertIn("mr-sentinel", sig)
        self.assertIn("scanned by X", sig)

    def test_completion_tags_lite_mode(self):
        text = reviewer.completion_text("g/p", 10, None, self.FINDINGS, 3,
                                        language="en", mode="lite")
        self.assertIn("[lite]", text.lower())

    def test_completion_tags_deep_mode(self):
        text = reviewer.completion_text("g/p", 10, None, self.FINDINGS, 3,
                                        language="en", mode="deep")
        self.assertIn("[deep]", text.lower())


class TestAutoMerge(unittest.TestCase):
    CFG = {"slack": {}}  # _slack_say no-ops with no bot_token/webhook -> no network

    def _run(self, mr):
        with mock.patch.object(reviewer, "gitlab_client") as gl:
            gl.get_mr.return_value = mr
            reviewer._maybe_auto_merge(self.CFG, "http://gl", "tok", "g/p", 10, "http://gl/10")
            return gl

    GREEN = {"detailed_merge_status": "mergeable", "head_pipeline": {"status": "success"}}

    def test_merges_when_clean_and_green(self):
        gl = self._run(self.GREEN)
        gl.merge_mr.assert_called_once()

    def test_skips_draft(self):
        gl = self._run({**self.GREEN, "draft": True})
        gl.merge_mr.assert_not_called()
        gl.post_note.assert_called_once()  # rail leaves a human-visible note

    def test_skips_red_pipeline(self):
        gl = self._run({"detailed_merge_status": "mergeable",
                        "head_pipeline": {"status": "failed"}})
        gl.merge_mr.assert_not_called()

    def test_skips_when_not_mergeable(self):
        gl = self._run({"detailed_merge_status": "conflict",
                        "head_pipeline": {"status": "success"}})
        gl.merge_mr.assert_not_called()

    def test_merge_api_error_is_swallowed(self):
        # a failed merge must warn, not crash the reviewer
        with mock.patch.object(reviewer, "gitlab_client") as gl:
            gl.get_mr.return_value = self.GREEN
            gl.merge_mr.side_effect = RuntimeError("409 conflict")
            reviewer._maybe_auto_merge(self.CFG, "http://gl", "tok", "g/p", 10, "http://gl/10")
            gl.merge_mr.assert_called_once()  # attempted, error contained


class TestSlackThreading(unittest.TestCase):
    """reviewer replies belong in the MR notification's thread (thread_ts carried through)."""
    BOT_CFG = {"slack": {"bot_token": "xoxb", "channel_id": "C1"}}

    def test_slack_say_threads_when_ts_given(self):
        with mock.patch("slack_client.chat_post_message") as m:
            reviewer._slack_say(self.BOT_CFG, "hi", thread_ts="123.45")
        m.assert_called_once_with("xoxb", "C1", "hi", "123.45")

    def test_slack_say_top_level_when_no_ts(self):
        with mock.patch("slack_client.chat_post_message") as m:
            reviewer._slack_say(self.BOT_CFG, "hi")
        m.assert_called_once_with("xoxb", "C1", "hi", None)

    def test_auto_merge_messages_carry_thread_ts(self):
        with mock.patch.object(reviewer, "gitlab_client") as gl, \
             mock.patch.object(reviewer, "_slack_say") as say:
            gl.get_mr.return_value = {"detailed_merge_status": "mergeable",
                                      "head_pipeline": {"status": "success"}}
            reviewer._maybe_auto_merge(self.BOT_CFG, "http://gl", "tok", "g/p", 10,
                                       "http://gl/10", thread_ts="123.45")
        self.assertTrue(say.call_args_list)  # at least one message emitted
        for call in say.call_args_list:
            self.assertEqual(call.args[2], "123.45")  # every one threaded


if __name__ == "__main__":
    unittest.main()
