"""Dashboard tests. The history db is a real temp file shared by the ORM and the
core (settings TEST NAME); every test starts from an empty, migrated schema."""
import json
from unittest import mock

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.test import TransactionTestCase
from django.urls import reverse

import review_common as rc
from history import sync

from . import services
from .models import Finding, FindingReview, ScoringConfig, SyncRequest

ME = 42
TABLES = ("score_events", "score_state", "email_aliases", "roster_additions", "finding_blame",
          "mr_commits", "person_roles", "finding_reviews", "followup_reviews", "person_evaluations", "followups", "findings", "mr_files", "mrs", "people",
          "sync_requests", "sync_state")


def ai_body(sev="high", title="leak", category="security"):
    return rc.format_comment_body({"severity": sev, "title": title, "problem": "p",
                                   "category": category}, "— 🤖 mr-sentinel AI review (m)")


def mr(i, author=7, created="2026-09-20T00:00:00Z"):
    return {"id": 100 + i, "iid": i, "author": {"id": author, "username": f"pk{author}", "name": "小明"},
            "title": f"[feat] {i}", "state": "merged", "source_branch": "feature/x",
            "target_branch": "dev", "web_url": f"http://gl/mr/{i}", "created_at": created,
            "merged_at": created, "updated_at": created}


class DashboardCase(TransactionTestCase):
    databases = {"default", "history"}

    def setUp(self):
        conn = services.history_conn()               # creates + migrates the test file
        with conn:
            for table in TABLES:
                conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM scoring_configs WHERE version > 1")
            for i in range(5):
                sync.store_mr(conn, "g/app", mr(i), [{"id": f"d{i}", "notes": [{
                    "id": i + 1, "author": {"id": ME}, "body": ai_body(),
                    "created_at": "2026-09-20T00:00:00Z",
                    "position": {"new_path": "a.py", "new_line": 3}}]}], [], ME, ["a.py"])
        conn.close()
        # our pages sit behind admin_view: staff only
        self.user = User.objects.create_superuser("admin", password="pw-for-tests-only")
        self.client.force_login(self.user)


class TestAccess(DashboardCase):
    def test_every_page_needs_login(self):
        self.client.logout()
        for url in (reverse("admin:index"), reverse("person", args=[7]), reverse("scoring")):
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302, url)
            self.assertIn("login", resp["Location"])


class TestPages(DashboardCase):
    def test_non_staff_accounts_cannot_open_pages(self):
        User.objects.create_user("viewer", password="pw-for-tests-only")
        self.client.force_login(User.objects.get(username="viewer"))
        self.assertEqual(self.client.get(reverse("person", args=[7])).status_code, 302)

    def test_root_redirects_to_the_overview(self):
        self.assertEqual(self.client.get("/")["Location"], reverse("admin:index"))

    def test_overview_shows_the_core_score(self):
        resp = self.client.get(reverse("admin:index"))
        self.assertEqual(resp.status_code, 200)
        (row,) = resp.context["ranked"]
        # 5 high security findings, merged unfixed, MRs not rated yet: each
        # min(3, cap 2) − 0.75 -> (9 + 5 × 1.25) / 8 = 1.91; only security assessed
        self.assertEqual((row["username"], row["reviewed_mrs"], row["score"]), ("pk7", 5, 15.2))
        self.assertEqual((row["items"]["security"]["score"], row["coverage"]), (1.91, 1))
        self.assertContains(resp, "小明")

    def test_person_page_and_gitlab_links(self):
        resp = self.client.get(reverse("person", args=[7]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context["shown"]), 5)
        self.assertContains(resp, "http://gl/mr/0#note_1")

    def test_unknown_person_is_404(self):
        self.assertEqual(self.client.get(reverse("person", args=[999])).status_code, 404)


