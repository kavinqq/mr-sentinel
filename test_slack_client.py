"""Tests for slack_client payload construction (urllib mocked at the _post seam)."""
import unittest
from unittest import mock

import slack_client


class TestChatPostMessage(unittest.TestCase):
    OK = {"ok": True, "ts": "9.9"}

    def test_includes_thread_ts_when_given(self):
        with mock.patch.object(slack_client, "_post", return_value=self.OK) as p:
            ts = slack_client.chat_post_message("tok", "C1", "hi", thread_ts="123.45")
        self.assertEqual(ts, "9.9")
        payload = p.call_args.args[2]
        self.assertEqual(payload["thread_ts"], "123.45")

    def test_omits_thread_ts_when_none(self):
        with mock.patch.object(slack_client, "_post", return_value=self.OK) as p:
            slack_client.chat_post_message("tok", "C1", "hi")
        payload = p.call_args.args[2]
        self.assertNotIn("thread_ts", payload)


if __name__ == "__main__":
    unittest.main()
