"""Tests for the pure Slack command layer: parsing, permissions, text replies."""
import unittest
from datetime import datetime, timedelta, timezone

import commands
import overrides

BOT = "U0BF20M3SR3"
ADMIN = "U085GM4TUHJ"
OTHER = "U0860MNJK6Y"
SLACK_CFG = {"admin_user_ids": [ADMIN]}


class TestParse(unittest.TestCase):
    def parse(self, text):
        return commands.parse(text, BOT)

    def test_plain_chatter_is_ignored(self):
        self.assertIsNone(self.parse("今天天氣不錯"))

    def test_mention_of_someone_else_is_ignored(self):
        self.assertIsNone(self.parse("<@U9999999> status"))

    def test_bare_mention_asks_for_usage(self):
        self.assertEqual(self.parse(f"<@{BOT}>").verb, "help")
        self.assertEqual(self.parse(f"  <@{BOT}>  ").verb, "help")

    def test_conversational_tag_falls_through_to_usage(self):
        # "謝啦 @bot" should not be scolded as an unknown command
        self.assertEqual(self.parse(f"謝啦 <@{BOT}>").verb, "help")

    def test_legacy_mention_form_with_label(self):
        self.assertEqual(self.parse(f"<@{BOT}|mrnotify> status").verb, "status")

    def test_verb_and_args(self):
        cmd = self.parse(f"<@{BOT}> rerun !481 deep")
        self.assertEqual(cmd.verb, "rerun")
        self.assertEqual(cmd.args, ["!481", "deep"])

    def test_verb_is_case_insensitive(self):
        self.assertEqual(self.parse(f"<@{BOT}> STATUS").verb, "status")

    def test_chinese_aliases(self):
        self.assertEqual(self.parse(f"<@{BOT}> 狀態").verb, "status")
        self.assertEqual(self.parse(f"<@{BOT}> 重跑 !481").verb, "rerun")
        self.assertEqual(self.parse(f"<@{BOT}> 暫停 2h").verb, "pause")

    def test_command_before_the_mention_still_counts(self):
        self.assertEqual(self.parse(f"status <@{BOT}>").verb, "status")

    def test_mention_in_the_middle(self):
        self.assertEqual(self.parse(f"幫個忙 <@{BOT}> status").verb, "status")

    def test_unknown_verb_is_reported_not_swallowed(self):
        cmd = self.parse(f"<@{BOT}> frobnicate now")
        self.assertEqual(cmd.verb, "unknown")
        self.assertEqual(cmd.args, ["frobnicate", "now"])

    def test_fullwidth_whitespace_is_normalized(self):
        self.assertEqual(self.parse(f"<@{BOT}>　status").verb, "status")


class TestAuthorize(unittest.TestCase):
    def deny(self, verb, user, cfg=SLACK_CFG, args=()):
        return commands.authorize(commands.Command(verb, list(args)), user, cfg)

    def test_public_commands_are_open_to_everyone(self):
        self.assertIsNone(self.deny("status", OTHER))
        self.assertIsNone(self.deny("help", OTHER))

    def test_rerun_is_open_to_channel_members(self):
        self.assertIsNone(self.deny("rerun", OTHER, args=["!481"]))

    def test_admin_commands_are_denied_to_others(self):
        for verb in ("set", "reset", "pause", "resume", "automerge", "projects"):
            with self.subTest(verb=verb):
                denial = self.deny(verb, OTHER)
                self.assertIsNotNone(denial)
                self.assertIn("admin", denial.lower())

    def test_admin_commands_allowed_for_admin(self):
        self.assertIsNone(self.deny("set", ADMIN))

    def test_an_unmapped_verb_fails_closed(self):
        self.assertIsNotNone(self.deny("nuke_everything", OTHER))

    def test_fails_closed_when_no_admin_is_configured(self):
        denial = self.deny("set", ADMIN, cfg={})
        self.assertIsNotNone(denial)
        self.assertIn("admin_user_ids", denial)

    def test_mention_user_ids_is_the_admin_fallback(self):
        self.assertIsNone(self.deny("set", ADMIN, cfg={"mention_user_ids": [ADMIN]}))

    def test_explicit_admin_list_wins_over_the_fallback(self):
        cfg = {"admin_user_ids": [ADMIN], "mention_user_ids": [OTHER]}
        self.assertIsNotNone(self.deny("set", OTHER, cfg=cfg))


class TestDuration(unittest.TestCase):
    def test_units(self):
        self.assertEqual(commands.parse_duration("30m"), 1800)
        self.assertEqual(commands.parse_duration("2h"), 7200)
        self.assertEqual(commands.parse_duration("90"), 5400)   # bare number = minutes

    def test_default_when_missing(self):
        self.assertEqual(commands.parse_duration(None), commands.DEFAULT_PAUSE_SECONDS)

    def test_over_the_cap_is_refused(self):
        self.assertIsNone(commands.parse_duration("24h"))

    def test_junk_is_refused(self):
        self.assertIsNone(commands.parse_duration("soon"))


