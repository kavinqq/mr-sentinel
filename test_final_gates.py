"""Tests for the final gates (marker rule + release guard) as one fail-closed verdict."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import final_gates

CTX = {"project": "g/p", "iid": 5, "title": "t", "description": "", "target_branch": "master",
       "diff_refs": {"start_sha": "s", "head_sha": "h"}, "changes": []}
CFG = {"language": "zh-TW", "release_guard": {"test_hosts": ["labpocket.tw"]}}
MARKER = {"new_path": "a.tsx", "diff": "@@ -0,0 +3 @@\n+App測試用按鈕\n"}
PLAIN = {"new_path": "b.py", "diff": "@@ -0,0 +1 @@\n+x = 1\n"}
LEAK = {"severity": "high", "category": "correctness", "title": "UAT host", "file": "b.py",
        "line": 1, "problem": "p", "impact": "i", "fix": "f", "evidence": "- e",
        "confidence": "sure"}


class FakeEngine:
    """Answers each guard batch from `answers` (dict, raw text, or rc int)."""

    def __init__(self, answer):
        self.answer = answer
        self.batches = []

    def run_guard(self, work, ctx_path, out_path, wt, review_cfg):
        batch = json.loads(ctx_path.read_text())
        self.batches.append(batch)
        answer = self.answer(batch) if callable(self.answer) else self.answer
        if isinstance(answer, int):
            return answer
        out_path.write_text(answer if isinstance(answer, str) else json.dumps(answer))
        return 0


def clean(batch):
    return {"checked_files": batch["files"], "findings": []}


class TestRun(unittest.TestCase):
    def run_gates(self, changes, answer, cfg=CFG):
        engine = FakeEngine(answer)
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(final_gates.artifact_gate, "collect", return_value=changes), \
             mock.patch.object(final_gates.artifact_gate, "exempt_test_files", return_value=set()):
            out = final_gates.run(engine, Path(d), CTX, "/clone", Path(d) / "wt", cfg)
        return out, engine

    def test_clean_change_passes(self):
        out, _ = self.run_gates([PLAIN], clean)
        self.assertTrue(out.passed)
        self.assertEqual(out.record("h", "master"),
                         {"head_sha": "h", "target_branch": "master", "ok": True, "leaks": 0})

    def test_rule_hit_fails_even_when_the_guard_is_clean(self):
        out, _ = self.run_gates([MARKER, PLAIN], clean)
        self.assertFalse(out.passed)
        self.assertEqual([f["gate"] for f in out.findings], ["artifact"])

    def test_guard_leak_fails(self):
        out, _ = self.run_gates([PLAIN], lambda b: {"checked_files": b["files"], "findings": [LEAK]})
        self.assertFalse(out.passed)
        self.assertEqual(out.record("h", "master")["leaks"], 1)

    def test_guard_duplicate_of_a_rule_line_is_dropped(self):
        dup = {**LEAK, "file": "a.tsx", "line": 3}
        out, _ = self.run_gates([MARKER], lambda b: {"checked_files": b["files"], "findings": [dup]})
        self.assertEqual([f["gate"] for f in out.findings], ["artifact"])

    def test_unreadable_diff_fails_closed_without_calling_the_guard(self):
        out, engine = self.run_gates(None, clean)
        self.assertFalse(out.ok)
        self.assertEqual(engine.batches, [])
        self.assertIn("diff", out.problem)

    def test_missing_test_hosts_fails_closed(self):
        out, engine = self.run_gates([PLAIN], clean, cfg={"language": "en", "release_guard": {}})
        self.assertFalse(out.ok)
        self.assertIn("test_hosts", out.problem)
        self.assertEqual(engine.batches, [])

    def test_engine_failure_fails_closed(self):
        out, _ = self.run_gates([PLAIN], 1)
        self.assertFalse(out.ok)

    def test_malformed_answer_fails_closed(self):
        for answer in ("not json", {"findings": [{}]}, {"findings": []}):
            with self.subTest(answer=answer):
                out, _ = self.run_gates([PLAIN], answer)
                self.assertFalse(out.ok)

    def test_one_failed_batch_fails_the_whole_guard(self):
        cfg = {**CFG, "release_guard": {**CFG["release_guard"], "max_diff_lines": 1}}
        changes = [PLAIN, {"new_path": "c.py", "diff": "@@ -0,0 +1 @@\n+y = 2\n"}]
        out, engine = self.run_gates(changes, lambda b: clean(b) if b["batch"]["index"] == 1 else 1,
                                     cfg=cfg)
        self.assertEqual(len(engine.batches), 2)
        self.assertFalse(out.ok)

    def test_too_many_batches_fails_closed(self):
        cfg = {**CFG, "release_guard": {**CFG["release_guard"], "max_diff_lines": 1,
                                        "max_batches": 1}}
        changes = [PLAIN, {"new_path": "c.py", "diff": "@@ -0,0 +1 @@\n+y = 2\n"}]
        out, engine = self.run_gates(changes, clean, cfg=cfg)
        self.assertFalse(out.ok)
        self.assertEqual(engine.batches, [])


class TestApplies(unittest.TestCase):
    def test_only_release_branches(self):
        self.assertTrue(final_gates.applies("master", {}, "master"))
        self.assertTrue(final_gates.applies("MASTER", {}, "MASTER"))     # pocketms-frontend
        self.assertTrue(final_gates.applies("main", {}, "develop"))
        self.assertFalse(final_gates.applies("UAT-for-online", {}, "master"))
        self.assertFalse(final_gates.applies(None, {}, "master"))

    def test_the_projects_own_main_branch_is_a_release_branch(self):
        self.assertTrue(final_gates.applies("production", {}, "production"))

    def test_unknown_main_branch_gates_every_mr(self):
        self.assertTrue(final_gates.applies("UAT-for-online", {}, None))

    def test_configured_targets_replace_the_default(self):
        cfg = {"release_guard": {"target_branches": ["new-merge-branch", "master"]}}
        self.assertTrue(final_gates.applies("new-merge-branch", cfg, "master"))
        self.assertFalse(final_gates.applies("main", cfg, "master"))


class TestStatus(unittest.TestCase):
    def test_states(self):
        ok = final_gates.Outcome(rule=[], guard=[])
        hit = final_gates.Outcome(rule=[{"gate": "artifact", "file": "a", "hit_lines": [1]}], guard=[])
        broken = final_gates.Outcome(rule=[], guard=None, problem="guard failed")
        self.assertEqual(final_gates.status(ok)[0], "success")
        self.assertEqual(final_gates.status(hit)[0], "failed")
        self.assertEqual(final_gates.status(broken)[0], "failed")
        self.assertIn("guard failed", final_gates.status(broken)[1])


RULE_HIT = {"gate": "artifact", "file": "a.tsx", "line": 3, "hit_lines": [3],
            "title": "測試用程式碼會跟著上線", "confidence": "sure"}
UNSURE_LEAK = {**LEAK, "title": "預設 API 指向 UAT", "confidence": "unsure",
               "human_check": "確認正式機有設定 `API_HOST`"}


class TestSlackText(unittest.TestCase):
    def text(self, outcome):
        return final_gates.slack_text("g/app", 5, "http://gl/5", "master", outcome)

    def test_sure_and_unsure_findings_are_listed_apart(self):
        text = self.text(final_gates.Outcome(rule=[RULE_HIT], guard=[UNSURE_LEAK]))
        sure, unsure = text.split("需要人工確認")
        self.assertIn("`a.tsx:3`", sure)
        self.assertIn("`b.py:1`", unsure)
        self.assertIn("確認正式機有設定 `API_HOST`", unsure)
        self.assertIn("`master`", text)

    def test_only_sure_findings_ask_for_no_human_check(self):
        text = self.text(final_gates.Outcome(rule=[RULE_HIT], guard=[]))
        self.assertNotIn("需要人工確認", text)

    def test_incomplete_check_asks_for_a_human_check(self):
        text = self.text(final_gates.Outcome(rule=[RULE_HIT], guard=None, problem="guard failed"))
        self.assertIn("需要人工確認", text)
        self.assertIn("guard failed", text)
        self.assertIn("`a.tsx:3`", text)

    def test_status_counts_unsure_findings_separately(self):
        _, description = final_gates.status(
            final_gates.Outcome(rule=[RULE_HIT], guard=[UNSURE_LEAK]))
        self.assertIn("1 個需要人工確認", description)


if __name__ == "__main__":
    unittest.main()
