"""Tests for the review-history package (sqlite in a temp dir; GitLab/engine mocked)."""
import json
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
        # 5×1.5 + 2 + 0.5 = 10 findings, +3 follow-up, /5 MRs = 2.6 -> junior (> 2.5)
        self.assertEqual((r["finding_weight"], r["followup_weight"], r["score"]), (10.0, 3.0, 2.6))
        self.assertEqual(r["level"], "junior")
        self.assertEqual(r["appeal_accepted"], 1)
        self.assertEqual(r["aspects"]["uncategorized"]["count"], 1)

    def test_too_few_mrs_has_no_level(self):
        r = score.person_report(self.mrs(4), [], [], CFG, 1, NOW)
        self.assertEqual((r["score"], r["level"]), (0.0, None))

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
        self.assertEqual(row["score"], 6.0)                       # 4 × 7.5 / 5


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
             mock.patch.object(gl, "get_mr_files", return_value=[]):
            sync.sync_project(self.conn, {"gitlab_url": "u", "gitlab_token": "t"}, "g/app", ME)
        # as raw strings "2026-09-01T10:00:00+08:00" would have won
        self.assertEqual(db.get_state(self.conn, "mrs_updated_at:g/app"), "2026-09-01T03:00:00Z")

    def test_mr_is_reread_under_the_lock(self):
        stale, fresh = mr(state="opened", merged=None), mr(state="merged")
        gl = sync.gitlab_client
        with mock.patch.object(gl, "get_mr", return_value=fresh), \
             mock.patch.object(gl, "list_discussions", return_value=[]), \
             mock.patch.object(gl, "get_award_emojis", return_value=[]), \
             mock.patch.object(gl, "get_mr_files", return_value=["a.py"]):
            sync.sync_one(self.conn, "u", "t", "g/app", stale, ME)
        self.assertEqual(self.conn.execute("SELECT state FROM mrs").fetchone()[0], "merged")


class TestSyncMrHook(unittest.TestCase):
    def test_never_raises(self):
        with mock.patch.object(sync.db, "connect", side_effect=OSError("disk full")), \
             self.assertLogs("mr_sentinel.history", "ERROR"):
            self.assertFalse(sync.sync_mr({"gitlab_url": "u", "gitlab_token": "t"}, "g/app", 5))


if __name__ == "__main__":
    unittest.main()
