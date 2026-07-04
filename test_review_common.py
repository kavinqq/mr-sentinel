"""Unit tests for review_common pure functions."""
import unittest

import review_common as rc

CFG = {
    "project_map": {
        "group/backend-app": "/home/me/backend-app",
        "group/frontend-app": "/home/me/frontend-app",
    },
    "max_changed_files": 60,
    "max_diff_lines": 3000,
}


class TestTargeting(unittest.TestCase):
    def test_is_target_true(self):
        self.assertTrue(rc.is_review_target("group/backend-app", CFG))

    def test_is_target_false(self):
        self.assertFalse(rc.is_review_target("group/other-app", CFG))

    def test_resolve_path(self):
        self.assertEqual(
            rc.resolve_local_path("group/frontend-app", CFG), "/home/me/frontend-app"
        )

    def test_resolve_path_missing(self):
        self.assertIsNone(rc.resolve_local_path("group/other-app", CFG))


class TestNoiseAndSize(unittest.TestCase):
    def test_is_noise_lockfile(self):
        self.assertTrue(rc.is_noise_path("frontend/package-lock.json"))
        self.assertTrue(rc.is_noise_path("poetry.lock"))

    def test_is_noise_dir(self):
        self.assertTrue(rc.is_noise_path("app/node_modules/x/y.js"))
        self.assertTrue(rc.is_noise_path("static/dist/main.js"))

    def test_is_noise_minified_and_maps(self):
        self.assertTrue(rc.is_noise_path("assets/app.min.js"))
        self.assertTrue(rc.is_noise_path("assets/app.js.map"))

    def test_is_noise_normal_false(self):
        self.assertFalse(rc.is_noise_path("backend/home/views.py"))

    def test_filter_drops_noise(self):
        changes = [
            {"new_path": "backend/home/views.py", "diff": "+a\n-b\n"},
            {"new_path": "package-lock.json", "diff": "+x\n"},
        ]
        out = rc.filter_noise_changes(changes)
        self.assertEqual([c["new_path"] for c in out], ["backend/home/views.py"])

    def test_diff_stats_counts_plus_minus_only(self):
        changes = [
            {"new_path": "a.py", "diff": "@@ -1 +1,2 @@\n+added\n-removed\n context\n+++ b/a.py\n"}
        ]
        files, lines = rc.diff_stats(changes)
        self.assertEqual(files, 1)
        self.assertEqual(lines, 2)  # +added / -removed only; headers and context excluded

    def test_size_guard_files(self):
        skip, reason = rc.should_skip_for_size(61, 10, CFG)
        self.assertTrue(skip)
        self.assertIn("61", reason)

    def test_size_guard_lines(self):
        skip, reason = rc.should_skip_for_size(1, 3001, CFG)
        self.assertTrue(skip)
        self.assertIn("3001", reason)

    def test_size_guard_ok(self):
        skip, reason = rc.should_skip_for_size(3, 100, CFG)
        self.assertFalse(skip)
        self.assertEqual(reason, "")


DIFF_REFS = {"base_sha": "b", "start_sha": "s", "head_sha": "h"}


class TestFindingFormat(unittest.TestCase):
    def test_sort_high_first(self):
        fs = [{"severity": "low"}, {"severity": "high"}, {"severity": "medium"}]
        self.assertEqual([f["severity"] for f in rc.sort_findings(fs)], ["high", "medium", "low"])

    def test_sort_stable_unknown_last(self):
        fs = [{"severity": "weird"}, {"severity": "high"}]
        self.assertEqual([f["severity"] for f in rc.sort_findings(fs)], ["high", "weird"])

    def test_format_body_emoji_label_and_signature(self):
        body = rc.format_comment_body(
            {"severity": "high", "title": "SQL injection", "file": "a.py", "line": 10,
             "body": "User input is concatenated into the query.\nFix: use parameterized queries."},
            signature="🤖 mr-sentinel (scanned by opus, vetted by sonnet)")
        self.assertIn("🔴", body)
        self.assertIn("[High]", body)
        self.assertIn("SQL injection", body)
        self.assertIn("parameterized", body)
        self.assertIn("mr-sentinel", body)

    def test_format_body_default_signature(self):
        body = rc.format_comment_body(
            {"severity": "low", "title": "t", "file": "a.py", "line": 1, "body": "b"})
        self.assertIn("🟡", body)
        self.assertIn("mr-sentinel", body)


class TestPositionEmojiTs(unittest.TestCase):
    def test_build_position_inline(self):
        pos = rc.build_position({"file": "a.py", "line": 12}, DIFF_REFS)
        self.assertEqual(pos["new_path"], "a.py")
        # GitLab requires old_path too for position_type=text; omitting it 400s
        # every inline discussion and silently degrades to plain notes
        self.assertEqual(pos["old_path"], "a.py")
        self.assertEqual(pos["new_line"], 12)
        self.assertEqual(pos["position_type"], "text")
        self.assertEqual(pos["head_sha"], "h")

    def test_build_position_uses_explicit_old_path_for_renames(self):
        pos = rc.build_position({"file": "new.py", "old_path": "old.py", "line": 5}, DIFF_REFS)
        self.assertEqual(pos["new_path"], "new.py")
        self.assertEqual(pos["old_path"], "old.py")

    def test_build_position_none_when_no_line(self):
        self.assertIsNone(rc.build_position({"file": "a.py", "line": None}, DIFF_REFS))

    def test_position_form_flattens(self):
        form = rc.position_form({"new_path": "a.py", "new_line": 12, "position_type": "text"})
        self.assertEqual(form["position[new_path]"], "a.py")
        self.assertEqual(form["position[new_line]"], 12)

    def test_has_own_emoji_true(self):
        emojis = [{"name": "thumbsup", "user": {"id": 5}}, {"name": "eyes", "user": {"id": 9}}]
        self.assertTrue(rc.has_own_award_emoji(emojis, 9))

    def test_has_own_emoji_false_other_user(self):
        emojis = [{"name": "eyes", "user": {"id": 5}}]
        self.assertFalse(rc.has_own_award_emoji(emojis, 9))

    def test_slack_ts_for(self):
        state = {"slack_ts": {"2451": "1700000000.001"}}
        self.assertEqual(rc.slack_ts_for(state, 2451), "1700000000.001")
        self.assertIsNone(rc.slack_ts_for(state, 9999))


if __name__ == "__main__":
    unittest.main()