class TestRerunRateLimit(unittest.TestCase):
    NOW = datetime(2026, 7, 30, 9, 0, tzinfo=timezone.utc)

    def log(self, n, minutes_ago):
        return [(self.NOW - timedelta(minutes=minutes_ago)).isoformat() for _ in range(n)]

    def test_under_the_limit_is_allowed(self):
        self.assertTrue(commands.rerun_allowed(self.log(3, 10), self.NOW, limit=6))

    def test_at_the_limit_is_blocked(self):
        self.assertFalse(commands.rerun_allowed(self.log(6, 10), self.NOW, limit=6))

    def test_old_entries_fall_out_of_the_window(self):
        self.assertTrue(commands.rerun_allowed(self.log(6, 120), self.NOW, limit=6))

    def test_admin_bypasses_the_limit(self):
        self.assertTrue(commands.rerun_allowed(self.log(99, 1), self.NOW, limit=6, is_admin=True))

    def test_junk_entries_do_not_crash_the_count(self):
        self.assertTrue(commands.rerun_allowed(["not-a-date"], self.NOW, limit=1))


class TestHelp(unittest.TestCase):
    def test_admin_only_sections_are_hidden_from_others(self):
        member = commands.format_help(is_admin=False)
        self.assertNotIn("projects add", member)
        self.assertNotIn("reset", member)
        self.assertIn("projects add", commands.format_help(is_admin=True))

    def test_everyone_sees_status_and_rerun(self):
        member = commands.format_help(is_admin=False)
        self.assertIn("status", member)
        self.assertIn("rerun", member)

    def test_mentions_the_real_bot_handle(self):
        self.assertIn("@mrnotify", commands.format_help(is_admin=True, handle="mrnotify"))


class TestSettingsRendering(unittest.TestCase):
    CURRENT = {"engine": "claude", "effort": "medium", "language": "zh-TW"}

    def test_every_settable_key_is_listed(self):
        # no nine-option ceiling any more: all of them must show up
        text = commands.format_settings(self.CURRENT)
        for key in overrides.SETTABLE:
            with self.subTest(key=key):
                self.assertIn(f"`{key}`", text)

    def test_current_values_and_allowed_values_are_both_shown(self):
        text = commands.format_settings(self.CURRENT)
        self.assertIn("medium", text)          # current
        self.assertIn("xhigh", text)           # allowed
        self.assertIn("reset", text)

    def test_missing_current_value_renders_as_unknown(self):
        self.assertIn("?", commands.format_settings({}))

    def test_value_help_lists_the_choices_for_an_enum(self):
        text = commands.format_value_help("effort", current="medium")
        self.assertIn("medium", text)
        self.assertIn("high", text)
        self.assertIn("set effort", text)

    def test_value_help_for_a_free_text_key_says_so(self):
        text = commands.format_value_help("model")
        self.assertIn("自由填", text)
        self.assertIn("set model", text)

    def test_value_help_for_an_int_key_shows_the_range(self):
        self.assertIn("500", commands.format_value_help("maxfiles"))

    def test_value_help_rejects_an_unknown_key_and_lists_valid_ones(self):
        text = commands.format_value_help("gitlab_token")
        self.assertIn("不認得", text)
        self.assertIn("effort", text)

    def test_allowed_desc_per_kind(self):
        self.assertIn("codex", commands.allowed_desc("engine"))
        self.assertEqual(commands.allowed_desc("automerge"), "on / off")
        self.assertIn("50", commands.allowed_desc("maxlines"))
        self.assertIn("自由填", commands.allowed_desc("skeptic"))


class TestRerunTargets(unittest.TestCase):
    MRS = [
        {"mr_id": "1", "project": "g/auth-service", "iid": "481", "title": "修發信驗證"},
        {"mr_id": "2", "project": "g/fast-pxy", "iid": "72", "title": "加 retry"},
    ]

    def test_each_line_is_a_runnable_command(self):
        text = commands.format_rerun_targets(self.MRS)
        self.assertIn("`rerun g/auth-service!481`", text)
        self.assertIn("修發信驗證", text)
        self.assertIn("`rerun g/fast-pxy!72`", text)

    def test_no_cap_on_how_many_are_listed(self):
        many = [{"project": "g/p", "iid": str(i), "title": f"t{i}"} for i in range(20)]
        text = commands.format_rerun_targets(many)
        self.assertIn("g/p!19", text)

    def test_empty_list_explains_what_to_do(self):
        text = commands.format_rerun_targets([])
        self.assertIn("rerun", text)


class TestProjectsRendering(unittest.TestCase):
    def test_lists_paths_with_add_and_remove_syntax(self):
        text = commands.format_projects({"g/a": "/local/a"})
        self.assertIn("`g/a`", text)
        self.assertIn("/local/a", text)
        self.assertIn("projects add", text)
        self.assertIn("projects rm", text)

    def test_empty_map_still_renders(self):
        self.assertIn("projects add", commands.format_projects({}))


