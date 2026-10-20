"""Tests for the deterministic test-artifact gate."""
import subprocess
import tempfile
import unittest
from pathlib import Path

import artifact_gate


def change(path, diff):
    return {"new_path": path, "old_path": path, "diff": diff}


BUTTON_DIFF = (
    "@@ -388,0 +389,7 @@ export const TradeActivation\n"
    "+\t\t\t\t\t<button\n"
    "+\t\t\t\t\t\ttype=\"button\"\n"
    "+\t\t\t\t\t\tonClick={postCloseBrowserToApp}\n"
    "+\t\t\t\t\t>\n"
    "+\t\t\t\t\t\tApp測試用按鈕\n"
    "+\t\t\t\t\t</button>\n"
    "+\n"
)


class TestAddedLines(unittest.TestCase):
    def test_numbers_follow_the_new_side_of_each_hunk(self):
        diff = ("@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"
                "@@ -10,0 +11,2 @@\n+x\n+y\n")
        self.assertEqual(list(artifact_gate.added_lines(diff)), [(2, "B"), (11, "x"), (12, "y")])

    def test_file_headers_are_not_added_lines(self):
        diff = "--- a/f\n+++ b/f\n@@ -0,0 +1 @@\n+only\n"
        self.assertEqual(list(artifact_gate.added_lines(diff)), [(1, "only")])

    def test_added_line_starting_with_plus_plus_is_content(self):
        diff = "--- a/f\n+++ b/f\n@@ -0,0 +1 @@\n+++i; // TEST ONLY\n"
        self.assertEqual(list(artifact_gate.added_lines(diff)), [(1, "++i; // TEST ONLY")])

    def test_no_newline_marker_is_ignored(self):
        diff = "@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b\n"
        self.assertEqual(list(artifact_gate.added_lines(diff)), [(1, "b")])


class TestMarkers(unittest.TestCase):
    def hit(self, line):
        return artifact_gate.marker_in(line)

    def test_flags_test_only_ui_and_notes(self):
        for line in ("App測試用按鈕", "// 測試專用帳號", "僅供測試", "暫時測試 先寫死",
                     "# 上線前要移除", "// TEST ONLY", "debug_only = True",
                     "// remove before release", "DO NOT MERGE", "for testing only"):
            with self.subTest(line=line):
                self.assertIsNotNone(self.hit(line))

    def test_helpers_for_automated_tests_are_not_artifacts(self):
        for line in ("help_text='通知類型(方便單元測試用)'", "重置 singleton（整合測試用）",
                     "# used by unit tests", "def test_button():", "測試結果"):
            with self.subTest(line=line):
                self.assertIsNone(self.hit(line))


class TestTestPaths(unittest.TestCase):
    def test_test_and_mock_files_are_exempt(self):
        for path in ("tests/test_x.py", "app/test_views.py", "src/a.test.tsx", "src/a.spec.ts",
                     "src/__tests__/a.tsx", "src/pages/__mock__/x.mock.js", "e2e/login.ts",
                     "conftest.py", "playwright.config.ts", "src/Button.stories.tsx"):
            with self.subTest(path=path):
                self.assertTrue(artifact_gate.is_test_path(path))

    def test_product_files_are_not_exempt(self):
        for path in ("frontend/src/pages/Sign/TradeActivation.tsx", "backend/edda/views.py",
                     "src/contest/latest.ts"):
            with self.subTest(path=path):
                self.assertFalse(artifact_gate.is_test_path(path))


class TestScan(unittest.TestCase):
    def test_the_app_test_button_is_a_high_finding_on_its_line(self):
        [f] = artifact_gate.scan([change("src/TradeActivation.tsx", BUTTON_DIFF)], "zh-TW")
        self.assertEqual((f["severity"], f["file"], f["line"]), ("high", "src/TradeActivation.tsx", 393))
        self.assertEqual(f["gate"], artifact_gate.GATE_NAME)
        self.assertIn("測試用", f["problem"])

    def test_one_finding_per_file_listing_every_hit(self):
        diff = "@@ -0,0 +1,3 @@\n+// TEST ONLY\n+ok\n+// 測試用\n"
        [f] = artifact_gate.scan([change("a.py", diff)], "zh-TW")
        self.assertEqual(f["line"], 1)
        self.assertIn("a.py:1", f["evidence"])
        self.assertIn("a.py:3", f["evidence"])

    def test_only_verified_test_files_and_unshipped_files_are_skipped(self):
        changes = [change("tests/test_a.py", "@@ -0,0 +1 @@\n+# 測試用\n"),
                   change("app.js.map", "@@ -0,0 +1 @@\n+測試用\n"),
                   change("yarn.lock", "@@ -0,0 +1 @@\n+測試用\n")]
        self.assertEqual(artifact_gate.scan(changes, "zh-TW", exempt={"tests/test_a.py"}), [])

    def test_shipped_build_output_is_scanned(self):
        for path in ("dist/app.min.js", "build/main.js", "vendor/x.php", "icon.svg"):
            with self.subTest(path=path):
                changes = [change(path, "@@ -0,0 +1 @@\n+測試用\n")]
                self.assertEqual(len(artifact_gate.scan(changes, "zh-TW")), 1)

    def test_a_test_looking_path_is_scanned_unless_verified(self):
        changes = [change("src/mocks/AppTestEntry.tsx", "@@ -0,0 +1 @@\n+App測試用按鈕\n")]
        self.assertEqual(len(artifact_gate.scan(changes, "zh-TW")), 1)

    def test_removed_lines_never_trigger(self):
        diff = "@@ -5 +4,0 @@\n-App測試用按鈕\n"
        self.assertEqual(artifact_gate.scan([change("a.tsx", diff)], "zh-TW"), [])

    def test_english_text_when_configured(self):
        [f] = artifact_gate.scan([change("a.py", "@@ -0,0 +1 @@\n+# TEST ONLY\n")], "en")
        self.assertIn("test", f["title"].lower())


