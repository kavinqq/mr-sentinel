"""Tests for the Slack listener: event collection, dispatch, and the rerun path.

Slack and GitLab are mocked at the client-module seam, so the whole suite runs
offline in milliseconds.
"""
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import blocks
import commands
import overrides
import review_common
import slack_bot

BOT = "U0BF20M3SR3"
ADMIN = "U085GM4TUHJ"
OTHER = "U0860MNJK6Y"


def config(**review) -> dict:
    cfg = {
        "gitlab_url": "https://git.example.com",
        "gitlab_token": "t",
        "slack": {"bot_token": "xoxb", "channel_id": "C1", "admin_user_ids": [ADMIN]},
        "watch": {"group_ids": ["1", "2"]},
        "review": {
            "language": "zh-TW", "engine": "claude",
            "max_changed_files": 60, "max_diff_lines": 3000,
            "review_timeout_seconds": 900,
            "project_map": {"g/app": "/local/app"},
            "claude": {"model": "claude-opus-4-8", "skeptic_model": "sonnet",
                       "effort": "medium"},
        },
    }
    cfg["review"].update(review)
    return cfg


def state() -> dict:
    return {
        "seen": {"100": "2026-07-30T08:00:00+00:00"},
        "slack_ts": {"100": "1700.0"},
        "mrs": {"100": {"project": "g/app", "iid": "481", "title": "修發信驗證"}},
        "last_poll": "2026-07-30T09:19:02+00:00",
        "poll_errors": 0, "opened_count": 1,
    }


def msg(text, ts="2000.0", user=ADMIN, **extra) -> dict:
    return {"text": text, "ts": ts, "user": user, **extra}


class BotHarness:
    """A Bot with every outbound call captured and temp-file-backed state."""

    def __enter__(self):
        # never touch the real sentinel.db (or GitLab) from a test
        self._history = mock.patch.object(slack_bot.history_sync, "sync_mr")
        self._history.start()
        # a spawn that slips past a test's own mock runs a real review against GitLab and Slack
        self._spawns = [mock.patch.object(module, "spawn_detached")
                        for module in (slack_bot.reviewer, slack_bot.appeal, slack_bot.merge_retry)]
        for patcher in self._spawns:
            patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        # locks and review metas: never the real reviews/ directory
        self._reviews = mock.patch.object(slack_bot.reviewer, "REVIEWS_DIR", root / "reviews")
        self._reviews.start()
        self.bot = slack_bot.Bot(config(), state(), {"bot_user_id": BOT, "cursor": "1000.0"},
                                 bot_state_path=root / "bot_state.json",
                                 overrides_path=root / "overrides.json")
        self.posted = []
        self.reactions = []
        self.bot.say = lambda text, thread_ts=None: (
            self.posted.append((text, thread_ts)) or "9999.0")
        return self

    def __exit__(self, *exc):
        self._history.stop()
        self._reviews.stop()
        for patcher in self._spawns:
            patcher.stop()
        self._tmp.cleanup()

    @property
    def last(self) -> str:
        return self.posted[-1][0] if self.posted else ""

    def overrides(self) -> dict:
        return overrides.load(self.bot.overrides_path)


class TestCollectEvents(unittest.TestCase):
    def test_only_messages_newer_than_the_cursor(self):
        events, cursor = slack_bot.collect_events(
            [msg("new", "2000.0"), msg("old", "500.0")], "1000.0", lambda *a: [])
        self.assertEqual([e["text"] for e in events], ["new"])
        self.assertEqual(cursor, "2000.0")

    def test_cursor_advances_even_when_nothing_is_actionable(self):
        _, cursor = slack_bot.collect_events([msg("chatter", "3000.0")], "1000.0", lambda *a: [])
        self.assertEqual(cursor, "3000.0")

    def test_replies_in_an_old_thread_are_picked_up(self):
        parent = msg("MR notification", "500.0", latest_reply="2500.0")
        replies = [parent, msg("rerun", "2500.0", thread_ts="500.0")]
        events, cursor = slack_bot.collect_events([parent], "1000.0", lambda ts, oldest: replies)
        self.assertEqual([e["text"] for e in events], ["rerun"])
        self.assertEqual(cursor, "2500.0")

    def test_threads_without_new_replies_are_not_fetched(self):
        fetch = mock.Mock(return_value=[])
        slack_bot.collect_events([msg("p", "500.0", latest_reply="600.0")], "1000.0", fetch)
        fetch.assert_not_called()

    def test_a_broadcast_reply_is_not_handled_twice(self):
        parent = msg("p", "500.0", latest_reply="2500.0")
        reply = msg("rerun", "2500.0", thread_ts="500.0", subtype="thread_broadcast")
        events, _ = slack_bot.collect_events([parent, reply], "1000.0",
                                            lambda ts, oldest: [parent, reply])
        self.assertEqual(len(events), 1)

    def test_events_are_ordered_oldest_first(self):
        events, _ = slack_bot.collect_events(
            [msg("b", "3000.0"), msg("a", "2000.0")], "1000.0", lambda *a: [])
        self.assertEqual([e["text"] for e in events], ["a", "b"])


class TestIsActionable(unittest.TestCase):
    def test_own_messages_are_skipped(self):
        self.assertFalse(slack_bot.is_actionable(msg("hi", user=BOT), BOT))

    def test_bot_messages_are_skipped(self):
        self.assertFalse(slack_bot.is_actionable(msg("hi", bot_id="B1"), BOT))

    def test_join_noise_is_skipped(self):
        self.assertFalse(slack_bot.is_actionable(msg("joined", subtype="channel_join"), BOT))

    def test_normal_and_broadcast_messages_pass(self):
        self.assertTrue(slack_bot.is_actionable(msg("hi"), BOT))
        self.assertTrue(slack_bot.is_actionable(msg("hi", subtype="thread_broadcast"), BOT))


class TestTick(unittest.TestCase):
    def tick(self, harness, messages, replies=()):
        with mock.patch.object(slack_bot.slack_client, "conversations_history",
                               return_value=list(messages)), \
             mock.patch.object(slack_bot.slack_client, "conversations_replies",
                               return_value=list(replies)):
            harness.bot.tick()

    def test_first_run_only_baselines_the_cursor(self):
        with BotHarness() as h:
            h.bot.bot_state.pop("cursor")
            self.tick(h, [msg(f"<@{BOT}> status", "2000.0")])
            self.assertEqual(h.bot.bot_state["cursor"], "2000.0")
            self.assertEqual(h.posted, [])          # nothing executed on a fresh install

    def test_cursor_advances_before_the_command_runs(self):
        with BotHarness() as h:
            def boom(*a, **k):
                raise RuntimeError("kaboom")
            h.bot.do_status = boom
            self.tick(h, [msg(f"<@{BOT}> status", "2000.0")])
            self.assertEqual(h.bot.bot_state["cursor"], "2000.0")
            self.assertIn("失敗", h.last)            # reported, not retried forever

    def test_plain_chatter_produces_no_reply(self):
        with BotHarness() as h:
            self.tick(h, [msg("午餐吃什麼", "2000.0")])
            self.assertEqual(h.posted, [])

    def test_status_command_answers_in_thread(self):
        with BotHarness() as h:
            self.tick(h, [msg(f"<@{BOT}> status", "2000.0")])
            self.assertIn("mr-sentinel 狀態", h.last)
            self.assertEqual(h.posted[-1][1], "2000.0")

    def test_non_admin_is_denied_a_settings_change(self):
        with BotHarness() as h:
            self.tick(h, [msg(f"<@{BOT}> set effort high", "2000.0", user=OTHER)])
            self.assertIn("admin", h.last)
            self.assertEqual(h.overrides(), {})

    def test_unknown_command_gets_help(self):
        with BotHarness() as h:
            self.tick(h, [msg(f"<@{BOT}> 幫我買咖啡", "2000.0")])
            self.assertIn("不認得", h.last)
            self.assertIn("status", h.last)

    def test_bare_mention_gets_usage(self):
        with BotHarness() as h:
            self.tick(h, [msg(f"<@{BOT}>", "2000.0")])
            self.assertIn("mr-sentinel", h.last)
            self.assertIn("rerun", h.last)
            self.assertFalse(h.bot.bot_state.get("menus"))   # no widget state at all


