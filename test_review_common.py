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

    def test_green_pipeline_of_an_older_commit_blocks(self):
        mr = {"sha": "new", "detailed_merge_status": "mergeable",
              "pipeline": {"status": "success", "sha": "old"}}
        self.assertIn("older commit", rc.auto_merge_blocker(mr, reviewed_sha="new"))

    def test_green_pipeline_of_the_head_passes(self):
        mr = {"sha": "new", "detailed_merge_status": "mergeable",
              "head_pipeline": {"status": "success", "sha": "new"}}
        self.assertIsNone(rc.auto_merge_blocker(mr, reviewed_sha="new", has_ci=True))

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

    def test_an_admin_override_with_no_reply_survives(self):
        """Codex P3: deleting it would drop the ruling and raise the finding again."""
        ruling = {"id": 2, "author": {"id": self.ME},
                  "body": f"✅ 冠霖 裁定放行\n\n{rc.APPEAL_ACCEPT}"}
        self.assertEqual(rc.deletable_ai_notes([{"notes": [self.ai_note(1), ruling]}],
                                               self.ME), [])

    def test_a_developer_resolving_without_reply_is_still_deletable(self):
        r = {"resolvable": True, "resolved": True, "resolved_by": {"id": 99}}
        self.assertEqual(rc.deletable_ai_notes([{"notes": [{**self.ai_note(1), **r}]}],
                                               self.ME), [1])

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



ME, DEV = 42, 7
AI_BODY = "🟠 [Medium] x\n\n— 🤖 mr-sentinel AI review (m)"


def note(author, body, **extra):
    return {"author": {"id": author, "name": f"u{author}"}, "body": body, **extra}


def thread(*notes, did="d1"):
    return {"id": did, "notes": list(notes)}


