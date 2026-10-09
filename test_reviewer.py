"""Tests for reviewer pure helpers."""
import unittest
from pathlib import Path
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
            reviewer._maybe_auto_merge(self.CFG, "http://gl", "tok", "g/p", 10, "http://gl/10",
                                       reviewed_sha="h")
            return gl

    GREEN = {"detailed_merge_status": "mergeable", "head_pipeline": {"status": "success"},
             "sha": "h"}

    def test_merges_when_clean_and_green(self):
        gl = self._run(self.GREEN)
        gl.merge_mr.assert_called_once()

    def test_merge_is_pinned_to_reviewed_sha(self):
        gl = self._run(self.GREEN)
        self.assertEqual(gl.merge_mr.call_args.kwargs["sha"], "h")

    def test_skips_when_pushed_after_review(self):
        gl = self._run({**self.GREEN, "sha": "pushed-later"})
        gl.merge_mr.assert_not_called()

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
            reviewer._maybe_auto_merge(self.CFG, "http://gl", "tok", "g/p", 10, "http://gl/10",
                                       reviewed_sha="h")
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
             mock.patch.object(reviewer, "_slack_say") as say, \
             mock.patch("slack_client.add_reaction"):
            gl.get_mr.return_value = {"detailed_merge_status": "mergeable",
                                      "head_pipeline": {"status": "success"}, "sha": "h"}
            reviewer._maybe_auto_merge(self.BOT_CFG, "http://gl", "tok", "g/p", 10,
                                       "http://gl/10", thread_ts="123.45", reviewed_sha="h")
        self.assertTrue(say.call_args_list)  # at least one message emitted
        for call in say.call_args_list:
            self.assertEqual(call.args[2], "123.45")  # every one threaded


class TestMergedReaction(unittest.TestCase):
    BOT_CFG = {"slack": {"bot_token": "xoxb", "channel_id": "C1"}}
    GREEN = {"detailed_merge_status": "mergeable", "head_pipeline": {"status": "success"},
             "sha": "h"}

    def _merge(self, mr, thread_ts="123.45", react_side_effect=None):
        with mock.patch.object(reviewer, "gitlab_client") as gl, \
             mock.patch.object(reviewer, "_slack_say"), \
             mock.patch("slack_client.add_reaction", side_effect=react_side_effect) as react:
            gl.get_mr.return_value = mr
            reviewer._maybe_auto_merge(self.BOT_CFG, "http://gl", "tok", "g/p", 10,
                                       "http://gl/10", thread_ts=thread_ts, reviewed_sha="h")
        return react

    def test_marks_notification_done_after_merge(self):
        react = self._merge(self.GREEN)
        react.assert_called_once_with("xoxb", "C1", "123.45", "done")

    def test_falls_back_when_workspace_lacks_done_emoji(self):
        react = self._merge(self.GREEN, react_side_effect=[
            RuntimeError("Slack reactions.add failed: invalid_name"), None])
        self.assertEqual([c.args[3] for c in react.call_args_list], ["done", "white_check_mark"])

    def test_no_reaction_when_merge_blocked(self):
        react = self._merge({**self.GREEN, "draft": True})
        react.assert_not_called()

    def test_no_reaction_without_notification_ts(self):
        react = self._merge(self.GREEN, thread_ts=None)
        react.assert_not_called()

    def test_reaction_failure_is_swallowed(self):
        self._merge(self.GREEN, react_side_effect=RuntimeError("missing_scope"))


