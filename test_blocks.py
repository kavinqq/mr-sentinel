"""Tests for the pure Block Kit builders and click parsing."""
import json
import unittest

import blocks
import commands


def click(value, action_id="mrs:rerun:auto", **extra) -> dict:
    payload = {
        "type": "block_actions",
        "user": {"id": "U1"},
        "channel": {"id": "C1"},
        "container": {"type": "message", "message_ts": "200.1", "channel_id": "C1"},
        "message": {"ts": "200.1", "thread_ts": "100.1", "text": "done",
                    "blocks": blocks.message("done", blocks.rerun_buttons("g/app", 7))},
        "actions": [{"action_id": action_id,
                     "value": value if isinstance(value, str) else json.dumps(value)}],
    }
    payload.update(extra)
    return payload


class TestBuilders(unittest.TestCase):
    def test_single_rerun_button_encodes_target_in_auto_mode(self):
        (button,) = blocks.rerun_buttons("g/app", 7)
        self.assertEqual(json.loads(button["value"]), {"verb": "rerun", "args": ["g/app!7", "auto"]})
        self.assertTrue(button["action_id"].startswith("mrs:"))

    def test_custom_label(self):
        (button,) = blocks.rerun_buttons("g/app", 7, label="🔁 再試一次")
        self.assertEqual(button["text"]["text"], "🔁 再試一次")

    def test_message_without_buttons_has_no_actions_block(self):
        self.assertEqual([b["type"] for b in blocks.message("hi", [])], ["section"])

    def test_resolved_drops_buttons_and_appends_note(self):
        original = blocks.message("done", blocks.rerun_buttons("g/app", 7))
        out = blocks.resolved(original, "♻️ <@U1> 已觸發重審")
        self.assertEqual([b["type"] for b in out], ["section", "context"])
        self.assertIn("U1", out[-1]["elements"][0]["text"])


class TestParseClick(unittest.TestCase):
    def test_round_trip_to_command(self):
        got = blocks.parse_click(click({"verb": "rerun", "args": ["g/app!7", "deep"]}))
        self.assertEqual(got["command"], commands.Command("rerun", ["g/app!7", "deep"]))
        self.assertEqual((got["user"], got["channel"]), ("U1", "C1"))
        self.assertEqual((got["message_ts"], got["thread_ts"]), ("200.1", "100.1"))
        self.assertEqual(got["action_id"], "rerun:auto")

    def test_foreign_action_ignored(self):
        self.assertIsNone(blocks.parse_click(click({"verb": "rerun"}, action_id="other")))

    def test_garbage_values_ignored(self):
        for bad in ("not json", json.dumps([1]), json.dumps({"verb": 3}),
                    json.dumps({"verb": "rerun", "args": [1]})):
            self.assertIsNone(blocks.parse_click(click(bad)), bad)

    def test_other_payload_types_ignored(self):
        self.assertIsNone(blocks.parse_click({"type": "view_submission"}))
        self.assertIsNone(blocks.parse_click(click({"verb": "rerun"}, actions=[])))


if __name__ == "__main__":
    unittest.main()
