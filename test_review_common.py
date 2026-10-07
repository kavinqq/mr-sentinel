"""Unit tests for review_common pure functions."""
import unittest

import review_common as rc

CFG = {
    "project_map": {
        "group/backend-app": "/home/me/backend-app",
        "group/frontend-app": "/home/me/frontend-app",
    },
    "max_changed_files": 60,
    "max_diff_lines": 3000,
}


class TestTargeting(unittest.TestCase):
    def test_is_target_true(self):
        self.assertTrue(rc.is_review_target("group/backend-app", CFG))

    def test_is_target_false(self):
        self.assertFalse(rc.is_review_target("group/other-app", CFG))

    def test_resolve_path(self):
        self.assertEqual(
            rc.resolve_local_path("group/frontend-app", CFG), "/home/me/frontend-app"
        )

    def test_resolve_path_missing(self):
        self.assertIsNone(rc.resolve_local_path("group/other-app", CFG))


class TestNoiseAndSize(unittest.TestCase):
    def test_is_noise_lockfile(self):
        self.assertTrue(rc.is_noise_path("frontend/package-lock.json"))
        self.assertTrue(rc.is_noise_path("poetry.lock"))

    def test_is_noise_dir(self):
        self.assertTrue(rc.is_noise_path("app/node_modules/x/y.js"))
        self.assertTrue(rc.is_noise_path("static/dist/main.js"))

    def test_is_noise_minified_and_maps(self):
        self.assertTrue(rc.is_noise_path("assets/app.min.js"))
        self.assertTrue(rc.is_noise_path("assets/app.js.map"))

    def test_is_noise_normal_false(self):
        self.assertFalse(rc.is_noise_path("backend/home/views.py"))

    def test_filter_drops_noise(self):
        changes = [
            {"new_path": "backend/home/views.py", "diff": "+a\n-b\n"},
            {"new_path": "package-lock.json", "diff": "+x\n"},
        ]
        out = rc.filter_noise_changes(changes)
        self.assertEqual([c["new_path"] for c in out], ["backend/home/views.py"])

    def test_diff_stats_counts_plus_minus_only(self):
        changes = [
            {"new_path": "a.py", "diff": "@@ -1 +1,2 @@\n+added\n-removed\n context\n+++ b/a.py\n"}
        ]
        files, lines = rc.diff_stats(changes)
        self.assertEqual(files, 1)
        self.assertEqual(lines, 2)  # +added / -removed only; headers and context excluded

    def test_plan_small_is_lite(self):
        # small MR -> a single scan pass (no adversarial vetting)
        self.assertEqual(rc.plan_review(3, 100, CFG), ("lite", ""))

    def test_plan_deep_when_files_exceed(self):
        mode, reason = rc.plan_review(61, 10, CFG)
        self.assertEqual(mode, "deep")  # large MR -> 3-gate escalation
        self.assertIn("61", reason)

    def test_plan_deep_when_lines_exceed(self):
        mode, reason = rc.plan_review(1, 3001, CFG)
        self.assertEqual(mode, "deep")
        self.assertIn("3001", reason)


class TestAutoMergeBlocker(unittest.TestCase):
    OK_MR = {"detailed_merge_status": "mergeable",
             "head_pipeline": {"status": "success"}}

    def test_clean_and_green_has_no_blocker(self):
        self.assertIsNone(rc.auto_merge_blocker(self.OK_MR))

    def test_draft_blocks(self):
        self.assertIn("draft", rc.auto_merge_blocker({**self.OK_MR, "draft": True}))

    def test_work_in_progress_blocks(self):
        self.assertIn("draft", rc.auto_merge_blocker({**self.OK_MR, "work_in_progress": True}))

    def test_not_mergeable_blocks(self):
        mr = {"detailed_merge_status": "conflict", "head_pipeline": {"status": "success"}}
        self.assertIn("mergeable", rc.auto_merge_blocker(mr))

    def test_unchecked_merge_status_blocks(self):
        # GitLab hasn't computed mergeability yet -> refuse (conservative)
        mr = {"merge_status": "unchecked", "head_pipeline": {"status": "success"}}
        self.assertIsNotNone(rc.auto_merge_blocker(mr))

    def test_red_pipeline_blocks(self):
        mr = {"detailed_merge_status": "mergeable", "head_pipeline": {"status": "failed"}}
        self.assertIn("pipeline", rc.auto_merge_blocker(mr))

    def test_running_pipeline_blocks(self):
        mr = {"detailed_merge_status": "mergeable", "head_pipeline": {"status": "running"}}
        self.assertIn("pipeline", rc.auto_merge_blocker(mr))

    def test_no_pipeline_falls_back_to_merge_status(self):
        # a project without CI: nothing to gate on, mergeability alone decides
        self.assertIsNone(rc.auto_merge_blocker({"merge_status": "can_be_merged"}))

    def test_missing_pipeline_blocks_when_project_has_ci(self):
        self.assertIn("pipeline", rc.auto_merge_blocker({"merge_status": "can_be_merged"},
                                                        has_ci=True))

    def test_green_pipeline_passes_when_project_has_ci(self):
        self.assertIsNone(rc.auto_merge_blocker(self.OK_MR, has_ci=True))

    def test_new_commits_since_review_block(self):
        mr = {**self.OK_MR, "sha": "new"}
        self.assertIn("new commits", rc.auto_merge_blocker(mr, reviewed_sha="old"))

    def test_reviewed_head_passes(self):
        self.assertIsNone(rc.auto_merge_blocker({**self.OK_MR, "sha": "h"}, reviewed_sha="h"))

    def test_legacy_merge_status_accepted(self):
        mr = {"merge_status": "can_be_merged", "pipeline": {"status": "success"}}
        self.assertIsNone(rc.auto_merge_blocker(mr))