class TestAppeals(unittest.TestCase):
    def status(self, *notes):
        return rc.discussion_status(thread(*notes), ME)

    def test_status_lifecycle(self):
        finding = note(ME, AI_BODY, position={"new_path": "a.py", "new_line": 3})
        reply = note(DEV, "這是刻意的,上游已驗證")
        accept = note(ME, rc.appeal_reply_body("accept", "ok", "m"))
        reject = note(ME, rc.appeal_reply_body("reject", "still broken", "m"))
        self.assertEqual(self.status(finding), "unanswered")
        self.assertEqual(self.status(finding, reply), "appeal")
        self.assertEqual(self.status(finding, reply, accept), "closed")
        self.assertEqual(self.status(finding, reply, reject), "rejected")
        self.assertEqual(self.status(finding, reply, reject, note(DEV, "再看一次")), "appeal")

    def test_accepted_but_unresolved_resolvable_thread_stays_open(self):
        finding = note(ME, AI_BODY, resolvable=True)
        accept = note(ME, rc.appeal_reply_body("accept", "ok", "m"))
        self.assertEqual(self.status(finding, note(DEV, "x"), accept), "accepted")
        from collections import Counter
        self.assertEqual(rc.open_findings(Counter(accepted=1)), 1)

    def test_resolved_thread_is_closed(self):
        """Shape as GitLab returns it (verified live on a merged MR): every note
        of a resolvable thread carries resolvable/resolved/resolved_by."""
        r = {"resolvable": True, "resolved": True, "resolved_by": {"id": ME}}
        accept = note(ME, rc.appeal_reply_body("accept", "ok", "m"), **r)
        self.assertEqual(self.status(note(ME, AI_BODY, **r), note(DEV, "x", **r), accept),
                         "closed")

    def test_an_unjudged_reply_stays_open_whoever_resolved(self):
        """Closing a reply nobody judged is the admin's ✅, which leaves a verdict."""
        r = {"resolvable": True, "resolved": True, "resolved_by": {"id": ME}}
        self.assertEqual(self.status(note(ME, AI_BODY, **r), note(DEV, "x", **r)), "appeal")

    def test_developer_resolving_without_reply_is_still_unanswered(self):
        """!60: the author resolved 7 findings without a word; nobody judged them."""
        r = {"resolvable": True, "resolved": True, "resolved_by": {"id": DEV}}
        self.assertEqual(self.status(note(ME, AI_BODY, **r)), "unanswered")

    def test_developer_resolving_after_a_reply_still_needs_judging(self):
        r = {"resolvable": True, "resolved": True, "resolved_by": {"id": DEV}}
        self.assertEqual(self.status(note(ME, AI_BODY, **r), note(DEV, "先保留", **r)), "appeal")

    def test_developer_resolving_a_rejected_thread_keeps_it_rejected(self):
        r = {"resolvable": True, "resolved": True, "resolved_by": {"id": DEV}}
        reject = note(ME, rc.appeal_reply_body("reject", "still broken", "m"), **r)
        self.assertEqual(self.status(note(ME, AI_BODY, **r), note(DEV, "x", **r), reject),
                         "rejected")

    def test_accepted_thread_resolved_by_anyone_is_closed(self):
        """The verdict is ours already; GitLab keeps the first resolver when we
        resolve a thread a developer had resolved."""
        r = {"resolvable": True, "resolved": True, "resolved_by": {"id": DEV}}
        accept = note(ME, rc.appeal_reply_body("accept", "ok", "m"), **r)
        self.assertEqual(self.status(note(ME, AI_BODY, **r), note(DEV, "x", **r), accept),
                         "closed")

    def test_a_later_reject_beats_an_earlier_resolve_by_us(self):
        """Codex P1: accepted+resolved, developer replies, we reject, the developer
        deletes the reply — every remaining note was resolved by us."""
        r = {"resolvable": True, "resolved": True, "resolved_by": {"id": ME}}
        accept = note(ME, rc.appeal_reply_body("accept", "ok", "m"), **r)
        reject = note(ME, rc.appeal_reply_body("reject", "no", "m"), **r)
        self.assertEqual(self.status(note(ME, AI_BODY, **r), note(DEV, "x", **r), accept, reject),
                         "rejected")

    def test_owner_resolving_by_hand_is_ours(self):
        """The bot runs on the owner's account: indistinguishable, and the owner may."""
        r = {"resolvable": True, "resolved": True, "resolved_by": {"id": ME}}
        self.assertEqual(self.status(note(ME, AI_BODY, **r)), "closed")

    def test_resolver_unknown_is_not_ours(self):
        r = {"resolvable": True, "resolved": True}
        self.assertEqual(self.status(note(ME, AI_BODY, **r)), "unanswered")

    def test_partly_resolved_thread_is_not_closed(self):
        self.assertEqual(self.status(note(ME, AI_BODY, resolvable=True, resolved=True),
                                     note(DEV, "x", resolvable=True, resolved=False)), "appeal")

    def test_a_reject_is_judged_again_once_the_version_moved(self):
        """Fixed as asked without a reply: the judge looks at the new code — or at the
        same commit aimed at another target branch."""
        v1 = rc.mr_version({"sha": "h1", "target_branch": "master"})
        reject = note(ME, rc.appeal_reply_body("reject", "still broken", "m", judged_on=v1))
        threads = [thread(note(ME, AI_BODY), note(DEV, "x"), reject, did="r")]
        self.assertEqual(rc.collect_appeals(threads, ME)[0], [])
        self.assertEqual(rc.collect_appeals(threads, ME, version=v1)[0], [])
        retargeted = rc.mr_version({"sha": "h1", "target_branch": "main"})
        self.assertEqual(len(rc.collect_appeals(threads, ME, version=retargeted)[0]), 1)
        [again], counts = rc.collect_appeals(threads, ME, version="h2:master")
        self.assertEqual((again["id"], again["status"], again["thread"][-1]["role"]),
                         ("r", "rejected", "ai"))
        self.assertEqual(counts["rejected"], 1)

    def test_a_reject_from_before_heads_were_recorded_is_judged_again(self):
        reject = note(ME, rc.appeal_reply_body("reject", "still broken", "m"))
        threads = [thread(note(ME, AI_BODY), note(DEV, "x"), reject, did="r")]
        self.assertEqual(len(rc.collect_appeals(threads, ME, version="h1:master")[0]), 1)

    def test_a_verdict_records_the_version_it_was_given_on(self):
        version = rc.mr_version({"sha": "abc123", "target_branch": "release/1.2"})
        body = rc.appeal_reply_body("accept", "ok", "m", judged_on=version)
        self.assertEqual(rc.judged_on(body), "abc123:release/1.2")
        self.assertIn(rc.APPEAL_ACCEPT, body)
        self.assertIsNone(rc.judged_on(rc.appeal_reply_body("accept", "ok", "m")))

    def test_unresolved_accepts_lists_only_stuck_threads(self):
        accept = rc.appeal_reply_body("accept", "ok", "m")
        stuck = thread(note(ME, AI_BODY, resolvable=True), note(DEV, "x", resolvable=True),
                       note(ME, accept, resolvable=True), did="stuck")
        plain = thread(note(ME, AI_BODY), note(DEV, "x"), note(ME, accept), did="plain")
        self.assertEqual(rc.unresolved_accepts([stuck, plain], ME), ["stuck"])

    def test_non_ai_threads_are_ignored(self):
        self.assertIsNone(self.status(note(DEV, "human review")))
        self.assertIsNone(self.status(note(ME, "🤖 mr-sentinel: auto-merge note")))
        self.assertIsNone(rc.discussion_status({"notes": [{"system": True, "body": "x"}]}, ME))

    def test_collect_appeals_shapes_engine_input(self):
        ds = [thread(note(ME, AI_BODY, position={"new_path": "a.py", "new_line": 3}),
                     note(DEV, "不用修"), did="d1"),
              thread(note(ME, AI_BODY), did="d2")]
        appeals, counts = rc.collect_appeals(ds, ME)
        self.assertEqual(counts, {"appeal": 1, "unanswered": 1})
        (a,) = appeals
        self.assertEqual((a["id"], a["file"], a["line"]), ("d1", "a.py", 3))
        self.assertEqual(a["thread"], [{"role": "developer", "author": "u7", "body": "不用修"}])

    def test_valid_verdicts_drops_malformed(self):
        result = {"verdicts": [
            {"id": "d1", "verdict": "accept", "reason": "ok"},
            {"id": "d2", "verdict": "maybe", "reason": "?"},
            {"id": "d3", "verdict": "reject", "reason": ""},
            {"id": "zz", "verdict": "accept", "reason": "not asked"},
            "junk"]}
        self.assertEqual(rc.valid_verdicts(result, ["d1", "d2", "d3"]), {"d1": ("accept", "ok")})
        self.assertEqual(rc.valid_verdicts(None, ["d1"]), {})

    def test_reply_markers_never_look_like_findings(self):
        """A verdict reply must not carry the finding marker, or a re-review
        cleanup could mistake it for a deletable AI finding."""
        for verdict in ("accept", "reject"):
            self.assertNotIn(rc.SIGNATURE_MARKER, rc.appeal_reply_body(verdict, "r", "m"))

    def test_summary_and_open_count(self):
        from collections import Counter
        judged = {"d1": ("accept", "a"), "d2": ("reject", "b")}
        text = rc.appeal_summary_text("g/app", 5, "http://mr", judged,
                                      Counter(closed=1, rejected=1, unanswered=2))
        self.assertIn("理由成立 1", text)
        self.assertIn("2 則 AI 留言沒人回覆", text)
        self.assertEqual(rc.open_findings(Counter(closed=3)), 0)
        self.assertIn(":tada:", rc.appeal_summary_text("g/app", 5, None, judged, Counter(closed=2)))


