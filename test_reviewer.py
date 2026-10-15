"""Tests for reviewer pure helpers."""
import tempfile
import unittest
import urllib.error
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


class TestMainCrashReport(unittest.TestCase):
    """A claimed MR is never retried by the poller: every crash must be retried once
    automatically, and after that reach Slack with a manual retry button."""

    def _main(self, exc, argv_extra=(), reset=True):
        said = []
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(reviewer, "REVIEWS_DIR", Path(d)), \
             mock.patch.object(reviewer, "SCRIPT_DIR", Path(d)), \
             mock.patch.object(reviewer, "load_config", return_value={}), \
             mock.patch.object(reviewer, "load_state", return_value={}), \
             mock.patch.object(reviewer, "run_review", side_effect=exc), \
             mock.patch.object(reviewer, "reset_for_rerun", return_value=reset) as reset_mock, \
             mock.patch.object(reviewer, "spawn_detached") as spawn, \
             mock.patch.object(reviewer, "_slack_say",
                               side_effect=lambda cfg, text, ts=None, b=None: said.append((text, b))), \
             mock.patch("sys.argv", ["reviewer.py", "--project", "g/app", "--iid", "5",
                                     "--mr-id", "100", *argv_extra]), \
             self.assertLogs("mr_sentinel.reviewer", "ERROR"):
            rc = reviewer.main()
        return rc, said, reset_mock, spawn

    def test_first_crash_retries_automatically(self):
        rc, said, reset, spawn = self._main(
            urllib.error.HTTPError("u", 500, "Internal Server Error", {}, None))
        self.assertEqual(rc, 1)
        reset.assert_called_once()
        spawn.assert_called_once_with("g/app", "5", "100", "auto", attempt=1)
        self.assertIn("自動重試", said[-1][0])
        self.assertIn("HTTPError", said[-1][0])

    def test_retry_keeps_the_requested_depth(self):
        _, _, _, spawn = self._main(RuntimeError("boom"), ("--mode", "deep"))
        spawn.assert_called_once_with("g/app", "5", "100", "deep", attempt=1)

    def test_negative_attempt_cannot_extend_the_retry_budget(self):
        _, _, reset, spawn = self._main(RuntimeError("boom"), ("--attempt", "-5"))
        spawn.assert_called_once_with("g/app", "5", "100", "auto", attempt=1)

    def test_crash_after_the_retry_is_reported_with_button(self):
        _, said, reset, spawn = self._main(RuntimeError("boom"), ("--attempt", "1"))
        reset.assert_not_called()
        spawn.assert_not_called()
        text, buttons = said[-1]
        self.assertIn("中途失敗", text)
        self.assertIn("RuntimeError", text)
        self.assertTrue(buttons)

    def test_incomplete_reset_reports_instead_of_retrying(self):
        """Old comments kept (history write failed) -> a retry would post a second round."""
        _, said, _, spawn = self._main(RuntimeError("boom"), reset=False)
        spawn.assert_not_called()
        self.assertTrue(said[-1][1])

    def test_failed_reset_reports_instead_of_retrying(self):
        said = []
        with mock.patch.object(reviewer, "reset_for_rerun", side_effect=OSError("gitlab down")), \
             mock.patch.object(reviewer, "spawn_detached") as spawn, \
             mock.patch.object(reviewer, "_slack_say",
                               side_effect=lambda cfg, text, ts=None, b=None: said.append((text, b))), \
             self.assertLogs("mr_sentinel.reviewer", "ERROR"):
            self.assertFalse(reviewer._retry_automatically(
                {}, {}, "g/app", 5, 100, 0, RuntimeError("x")))
        spawn.assert_not_called()
        self.assertEqual(said, [])

    def test_dry_run_stays_off_slack_and_never_retries(self):
        _, said, reset, _ = self._main(RuntimeError("boom"), ("--dry-run",))
        self.assertEqual(said, [])
        reset.assert_not_called()


