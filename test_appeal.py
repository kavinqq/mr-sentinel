"""Tests for appeal.py: verdicts go back to GitLab threads, merge keeps its rails.

GitLab, Slack, git and the engine are all mocked; runs offline.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import appeal
import review_common as rc

ME, DEV = 42, 7
AI = "🟠 [Medium] x\n\n— 🤖 mr-sentinel AI review (m)"


def note(author, body, **extra):
    return {"author": {"id": author, "name": f"u{author}"}, "body": body, **extra}


def config(**review):
    return {"gitlab_url": "https://gl", "gitlab_token": "t",
            "slack": {"bot_token": "xoxb", "channel_id": "C1"},
            "review": {"engine": "claude", "language": "zh-TW",
                       "review_timeout_seconds": 900, "project_map": {"g/app": "/local"},
                       "claude": {"model": "opus"}, **review}}


class Harness:
    """Runs appeal.run_appeal with a scripted engine verdict and a GitLab whose
    discussions reflect the replies posted during the run."""

    def __init__(self, discussions, verdicts, engine_rc=0, meta=None, during_run=None,
                 resolve_error=None, **review):
        self.discussions = discussions
        self.during_run = during_run          # mutate GitLab while the "model" runs
        self.resolve_error = resolve_error
        self.verdicts = verdicts
        self.engine_rc = engine_rc
        self.meta = meta
        self.cfg = config(**review)
        self.replies, self.resolved, self.said = [], [], []

    def reply(self, base, token, project, iid, did, body):
        self.replies.append((did, body))
        for d in self.discussions:
            if d["id"] == did:
                d["notes"].append(note(ME, body))

    def resolve(self, base, token, project, iid, did):
        if self.resolve_error:
            raise self.resolve_error
        self.resolved.append(did)
        for d in self.discussions:
            if d["id"] == did:
                for n in d["notes"]:
                    if n.get("resolvable"):
                        n["resolved"] = True

    def run_engine(self, work, ctx_path, out_path, wt, cfg):
        self.ctx = json.loads(ctx_path.read_text())
        if self.during_run:
            self.during_run(self.discussions)
        if self.engine_rc == 0:
            out_path.write_text(json.dumps({"verdicts": self.verdicts}))
        return self.engine_rc

    def run(self):
        with tempfile.TemporaryDirectory() as d:
            reviews = Path(d)
            if self.meta:
                (reviews / "100").mkdir()
                (reviews / "100" / "review_meta.json").write_text(json.dumps(self.meta))
            gl = appeal.gitlab_client
            engine = mock.Mock(run_appeal=self.run_engine)
            with mock.patch.object(appeal, "REVIEWS_DIR", reviews), \
                 mock.patch.object(gl, "get_current_user", return_value={"id": ME}), \
                 mock.patch.object(gl, "list_discussions",
                                   side_effect=lambda *a: json.loads(json.dumps(self.discussions))), \
                 mock.patch.object(gl, "get_mr", return_value={"sha": "head2", "web_url": "u"}), \
                 mock.patch.object(gl, "reply_discussion", side_effect=self.reply), \
                 mock.patch.object(gl, "resolve_discussion", side_effect=self.resolve), \
                 mock.patch.object(appeal.reviewer, "prepare_worktree", return_value=("/local", None)), \
                 mock.patch.object(appeal.reviewer, "_run_git"), \
                 mock.patch.object(appeal.reviewer, "_slack_say",
                                   side_effect=lambda cfg, text, ts, b=None: self.said.append((text, b))), \
                 mock.patch.object(appeal.reviewer, "_maybe_auto_merge") as self.merge, \
                 mock.patch.object(appeal.history_sync, "sync_mr") as self.history, \
                 mock.patch.object(appeal.engines, "get_engine", return_value=engine):
                self.rc = appeal.run_appeal("g/app", "5", "100", self.cfg, {})
        return self


def disputed(did, *extra):
    return {"id": did, "notes": [note(ME, AI, position={"new_path": "a.py", "new_line": 3}),
                                 note(DEV, "這是刻意的"), *extra]}


class TestRunAppeal(unittest.TestCase):
    def test_accept_replies_resolves_and_reports(self):
        h = Harness([disputed("d1"), disputed("d2")],
                    [{"id": "d1", "verdict": "accept", "reason": "上游已驗證"},
                     {"id": "d2", "verdict": "reject", "reason": "a.py:3 仍會 None"}]).run()
        self.assertEqual(h.rc, 0)
        self.assertEqual([d for d, _ in h.replies], ["d1", "d2"])
        self.assertIn(rc.APPEAL_ACCEPT, h.replies[0][1])
        self.assertIn(rc.APPEAL_REJECT, h.replies[1][1])
        self.assertEqual(h.resolved, ["d1"])                  # rejected thread stays open
        summary, buttons = h.said[-1]
        self.assertIn("理由成立 1", summary)
        self.assertTrue(buttons)                              # still open -> can press again
        h.merge.assert_not_called()
        self.assertEqual([a["id"] for a in h.ctx["appeals"]], ["d1", "d2"])
        self.assertIn("申訴", h.history.call_args.kwargs["trigger"])   # re-scored after verdicts

    def test_nothing_to_judge_skips_the_engine(self):
        h = Harness([{"id": "d1", "notes": [note(ME, AI)]}], []).run()
        self.assertEqual(h.replies, [])
        self.assertIn("沒有新的開發者回覆", h.said[-1][0])

    def test_engine_failure_posts_nothing_on_gitlab(self):
        h = Harness([disputed("d1")], [], engine_rc=1).run()
        self.assertEqual(h.rc, 1)
        self.assertEqual(h.replies, [])
        self.assertIn("沒跑完", h.said[-1][0])

    def test_unjudged_thread_stays_open(self):
        """A model that skips an id must not silently close that thread."""
        h = Harness([disputed("d1"), disputed("d2")],
                    [{"id": "d1", "verdict": "accept", "reason": "ok"}],
                    auto_merge_on_clean=True, meta={"mode": "deep", "head_sha": "h1"}).run()
        self.assertEqual([d for d, _ in h.replies], ["d1"])
        h.merge.assert_not_called()

    def test_all_accepted_after_deep_review_merges_at_the_reviewed_sha(self):
        h = Harness([disputed("d1")], [{"id": "d1", "verdict": "accept", "reason": "ok"}],
                    auto_merge_on_clean=True, meta={"mode": "deep", "head_sha": "h1"}).run()
        h.merge.assert_called_once()
        # the sha the review covered — not the current head (head2), which may be unreviewed
        self.assertEqual(h.merge.call_args.kwargs["reviewed_sha"], "h1")
        self.assertIsNone(h.said[-1][1])                      # nothing open -> no buttons

    def test_all_accepted_after_lite_review_does_not_merge(self):
        h = Harness([disputed("d1")], [{"id": "d1", "verdict": "accept", "reason": "ok"}],
                    auto_merge_on_clean=True, meta={"mode": "lite", "head_sha": "h1"}).run()
        h.merge.assert_not_called()
        self.assertIn("deep review 才自動合併", h.said[-1][0])

    def test_auto_merge_off_never_merges(self):
        h = Harness([disputed("d1")], [{"id": "d1", "verdict": "accept", "reason": "ok"}],
                    meta={"mode": "deep", "head_sha": "h1"}).run()
        h.merge.assert_not_called()

    def test_reply_added_while_the_model_ran_is_not_answered(self):
        def dev_adds_counter_evidence(discussions):
            discussions[0]["notes"].append(note(DEV, "補充:其實還有一個情境"))
        h = Harness([disputed("d1")], [{"id": "d1", "verdict": "accept", "reason": "ok"}],
                    during_run=dev_adds_counter_evidence,
                    auto_merge_on_clean=True, meta={"mode": "deep", "head_sha": "h1"}).run()
        self.assertEqual(h.replies, [])
        self.assertEqual(h.resolved, [])
        h.merge.assert_not_called()

    def test_refused_resolve_is_reported_and_retried_next_press(self):
        """The Taiga-class bug: allowed to reply, refused to change state. It must
        be visible, block merging, and recover on the next press."""
        import urllib.error
        d = disputed("d1")
        for n in d["notes"]:
            n["resolvable"] = True
        err = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        first = Harness([d], [{"id": "d1", "verdict": "accept", "reason": "ok"}],
                        resolve_error=err, auto_merge_on_clean=True,
                        meta={"mode": "deep", "head_sha": "h1"}).run()
        self.assertIn("GitLab 拒絕 resolve", first.said[-1][0])
        first.merge.assert_not_called()
        # permission fixed; press again: no new reply, so no model call — just the resolve
        second = Harness(first.discussions, [], auto_merge_on_clean=True,
                         meta={"mode": "deep", "head_sha": "h1"})
        second.run_engine = mock.Mock(side_effect=AssertionError("engine must not run"))
        second.run()
        self.assertEqual(second.resolved, ["d1"])
        second.merge.assert_called_once()

    def test_failed_resolve_keeps_the_thread_open(self):
        import urllib.error
        d = disputed("d1")
        d["notes"][0]["resolvable"] = True
        err = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        h = Harness([d], [{"id": "d1", "verdict": "accept", "reason": "ok"}], resolve_error=err,
                    auto_merge_on_clean=True, meta={"mode": "deep", "head_sha": "h1"}).run()
        self.assertEqual(len(h.replies), 1)
        h.merge.assert_not_called()
        self.assertTrue(h.said[-1][1])                        # still open -> buttons stay


class TestMain(unittest.TestCase):
    def test_crash_mid_run_is_reported_with_retry_buttons(self):
        """The click already retired its button; a silent failure would strand the user."""
        said = []
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(appeal, "REVIEWS_DIR", Path(d)), \
             mock.patch.object(appeal, "SCRIPT_DIR", Path(d)), \
             mock.patch.object(appeal, "load_config", return_value=config()), \
             mock.patch.object(appeal, "load_state", return_value={}), \
             mock.patch.object(appeal, "run_appeal", side_effect=RuntimeError("boom")), \
             mock.patch.object(appeal.reviewer, "_slack_say",
                               side_effect=lambda cfg, text, ts, b=None: said.append((text, b))), \
             mock.patch("sys.argv", ["appeal.py", "--project", "g/app", "--iid", "5",
                                     "--mr-id", "100"]), \
             self.assertLogs("mr_sentinel.appeal", "ERROR"):
            self.assertEqual(appeal.main(), 1)
        text, buttons = said[-1]
        self.assertIn("申訴中途失敗", text)
        self.assertTrue(buttons)


if __name__ == "__main__":
    unittest.main()