class TestSettledAs(unittest.TestCase):
    """How a finding was settled decides whether it may come back: a fix can be undone,
    a reason or a ruling is about the code as it is."""
    AI = "🟠 [Medium] x\n\n" + rc.SIGNATURE_MARKER

    def thread(self, *later):
        return {"id": "d", "notes": [{"id": 1, "author": {"id": 42}, "body": self.AI,
                                      "position": {"new_path": "a.py", "new_line": 3}},
                                     {"id": 2, "author": {"id": 99}, "body": "已修正"}, *later]}

    def test_a_confirmed_fix_is_marked_and_said_so(self):
        body = rc.appeal_reply_body("accept", "a.py:3 已改", "m", fixed=True)
        self.assertIn("已確認修正", body)
        self.assertIn(rc.APPEAL_FIXED, body)
        self.assertNotIn(rc.APPEAL_FIXED, rc.appeal_reply_body("accept", "ok", "m"))

    def test_already_raised_says_how_each_was_settled(self):
        fixed = {"id": 3, "author": {"id": 42},
                 "body": rc.appeal_reply_body("accept", "已改", "m", fixed=True)}
        reason = {"id": 3, "author": {"id": 42}, "body": rc.appeal_reply_body("accept", "ok", "m")}
        self.assertEqual(rc.already_raised([self.thread(fixed)], 42)[0]["settled_as"], "fixed")
        self.assertEqual(rc.already_raised([self.thread(reason)], 42)[0]["settled_as"], "reason")
        self.assertIsNone(rc.already_raised([self.thread()], 42)[0]["settled_as"])

    def test_an_accept_from_before_the_markers_is_unknown(self):
        """Codex P1: it may have been a fix; treated as a reason, an undone fix slips by."""
        legacy = {"id": 3, "author": {"id": 42}, "body": f"✅ 理由成立 {rc.APPEAL_ACCEPT}"}
        self.assertEqual(rc.already_raised([self.thread(legacy)], 42)[0]["settled_as"], "unknown")

    def test_a_fix_or_unknown_settlement_never_hides_the_finding_coming_back(self):
        """Codex P1: the title filter dropped the re-raised finding of an undone fix."""
        fixed = {"id": 3, "author": {"id": 42},
                 "body": rc.appeal_reply_body("accept", "已改", "m", fixed=True)}
        legacy = {"id": 3, "author": {"id": 42}, "body": f"ok {rc.APPEAL_ACCEPT}"}
        reason = {"id": 3, "author": {"id": 42}, "body": rc.appeal_reply_body("accept", "ok", "m")}
        found = [{"file": "a.py", "line": 3, "title": "x"}]
        self.assertEqual(rc.not_yet_raised(found, [self.thread(fixed)], 42), found)
        self.assertEqual(rc.not_yet_raised(found, [self.thread(legacy)], 42), found)
        self.assertEqual(rc.not_yet_raised(found, [self.thread(reason)], 42), [])

    def test_fixed_verdicts_are_read_from_the_engine(self):
        result = {"verdicts": [{"id": "a", "verdict": "accept", "reason": "r", "fixed": True},
                               {"id": "b", "verdict": "accept", "reason": "r"},
                               {"id": "c", "verdict": "reject", "reason": "r", "fixed": True}]}
        self.assertEqual(rc.fixed_verdicts(result, ["a", "b", "c"]), {"a": True})

    def test_label_and_verdict_come_from_the_same_entry(self):
        """Codex P1: a malformed duplicate lent its fixed=false to the valid accept."""
        result = {"verdicts": [{"id": "a", "verdict": "accept", "reason": "", "fixed": False},
                               {"id": "a", "verdict": "accept", "reason": "已修正"}]}
        self.assertEqual(rc.valid_verdicts(result, ["a"]), {"a": ("accept", "已修正")})
        self.assertEqual(rc.fixed_verdicts(result, ["a"]), {})

    def test_an_unlabelled_accept_is_unknown_not_a_reason(self):
        """Codex P1: a missing label read as a reason let an undone fix be skipped."""
        result = {"verdicts": [{"id": "a", "verdict": "accept", "reason": "r"},
                               {"id": "b", "verdict": "accept", "reason": "r", "fixed": "yes"},
                               {"id": "c", "verdict": "accept", "reason": "r", "fixed": False}]}
        kinds = rc.fixed_verdicts(result, ["a", "b", "c"])
        self.assertEqual(kinds, {"c": False})
        unlabelled = {"id": 3, "author": {"id": 42},
                      "body": rc.appeal_reply_body("accept", "r", "m", fixed=kinds.get("a"))}
        self.assertEqual(rc.already_raised([self.thread(unlabelled)], 42)[0]["settled_as"],
                         "unknown")