class TestIsCleanResult(unittest.TestCase):
    def test_empty_findings_list_is_clean(self):
        self.assertTrue(rc.is_clean_result({"findings": []}))

    def test_any_finding_is_not_clean(self):
        self.assertFalse(rc.is_clean_result({"findings": [{"severity": "low"}]}))

    def test_missing_findings_key_is_not_clean(self):
        self.assertFalse(rc.is_clean_result({}))

    def test_non_list_findings_is_not_clean(self):
        self.assertFalse(rc.is_clean_result({"findings": None}))


class TestAutoMergeEligible(unittest.TestCase):
    def test_clean_deep_is_eligible(self):
        self.assertTrue(rc.auto_merge_eligible("deep", {"findings": []}))

    def test_clean_lite_is_not_eligible(self):
        self.assertFalse(rc.auto_merge_eligible("lite", {"findings": []}))

    def test_deep_with_findings_is_not_eligible(self):
        self.assertFalse(rc.auto_merge_eligible("deep", {"findings": [{"severity": "low"}]}))


class TestProjectPathAndScope(unittest.TestCase):
    def test_project_path_from_mr(self):
        mr = {"web_url": "https://gl.example.com/group/sub/repo/-/merge_requests/7"}
        self.assertEqual(rc.project_path_from_mr(mr, "https://gl.example.com"), "group/sub/repo")

    def test_is_in_scope_empty_prefixes_accepts_all(self):
        self.assertTrue(rc.is_in_scope("any/project", []))

    def test_is_in_scope_prefix_match(self):
        self.assertTrue(rc.is_in_scope("group/backend/app", ["group/backend/", "group/frontend/"]))
        self.assertFalse(rc.is_in_scope("other/app", ["group/backend/"]))


DIFF_REFS = {"base_sha": "b", "start_sha": "s", "head_sha": "h"}


class TestFindingFormat(unittest.TestCase):
    def test_sort_high_first(self):
        fs = [{"severity": "low"}, {"severity": "high"}, {"severity": "medium"}]
        self.assertEqual([f["severity"] for f in rc.sort_findings(fs)], ["high", "medium", "low"])

    def test_sort_stable_unknown_last(self):
        fs = [{"severity": "weird"}, {"severity": "high"}]
        self.assertEqual([f["severity"] for f in rc.sort_findings(fs)], ["high", "weird"])

    def test_format_body_emoji_label_and_signature(self):
        body = rc.format_comment_body(
            {"severity": "high", "title": "SQL injection", "file": "a.py", "line": 10,
             "body": "User input is concatenated into the query.\nFix: use parameterized queries."},
            signature="🤖 mr-sentinel (scanned by opus, vetted by sonnet)")
        self.assertIn("🔴", body)
        self.assertIn("High", body)
        self.assertIn("SQL injection", body)
        self.assertIn("parameterized", body)
        self.assertIn("mr-sentinel", body)

    def test_format_body_default_signature(self):
        body = rc.format_comment_body(
            {"severity": "low", "title": "t", "file": "a.py", "line": 1, "body": "b"})
        self.assertIn("🟡", body)
        self.assertIn("mr-sentinel", body)


