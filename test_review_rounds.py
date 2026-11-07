"""Later review rounds post a new finding only where the code changed since the
last completed round (review_rounds.py). Real git repositories, no mocks."""
import subprocess
import tempfile
import unittest

import review_common as rc
import review_rounds

FINANCIAL = "src/pages/Todo/pages/components/StockLendingFinancialSection.js"
STATIC = "src/pages/Operation/pages/components/PKFansExchange/PKFansExchangeStatic.js"
NAVBAR = "src/components/Navbar.js"


def js(lines: int, tag: str = "v1") -> str:
    return "".join(f"const line{n} = '{tag}';\n" for n in range(1, lines + 1))


class Repo:
    def __init__(self, test):
        """`test`: a TestCase, or a TestCase class for one clone per class."""
        d = tempfile.TemporaryDirectory()
        (test.addClassCleanup if isinstance(test, type) else test.addCleanup)(d.cleanup)
        self.path = d.name
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", self.path, "-c", "user.name=t", "-c",
                               "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, files: dict) -> str:
        from pathlib import Path
        for name, text in files.items():
            p = Path(self.path) / name
            if text is None:
                p.unlink()
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(text) if isinstance(text, bytes) else p.write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "c")
        return self.git("rev-parse", "HEAD")


def edit(text: str, line: int, new: str) -> str:
    rows = text.splitlines(keepends=True)
    rows[line - 1] = new + "\n"
    return "".join(rows)


def finding(file, line, title="財力門檻比例寫死", **extra):
    return {"severity": "medium", "title": title, "file": file, "line": line, **extra}