class TestRoles(DashboardCase):
    def test_team_leader_moves_out_of_the_ranking_and_back(self):
        self.client.post(reverse("set_role", args=[7]), {"role": "lead"})
        ctx = self.client.get(reverse("admin:index")).context
        self.assertEqual((len(ctx["ranked"]), len(ctx["leads"])), (0, 1))
        self.assertIsNone(ctx["leads"][0]["level"])
        self.assertContains(self.client.get(reverse("person", args=[7])),
                            '<option value="lead" selected>')
        self.client.post(reverse("set_role", args=[7]), {"role": "member"})
        self.assertEqual(len(self.client.get(reverse("admin:index")).context["ranked"]), 1)
        self.assertEqual(services.PersonRole.objects.count(), 2)        # both kept as history

    def test_unknown_role_is_refused(self):
        self.client.post(reverse("set_role", args=[7]), {"role": "boss"})
        self.assertFalse(services.PersonRole.objects.exists())


class TestMembersAndLog(DashboardCase):
    def test_overview_rows_expand_into_mr_records(self):
        ctx = self.client.get(reverse("admin:index")).context
        (row,) = ctx["ranked"]
        self.assertEqual(len(row["records"]), 5)
        rec = row["records"][0]
        self.assertEqual((rec["kind"], rec["total"], rec["counts"]["high"]), ("個人", 1, 1))
        self.assertContains(self.client.get(reverse("admin:index")), 'class="ms-acc"')

    def test_team_leader_block_comes_before_members(self):
        self.client.post(reverse("set_role", args=[7]), {"role": "lead"})
        html = self.client.get(reverse("admin:index")).content.decode()
        self.assertLess(html.index("<h2>Team leader</h2>"), html.index("<h2>成員</h2>"))

    def test_departed_member_is_listed_apart_and_unranked(self):
        self.client.post(reverse("member_role", args=[7]), {"role": "departed"})
        ctx = self.client.get(reverse("admin:index")).context
        self.assertEqual((len(ctx["ranked"]), len(ctx["departed"])), (0, 1))

    def test_every_human_change_lands_in_the_score_log(self):
        services.record("baseline", "system")
        self.client.post(reverse("review_finding", args=[1]), {"action": "exclude", "reason": "誤判"})
        event = services.ScoreEvent.objects.filter(gitlab_id=7).first()
        self.assertEqual((event.old_findings, event.new_findings, event.actor), (5, 4, "admin"))
        self.assertIn("finding #1", event.trigger)
        self.assertContains(self.client.get(reverse("score_log")), "finding #1")

    def test_bulk_roles_store_only_changes_and_log_once(self):
        before = services.ScoreEvent.objects.count()
        self.client.post(reverse("members"), {"action": "roles", "role_7": "external"})
        self.assertEqual(services.PersonRole.objects.get().role, "external")
        self.client.post(reverse("members"), {"action": "roles", "role_7": "external"})   # no change
        self.assertEqual(services.PersonRole.objects.count(), 1)
        self.client.post(reverse("members"), {"action": "roles", "role_7": "boss"})       # refused
        self.assertEqual(services.PersonRole.objects.count(), 1)
        row = next(r for r in services.team()[2] if r["author_id"] == 7)
        self.assertEqual((row["role"], row["ranked"]), ("external", False))

    def test_add_member_queues_and_starts_a_sync(self):
        with mock.patch.object(services.subprocess, "Popen") as popen:
            self.client.post(reverse("members"), {"username": "@newbie"})
        self.assertEqual(services.RosterAddition.objects.get().username, "newbie")
        popen.assert_called_once()
        self.client.post(reverse("members"), {"username": "pk7"})      # already known
        self.assertEqual(services.RosterAddition.objects.count(), 1)
        self.client.post(reverse("members"), {"username": "a b; drop"})
        self.assertEqual(services.RosterAddition.objects.count(), 1)

    def test_confirming_an_email_moves_findings_and_logs_it(self):
        conn = services.history_conn()
        with conn:
            conn.execute("INSERT INTO people VALUES (8, 'pk8', '小華')")
            conn.execute("INSERT INTO finding_blame VALUES (1, 'c', 'hua@x', '華', NULL, 'now')")
        conn.close()
        page = self.client.get(reverse("emails"))
        self.assertEqual(page.context["unknown"][0]["email"], "hua@x")
        services.record("baseline", "system")
        self.client.post(reverse("emails"), {"email": "hua@x", "person": "8"})
        self.assertEqual(self.client.get(reverse("emails")).context["unknown"], [])
        self.assertTrue(services.ScoreEvent.objects.filter(trigger__contains="hua@x").exists())