class TestStructuredCommentBody(unittest.TestCase):
    FINDING = {
        "severity": "medium", "title": "ES 查詢失敗被當成命中 0 人",
        "file": "audience.py", "line": 245,
        "problem": "`get_existing_ino_list` 重試全敗後 `return []` 不拋例外。",
        "impact": "ES 逾時 → 有效名單被回「沒有任何有效會員」擋下上架。",
        "fix": "重試全敗時 re-raise,讓外層 except 接住回 None。",
        "evidence": "DATE 路徑走 `dsl.get_count()` 會拋例外,兩條路徑不對稱。",
    }

    def test_the_three_lines_are_labelled_and_in_order(self):
        body = rc.format_comment_body(self.FINDING)
        self.assertLess(body.index("問題"), body.index("後果"))
        self.assertLess(body.index("後果"), body.index("修正"))

    def test_the_fix_is_visible_without_expanding_anything(self):
        body = rc.format_comment_body(self.FINDING)
        head = body.split("<details>")[0]
        self.assertIn("re-raise", head)          # the whole point of the redesign

    def test_each_labelled_line_is_its_own_paragraph(self):
        # GitLab treats a single newline as a space, merging the three into one blob
        head = rc.format_comment_body(self.FINDING).split("<details>")[0]
        self.assertIn("\n\n**後果**", head)
        self.assertIn("\n\n**修正**", head)

    def test_evidence_is_collapsed(self):
        body = rc.format_comment_body(self.FINDING)
        self.assertIn("<details>", body)
        self.assertIn(rc.EVIDENCE_SUMMARY, body)
        self.assertIn("不對稱", body.split("<details>")[1])

    def test_markdown_inside_details_gets_its_blank_lines(self):
        # without them GitLab renders the evidence as one literal blob
        block = rc.format_comment_body(self.FINDING).split("<details>")[1]
        self.assertTrue(block.startswith(f"\n<summary>{rc.EVIDENCE_SUMMARY}</summary>\n\n"))
        self.assertIn("\n\n</details>", block)

    def test_no_evidence_means_no_details_block(self):
        body = rc.format_comment_body({k: v for k, v in self.FINDING.items()
                                       if k != "evidence"})
        self.assertNotIn("<details>", body)
        self.assertIn("re-raise", body)

    def test_partial_fields_render_only_what_exists(self):
        body = rc.format_comment_body({"severity": "high", "title": "t",
                                       "problem": "壞了"})
        self.assertIn("問題", body)
        self.assertNotIn("後果", body)
        self.assertNotIn("修正", body)

    def test_legacy_body_only_findings_still_render(self):
        # findings files written before the split must remain re-postable
        body = rc.format_comment_body({"severity": "low", "title": "t",
                                       "body": "舊格式的一大段散文"})
        self.assertIn("舊格式的一大段散文", body)
        self.assertNotIn("問題", body)

    def test_structured_fields_win_over_a_stale_body(self):
        body = rc.format_comment_body({**self.FINDING, "body": "不該出現的舊文字"})
        self.assertNotIn("不該出現的舊文字", body)

    def test_signature_is_last(self):
        body = rc.format_comment_body(self.FINDING, signature="— SIG")
        self.assertTrue(body.rstrip().endswith("— SIG"))

    def test_location_is_not_repeated_in_the_body(self):
        # inline comments are already attached to the line; the note fallback
        # prepends it separately (post_comment._note_prefix)
        body = rc.format_comment_body(self.FINDING)
        self.assertNotIn("audience.py", body)


