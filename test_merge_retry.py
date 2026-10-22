"""Tests for merge_retry.py: the 🔀 / ✅ buttons keep every auto-merge rail.

GitLab and Slack are mocked; runs offline.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import merge_retry

ME = 42
AI = "🟠 [Medium] x\n\n— 🤖 mr-sentinel AI review (m)"
MR = {"id": 100, "iid": "5", "state": "opened", "sha": "abc1234", "target_branch": "master",
      "source_branch": "feat", "web_url": "http://gl/5", "head_pipeline": {"status": "success"}}
UNSURE_GUARD = {"head_sha": "abc1234", "target_branch": "master", "ok": True, "leaks": 1}


def config(auto_merge=True):
    return {"gitlab_url": "http://gl", "gitlab_token": "t",
            "slack": {"bot_token": "xoxb", "channel_id": "C1", "admin_user_ids": ["U1"]},
            "review": {"auto_merge_on_clean": auto_merge}}


class Harness:
    def __init__(self, meta, discussions=(), mr=MR, cfg=None, status_error=None):
        self.meta, self.discussions, self.mr = meta, list(discussions), mr
        self.cfg = cfg or config()
        self.status_error = status_error
        self.note_error = None
        self.resolve_errors = 0
        self.said = []

    def run(self, waived_by=None):
        with tempfile.TemporaryDirectory() as d:
            reviews = Path(d)
            (reviews / "100").mkdir()
            if self.meta is not None:
                (reviews / "100" / "review_meta.json").write_text(json.dumps(self.meta))
            gl = merge_retry.gitlab_client
            with mock.patch.object(merge_retry, "REVIEWS_DIR", reviews), \
                 mock.patch.object(merge_retry.reviewer, "REVIEWS_DIR", reviews), \
                 mock.patch.object(gl, "get_mr", return_value=self.mr), \
                 mock.patch.object(gl, "get_current_user", return_value={"id": ME}), \
                 mock.patch.object(gl, "list_discussions", return_value=self.discussions), \
                 mock.patch.object(gl, "set_commit_status",
                                   side_effect=self.status_error) as self.status, \
                 mock.patch.object(gl, "post_note", side_effect=self.note_error) as self.note, \
                 mock.patch.object(gl, "reply_discussion", side_effect=self.reply), \
                 mock.patch.object(gl, "resolve_discussion", side_effect=self.resolve), \
                 mock.patch.object(merge_retry.slack_client, "users_info",
                                   return_value={"profile": {"display_name": "冠霖"}}), \
                 mock.patch.object(merge_retry.reviewer, "_slack_say",
                                   side_effect=lambda cfg, text, ts, b=None:
                                   self.said.append((text, b))), \
                 mock.patch.object(merge_retry.reviewer, "sync_release_status") as self.sync, \
                 mock.patch.object(merge_retry.reviewer, "_maybe_auto_merge") as self.merge:
                self.rc = merge_retry.run_merge("g/app", "5", "100", self.cfg, {}, waived_by)
                self.meta_after = json.loads((reviews / "100" / "review_meta.json").read_text()) \
                    if self.meta is not None else None
        return self

    def thread(self, did):
        return next(d for d in self.discussions if d["id"] == did)

    def reply(self, base, token, project, iid, did, body):
        self.thread(did)["notes"].append({"author": {"id": ME}, "body": body})

    def resolve(self, base, token, project, iid, did):
        if self.resolve_errors:
            self.resolve_errors -= 1
            raise RuntimeError("502")
        for n in self.thread(did)["notes"]:
            if n.get("resolvable"):
                n["resolved"] = True

    @property
    def last(self):
        return self.said[-1][0] if self.said else ""


DEEP = {"mode": "deep", "head_sha": "abc1234", "findings": 0, "guard": UNSURE_GUARD}


class TestMergeRetry(unittest.TestCase):
    def test_clean_deep_review_goes_through_the_rails(self):
        h = Harness(DEEP).run()
        h.merge.assert_called_once()
        self.assertEqual(h.merge.call_args.kwargs["reviewed_sha"], "abc1234")

    def test_open_ai_thread_blocks(self):
        h = Harness(DEEP, [{"id": "d1", "notes": [{"author": {"id": ME}, "body": AI}]}]).run()
        h.merge.assert_not_called()
        self.assertIn("1 則 AI 留言沒處理完", h.last)

    def test_lite_review_needs_a_rerun(self):
        h = Harness({**DEEP, "mode": "lite"}).run()
        h.merge.assert_not_called()
        self.assertIn("deep", h.last)

    def test_auto_merge_off_merges_nothing(self):
        h = Harness(DEEP, cfg=config(auto_merge=False)).run()
        h.merge.assert_not_called()
        self.assertIn("自動合併功能沒開", h.last)

    def test_closed_mr_is_left_alone(self):
        h = Harness(DEEP, mr={**MR, "state": "merged"}).run()
        h.merge.assert_not_called()
        self.assertIn("merged", h.last)

    def test_plain_retry_never_touches_the_gate(self):
        h = Harness(DEEP).run()
        h.status.assert_not_called()
        self.assertNotIn("waived_by", h.meta_after["guard"])


def ai_thread(did, signature="scanned by opus", replied=False, rejected=False):
    body = f"🟠 x\n\n— 🤖 mr-sentinel AI review ({signature})"
    notes = [{"author": {"id": ME}, "body": body, "resolvable": True, "resolved": False}]
    if replied:
        notes.append({"author": {"id": 7}, "body": "不用修"})
    if rejected:
        notes.append({"author": {"id": ME}, "body": f"仍建議修 {merge_retry.review_common.APPEAL_REJECT}"})
    return {"id": did, "notes": notes}


class TestOverride(unittest.TestCase):
    """✅ is the admin breaking a stalemate: everything still open is released."""

    def test_override_answers_every_open_thread_and_merges(self):
        h = Harness(DEEP, [ai_thread("r1", replied=True, rejected=True),
                           ai_thread("g1", signature="release guard · m")]).run(waived_by="U1")
        for did in ("r1", "g1"):
            self.assertIn("冠霖 裁定放行", h.thread(did)["notes"][-1]["body"])
            self.assertTrue(h.thread(did)["notes"][0]["resolved"])
        h.merge.assert_called_once()

    def test_override_is_recorded_for_this_head_with_who(self):
        h = Harness(DEEP, [ai_thread("r1")]).run(waived_by="U1")
        override = h.meta_after["override"]
        self.assertEqual((override["head_sha"], override["target_branch"], override["by"],
                          override["by_id"]), ("abc1234", "master", "冠霖", "U1"))
        note = h.note.call_args.args[4]
        for part in ("U1", "abc1234", "master"):
            self.assertIn(part, note)
        self.assertIn("還沒合併", h.said[0][0])            # an override is not a merge

    def test_non_admin_started_by_hand_is_refused(self):
        h = Harness(DEEP, [ai_thread("r1")]).run(waived_by="U_NOT_ADMIN")
        self.assertNotIn("override", h.meta_after)
        self.assertEqual(len(h.thread("r1")["notes"]), 1)
        h.merge.assert_not_called()
        self.assertIn("不是 admin", h.last)

    def test_unreviewed_head_is_refused_with_the_rerun_it_asks_for(self):
        h = Harness({**DEEP, "head_sha": "old0000"}, [ai_thread("r1")]).run(waived_by="U1")
        self.assertNotIn("override", h.meta_after)
        h.merge.assert_not_called()
        self.assertIn("mrs:rerun:auto", [b["action_id"] for b in h.said[-1][1]])

    def test_failed_audit_note_is_said_out_loud(self):
        h = Harness(DEEP)
        h.note_error = RuntimeError("500")
        h.run(waived_by="U1")
        self.assertIn("放行紀錄留言沒寫成功", h.said[0][0])

    def test_thread_that_failed_to_close_is_retried_on_the_next_press(self):
        h = Harness(DEEP, [ai_thread("r1")])
        h.resolve_errors = 1
        h.run(waived_by="U1")
        h.merge.assert_not_called()                        # still open after the failure
        h.meta = h.meta_after
        h.run()
        self.assertTrue(h.thread("r1")["notes"][0]["resolved"])
        self.assertEqual(sum("裁定放行" in n["body"] for n in h.thread("r1")["notes"]), 1)
        h.merge.assert_called_once()

    def test_unfinished_review_cannot_be_overridden(self):
        h = Harness({**DEEP, "guard": {**UNSURE_GUARD, "ok": False}}, [ai_thread("r1")])
        h.run(waived_by="U1")
        self.assertNotIn("override", h.meta_after)
        self.assertEqual(len(h.thread("r1")["notes"]), 1)
        self.assertIn("mrs:rerun:auto", [b["action_id"] for b in h.said[-1][1]])

    def test_audit_note_lists_each_released_comment_and_its_state(self):
        h = Harness(DEEP, [ai_thread("r1", replied=True, rejected=True)]).run(waived_by="U1")
        note = h.note.call_args.args[4]
        self.assertIn("申訴被駁回", note)

    def test_release_status_is_synced_even_while_other_comments_are_open(self):
        h = Harness(DEEP, [ai_thread("r1")]).run()
        h.sync.assert_called_once()
        h.merge.assert_not_called()

    def test_plain_retry_leaves_open_threads_alone(self):
        h = Harness(DEEP, [ai_thread("r1")]).run()
        self.assertEqual(len(h.thread("r1")["notes"]), 1)
        h.merge.assert_not_called()
        self.assertIn("mrs:waive", [b["action_id"] for b in h.said[-1][1]])


if __name__ == "__main__":
    unittest.main()