class TestChangedSince(unittest.TestCase):
    def test_a_modified_line_and_its_neighbours_count_as_changed(self):
        repo = Repo(self)
        h1 = repo.commit({FINANCIAL: js(40), NAVBAR: js(300)})
        h2 = repo.commit({FINANCIAL: edit(js(40), 20, "const ratio = 0.3;")})
        changed, why = review_rounds.changed_since(repo.path, h1, h2)
        self.assertEqual(why, "")
        self.assertEqual(changed, {FINANCIAL: {20}})

    def test_a_pure_deletion_touches_the_lines_on_either_side(self):
        """A reverted fix often only deletes lines: the code around it is what changed."""
        repo = Repo(self)
        base = js(30)
        fixed = edit(base, 10, "if (amount == null) return 0;\nconst line10 = 'v1';")
        h1 = repo.commit({FINANCIAL: fixed})
        h2 = repo.commit({FINANCIAL: base})
        changed, _ = review_rounds.changed_since(repo.path, h1, h2)
        self.assertEqual(changed, {FINANCIAL: {9, 10}})

    def test_added_deleted_and_binary_files_are_changed_as_a_whole(self):
        repo = Repo(self)
        h1 = repo.commit({FINANCIAL: js(5), STATIC: js(5), "logo.png": b"\x89PNG\x00\x01"})
        h2 = repo.commit({FINANCIAL: None, NAVBAR: js(3), "logo.png": b"\x89PNG\x00\x02"})
        changed, _ = review_rounds.changed_since(repo.path, h1, h2)
        self.assertEqual(changed[FINANCIAL], None)
        self.assertEqual(changed[NAVBAR], {1, 2, 3})
        self.assertEqual(changed["logo.png"], None)
        self.assertNotIn(STATIC, changed)

    def test_an_added_line_starting_with_plus_plus_is_not_a_file_header(self):
        """Codex P2: with -U0, adding `++ counter;` shows as `+++ counter;`."""
        repo = Repo(self)
        h1 = repo.commit({FINANCIAL: js(60)})
        text = edit(edit(js(60), 10, "++ counter;"), 40, "const ratio = 0.3;")
        h2 = repo.commit({FINANCIAL: text})
        changed, _ = review_rounds.changed_since(repo.path, h1, h2)
        self.assertEqual(changed, {FINANCIAL: {10, 40}})

    def test_renames_are_chained_to_the_current_name(self):
        repo = Repo(self)
        start = repo.commit({"README": "x\n", "src/keep.py": "x\n"})
        repo.commit({"old.py": js(30)})
        repo.git("mv", "old.py", "mid.py")
        repo.git("commit", "-q", "-m", "mv")
        repo.git("mv", "mid.py", "src/new.py")
        repo.git("commit", "-q", "-m", "mv")
        head = repo.git("rev-parse", "HEAD")
        moved = review_rounds.renames(repo.path, start, head)
        self.assertEqual(moved, {"old.py": {"mid.py", "src/new.py"}, "mid.py": {"src/new.py"}})
        self.assertEqual(review_rounds.follow({"old.py", FINANCIAL}, moved),
                         {"old.py", "mid.py", "src/new.py", FINANCIAL})

    def test_a_reused_path_keeps_every_file_it_named(self):
        """Codex P2 (pass 5): a→b, x→a, a→c. A comment on a.py may be about the
        first a (now b) or the second (now c); overwriting kept only c."""
        repo = Repo(self)
        start = repo.commit({"README": "x\n"})
        repo.commit({"a.py": js(30), "x.py": js(40)})
        for old, new in (("a.py", "b.py"), ("x.py", "a.py"), ("a.py", "c.py")):
            repo.git("mv", old, new)
            repo.git("commit", "-q", "-m", "mv")
        head = repo.git("rev-parse", "HEAD")
        moved = review_rounds.renames(repo.path, start, head)
        self.assertEqual(review_rounds.follow({"a.py"}, moved), {"a.py", "b.py", "c.py"})

    def test_a_rename_taken_in_by_merging_the_target_is_seen(self):
        """Codex P2 (pass 4): the rename is on the target; start..head excludes it."""
        repo = Repo(self)
        base = repo.commit({"old.py": js(30), "README": "x\n"})
        repo.git("checkout", "-q", "-b", "feature")
        repo.commit({"README": "y\n"})
        repo.git("checkout", "-q", "main")
        repo.git("mv", "old.py", "new.py")
        repo.git("commit", "-q", "-m", "rename on target")
        target = repo.git("rev-parse", "HEAD")
        repo.git("checkout", "-q", "feature")
        repo.git("merge", "-q", "--no-edit", "main")
        head = repo.git("rev-parse", "HEAD")
        self.assertNotEqual(base, target)
        self.assertEqual(review_rounds.renames(repo.path, target, head), {"old.py": {"new.py"}})

    def test_renames_without_a_start_are_unknown(self):
        self.assertIsNone(review_rounds.renames("/x", "", "h"))

    def test_a_rerun_on_the_same_head_changed_nothing(self):
        repo = Repo(self)
        h1 = repo.commit({FINANCIAL: js(5)})
        self.assertEqual(review_rounds.changed_since(repo.path, h1, h1), ({}, ""))

    def test_a_rebased_branch_does_not_chain(self):
        repo = Repo(self)
        root = repo.commit({FINANCIAL: js(5)})
        old_head = repo.commit({FINANCIAL: edit(js(5), 2, "const a = 1;")})
        repo.git("checkout", "-q", "-b", "rebased", root)
        new_head = repo.commit({FINANCIAL: edit(js(5), 2, "const a = 2;")})
        changed, why = review_rounds.changed_since(repo.path, old_head, new_head)
        self.assertIsNone(changed)
        self.assertIn("not an ancestor", why)

    def test_a_previous_head_the_clone_does_not_have_does_not_chain(self):
        repo = Repo(self)
        h1 = repo.commit({FINANCIAL: js(5)})
        changed, why = review_rounds.changed_since(repo.path, "0" * 40, h1)
        self.assertIsNone(changed)
        self.assertIn("unknown", why)

    def test_no_clone_does_not_chain(self):
        changed, why = review_rounds.changed_since(None, "a" * 40, "b" * 40)
        self.assertIsNone(changed)
        self.assertTrue(why)


