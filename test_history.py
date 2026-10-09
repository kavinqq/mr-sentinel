"""Tests for the review-history package (sqlite in a temp dir; GitLab/engine mocked)."""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import review_common as rc
from history import classify, db, followups, score, sync
from history import __main__ as cli
from history.parse import category_marker, is_fix_mr, parse_comment

ME, DEV = 42, 7
NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)


def ai_body(sev="medium", title="t", category=None):
    return rc.format_comment_body({"severity": sev, "title": title, "problem": "p",
                                   "category": category}, "— 🤖 mr-sentinel AI review (m)")


def note(nid, author, body, **extra):
    return {"id": nid, "author": {"id": author, "name": f"u{author}"}, "body": body,
            "created_at": "2026-09-01T00:00:00Z", **extra}


def mr(mid=100, iid=5, author=DEV, state="merged", title="[feat] x", merged="2026-09-01T00:00:00Z",
       created="2026-09-01T00:00:00Z", branch="feature/x"):
    return {"id": mid, "iid": iid, "author": {"id": author, "username": f"pk{author}", "name": "Dev"},
            "title": title, "state": state, "source_branch": branch, "target_branch": "dev",
            "web_url": f"http://gl/{iid}", "created_at": created, "merged_at": merged,
            "updated_at": merged}


class DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "sub" / "sentinel.db"
        self.conn = db.connect(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()


class TestDb(DbCase):
    def test_creates_file_and_seeds_scoring_v1(self):
        self.assertTrue(self.path.exists())
        version, cfg = db.scoring_config(self.conn)
        self.assertEqual(version, 1)
        self.assertEqual(cfg["min_reviewed_mrs"], 5)

    def test_reconnect_does_not_rerun_migrations(self):
        self.conn.close()
        self.conn = db.connect(self.path)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM scoring_configs").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], len(db.MIGRATIONS))

    def test_wal_mode(self):
        self.assertEqual(self.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")


class TestParse(unittest.TestCase):
    def test_current_and_old_layouts(self):
        self.assertEqual(parse_comment(ai_body("high", "SQL injection", "security")),
                         {"severity": "high", "title": "SQL injection", "category": "security"})
        old = "🟠 [Medium] sync_to_model 的 atomic 未涵蓋 m2m save\n\n本次修正..."
        self.assertEqual(parse_comment(old)["severity"], "medium")
        self.assertIn("atomic", parse_comment(old)["title"])
        self.assertIsNone(parse_comment(old)["category"])

    def test_marker_only_for_known_categories(self):
        self.assertEqual(category_marker("nonsense"), "")
        self.assertNotIn("mr-sentinel:category", ai_body(category="nonsense"))
        self.assertIn(rc.SIGNATURE_MARKER, ai_body(category="security"))   # still ours

    def test_fix_detection(self):
        for title, branch in [("[ fix ] 修登入", "dev"), ("修正金額", "x"), ("x", "hotfix/1"),
                              ("Bug: null", "b")]:
            self.assertTrue(is_fix_mr(title, branch), title)
        for title, branch in [("[feat] 新功能", "feature/prefix-1"), ("ver: 版號更新 1.0.1", "dev"),
                              ("[ ver ] 版本更新 fix", "release")]:
            self.assertFalse(is_fix_mr(title, branch), title)


class TestRelease(unittest.TestCase):
    def test_release_detection(self):
        from history.parse import is_release_mr
        for title, src, tgt in [("ver: 版號更新 2.12.6.8", "pre-prod", "master"),
                                ("[ ver ] 版本更新至 1.15", "dev", "master"),
                                ("ver: 版號 0.0.14.53", "new-merge-branch", "master"),   # by title
                                ("anything", "release/1.2", "master"),
                                ("ver: 版號更新", "some-branch", "UAT")]:
            self.assertTrue(is_release_mr(title, src, tgt), (title, src))
        for title, src, tgt in [("[feat] 新增 version 檢查", "feature/version-check", "UAT"),
                                ("[ fix ] 修登入", "fix/login", "UAT"),
                                ("合併帳號", "feature/merge-accounts", "dev"),
                                # someone shipping their own work via a merge branch
                                ("[ feat ] 第三方登入改版", "new-merge-branch", "master"),
                                ("1.49.1_任務卡片管理增加複製任務", "mr/0806_merge", "MASTER")]:
            self.assertFalse(is_release_mr(title, src, tgt), (title, src))


class TestAttribution(DbCase):
    def setup_release(self):
        with self.conn:
            release = mr(mid=2, iid=2, author=8, title="ver: 版號更新 1.0", branch="pre-prod",
                         merged="2026-09-05T00:00:00Z", created="2026-09-04T00:00:00Z")
            release["target_branch"] = "master"
            sync.store_mr(self.conn, "g/app", release, [
                {"id": "d1", "notes": [note(10, ME, ai_body("high", "x", "security"),
                                            position={"new_path": "a.py", "new_line": 1},
                                            created_at="2026-09-04T12:00:00Z")]}], [], ME, ["a.py"])

    def test_release_finding_without_known_author_counts_for_nobody(self):
        self.setup_release()
        attributed = score.attribution(self.conn, CFG, NOW)
        self.assertEqual(attributed["findings"], [])
        self.assertEqual([f["note_id"] for f in attributed["unattributed"]], [10])
        self.assertNotIn("pk8", {r["username"] for r in score.team_report(self.conn, 1, CFG, NOW)})

    def test_confirmed_email_hands_a_release_finding_to_its_author(self):
        self.setup_release()
        with self.conn:
            sync.store_mr(self.conn, "g/app", mr(mid=1, iid=1, author=7), [], [], ME, None)
            self.conn.execute("INSERT INTO finding_blame VALUES (10, 'c1', 'seven@x', 'S', NULL, 'now')")
        self.assertEqual(score.attribution(self.conn, CFG, NOW)["unknown_emails"][0]["email"], "seven@x")
        with self.conn:
            self.conn.execute("INSERT INTO email_aliases(email, gitlab_id, actor, created_at) "
                              "VALUES ('seven@x', 7, 'me', '2026-10-01T00:00:00Z')")
        (f,) = score.attribution(self.conn, CFG, NOW)["findings"]
        self.assertEqual((f["how"], f["owner_author_id"]), ("blame", 7))


class TestBlame(DbCase):
    PORCELAIN = ("4f3c2a1b0000000000000000000000000000abcd 12 12 1\nauthor 小華\n"
                 "author-mail <Hua@Pocket.TW>\nauthor-time 1700000000\n\tcode\n")

    def release_with_finding(self, head_sha="h" * 40):
        rel = mr(mid=2, iid=2, author=8, title="ver: 版號更新", branch="pre-prod",
                 created="2026-09-04T00:00:00Z", merged="2026-09-05T00:00:00Z")
        rel.update(target_branch="master", sha=head_sha)
        with self.conn:
            sync.store_mr(self.conn, "g/app", rel, [{"id": "d", "notes": [note(10, ME, ai_body(),
                          position={"new_path": "a.py", "new_line": 12},
                          created_at="2026-09-04T12:00:00Z")]}], [], ME, None)

    def test_porcelain_parsing(self):
        from history.blame import parse_porcelain
        self.assertEqual(parse_porcelain(self.PORCELAIN),
                         {"sha": "4f3c2a1b0000000000000000000000000000abcd",
                          "email": "hua@pocket.tw", "name": "小華"})

    def test_blames_release_findings_once_and_records_failures(self):
        from history import blame
        self.release_with_finding()
        cfg = {"review": {"project_map": {"g/app": "/clone"}}}
        with mock.patch.object(blame, "blame_line", return_value={"sha": "c1", "email": "x@y", "name": "X"}), \
             mock.patch("os.path.exists", return_value=True):
            self.assertEqual(blame.blame_pending(self.conn, cfg), (1, 0))
            self.assertEqual(blame.blame_pending(self.conn, cfg), (0, 0))     # not again
        row = self.conn.execute("SELECT commit_sha, author_email, error FROM finding_blame").fetchone()
        self.assertEqual(tuple(row), ("c1", "x@y", None))

    def test_missing_clone_is_recorded_not_crashed(self):
        from history import blame
        self.release_with_finding()
        with self.assertLogs("mr_sentinel.history", "WARNING"):
            self.assertEqual(blame.blame_pending(self.conn, {"review": {"project_map": {}}}), (0, 1))
        self.assertIn("no local clone",
                      self.conn.execute("SELECT error FROM finding_blame").fetchone()[0])
        self.assertEqual(blame.retry_failed(self.conn), 1)

    def commits(self, mid, author, emails):
        with self.conn:
            sync.store_mr(self.conn, "g/app", mr(mid=mid, iid=mid, author=author), [], [], ME, None,
                          [{"id": f"c{mid}{i}", "author_email": e} for i, e in enumerate(emails)])

    def test_only_a_pure_mr_proves_an_email(self):
        self.commits(1, 7, ["seven@x", "seven@x"])           # 7 alone wrote this MR: proven
        self.commits(3, 9, ["nine@x", "seven@x", "john@x"])  # mixed merge MR: proves nothing
        self.assertEqual(score.email_owners(self.conn), {"seven@x": 7})

    def test_ambiguous_or_ignored_emails_map_to_nobody(self):
        self.commits(1, 7, ["shared@x"])
        self.commits(3, 9, ["shared@x"])                      # two people: ambiguous
        self.assertEqual(score.email_owners(self.conn), {})
        with self.conn:
            self.conn.execute("INSERT INTO email_aliases(email, gitlab_id, actor, created_at) "
                              "VALUES ('shared@x', 9, 'me', '2026-10-01T00:00:00Z'), "
                              "       ('shared@x', NULL, 'me', '2026-10-02T00:00:00Z')")
        self.assertEqual(score.email_owners(self.conn), {})        # latest says: ignore

    def test_blame_moves_a_finding_off_a_merge_mr_onto_its_author(self):
        self.commits(1, 7, ["seven@x"])                       # proves seven@x = 7
        with self.conn:      # 9's personal merge MR carries 7's line
            sync.store_mr(self.conn, "g/app", mr(mid=3, iid=3, author=9), [{"id": "d", "notes": [
                note(30, ME, ai_body(), position={"new_path": "a.py", "new_line": 1})]}], [], ME, None)
            self.conn.execute("INSERT INTO finding_blame VALUES (30, 'c10', 'seven@x', 'S', NULL, 'now')")
        (f,) = score.attribution(self.conn, CFG, NOW)["findings"]
        self.assertEqual((f["how"], f["owner_author_id"], f["owner_mr_id"]), ("blame", 7, None))


class TestStoreMr(DbCase):
    def store(self, discussions, awards=(), files=None, m=None):
        with self.conn:
            return sync.store_mr(self.conn, "g/app", m or mr(), discussions, list(awards), ME, files)

    def finding(self, nid):
        return self.conn.execute("SELECT * FROM findings WHERE note_id = ?", (nid,)).fetchone()

    def test_findings_status_appeal_and_files(self):
        accept = rc.appeal_reply_body("accept", "ok", "m")
        ds = [{"id": "d1", "notes": [note(1, ME, ai_body("high", "leak", "security"),
                                          position={"new_path": "a.py", "new_line": 3}),
                                     note(2, DEV, "不用修"), note(3, ME, accept)]},
              {"id": "d2", "notes": [note(4, DEV, "human comment")]}]
        self.assertEqual(self.store(ds, files=["a.py", "package-lock.json"]), 1)
        f = self.finding(1)
        self.assertEqual((f["severity"], f["category"], f["category_source"], f["file"], f["line"]),
                         ("high", "security", "review", "a.py", 3))
        self.assertEqual((f["status"], f["appeal_verdict"]), ("closed", "accept"))
        files = [r[0] for r in self.conn.execute("SELECT path FROM mr_files")]
        self.assertEqual(files, ["a.py"])                         # lock file is noise
        self.assertEqual(self.conn.execute("SELECT reviewed FROM mrs").fetchone()[0], 1)

    def test_deleted_comments_are_kept_as_absent(self):
        ds = [{"id": "d1", "notes": [note(1, ME, ai_body())]},
              {"id": "d2", "notes": [note(2, ME, ai_body())]}]
        self.store(ds)
        self.store(ds[:1])
        self.assertEqual((self.finding(1)["present"], self.finding(2)["present"]), (1, 0))
        self.store([])                                            # every comment gone
        self.assertEqual(self.finding(1)["present"], 0)

    def test_classifier_category_survives_resync_without_marker(self):
        ds = [{"id": "d1", "notes": [note(1, ME, ai_body())]}]
        self.store(ds)
        self.conn.execute("UPDATE findings SET category='performance', category_source='classifier'")
        self.store(ds)
        self.assertEqual(self.finding(1)["category"], "performance")

    def test_reviewed_from_eyes_and_never_unset(self):
        self.store([], awards=[{"name": "eyes", "user": {"id": ME}}])
        self.store([], awards=[])                                 # rerun briefly unclaimed
        self.assertEqual(self.conn.execute("SELECT reviewed FROM mrs").fetchone()[0], 1)


class TestFollowups(unittest.TestCase):
    def f(self, mid, merged, files, project="g/app"):
        return {"mr_id": mid, "project": project, "merged_at": merged, "files": files}

    def test_fix_pinned_on_latest_earlier_feature_within_window(self):
        features = [self.f(1, "2026-09-01T00:00:00Z", ["a.py"]),
                    self.f(2, "2026-09-10T00:00:00Z", ["a.py"]),
                    self.f(3, "2026-07-01T00:00:00Z", ["b.py"])]
        fixes = [self.f(9, "2026-09-15T00:00:00Z", ["a.py", "b.py"])]
        rows = followups.compute(features, fixes, [], days=30)
        self.assertEqual([(r["feature_mr_id"], r["kind"], r["days_after"]) for r in rows],
                         [(2, "fix_mr", 5.0)])                   # b.py's feature is 76 days old

    def test_ai_refind_ignores_the_same_mr_and_other_projects(self):
        features = [self.f(1, "2026-09-01T00:00:00Z", ["a.py"])]
        findings = [{"note_id": 10, "mr_id": 5, "project": "g/app", "file": "a.py",
                     "created_at": "2026-09-03T00:00:00Z"},
                    {"note_id": 11, "mr_id": 1, "project": "g/app", "file": "a.py",
                     "created_at": "2026-09-03T00:00:00Z"},
                    {"note_id": 12, "mr_id": 6, "project": "g/other", "file": "a.py",
                     "created_at": "2026-09-03T00:00:00Z"}]
        rows = followups.compute(features, [], findings, days=30)
        self.assertEqual([(r["feature_mr_id"], r["source_ref"]) for r in rows], [(1, "10")])


CFG = json.loads(json.dumps(db.DEFAULT_SCORING))


class TestScore(unittest.TestCase):
    def findings(self, *specs):
        return score.effective_findings(
            [{"note_id": i, "mr_id": 1, "severity": s, "category": c, "appeal_verdict": a}
             for i, (s, c, a) in enumerate(specs)], [])

    def mrs(self, n):
        return [{"mr_id": i, "reviewed": 1} for i in range(n)]

    def test_formula_and_level(self):
        fs = self.findings(("high", "security", None), ("medium", "correctness", None),
                           ("low", None, None), ("high", "correctness", "accept"))
        r = score.person_report(self.mrs(5), fs, [{"kind": "fix_mr"}], CFG, 1, NOW)
        # security 5×1.5 = 7.5; correctness 2 + an unconfirmed fix MR 3×0.5 = 3.5;
        # the uncategorized low is listed but not scored; the accepted appeal is out
        self.assertEqual((r["finding_weight"], r["followup_weight"]), (9.5, 1.5))
        self.assertEqual(r["unclassified"], 1)
        # per item 5 − 4 × weight / 5 MRs, floored at 0: security 0, correctness 2.2
        self.assertEqual({k: v["score"] for k, v in r["items"].items()},
                         {"security": 0.0, "requirements": 5.0, "correctness": 2.2,
                          "compatibility": 5.0, "operability": 5.0, "performance": 5.0,
                          "verification": 5.0, "maintainability": 5.0})
        self.assertEqual((r["score"], r["max_score"], r["level"]), (32.2, 40.0, "junior"))
        self.assertEqual(r["items"]["correctness"]["followups"], 1)
        self.assertEqual(r["appeal_accepted"], 1)

    def test_a_low_average_cannot_buy_back_a_high_finding(self):
        # 1 high over 40 MRs: 40 − 4×5/40 = 39.5 would be senior on score alone
        fs = self.findings(("high", "correctness", None))
        r = score.person_report(self.mrs(40), fs, [], CFG, 1, NOW)
        self.assertEqual((r["score"], r["level"]), (39.5, "mid"))
        self.assertEqual(r["next_level"], "mid+")
        self.assertEqual(r["next_level_misses"], ["high finding 1 則 > 0"])

    def test_clean_rate_gate(self):
        # 3 low maintainability (×0.5) on 3 different MRs of 10: 40 − 4×0.75/10 = 39.7,
        # but only 70% clean
        fs = score.effective_findings(
            [{"note_id": i, "mr_id": i, "severity": "low", "category": "maintainability",
              "appeal_verdict": None} for i in range(3)], [])
        r = score.person_report(self.mrs(10), fs, [], CFG, 1, NOW)
        self.assertEqual((r["score"], r["clean_mrs"], r["clean_rate"]), (39.7, 7, 0.7))
        self.assertEqual(r["level"], "mid")
        self.assertIn("乾淨 MR 70% < 85%", r["next_level_misses"])

    def test_spotless_is_senior_and_has_nothing_above(self):
        r = score.person_report(self.mrs(20), [], [], CFG, 1, NOW)
        self.assertEqual((r["score"], r["level"], r["next_level"], r["next_level_misses"]),
                         (40.0, "senior", None, []))

    def test_levels_without_gates_still_work(self):
        cfg = {**CFG, "levels": [{"level": "a", "min_score": 5}, {"level": "b", "min_score": None}]}
        r = score.person_report(self.mrs(5), self.findings(("high", "correctness", None)), [],
                                cfg, 1, NOW)
        self.assertEqual(r["level"], "a")

    def test_too_few_mrs_has_no_level(self):
        r = score.person_report(self.mrs(4), [], [], CFG, 1, NOW)
        self.assertEqual((r["score"], r["level"]), (40.0, None))

    def test_latest_override_wins_per_field(self):
        raw = [{"note_id": 1, "mr_id": 1, "severity": "high", "category": "correctness"}]
        reviews = [{"id": 1, "note_id": 1, "category": "security", "excluded": None,
                    "created_at": "2026-10-01"},
                   {"id": 2, "note_id": 1, "category": None, "excluded": 1, "created_at": "2026-10-02"},
                   {"id": 3, "note_id": 1, "category": None, "excluded": 0, "created_at": "2026-10-03"}]
        (f,) = score.effective_findings(raw, reviews)
        self.assertEqual((f["category"], f["excluded"]), ("security", False))


class TestTeamReport(DbCase):
    def test_end_to_end_from_db(self):
        with self.conn:
            for i in range(5):
                sync.store_mr(self.conn, "g/app", mr(mid=100 + i, iid=i, created="2026-09-20T00:00:00Z"),
                              [{"id": f"d{i}", "notes": [note(i + 1, ME, ai_body("high", "x", "security"))]}],
                              [], ME, ["a.py"])
            self.conn.execute("INSERT INTO finding_reviews(note_id, excluded, reason, actor, created_at) "
                              "VALUES (1, 1, 'false positive', 'me', '2026-10-01')")
        (row,) = score.team_report(self.conn, 1, CFG, NOW)
        self.assertEqual((row["username"], row["reviewed_mrs"], row["findings"], row["excluded"]),
                         ("pk7", 5, 4, 1))
        # 4 high security findings, merged unfixed: 4 × 5 × 1.5 × 2
        self.assertEqual((row["weights"]["security"], row["escaped"]), (60.0, 4))
        self.assertEqual(row["score"], 35.0)              # security floored at 0, 7 × 5 left


class TestRoles(DbCase):
    def seed(self):
        with self.conn:
            for author, n in ((7, 5), (8, 5)):
                for i in range(n):
                    sync.store_mr(self.conn, "g/app",
                                  mr(mid=author * 100 + i, iid=author * 100 + i, author=author,
                                     created="2026-09-20T00:00:00Z"),
                                  [{"id": f"d{author}{i}", "notes": [note(author * 100 + i, ME,
                                    ai_body("high", "x", "security" if author == 7 else "correctness"))]}],
                                  [], ME, None)

    def test_lead_is_reported_but_never_ranked_nor_averaged(self):
        self.seed()
        with self.conn:
            self.conn.execute("INSERT INTO person_roles(gitlab_id, role, actor, created_at) "
                              "VALUES (7, 'lead', 'me', '2026-10-01T00:00:00Z')")
        rows = score.team_report(self.conn, 1, CFG, NOW)
        lead = next(r for r in rows if r["author_id"] == 7)
        self.assertEqual((lead["ranked"], lead["level"], lead["role"]), (False, None, "lead"))
        self.assertIsNotNone(lead["score"])                    # stats still there
        self.assertEqual(rows[-1]["author_id"], 7)             # listed after the ranked team
        avg = score.team_average(rows, CFG)
        self.assertEqual(avg["security"], 5.0)                 # the lead's findings are not in it
        self.assertEqual(avg["correctness"], 0.0)

    def test_latest_role_wins(self):
        self.seed()
        with self.conn:
            self.conn.executemany("INSERT INTO person_roles(gitlab_id, role, actor, created_at) "
                                  "VALUES (7, ?, 'me', ?)", [("lead", "2026-10-01T00:00:00Z"),
                                                             ("member", "2026-10-02T00:00:00Z")])
        self.assertEqual(db.person_roles(self.conn), {7: "member"})

    def test_profile_axes_match_the_average(self):
        self.seed()
        rows = score.team_report(self.conn, 1, CFG, NOW)
        self.assertEqual(set(score.per_mr_profile(rows[0])), set(score.team_average(rows, CFG)))


class TestRoster(DbCase):
    def test_added_member_resolves_and_shows_without_mrs(self):
        with self.conn:
            self.conn.executemany("INSERT INTO roster_additions(username, actor, created_at) VALUES (?, 'me', 'x')",
                                  [("newbie",), ("ghost",)])
        found = {"newbie": {"id": 77, "username": "newbie", "name": "新人"}}
        with mock.patch.object(sync.gitlab_client, "find_user", side_effect=lambda b, t, u: found.get(u)):
            self.assertEqual(sync.resolve_roster(self.conn, {"gitlab_url": "u", "gitlab_token": "t"}), 1)
        (row,) = score.team_report(self.conn, 1, CFG, NOW)
        self.assertEqual((row["username"], row["reviewed_mrs"], row["level"]), ("newbie", 0, None))
        self.assertIn("找不到", self.conn.execute(
            "SELECT error FROM roster_additions WHERE username='ghost'").fetchone()[0])

    def test_departed_is_not_ranked(self):
        with self.conn:
            for i in range(5):
                sync.store_mr(self.conn, "g/app", mr(mid=i + 1, iid=i + 1, created="2026-09-20T00:00:00Z"),
                              [], [], ME, None)
            self.conn.execute("INSERT INTO person_roles(gitlab_id, role, actor, created_at) "
                              "VALUES (7, 'departed', 'me', 'x')")
        (row,) = score.team_report(self.conn, 1, CFG, NOW)
        self.assertEqual((row["ranked"], row["level"], row["role"]), (False, None, "departed"))


class TestEightItems(unittest.TestCase):
    def mrs(self, n):
        return [{"mr_id": i, "reviewed": 1} for i in range(n)]

    def test_merged_unfixed_costs_twice(self):
        fixed, shipped = score.effective_findings(
            [{"note_id": 1, "mr_id": 1, "severity": "medium", "category": "correctness"},
             {"note_id": 2, "mr_id": 2, "severity": "medium", "category": "correctness",
              "escaped": True}], [])
        self.assertEqual(score.finding_weight(fixed, CFG), 2.0)
        self.assertEqual(score.finding_weight(shipped, CFG), 4.0)

    def test_escape_rule(self):
        merged = {"state": "merged", "head_sha": "abc"}
        self.assertTrue(score.escaped({"head_sha": "abc", "status": "closed", "present": 1}, merged))
        self.assertTrue(score.escaped({"head_sha": "old", "status": "unanswered", "present": 1}, merged))
        self.assertFalse(score.escaped({"head_sha": "old", "status": "closed", "present": 1}, merged))
        self.assertFalse(score.escaped({"head_sha": "abc", "status": "unanswered", "present": 0}, merged))
        self.assertFalse(score.escaped({"head_sha": "abc", "status": "unanswered", "present": 1},
                                       {"state": "opened", "head_sha": "abc"}))

    def test_followups_count_under_a_category_by_verdict(self):
        fus = [{"kind": "fix_mr"},                                              # 3 × 0.5
               {"kind": "fix_mr", "verdict": "confirmed"},                      # 3
               {"kind": "fix_mr", "verdict": "unrelated"},                      # 0, not counted
               {"kind": "ai_refind", "category": "security", "verdict": "confirmed"}]   # 1
        r = score.person_report(self.mrs(10), [], fus, CFG, 1, NOW)
        self.assertEqual(r["weights"]["correctness"], 4.5)
        self.assertEqual(r["weights"]["security"], 1.0)
        self.assertEqual(r["followups"], {"fix_mr": 2, "ai_refind": 1})

    def test_old_category_counts_provisionally_and_unknown_is_not_scored(self):
        fs = score.effective_findings(
            [{"note_id": 1, "mr_id": 1, "severity": "low", "category": None, "category_legacy": "code_smell"},
             {"note_id": 2, "mr_id": 1, "severity": "low", "category": None, "category_legacy": "code_quality"},
             {"note_id": 3, "mr_id": 1, "severity": "low", "category": "needs_review"}], [])
        self.assertEqual([(f["category"], f["category_provisional"]) for f in fs],
                         [("maintainability", True), ("uncategorized", False), ("needs_review", False)])
        r = score.person_report(self.mrs(5), fs, [], CFG, 1, NOW)
        self.assertEqual((r["findings"], r["unclassified"], r["items"]["maintainability"]["count"]),
                         (3, 2, 1))

    def test_a_weak_item_blocks_the_level(self):
        # 2 high correctness over 40 MRs: total 39.0 but correctness 4.0
        fs = self.findings_of(("high", "correctness"), ("high", "correctness"))
        r = score.person_report(self.mrs(40), fs, [], {**CFG, "levels": [
            {"level": "a", "min_score": 38, "min_item": 4.5},
            {"level": "b", "min_score": None}]}, 1, NOW)
        self.assertEqual((r["score"], r["level"]), (39.0, "b"))
        self.assertEqual(r["next_level_misses"], ["最弱一項 4.0 < 4.5"])

    def findings_of(self, *specs):
        return score.effective_findings(
            [{"note_id": i, "mr_id": i, "severity": s, "category": c} for i, (s, c) in enumerate(specs)], [])

    def test_old_comment_marker_maps_or_waits_for_the_classifier(self):
        self.assertEqual(parse_comment("x <!-- mr-sentinel:category=code_smell -->")["category"],
                         "maintainability")
        self.assertIsNone(parse_comment("x <!-- mr-sentinel:category=code_quality -->")["category"])
        self.assertEqual(parse_comment("x <!-- mr-sentinel:category=operability -->")["category"],
                         "operability")

    def test_classifier_may_not_file_requirements_it_cannot_see(self):
        reply = {"categories": [{"id": 1, "category": "requirements"},
                                {"id": 2, "category": "needs_review"},
                                {"id": 3, "category": "code_smell"},
                                {"id": 4, "category": "operability"}]}
        self.assertEqual(classify.valid_categories(reply, [1, 2, 3, 4]),
                         {2: "needs_review", 4: "operability"})

    def test_migration_7_keeps_the_old_category_and_clears_classifier_ones(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.db")
            conn = sqlite3.connect(path)
            for number, script in enumerate(db.MIGRATIONS[:6], start=1):
                conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
            conn.execute("INSERT INTO scoring_configs VALUES (1, ?, 'x', 'system', 'x')",
                         (json.dumps({**CFG, "levels": [{"level": "a", "min_score": None}]}),))
            conn.execute("INSERT INTO mrs(mr_id, project, iid) VALUES (1, 'g/a', 1)")
            conn.executemany("INSERT INTO findings(note_id, mr_id, category, category_source) "
                             "VALUES (?, 1, ?, ?)", [(1, "correctness", "classifier"),
                                                     (2, "security", "review"),
                                                     (3, "code_smell", "review")])
            conn.commit(); conn.close()
            conn = db.connect(path)
            rows = [tuple(r) for r in conn.execute(
                "SELECT note_id, category, category_legacy FROM findings ORDER BY note_id")]
            self.assertEqual(rows, [(1, None, "correctness"), (2, "security", "security"),
                                    (3, None, "code_smell")])
            conn.close()


class TestTenPointMigration(unittest.TestCase):
    def test_old_lower_is_better_config_gets_a_successor_and_names_itself(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.db")
            conn = sqlite3.connect(path); conn.row_factory = sqlite3.Row
            for number, script in enumerate(db.MIGRATIONS[:5], start=1):
                conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
            old = {**CFG, "severity_weight": {"high": 9, "medium": 2, "low": 0.5},
                   "levels": [{"level": "senior", "max_score": 0.8},
                              {"level": "junior", "max_score": None}]}
            for key in ("deduction_per_weight", "item_max", "escape_multiplier"):
                del old[key]
            conn.execute("INSERT INTO scoring_configs VALUES (1, ?, 'old', 'system', 'x')",
                         (json.dumps(old),))
            conn.commit(); conn.close()
            conn = db.connect(path)
            version, cfg = db.scoring_config(conn)
            self.assertEqual(version, 3)                  # 6: 10-point, 7: 8 items × 5
            self.assertEqual(cfg["severity_weight"]["high"], 9)              # tuned weight kept
            self.assertEqual(cfg["levels"], db.DEFAULT_SCORING["levels"])
            from history import snapshot
            with conn:
                sync.store_mr(conn, "g/app", mr(mid=1, iid=1, created="2026-09-20T00:00:00Z"),
                              [{"id": "d", "notes": [note(1, ME, ai_body("low", "x", "correctness"))]}],
                              [], ME, None)
            snapshot.record(conn, "排程同步")
            trigger = conn.execute("SELECT trigger FROM score_events LIMIT 1").fetchone()[0]
            self.assertTrue(trigger.startswith(db.TAXONOMY_CHANGE_NOTE), trigger)
            self.assertIsNone(db.get_state(conn, "score_note"))
            conn.close()

    def test_fresh_db_does_not_get_a_second_version(self):
        with tempfile.TemporaryDirectory() as d:
            conn = db.connect(os.path.join(d, "new.db"))
            self.assertEqual(db.scoring_config(conn)[0], 1)
            conn.close()


class TestScan(DbCase):
    CONFIG = {"gitlab_url": "u", "gitlab_token": "t",
              "review": {"project_map": {"g/app": {}, "g/web": {}}}}

    def patches(self, list_mrs):
        from history import scan
        return [mock.patch.object(scan.gitlab_client, "list_mrs", side_effect=list_mrs),
                mock.patch.object(scan.sync, "_me", return_value=ME),
                mock.patch.object(scan.sync, "resolve_roster"),
                mock.patch.object(scan.sync, "sync_one", return_value=1),
                mock.patch.object(scan.classify, "classify_pending", return_value=(0, 0)),
                mock.patch.object(scan.blame, "blame_pending", return_value=(0, 0))]

    def run_scan(self, list_mrs, **kw):
        from history import scan
        ps = self.patches(list_mrs)
        for p in ps:
            p.start()
        try:
            return scan.scan(self.conn, self.CONFIG, **kw)
        finally:
            for p in ps:
                p.stop()

    def test_scans_every_project_over_the_window_and_marks_it_done(self):
        from history import scan
        calls = []
        def list_mrs(base, token, project, updated_after=None):
            calls.append((project, updated_after))
            return [{"iid": 1, "updated_at": "2026-09-01T00:00:00Z"}]
        self.assertTrue(scan.needed(self.conn))
        result = self.run_scan(list_mrs)
        self.assertEqual([c[0] for c in calls], ["g/app", "g/web"])
        self.assertEqual(result["days"], 90)                          # scoring window
        since = datetime.fromisoformat(calls[0][1].replace("Z", "+00:00"))
        self.assertAlmostEqual((datetime.now(timezone.utc) - since).days, 90, delta=1)
        self.assertEqual(result["projects"]["g/app"], {"mrs": 1, "findings": 1})
        self.assertEqual(db.get_state(self.conn, "mrs_updated_at:g/web"), "2026-09-01T00:00:00Z")
        self.assertFalse(scan.needed(self.conn))

    def test_a_failed_project_does_not_count_as_the_first_scan(self):
        from history import scan
        def list_mrs(base, token, project, updated_after=None):
            if project == "g/web":
                raise OSError("down")
            return []
        result = self.run_scan(list_mrs)
        self.assertIn("g/web", result["failed"])
        self.assertTrue(scan.needed(self.conn))                    # next run tries again

    def test_dry_run_writes_nothing(self):
        from history import scan
        result = self.run_scan(lambda *a, **k: [{"iid": 1}, {"iid": 2}], dry_run=True, days=30)
        self.assertEqual(result["projects"]["g/app"], {"mrs": 2})
        self.assertTrue(scan.needed(self.conn))
        self.assertIsNone(db.get_state(self.conn, "mrs_updated_at:g/app"))

    def test_run_scans_the_first_time_then_syncs(self):
        from history import scan
        with mock.patch.object(scan, "scan", return_value={"failed": {}}) as first, \
             mock.patch.object(cli.sync, "sync_all", return_value={"failed": {}}) as incremental, \
             mock.patch.object(cli.classify, "classify_pending", return_value=(0, 0)), \
             mock.patch.object(cli.blame, "blame_pending", return_value=(0, 0)):
            cli.run(self.conn, self.CONFIG)
            self.assertEqual((first.call_count, incremental.call_count), (1, 0))
            db.set_state(self.conn, "initial_scan_at", "2026-10-09T00:00:00Z")
            cli.run(self.conn, self.CONFIG)
            self.assertEqual((first.call_count, incremental.call_count), (1, 1))


class TestEvaluate(DbCase):
    CONFIG = {"review": {"engine": "claude", "language": "zh-TW"}}
    REPLY = {"summary": "s", "strengths": [{"point": "p1", "evidence": "e"}],
             "weaknesses": [{"point": "w1", "evidence": "e", "advice": "a"}]}

    def seed(self):
        with self.conn:
            for i in range(5):
                sync.store_mr(self.conn, "g/app", mr(mid=i + 1, iid=i + 1, created="2026-09-20T00:00:00Z"),
                              [{"id": f"d{i}", "notes": [note(i + 1, ME, ai_body("low", f"t{i}", "correctness"))]}],
                              [], ME, None)

    def run_eval(self, reply, **kw):
        from history import evaluate
        engine = mock.Mock()
        engine.run_json.side_effect = reply if isinstance(reply, Exception) else (lambda *a: reply)
        with mock.patch.object(evaluate.engines, "get_engine", return_value=engine), \
             mock.patch.object(evaluate, "WORK_DIR", Path(self.tmp.name) / "w"):
            return evaluate.evaluate_pending(self.conn, self.CONFIG, **kw), engine

    def test_writes_once_then_only_when_the_record_changes(self):
        from history import evaluate
        self.seed()
        (done, bad), engine = self.run_eval(self.REPLY)
        self.assertEqual((done, bad), (1, 0))
        prompt = engine.run_json.call_args.args[0]
        self.assertIn("Traditional Chinese", prompt)
        self.assertIn('"t0"', prompt)                                 # the findings go in
        (e,) = evaluate.latest(self.conn).values()
        self.assertEqual((e["summary"], e["weaknesses"][0]["advice"]), ("s", "a"))
        self.assertEqual(self.run_eval(self.REPLY)[0], (0, 0))       # nothing changed: no AI call
        self.assertEqual(self.run_eval(self.REPLY, force=True)[0], (1, 0))
        with self.conn:      # a human excludes a finding -> the record changed
            self.conn.execute("INSERT INTO finding_reviews(note_id, excluded, actor, created_at) "
                              "VALUES (1, 1, 'lead', '2026-10-09T00:00:00Z')")
        self.assertEqual(evaluate.stale(self.conn), {DEV})
        self.assertEqual(self.run_eval(self.REPLY)[0], (1, 0))

    def test_a_bad_reply_keeps_the_previous_evaluation(self):
        from history import evaluate
        self.seed()
        self.run_eval(self.REPLY)
        self.assertEqual(self.run_eval({"summary": "", "strengths": []}, force=True)[0], (0, 1))
        self.assertEqual(self.run_eval(RuntimeError("down"), force=True)[0], (0, 1))
        self.assertEqual(evaluate.latest(self.conn)[DEV]["summary"], "s")

    def test_reply_is_trimmed_to_the_contract(self):
        from history import evaluate
        out = evaluate.valid_reply({"summary": " a  b ", "strengths": [{"point": "x"}] * 9 + ["junk"],
                                    "weaknesses": [{"point": ""}, {"point": "y", "evidence": "z" * 999}]})
        self.assertEqual(out["summary"], "a b")
        self.assertEqual(len(out["strengths"]), 4)
        self.assertEqual(out["weaknesses"], [{"point": "y", "evidence": "z" * 400, "advice": ""}])
        self.assertIsNone(evaluate.valid_reply({"summary": "only"}))


class TestScoreLog(DbCase):
    def seed(self, n=5, sev="high"):
        with self.conn:
            for i in range(n):
                sync.store_mr(self.conn, "g/app", mr(mid=i + 1, iid=i + 1, created="2026-09-20T00:00:00Z"),
                              [{"id": f"d{i}", "notes": [note(i + 1, ME, ai_body(sev, "x", "correctness"))]}],
                              [], ME, None)

    def test_first_record_then_only_changes(self):
        from history import snapshot
        self.seed(sev="low")
        first = snapshot.record(self.conn, "init")
        self.assertEqual({e["gitlab_id"] for e in first}, {7, db.TEAM})
        self.assertEqual(snapshot.record(self.conn, "nothing new"), [])     # no change, no log
        with self.conn:      # a human excludes one finding -> score rises
            self.conn.execute("INSERT INTO finding_reviews(note_id, excluded, actor, created_at) "
                              "VALUES (1, 1, 'lead', '2026-10-09T00:00:00Z')")
        (person, team) = sorted(snapshot.record(self.conn, "覆核 note 1", "lead"),
                                key=lambda e: e["gitlab_id"] == db.TEAM)
        # 5 low correctness, merged unfixed (×2): 5 − 4×5/5 = 1 -> 36; one out: 36.8
        self.assertEqual((person["old_score"], person["new_score"]), (36.0, 36.8))
        row = self.conn.execute("SELECT trigger, actor FROM score_events ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(tuple(row), ("覆核 note 1", "lead"))

    def test_sync_mr_runs_the_whole_chain(self):
        from history import snapshot
        with mock.patch.object(sync, "sync_one"), mock.patch.object(sync, "_me", return_value=ME), \
             mock.patch.object(sync.gitlab_client, "get_mr", return_value=mr()), \
             mock.patch.object(snapshot, "refresh") as refresh:
            self.assertTrue(sync.sync_mr({"gitlab_url": "u", "gitlab_token": "t"}, "g/app", 5,
                                         conn=self.conn, trigger="review 完成 app!5"))
        self.assertEqual(refresh.call_args.args[2], "review 完成 app!5")


class TestCodexRound(DbCase):
    def test_own_later_finding_is_not_also_an_ai_refind(self):
        with self.conn:
            for i, created in ((1, "2026-09-01T00:00:00Z"), (2, "2026-09-05T00:00:00Z")):
                sync.store_mr(self.conn, "g/app", mr(mid=i, iid=i, created=created, merged=created),
                              [] if i == 1 else [{"id": "d", "notes": [note(20, ME, ai_body("medium", "x", "correctness"),
                              position={"new_path": "a.py", "new_line": 1}, created_at="2026-09-05T00:00:00Z")]}],
                              [], ME, ["a.py"])
        followups.refresh(self.conn, 30)
        self.assertEqual(self.conn.execute("SELECT kind FROM followups").fetchall()[0][0], "ai_refind")
        (row,) = score.team_report(self.conn, 1, CFG, NOW)
        self.assertEqual((row["findings"], row["followups"]["ai_refind"]), (1, 0))   # counted once

    def test_truncated_commit_list_proves_nothing(self):
        with self.conn:
            sync.store_mr(self.conn, "g/app", mr(mid=1, iid=1), [], [], ME, None,
                          ([{"id": "c", "author_email": "x@y"}], False))
        self.assertEqual(score.email_owners(self.conn), {})

    def test_snapshot_logs_count_changes_too(self):
        from history import snapshot
        with self.conn:
            for i in range(5):
                sync.store_mr(self.conn, "g/app", mr(mid=i + 1, iid=i + 1, created="2026-09-20T00:00:00Z"),
                              [], [], ME, None)
        snapshot.record(self.conn, "init")
        with self.conn:      # one more clean MR: score stays 0.0, MR count changes
            sync.store_mr(self.conn, "g/app", mr(mid=9, iid=9, created="2026-09-21T00:00:00Z"), [],
                          [{"name": "eyes", "user": {"id": ME}}], ME, None)
        events = snapshot.record(self.conn, "new clean MR")
        self.assertTrue(any(e["gitlab_id"] == 7 for e in events))

    def test_blame_rejects_bad_shas_before_git(self):
        from history import blame
        with self.assertRaises(ValueError):
            blame.blame_line("/clone", 1, "HEAD; rm -rf /", "a.py", 1)


class TestClassify(DbCase):
    def test_only_valid_answers_fill_only_empty_categories(self):
        with self.conn:
            sync.store_mr(self.conn, "g/app", mr(), [
                {"id": "d1", "notes": [note(1, ME, ai_body())]},
                {"id": "d2", "notes": [note(2, ME, ai_body())]},
                {"id": "d3", "notes": [note(3, ME, ai_body(category="performance"))]}], [], ME, None)
        engine = mock.Mock()
        engine.run_json.return_value = {"categories": [
            {"id": 1, "category": "security"}, {"id": 2, "category": "made_up"},
            {"id": 3, "category": "security"}]}
        cfg = {"review": {"engine": "claude"}}
        with mock.patch.object(classify.engines, "get_engine", return_value=engine), \
             mock.patch.object(classify, "WORK_DIR", Path(self.tmp.name)):
            self.assertEqual(classify.classify_pending(self.conn, cfg), (1, 0))
        cats = dict(self.conn.execute("SELECT note_id, category FROM findings").fetchall())
        self.assertEqual(cats, {1: "security", 2: None, 3: "performance"})
        prompt = engine.run_json.call_args.args[0]
        self.assertNotIn("<details>", prompt)

    def test_failed_batch_leaves_findings_for_next_run(self):
        with self.conn:
            sync.store_mr(self.conn, "g/app", mr(), [{"id": "d1", "notes": [note(1, ME, ai_body())]}],
                          [], ME, None)
        engine = mock.Mock()
        engine.run_json.side_effect = RuntimeError("quota")
        with mock.patch.object(classify.engines, "get_engine", return_value=engine), \
             mock.patch.object(classify, "WORK_DIR", Path(self.tmp.name)), \
             self.assertLogs("mr_sentinel.history", "ERROR"):
            self.assertEqual(classify.classify_pending(self.conn, {"review": {"engine": "claude"}}),
                             (0, 1))                                 # failure is reported


class TestRun(DbCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(cli.evaluate, "evaluate_pending", return_value=(0, 0))
        self.evaluate = patcher.start()
        self.addCleanup(patcher.stop)
        with self.conn:                  # already scanned once: run() is the incremental job
            db.set_state(self.conn, "initial_scan_at", "2026-10-01T00:00:00Z")

    def test_dashboard_requests_are_claimed_and_closed(self):
        with self.conn:
            self.conn.execute("INSERT INTO sync_requests(kind, requested_by, requested_at) "
                              "VALUES ('full_sync', 'me', '2026-10-09')")
        with mock.patch.object(cli.sync, "sync_all", return_value={"synced": {}, "failed": {}}) as s, \
             mock.patch.object(cli.classify, "classify_pending", return_value=(0, 0)):
            cli.run(self.conn, {"review": {}})
        self.assertTrue(s.call_args.kwargs["full"])
        req = self.conn.execute("SELECT * FROM sync_requests").fetchone()
        self.assertIsNotNone(req["started_at"])
        self.assertIsNotNone(req["finished_at"])

    def request(self, started=None):
        with self.conn:
            self.conn.execute("INSERT INTO sync_requests(kind, requested_by, requested_at, "
                              "started_at) VALUES ('sync', 'me', '2026-10-09T00:00:00Z', ?)",
                              (started,))

    def test_crashed_run_still_finishes_the_request_with_the_error(self):
        self.request()
        with mock.patch.object(cli.sync, "sync_all", side_effect=RuntimeError("gitlab down")):
            with self.assertRaises(RuntimeError):
                cli.run(self.conn, {"review": {}})
        req = self.conn.execute("SELECT * FROM sync_requests").fetchone()
        self.assertIsNotNone(req["finished_at"])
        self.assertIn("gitlab down", req["result"])

    def test_stale_unfinished_claim_is_picked_up_again(self):
        self.request(started="2026-01-01T00:00:00Z")              # killed long ago
        self.request(started=db.now_iso())                         # running right now
        _, ids = cli._requested(self.conn)
        self.assertEqual(ids, [1])

    def test_failed_classify_batches_surface_as_failure(self):
        with mock.patch.object(cli.sync, "sync_all", return_value={"synced": {}, "failed": {}}), \
             mock.patch.object(cli.classify, "classify_pending", return_value=(3, 2)):
            result = cli.run(self.conn, {"review": {}})
        self.assertIn("classify", result["failed"])


class TestTimestamps(DbCase):
    def test_everything_is_stored_as_utc_z(self):
        self.assertEqual(db.utc("2026-09-01T08:00:00+08:00"), "2026-09-01T00:00:00Z")
        self.assertEqual(db.utc("2026-09-01T00:00:00.262Z"), "2026-09-01T00:00:00Z")
        self.assertIsNone(db.utc(None))
        with self.conn:
            sync.store_mr(self.conn, "g/app", mr(created="2026-09-01T08:00:00+08:00"), [], [], ME, None)
        self.assertEqual(self.conn.execute("SELECT created_at FROM mrs").fetchone()[0],
                         "2026-09-01T00:00:00Z")


class TestSyncProject(DbCase):
    def test_backfill_is_bounded_by_update_time_not_creation(self):
        """An MR opened before the bot existed but reviewed after must be read."""
        with mock.patch.object(sync.gitlab_client, "list_mrs", return_value=[]) as lm:
            sync.sync_project(self.conn, {"gitlab_url": "u", "gitlab_token": "t"}, "g/app", ME)
        self.assertEqual(lm.call_args.kwargs, {"updated_after": sync.DEFAULT_SINCE})

    def test_failed_fetch_rolls_back_and_releases_the_write_lock(self):
        with mock.patch.object(sync.gitlab_client, "get_mr", return_value=mr()), \
             mock.patch.object(sync.gitlab_client, "list_discussions",
                               side_effect=RuntimeError("timeout")):
            with self.assertRaises(RuntimeError):
                sync.sync_one(self.conn, "u", "t", "g/app", mr(), ME)
        self.assertFalse(self.conn.in_transaction)
        with self.conn:                                            # db still writable
            sync.store_mr(self.conn, "g/app", mr(), [], [], ME, None)


class TestCursor(DbCase):
    def test_cursor_is_the_latest_update_after_normalising(self):
        mrs = [mr(mid=1, iid=1, merged="2026-09-01T10:00:00+08:00"),     # = 02:00Z
               mr(mid=2, iid=2, merged="2026-09-01T03:00:00Z")]
        gl = sync.gitlab_client
        with mock.patch.object(gl, "list_mrs", return_value=mrs), \
             mock.patch.object(gl, "get_mr", side_effect=lambda b, t, p, iid: mrs[iid - 1]), \
             mock.patch.object(gl, "list_discussions", return_value=[]), \
             mock.patch.object(gl, "get_award_emojis", return_value=[]), \
             mock.patch.object(gl, "get_mr_files", return_value=[]), \
             mock.patch.object(gl, "get_mr_commits", return_value=[]):
            sync.sync_project(self.conn, {"gitlab_url": "u", "gitlab_token": "t"}, "g/app", ME)
        # as raw strings "2026-09-01T10:00:00+08:00" would have won
        self.assertEqual(db.get_state(self.conn, "mrs_updated_at:g/app"), "2026-09-01T03:00:00Z")

    def test_mr_is_reread_under_the_lock(self):
        stale, fresh = mr(state="opened", merged=None), mr(state="merged")
        gl = sync.gitlab_client
        with mock.patch.object(gl, "get_mr", return_value=fresh), \
             mock.patch.object(gl, "list_discussions", return_value=[]), \
             mock.patch.object(gl, "get_award_emojis", return_value=[]), \
             mock.patch.object(gl, "get_mr_files", return_value=["a.py"]), \
             mock.patch.object(gl, "get_mr_commits", return_value=[{"id": "c1", "author_email": "A@x"}]):
            sync.sync_one(self.conn, "u", "t", "g/app", stale, ME)
        self.assertEqual(self.conn.execute("SELECT sha, author_email FROM mr_commits").fetchall()[0][:],
                         ("c1", "a@x"))                         # emails are stored lowercased
        self.assertEqual(self.conn.execute("SELECT state FROM mrs").fetchone()[0], "merged")


class TestSyncMrHook(unittest.TestCase):
    def test_never_raises(self):
        with mock.patch.object(sync.db, "connect", side_effect=OSError("disk full")), \
             self.assertLogs("mr_sentinel.history", "ERROR"):
            self.assertFalse(sync.sync_mr({"gitlab_url": "u", "gitlab_token": "t"}, "g/app", 5))


if __name__ == "__main__":
    unittest.main()
