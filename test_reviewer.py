"""Tests for reviewer pure helpers."""
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import review_common as rc
import reviewer

REAL_RELEASE_BLOCKER = reviewer._release_blocker
_gate_patch = mock.patch.object(reviewer, "_release_blocker", return_value=None)


def setUpModule():
    # auto-merge tests below are about the other rails; the release rail has its own class
    _gate_patch.start()


def tearDownModule():
    _gate_patch.stop()


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

    def test_an_earlier_comment_still_open_holds_a_clean_rerun(self):
        """The developer fixed some comments, answered one, the appeal rejected it, and
        the rerun came back clean: that answered comment still holds the merge."""
        ai = "🟠 [Medium] x\n\n" + rc.SIGNATURE_MARKER
        rejected = f"不成立 {rc.APPEAL_REJECT}\n\n{rc.SIGNATURE_MARKER}"
        with mock.patch.object(reviewer, "gitlab_client") as gl:
            gl.get_mr.return_value = self.GREEN
            gl.get_current_user.return_value = {"id": 42}
            gl.list_discussions.return_value = [{"id": "d1", "notes": [
                {"id": 1, "author": {"id": 42}, "body": ai, "resolvable": True},
                {"id": 2, "author": {"id": 99}, "body": "不用修"},
                {"id": 3, "author": {"id": 42}, "body": rejected}]}]
            merged, report, buttons, _ = reviewer.try_auto_merge(
                self.CFG, "http://gl", "tok", "g/p", 10, reviewed_sha="h")
        self.assertFalse(merged)
        gl.merge_mr.assert_not_called()
        self.assertIn("還有 1 則 review 留言沒處理完", report)
        self.assertIn("waive", str(buttons))

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
             mock.patch.object(reviewer.final_gates, "run", return_value=outcome()), \
             mock.patch.object(reviewer, "_set_status"), \
             mock.patch.object(reviewer, "_slack_say_verdict") as announced, \
             mock.patch.object(reviewer, "try_auto_merge", side_effect=merge_side_effect,
                               return_value=(True, "", [], [])), \
             mock.patch.object(reviewer, "_run_git", side_effect=cleanup_side_effect):
            gl.get_current_user.return_value = {"id": 7}
            gl.get_award_emojis.return_value = []
            fm.build_context.return_value = ctx
            pc.post_findings.return_value = 0
            with self.assertLogs("mr_sentinel.reviewer", "ERROR"):
                rc = reviewer.run_review("g/app", 5, 100, cfg, {}, False)
        return rc, announced

    def test_auto_merge_crash_is_not_a_failed_review(self):
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


GATE_HIT = {"severity": "high", "category": "correctness", "title": "t", "file": "a.tsx",
            "line": 3, "gate": "artifact", "hit_lines": [3]}
LEAK = {"severity": "high", "category": "correctness", "title": "UAT host", "file": "b.py",
        "line": 9, "gate": "release_guard"}


def outcome(rule=(), guard=(), broken=False):
    import final_gates
    return final_gates.Outcome(rule=list(rule), guard=None if broken else list(guard),
                               problem="guard failed" if broken else "")


class TestWithoutCovered(unittest.TestCase):
    def test_review_findings_on_gate_lines_are_duplicates(self):
        ai = [{"file": "a.tsx", "line": 3, "title": "dup of rule"},
              {"file": "b.py", "line": 9, "title": "dup of guard"},
              {"file": "c.py", "line": 1, "title": "real"}]
        kept = reviewer.without_covered(ai, [GATE_HIT, LEAK])
        self.assertEqual([f["title"] for f in kept], ["real"])