class TestSettings(unittest.TestCase):
    def test_set_writes_an_override(self):
        with BotHarness() as h:
            h.bot.dispatch(commands.Command("set", ["effort", "high"]), ADMIN, None)
            self.assertEqual(h.overrides()["set"]["effort"], "high")
            self.assertIn("生效", h.last)

    def test_invalid_value_is_refused_and_nothing_is_written(self):
        with BotHarness() as h:
            h.bot.dispatch(commands.Command("set", ["effort", "turbo"]), ADMIN, None)
            self.assertEqual(h.overrides(), {})
            self.assertIn("只能是", h.last)

    def test_set_without_a_value_lists_the_allowed_values(self):
        with BotHarness() as h:
            h.bot.dispatch(commands.Command("set", ["effort"]), ADMIN, None)
            self.assertIn("medium", h.last)     # current
            self.assertIn("xhigh", h.last)      # allowed

    def test_set_with_no_args_lists_every_setting(self):
        with BotHarness() as h:
            h.bot.dispatch(commands.Command("set", []), ADMIN, None)
            self.assertIn("`engine`", h.last)
            self.assertIn("`timeout`", h.last)
            self.assertIn("claude-opus-4-8", h.last)   # current model from config

    def test_automerge_with_no_args_explains_itself(self):
        with BotHarness() as h:
            h.bot.dispatch(commands.Command("automerge", []), ADMIN, None)
            self.assertIn("on / off", h.last)
            self.assertEqual(h.overrides(), {})

    def test_automerge_is_a_shortcut_for_set(self):
        with BotHarness() as h:
            h.bot.dispatch(commands.Command("automerge", ["on"]), ADMIN, None)
            self.assertIs(h.overrides()["set"]["automerge"], True)

    def test_reset_restores_the_config_value(self):
        with BotHarness() as h:
            h.bot.dispatch(commands.Command("set", ["effort", "high"]), ADMIN, None)
            h.bot.dispatch(commands.Command("reset", ["effort"]), ADMIN, None)
            self.assertNotIn("effort", h.overrides().get("set", {}))

    def test_pause_then_resume(self):
        with BotHarness() as h:
            h.bot.dispatch(commands.Command("pause", ["2h"]), ADMIN, None)
            self.assertTrue(overrides.is_paused(h.overrides(), datetime.now(timezone.utc)))
            h.bot.dispatch(commands.Command("resume", []), ADMIN, None)
            self.assertFalse(overrides.is_paused(h.overrides(), datetime.now(timezone.utc)))

    def test_pause_refuses_an_absurd_duration(self):
        with BotHarness() as h:
            h.bot.dispatch(commands.Command("pause", ["3d"]), ADMIN, None)
            self.assertIsNone(h.overrides().get("paused_until"))
            self.assertIn("最多", h.last)


class TestProjects(unittest.TestCase):
    def test_add_requires_an_existing_local_clone(self):
        with BotHarness() as h:
            h.bot.dispatch(commands.Command("projects", ["add", "g/new", "/nope"]), ADMIN, None)
            self.assertIn("不存在", h.last)
            self.assertEqual(h.overrides(), {})

    def test_add_requires_the_local_path_to_be_a_git_repo(self):
        with BotHarness() as h, tempfile.TemporaryDirectory() as plain:
            h.bot.dispatch(commands.Command("projects", ["add", "g/new", plain]), ADMIN, None)
            self.assertIn("git repo", h.last)
            self.assertEqual(h.overrides(), {})

    def test_add_verifies_the_project_exists_on_gitlab(self):
        import urllib.error
        with BotHarness() as h, tempfile.TemporaryDirectory() as clone:
            (Path(clone) / ".git").mkdir()
            with mock.patch.object(slack_bot.gitlab_client, "get_project",
                                   side_effect=urllib.error.HTTPError(
                                       "u", 404, "nf", None, None)):
                h.bot.dispatch(commands.Command("projects", ["add", "g/typo", clone]),
                               ADMIN, None)
            self.assertIn("找不到", h.last)
            self.assertEqual(h.overrides(), {})

    def test_add_queues_a_baseline_so_the_backlog_is_not_notified(self):
        with BotHarness() as h, tempfile.TemporaryDirectory() as clone:
            (Path(clone) / ".git").mkdir()
            with mock.patch.object(slack_bot.gitlab_client, "get_project",
                                   return_value={"path_with_namespace": "g/new"}):
                h.bot.dispatch(commands.Command("projects", ["add", "g/new", clone]),
                               ADMIN, None)
            ov = h.overrides()
            self.assertEqual(ov["projects"]["add"]["g/new"], clone)
            self.assertIn("g/new", ov["_baseline_pending"])
            self.assertIn("不會補通知", h.last)

    def test_remove_only_accepts_a_watched_project(self):
        with BotHarness() as h:
            h.bot.dispatch(commands.Command("projects", ["rm", "g/unknown"]), ADMIN, None)
            self.assertIn("不在監看清單", h.last)
            h.bot.dispatch(commands.Command("projects", ["rm", "g/app"]), ADMIN, None)
            self.assertIn("g/app", h.overrides()["projects"]["remove"])


