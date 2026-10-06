"""Tests for the Slack-settable override layer (whitelist, validation, merge)."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import overrides


def base_config() -> dict:
    return {
        "gitlab_url": "https://git.example.com",
        "gitlab_token": "secret",
        "review": {
            "language": "en",
            "engine": "claude",
            "max_changed_files": 60,
            "max_diff_lines": 3000,
            "review_timeout_seconds": 900,
            "project_map": {"g/a": "/local/a", "g/b": "/local/b"},
            "claude": {"model": "claude-opus-4-8", "skeptic_model": "sonnet", "effort": "medium"},
            "codex": {"model": "", "skeptic_model": ""},
        },
    }


class TestSetValue(unittest.TestCase):
    def test_valid_choice_is_stored(self):
        ov, msg, err = overrides.set_value({}, "effort", "high", by="U1")
        self.assertIsNone(err)
        self.assertEqual(ov["set"]["effort"], "high")
        self.assertIn("high", msg)

    def test_invalid_choice_is_rejected_with_options_listed(self):
        ov, msg, err = overrides.set_value({}, "effort", "turbo", by="U1")
        self.assertIsNotNone(err)
        self.assertIn("medium", err)          # tells the user what IS allowed
        self.assertEqual(ov, {})              # nothing written on rejection

    def test_unknown_key_is_rejected(self):
        _, _, err = overrides.set_value({}, "gitlab_token", "haha", by="U1")
        self.assertIsNotNone(err)

    def test_ints_are_coerced_and_range_checked(self):
        ov, _, err = overrides.set_value({}, "maxfiles", "80", by="U1")
        self.assertIsNone(err)
        self.assertEqual(ov["set"]["maxfiles"], 80)
        _, _, err = overrides.set_value({}, "maxfiles", "99999", by="U1")
        self.assertIsNotNone(err)
        _, _, err = overrides.set_value({}, "maxfiles", "abc", by="U1")
        self.assertIsNotNone(err)

    def test_bools_accept_on_off(self):
        ov, _, err = overrides.set_value({}, "automerge", "on", by="U1")
        self.assertIsNone(err)
        self.assertIs(ov["set"]["automerge"], True)
        ov, _, err = overrides.set_value(ov, "automerge", "off", by="U1")
        self.assertIs(ov["set"]["automerge"], False)

    def test_model_name_rejects_junk_but_allows_unknown_models(self):
        # no model whitelist on purpose: model names churn faster than this repo
        _, _, err = overrides.set_value({}, "model", "claude-opus-9-future", by="U1")
        self.assertIsNone(err)
        _, _, err = overrides.set_value({}, "model", "rm -rf /", by="U1")
        self.assertIsNotNone(err)

    def test_audit_trail_records_who_and_what(self):
        ov, _, _ = overrides.set_value({}, "effort", "high", by="U085GM4TUHJ")
        entry = ov["_audit"][-1]
        self.assertEqual(entry["by"], "U085GM4TUHJ")
        self.assertIn("effort", entry["change"])

    def test_audit_trail_is_capped(self):
        ov = {}
        for i in range(overrides.AUDIT_LIMIT + 10):
            ov, _, _ = overrides.set_value(ov, "maxfiles", str(50 + i % 20), by="U1")
        self.assertEqual(len(ov["_audit"]), overrides.AUDIT_LIMIT)


class TestApply(unittest.TestCase):
    def test_overrides_layer_on_top_of_config(self):
        cfg = overrides.apply(base_config(), {"set": {"effort": "high", "engine": "codex"}})
        self.assertEqual(cfg["review"]["claude"]["effort"], "high")
        self.assertEqual(cfg["review"]["engine"], "codex")

    def test_apply_never_touches_non_whitelisted_keys(self):
        hostile = {"set": {"gitlab_token": "stolen"},
                   "gitlab_token": "stolen", "gitlab_url": "http://evil"}
        cfg = overrides.apply(base_config(), hostile)
        self.assertEqual(cfg["gitlab_token"], "secret")
        self.assertEqual(cfg["gitlab_url"], "https://git.example.com")

    def test_apply_drops_values_that_no_longer_validate(self):
        # a hand-edited overrides.json must not be able to smuggle in junk
        cfg = overrides.apply(base_config(), {"set": {"effort": "turbo", "maxfiles": 70}})
        self.assertEqual(cfg["review"]["claude"]["effort"], "medium")  # untouched
        self.assertEqual(cfg["review"]["max_changed_files"], 70)      # valid one still applied

    def test_project_patch_adds_and_removes(self):
        cfg = overrides.apply(base_config(),
                              {"projects": {"add": {"g/c": "/local/c"}, "remove": ["g/a"]}})
        self.assertEqual(cfg["review"]["project_map"], {"g/b": "/local/b", "g/c": "/local/c"})

    def test_project_patch_is_a_patch_not_a_snapshot(self):
        # a project later added by hand to config.json must survive the patch
        cfg = base_config()
        cfg["review"]["project_map"]["g/hand-added"] = "/local/hand"
        merged = overrides.apply(cfg, {"projects": {"add": {"g/c": "/local/c"}}})
        self.assertIn("g/hand-added", merged["review"]["project_map"])

    def test_apply_is_a_noop_without_overrides(self):
        self.assertEqual(overrides.apply(base_config(), {}), base_config())


class TestProjects(unittest.TestCase):
    def test_add_records_baseline_pending_with_timestamp(self):
        now = datetime(2026, 7, 30, 9, 30, tzinfo=timezone.utc)
        ov = overrides.add_project({}, "g/c", "/local/c", by="U1", now=now)
        self.assertEqual(ov["projects"]["add"]["g/c"], "/local/c")
        self.assertEqual(ov["_baseline_pending"]["g/c"], now.isoformat())

    def test_remove_of_a_slack_added_project_just_drops_the_add(self):
        ov = overrides.add_project({}, "g/c", "/local/c", by="U1")
        ov = overrides.remove_project(ov, "g/c", by="U1")
        self.assertNotIn("g/c", ov.get("projects", {}).get("add", {}))
        self.assertNotIn("g/c", ov.get("projects", {}).get("remove", []))

    def test_remove_of_a_config_project_is_recorded_as_removal(self):
        ov = overrides.remove_project({}, "g/a", by="U1")
        self.assertIn("g/a", ov["projects"]["remove"])

    def test_baseline_pending_is_cleared_after_the_poller_handles_it(self):
        ov = overrides.add_project({}, "g/c", "/local/c", by="U1")
        ov = overrides.clear_baseline_pending(ov, ["g/c"])
        self.assertFalse(ov.get("_baseline_pending"))


class TestPause(unittest.TestCase):
    NOW = datetime(2026, 7, 30, 9, 0, tzinfo=timezone.utc)

    def test_pause_sets_a_deadline(self):
        ov = overrides.set_pause({}, 3600, by="U1", now=self.NOW)
        self.assertEqual(overrides.paused_until(ov), self.NOW + timedelta(hours=1))

    def test_is_paused_expires_on_its_own(self):
        ov = overrides.set_pause({}, 3600, by="U1", now=self.NOW)
        self.assertTrue(overrides.is_paused(ov, self.NOW + timedelta(minutes=59)))
        self.assertFalse(overrides.is_paused(ov, self.NOW + timedelta(minutes=61)))

    def test_resume_clears_it(self):
        ov = overrides.set_pause({}, 3600, by="U1", now=self.NOW)
        ov = overrides.resume(ov, by="U1")
        self.assertFalse(overrides.is_paused(ov, self.NOW))

    def test_garbage_deadline_is_not_a_pause(self):
        self.assertFalse(overrides.is_paused({"paused_until": "not-a-date"}, self.NOW))


class TestReset(unittest.TestCase):
    def test_reset_one_key(self):
        ov, _, _ = overrides.set_value({}, "effort", "high", by="U1")
        ov, _, _ = overrides.set_value(ov, "engine", "codex", by="U1")
        ov, msg, err = overrides.reset(ov, "effort", by="U1")
        self.assertIsNone(err)
        self.assertNotIn("effort", ov["set"])
        self.assertIn("engine", ov["set"])

    def test_reset_all_keeps_only_the_audit_trail(self):
        ov, _, _ = overrides.set_value({}, "effort", "high", by="U1")
        ov = overrides.add_project(ov, "g/c", "/local/c", by="U1")
        ov, _, err = overrides.reset(ov, "all", by="U1")
        self.assertIsNone(err)
        self.assertFalse(ov.get("set"))
        self.assertFalse(ov.get("projects"))
        self.assertTrue(ov["_audit"])

    def test_reset_unknown_key_errors(self):
        _, _, err = overrides.reset({}, "nope", by="U1")
        self.assertIsNotNone(err)


class TestDescribe(unittest.TestCase):
    def test_lists_only_changed_keys(self):
        ov, _, _ = overrides.set_value({}, "effort", "high", by="U1")
        changed = overrides.changed_keys(ov)
        self.assertEqual(changed, ["effort"])
        self.assertEqual(overrides.changed_keys({}), [])


class TestIO(unittest.TestCase):
    def test_roundtrip_and_missing_file_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "overrides.json"
            self.assertEqual(overrides.load(path), {})
            ov, _, _ = overrides.set_value({}, "effort", "high", by="U1")
            overrides.save(ov, path)
            self.assertEqual(overrides.load(path)["set"]["effort"], "high")

    def test_corrupt_file_reads_as_empty_instead_of_crashing(self):
        # a broken overrides.json must never take the whole poller down
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "overrides.json"
            path.write_text("{not json")
            self.assertEqual(overrides.load(path), {})

    def test_save_is_atomic(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "overrides.json"
            overrides.save({"set": {"effort": "high"}}, path)
            self.assertFalse(list(Path(d).glob("*.tmp")))
            self.assertEqual(json.loads(path.read_text())["set"]["effort"], "high")


if __name__ == "__main__":
    unittest.main()