class TestSplit(unittest.TestCase):
    SCOPE = review_rounds.Scope(previous="h1", changed={FINANCIAL: {20}, STATIC: None},
                                carried=frozenset())

    def split(self, findings, scope=None):
        kept, dropped = review_rounds.split(findings, scope or self.SCOPE)
        return [f["title"] for f in kept], [f["title"] for f in dropped]

    def test_a_new_finding_on_an_untouched_line_is_held_back(self):
        self.assertEqual(self.split([finding(FINANCIAL, 232, "untouched")]), ([], ["untouched"]))

    def test_a_new_finding_on_or_next_to_a_changed_line_is_posted(self):
        kept, dropped = self.split([finding(FINANCIAL, 20, "on"), finding(FINANCIAL, 23, "near"),
                                    finding(FINANCIAL, 24, "far")])
        self.assertEqual((kept, dropped), (["on", "near"], ["far"]))

    def test_a_finding_in_a_file_that_did_not_change_is_held_back(self):
        self.assertEqual(self.split([finding(NAVBAR, 215, "nav")]), ([], ["nav"]))

    def test_a_file_level_finding_follows_whether_its_file_changed(self):
        kept, dropped = self.split([finding(FINANCIAL, None, "changed file"),
                                    finding(NAVBAR, None, "untouched file")])
        self.assertEqual((kept, dropped), (["changed file"], ["untouched file"]))

    def test_a_finding_about_no_file_is_always_posted(self):
        self.assertEqual(self.split([finding(None, None, "MR 標題只寫現買改資買")]),
                         (["MR 標題只寫現買改資買"], []))

    def test_any_line_of_a_wholly_changed_file_counts(self):
        self.assertEqual(self.split([finding(STATIC, 51, "static")]), (["static"], []))

    def test_a_carried_file_keeps_every_finding(self):
        scope = review_rounds.Scope(previous="h1", changed={}, carried=frozenset({NAVBAR}))
        self.assertEqual(self.split([finding(NAVBAR, 215, "nav")], scope), (["nav"], []))

    def test_a_line_the_model_gave_as_text_still_counts(self):
        self.assertEqual(self.split([finding(FINANCIAL, "21", "text line")]), (["text line"], []))