class TestRerun(unittest.TestCase):
    AWARDS = [{"id": 7, "name": "eyes", "user": {"id": 42}}]
    DISCUSSIONS = [
        {"notes": [{"id": 1, "author": {"id": 42},
                    "body": "🔴 [High] bug\n\n— 🤖 mr-sentinel AI review (x)"}]},
        {"notes": [{"id": 2, "author": {"id": 42},
                    "body": "🟠 [Medium] y\n\n— 🤖 mr-sentinel AI review (x)"},
                   {"id": 3, "author": {"id": 99}, "body": "這個其實沒問題"},
                   {"id": 4, "author": {"id": 42},
                    "body": f"不成立 {review_common.APPEAL_REJECT}\n"
                            f"{review_common.JUDGED_ON_MARKER.format('a1:None')}"
                            f"\n\n— 🤖 mr-sentinel AI review (x)"}]},
    ]

    def rerun(self, harness, args, user=ADMIN, thread_ts=None, awards=None, discussions=None):
        gl = slack_bot.gitlab_client
        with mock.patch.object(gl, "get_current_user", return_value={"id": 42}), \
             mock.patch.object(gl, "get_mr", return_value={"id": 100, "state": "opened",
                                                          "sha": "a1"}), \
             mock.patch.object(gl, "get_award_emojis",
                               return_value=self.AWARDS if awards is None else awards), \
             mock.patch.object(gl, "delete_award_emoji") as unclaim, \
             mock.patch.object(gl, "list_discussions",
                               return_value=self.DISCUSSIONS if discussions is None
                               else discussions), \
             mock.patch.object(gl, "delete_note") as delete_note, \
             mock.patch.object(slack_bot.reviewer, "spawn_detached") as spawn, \
             mock.patch.object(slack_bot.appeal, "spawn_detached") as self.appeal_spawn:
            harness.bot.dispatch(commands.Command("rerun", args), user, thread_ts)
        return unclaim, delete_note, spawn

    def test_replies_waiting_are_judged_before_the_rerun(self):
        """RDs fix some comments and answer others, then press one button."""
        waiting = [*self.DISCUSSIONS[:1], {"id": "d2", "notes": self.DISCUSSIONS[1]["notes"][:2]}]
        with BotHarness() as h:
            _, delete_note, spawn = self.rerun(h, ["g/app!481"], discussions=waiting)
            spawn.assert_not_called()
            self.appeal_spawn.assert_called_once_with("g/app", "481", "100", review_mode="auto")
            self.assertEqual([c.args[-1] for c in delete_note.call_args_list], [1])
            self.assertIn("先判斷 1 則回覆", h.last)

    def test_explicit_target_unclaims_and_respawns(self):
        with BotHarness() as h:
            unclaim, _, spawn = self.rerun(h, ["g/app!481", "deep"])
            unclaim.assert_called_once()
            spawn.assert_called_once_with("g/app", "481", "100", "deep")
            self.assertIn("重跑中", h.last)

    def test_unrecorded_comments_are_not_deleted(self):
        """History write failed: deleting now would lose those findings for good."""
        with BotHarness() as h:
            slack_bot.history_sync.sync_mr.return_value = False
            _, delete_note, spawn = self.rerun(h, ["g/app!481"])
            delete_note.assert_not_called()
            spawn.assert_called_once()                              # the rerun still happens
            self.assertIn("舊留言先保留", h.last)

    def test_default_mode_is_auto(self):
        with BotHarness() as h:
            _, _, spawn = self.rerun(h, ["g/app!481"])
            self.assertEqual(spawn.call_args.args[3], "auto")

    def test_bare_iid_resolves_through_the_known_mrs(self):
        with BotHarness() as h:
            _, _, spawn = self.rerun(h, ["!481"])
            spawn.assert_called_once_with("g/app", "481", "100", "auto")

    def test_inside_a_notification_thread_the_mr_is_implied(self):
        with BotHarness() as h:
            _, _, spawn = self.rerun(h, [], thread_ts="1700.0")
            spawn.assert_called_once_with("g/app", "481", "100", "auto")

    def test_with_no_target_and_no_thread_it_lists_runnable_commands(self):
        with BotHarness() as h:
            _, _, spawn = self.rerun(h, [])
            spawn.assert_not_called()
            self.assertIn("`rerun g/app!481`", h.last)   # copy-a-line, no widget state

    def test_only_unreplied_ai_comments_are_deleted(self):
        with BotHarness() as h:
            _, delete_note, _ = self.rerun(h, ["g/app!481"])
            self.assertEqual([c.args[-1] for c in delete_note.call_args_list], [1])
            self.assertIn("清掉 1 則", h.last)

    def test_a_project_outside_the_review_list_is_refused(self):
        with BotHarness() as h:
            _, _, spawn = self.rerun(h, ["g/other!5"])
            spawn.assert_not_called()
            self.assertIn("不在 review 清單", h.last)

    def test_unknown_iid_asks_for_the_project(self):
        with BotHarness() as h:
            _, _, spawn = self.rerun(h, ["!999"])
            spawn.assert_not_called()
            self.assertIn("找不到", h.last)

    def test_non_admin_is_rate_limited(self):
        with BotHarness() as h:
            now = datetime.now(timezone.utc)
            h.bot.bot_state["rerun_log"] = [
                (now - timedelta(minutes=1)).isoformat()
                for _ in range(commands.RERUN_LIMIT_PER_HOUR)]
            _, _, spawn = self.rerun(h, ["g/app!481"], user=OTHER)
            spawn.assert_not_called()
            self.assertIn("太頻繁", h.last)

    def test_admin_is_not_rate_limited(self):
        with BotHarness() as h:
            now = datetime.now(timezone.utc)
            h.bot.bot_state["rerun_log"] = [now.isoformat() for _ in range(50)]
            _, _, spawn = self.rerun(h, ["g/app!481"], user=ADMIN)
            spawn.assert_called_once()

    def test_a_never_claimed_mr_still_reruns(self):
        with BotHarness() as h:
            unclaim, _, spawn = self.rerun(h, ["g/app!481"], awards=[])
            unclaim.assert_not_called()
            spawn.assert_called_once()
            self.assertIn("沒有認領標記", h.last)


def click_payload(args, user=ADMIN, channel="C1", message_ts="3000.0", verb="rerun"):
    import json as _json
    buttons = slack_bot.blocks.rerun_buttons("g/app", 481)
    return {
        "type": "block_actions", "user": {"id": user}, "channel": {"id": channel},
        "container": {"type": "message", "message_ts": message_ts, "channel_id": channel},
        "message": {"ts": message_ts, "thread_ts": "1700.0", "text": "done",
                    "blocks": slack_bot.blocks.message("done", buttons)},
        "actions": [{"action_id": "mrs:rerun:auto",
                     "value": _json.dumps({"verb": verb, "args": args})}],
    }