class TestVerdictImage(unittest.TestCase):
    BOT_CFG = {"slack": {"bot_token": "xoxb", "channel_id": "C1"}}

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "no_bug").mkdir()
        (self.dir / "no_bug" / "any name.jpeg").write_bytes(b"img")

    def tearDown(self):
        self.tmp.cleanup()

    def test_uploads_image_with_completion_text(self):
        with mock.patch.object(reviewer, "VERDICT_DIR", self.dir), \
             mock.patch("slack_client.upload_file") as up, \
             mock.patch("slack_client.chat_post_message") as msg:
            reviewer._slack_say_verdict(self.BOT_CFG, "done", [], thread_ts="1.2")
        up.assert_called_once_with("xoxb", "C1", "any name.jpeg", b"img",
                                   initial_comment="done", thread_ts="1.2")
        msg.assert_not_called()

    def test_falls_back_to_text_when_upload_fails(self):
        with mock.patch.object(reviewer, "VERDICT_DIR", self.dir), \
             mock.patch("slack_client.upload_file", side_effect=RuntimeError("missing_scope")), \
             mock.patch("slack_client.chat_post_message") as msg:
            reviewer._slack_say_verdict(self.BOT_CFG, "done", [], thread_ts="1.2")
        msg.assert_called_once_with("xoxb", "C1", "done", "1.2")

    def test_text_only_when_no_image_for_tier(self):
        with mock.patch.object(reviewer, "VERDICT_DIR", self.dir), \
             mock.patch("slack_client.upload_file") as up, \
             mock.patch("slack_client.chat_post_message") as msg:
            reviewer._slack_say_verdict(self.BOT_CFG, "done", [{"severity": "high"}])
        up.assert_not_called()
        msg.assert_called_once_with("xoxb", "C1", "done", None)

    BTN_CFG = {"slack": {"bot_token": "xoxb", "channel_id": "C1", "app_token": "xapp"}}
    BUTTONS = [{"type": "button", "action_id": "mrs:rerun:auto"}]

    def test_buttons_split_image_and_text(self):
        """A file share cannot hold buttons: image goes up bare, text+buttons follow."""
        with mock.patch.object(reviewer, "VERDICT_DIR", self.dir), \
             mock.patch.object(reviewer, "socket_listener_alive", return_value=True), \
             mock.patch("slack_client.upload_file") as up, \
             mock.patch("slack_client.chat_post_message") as msg:
            reviewer._slack_say_verdict(self.BTN_CFG, "done", [], "1.2", self.BUTTONS)
        self.assertEqual(up.call_args.kwargs["initial_comment"], "")
        sent = msg.call_args.kwargs["blocks"]
        self.assertEqual([b["type"] for b in sent], ["section", "actions"])
        self.assertEqual(msg.call_args.args[:4], ("xoxb", "C1", "done", "1.2"))

    def test_buttons_dropped_without_app_token(self):
        """No socket listener -> a click could not be answered, so show none."""
        with mock.patch.object(reviewer, "VERDICT_DIR", self.dir / "nope"), \
             mock.patch("slack_client.chat_post_message") as msg:
            reviewer._slack_say_verdict(self.BOT_CFG, "done", [], "1.2", self.BUTTONS)
        msg.assert_called_once_with("xoxb", "C1", "done", "1.2")

    def test_buttons_dropped_when_listener_heartbeat_is_stale(self):
        """Token configured but the socket listener is down: no dead buttons."""
        with mock.patch.object(reviewer, "VERDICT_DIR", self.dir / "nope"), \
             mock.patch.object(reviewer, "socket_listener_alive", return_value=False), \
             mock.patch("slack_client.chat_post_message") as msg:
            reviewer._slack_say_verdict(self.BTN_CFG, "done", [], "1.2", self.BUTTONS)
        msg.assert_called_once_with("xoxb", "C1", "done", "1.2")

    def test_heartbeat_freshness(self):
        beat = self.dir / ".socket-alive"
        self.assertFalse(reviewer.socket_listener_alive(beat))          # never written
        beat.touch()
        mtime = beat.stat().st_mtime
        self.assertTrue(reviewer.socket_listener_alive(beat, now=mtime + 10))
        self.assertFalse(reviewer.socket_listener_alive(beat, now=mtime + 600))

    def test_text_only_when_dir_missing(self):
        with mock.patch.object(reviewer, "VERDICT_DIR", self.dir / "nope"), \
             mock.patch("slack_client.upload_file") as up, \
             mock.patch("slack_client.chat_post_message"):
            reviewer._slack_say_verdict(self.BOT_CFG, "done", [])
        up.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class TestDeepConfirmation(unittest.TestCase):
    """A clean lite pass is one gate; auto-merge needs the 3-gate deep pass to agree."""
    CLEAN, DIRTY = {"findings": []}, {"findings": [{"severity": "low"}]}

    def _run(self, mode, auto_merge, outputs):
        calls = []
        def run(m):
            calls.append(m)
            return outputs[m]
        return reviewer._review_with_confirmation(run, mode, auto_merge), calls

    def test_clean_lite_escalates_to_deep_when_auto_merge_on(self):
        (result, mode), calls = self._run("lite", True, {"lite": self.CLEAN, "deep": self.CLEAN})
        self.assertEqual(calls, ["lite", "deep"])
        self.assertEqual((result, mode), (self.CLEAN, "deep"))

    def test_deep_findings_replace_clean_lite(self):
        (result, mode), _ = self._run("lite", True, {"lite": self.CLEAN, "deep": self.DIRTY})
        self.assertEqual((result, mode), (self.DIRTY, "deep"))

    def test_no_escalation_when_auto_merge_off(self):
        (_, mode), calls = self._run("lite", False, {"lite": self.CLEAN})
        self.assertEqual((calls, mode), (["lite"], "lite"))

    def test_no_escalation_when_lite_has_findings(self):
        (_, _), calls = self._run("lite", True, {"lite": self.DIRTY})
        self.assertEqual(calls, ["lite"])

    def test_failed_deep_keeps_lite_result_and_blocks_merge(self):
        (result, mode), _ = self._run("lite", True, {"lite": self.CLEAN, "deep": None})
        self.assertEqual((result, mode), (self.CLEAN, "lite"))

    def test_failed_first_pass_returns_none(self):
        (result, _), _ = self._run("deep", True, {"deep": None})
        self.assertIsNone(result)


class TestAutoMergeCiDetection(unittest.TestCase):
    NO_PIPELINE = {"detailed_merge_status": "mergeable", "sha": "h"}

    def _run(self, has_ci):
        with mock.patch.object(reviewer, "gitlab_client") as gl, \
             mock.patch.object(reviewer, "_slack_say"):
            gl.get_mr.return_value = self.NO_PIPELINE
            if isinstance(has_ci, Exception):
                gl.has_ci_config.side_effect = has_ci
            else:
                gl.has_ci_config.return_value = has_ci
            reviewer._maybe_auto_merge({"slack": {}}, "http://gl", "tok", "g/p", 10,
                                       "http://gl/10", reviewed_sha="h")
        return gl

    def test_project_without_ci_merges_on_mergeability(self):
        self._run(False).merge_mr.assert_called_once()

    def test_project_with_ci_but_no_pipeline_is_blocked(self):
        self._run(True).merge_mr.assert_not_called()

    def test_ci_lookup_failure_blocks(self):
        self._run(RuntimeError("500")).merge_mr.assert_not_called()