class ReviewHarness:
    CFG = {"gitlab_url": "http://gl", "gitlab_token": "t",
           "review": {"language": "zh", "engine": "claude", "auto_merge_on_clean": True}}
    CTX = {"diff_refs": {"head_sha": "h"}, "stats": {"files": 1, "lines": 1}, "web_url": "u",
           "changes": [], "target_branch": "master", "source_branch": "feat"}

    def patches(self, d, gates):
        self.stack = [
            mock.patch.object(reviewer, "REVIEWS_DIR", Path(d)),
            mock.patch.object(reviewer, "gitlab_client"),
            mock.patch.object(reviewer, "fetch_mr"),
            mock.patch.object(reviewer, "engines"),
            mock.patch.object(reviewer, "post_comment"),
            mock.patch.object(reviewer, "history_sync"),
            mock.patch.object(reviewer, "history_rate"),
            mock.patch.object(reviewer, "prepare_worktree", return_value=("/clone", None)),
            mock.patch.object(reviewer.final_gates, "run", return_value=gates),
            mock.patch.object(reviewer, "_slack_say"),
            mock.patch.object(reviewer, "_slack_say_verdict"),
            mock.patch.object(reviewer, "try_auto_merge", return_value=(True, "", [], [])),
            mock.patch.object(reviewer, "_mark_notification_merged"),
            mock.patch.object(reviewer, "_set_status"),
            mock.patch.object(reviewer, "_run_git"),
        ]
        m = {}
        for patcher in self.stack:
            m[patcher.attribute] = patcher.start()
            self.addCleanup(patcher.stop)
        m["gitlab_client"].get_current_user.return_value = {"id": 7}
        m["gitlab_client"].get_award_emojis.return_value = []
        m["gitlab_client"].list_discussions.return_value = []
        m["fetch_mr"].build_context.return_value = self.CTX
        m["gitlab_client"].get_project.return_value = {"default_branch": "master"}
        m["post_comment"].post_findings.side_effect = lambda *a, **k: len(a[4])
        return m

    @staticmethod
    def posted(m):
        return [f for c in m["post_comment"].post_findings.call_args_list for f in c.args[4]]

    @staticmethod
    def said(m):
        return " ".join(c.args[1] for c in m["_slack_say"].call_args_list)

    @staticmethod
    def statuses(m):
        return [c.args[5] for c in m["_set_status"].call_args_list]


