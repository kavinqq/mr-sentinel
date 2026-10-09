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

    def test_every_prompt_gets_the_one_taxonomy(self):
        from history.parse import CATEGORIES
        for name in ("review.md", "codex_scan.md", "skeptic.md"):
            tpl = (pathlib.Path(claude_engine.__file__).resolve().parent.parent / "prompts" / name).read_text()
            out = claude_engine.render_prompt(tpl, "en", "/wt", "c.json", "o.json", "v")
            self.assertNotIn("__TAXONOMY__", out, name)
            for key in CATEGORIES:
                self.assertIn(f"- {key}:", out, (name, key))
            self.assertNotIn("code_smell", out, name)

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

    def test_run_appeal_is_single_pass_with_appeal_prompt(self):
        with tempfile.TemporaryDirectory() as d:
            work = pathlib.Path(d)
            out = work / "appeal_verdicts.json"

            def fake_run(cmd, **kw):
                out.write_text("{}")
                return mock.Mock(returncode=0)

            with mock.patch("engines.resolve_cli", return_value="claude"), \
                 mock.patch("subprocess.run", side_effect=fake_run) as run:
                rc = claude_engine.run_appeal(work, work / "appeal_context.json", out,
                                              "/tmp/wt", CFG)
        self.assertEqual(rc, 0)
        cmd = run.call_args.args[0]
        self.assertNotIn("--agents", cmd)                       # no skeptic pass
        self.assertIn(claude_engine.LITE_ALLOWED_TOOLS, cmd)
        prompt = cmd[cmd.index("-p") + 1]
        self.assertIn("appeal_verdicts.json", prompt)
        self.assertIn("When in doubt, ACCEPT", prompt)

    def test_run_appeal_fails_without_output(self):
        with tempfile.TemporaryDirectory() as d:
            work = pathlib.Path(d)
            with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)):
                self.assertEqual(claude_engine.run_appeal(
                    work, work / "c.json", work / "o.json", "/tmp/wt", CFG), 1)

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


class TestTierModes(unittest.TestCase):
    def test_label_lite_marks_single_pass(self):
        lite = claude_engine.label(CFG, mode="lite")
        self.assertNotIn("vetted", lite)
        self.assertIn("single-pass", lite)

    def test_label_deep_names_all_three_gates(self):
        deep = claude_engine.label(CFG, mode="deep")
        self.assertIn("scanned by claude-opus-4-8", deep)  # gate 1
        self.assertIn("sonnet", deep)                      # gate 2
        self.assertIn("adjudicated", deep)                 # gate 3

    def test_render_prompt_fills_vetting_token(self):
        out = claude_engine.render_prompt("a __VETTING__ b", "en", "/wt",
                                          "c", "o", vetting="VET BLOCK")
        self.assertIn("VET BLOCK", out)
        self.assertNotIn("__VETTING__", out)

    def test_deep_vetting_dispatches_skeptic_and_adjudicates(self):
        self.assertIn("Task tool", claude_engine.DEEP_VETTING)      # gate 2 dispatch
        self.assertIn("ADJUDICAT", claude_engine.DEEP_VETTING.upper())  # gate 3
        self.assertNotIn("Task tool", claude_engine.LITE_VETTING)   # no gate 2 in lite

    def test_build_cmd_deep_allows_task_and_agents(self):
        cmd = claude_engine.build_cmd("PROMPT", "{}", "/tmp/wt", CFG)
        self.assertIn("--agents", cmd)
        tools = cmd[cmd.index("--allowedTools") + 1]
        self.assertIn("Task", tools)  # deep mode dispatches the skeptic subagent

    def test_build_cmd_lite_omits_agents_and_task(self):
        cmd = claude_engine.build_cmd("PROMPT", None, "/tmp/wt", CFG)
        self.assertNotIn("--agents", cmd)  # no skeptic persona injected
        tools = cmd[cmd.index("--allowedTools") + 1]
        self.assertNotIn("Task", tools)  # nothing to dispatch
        self.assertIn("Write", tools)    # but still writes the findings file
        self.assertIn("claude-opus-4-8", cmd)  # same scanning model

    def test_run_review_lite_issues_single_agentless_command(self):
        with tempfile.TemporaryDirectory() as d:
            work = pathlib.Path(d)
            (work / "mr_context.json").write_text("{}")
            captured = {}

            def fake_run(cmd, **kw):
                captured["cmd"] = cmd
                (work / "final_findings.json").write_text('{"findings": []}')
                return mock.Mock(returncode=0)

            with mock.patch("subprocess.run", side_effect=fake_run), \
                 mock.patch("engines.resolve_cli", side_effect=lambda name: name):
                rc = claude_engine.run_review(work, work / "mr_context.json",
                                              work / "final_findings.json", "/tmp/wt",
                                              CFG, mode="lite")
            self.assertEqual(rc, 0)
            self.assertNotIn("--agents", captured["cmd"])

    def test_run_review_deep_issues_agents_command(self):
        with tempfile.TemporaryDirectory() as d:
            work = pathlib.Path(d)
            (work / "mr_context.json").write_text("{}")
            captured = {}

            def fake_run(cmd, **kw):
                captured["cmd"] = cmd
                (work / "final_findings.json").write_text('{"findings": []}')
                return mock.Mock(returncode=0)

            with mock.patch("subprocess.run", side_effect=fake_run), \
                 mock.patch("engines.resolve_cli", side_effect=lambda name: name):
                rc = claude_engine.run_review(work, work / "mr_context.json",
                                              work / "final_findings.json", "/tmp/wt",
                                              CFG, mode="deep")
            self.assertEqual(rc, 0)
            self.assertIn("--agents", captured["cmd"])


if __name__ == "__main__":
    unittest.main()