class TestCrashAfterCompletion(unittest.TestCase):
    """Once the completion message is out, nothing after it may read as a failed
    review: main() would answer by deleting the comments and reviewing again."""

    def _run_review(self, merge_side_effect=None, cleanup_side_effect=None):
        cfg = {"gitlab_url": "http://gl", "gitlab_token": "t",
               "review": {"language": "zh", "engine": "claude", "auto_merge_on_clean": True}}
        ctx = {"diff_refs": {"head_sha": "h"}, "stats": {"files": 1, "lines": 1}, "web_url": "u"}
        clean = {"findings": []}
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(reviewer, "REVIEWS_DIR", Path(d)), \
             mock.patch.object(reviewer, "gitlab_client") as gl, \
             mock.patch.object(reviewer, "fetch_mr") as fm, \
             mock.patch.object(reviewer, "engines"), \
             mock.patch.object(reviewer, "post_comment") as pc, \
             mock.patch.object(reviewer, "history_sync"), \
             mock.patch.object(reviewer, "history_rate"), \
             mock.patch.object(reviewer, "prepare_worktree", return_value=("/clone", None)), \
             mock.patch.object(reviewer, "_review_with_confirmation", return_value=(clean, "deep")), \
             mock.patch.object(reviewer, "_slack_say_verdict") as announced, \
             mock.patch.object(reviewer, "_maybe_auto_merge", side_effect=merge_side_effect), \
             mock.patch.object(reviewer, "_run_git", side_effect=cleanup_side_effect):
            gl.get_current_user.return_value = {"id": 7}
            gl.get_award_emojis.return_value = []
            fm.build_context.return_value = ctx
            pc.post_findings.return_value = 0
            with self.assertLogs("mr_sentinel.reviewer", "ERROR"):
                rc = reviewer.run_review("g/app", 5, 100, cfg, {}, False)
        return rc, announced

    def test_auto_merge_crash_after_the_announcement_is_not_a_failed_review(self):
        rc, announced = self._run_review(merge_side_effect=RuntimeError("boom"))
        self.assertEqual(rc, 0)
        announced.assert_called_once()

    def test_worktree_cleanup_timeout_is_not_a_failed_review(self):
        import subprocess
        rc, announced = self._run_review(
            cleanup_side_effect=subprocess.TimeoutExpired("git", 120))
        self.assertEqual(rc, 0)
        announced.assert_called_once()


class TestUnusableConfig(unittest.TestCase):
    def test_config_failure_is_logged_not_raised(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(reviewer, "REVIEWS_DIR", Path(d)), \
             mock.patch.object(reviewer, "SCRIPT_DIR", Path(d)), \
             mock.patch.object(reviewer, "load_config", side_effect=SystemExit("config not found")), \
             mock.patch.object(reviewer, "run_review") as run, \
             mock.patch("sys.argv", ["reviewer.py", "--project", "g/app", "--iid", "5",
                                     "--mr-id", "100"]), \
             self.assertLogs("mr_sentinel.reviewer", "ERROR"):
            self.assertEqual(reviewer.main(), 1)
        run.assert_not_called()


class TestTakeLock(unittest.TestCase):
    def test_retry_waits_for_the_parent_to_release_the_lock(self):
        import fcntl
        with tempfile.NamedTemporaryFile() as a, open(a.name) as b:
            fcntl.flock(a, fcntl.LOCK_EX)
            with mock.patch.object(reviewer.time, "sleep",
                                   side_effect=lambda _: fcntl.flock(a, fcntl.LOCK_UN)):
                self.assertTrue(reviewer.take_lock(b, wait=5))

    def test_first_attempt_gives_up_at_once_when_a_review_is_running(self):
        import fcntl
        with tempfile.NamedTemporaryFile() as a, open(a.name) as b:
            fcntl.flock(a, fcntl.LOCK_EX)
            self.assertFalse(reviewer.take_lock(b))


class TestResetForRerun(unittest.TestCase):
    CFG = {"gitlab_url": "http://gl", "gitlab_token": "t"}

    def _run(self, recorded):
        with mock.patch.object(reviewer, "gitlab_client") as gl, \
             mock.patch.object(reviewer, "history_sync") as hs:
            gl.get_current_user.return_value = {"id": 7}
            gl.get_award_emojis.return_value = [
                {"id": 1, "name": "eyes", "user": {"id": 7}},
                {"id": 2, "name": "eyes", "user": {"id": 99}},
                {"id": 3, "name": "thumbsup", "user": {"id": 7}}]
            gl.list_discussions.return_value = [{"notes": [
                {"id": 50, "author": {"id": 7},
                 "body": "x " + reviewer.review_common.SIGNATURE_MARKER}]}]
            hs.sync_mr.return_value = recorded
            result = reviewer.reset_for_rerun(self.CFG, "g/app", 5)
        return result, gl

    def test_drops_only_our_eyes_and_our_unanswered_comments(self):
        result, gl = self._run(recorded=True)
        self.assertTrue(result)
        gl.delete_award_emoji.assert_called_once_with("http://gl", "t", "g/app", 5, 1)
        gl.delete_note.assert_called_once_with("http://gl", "t", "g/app", 5, 50)

    def test_touches_nothing_when_the_history_write_failed(self):
        result, gl = self._run(recorded=False)
        self.assertFalse(result)
        gl.delete_award_emoji.assert_not_called()
        gl.delete_note.assert_not_called()