class TestButtons(unittest.TestCase):
    def press(self, harness, payload, rerun_log=None):
        if rerun_log is not None:
            harness.bot.bot_state["rerun_log"] = rerun_log
        gl = slack_bot.gitlab_client
        with mock.patch.object(gl, "get_current_user", return_value={"id": 42}), \
             mock.patch.object(gl, "get_award_emojis", return_value=[]), \
             mock.patch.object(gl, "delete_award_emoji"), \
             mock.patch.object(gl, "list_discussions", return_value=[]), \
             mock.patch.object(gl, "delete_note"), \
             mock.patch.object(slack_bot.reviewer, "spawn_detached") as spawn, \
             mock.patch.object(slack_bot.slack_client, "chat_update") as update, \
             mock.patch.object(slack_bot.slack_client, "post_ephemeral") as whisper:
            harness.bot.on_socket_event("interactive", payload)
        return spawn, update, whisper

    def test_click_reruns_in_the_thread_and_retires_the_buttons(self):
        with BotHarness() as h:
            spawn, update, _ = self.press(h, click_payload(["g/app!481", "deep"]))
            spawn.assert_called_once_with("g/app", "481", "100", "deep")
            self.assertEqual(h.posted[-1][1], "1700.0")          # reply lands in the MR thread
            channel, ts, _, new_blocks = update.call_args.args[1:]
            self.assertEqual((channel, ts), ("C1", "3000.0"))
            self.assertNotIn("actions", [b["type"] for b in new_blocks])
            self.assertIn(ADMIN, new_blocks[-1]["elements"][0]["text"])

    def test_failed_message_update_is_retried_then_explained(self):
        with BotHarness() as h:
            gl = slack_bot.gitlab_client
            with mock.patch.object(gl, "get_current_user", return_value={"id": 42}), \
                 mock.patch.object(gl, "get_award_emojis", return_value=[]), \
                 mock.patch.object(gl, "list_discussions", return_value=[]), \
                 mock.patch.object(slack_bot.reviewer, "spawn_detached"), \
                 mock.patch.object(slack_bot.slack_client, "chat_update",
                                   side_effect=RuntimeError("cant_update_message")) as update, \
                 self.assertLogs("mr_sentinel.slack_bot", "ERROR"):
                h.bot.on_socket_event("interactive", click_payload(["g/app!481"]))
            self.assertEqual(update.call_count, 2)
            self.assertIn("不會重複執行", h.last)
            self.assertIn("3000.0", h.bot.bot_state["clicks"])      # still claimed: it ran
            # even after the bounded click log forgets it, the visible button stays dead
            h.bot.bot_state["clicks"] = {}
            with mock.patch.object(slack_bot.reviewer, "spawn_detached") as spawn, \
                 mock.patch.object(slack_bot.slack_client, "post_ephemeral"):
                h.bot.on_socket_event("interactive", click_payload(["g/app!481"]))
            spawn.assert_not_called()

    def test_press_while_a_review_runs_keeps_the_buttons_and_touches_nothing(self):
        import fcntl
        with BotHarness() as h, tempfile.TemporaryDirectory() as reviews, \
             mock.patch.object(slack_bot.reviewer, "REVIEWS_DIR", Path(reviews)), \
             open(Path(reviews) / ".lock-100", "w") as running, \
             mock.patch.object(slack_bot.gitlab_client, "delete_note") as delete_note:
            fcntl.flock(running, fcntl.LOCK_EX)           # a reviewer holds the MR's lock
            spawn, update, _ = self.press(h, click_payload(["g/app!481"]))
        spawn.assert_not_called()
        delete_note.assert_not_called()
        update.assert_not_called()
        self.assertIn("正在 review", h.last)

    def test_rerun_holds_the_lock_while_it_resets_and_spawns(self):
        """No gap between "is a review running?" and the reset, or a review could start
        in it and have its claim and comments deleted underneath."""
        import fcntl
        held_during_spawn = []
        with BotHarness() as h, tempfile.TemporaryDirectory() as reviews, \
             mock.patch.object(slack_bot.reviewer, "REVIEWS_DIR", Path(reviews)):
            def spawn(*_):
                with open(Path(reviews) / ".lock-100", "w") as probe:
                    try:
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        held_during_spawn.append(False)
                    except BlockingIOError:
                        held_during_spawn.append(True)
            gl = slack_bot.gitlab_client
            with mock.patch.object(gl, "get_current_user", return_value={"id": 42}), \
                 mock.patch.object(gl, "get_award_emojis", return_value=[]), \
                 mock.patch.object(gl, "list_discussions", return_value=[]), \
                 mock.patch.object(slack_bot.reviewer, "spawn_detached", side_effect=spawn), \
                 mock.patch.object(slack_bot.slack_client, "chat_update"):
                h.bot.on_socket_event("interactive", click_payload(["g/app!481"]))
        self.assertEqual(held_during_spawn, [True])

    def test_second_press_of_the_same_message_is_ignored(self):
        with BotHarness() as h:
            self.press(h, click_payload(["g/app!481"]))
            spawn, _, whisper = self.press(h, click_payload(["g/app!481"], user=OTHER))
            spawn.assert_not_called()
            whisper.assert_called_once()

    def test_rate_limited_press_keeps_the_buttons(self):
        now = datetime.now(timezone.utc).isoformat()
        with BotHarness() as h:
            spawn, update, _ = self.press(h, click_payload(["g/app!481"], user=OTHER),
                                          rerun_log=[now] * commands.RERUN_LIMIT_PER_HOUR)
            spawn.assert_not_called()
            update.assert_not_called()
            self.assertIn("太頻繁", h.last)
            self.assertNotIn("3000.0", h.bot.bot_state.get("clicks", {}))   # can press later

    def test_forged_verb_is_refused(self):
        """A button value is user-controlled data: it must not reach admin verbs."""
        with BotHarness() as h:
            with mock.patch.object(h.bot, "dispatch") as dispatch:
                self.press(h, click_payload(["effort", "high"], verb="set"))
            dispatch.assert_not_called()

    def test_click_from_another_channel_is_ignored(self):
        with BotHarness() as h:
            spawn, _, _ = self.press(h, click_payload(["g/app!481"], channel="C999"))
            spawn.assert_not_called()


