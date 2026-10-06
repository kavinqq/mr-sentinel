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

    def test_notify_slack_falls_back_to_webhook(self):
        cfg = dict(CFG, slack={"webhook_url": "https://hooks.slack.com/x"})
        with mock.patch("slack_client.post_webhook") as m:
            ts = poller.notify_slack(cfg, "hi")
        self.assertIsNone(ts)  # webhook 拿不到 ts
        m.assert_called_once_with("https://hooks.slack.com/x", "hi")

    def test_notify_slack_prefers_bot_token_over_webhook(self):
        cfg = dict(CFG, slack={"bot_token": "xoxb", "channel_id": "C1",
                               "webhook_url": "https://hooks.slack.com/x"})
        with mock.patch("slack_client.chat_post_message", return_value="9.9") as bot, \
             mock.patch("slack_client.post_webhook") as hook:
            ts = poller.notify_slack(cfg, "hi")
        self.assertEqual(ts, "9.9")
        hook.assert_not_called()


class TestGroupPollingAndInitGuard(unittest.TestCase):
    def test_poll_opened_group_mode_filters_and_groups_by_path(self):
        cfg = {"gitlab_url": "https://gl", "gitlab_token": "t",
               "watch": {"group_ids": [5], "path_prefixes": ["dev/backend/"]},
               "review": {"project_map": {}}}
        mrs = [
            {"id": 1, "web_url": "https://gl/dev/backend/app-a/-/merge_requests/1"},
            {"id": 2, "web_url": "https://gl/dev/backend/app-b/-/merge_requests/2"},
            {"id": 3, "web_url": "https://gl/other/x/-/merge_requests/3"},  # 不在 prefix 範圍
        ]
        with mock.patch("gitlab_client.list_group_opened_mrs", return_value=mrs):
            result, errors = poller.poll_opened(cfg)
        self.assertEqual(errors, 0)
        self.assertEqual(set(result), {"dev/backend/app-a", "dev/backend/app-b"})

    def test_run_once_aborts_init_when_poll_failed(self):
        """斷網時絕不建基線——空基線會讓恢復後所有現存 MR 被當成新的。"""
        cfg = {"gitlab_url": "https://gl", "gitlab_token": "t", "slack": {},
               "watch": {"group_ids": [], "path_prefixes": []},
               "review": {"project_map": {"p": "/x"}}}
        with mock.patch.object(poller, "poll_opened", return_value=({}, 1)), \
             mock.patch.object(poller, "load_state", return_value=None), \
             mock.patch.object(poller, "save_state") as save:
            rc_ = poller.run_once(cfg, mock.Mock(), dry_run=False)
        self.assertEqual(rc_, 1)
        save.assert_not_called()  # 關鍵:沒建出空基線

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


class TestIdentities(unittest.TestCase):
    def test_records_everything_needed_to_describe_an_mr_later(self):
        state = {}
        poller.record_identities(state, {"g/app": [
            {"id": 100, "iid": 7, "title": "T", "web_url": "u",
             "author": {"name": "Ann"}, "source_branch": "f", "target_branch": "develop"}]})
        self.assertEqual(state["mrs"]["100"],
                         {"project": "g/app", "iid": 7, "title": "T", "web_url": "u",
                          "author": "Ann", "source_branch": "f", "target_branch": "develop"})

    def test_a_payload_missing_optional_fields_does_not_crash(self):
        state = {}
        poller.record_identities(state, {"g/app": [{"id": 100, "iid": 7}]})
        self.assertEqual(state["mrs"]["100"]["author"], "")

    def test_prune_mrs_follows_seen(self):
        state = {"seen": {"1": "x"}, "mrs": {"1": {}, "2": {}}}
        poller.prune_mrs(state)
        self.assertEqual(list(state["mrs"]), ["1"])


class TestBaselineProjects(unittest.TestCase):
    NOW = poller.parse_dt("2026-07-30T10:00:00Z")
    ADDED_AT = "2026-07-30T09:00:00+00:00"

    def opened(self, *created):
        return {"g/new": [{"id": 100 + i, "created_at": ts} for i, ts in enumerate(created)]}

    def test_pre_existing_mrs_are_silenced(self):
        state = {"seen": {}}
        done = poller.baseline_projects(state, self.opened("2026-07-29T00:00:00Z"),
                                       {"g/new": self.ADDED_AT}, self.NOW)
        self.assertEqual(done, ["g/new"])
        self.assertIn("100", state["seen"])

    def test_mrs_created_after_the_request_are_left_to_be_notified(self):
        # the timestamp is the whole point: a flag that fails to clear must never
        # be able to swallow a genuinely new MR
        state = {"seen": {}}
        poller.baseline_projects(state, self.opened("2026-07-30T09:30:00Z"),
                                {"g/new": self.ADDED_AT}, self.NOW)
        self.assertEqual(state["seen"], {})

    def test_already_seen_entries_keep_their_original_timestamp(self):
        state = {"seen": {"100": "2026-07-01T00:00:00Z"}}
        poller.baseline_projects(state, self.opened("2026-07-29T00:00:00Z"),
                                {"g/new": self.ADDED_AT}, self.NOW)
        self.assertEqual(state["seen"]["100"], "2026-07-01T00:00:00Z")

    def test_a_junk_timestamp_falls_back_to_now(self):
        state = {"seen": {}}
        poller.baseline_projects(state, self.opened("2026-07-29T00:00:00Z"),
                                {"g/new": "nonsense"}, self.NOW)
        self.assertIn("100", state["seen"])

    def test_a_project_with_no_open_mrs_still_clears(self):
        state = {"seen": {}}
        self.assertEqual(
            poller.baseline_projects(state, {}, {"g/new": self.ADDED_AT}, self.NOW), ["g/new"])


class TestPause(unittest.TestCase):
    MRS = {"group/backend-app": [{"id": 1, "iid": 7, "title": "T", "web_url": "u",
                                  "created_at": "2026-07-30T09:00:00Z",
                                  "author": {"name": "A"},
                                  "source_branch": "f", "target_branch": "main"}]}

    def run_once(self, cfg, state):
        with mock.patch.object(poller, "poll_opened", return_value=(self.MRS, 0)), \
             mock.patch.object(poller, "load_state", return_value=state), \
             mock.patch.object(poller, "save_state"), \
             mock.patch.object(poller, "notify_slack", return_value="1.0") as notify, \
             mock.patch.object(poller, "maybe_spawn_review") as spawn, \
             mock.patch.object(poller.overrides, "load", return_value={}):
            poller.run_once(cfg, mock.Mock(), dry_run=False)
        return notify, spawn

    def test_paused_holds_the_mr_without_marking_it_seen(self):
        cfg = dict(CFG, paused_until="2099-01-01T00:00:00+00:00")
        state = {"seen": {}}
        notify, spawn = self.run_once(cfg, state)
        notify.assert_not_called()
        spawn.assert_not_called()
        self.assertEqual(state["seen"], {})        # nothing lost: delivered on resume
        self.assertEqual(state["held_while_paused"], 1)

    def test_an_expired_pause_notifies_normally(self):
        cfg = dict(CFG, paused_until="2020-01-01T00:00:00+00:00")
        notify, spawn = self.run_once(cfg, {"seen": {}})
        notify.assert_called_once()
        spawn.assert_called_once()

    def test_without_a_pause_it_notifies(self):
        notify, _ = self.run_once(dict(CFG), {"seen": {}})
        notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