class TestRadar(DashboardCase):
    def test_person_radar_has_two_datasets_and_eight_axes(self):
        radar = services.person_detail(7)["radar"]
        data = json.loads(radar["data"])
        self.assertEqual(len(data["labels"]), 8)
        self.assertEqual(json.loads(radar["options"])["scales"]["r"]["max"], 5)
        self.assertEqual([d["label"] for d in data["datasets"]], ["小明", "團隊平均"])
        self.assertEqual(json.loads(radar["options"])["scales"]["r"]["pointLabels"]["font"]["size"], 15)

    def test_radar_canvas_is_rendered_for_unfold(self):
        resp = self.client.get(reverse("person", args=[7]))
        self.assertContains(resp, 'class="chart" data-type="radar"')


class TestReview(DashboardCase):
    def post(self, note_id, **data):
        return self.client.post(reverse("review_finding", args=[note_id]), data)

    def test_exclude_needs_a_reason_and_then_stops_counting(self):
        self.post(1, action="exclude")
        self.assertFalse(FindingReview.objects.exists())
        self.post(1, action="exclude", reason="其實有驗證")
        review = FindingReview.objects.get()
        self.assertEqual((review.excluded, review.actor, review.reason), (1, "admin", "其實有驗證"))
        (row,) = services.team()[2]
        self.assertEqual((row["findings"], row["excluded"]), (4, 1))

    def test_category_change_is_recorded_not_applied_to_the_raw_row(self):
        self.post(1, action="category", category="correctness")
        self.assertEqual(Finding.objects.get(note_id=1).category, "security")    # raw untouched
        detail = services.person_detail(7)
        changed = next(f for f in detail["findings"] if f["note_id"] == 1)
        self.assertEqual(changed["category"], "correctness")

    def test_unknown_category_is_refused(self):
        self.post(1, action="category", category="nonsense")
        self.assertFalse(FindingReview.objects.exists())

    def test_redirect_never_leaves_the_site(self):
        resp = self.post(1, action="category", category="correctness", next="https://evil.example/")
        self.assertTrue(resp["Location"].startswith(reverse("person", args=[7])))


class TestAppendOnly(DashboardCase):
    def test_raw_rows_cannot_be_saved_and_human_rows_cannot_be_edited(self):
        with self.assertRaises(PermissionDenied):
            Finding.objects.get(note_id=1).save()
        review = services.review_finding(1, "admin", excluded=True, reason="x")
        review.reason = "rewritten history"
        with self.assertRaises(PermissionDenied):
            review.save()
        with self.assertRaises(PermissionDenied):
            review.delete()


