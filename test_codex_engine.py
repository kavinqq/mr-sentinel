"""Tests for the codex engine (subprocess mocked; pure helpers covered)."""
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from engines import codex_engine

CFG = {
    "language": "en",
    "review_timeout_seconds": 900,
    "codex": {"model": "", "skeptic_model": ""},
}


class TestParseReply(unittest.TestCase):
    def test_raw_json(self):
        self.assertEqual(codex_engine.parse_json_reply('{"a": 1}'), {"a": 1})

    def test_fenced_json(self):
        self.assertEqual(codex_engine.parse_json_reply('```json\n{"a": 1}\n```'), {"a": 1})

    def test_json_with_surrounding_prose(self):
        self.assertEqual(
            codex_engine.parse_json_reply('Sure, here it is:\n{"a": 1}\nHope that helps!'),
            {"a": 1})


class TestApplyVerdicts(unittest.TestCase):
    CANDS = [
        {"severity": "low", "title": "A", "file": "a.py", "line": 1, "body": "x"},
        {"severity": "high", "title": "B", "file": "b.py", "line": 2, "body": "y"},
        {"severity": "medium", "title": "C", "file": "c.py", "line": 3, "body": "z"},
    ]

    def test_keep_drop_and_severity_override(self):
        verdicts = [
            {"index": 0, "verdict": "keep", "severity": "medium"},
            {"index": 1, "verdict": "drop", "reason": "convention"},
            {"index": 2, "verdict": "keep", "severity": "medium"},
        ]
        out = codex_engine.apply_verdicts(self.CANDS, verdicts)
        self.assertEqual([f["title"] for f in out], ["A", "C"])
        self.assertEqual(out[0]["severity"], "medium")  # override applied

    def test_candidate_without_verdict_is_dropped(self):
        out = codex_engine.apply_verdicts(self.CANDS, [{"index": 1, "verdict": "keep"}])
        self.assertEqual([f["title"] for f in out], ["B"])  # 0 and 2 unmentioned -> dropped


class TestCommand(unittest.TestCase):
    def test_build_cmd_read_only_sandbox(self):
        cmd = codex_engine.build_cmd("PROMPT", "/tmp/work", "/tmp/last.txt", model="")
        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertIn("read-only", cmd)
        self.assertIn("--output-last-message", cmd)
        self.assertIn("--skip-git-repo-check", cmd)  # work dir is not a git repo
        self.assertNotIn("-m", cmd)  # empty model -> CLI default

    def test_build_cmd_with_model(self):
        cmd = codex_engine.build_cmd("PROMPT", "/tmp/work", "/tmp/last.txt", model="o4-mini")
        self.assertIn("-m", cmd)
        self.assertIn("o4-mini", cmd)


class TestRunReview(unittest.TestCase):
    def test_two_pass_flow_writes_final_findings(self):
        scan_reply = json.dumps({"findings": [
            {"severity": "high", "title": "B", "file": "b.py", "line": 2, "body": "y"}]})
        verdict_reply = json.dumps({"verdicts": [
            {"index": 0, "verdict": "keep", "severity": "high"}]})
        with tempfile.TemporaryDirectory() as d:
            work = pathlib.Path(d)
            ctx = {"project": "g/p", "iid": 5,
                   "diff_refs": {"base_sha": "b", "start_sha": "s", "head_sha": "h"},
                   "changes": [{"new_path": "b.py", "diff": "+y\n"}]}
            (work / "mr_context.json").write_text(json.dumps(ctx))
            with mock.patch.object(codex_engine, "_exec_pass",
                                   side_effect=[scan_reply, verdict_reply]):
                rc = codex_engine.run_review(work, work / "mr_context.json",
                                             work / "final_findings.json", "/tmp/wt", CFG)
            self.assertEqual(rc, 0)
            out = json.loads((work / "final_findings.json").read_text())
            self.assertEqual(out["mr"]["iid"], 5)
            self.assertEqual(len(out["findings"]), 1)

    def test_empty_scan_skips_skeptic_pass(self):
        with tempfile.TemporaryDirectory() as d:
            work = pathlib.Path(d)
            ctx = {"project": "g/p", "iid": 5, "diff_refs": {}, "changes": []}
            (work / "mr_context.json").write_text(json.dumps(ctx))
            with mock.patch.object(codex_engine, "_exec_pass",
                                   side_effect=['{"findings": []}']) as m:
                rc = codex_engine.run_review(work, work / "mr_context.json",
                                             work / "final_findings.json", "/tmp/wt", CFG)
            self.assertEqual(rc, 0)
            self.assertEqual(m.call_count, 1)  # no candidates -> no skeptic call
            out = json.loads((work / "final_findings.json").read_text())
            self.assertEqual(out["findings"], [])


if __name__ == "__main__":
    unittest.main()
