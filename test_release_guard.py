"""Tests for the release-guard pass plumbing (the model call itself is in engines/)."""
import unittest

import release_guard


def change(path, diff):
    return {"new_path": path, "old_path": path, "diff": diff}


def hunks(n, lines_each=1, start=1):
    out = ""
    for i in range(n):
        first = start + i * 10
        out += f"@@ -0,0 +{first},{lines_each} @@\n" + "".join(f"+l{i}_{j}\n" for j in range(lines_each))
    return out


GUARD_CFG = {"test_hosts": ["labpocket.tw", "172.21.2."], "environment_notes": "UAT = labpocket"}
CTX = {"project": "g/p", "iid": 5, "title": "t", "description": "d",
       "target_branch": "master", "diff_refs": {"head_sha": "h"}}


class TestCandidates(unittest.TestCase):
    def test_configured_test_host_is_a_candidate(self):
        diff = "@@ -0,0 +255,2 @@\n+ok = 1\n+url = 'https://www.labpocket.tw/school/report/1/'\n"
        [c] = release_guard.candidates([change("backend/school_utils.py", diff)], ["labpocket.tw"])
        self.assertEqual((c["file"], c["line"]), ("backend/school_utils.py", 256))
        self.assertIn("labpocket.tw", c["why"])

    def test_generic_test_environment_signals(self):
        for line in ("BASE = 'http://localhost:8000'", "api: 'https://uat-api.example.com'",
                     "DEBUG = True", "worker.start() // msw", "SKIP_CAPTCHA = 1",
                     "const testAccount = 'A123456789'", "if (true) {", "staging-pxy.internal",
                     "host = 'qa.example.com'", "env: 'preprod'"):
            with self.subTest(line=line):
                diff = f"@@ -0,0 +1 @@\n+{line}\n"
                self.assertEqual(len(release_guard.candidates([change("a.ts", diff)], [])), 1)

    def test_other_strange_lines_are_candidates(self):
        for line in ("console.log(user)", "print(token)", "var_dump($order);",
                     "# TODO 上線前改回來", "// FIXME", "# HACK around the api",
                     "password = 'abc123'", "API_KEY = 'sk-1'", "// 臨時先這樣",
                     "# 之後再刪", "# def old_check(user):"):
            with self.subTest(line=line):
                diff = f"@@ -0,0 +1 @@\n+{line}\n"
                self.assertEqual(len(release_guard.candidates([change("a.py", diff)], [])), 1)

    def test_plain_code_is_not_a_candidate(self):
        diff = "@@ -0,0 +1,2 @@\n+total = price * qty\n+return render(request)\n"
        self.assertEqual(release_guard.candidates([change("a.py", diff)], []), [])

    def test_test_folders_are_still_in_scope_and_flagged(self):
        # a folder name proves nothing: production code can import src/mocks/x.ts
        diff = "@@ -0,0 +1 @@\n+BASE = 'http://localhost'\n"
        [c] = release_guard.candidates([change("src/mocks/a.ts", diff)], [])
        self.assertTrue(c["test_path"])