class TestAppeal(unittest.TestCase):
    AI = "🟠 [Medium] x\n\n— 🤖 mr-sentinel AI review (m)"

    def appeal(self, harness, discussions, args=("g/app!481",), via_button=False,
               mr_state="opened", head="h1", reviewed="h1", user=ADMIN, meta=None,
               target="master", busy=False, delete_error=None):
        gl = slack_bot.gitlab_client
        meta = {"head_sha": reviewed, "target_branch": "master", **(meta or {})} \
            if reviewed else {}
        with mock.patch.object(gl, "get_current_user", return_value={"id": 42}), \
             mock.patch.object(gl, "list_discussions", return_value=discussions), \
             mock.patch.object(gl, "get_mr", return_value={
                 "id": 100, "state": mr_state, "sha": head, "target_branch": target,
                 "web_url": "http://gl/g/app/-/merge_requests/481"}), \
             mock.patch.object(slack_bot.reviewer, "read_review_meta", return_value=meta), \
             mock.patch.object(slack_bot.Bot, "mr_busy", return_value=busy), \
             mock.patch.object(gl, "unresolve_discussion") as self.reopened, \
             mock.patch.object(gl, "get_award_emojis", return_value=[]), \
             mock.patch.object(gl, "delete_note", side_effect=delete_error) as self.deleted, \
             mock.patch.object(slack_bot.reviewer, "spawn_detached") as self.review_spawn, \
             mock.patch.object(slack_bot.merge_retry, "spawn_detached") as self.merge_spawn, \
             mock.patch.object(slack_bot.appeal, "spawn_detached") as spawn, \
             mock.patch.object(slack_bot.slack_client, "chat_update") as update, \
             mock.patch.object(slack_bot.slack_client, "post_ephemeral"):
            if via_button:
                harness.bot.on_socket_event("interactive",
                                            click_payload(list(args), verb="appeal"))
            else:
                harness.bot.dispatch(commands.Command("appeal", list(args)), user, None)
        return spawn, update

    def replied(self):
        return [{"id": "d1", "notes": [{"author": {"id": 42}, "body": self.AI},
                                       {"author": {"id": 99, "name": "dev"}, "body": "不用修"}]}]

    def test_replies_waiting_spawn_the_judge(self):
        with BotHarness() as h:
            spawn, _ = self.appeal(h, self.replied())
            spawn.assert_called_once_with("g/app", "481", "100")
            self.assertIn("1 則回覆", h.last)
            self.assertEqual(len(h.bot.bot_state["rerun_log"]), 1)   # shares the budget

    def test_new_commits_are_re_reviewed_after_the_replies_are_judged(self):
        """RDs fix some comments and answer others, then press one button."""
        with BotHarness() as h:
            spawn, _ = self.appeal(h, self.replied(), head="h2")
            spawn.assert_called_once_with("g/app", "481", "100", review_mode="auto")
            self.assertIn("重跑中", h.last)
            self.assertIn("先判斷 1 則回覆", h.last)

    def test_new_commits_and_no_reply_just_re_review(self):
        with BotHarness() as h:
            spawn, _ = self.appeal(h, [{"id": "d1", "notes": [{"id": 1, "author": {"id": 42},
                                                               "body": self.AI}]}], head="h2")
            spawn.assert_not_called()
            self.review_spawn.assert_called_once_with("g/app", "481", "100", "auto")
            self.assertNotIn("還沒看到新的回覆", h.last)

    def test_new_commits_also_retry_an_accept_whose_resolve_failed(self):
        accepted = f"成立 {review_common.APPEAL_ACCEPT}\n\n— 🤖 mr-sentinel AI review (m)"
        stuck = [{"id": "d1", "notes": [
            {"id": 1, "author": {"id": 42}, "body": self.AI, "resolvable": True},
            {"id": 2, "author": {"id": 99}, "body": "不用修"},
            {"id": 3, "author": {"id": 42}, "body": accepted}]}]
        with BotHarness() as h:
            spawn, _ = self.appeal(h, stuck, head="h2")
            spawn.assert_called_once_with("g/app", "481", "100", review_mode="auto")
            self.review_spawn.assert_not_called()

    def test_no_reply_yet_explains_and_spends_nothing(self):
        with BotHarness() as h:
            spawn, _ = self.appeal(h, [{"id": "d1", "notes": [{"author": {"id": 42},
                                                               "body": self.AI}]}])
            spawn.assert_not_called()
            self.assertIn("沒看到新的 commit,也沒看到新的回覆", h.last)
            self.assertIn("push", h.last)             # fixed: the new commit is the signal
            self.assertIn("立刻加入留言", h.last)     # a "Start a review" draft is invisible to us
            self.assertIn("最下方", h.last)           # a top-level comment is not a reply
            self.assertIn(f"再按一次「{blocks.CHECK_LABEL}」", h.last)
            self.assertEqual(h.bot.bot_state.get("rerun_log", []), [])

    def test_closed_mr_says_so_and_points_to_the_replacement(self):
        """The developer closed the MR, reopened the change as a new one and replied
        there — pressing the old thread's button must not claim "no reply yet"."""
        with BotHarness() as h:
            h.bot.state["mrs"]["101"] = {"project": "g/app", "iid": 482, "author": "dev",
                                         "web_url": "http://gl/g/app/-/merge_requests/482"}
            h.bot.state["mrs"]["100"]["author"] = "dev"
            spawn, _ = self.appeal(h, [{"id": "d1", "notes": [{"author": {"id": 42},
                                                               "body": self.AI}]}],
                                   mr_state="closed")
            spawn.assert_not_called()
            self.assertNotIn("沒看到新的回覆", h.last)
            self.assertIn("已關閉", h.last)
            self.assertIn("g/app!482", h.last)

    def test_button_retires_with_appeal_note(self):
        with BotHarness() as h:
            _, update = self.appeal(h, self.replied(), via_button=True)
            note = update.call_args.args[4][-1]["elements"][0]["text"]
            self.assertIn("重新檢查", note)

    def test_aliases_and_tier(self):
        self.assertEqual(commands.parse(f"<@{BOT}> 不用修 !481", BOT).verb, "appeal")
        self.assertIsNone(commands.authorize(commands.Command("appeal", []), OTHER, {}))