class TestCarriedFiles(unittest.TestCase):
    """Files where a finding from before still stands: a re-find there is not new."""
    ME = 7

    def gate_thread(self, file, head, replies=()):
        body = f"🔴 **High** · x\n\n{rc.SIGNATURE_MARKER}\n{rc.GATE_HEAD_MARKER.format(head)}"
        return {"id": f"d-{file}", "notes": [
            {"id": 1, "author": {"id": self.ME}, "body": body,
             "position": {"new_path": file, "new_line": 3}, "resolvable": True,
             "resolved": False}, *replies]}

    def settled_thread(self, file, kind, judged_head="h3", anchored=True):
        """A finding closed by our accept: kind "fixed" / "reason" / "unknown"."""
        marker = {"fixed": f"{rc.APPEAL_ACCEPT}\n{rc.APPEAL_FIXED}",
                  "reason": f"{rc.APPEAL_ACCEPT}\n{rc.APPEAL_REASON}",
                  "unknown": rc.APPEAL_ACCEPT}[kind]
        judged = rc.JUDGED_ON_MARKER.format(f"{judged_head}:master") if judged_head else ""
        first = {"id": 1, "author": {"id": self.ME}, "resolvable": True, "resolved": True,
                 "body": f"🟠 **Medium** · x\n\n{rc.SIGNATURE_MARKER}"}
        if anchored:
            first["position"] = {"new_path": file, "new_line": 3}
        else:
            first["body"] = f"`{file}:3`\n\n{first['body']}"
        return {"id": f"s-{file}", "notes": [
            first, {"id": 2, "author": {"id": 99}, "body": "改好了", "resolvable": True,
                    "resolved": True},
            {"id": 3, "author": {"id": self.ME}, "resolvable": True, "resolved": True,
             "body": f"✅ ok\n{rc.APPEAL_SIGNATURE}\n{judged}\n{marker}"}]}

    def carried(self, discussions=(), recorded=(), changed=None, moved=None):
        """`changed`: files changed since any verdict head; None = git cannot tell."""
        return review_rounds.carried_files(
            list(discussions), self.ME, "h3", list(recorded),
            lambda sha: None if changed is None else set(changed), moved or {})

    def test_an_open_gate_comment_from_an_earlier_head_carries_its_file(self):
        """It no longer holds this head's status: the gate must post it again."""
        reply = {"id": 2, "author": {"id": 99}, "body": "這是設定值"}
        self.assertEqual(self.carried([self.gate_thread(NAVBAR, "h2", [reply])]), {NAVBAR})

    def test_a_gate_comment_closed_on_a_reason_carries_nothing(self):
        thread = self.settled_thread(NAVBAR, "reason", judged_head="h2")
        thread["notes"][0]["body"] += "\n" + rc.GATE_HEAD_MARKER.format("h2")
        self.assertEqual(self.carried([thread], changed={}), set())

    def test_an_unanswered_comment_the_rerun_deleted_carries_its_file(self):
        """The rerun cleanup deletes unanswered comments; only the history db has them."""
        rows = [{"file": STATIC, "status": "unanswered", "body": "x"},
                {"file": FINANCIAL, "status": "closed", "body": "x"}]
        self.assertEqual(self.carried(recorded=rows), {STATIC})

    def test_one_deleted_two_rounds_ago_still_carries_its_file(self):
        """Codex P1: h1 posted it, the rerun deleted it, the h2 round did not find it
        again — an h3 re-find on unchanged code is still not new."""
        rows = [{"file": NAVBAR, "head_sha": "h1", "status": "unanswered", "body": "x"}]
        self.assertEqual(self.carried(recorded=rows), {NAVBAR})

    def test_a_deleted_plain_note_names_its_file_in_its_prefix(self):
        body = f"`{STATIC}`\n\n🔴 x\n{rc.SIGNATURE_MARKER}\n{rc.GATE_HEAD_MARKER.format('h2')}"
        rows = [{"file": None, "status": "unanswered", "body": body},
                {"file": None, "status": "unanswered",
                 "body": f"`{NAVBAR}:215`\n\n🟠 x\n{rc.SIGNATURE_MARKER}"}]
        self.assertEqual(self.carried(recorded=rows), {STATIC, NAVBAR})

    def test_an_open_gate_note_without_a_position_carries_its_file(self):
        """Codex P1: a file-level gate finding, or one whose position GitLab refused, is
        a plain note; answered and still open, it no longer holds the new head."""
        thread = self.gate_thread(NAVBAR, "h2", [{"id": 2, "author": {"id": 99}, "body": "?"}])
        first = thread["notes"][0]
        first["body"] = f"`{NAVBAR}`\n\n{first['body']}"
        del first["position"]
        self.assertEqual(self.carried([thread]), {NAVBAR})

    def test_a_reason_settled_finding_is_reopened_only_where_its_file_changed(self):
        threads = [self.settled_thread(STATIC, "reason", "h2"),
                   self.settled_thread(NAVBAR, "reason", "h2")]
        self.assertEqual(self.carried(threads, changed={STATIC: {51}}), {STATIC})

    def test_a_fix_undone_in_another_file_reopens_the_settled_one(self):
        """Codex P1: fixed through the caller, the revert changes only the caller."""
        threads = [self.settled_thread("src/a.cpp", "fixed", "h2"),
                   self.settled_thread("src/b.cpp", "unknown", None, anchored=False)]
        self.assertEqual(self.carried(threads, changed={"src/caller.cpp": {12}}),
                         {"src/a.cpp", "src/b.cpp"})

    def test_a_reason_is_measured_from_its_verdict_not_the_last_round(self):
        """Codex P1 (pass 3): the reason no longer holds after h2 changed the file, the
        h2 round missed it and moved last_round; a rerun on h2 must still post it."""
        threads = [self.settled_thread(STATIC, "reason", "h1")]
        self.assertEqual(self.carried(threads, changed={STATIC}), {STATIC})

    def test_a_reason_on_a_renamed_file_follows_the_new_name(self):
        """Codex P2 (pass 4): commented on old.py, accepted on h2 after the rename to
        new.py, new.py changed on h3 — the comment still names old.py."""
        threads = [self.settled_thread("old.py", "reason", "h2")]
        self.assertEqual(self.carried(threads, changed={"new.py"}, moved={"old.py": {"new.py"}}),
                         {"old.py"})

    def test_a_reason_without_a_verdict_head_or_an_unknown_one_is_carried(self):
        override = self.settled_thread(STATIC, "reason", None)          # admin ✅
        self.assertEqual(self.carried([override], changed=set()), {STATIC})
        self.assertEqual(self.carried([self.settled_thread(NAVBAR, "reason", "h1")]), {NAVBAR})

    def test_a_missed_revert_is_still_reopened_on_a_same_head_rerun(self):
        """Codex P1 (pass 2): the round that saw the revert missed it and moved
        last_round; a rerun on that head changes nothing, the fix is still undone."""
        threads = [self.settled_thread("src/a.cpp", "fixed", "h2")]
        self.assertEqual(self.carried(threads, changed={}), {"src/a.cpp"})

    def test_a_fix_confirmed_on_this_very_head_cannot_have_been_undone(self):
        threads = [self.settled_thread("src/a.cpp", "fixed", "h3")]
        self.assertEqual(self.carried(threads, changed={"src/a.cpp": {3}}), set())


if __name__ == "__main__":
    unittest.main()