class TestBatches(unittest.TestCase):
    def test_every_added_line_is_sent_somewhere(self):
        changes = [change(f"f{i}.py", hunks(3, lines_each=10)) for i in range(5)]
        batches = release_guard.build_batches(CTX, changes, GUARD_CFG, max_lines=40, max_batches=20)
        sent = [(c["new_path"], n) for b in batches for c in b["changes"]
                for n, _ in release_guard.added_lines(c["diff"])]
        expected = [(c["new_path"], n) for c in changes for n, _ in release_guard.added_lines(c["diff"])]
        self.assertEqual(sorted(sent), sorted(expected))
        self.assertTrue(all(b["batch"]["total"] == len(batches) for b in batches))

    def test_a_file_larger_than_the_budget_is_split_at_hunks(self):
        batches = release_guard.build_batches(CTX, [change("big.py", hunks(6, lines_each=10))],
                                              GUARD_CFG, max_lines=25, max_batches=20)
        self.assertEqual(len(batches), 3)
        self.assertTrue(all(b["changes"][0]["new_path"] == "big.py" for b in batches))

    def test_a_hunk_larger_than_the_budget_is_split_keeping_line_numbers(self):
        diff = "@@ -5,2 +5,60 @@\n-old\n x\n" + "".join(f"+l{j}\n" for j in range(59))
        batches = release_guard.build_batches(CTX, [change("big.py", diff)], GUARD_CFG,
                                              max_lines=25, max_batches=20)
        self.assertEqual(len(batches), 3)
        sent = [n for b in batches for c in b["changes"] for n, _ in release_guard.added_lines(c["diff"])]
        self.assertEqual(sent, [n for n, _ in release_guard.added_lines(diff)])
        self.assertTrue(all(release_guard._added_count(c["diff"]) <= 25
                            for b in batches for c in b["changes"]))

    def test_split_ignores_no_newline_markers_and_removed_lines(self):
        diff = ("@@ -1,3 +1,4 @@\n-a\n-b\n" + "".join(f"+l{j}\n" for j in range(4))
                + "\\ No newline at end of file\n")
        pieces = release_guard._split_hunk(diff, 3)
        self.assertEqual([n for p in pieces for n, _ in release_guard.added_lines(p)], [1, 2, 3, 4])

    def test_a_removal_only_hunk_is_not_split(self):
        diff = "@@ -1,50 +0,0 @@\n" + "".join(f"-l{j}\n" for j in range(50))
        self.assertEqual(release_guard._split_hunk(diff, 3), [diff])

    def test_a_huge_hunk_still_fails_closed_past_max_batches(self):
        diff = "@@ -0,0 +1,100 @@\n" + "".join(f"+l{j}\n" for j in range(100))
        self.assertIsNone(release_guard.build_batches(CTX, [change("big.py", diff)], GUARD_CFG,
                                                      max_lines=10, max_batches=3))

    def test_context_carries_hosts_notes_and_batch_candidates(self):
        diff = "@@ -0,0 +1,2 @@\n+a\n+url='http://172.21.2.68/'\n"
        [b] = release_guard.build_batches(CTX, [change("a.py", diff)], GUARD_CFG,
                                          max_lines=100, max_batches=5)
        self.assertEqual(b["test_hosts"], ["labpocket.tw", "172.21.2."])
        self.assertEqual(b["environment_notes"], "UAT = labpocket")
        self.assertEqual([c["line"] for c in b["candidates"]], [2])
        self.assertEqual(b["files"], ["a.py"])

    def test_too_large_to_cover_fails_closed(self):
        changes = [change(f"f{i}.py", hunks(1, lines_each=10)) for i in range(10)]
        self.assertIsNone(release_guard.build_batches(CTX, changes, GUARD_CFG,
                                                      max_lines=10, max_batches=3))

    def test_nothing_added_is_one_empty_batch(self):
        [b] = release_guard.build_batches(CTX, [change("a.py", "@@ -1 +0,0 @@\n-x\n")], GUARD_CFG,
                                          max_lines=10, max_batches=3)
        self.assertEqual(b["files"], ["a.py"])


FINDING = {"severity": "high", "category": "security", "title": "t", "file": "a.py", "line": 3,
           "problem": "p", "impact": "i", "fix": "f", "evidence": "- e", "confidence": "sure"}
UNSURE = {**FINDING, "confidence": "unsure", "human_check": "確認正式機的 `API_HOST`"}