class TestScoring(DashboardCase):
    def default(self):
        return json.loads(ScoringConfig.objects.get(version=1).config)

    def test_valid_change_creates_the_next_version_and_takes_effect(self):
        cfg = self.default()
        cfg["prior_strength"] = 0                    # no shrink: the raw grades
        resp = self.client.post(reverse("scoring"), {"config": json.dumps(cfg), "note": "test"})
        self.assertEqual(resp.status_code, 302)
        latest = ScoringConfig.objects.order_by("-version").first()
        self.assertEqual((latest.version, latest.actor), (2, "admin"))
        version, _, (row,) = services.team()
        self.assertEqual(row["items"]["security"]["score"], 1.25)   # cap 2 − 0.75 escaped
        self.assertEqual((version, row["score"]), (2, 10.0))

    def test_invalid_configs_are_rejected(self):
        bad = []
        cfg = self.default(); cfg["levels"][-1]["min_score"] = 1; bad.append(cfg)
        cfg = self.default(); cfg["levels"][0]["min_score"] = 5; bad.append(cfg)  # not decreasing
        cfg = self.default(); cfg["levels"][0]["min_score"] = 41; bad.append(cfg)  # over 40
        cfg = self.default(); cfg["levels"][0]["max_score"] = 1; bad.append(cfg)   # old key
        cfg = self.default(); cfg["levels"][0]["min_score"] = "low"; bad.append(cfg)
        cfg = self.default(); cfg["levels"][0]["max_high"] = -1; bad.append(cfg)
        cfg = self.default(); cfg["levels"][0]["min_coverage"] = 9; bad.append(cfg)
        cfg = self.default(); cfg["levels"][0]["min_clean_rate"] = 90; bad.append(cfg)  # not 0.9
        cfg = self.default(); cfg["levels"][0]["max_hihg"] = 0; bad.append(cfg)       # typo
        cfg = self.default(); cfg["levels"][1]["level"] = cfg["levels"][0]["level"]; bad.append(cfg)
        cfg = self.default(); del cfg["window_days"]; bad.append(cfg)
        cfg = self.default(); cfg["finding_cap"] = {"high": 2}; bad.append(cfg)       # incomplete
        cfg = self.default(); cfg["escape_increment"]["high"] = -1; bad.append(cfg)
        cfg = self.default(); cfg["prior_score"] = 9; bad.append(cfg)
        cfg = self.default(); cfg["required_items"] = ["code_smell"]; bad.append(cfg)
        cfg = self.default(); cfg["severity_weight"] = {}; bad.append(cfg)          # retired key
        for cfg in bad:
            self.assertTrue(services.validate_scoring(cfg), cfg)
        self.assertEqual(services.validate_scoring(self.default()), [])
        self.client.post(reverse("scoring"), {"config": "{not json"})
        self.assertEqual(ScoringConfig.objects.count(), 1)


class TestTrend(DashboardCase):
    def test_monthly_trend_matches_the_score_formula(self):
        conn = services.history_conn()
        with conn:
            conn.execute("INSERT INTO followups VALUES (100, 'fix_mr', '999', 'a.py', 3)")
        conn.close()
        detail = services.person_detail(7)
        (month,) = detail["trend"]
        # 5 security grades of 1.25; an unconfirmed fix MR adds no grade
        self.assertEqual((month["mrs"], month["grades"], month["avg"]), (5, 5, 1.25))


class TestFollowupReview(DashboardCase):
    def test_confirm_then_unrelated_changes_the_weight_and_logs(self):
        conn = services.history_conn()
        with conn:
            conn.execute("INSERT INTO followups VALUES (100, 'fix_mr', '999', 'a.py', 3)")
        conn.close()
        services.record("baseline", "system")
        (fu,) = services.person_detail(7)["followups"]
        self.assertEqual((fu["verdict"], fu["increment"], fu["counts_under"]), (None, 0, "正確性"))
        url = reverse("review_followup", args=[7])
        key = {"feature_mr_id": 100, "kind": "fix_mr", "source_ref": "999"}
        self.client.post(url, {**key, "verdict": "confirmed"})
        (fu,) = services.person_detail(7)["followups"]
        self.assertEqual((fu["verdict"], fu["increment"]), ("confirmed", 0.75))
        self.assertEqual(services.person_detail(7)["summary"]["confirmed_followups"], 1)
        self.client.post(url, {**key, "verdict": "unrelated"})
        detail = services.person_detail(7)
        self.assertEqual(detail["followups"][0]["increment"], 0)
        self.assertEqual(detail["summary"]["followups"]["fix_mr"], 0)
        self.assertIn("後續 bug 覆核", services.ScoreEvent.objects.first().trigger)
        # a made-up follow-up or verdict is refused
        self.client.post(url, {**key, "source_ref": "1", "verdict": "confirmed"})
        self.client.post(url, {**key, "verdict": "maybe"})
        self.assertEqual(services.FollowupReview.objects.count(), 2)


