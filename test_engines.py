"""Tests for the engine layer (claude engine command building; subprocess mocked)."""
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import engines
from engines import claude_engine

CFG = {
    "language": "zh-TW",
    "review_timeout_seconds": 900,
    "claude": {"model": "claude-opus-4-8", "skeptic_model": "sonnet", "effort": "medium"},
}


class TestRegistry(unittest.TestCase):
    def test_get_claude(self):
        self.assertIs(engines.get_engine("claude"), claude_engine)

    def test_unknown_engine_exits(self):
        with self.assertRaises(SystemExit):
            engines.get_engine("gpt-magic")


class TestResolveCli(unittest.TestCase):
    def test_resolves_from_path(self):
        # python3 一定在 PATH → 應回絕對路徑
        self.assertTrue(engines.resolve_cli("python3").startswith("/"))

    def test_falls_back_to_extra_dirs(self):
        import tempfile, os, stat
        with tempfile.TemporaryDirectory() as d:
            fake = os.path.join(d, "somecli")
            open(fake, "w").write("#!/bin/sh\n")
            os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
            with mock.patch("shutil.which", return_value=None):
                self.assertEqual(engines.resolve_cli("somecli", extra_dirs=(d,)), fake)

    def test_missing_cli_raises_with_hint(self):
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(FileNotFoundError) as ctx:
                engines.resolve_cli("no-such-cli", extra_dirs=())
        self.assertIn("PATH", str(ctx.exception))  # 錯誤訊息要提示 launchd PATH 問題

    def test_run_review_returns_1_when_cli_missing(self):
        """CLI 缺失必須回 rc=1（讓 reviewer 的 Slack 告警路徑生效），不能讓例外飛出 engine。"""
        import pathlib, tempfile
        with tempfile.TemporaryDirectory() as d:
            work = pathlib.Path(d)
            (work / "mr_context.json").write_text("{}")
            with mock.patch("subprocess.run", side_effect=FileNotFoundError("claude")):
                rc = claude_engine.run_review(work, work / "mr_context.json",
                                              work / "final_findings.json", "/tmp/wt", CFG)
        self.assertEqual(rc, 1)


class TestPromptRendering(unittest.TestCase):
    def test_render_replaces_all_tokens(self):
        out = claude_engine.render_prompt(
            "review __LANGUAGE__ __WORKTREE__ __CONTEXT_FILE__ __OUTPUT_FILE__",
            language="zh-TW", worktree="/tmp/wt",
            context_file="mr_context.json", output_file="final_findings.json")
        self.assertNotIn("__", out)
        self.assertIn("/tmp/wt", out)
        self.assertIn("Traditional Chinese", out)

    def test_language_name_fallback_is_raw_code(self):
        self.assertEqual(claude_engine.language_name("xx-YY"), "xx-YY")

    def test_agents_json_carries_skeptic_model_and_prompt(self):
        agents = json.loads(claude_engine.build_agents_json("persona text", CFG))
        self.assertEqual(agents["skeptic"]["model"], "sonnet")
        self.assertIn("persona text", agents["skeptic"]["prompt"])


class TestCommand(unittest.TestCase):
    def test_build_cmd_shape(self):
        cmd = claude_engine.build_cmd("PROMPT", "{}", "/tmp/wt", CFG)
        self.assertEqual(cmd[0], "claude")
        for flag in ("-p", "--model", "--effort", "--agents", "--add-dir",
                     "--allowedTools", "--output-format"):
            self.assertIn(flag, cmd)
        self.assertIn("claude-opus-4-8", cmd)
        self.assertIn("medium", cmd)
        # --bare would force API-key billing and bypass subscription auth
        self.assertNotIn("--bare", cmd)

    def test_run_review_fails_when_no_output(self):
        with tempfile.TemporaryDirectory() as d:
            work = pathlib.Path(d)
            (work / "mr_context.json").write_text("{}")
            fake = mock.Mock(returncode=0)
            with mock.patch("subprocess.run", return_value=fake):
                rc = claude_engine.run_review(work, work / "mr_context.json",
                                              work / "final_findings.json", "/tmp/wt", CFG)
        self.assertEqual(rc, 1)  # process "succeeded" but produced no findings file

    def test_run_review_returns_1_on_timeout(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            work = pathlib.Path(d)
            (work / "mr_context.json").write_text("{}")
            with mock.patch("subprocess.run",
                            side_effect=subprocess.TimeoutExpired("claude", 900)):
                rc = claude_engine.run_review(work, work / "mr_context.json",
                                              work / "final_findings.json", "/tmp/wt", CFG)
        self.assertEqual(rc, 1)  # timeout must not raise past the engine contract


if __name__ == "__main__":
    unittest.main()
