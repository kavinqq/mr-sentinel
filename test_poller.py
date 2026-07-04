"""Tests for poller pure functions and orchestration seams (IO mocked)."""
import unittest
from unittest import mock

import poller

CFG = {
    "gitlab_url": "https://gl",
    "gitlab_token": "tok",
    "slack": {},
    "review": {"project_map": {"group/backend-app": "/home/me/backend-app"}},
}


class TestSelectAndPrune(unittest.TestCase):
    def test_select_new_skips_seen_and_sorts_oldest_first(self):
        mrs = [
            {"id": 2, "created_at": "2026-07-02T00:00:00Z"},
            {"id": 1, "created_at": "2026-07-01T00:00:00Z"},
            {"id": 3, "created_at": "2026-07-03T00:00:00Z"},
        ]
        seen = {"2": "2026-07-02T00:00:00Z"}
        out = poller.select_new(mrs, seen)
        self.assertEqual([m["id"] for m in out], [1, 3])

    def test_prune_keeps_still_opened_regardless_of_age(self):
        from datetime import datetime, timezone
        now = datetime(2026, 7, 4, tzinfo=timezone.utc)
        seen = {
            "1": "2026-06-01T00:00:00Z",  # old but still opened -> keep
            "2": "2026-06-01T00:00:00Z",  # old and gone -> drop
            "3": "2026-07-03T00:00:00Z",  # young and gone -> keep (grace window)
        }
        pruned = poller.prune_seen(seen, opened_ids={"1"}, now=now, days=7)
        self.assertEqual(set(pruned), {"1", "3"})

    def test_prune_slack_ts_follows_seen(self):
        state = {"seen": {"1": "x"}, "slack_ts": {"1": "t1", "2": "t2"}}
        poller.prune_slack_ts(state)
        self.assertEqual(state["slack_ts"], {"1": "t1"})


class TestNotifyAndSpawn(unittest.TestCase):
    def test_notify_slack_returns_ts_with_bot_token(self):
        cfg = dict(CFG, slack={"bot_token": "xoxb", "channel_id": "C1"})
        with mock.patch("slack_client.chat_post_message", return_value="123.45") as m:
            ts = poller.notify_slack(cfg, "hi")
        self.assertEqual(ts, "123.45")
        m.assert_called_once_with("xoxb", "C1", "hi")

    def test_notify_slack_none_when_unconfigured(self):
        with mock.patch("slack_client.chat_post_message") as m:
            ts = poller.notify_slack(CFG, "hi")
        self.assertIsNone(ts)
        m.assert_not_called()

    def test_spawn_review_only_for_mapped_projects(self):
        import pathlib
        import tempfile
        mr = {"id": 1, "iid": 2}
        with tempfile.TemporaryDirectory() as d:
            # redirect SCRIPT_DIR so the spawn-log file never touches the real reviews/
            with mock.patch.object(poller, "SCRIPT_DIR", pathlib.Path(d)), \
                 mock.patch("subprocess.Popen") as pop:
                poller.maybe_spawn_review(CFG, mr, "group/other-app")
                pop.assert_not_called()
                poller.maybe_spawn_review(CFG, mr, "group/backend-app")
                pop.assert_called_once()

    def test_build_notification_mentions(self):
        mr = {"iid": 7, "title": "T", "web_url": "https://gl/mr/7",
              "author": {"name": "Ann"}, "source_branch": "f", "target_branch": "main"}
        cfg = dict(CFG, slack={"bot_token": "x", "channel_id": "C", "mention_user_ids": ["U1"]})
        text = poller.build_notification(mr, "group/backend-app", cfg)
        self.assertIn("!7", text)
        self.assertIn("group/backend-app", text)
        self.assertIn("<@U1>", text)


if __name__ == "__main__":
    unittest.main()