class TestOpenThreads(unittest.TestCase):
    """What is still open by our rules, so GitLab and Slack can point straight at it."""
    AI = "🟡 **Low** · 測試驗的是死碼\n\n" + rc.SIGNATURE_MARKER

    def test_lists_open_threads_with_what_gitlab_shows(self):
        r = {"resolvable": True, "resolved": True, "resolved_by": {"id": 99}}
        reject = {"id": 3, "author": {"id": 42}, **r,
                  "body": rc.appeal_reply_body("reject", "貼錯串", "m")}
        closed = {"id": "c", "notes": [{"id": 9, "author": {"id": 42}, "body": self.AI, **r,
                                        "resolved_by": {"id": 42}},
                                       {"id": 10, "author": {"id": 42}, **r, "resolved_by": {"id": 42},
                                        "body": rc.appeal_reply_body("accept", "ok", "m")}]}
        rejected = {"id": "r", "notes": [{"id": 1, "author": {"id": 42}, "body": self.AI, **r},
                                         {"id": 2, "author": {"id": 99}, "body": "x", **r}, reject]}
        [t] = rc.open_threads([closed, rejected], 42)
        self.assertEqual((t["id"], t["status"], t["note_id"], t["title"], t["shown_resolved"]),
                         ("r", "rejected", 1, "測試驗的是死碼", True))

    def test_lines_link_each_thread(self):
        t = {"id": "r", "status": "rejected", "note_id": 1, "title": "死碼", "severity": "low",
             "shown_resolved": False}
        text = rc.open_thread_lines([t], "http://gl/mr/60")
        self.assertIn("<http://gl/mr/60#note_1|死碼>", text)
        self.assertIn("申訴被駁回", text)

    def test_a_title_cannot_break_the_link(self):
        t = {"id": "r", "status": "rejected", "note_id": 1, "title": "Array<T> & |x>",
             "severity": "low", "shown_resolved": False}
        self.assertIn("|Array&lt;T&gt; &amp; |x&gt;>", rc.open_thread_lines([t], "http://gl/mr"))

    def test_whole_links_only_within_the_budget(self):
        """A message is cut at 3000 characters; half a link is a broken link."""
        many = [{"id": str(i), "status": "unanswered", "note_id": i, "title": "很長" * 50,
                 "severity": "medium", "shown_resolved": False} for i in range(10)]
        text = rc.open_thread_lines(many, "http://gl/mr/60", budget=400)
        self.assertLessEqual(len(text), 400)
        self.assertEqual(text.count("<"), text.count(">"))
        self.assertIn("…還有", text)

    def test_long_lists_are_cut_with_a_count(self):
        many = [{"id": str(i), "status": "unanswered", "note_id": i, "title": f"t{i}",
                 "severity": "medium", "shown_resolved": False} for i in range(12)]
        text = rc.open_thread_lines(many, "http://gl/mr/60", limit=10)
        self.assertEqual(text.count("#note_"), 10)
        self.assertIn("還有 2 則", text)