class TestCheckButtonScenarios(TestAppeal):
    """Every case of the one 「🔁 已修正或已回覆,重新檢查」 button. The developer only
    says "I'm done"; whether they fixed or answered is ours to work out. h1 is the
    reviewed head, h2 a newer push. Numbers match the scenario table in the README."""
    AI = TestAppeal.AI
    R = {"resolvable": True, "resolved": True, "resolved_by": {"id": 99}}

    def finding(self, *replies, did="d1", **extra):
        return {"id": did, "notes": [{"id": 1, "author": {"id": 42}, "body": self.AI, **extra},
                                     *replies]}

    def reply(self, body="不用改,這是刻意的"):
        return {"id": 2, "author": {"id": 99, "name": "dev"}, "body": body}

    def reject(self, head):
        body = review_common.appeal_reply_body("reject", "還是會壞", "m",
                                               judged_on=f"{head}:master")
        return {"id": 3, "author": {"id": 42}, "body": body}

    def accept(self, **extra):
        return {"id": 3, "author": {"id": 42},
                "body": review_common.appeal_reply_body("accept", "ok", "m"), **extra}

    def budget(self, h):
        return len(h.bot.bot_state.get("rerun_log", []))

    # ---- the developer pushed a fix (head moved past the review) ----

    def test_01_fixed_and_pushed_no_reply_re_reviews_and_drops_the_old_comment(self):
        with BotHarness() as h:
            spawn, _ = self.appeal(h, [self.finding()], head="h2")
            spawn.assert_not_called()
            self.review_spawn.assert_called_once_with("g/app", "481", "100", "auto")
            self.assertEqual(self.deleted.call_count, 1)    # re-review decides if it is still there

    def test_02_fixed_and_pushed_with_a_reply_is_judged_then_re_reviewed(self):
        with BotHarness() as h:
            spawn, _ = self.appeal(h, [self.finding(self.reply("已修正"))], head="h2")
            spawn.assert_called_once_with("g/app", "481", "100", review_mode="auto")
            self.deleted.assert_not_called()                # a conversation is never deleted

    def test_03_fixed_some_answered_others_then_pushed(self):
        threads = [self.finding(did="fixed"), self.finding(self.reply(), did="answered")]
        with BotHarness() as h:
            spawn, _ = self.appeal(h, threads, head="h2")
            spawn.assert_called_once_with("g/app", "481", "100", review_mode="auto")
            self.assertEqual(self.deleted.call_count, 1)
            self.assertIn("先判斷 1 則回覆,再重審", h.last)

    def test_04_rejected_then_fixed_as_asked_without_a_reply_is_re_judged_on_the_new_code(self):
        with BotHarness() as h:
            spawn, _ = self.appeal(h, [self.finding(self.reply(), self.reject("h1"))], head="h2")
            spawn.assert_called_once_with("g/app", "481", "100", review_mode="auto")
            self.assertIn("再確認 1 則之前判定要修的留言", h.last)

    def test_05_no_review_on_record_counts_as_changed_code(self):
        with BotHarness() as h:
            spawn, _ = self.appeal(h, [self.finding()], head="h2", reviewed=None)
            self.review_spawn.assert_called_once_with("g/app", "481", "100", "auto")

    # ---- nothing pushed since the review ----

    def test_06_answered_without_a_push_is_judged_only(self):
        with BotHarness() as h:
            spawn, _ = self.appeal(h, [self.finding(self.reply())])
            spawn.assert_called_once_with("g/app", "481", "100")
            self.review_spawn.assert_not_called()           # same code: no reworded repeat
            self.assertEqual(self.budget(h), 1)

    def test_07_a_reject_on_an_older_head_is_re_judged_even_after_the_review_moved_on(self):
        """Codex: the re-judge failed but the review that followed recorded h2."""
        with BotHarness() as h:
            spawn, _ = self.appeal(h, [self.finding(self.reply(), self.reject("h1"))],
                                   head="h2", reviewed="h2")
            spawn.assert_called_once_with("g/app", "481", "100")
            self.review_spawn.assert_not_called()

    def test_08_a_reject_on_this_head_waits_for_a_reply_or_a_fix(self):
        with BotHarness() as h:
            spawn, _ = self.appeal(h, [self.finding(self.reply(), self.reject("h1"))])
            spawn.assert_not_called()
            self.review_spawn.assert_not_called()
            self.assertIn("沒看到新的 commit,也沒看到新的回覆", h.last)
            self.assertEqual(self.budget(h), 0)

    def test_09_pressed_before_doing_anything_explains_both_ways(self):
        with BotHarness() as h:
            spawn, _ = self.appeal(h, [self.finding()])
            spawn.assert_not_called()
            self.assertIn("push 上去", h.last)
            self.assertIn("Reply 寫理由", h.last)
            self.assertIn("目前有 1 則還沒處理", h.last)
            self.assertIn("改原本的回覆不算", h.last)   # row 19: an edited reply is not seen
            self.assertEqual(self.budget(h), 0)

    def test_10_resolved_by_the_developer_without_a_reply_or_a_push_is_not_done(self):
        with BotHarness() as h:
            spawn, _ = self.appeal(h, [self.finding(**self.R)])
            spawn.assert_not_called()
            self.merge_spawn.assert_not_called()
            self.assertIn("目前有 1 則還沒處理", h.last)

    def test_10b_what_is_left_is_reopened_on_gitlab_and_linked(self):
        """!60: the one rejected comment hid among 17 resolved ones on GitLab."""
        with BotHarness() as h:
            self.appeal(h, [self.finding(**self.R)])
            self.assertEqual([c.args[4] for c in self.reopened.call_args_list], ["d1"])
            self.assertIn("http://gl/g/app/-/merge_requests/481#note_1", h.last)

    def test_10c_a_busy_mr_is_listed_but_not_touched(self):
        """Codex: an admin override may be closing that thread right now."""
        import fcntl
        with BotHarness() as h:
            slack_bot.reviewer.REVIEWS_DIR.mkdir(parents=True)
            with open(slack_bot.reviewer.REVIEWS_DIR / ".lock-100", "w") as running:
                fcntl.flock(running, fcntl.LOCK_EX)
                self.appeal(h, [self.finding(**self.R)])
            self.reopened.assert_not_called()
            self.assertIn("#note_1", h.last)

    def test_10d_the_lock_is_taken_before_the_read_it_acts_on(self):
        """Codex: a read before the lock may be stale by the time it is reopened."""
        order = []
        real_take = slack_bot.reviewer.take_lock
        with BotHarness() as h, \
             mock.patch.object(slack_bot.reviewer, "take_lock",
                               side_effect=lambda f, *a: order.append("lock") or real_take(f, *a)):
            gl = slack_bot.gitlab_client
            calls = []
            with mock.patch.object(gl, "get_current_user", return_value={"id": 42}), \
                 mock.patch.object(gl, "unresolve_discussion"), \
                 mock.patch.object(gl, "list_discussions",
                                   side_effect=lambda *a: calls.append(order[:]) or
                                   [self.finding(**self.R)]):
                h.bot.reopen_under_lock("http://gl", "t", "g/app", "481", "100")
        self.assertEqual(calls, [["lock"]])

    def test_11_everything_closed_but_not_merged_checks_the_merge(self):
        bot_resolved = {"resolvable": True, "resolved": True, "resolved_by": {"id": 42}}
        with BotHarness() as h:
            spawn, _ = self.appeal(h, [self.finding(self.reply(), self.accept(**bot_resolved),
                                                    **bot_resolved)])
            spawn.assert_not_called()
            self.merge_spawn.assert_called_once_with("g/app", "481", "100")
            self.assertEqual(self.budget(h), 0)

    def test_12_an_accept_gitlab_refused_to_resolve_is_retried_free(self):
        unresolved = {"resolvable": True, "resolved": False}
        with BotHarness() as h:
            spawn, _ = self.appeal(h, [self.finding(self.reply(), self.accept(**unresolved),
                                                    **unresolved)])
            spawn.assert_called_once_with("g/app", "481", "100")
            self.assertEqual(self.budget(h), 0)

    def test_13_a_merged_or_closed_mr_says_so(self):
        with BotHarness() as h:
            spawn, _ = self.appeal(h, [self.finding(self.reply())], mr_state="merged")
            spawn.assert_not_called()
            self.review_spawn.assert_not_called()
            self.assertNotIn("沒看到新的", h.last)

    # ---- the last review no longer stands (Codex round 2) ----

    def test_14_an_unfinished_review_on_this_head_is_re_run(self):
        with BotHarness() as h:
            self.appeal(h, [self.finding()], meta={"mode": slack_bot.reviewer.UNFINISHED_MODE})
            self.review_spawn.assert_called_once_with("g/app", "481", "100", "auto")

    def test_15_retargeted_without_a_new_commit_is_re_reviewed(self):
        with BotHarness() as h:
            self.appeal(h, [self.finding()], target="main")
            self.review_spawn.assert_called_once_with("g/app", "481", "100", "auto")

    def test_15d_a_reject_is_re_judged_once_the_target_changed(self):
        """Codex P2: the same commit aimed at another branch is another change."""
        with BotHarness() as h:
            spawn, _ = self.appeal(h, [self.finding(self.reply(), self.reject("h1"))],
                                   target="main")
            spawn.assert_called_once_with("g/app", "481", "100", review_mode="auto")

    def test_15b_a_meta_from_before_targets_were_recorded_falls_back_to_the_guard(self):
        with BotHarness() as h:
            self.appeal(h, [self.finding()], target="main",
                        meta={"target_branch": None, "guard": {"target_branch": "master"}})
            self.review_spawn.assert_called_once_with("g/app", "481", "100", "auto")

    def test_15c_no_target_on_record_at_all_is_re_reviewed(self):
        with BotHarness() as h:
            self.appeal(h, [self.finding()], meta={"target_branch": None})
            self.review_spawn.assert_called_once_with("g/app", "481", "100", "auto")

    def test_16a_old_comments_a_rerun_could_not_clear_force_another_rerun(self):
        with BotHarness() as h:
            h.bot.bot_state["cleanup_pending"] = {"100": "2026-11-05T12:00:00"}
            self.appeal(h, [self.finding()])
            self.review_spawn.assert_called_once_with("g/app", "481", "100", "auto")

    def test_16c_a_rerun_voids_the_last_verdict_before_deleting_its_comments(self):
        """Codex P1: the old deep verdict outlived its deleted comments and merged."""
        with BotHarness() as h:
            work = slack_bot.reviewer.REVIEWS_DIR / "100"
            work.mkdir(parents=True)
            slack_bot.reviewer.write_review_meta(work, "deep", "h1", 1, None, "master")
            gl = slack_bot.gitlab_client
            with mock.patch.object(gl, "get_current_user", return_value={"id": 42}), \
                 mock.patch.object(gl, "get_award_emojis", return_value=[]), \
                 mock.patch.object(gl, "list_discussions", return_value=[self.finding()]), \
                 mock.patch.object(gl, "delete_note"):
                h.bot.rerun_mr("g/app", "481", "100", "auto", None)
            self.assertEqual(slack_bot.reviewer.read_review_meta(work)["mode"],
                             slack_bot.reviewer.UNFINISHED_MODE)

    def test_16b_a_failed_delete_is_remembered_for_the_next_press(self):
        refused = urllib.error.HTTPError("u", 500, "x", {}, None)
        with BotHarness() as h:
            self.appeal(h, [self.finding()], head="h2", delete_error=refused)
            self.review_spawn.assert_called_once()          # the rerun itself still runs
            self.assertIn("100", h.bot.bot_state["cleanup_pending"])
            self.assertIn("1 則舊留言刪除失敗", h.last)
        with BotHarness() as h:                             # a clean rerun clears the debt
            h.bot.bot_state["cleanup_pending"] = {"100": "2026-11-05T12:00:00"}
            self.appeal(h, [self.finding()], head="h2")
            self.assertNotIn("100", h.bot.bot_state["cleanup_pending"])

    # ---- quota and concurrency: only a model run costs quota ----

    def full_budget(self, h):
        h.bot.bot_state["rerun_log"] = [h.bot.now().isoformat()] * commands.RERUN_LIMIT_PER_HOUR

    def test_17a_quota_used_up_still_lets_the_merge_check_through(self):
        bot_resolved = {"resolvable": True, "resolved": True, "resolved_by": {"id": 42}}
        with BotHarness() as h:
            self.full_budget(h)
            self.appeal(h, [self.finding(self.reply(), self.accept(**bot_resolved),
                                         **bot_resolved)], user=OTHER)
            self.merge_spawn.assert_called_once()

    def test_17b_quota_used_up_still_tells_what_to_do(self):
        with BotHarness() as h:
            self.full_budget(h)
            self.appeal(h, [self.finding()], user=OTHER)
            self.assertIn("沒看到新的 commit", h.last)

    def test_17c_quota_used_up_refuses_a_model_run(self):
        with BotHarness() as h:
            self.full_budget(h)
            spawn, _ = self.appeal(h, [self.finding(self.reply())], user=OTHER)
            spawn.assert_not_called()
            self.assertIn("重跑太頻繁", h.last)

    def test_18_pressed_while_a_review_runs_costs_nothing(self):
        with BotHarness() as h:
            spawn, _ = self.appeal(h, [self.finding(self.reply())], busy=True)
            spawn.assert_not_called()
            self.assertIn("跑完後再按一次", h.last)
            self.assertEqual(self.budget(h), 0)