class TestReviewEntryRendering(unittest.TestCase):
    FULL = {"iid": "2851", "project": "team/backend/billing-api",
            "title": "[ fix ] 修發信驗證流程", "author": "陳小明",
            "source_branch": "feat/mail", "target_branch": "develop",
            "web_url": "https://gl/x/-/merge_requests/2851",
            "verdict": "✅ 無發現", "when": "07-29 16:29"}

    def test_everything_the_user_asked_for_is_present(self):
        text = "\n".join(commands.format_review_entry(self.FULL))
        for needle in ("2851", "billing-api", "修發信驗證流程", "陳小明",
                       "feat/mail", "develop", "無發現", "07-29 16:29"):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)

    def test_the_mr_number_links_to_gitlab(self):
        head = commands.format_review_entry(self.FULL)[0]
        self.assertIn("<https://gl/x/-/merge_requests/2851|!2851>", head)

    def test_project_is_shortened_to_the_repo_name(self):
        head = commands.format_review_entry(self.FULL)[0]
        self.assertIn("billing-api", head)
        self.assertNotIn("py_backend", head)

    def test_without_a_url_it_still_shows_the_number(self):
        head = commands.format_review_entry({"iid": "7"})[0]
        self.assertIn("!7", head)
        self.assertNotIn("<", head)

    def test_a_project_with_no_review_shows_no_mr_number(self):
        head = commands.format_review_entry({"project": "g/never"})[0]
        self.assertIn("never", head)
        self.assertNotIn("!", head)

    def test_long_titles_are_truncated(self):
        entry = dict(self.FULL, title="標" * 200)
        head = commands.format_review_entry(entry)[0]
        self.assertLess(len(head), 200)
        self.assertIn("…", head)

    def test_missing_fields_are_simply_omitted(self):
        lines = commands.format_review_entry({"iid": "7", "project": "g/p"})
        self.assertEqual(len(lines), 1)          # nothing for the detail line to say
        self.assertIn("!7", lines[0])

    def test_a_missing_target_branch_is_marked_rather_than_dropped(self):
        detail = commands.format_review_entry({"iid": "7", "source_branch": "f"})[1]
        self.assertIn("`f` → `?`", detail)


class TestStatusRendering(unittest.TestCase):
    def test_renders_the_facts_it_is_given(self):
        text = commands.format_status({
            "last_poll": "2026-07-30T09:19:02+00:00",
            "poll_errors": 0,
            "opened_count": 4,
            "running": [{"iid": "481", "project": "g/auth-service", "title": "修發信",
                         "verdict": "已跑 3m12s"}],
            "per_project": [{"iid": "478", "project": "g/auth-service", "author": "Ann",
                             "source_branch": "f", "target_branch": "develop",
                             "verdict": "✅ 無發現", "when": "07-29 16:29"}],
            "never_reviewed": ["g/sub/quiet-app"],
            "settings": {"engine": "claude", "language": "zh-TW", "effort": "medium",
                         "model": "claude-opus-4-8", "automerge": False},
            "changed_keys": ["effort"],
            "paused_until": None,
            "projects": 6,
            "groups": 2,
        })
        for needle in ("!481", "!478", "claude", "zh-TW", "effort", "6",
                       "各專案最新", "quiet-app"):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)

    def test_never_reviewed_projects_are_named_on_one_line(self):
        text = commands.format_status({"never_reviewed": ["g/a", "g/b", "g/c"]})
        self.assertIn("還沒有 review 紀錄", text)
        for name in ("`a`", "`b`", "`c`"):
            self.assertIn(name, text)

    def test_poll_errors_are_flagged(self):
        text = commands.format_status({"last_poll": "2026-07-30T09:19:02+00:00",
                                       "poll_errors": 2})
        self.assertIn("2", text)
        self.assertIn("warning", text)

    def test_shows_pause_prominently(self):
        text = commands.format_status({"paused_until": "2026-07-30T11:00:00+00:00",
                                       "pending_while_paused": 3})
        self.assertIn("暫停", text)
        self.assertIn("3", text)

    def test_survives_missing_facts(self):
        self.assertTrue(commands.format_status({}))


class TestTargetParsing(unittest.TestCase):
    def test_project_qualified_target(self):
        self.assertEqual(commands.parse_target("team/backend/auth-service!481"),
                         ("team/backend/auth-service", "481"))

    def test_bare_iid(self):
        self.assertEqual(commands.parse_target("!481"), (None, "481"))
        self.assertEqual(commands.parse_target("481"), (None, "481"))

    def test_junk(self):
        self.assertEqual(commands.parse_target("deep"), (None, None))
        self.assertEqual(commands.parse_target(""), (None, None))
        self.assertEqual(commands.parse_target("g/p!abc"), (None, None))


if __name__ == "__main__":
    unittest.main()