class TestNotYetRaised(unittest.TestCase):
    AI = "🟠 [Medium] None 沒處理\n\n" + rc.SIGNATURE_MARKER

    def thread(self, *replies, line=3):
        return {"id": "d", "notes": [
            {"id": 1, "author": {"id": 42}, "body": self.AI,
             "position": {"new_path": "a.py", "new_line": line}},
            *({"id": 2 + i, "author": {"id": 99}, "body": r} for i, r in enumerate(replies))]}

    def test_an_answered_finding_is_not_raised_again_even_on_another_line(self):
        found = [{"file": "a.py", "line": 9, "title": "None 沒處理"},
                 {"file": "b.py", "line": 3, "title": "None 沒處理"}]
        kept = rc.not_yet_raised(found, [self.thread("不用修", line=3)], 42)
        self.assertEqual(kept, [found[1]])

    def test_a_new_problem_on_an_answered_line_is_still_raised(self):
        found = [{"file": "a.py", "line": 3, "title": "越權存取"}]
        self.assertEqual(rc.not_yet_raised(found, [self.thread("不用修")], 42), found)

    def test_a_finding_posted_as_a_plain_note_is_matched_by_its_body(self):
        note = {"id": "d", "notes": [
            {"id": 1, "author": {"id": 42}, "body": "`a.py:3`\n\n" + self.AI},
            {"id": 2, "author": {"id": 99}, "body": "不用修"}]}
        found = [{"file": "a.py", "line": 3, "title": "None 沒處理"}]
        self.assertEqual(rc.not_yet_raised(found, [note], 42), [])

    def test_a_gate_finding_is_only_matched_against_its_own_head(self):
        gated = self.thread("不用修")
        gated["notes"][0]["body"] += "\n" + rc.GATE_HEAD_MARKER.format("h1")
        found = [{"file": "a.py", "line": 3, "title": "None 沒處理"}]
        self.assertEqual(rc.not_yet_raised(found, [gated], 42, head="h1"), [])
        self.assertEqual(rc.not_yet_raised(found, [gated], 42, head="h2"), found)

    def test_an_unanswered_thread_does_not_hide_a_finding(self):
        found = [{"file": "a.py", "line": 3, "title": "x"}]
        self.assertEqual(rc.not_yet_raised(found, [self.thread()], 42), found)


