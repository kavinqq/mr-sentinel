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


class TestUploadFile(unittest.TestCase):
    def test_three_step_upload_threads_and_comments(self):
        with mock.patch.object(slack_client, "_post_form",
                               return_value={"ok": True, "upload_url": "https://up", "file_id": "F1"}) as form, \
             mock.patch.object(slack_client, "_put_bytes") as put, \
             mock.patch.object(slack_client, "_post", return_value={"ok": True}) as post:
            slack_client.upload_file("tok", "C1", "pic.jpeg", b"abc",
                                     initial_comment="done", thread_ts="1.2")
        self.assertEqual(form.call_args.args[2], {"filename": "pic.jpeg", "length": 3})
        put.assert_called_once_with("https://up", b"abc")
        method, _, payload = post.call_args.args
        self.assertEqual(method, "files.completeUploadExternal")
        self.assertEqual(payload["files"], [{"id": "F1", "title": "pic.jpeg"}])
        self.assertEqual(payload["channel_id"], "C1")
        self.assertEqual(payload["initial_comment"], "done")
        self.assertEqual(payload["thread_ts"], "1.2")

    def test_missing_scope_raises(self):
        with mock.patch.object(slack_client, "_post_form",
                               return_value={"ok": False, "error": "missing_scope"}):
            with self.assertRaisesRegex(RuntimeError, "missing_scope"):
                slack_client.upload_file("tok", "C1", "pic.jpeg", b"abc", initial_comment="x")


if __name__ == "__main__":
    unittest.main()