class TestReferenceTokens(unittest.TestCase):
    def test_tokens_name_the_file_the_way_an_import_would(self):
        self.assertEqual(artifact_gate.reference_tokens("src/Button.test.tsx"), ["Button.test"])
        self.assertEqual(artifact_gate.reference_tokens("app/tests/test_views.py"),
                         ["tests/test_views", "tests.test_views"])
        self.assertEqual(artifact_gate.reference_tokens("src/mocks/browser.ts"),
                         ["mocks/browser", "mocks.browser"])

    def test_directory_entry_files_can_never_be_exempt(self):
        for path in ("src/mocks/index.ts", "tests/__init__.py", "e2e/main.ts"):
            with self.subTest(path=path):
                self.assertIsNone(artifact_gate.reference_tokens(path))


class TestExemptTestFiles(unittest.TestCase):
    def repo(self, files):
        d = tempfile.TemporaryDirectory()
        git = ["git", "-C", d.name, "-c", "user.name=t", "-c", "user.email=t@t",
               "-c", "core.hooksPath=/dev/null"]
        subprocess.run(["git", "init", "-q", d.name], check=True)
        for path, text in files.items():
            (Path(d.name) / path).parent.mkdir(parents=True, exist_ok=True)
            (Path(d.name) / path).write_text(text)
        subprocess.run(git + ["add", "."], check=True)
        subprocess.run(git + ["commit", "-qm", "c"], check=True)
        head = subprocess.run(["git", "-C", d.name, "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
        self.addCleanup(d.cleanup)
        return d.name, head

    def test_test_file_referenced_only_by_tests_is_exempt(self):
        local, head = self.repo({"src/Button.test.tsx": "x", "src/Button.tsx": "y",
                                 "src/other.test.tsx": "import './Button.test'"})
        self.assertEqual(artifact_gate.exempt_test_files(local, head, ["src/Button.test.tsx"]),
                         {"src/Button.test.tsx"})

    def test_mock_imported_by_production_is_not_exempt(self):
        local, head = self.repo({"src/mocks/browser.ts": "worker", "src/main.tsx":
                                 "import { worker } from './mocks/browser'"})
        self.assertEqual(artifact_gate.exempt_test_files(local, head, ["src/mocks/browser.ts"]),
                         set())

    def test_product_paths_are_never_exempt(self):
        local, head = self.repo({"src/a.tsx": "x"})
        self.assertEqual(artifact_gate.exempt_test_files(local, head, ["src/a.tsx"]), set())

    def test_without_a_clone_nothing_is_exempt(self):
        self.assertEqual(artifact_gate.exempt_test_files(None, "h", ["tests/test_a.py"]), set())


class TestCollect(unittest.TestCase):
    def test_api_diff_used_when_git_is_unavailable(self):
        changes = [change("a.py", "@@ -0,0 +1 @@\n+x\n")]
        self.assertEqual(artifact_gate.collect(None, {}, "master", changes), changes)

    def test_unknown_when_the_api_diff_is_empty(self):
        # GitLab returns empty diffs for large MRs: "nothing found" must not read as clean
        self.assertIsNone(artifact_gate.collect(None, {}, "master", [change("a.py", "")]))
        self.assertIsNone(artifact_gate.collect(None, {}, "master", []))

    def test_git_diff_from_a_real_clone(self):
        with tempfile.TemporaryDirectory() as d:
            git = ["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@t",
                   "-c", "core.hooksPath=/dev/null"]
            subprocess.run(["git", "init", "-q", d], check=True)
            (Path(d) / "a.tsx").write_text("one\n")
            subprocess.run(git + ["add", "."], check=True)
            subprocess.run(git + ["commit", "-qm", "base"], check=True)
            base = subprocess.run(["git", "-C", d, "rev-parse", "HEAD"], capture_output=True,
                                  text=True, check=True).stdout.strip()
            (Path(d) / "a.tsx").write_text("one\nApp測試用按鈕\n")
            (Path(d) / "中文.py").write_text("# TEST ONLY\n")
            subprocess.run(git + ["add", "."], check=True)
            subprocess.run(git + ["commit", "-qm", "head"], check=True)
            head = subprocess.run(["git", "-C", d, "rev-parse", "HEAD"], capture_output=True,
                                  text=True, check=True).stdout.strip()
            changes = artifact_gate.collect(d, {"start_sha": base, "head_sha": head}, "master", None)
        found = {(f["file"], f["line"]) for f in artifact_gate.scan(changes, "zh-TW")}
        self.assertEqual(found, {("a.tsx", 2), ("中文.py", 1)})


if __name__ == "__main__":
    unittest.main()