class TestAlreadyRaised(unittest.TestCase):
    """What the review model is told was already discussed, so a reworded copy of
    a settled finding is not raised again (!60: titles differ on every run)."""
    AI = TestNotYetRaised.AI

    def test_answered_threads_go_to_the_model_with_the_whole_conversation(self):
        answered = TestNotYetRaised.thread(TestNotYetRaised(), "後端之後會上線")
        accept = {"id": 9, "author": {"id": 42}, "body": rc.appeal_reply_body("accept", "ok", "m")}
        answered["notes"].append(accept)
        self.assertEqual(rc.already_raised([answered], 42), [{
            "file": "a.py", "line": 3, "status": "closed", "settled_as": "reason",
            "finding": self.AI,
            "replies": [{"role": "developer", "body": "後端之後會上線"},
                        {"role": "ai", "body": accept["body"]}]}])

    def test_unanswered_and_non_ai_threads_are_left_out(self):
        unanswered = TestNotYetRaised.thread(TestNotYetRaised())
        human = {"id": "h", "notes": [{"id": 1, "author": {"id": 99}, "body": "nit"}]}
        self.assertEqual(rc.already_raised([unanswered, human], 42), [])


if __name__ == "__main__":
    unittest.main()


class TestGateHead(unittest.TestCase):
    def test_release_comment_names_its_head_invisibly(self):
        body = rc.format_comment_body({"title": "t", "gate_head": "abc1234"})
        thread = {"notes": [{"body": body}]}
        self.assertEqual(rc.gate_head(thread), "abc1234")
        self.assertIn("<!--", body.splitlines()[-1])

    def test_ordinary_comment_has_no_head(self):
        body = rc.format_comment_body({"title": "t"})
        self.assertIsNone(rc.gate_head({"notes": [{"body": body}]}))


class TestBotOnAPersonsAccount(unittest.TestCase):
    """Regression (!66): the bot runs on its owner's own GitLab account, so the owner's
    replies share the bot's author id. Only the bot's markers tell its notes apart."""
    ME = 14
    AI = "🟠 **Medium** · x\n\n— 🤖 mr-sentinel AI review (m)"

    def thread(self, *extra):
        return {"id": "d1", "notes": [{"id": 1, "author": {"id": self.ME}, "body": self.AI},
                                      *extra]}

    def owner_reply(self):
        return {"id": 2, "author": {"id": self.ME}, "body": "誤判,這行是註解"}

    def test_owners_reply_is_an_appeal(self):
        self.assertEqual(rc.discussion_status(self.thread(self.owner_reply()), self.ME), "appeal")

    def test_owners_reply_is_handed_to_the_judge_as_the_developer(self):
        [appeal], _ = rc.collect_appeals([self.thread(self.owner_reply())], self.ME)
        self.assertEqual([t["role"] for t in appeal["thread"]], ["developer"])

    def test_our_verdict_after_the_owners_reply_is_still_ours(self):
        verdict = {"id": 3, "author": {"id": self.ME}, "body": f"仍建議修 {rc.APPEAL_REJECT}"}
        self.assertEqual(rc.discussion_status(self.thread(self.owner_reply(), verdict), self.ME),
                         "rejected")

    def test_rerun_never_deletes_a_thread_the_owner_answered(self):
        self.assertEqual(rc.deletable_ai_notes([self.thread(self.owner_reply())], self.ME), [])

    def test_rerun_still_deletes_an_unanswered_one(self):
        self.assertEqual(rc.deletable_ai_notes([self.thread()], self.ME), [1])