class TestEvaluation(DashboardCase):
    def test_person_page_and_overview_show_the_evaluation(self):
        conn = services.history_conn()
        with conn:
            conn.execute("""INSERT INTO person_evaluations(gitlab_id, created_at, formula_version,
                            input_hash, summary, strengths, weaknesses) VALUES
                            (7, '2026-10-09T00:00:00Z', 1, 'old', '整體評語',
                             '[{"point": "優點一", "evidence": "證據"}]',
                             '[{"point": "缺點一", "evidence": "e", "advice": "改這個"}]')""")
        conn.close()
        resp = self.client.get(reverse("person", args=[7]))
        for text in ("整體評語", "優點一", "缺點一", "改這個", "紀錄已變動"):   # hash 'old' is stale
            self.assertContains(resp, text)
        self.assertContains(self.client.get(reverse("admin:index")), "缺點一")

    def test_regenerate_button_queues_and_comes_back(self):
        with mock.patch.object(services.subprocess, "Popen"):
            resp = self.client.post(reverse("sync_request"),
                                    {"kind": "evaluate", "next": reverse("person", args=[7])})
        self.assertEqual(resp["Location"], reverse("person", args=[7]))
        self.assertEqual(services.SyncRequest.objects.get().kind, "evaluate")


class TestCrossAuthorFollowup(DashboardCase):
    def test_refind_on_someone_elses_excluded_finding_does_not_count(self):
        conn = services.history_conn()
        with conn:          # another author's MR with a finding that re-hits pk7's file
            sync.store_mr(conn, "g/app", mr(50, author=8), [{"id": "x", "notes": [{
                "id": 500, "author": {"id": ME}, "body": ai_body(), "created_at": "2026-09-21T00:00:00Z"}]}],
                [], ME, ["a.py"])
            conn.execute("INSERT INTO followups VALUES (100, 'ai_refind', '500', 'a.py', 1)")
        conn.close()
        services.review_finding(500, "admin", excluded=True, reason="誤判")
        detail = services.person_detail(7)
        self.assertEqual(detail["followups"], [])
        self.assertEqual(detail["summary"]["followups"]["ai_refind"], 0)    # agrees with the score


class TestPaths(DashboardCase):
    def test_relative_db_paths_are_relative_to_the_repo(self):
        from django.conf import settings
        from history import db as hdb
        with mock.patch.dict("os.environ", {"MR_SENTINEL_DB": "data/x.db"}):
            self.assertEqual(hdb.resolve_path(), hdb.SCRIPT_DIR / "data/x.db")
        self.assertTrue(services.history_path().is_absolute())
        self.assertEqual(hdb.SCRIPT_DIR, settings.REPO_ROOT)


class TestSync(DashboardCase):
    def test_request_is_queued_and_the_core_job_started_detached(self):
        with mock.patch.object(services.subprocess, "Popen") as popen:
            self.client.post(reverse("sync_request"), {"kind": "full_sync"})
        req = SyncRequest.objects.get()
        self.assertEqual((req.kind, req.requested_by, req.finished_at), ("full_sync", "admin", None))
        args, kwargs = popen.call_args
        self.assertEqual(args[0][1:], ["-m", "history", "run"])
        self.assertTrue(kwargs["start_new_session"])

    def test_job_gets_the_dashboard_db_path(self):
        with mock.patch.object(services.subprocess, "Popen") as popen:
            services.request_sync("sync", "admin")
        self.assertEqual(popen.call_args.kwargs["env"]["MR_SENTINEL_DB"], str(services.history_path()))

    def test_failed_start_closes_the_request_and_tells_the_user(self):
        with mock.patch.object(services.subprocess, "Popen", side_effect=OSError("no python")):
            resp = self.client.post(reverse("sync_request"), {"kind": "sync"}, follow=True)
        req = SyncRequest.objects.get()
        self.assertIsNotNone(req.finished_at)
        self.assertIn("no python", req.result)
        self.assertContains(resp, "背景同步啟動失敗")

    def test_unknown_kind_is_refused(self):
        with mock.patch.object(services.subprocess, "Popen") as popen:
            self.client.post(reverse("sync_request"), {"kind": "drop_tables"})
        self.assertFalse(SyncRequest.objects.exists())
        popen.assert_not_called()


class TestRouter(DashboardCase):
    def test_django_never_migrates_the_history_db(self):
        from sentinel_dashboard.routers import HistoryRouter
        router = HistoryRouter()
        self.assertFalse(router.allow_migrate("history", "auth"))
        self.assertFalse(router.allow_migrate("default", "reviews"))
        self.assertIsNone(router.allow_migrate("default", "auth"))
