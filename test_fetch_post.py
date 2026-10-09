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


class TestMergeMr(unittest.TestCase):
    def test_sha_is_sent_so_gitlab_rejects_a_moved_head(self):
        import gitlab_client
        with mock.patch.object(gitlab_client, "_call", return_value="{}") as call:
            gitlab_client.merge_mr("https://gl", "tok", "g/p", 3, sha="abc")
        self.assertEqual(call.call_args.kwargs["form"], {"sha": "abc"})


class TestDiscussionWrites(unittest.TestCase):
    def test_reply_and_resolve_hit_the_thread(self):
        import gitlab_client
        with mock.patch.object(gitlab_client, "_call", return_value="{}") as call:
            gitlab_client.reply_discussion("https://gl", "tok", "g/p", 3, "abc", "hi")
            gitlab_client.resolve_discussion("https://gl", "tok", "g/p", 3, "abc")
        reply, resolve = call.call_args_list
        self.assertTrue(reply.args[0].endswith("/merge_requests/3/discussions/abc/notes"))
        self.assertEqual((reply.kwargs["method"], reply.kwargs["form"]), ("POST", {"body": "hi"}))
        self.assertTrue(resolve.args[0].endswith("/merge_requests/3/discussions/abc"))
        self.assertEqual((resolve.kwargs["method"], resolve.kwargs["form"]),
                         ("PUT", {"resolved": "true"}))


class TestListDiscussions(unittest.TestCase):
    def test_reads_every_page(self):
        import gitlab_client
        pages = [json.dumps([{"id": i} for i in range(100)]), json.dumps([{"id": "last"}])]
        with mock.patch.object(gitlab_client, "_call", side_effect=pages) as call:
            got = gitlab_client.list_discussions("https://gl", "tok", "g/p", 3)
        self.assertEqual(len(got), 101)
        self.assertIn("page=2", call.call_args_list[1].args[0])

    def test_runaway_pagination_raises_instead_of_truncating(self):
        import gitlab_client
        full = json.dumps([{"id": i} for i in range(100)])
        with mock.patch.object(gitlab_client, "_call", return_value=full):
            with self.assertRaises(RuntimeError):
                gitlab_client.list_discussions("https://gl", "tok", "g/p", 3, max_pages=2)


class TestHasCiConfig(unittest.TestCase):
    def _run(self, project, file_result):
        import gitlab_client
        def call(url, token, method="GET", form=None):
            if "/repository/files/" in url:
                if isinstance(file_result, Exception):
                    raise file_result
                return "{}"
            return json.dumps(project)
        with mock.patch.object(gitlab_client, "_call", side_effect=call) as m:
            return gitlab_client.has_ci_config("https://gl", "tok", "g/p", "abc"), m

    def _404(self):
        return urllib.error.HTTPError("u", 404, "nf", {}, None)

    def test_default_ci_file_present(self):
        found, m = self._run({"ci_config_path": ""}, None)
        self.assertTrue(found)
        self.assertIn(".gitlab-ci.yml", m.call_args_list[-1].args[0])

    def test_default_ci_file_absent(self):
        self.assertFalse(self._run({"ci_config_path": None}, self._404())[0])

    def test_custom_ci_path_is_checked(self):
        _, m = self._run({"ci_config_path": "ci/main.yml"}, None)
        self.assertIn("ci%2Fmain.yml", m.call_args_list[-1].args[0])

    def test_external_ci_config_counts_as_ci(self):
        self.assertTrue(self._run({"ci_config_path": "ci.yml@group/ci-templates"}, None)[0])

    def test_other_http_errors_propagate(self):
        with self.assertRaises(urllib.error.HTTPError):
            self._run({}, urllib.error.HTTPError("u", 500, "x", {}, None))