class TestPositionEmojiTs(unittest.TestCase):
    def test_build_position_inline(self):
        pos = rc.build_position({"file": "a.py", "line": 12}, DIFF_REFS)
        self.assertEqual(pos["new_path"], "a.py")
        # GitLab requires old_path too for position_type=text; omitting it 400s
        # every inline discussion and silently degrades to plain notes
        self.assertEqual(pos["old_path"], "a.py")
        self.assertEqual(pos["new_line"], 12)
        self.assertEqual(pos["position_type"], "text")
        self.assertEqual(pos["head_sha"], "h")

    def test_build_position_uses_explicit_old_path_for_renames(self):
        pos = rc.build_position({"file": "new.py", "old_path": "old.py", "line": 5}, DIFF_REFS)
        self.assertEqual(pos["new_path"], "new.py")
        self.assertEqual(pos["old_path"], "old.py")

    def test_build_position_none_when_no_line(self):
        self.assertIsNone(rc.build_position({"file": "a.py", "line": None}, DIFF_REFS))

    def test_position_form_flattens(self):
        form = rc.position_form({"new_path": "a.py", "new_line": 12, "position_type": "text"})
        self.assertEqual(form["position[new_path]"], "a.py")
        self.assertEqual(form["position[new_line]"], 12)

    def test_has_own_emoji_true(self):
        emojis = [{"name": "thumbsup", "user": {"id": 5}}, {"name": "eyes", "user": {"id": 9}}]
        self.assertTrue(rc.has_own_award_emoji(emojis, 9))

    def test_has_own_emoji_false_other_user(self):
        emojis = [{"name": "eyes", "user": {"id": 5}}]
        self.assertFalse(rc.has_own_award_emoji(emojis, 9))

    def test_slack_ts_for(self):
        state = {"slack_ts": {"2451": "1700000000.001"}}
        self.assertEqual(rc.slack_ts_for(state, 2451), "1700000000.001")
        self.assertIsNone(rc.slack_ts_for(state, 9999))


class TestDeletableAiNotes(unittest.TestCase):
    """Re-review cleanup: delete our own noise, never anyone's conversation."""
    ME = 42

    def ai_note(self, note_id, author=ME):
        return {"id": note_id, "author": {"id": author},
                "body": f"🔴 [High] x\n\n{rc.DEFAULT_SIGNATURE} (model)"}

    def test_our_unanswered_comment_is_deletable(self):
        discussions = [{"notes": [self.ai_note(1)]}]
        self.assertEqual(rc.deletable_ai_notes(discussions, self.ME), [1])

    def test_a_discussion_a_human_joined_is_left_completely_alone(self):
        discussions = [{"notes": [self.ai_note(1),
                                  {"id": 2, "author": {"id": 99}, "body": "其實沒問題"}]}]
        self.assertEqual(rc.deletable_ai_notes(discussions, self.ME), [])

    def test_our_own_unsigned_notes_survive(self):
        # e.g. the auto-merge explanation note, or anything hand-written
        discussions = [{"notes": [{"id": 1, "author": {"id": self.ME},
                                   "body": "🤖 mr-sentinel: 未自動合併"}]}]
        self.assertEqual(rc.deletable_ai_notes(discussions, self.ME), [])

    def test_someone_elses_signed_looking_note_is_not_ours_to_delete(self):
        discussions = [{"notes": [self.ai_note(1, author=99)]}]
        self.assertEqual(rc.deletable_ai_notes(discussions, self.ME), [])

    def test_system_notes_are_ignored_when_judging_authorship(self):
        # "changed the description" system notes are authored by the actor, and
        # would otherwise make every discussion look like a conversation
        discussions = [{"notes": [self.ai_note(1),
                                  {"id": 2, "author": {"id": 99}, "body": "changed",
                                   "system": True}]}]
        self.assertEqual(rc.deletable_ai_notes(discussions, self.ME), [1])

    def test_empty_and_noteless_discussions(self):
        self.assertEqual(rc.deletable_ai_notes([], self.ME), [])
        self.assertEqual(rc.deletable_ai_notes([{"notes": []}, {}], self.ME), [])


class TestVerdict(unittest.TestCase):
    def test_tier_follows_worst_severity(self):
        self.assertEqual(rc.verdict_tier([]), "clean")
        self.assertEqual(rc.verdict_tier([{"severity": "low"}]), "minor")
        self.assertEqual(rc.verdict_tier([{"severity": "low"}, {"severity": "medium"}]), "moderate")
        self.assertEqual(rc.verdict_tier([{"severity": "medium"}, {"severity": "high"}]), "major")

    def test_unknown_severity_counts_as_minor(self):
        self.assertEqual(rc.verdict_tier([{"severity": "weird"}, {}]), "minor")

    def test_every_tier_has_a_folder(self):
        self.assertEqual(set(rc.VERDICT_IMAGE_DIR), {"clean", "minor", "moderate", "major"})

    def test_picks_any_image_name_skipping_non_images(self):
        names = ["cat.JPG", "funny meme.png", "x.gif", "notes.txt", ".DS_Store"]
        self.assertEqual(rc.pick_image(names, choice=lambda xs: xs),
                         ["cat.JPG", "funny meme.png", "x.gif"])

    def test_none_when_no_candidates(self):
        self.assertIsNone(rc.pick_image([".DS_Store", "readme.md"]))


if __name__ == "__main__":
    unittest.main()
