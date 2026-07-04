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


if __name__ == "__main__":
    unittest.main()