class TestNormalize(unittest.TestCase):
    def ok(self, **over):
        return {"checked_files": ["a.py", "b.py"], "findings": [FINDING], **over}

    def test_valid_output_is_marked(self):
        [f] = release_guard.normalize(self.ok(), ["a.py", "b.py"])
        self.assertEqual(f["gate"], release_guard.GATE_NAME)

    def test_clean_output_with_full_coverage_passes(self):
        self.assertEqual(release_guard.normalize(self.ok(findings=[]), ["a.py", "b.py"]), [])

    def test_any_malformed_finding_fails_the_whole_guard(self):
        for bad in ({}, "unable to assess", None, {**FINDING, "severity": "low"},
                    {**FINDING, "file": "elsewhere.py"}, {**FINDING, "line": "3"},
                    {**FINDING, "title": ""}, {**FINDING, "problem": None},
                    {**FINDING, "category": "style"},
                    {k: v for k, v in FINDING.items() if k != "confidence"},
                    {**FINDING, "confidence": "maybe"},
                    {**UNSURE, "human_check": ""},
                    {k: v for k, v in UNSURE.items() if k != "human_check"}):
            with self.subTest(bad=bad):
                self.assertIsNone(release_guard.normalize(self.ok(findings=[FINDING, bad]),
                                                          ["a.py", "b.py"]))

    def test_finding_about_no_line_of_code_becomes_an_mr_note(self):
        release = {**UNSURE, "file": None, "line": None, "title": "前端要同批上線"}
        [f] = release_guard.normalize(self.ok(findings=[release]), ["a.py", "b.py"])
        self.assertIsNone(f["file"])

    def test_a_line_without_a_file_is_malformed(self):
        self.assertIsNone(release_guard.normalize(
            self.ok(findings=[{**FINDING, "file": None, "line": 3}]), ["a.py", "b.py"]))

    def test_unsure_finding_carries_what_a_human_must_check(self):
        [f] = release_guard.normalize(self.ok(findings=[UNSURE]), ["a.py", "b.py"], "zh-TW")
        self.assertEqual(f["confidence"], "unsure")
        self.assertIn("需要人工確認", f["evidence"])
        self.assertIn("`API_HOST`", f["evidence"])

    def test_unchecked_file_fails(self):
        self.assertIsNone(release_guard.normalize(self.ok(checked_files=["a.py"]), ["a.py", "b.py"]))

    def test_unhashable_checked_files_fail_instead_of_raising(self):
        self.assertIsNone(release_guard.normalize(self.ok(checked_files=[["a.py"], "b.py"]),
                                                  ["a.py", "b.py"]))

    def test_missing_lists_fail(self):
        self.assertIsNone(release_guard.normalize({}, ["a.py"]))
        self.assertIsNone(release_guard.normalize({"findings": [], "checked_files": "a.py"}, ["a.py"]))
        self.assertIsNone(release_guard.normalize("x", ["a.py"]))


class TestConfigProblem(unittest.TestCase):
    def test_test_hosts_are_mandatory(self):
        self.assertIsNotNone(release_guard.config_problem({}))
        self.assertIsNotNone(release_guard.config_problem({"test_hosts": ["  "]}))
        self.assertIsNone(release_guard.config_problem(GUARD_CFG))

    def test_batch_limits_must_be_positive_integers(self):
        for bad in ({"max_diff_lines": "3000"}, {"max_diff_lines": 0}, {"max_batches": None},
                    {"max_batches": True}, {"max_diff_lines": 1.5}):
            with self.subTest(bad=bad):
                self.assertIsNotNone(release_guard.config_problem({**GUARD_CFG, **bad}))
        self.assertIsNone(release_guard.config_problem({**GUARD_CFG, "max_diff_lines": 3000,
                                                        "max_batches": 15}))


class TestShippedArtifacts(unittest.TestCase):
    def test_build_output_and_vendored_code_go_to_the_model(self):
        changes = [change(p, "@@ -0,0 +1 @@\n+x\n")
                   for p in ("dist/app.js", "build/main.js", "vendor/lib.php", "icon.svg")]
        [b] = release_guard.build_batches(CTX, changes, GUARD_CFG, max_lines=100, max_batches=5)
        self.assertEqual(b["files"], sorted(c["new_path"] for c in changes))

    def test_unshipped_and_minified_files_are_not_sent(self):
        changes = [change(p, "@@ -0,0 +1 @@\n+x\n")
                   for p in ("package-lock.json", "yarn.lock", "app.js.map", "a.png", "app.min.js")]
        [b] = release_guard.build_batches(CTX, changes, GUARD_CFG, max_lines=100, max_batches=5)
        self.assertEqual(b["files"], [])

    def test_test_host_in_a_minified_bundle_is_a_rule_finding(self):
        diff = "@@ -0,0 +1 @@\n+var a=1;fetch('https://www.labpocket.tw/api')\n"
        [f] = release_guard.minified_host_findings([change("static/app.min.js", diff)],
                                                   ["labpocket.tw"], "zh-TW")
        self.assertEqual((f["file"], f["line"], f["confidence"]), ("static/app.min.js", 1, "sure"))
        self.assertEqual(f["hit_lines"], [1])
        self.assertIn("labpocket.tw", f["problem"])

    def test_readable_files_are_left_to_the_model(self):
        diff = "@@ -0,0 +1 @@\n+url = 'https://www.labpocket.tw/api'\n"
        self.assertEqual(release_guard.minified_host_findings([change("a.js", diff)],
                                                              ["labpocket.tw"], "zh-TW"), [])


if __name__ == "__main__":
    unittest.main()
