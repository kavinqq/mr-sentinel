"""Tests for fetch_mr.build_context and post_comment.post_findings (IO mocked)."""
import json
import pathlib
import tempfile
import unittest
import urllib.error
from unittest import mock

import fetch_mr
import post_comment

FAKE_CHANGES = {
    "title": "Fix login bug",
    "web_url": "https://gitlab.example.com/group/backend-app/-/merge_requests/45",
    "source_branch": "fix/login",
    "target_branch": "main",
    "diff_refs": {"base_sha": "b", "start_sha": "s", "head_sha": "h"},
    "changes": [
        {"new_path": "app/views.py", "old_path": "app/views.py", "diff": "@@\n+bug\n-old\n"},
        {"new_path": "package-lock.json", "old_path": "package-lock.json", "diff": "+x\n"},
    ],
}


class TestBuildContext(unittest.TestCase):
    def test_filters_noise_and_counts(self):
        with mock.patch("gitlab_client.get_mr_changes", return_value=FAKE_CHANGES):
            ctx = fetch_mr.build_context("https://gl", "tok", "group/backend-app", 45)
        self.assertEqual([c["new_path"] for c in ctx["changes"]], ["app/views.py"])
        self.assertEqual(ctx["stats"], {"files": 1, "lines": 2})
        self.assertEqual(ctx["diff_refs"]["head_sha"], "h")
        self.assertEqual(ctx["iid"], 45)
        self.assertEqual(ctx["title"], "Fix login bug")

    def test_write_context_roundtrip(self):
        with mock.patch("gitlab_client.get_mr_changes", return_value=FAKE_CHANGES):
            ctx = fetch_mr.build_context("https://gl", "tok", "group/backend-app", 45)
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "mr_context.json"
            fetch_mr.write_context(p, ctx)
            self.assertEqual(json.loads(p.read_text())["iid"], 45)


class FakeGitlab:
    def __init__(self, discussion_fail=False):
        self.discussions = []
        self.notes = []
        self.discussion_fail = discussion_fail

    def post_discussion(self, base, token, project, iid, body, position):
        if self.discussion_fail:
            raise urllib.error.HTTPError("u", 422, "unprocessable", {}, None)
        self.discussions.append((body, position))

    def post_note(self, base, token, project, iid, body):
        self.notes.append(body)


DIFF_REFS = {"base_sha": "b", "start_sha": "s", "head_sha": "h"}


class TestPostFindings(unittest.TestCase):
    def _findings(self):
        return [
            {"severity": "low", "title": "minor", "file": "a.py", "line": 3, "body": "x"},
            {"severity": "high", "title": "major", "file": "b.py", "line": 9, "body": "y"},
            {"severity": "medium", "title": "no line", "file": "c.py", "line": None, "body": "z"},
        ]

    def test_inline_sorted_and_note_for_no_line(self):
        fake = FakeGitlab()
        with mock.patch.object(post_comment, "gitlab_client", fake):
            n = post_comment.post_findings("B", "T", "p", 2, self._findings(), DIFF_REFS)
        self.assertEqual(n, 3)
        # two findings with a line -> inline discussions, high before low
        self.assertEqual([p["new_line"] for _, p in fake.discussions], [9, 3])
        # no line -> plain note, prefixed with the file path
        self.assertEqual(len(fake.notes), 1)
        self.assertIn("`c.py`", fake.notes[0])

    def test_fallback_to_note_on_422(self):
        fake = FakeGitlab(discussion_fail=True)
        with mock.patch.object(post_comment, "gitlab_client", fake):
            n = post_comment.post_findings("B", "T", "p", 2, [self._findings()[1]], DIFF_REFS)
        self.assertEqual(n, 1)
        self.assertEqual(len(fake.notes), 1)
        self.assertIn("`b.py:9`", fake.notes[0])

    def test_custom_signature_flows_through(self):
        fake = FakeGitlab()
        with mock.patch.object(post_comment, "gitlab_client", fake):
            post_comment.post_findings("B", "T", "p", 2, [self._findings()[1]], DIFF_REFS,
                                       signature="— sig-test")
        self.assertIn("sig-test", fake.discussions[0][0])


if __name__ == "__main__":
    unittest.main()