class TestSocketEvents(unittest.TestCase):
    def mention(self, ts, text=f"<@{BOT}> status", **extra):
        return {"event": {"type": "app_mention", "channel": "C1", "user": ADMIN,
                          "text": text, "ts": ts, **extra}}

    def test_mention_is_handled_and_advances_the_cursor(self):
        with BotHarness() as h:
            with mock.patch.object(h.bot, "handle_message") as handle:
                h.bot.on_socket_event("events_api", self.mention("2000.0"))
            handle.assert_called_once()
            self.assertEqual(h.bot.bot_state["cursor"], "2000.0")

    def test_same_mention_twice_runs_once(self):
        with BotHarness() as h:
            with mock.patch.object(h.bot, "handle_message") as handle:
                h.bot.on_socket_event("events_api", self.mention("2000.0"))
                h.bot.on_socket_event("events_api", self.mention("2000.0"))
            handle.assert_called_once()

    def test_older_mention_arriving_late_still_runs(self):
        """Socket delivery is not ordered: a cursor alone would drop this one."""
        with BotHarness() as h:
            with mock.patch.object(h.bot, "handle_message") as handle:
                h.bot.on_socket_event("events_api", self.mention("2000.0"))
                h.bot.on_socket_event("events_api", self.mention("1500.0"))
            self.assertEqual(handle.call_count, 2)
            self.assertEqual(h.bot.bot_state["cursor"], "2000.0")

    def test_catch_up_tick_skips_what_the_socket_handled(self):
        with BotHarness() as h:
            with mock.patch.object(h.bot, "handle_message") as handle:
                h.bot.on_socket_event("events_api", self.mention("2000.0"))
                h.bot.bot_state["cursor"] = "1000.0"            # e.g. reconnect replays
                with mock.patch.object(slack_bot.slack_client, "conversations_history",
                                       return_value=[msg(f"<@{BOT}> status", "2000.0")]):
                    h.bot.tick()
            handle.assert_called_once()

    def test_other_channels_and_event_types_are_ignored(self):
        with BotHarness() as h:
            with mock.patch.object(h.bot, "handle_message") as handle:
                h.bot.on_socket_event("events_api", self.mention("2000.0", channel="C9"))
                h.bot.on_socket_event("events_api", {"event": {"type": "message", "ts": "2001.0"}})
            handle.assert_not_called()

    def test_hello_runs_a_catch_up_tick(self):
        with BotHarness() as h:
            with mock.patch.object(h.bot, "tick") as tick:
                h.bot.on_socket_event("hello", {})
            tick.assert_called_once()


