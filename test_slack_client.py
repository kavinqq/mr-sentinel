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


class TestInteractive(unittest.TestCase):
    def test_blocks_only_sent_when_given(self):
        with mock.patch.object(slack_client, "_post", return_value={"ok": True, "ts": "1"}) as p:
            slack_client.chat_post_message("tok", "C1", "hi", blocks=[{"type": "section"}])
        self.assertEqual(p.call_args.args[2]["blocks"], [{"type": "section"}])

    def test_chat_update_raises_on_error(self):
        with mock.patch.object(slack_client, "_post",
                               return_value={"ok": False, "error": "cant_update_message"}):
            with self.assertRaises(RuntimeError):
                slack_client.chat_update("tok", "C1", "1.1", "t", [])

    def test_ephemeral_targets_one_user_in_thread(self):
        with mock.patch.object(slack_client, "_post", return_value={"ok": True}) as p:
            slack_client.post_ephemeral("tok", "C1", "U1", "no", thread_ts="1.1")
        self.assertEqual(p.call_args.args[0], "chat.postEphemeral")
        self.assertEqual(p.call_args.args[2],
                         {"channel": "C1", "user": "U1", "text": "no", "thread_ts": "1.1"})

    def test_connections_open_uses_the_app_token(self):
        with mock.patch.object(slack_client, "_post_form",
                               return_value={"ok": True, "url": "wss://x"}) as form:
            self.assertEqual(slack_client.apps_connections_open("xapp-1"), "wss://x")
        self.assertEqual(form.call_args.args[:2], ("apps.connections.open", "xapp-1"))


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