class TestAnsweredFindingsInReview(ReviewHarness, unittest.TestCase):
    """A rerun keeps the threads someone answered; the review must not raise them again."""
    AI = "🟠 [Medium] None 沒處理\n\n" + rc.SIGNATURE_MARKER
    OLD = {"severity": "medium", "category": "correctness", "title": "None 沒處理",
           "file": "a.py", "line": 3}

    def test_a_lite_pass_finding_only_answered_ones_is_confirmed_by_deep(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        m = self.patches(d.name, outcome())
        m["gitlab_client"].list_discussions.return_value = [{"id": "d1", "notes": [
            {"id": 1, "author": {"id": 7}, "body": self.AI,
             "position": {"new_path": "a.py", "new_line": 3}},
            {"id": 2, "author": {"id": 99}, "body": "不用修"}]}]
        modes = []

        def run_review(work, ctx_path, out_path, wt, cfg, mode):
            modes.append(mode)
            out_path.write_text(json.dumps({"findings": [self.OLD] if mode == "lite" else []}))
            return 0
        m["engines"].get_engine.return_value.run_review.side_effect = run_review
        m["engines"].get_engine.return_value.label.return_value = "m"
        reviewer.run_review("g/app", 5, 100, self.CFG, {}, False, force_mode="lite")
        self.assertEqual(modes, ["lite", "deep"])
        self.assertEqual(self.posted(m), [])


class TestFinalGatesInReview(ReviewHarness, unittest.TestCase):
    """The final gates run on every review, land even when the AI review says
    clean or fails, publish a commit status, and keep the MR from auto-merging."""

    def review(self, engine_result, gates):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        m = self.patches(d.name, gates)
        with mock.patch.object(reviewer, "_review_with_confirmation",
                               return_value=(engine_result, "deep")):
            rc = reviewer.run_review("g/app", 5, 100, self.CFG, {}, False)
        return rc, m, reviewer.read_review_meta(Path(d.name) / "100")

    def test_rule_hit_is_posted_even_when_ai_says_clean(self):
        rc, m, meta = self.review({"findings": []}, outcome(rule=[GATE_HIT]))
        self.assertEqual(rc, 0)
        self.assertEqual([f["gate"] for f in self.posted(m)], ["artifact"])
        self.assertEqual(self.statuses(m), ["pending", "failed"])
        m["try_auto_merge"].assert_not_called()

    def test_guard_leak_is_posted_and_recorded(self):
        _, m, meta = self.review({"findings": []}, outcome(guard=[LEAK]))
        self.assertEqual([f["gate"] for f in self.posted(m)], ["release_guard"])
        self.assertEqual(meta["guard"], {"head_sha": "h", "target_branch": "master",
                                         "ok": True, "leaks": 1})
        m["try_auto_merge"].assert_not_called()

    def test_blocked_merge_is_folded_into_the_one_completion_message(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        m = self.patches(d.name, outcome())
        m["try_auto_merge"].return_value = (
            False, "*原因*:MR 還是草稿(Draft)\n*下一步*:取消 Draft 後重新合併",
            reviewer.blocks.rerun_buttons("g/app", 5, label="🔁 重審")
            + reviewer.blocks.merge_buttons("g/app", 5), [])
        with mock.patch.object(reviewer, "_review_with_confirmation",
                               return_value=({"findings": []}, "deep")):
            reviewer.run_review("g/app", 5, 100, self.CFG, {}, False)
        m["_slack_say"].assert_not_called()
        text = m["_slack_say_verdict"].call_args.args[1]
        self.assertIn("還沒自動合併", text)
        self.assertIn("Draft", text)
        actions = self.completion_actions(m)
        self.assertEqual(len(actions), len(set(actions)))     # Slack rejects duplicates
        self.assertIn("mrs:merge", actions)
        m["_mark_notification_merged"].assert_not_called()

    def test_ci_failure_becomes_a_counted_comment_not_a_slack_section(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        m = self.patches(d.name, outcome())
        bug = {"severity": "high", "category": "verification", "file": None, "line": None,
               "title": "CI 沒過:lint:flake8 failed", "problem": "p", "impact": "i", "fix": "f"}
        m["try_auto_merge"].return_value = (False, "*原因*:CI 沒過", [], [bug])
        with mock.patch.object(reviewer, "_review_with_confirmation",
                               return_value=({"findings": []}, "deep")):
            reviewer.run_review("g/app", 5, 100, self.CFG, {}, False)
        self.assertIn(bug, self.posted(m))
        text = m["_slack_say_verdict"].call_args.args[1]
        self.assertIn("1 comment", text)
        self.assertNotIn("還沒自動合併", text)                 # the original completion look
        self.assertIn("mrs:appeal", self.completion_actions(m))
        trigger = m["history_sync"].sync_mr.call_args.kwargs["trigger"]
        self.assertIn("1 則", trigger)                          # counted in the history

    def test_merge_check_sees_this_reviews_own_verdict(self):
        """Regression (!316): the merge check ran before this review's meta was written,
        so a first review read "not reviewed" and needed a pointless rerun."""
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        m = self.patches(d.name, outcome())
        seen = {}
        m["try_auto_merge"].side_effect = lambda *a, **k: (
            seen.update(reviewer.read_review_meta(Path(d.name) / "100")) or (True, "", [], []))
        with mock.patch.object(reviewer, "_review_with_confirmation",
                               return_value=({"findings": []}, "deep")):
            reviewer.run_review("g/app", 5, 100, self.CFG, {}, False)
        self.assertEqual((seen.get("head_sha"), (seen.get("guard") or {}).get("head_sha")),
                         ("h", "h"))

    def test_merge_is_announced_in_the_completion_message(self):
        _, m, _ = self.review({"findings": []}, outcome())
        m["_slack_say"].assert_not_called()
        self.assertIn("已自動合併", m["_slack_say_verdict"].call_args.args[1])
        m["_mark_notification_merged"].assert_called_once()

    def completion_actions(self, m):
        buttons = m["_slack_say_verdict"].call_args.args[4] or []
        return [b["action_id"] for b in buttons]

    def test_completion_offers_the_admin_override_with_the_usual_buttons(self):
        unsure = {**LEAK, "confidence": "unsure", "human_check": "確認字號"}
        _, m, _ = self.review({"findings": []}, outcome(guard=[unsure]))
        self.assertEqual(self.completion_actions(m),
                         ["mrs:rerun:auto", "mrs:appeal", "mrs:waive"])

    def test_unfinished_gate_offers_only_a_rerun(self):
        _, m, _ = self.review({"findings": []}, outcome(broken=True))
        self.assertEqual(self.completion_actions(m), ["mrs:rerun:auto"])

    def test_clean_gates_pass_and_merge_may_proceed(self):
        _, m, meta = self.review({"findings": []}, outcome())
        self.assertEqual(self.posted(m), [])
        self.assertEqual(self.statuses(m), ["pending", "success"])
        self.assertEqual(meta["guard"], {"head_sha": "h", "target_branch": "master",
                                         "ok": True, "leaks": 0})
        m["try_auto_merge"].assert_called_once()

    def test_ai_duplicate_of_a_gate_line_is_dropped(self):
        ai = [{"file": "a.tsx", "line": 3, "title": "dup", "severity": "low"},
              {"file": "c.py", "line": 1, "title": "real", "severity": "medium"}]
        _, m, _ = self.review({"findings": ai}, outcome(rule=[GATE_HIT]))
        self.assertEqual(sorted(f["title"] for f in self.posted(m)), ["real", "t"])

    def test_gates_still_post_and_record_when_the_engine_fails(self):
        rc, m, meta = self.review(None, outcome(rule=[GATE_HIT], guard=[LEAK]))
        self.assertEqual(rc, 1)
        self.assertEqual(sorted(f["gate"] for f in self.posted(m)), ["artifact", "release_guard"])
        self.assertEqual(meta["guard"]["leaks"], 2)
        m["try_auto_merge"].assert_not_called()

    def test_incomplete_gates_say_rerun_and_block(self):
        _, m, meta = self.review({"findings": []}, outcome(broken=True))
        text = m["_slack_say_verdict"].call_args.args[1]
        self.assertIn("沒跑完", text)
        self.assertEqual(self.statuses(m)[-1], "failed")
        self.assertFalse(meta["guard"]["ok"])
        m["try_auto_merge"].assert_not_called()

    def test_mr_outside_release_branches_skips_the_final_gates(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        m = self.patches(d.name, outcome(rule=[GATE_HIT]))
        ctx = {**self.CTX, "target_branch": "UAT-for-online"}
        m["fetch_mr"].build_context.return_value = ctx
        with mock.patch.object(reviewer, "_review_with_confirmation",
                               return_value=({"findings": []}, "deep")):
            rc = reviewer.run_review("g/app", 5, 100, self.CFG, {}, False)
        self.assertEqual(rc, 0)
        m["run"].assert_not_called()
        m["_set_status"].assert_not_called()
        m["try_auto_merge"].assert_called_once()
        # no guard verdict is recorded, so a later retarget to master cannot reuse it
        self.assertIsNone(reviewer.read_review_meta(Path(d.name) / "100")["guard"])

    def test_mr_into_the_projects_own_main_branch_is_gated(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        m = self.patches(d.name, outcome())
        m["fetch_mr"].build_context.return_value = {**self.CTX, "target_branch": "production"}
        m["gitlab_client"].get_project.return_value = {"default_branch": "production"}
        with mock.patch.object(reviewer, "_review_with_confirmation",
                               return_value=({"findings": []}, "deep")):
            reviewer.run_review("g/app", 5, 100, self.CFG, {}, False)
        m["run"].assert_called_once()

    def test_unknown_main_branch_gates_the_mr(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        m = self.patches(d.name, outcome())
        m["fetch_mr"].build_context.return_value = {**self.CTX, "target_branch": "develop"}
        m["gitlab_client"].get_project.side_effect = RuntimeError("500")
        with mock.patch.object(reviewer, "_review_with_confirmation",
                               return_value=({"findings": []}, "deep")):
            reviewer.run_review("g/app", 5, 100, self.CFG, {}, False)
        m["run"].assert_called_once()

    def test_release_findings_are_ordinary_comments_with_no_separate_message(self):
        unsure = {**LEAK, "confidence": "unsure", "human_check": "確認正式機的 `API_HOST`"}
        _, m, _ = self.review({"findings": []}, outcome(guard=[unsure]))
        self.assertEqual([f["gate_head"] for f in self.posted(m)], ["h"])
        m["_slack_say"].assert_not_called()                      # only the completion message
        self.assertIn("1 comment", m["_slack_say_verdict"].call_args.args[1])
        m["try_auto_merge"].assert_not_called()

    def test_worktree_failure_marks_the_head_failed(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        m = self.patches(d.name, outcome())
        with mock.patch.object(reviewer, "prepare_worktree", return_value=("/clone", "fetch")):
            rc = reviewer.run_review("g/app", 5, 100, self.CFG, {}, False)
        self.assertEqual(rc, 1)
        self.assertEqual(self.statuses(m), ["pending", "failed"])
        self.assertIn("沒跑完", self.said(m))
        self.assertNotIn("正式分支", self.said(m))


class TestReleaseAutoMergeRail(unittest.TestCase):
    """Every auto-merge (clean review or accepted appeals) re-runs the marker rule
    and requires the guard to have passed this exact head, so no AI verdict can
    waive either, and anything unknown fails closed."""

    GREEN = {"id": 100, "iid": 10, "detailed_merge_status": "mergeable",
             "head_pipeline": {"status": "success"}, "sha": "h",
             "diff_refs": {"start_sha": "s", "head_sha": "h"}, "target_branch": "master"}
    CFG = {"slack": {}, "review": {"release_guard": {"test_hosts": ["uat.example"]}}}
    PASSED = {"head_sha": "h", "target_branch": "master", "ok": True, "leaks": 0}

    def _merge(self, changes, guard_meta, cfg=None, hits=(), threads=(), override=None):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(reviewer, "REVIEWS_DIR", Path(d)), \
             mock.patch.object(reviewer, "_release_blocker",
                               side_effect=lambda *a: REAL_RELEASE_BLOCKER(*a)), \
             mock.patch.object(reviewer.artifact_gate, "collect", return_value=changes), \
             mock.patch.object(reviewer.artifact_gate, "exempt_test_files", return_value=set()), \
             mock.patch.object(reviewer.artifact_gate, "scan", return_value=list(hits)), \
             mock.patch.object(reviewer, "gitlab_client") as gl, \
             mock.patch.object(reviewer, "_slack_say"):
            if guard_meta is not None:
                (Path(d) / "100").mkdir()
                reviewer.write_review_meta(Path(d) / "100", "deep", "h", 0, guard_meta)
                if override:
                    meta = reviewer.read_review_meta(Path(d) / "100")
                    reviewer.save_review_meta(Path(d) / "100", {**meta, "override": override})
            gl.get_mr.return_value = self.GREEN
            gl.get_current_user.return_value = {"id": 7}
            gl.list_discussions.return_value = list(threads)
            gl.list_commit_statuses.return_value = []
            gl.get_project.return_value = {"default_branch": "master"}
            gl.has_ci_config.return_value = True
            gl.get_mr_changes.return_value = {"changes": []}
            reviewer._maybe_auto_merge(cfg or self.CFG, "http://gl", "tok", "g/p", 10,
                                       "http://gl/10", reviewed_sha="h")
        return gl

    def test_clean_rule_and_passed_guard_merge(self):
        self._merge([], self.PASSED).merge_mr.assert_called_once()

    def test_rule_hit_blocks(self):
        gl = self._merge([], self.PASSED, hits=[GATE_HIT])
        gl.merge_mr.assert_not_called()
        gl.post_note.assert_called_once()

    def test_rail_scans_build_output_from_the_api_diff(self):
        bundle = {"new_path": "dist/app.js", "diff": "@@ -0,0 +1 @@\n+x\n"}
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(reviewer, "REVIEWS_DIR", Path(d)), \
             mock.patch.object(reviewer.artifact_gate, "collect", return_value=None) as collect, \
             mock.patch.object(reviewer, "gitlab_client") as gl:
            gl.get_project.return_value = {"default_branch": "master"}
            gl.get_mr_changes.return_value = {"changes": [bundle]}
            REAL_RELEASE_BLOCKER(self.CFG, "http://gl", "t", "g/p", self.GREEN)
        self.assertEqual(collect.call_args.args[3], [bundle])

    def test_unreadable_diff_blocks(self):
        self._merge(None, self.PASSED).merge_mr.assert_not_called()

    def test_guard_leak_blocks(self):
        self._merge([], {**self.PASSED, "leaks": 1}).merge_mr.assert_not_called()

    def test_guard_failed_blocks(self):
        self._merge([], {**self.PASSED, "ok": False}).merge_mr.assert_not_called()

    def test_guard_on_an_older_head_blocks(self):
        self._merge([], {**self.PASSED, "head_sha": "old"}).merge_mr.assert_not_called()

    def test_retargeted_mr_without_a_guard_run_blocks_and_says_why(self):
        gl = self._merge([], None)
        gl.merge_mr.assert_not_called()
        self.assertIn("最新的 commit 還沒完整 review 過", gl.post_note.call_args.args[4])

    def test_guard_run_for_another_target_blocks(self):
        self._merge([], {**self.PASSED, "target_branch": "main"}).merge_mr.assert_not_called()

    def test_no_guard_record_blocks(self):
        self._merge([], None).merge_mr.assert_not_called()

    def test_mr_outside_release_branches_is_not_held_by_the_release_rail(self):
        uat = {**self.GREEN, "target_branch": "UAT-for-online"}
        with mock.patch.object(reviewer, "REVIEWS_DIR", Path("/nonexistent")), \
             mock.patch.object(reviewer, "gitlab_client") as gl:
            gl.get_project.return_value = {"default_branch": "master"}
            self.assertIsNone(REAL_RELEASE_BLOCKER(self.CFG, "http://gl", "t", "g/p", uat))
        gl.get_mr_changes.assert_not_called()

    def test_unconfigured_test_hosts_block(self):
        self._merge([], self.PASSED, cfg={"slack": {}, "review": {}}).merge_mr.assert_not_called()


    def test_release_comments_all_closed_lets_it_merge(self):
        gl = self._merge([], {**self.PASSED, "leaks": 1}, threads=[release_thread("h", closed=True)])
        gl.merge_mr.assert_called_once()

    def test_an_open_release_comment_holds_it(self):
        gl = self._merge([], {**self.PASSED, "leaks": 1}, threads=[release_thread("h")])
        gl.merge_mr.assert_not_called()

    def test_answered_comments_from_an_older_head_do_not_count(self):
        gl = self._merge([], {**self.PASSED, "leaks": 1},
                         threads=[release_thread("old0000", closed=True)])
        gl.merge_mr.assert_not_called()

    def test_override_record_alone_releases_nothing(self):
        """An override works by closing comments; the record itself skips no check."""
        override = {"head_sha": "h", "target_branch": "master", "by": "冠霖"}
        for guard in ({**self.PASSED, "leaks": 1}, {**self.PASSED, "ok": False}):
            with self.subTest(guard=guard):
                gl = self._merge([], guard, threads=[release_thread("h")], override=override)
                gl.merge_mr.assert_not_called()

    def test_answered_marker_hit_lets_it_merge(self):
        """Regression (!66): a comment that only mentions 測試用 was appealed and accepted."""
        gl = self._merge([], {**self.PASSED, "leaks": 1}, hits=[GATE_HIT],
                         threads=[release_thread("h", closed=True)])
        gl.merge_mr.assert_called_once()

    def test_open_marker_hit_holds_it(self):
        gl = self._merge([], {**self.PASSED, "leaks": 1}, hits=[GATE_HIT],
                         threads=[release_thread("h")])
        gl.merge_mr.assert_not_called()

    def test_marker_hit_the_review_never_posted_holds_it(self):
        gl = self._merge([], self.PASSED, hits=[GATE_HIT])
        gl.merge_mr.assert_not_called()
        self.assertIn("測試字樣", gl.post_note.call_args.args[4])

    def test_override_for_an_older_head_does_not_carry_over(self):
        override = {"head_sha": "old", "target_branch": "master", "by": "冠霖"}
        gl = self._merge([], {**self.PASSED, "leaks": 1}, threads=[release_thread("h")],
                         override=override)
        gl.merge_mr.assert_not_called()


def release_thread(head, closed=False):
    body = rc.format_comment_body({"title": "t", "gate_head": head},
                                  "— 🤖 mr-sentinel AI review (release guard · m)")
    return {"id": f"d-{head}", "notes": [{"author": {"id": 7}, "body": body,
                                          "resolvable": True, "resolved": closed}]}


MR_HEAD = {"id": 100, "iid": 10, "sha": "h", "target_branch": "master",
           "detailed_merge_status": "mergeable",
           "head_pipeline": {"status": "failed", "web_url": "http://gl/p/1"}}


class TestBlockedMergeMessage(unittest.TestCase):
    """A clean MR that did not merge says why in plain words, with a way forward."""
    CI_FAILED = [{"name": "mr-sentinel/release-gate", "status": "failed", "description": "x"},
                 {"name": "unit-test", "status": "failed", "target_url": "http://gl/jobs/9"},
                 {"name": "lint", "status": "success"}]

    def _run(self, mr=MR_HEAD, release=None, statuses=CI_FAILED, note_on_block=True):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(reviewer, "REVIEWS_DIR", Path(d)), \
             mock.patch.object(reviewer, "STATUS_SETTLE_SECONDS", 0), \
             mock.patch.object(reviewer, "_release_blocker", return_value=release), \
             mock.patch.object(reviewer, "gitlab_client") as gl, \
             mock.patch.object(reviewer, "_slack_say") as say:
            gl.get_mr.return_value = mr
            gl.has_ci_config.return_value = True
            if isinstance(statuses, Exception):
                gl.list_commit_statuses.side_effect = statuses
            else:
                gl.list_commit_statuses.return_value = statuses
            reviewer._maybe_auto_merge({"slack": {}}, "http://gl", "tok", "g/p", 10,
                                       "http://gl/10", reviewed_sha="h",
                                       note_on_block=note_on_block)
        gl.merge_mr.assert_not_called()
        self.gl = gl
        text, buttons = say.call_args.args[1], say.call_args.args[3]
        return text, [b["action_id"] for b in buttons]

    def test_failed_ci_names_each_job_but_not_our_own_status(self):
        text, actions = self._run()
        self.assertIn("unit-test", text)
        self.assertIn("http://gl/jobs/9", text)
        self.assertNotIn("lint", text)
        self.assertNotIn("release-gate", text)
        self.assertEqual(actions, ["mrs:merge"])

    def test_open_comments_are_the_reason_not_the_red_pipeline(self):
        text, actions = self._run(release=f"{reviewer.OPEN_COMMENTS}2")
        self.assertIn("還有 2 則 review 留言沒處理完", text)
        self.assertNotIn("CI 沒過", text)
        self.assertEqual(actions, ["mrs:rerun:auto", "mrs:appeal", "mrs:waive"])

    def test_unreviewed_head_asks_for_a_rerun_without_internal_words(self):
        text, actions = self._run(release=reviewer.NOT_REVIEWED)
        self.assertIn("最新的 commit 還沒完整 review 過", text)
        self.assertNotIn("正式分支", text)
        self.assertEqual(actions, ["mrs:rerun:auto", "mrs:merge"])

    def test_message_is_plain_chinese_with_a_next_step(self):
        text, _ = self._run()
        self.assertIn("*原因*", text)
        self.assertIn("*下一步*", text)
        self.assertNotIn("merge manually", text)

    def test_red_pipeline_without_a_failed_job_still_points_somewhere(self):
        text, _ = self._run(statuses=[{"name": "lint", "status": "success"}])
        self.assertIn("http://gl/p/1", text)

    def test_status_lookup_failure_still_reports(self):
        text, actions = self._run(statuses=RuntimeError("502"))
        self.assertIn("查不到", text)
        self.assertIn("mrs:merge", actions)

    def test_running_pipeline_says_wait_without_a_job_list(self):
        text, _ = self._run(mr={**MR_HEAD, "head_pipeline": {"status": "running"}})
        self.assertIn("CI 還在跑", text)
        self.assertNotIn("• CI job", text)

    def test_a_pressed_retry_adds_no_mr_note(self):
        self._run(note_on_block=False)
        self.gl.post_note.assert_not_called()

    def test_new_commits_keep_the_rerun_button(self):
        _, actions = self._run(mr={**MR_HEAD, "sha": "pushed"})
        self.assertEqual(actions, ["mrs:rerun:auto"])

    def test_blocker_texts_cover_every_rail(self):
        for blocker in ("new commits since review", "draft MR", "not mergeable (conflict)",
                        "pipeline missing", "pipeline is for an older commit",
                        "pipeline failed", "pipeline running",
                        "test-marker rule could not read the diff", "test-only code in 2 file(s)",
                        reviewer.NOT_REVIEWED, reviewer.UNFINISHED,
                        f"{reviewer.OPEN_COMMENTS}1"):
            with self.subTest(blocker=blocker):
                why, next_step = reviewer.blocker_text(blocker)
                self.assertNotEqual(why, blocker)              # translated, not passed through
                self.assertTrue(next_step)


class TestSyncReleaseStatus(unittest.TestCase):
    """Once nothing holds the release, GitLab's own status turns green too."""

    def _sync(self, ours, override=None, guard_head="h", blocker=None):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(reviewer, "REVIEWS_DIR", Path(d)), \
             mock.patch.object(reviewer, "STATUS_SETTLE_SECONDS", 0), \
             mock.patch.object(reviewer, "_release_blocker", return_value=blocker), \
             mock.patch.object(reviewer, "gitlab_client") as gl:
            (Path(d) / "100").mkdir()
            reviewer.write_review_meta(Path(d) / "100", "deep", "h", 1,
                                       {"head_sha": guard_head, "target_branch": "master",
                                        "ok": True, "leaks": 1})
            if override:
                meta = reviewer.read_review_meta(Path(d) / "100")
                reviewer.save_review_meta(Path(d) / "100", {**meta, "override": override})
            gl.list_commit_statuses.return_value = [{"name": "mr-sentinel/release-gate",
                                                     "status": ours}]
            gl.get_mr.return_value = {**MR_HEAD, "head_pipeline": {"status": "success"}}
            reviewer.sync_release_status({}, "b", "t", "g/p", MR_HEAD)
        return gl

    def test_failed_status_turns_green_when_nothing_is_left(self):
        gl = self._sync("failed")
        state, _, description = gl.set_commit_status.call_args.args[4:7]
        self.assertEqual(state, "success")
        self.assertIn("處理完", description)

    def test_never_turns_green_while_the_release_rail_still_holds(self):
        gl = self._sync("failed", blocker=f"{reviewer.OPEN_COMMENTS}1")
        gl.set_commit_status.assert_not_called()

    def test_green_status_is_left_alone(self):
        self._sync("success").set_commit_status.assert_not_called()

    def test_verdict_for_another_head_is_never_turned_green(self):
        self._sync("failed", guard_head="old").set_commit_status.assert_not_called()


class TestBlockerFindings(unittest.TestCase):
    """Blockers that are the developer's to fix become findings with evidence."""
    MR = {**MR_HEAD, "web_url": "http://gl/g/p/-/merge_requests/10"}
    STATUSES = [{"name": "lint:flake8", "status": "failed", "id": 41},
                {"name": "lint:manual", "status": "failed", "id": 42, "allow_failure": True},
                {"name": "mr-sentinel/release-gate", "status": "failed"},
                {"name": "lint:isort", "status": "success", "id": 40}]
    TRACE = ("noise\n\x1b[32;1m$ flake8\x1b[0;m\nsection_end:1792553353:step_script\n"
             "orders.py:126:101: E501 line too long\n")

    def _findings(self, blocker, trace=TRACE):
        with mock.patch.object(reviewer, "gitlab_client") as gl:
            gl.list_commit_statuses.return_value = self.STATUSES
            if isinstance(trace, Exception):
                gl.get_job_trace.side_effect = trace
            else:
                gl.get_job_trace.return_value = trace
            return reviewer.blocker_findings("b", "t", "g/p", self.MR, blocker)

    def test_each_failed_job_is_one_bug_with_its_log_tail(self):
        [bug] = self._findings("pipeline failed")
        self.assertEqual((bug["severity"], bug["category"], bug["file"]),
                         ("high", "verification", None))
        self.assertIn("lint:flake8", bug["title"])
        self.assertIn("E501 line too long", bug["evidence"])
        self.assertIn("http://gl/g/p/-/jobs/41", bug["evidence"])
        self.assertNotIn("\x1b", bug["evidence"])
        self.assertNotIn("section_end", bug["evidence"])
        self.assertIn("申訴", bug["fix"])                      # an unrelated failure can be argued

    def test_missing_log_still_files_the_bug(self):
        [bug] = self._findings("pipeline failed", trace=RuntimeError("403"))
        self.assertIn("拿不到 job log", bug["evidence"])

    def test_conflict_is_a_bug(self):
        [bug] = self._findings("not mergeable (conflict)")
        self.assertEqual(bug["category"], "compatibility")
        self.assertIn("衝突", bug["title"])

    def test_other_blockers_are_not_bugs(self):
        for blocker in ("draft MR", "pipeline running", "pipeline missing",
                        "new commits since review", "not mergeable (checking)"):
            with self.subTest(blocker=blocker):
                self.assertEqual(self._findings(blocker), [])


class TestFailingStep(unittest.TestCase):
    def test_starts_at_the_last_command(self):
        trace = " + pkg==1\n + pkg==2\n$ setup\nok\n$ flake8 x\nx.py:1: E501\nERROR: Job failed\n"
        self.assertEqual(reviewer.failing_step(trace), "$ flake8 x\nx.py:1: E501\nERROR: Job failed")

    def test_without_a_command_keeps_the_tail(self):
        trace = "\n".join(f"l{i}" for i in range(100))
        self.assertEqual(len(reviewer.failing_step(trace).splitlines()), reviewer.TRACE_TAIL_LINES)