class TestStatusGathering(unittest.TestCase):
    def test_running_reviews_come_from_live_locks(self):
        with BotHarness() as h, tempfile.TemporaryDirectory() as reviews:
            lock = Path(reviews) / ".lock-100"
            lock.write_text("")
            with mock.patch.object(slack_bot.reviewer, "REVIEWS_DIR", Path(reviews)):
                self.assertEqual(h.bot.running_reviews(), [])     # unlocked = not running
                with open(lock, "w") as held:
                    import fcntl
                    fcntl.flock(held, fcntl.LOCK_EX)
                    running = h.bot.running_reviews()
            self.assertEqual(len(running), 1)
            self.assertEqual(running[0]["iid"], "481")
            self.assertEqual(running[0]["project"], "g/app")
            self.assertIn("已跑", running[0]["verdict"])

    @staticmethod
    def review(root, mr_id, project, findings="[]", mtime=None, ctx=None):
        work = Path(root) / str(mr_id)
        work.mkdir(exist_ok=True)
        path = work / "final_findings.json"
        path.write_text('{"mr": {"project": "%s"}, "findings": %s}' % (project, findings))
        (work / "mr_context.json").write_text(json.dumps(
            {"project": project, "iid": str(mr_id), **(ctx or {})}))
        if mtime:
            import os
            os.utime(path, (mtime, mtime))
        return path

    def test_latest_per_project_summarizes_severities(self):
        with BotHarness() as h, tempfile.TemporaryDirectory() as reviews:
            self.review(reviews, 100, "g/app", '[{"severity":"high"},{"severity":"low"}]')
            with mock.patch.object(slack_bot.reviewer, "REVIEWS_DIR", Path(reviews)):
                entries, never = h.bot.latest_per_project()
            self.assertEqual(entries[0]["iid"], "481")
            self.assertEqual(entries[0]["title"], "修發信驗證")
            self.assertIn("🔴1", entries[0]["verdict"])
            self.assertTrue(entries[0]["when"])
            self.assertEqual(never, [])

    def test_clean_reviews_read_as_clean(self):
        with BotHarness() as h, tempfile.TemporaryDirectory() as reviews:
            self.review(reviews, 100, "g/app")
            with mock.patch.object(slack_bot.reviewer, "REVIEWS_DIR", Path(reviews)):
                entries, _ = h.bot.latest_per_project()
            self.assertIn("無發現", entries[0]["verdict"])

    def test_only_the_newest_review_of_each_project_is_kept(self):
        with BotHarness() as h, tempfile.TemporaryDirectory() as reviews:
            self.review(reviews, 100, "g/app", mtime=1_000_000)
            self.review(reviews, 200, "g/app", '[{"severity":"high"}]', mtime=2_000_000)
            with mock.patch.object(slack_bot.reviewer, "REVIEWS_DIR", Path(reviews)):
                entries, _ = h.bot.latest_per_project()
            self.assertEqual(len(entries), 1)
            self.assertIn("🔴1", entries[0]["verdict"])      # the newer one won

    def test_projects_never_reviewed_are_reported_separately(self):
        cfg = config(project_map={"g/app": "/a", "g/quiet": "/q"})
        with BotHarness() as h, tempfile.TemporaryDirectory() as reviews:
            h.bot.config = cfg
            self.review(reviews, 100, "g/app")
            with mock.patch.object(slack_bot.reviewer, "REVIEWS_DIR", Path(reviews)):
                entries, never = h.bot.latest_per_project()
            self.assertEqual([e["project"] for e in entries], ["g/app"])
            self.assertEqual(never, ["g/quiet"])

    def test_reviews_of_unwatched_projects_are_ignored(self):
        # a project dropped from project_map should not linger in the report
        with BotHarness() as h, tempfile.TemporaryDirectory() as reviews:
            self.review(reviews, 300, "g/removed-long-ago")
            with mock.patch.object(slack_bot.reviewer, "REVIEWS_DIR", Path(reviews)):
                entries, never = h.bot.latest_per_project()
            self.assertEqual(entries, [])
            self.assertEqual(never, ["g/app"])

    def test_a_corrupt_findings_file_is_skipped_not_fatal(self):
        with BotHarness() as h, tempfile.TemporaryDirectory() as reviews:
            (Path(reviews) / "999").mkdir()
            (Path(reviews) / "999" / "final_findings.json").write_text("{oops")
            self.review(reviews, 100, "g/app", mtime=1_000_000)
            with mock.patch.object(slack_bot.reviewer, "REVIEWS_DIR", Path(reviews)):
                entries, _ = h.bot.latest_per_project()
            self.assertEqual(len(entries), 1)

    def test_no_reviews_at_all_means_everything_is_unreviewed(self):
        with BotHarness() as h, tempfile.TemporaryDirectory() as reviews:
            with mock.patch.object(slack_bot.reviewer, "REVIEWS_DIR", Path(reviews)):
                entries, never = h.bot.latest_per_project()
            self.assertEqual(entries, [])
            self.assertEqual(never, ["g/app"])

    def test_identity_gaps_are_filled_from_the_reviews_own_context_file(self):
        # author/branches were not in state.mrs when this MR was polled; the
        # review's own context file still knows them
        with BotHarness() as h, tempfile.TemporaryDirectory() as reviews:
            work = Path(reviews) / "100"
            work.mkdir()
            (work / "mr_context.json").write_text(json.dumps({
                "author": "陳小明", "source_branch": "feat/mail",
                "target_branch": "develop", "web_url": "https://gl/mr/481",
                "title": "(舊的標題,不該覆蓋 state 的)"}))
            with mock.patch.object(slack_bot.reviewer, "REVIEWS_DIR", Path(reviews)):
                info = h.bot.review_identity("100")
            self.assertEqual(info["author"], "陳小明")
            self.assertEqual(info["source_branch"], "feat/mail")
            self.assertEqual(info["web_url"], "https://gl/mr/481")
            self.assertEqual(info["title"], "修發信驗證")   # state.mrs wins where it has a value

    def test_identity_of_a_completely_unknown_mr_still_renders(self):
        with BotHarness() as h, tempfile.TemporaryDirectory() as reviews:
            with mock.patch.object(slack_bot.reviewer, "REVIEWS_DIR", Path(reviews)):
                info = h.bot.review_identity("999")
            self.assertEqual(info["iid"], "999")
            self.assertTrue(commands.format_review_entry(info))

    def test_status_reports_a_pause(self):
        with BotHarness() as h:
            h.bot.dispatch(commands.Command("pause", ["2h"]), ADMIN, None)
            h.bot.dispatch(commands.Command("status", []), ADMIN, None)
            self.assertIn("暫停中", h.last)

    def test_status_shows_project_title_author_and_branches(self):
        with BotHarness() as h, tempfile.TemporaryDirectory() as reviews:
            self.review(reviews, 100, "g/app", ctx={
                "author": "陳小明", "source_branch": "feat/mail",
                "target_branch": "develop", "web_url": "https://gl/mr/481"})
            with mock.patch.object(slack_bot.reviewer, "REVIEWS_DIR", Path(reviews)):
                h.bot.dispatch(commands.Command("status", []), ADMIN, None)
            for needle in ("app", "修發信驗證", "陳小明", "feat/mail", "develop",
                           "https://gl/mr/481", "各專案最新"):
                with self.subTest(needle=needle):
                    self.assertIn(needle, h.last)


class TestBotStateIO(unittest.TestCase):
    def test_missing_file_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(slack_bot.load_bot_state(Path(d) / "nope.json"), {})

    def test_corrupt_file_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bot_state.json"
            path.write_text("{oops")
            self.assertEqual(slack_bot.load_bot_state(path), {})

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bot_state.json"
            slack_bot.save_bot_state({"cursor": "1.0"}, path)
            self.assertEqual(slack_bot.load_bot_state(path)["cursor"], "1.0")


if __name__ == "__main__":
    unittest.main()


class TestMergeButtons(unittest.TestCase):
    def press(self, harness, verb, user=ADMIN, rerun_log=None):
        if rerun_log is not None:
            harness.bot.bot_state["rerun_log"] = rerun_log
        gl = slack_bot.gitlab_client
        with mock.patch.object(gl, "get_mr", return_value={"id": 100, "state": "opened"}), \
             mock.patch.object(slack_bot.merge_retry, "spawn_detached") as spawn, \
             mock.patch.object(slack_bot.slack_client, "chat_update") as update, \
             mock.patch.object(slack_bot.slack_client, "post_ephemeral") as whisper:
            harness.bot.on_socket_event("interactive",
                                        click_payload(["g/app!481"], user=user, verb=verb))
        return spawn, update, whisper

    def test_merge_retry_spawns_without_signing_anything_off(self):
        with BotHarness() as h:
            spawn, update, _ = self.press(h, "merge", user=OTHER)
            spawn.assert_called_once_with("g/app", "481", "100", waived_by=None)
            update.assert_called_once()                          # button retired

    def test_merge_retry_runs_no_model_so_spends_no_budget(self):
        now = datetime.now(timezone.utc).isoformat()
        with BotHarness() as h:
            spawn, _, _ = self.press(h, "merge", user=OTHER,
                                     rerun_log=[now] * commands.RERUN_LIMIT_PER_HOUR)
            spawn.assert_called_once()
            self.assertEqual(len(h.bot.bot_state["rerun_log"]), commands.RERUN_LIMIT_PER_HOUR)

    def test_admin_sign_off_carries_who_pressed(self):
        with BotHarness() as h:
            spawn, _, _ = self.press(h, "waive")
            spawn.assert_called_once_with("g/app", "481", "100", waived_by=ADMIN)

    def test_sign_off_is_admin_only(self):
        with BotHarness() as h:
            spawn, update, whisper = self.press(h, "waive", user=OTHER)
            spawn.assert_not_called()
            update.assert_not_called()
            self.assertIn("admin", whisper.call_args.args[3])
