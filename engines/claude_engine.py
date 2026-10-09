"""Claude Code engine: one headless session runs the whole scan → vet → finalize flow.

The adversarial pass rides on `claude --agents`: the skeptic persona is injected
inline as a custom subagent with its own (cheaper) model, so nothing has to be
installed under ~/.claude — the repo is self-contained.

Verified on 2026-07-04 (spike): `--agents '{"skeptic": {..., "model": "sonnet"}}'`
dispatches for real in -p mode (modelUsage shows both models).

Do NOT add `--bare`: it restricts auth to ANTHROPIC_API_KEY and would silently
switch a subscription user onto metered API billing.
"""
import json
import subprocess
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Read-only tools + Task (skeptic dispatch) + Write (the findings file only,
# enforced by the prompt; blast radius is the disposable worktree).
ALLOWED_TOOLS = "Read,Grep,Glob,Task,Write"
# Lite mode has no skeptic subagent to dispatch, so Task is dropped.
LITE_ALLOWED_TOOLS = "Read,Grep,Glob,Write"

# Steps 4-5 of prompts/review.md, injected via the __VETTING__ token.
#
# DEEP (large MRs) = 3 gates: gate 1 is the scan (step 3), gate 2 dispatches the
# Sonnet skeptic subagent, gate 3 is this Opus session's own final adjudication.
# LITE (small MRs) = 1 gate: the scan only, with a strict self-review. Either way
# the "when in doubt, DROP" bar is identical.
DEEP_VETTING = """4. ADVERSARIAL VETTING (gate 2): use the Task tool to dispatch the "skeptic"
   agent EXACTLY ONCE, passing ALL candidate findings together with only their
   relevant diff hunks. It returns keep/drop verdicts with reasons.
5. FINAL ADJUDICATION (gate 3 — you decide): this is a large, high-risk MR, so
   make the final call yourself. Start from the skeptic's verdicts, but RESCUE
   any dropped finding you are confident is a real defect, discard the rest, and
   finalize each severity. These comments post publicly and automatically:
   when still in doubt, DROP — a false positive costs more than a miss."""

LITE_VETTING = """4. SELF-REVIEW (mandatory): this is a small MR reviewed in a SINGLE pass —
   there is NO second reviewer. Be your own skeptic: re-read every candidate
   against the checkout at __WORKTREE__ and drop anything you are not highly
   confident is a real, actionable defect.
5. WHEN IN DOUBT, DROP — these comments are posted publicly and automatically;
   a false positive costs more than a miss. Prefer few, high-signal findings."""

LANGUAGE_NAMES = {
    "en": "English",
    "zh-TW": "Traditional Chinese (繁體中文)",
    "zh-CN": "Simplified Chinese (简体中文)",
    "ja": "Japanese (日本語)",
    "ko": "Korean (한국어)",
}


def language_name(code: str) -> str:
    return LANGUAGE_NAMES.get(code, code)


def render_prompt(template: str, language: str, worktree: str,
                  context_file: str, output_file: str, vetting: str = "") -> str:
    """Token replacement, not str.format(): the templates are full of JSON braces.

    __VETTING__ is expanded first because the injected block itself may contain
    other tokens (e.g. __WORKTREE__) that must still be resolved.
    """
    return (template
            .replace("__VETTING__", vetting)
            .replace("__LANGUAGE__", language_name(language))
            .replace("__WORKTREE__", worktree)
            .replace("__CONTEXT_FILE__", context_file)
            .replace("__OUTPUT_FILE__", output_file))


def build_agents_json(skeptic_prompt: str, review_cfg: dict) -> str:
    return json.dumps({
        "skeptic": {
            "description": ("Adversarial reviewer that vets candidate code-review "
                            "findings and refutes false positives. Returns keep/drop "
                            "verdicts as JSON."),
            "prompt": skeptic_prompt,
            "model": review_cfg["claude"]["skeptic_model"],
        }
    }, ensure_ascii=False)


def build_cmd(prompt: str, agents_json, worktree: str, review_cfg: dict) -> list[str]:
    """agents_json falsy -> lite mode: no skeptic subagent, no Task tool."""
    claude_cfg = review_cfg["claude"]
    cmd = [
        "claude", "-p", prompt,
        "--model", claude_cfg["model"],
        "--effort", claude_cfg["effort"],
    ]
    if agents_json:
        cmd += ["--agents", agents_json, "--allowedTools", ALLOWED_TOOLS]
    else:
        cmd += ["--allowedTools", LITE_ALLOWED_TOOLS]
    cmd += ["--add-dir", worktree, "--output-format", "json"]
    return cmd


def label(review_cfg: dict, mode: str = "deep") -> str:
    c = review_cfg["claude"]
    if mode == "lite":
        return f"scanned by {c['model']} · single-pass"
    return (f"scanned by {c['model']}, vetted by {c['skeptic_model']}, "
            f"adjudicated by {c['model']}")


def run_review(work_dir: Path, context_file: Path, output_file: Path,
               repo_dir, review_cfg: dict, mode: str = "deep") -> int:
    """Engine contract: read context_file, write findings to output_file, return rc.

    mode="lite" runs a single scan pass (small MRs, no skeptic subagent);
    mode="deep" runs scan -> Sonnet skeptic -> Opus adjudication (large MRs).
    """
    review_tpl = (PROMPTS_DIR / "review.md").read_text()
    language = review_cfg.get("language", "en")
    lite = mode == "lite"

    prompt = render_prompt(review_tpl, language, str(repo_dir),
                           context_file.name, output_file.name,
                           vetting=LITE_VETTING if lite else DEEP_VETTING)
    if lite:
        cmd = build_cmd(prompt, None, str(repo_dir), review_cfg)
    else:
        skeptic_tpl = (PROMPTS_DIR / "skeptic.md").read_text()
        skeptic = render_prompt(skeptic_tpl, language, str(repo_dir),
                                context_file.name, output_file.name)
        cmd = build_cmd(prompt, build_agents_json(skeptic, review_cfg),
                        str(repo_dir), review_cfg)

    try:
        from engines import resolve_cli
        cmd[0] = resolve_cli(cmd[0])  # schedulers run with a minimal PATH
        with open(work_dir / "claude.log", "w") as logf:
            proc = subprocess.run(cmd, cwd=str(work_dir), stdout=logf, stderr=subprocess.STDOUT,
                                  timeout=review_cfg["review_timeout_seconds"])
    except (subprocess.TimeoutExpired, OSError) as exc:
        # must not raise past the engine contract, or the reviewer's
        # "review did not finish" warning path never fires
        (work_dir / "claude.log").write_text(f"engine error: {exc}\n")
        return 1
    if proc.returncode != 0 or not output_file.exists():
        return 1
    return 0


def run_appeal(work_dir: Path, context_file: Path, output_file: Path,
               repo_dir, review_cfg: dict) -> int:
    """Re-judge findings the developer disputed (prompts/appeal.md).

    Same file contract as run_review: context in, verdicts JSON out, rc back.
    One pass, no skeptic — the findings were vetted already; this only weighs
    the developer's reply against the code."""
    prompt = render_prompt((PROMPTS_DIR / "appeal.md").read_text(),
                           review_cfg.get("language", "en"), str(repo_dir),
                           context_file.name, output_file.name)
    cmd = build_cmd(prompt, None, str(repo_dir), review_cfg)
    try:
        from engines import resolve_cli
        cmd[0] = resolve_cli(cmd[0])
        with open(work_dir / "claude-appeal.log", "w") as logf:
            proc = subprocess.run(cmd, cwd=str(work_dir), stdout=logf, stderr=subprocess.STDOUT,
                                  timeout=review_cfg["review_timeout_seconds"])
    except (subprocess.TimeoutExpired, OSError) as exc:
        (work_dir / "claude-appeal.log").write_text(f"engine error: {exc}\n")
        return 1
    if proc.returncode != 0 or not output_file.exists():
        return 1
    return 0


def run_json(prompt: str, work_dir: Path, review_cfg: dict) -> dict:
    """One tool-less call on the cheaper skeptic model; the reply must be JSON."""
    from engines import parse_json_reply, resolve_cli
    claude_cfg = review_cfg["claude"]
    cmd = [resolve_cli("claude"), "-p", prompt,
           "--model", claude_cfg.get("skeptic_model") or claude_cfg["model"],
           "--output-format", "json"]   # no --allowedTools: nothing to read, scratch cwd
    proc = subprocess.run(cmd, cwd=str(work_dir), capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=review_cfg["review_timeout_seconds"])
    if proc.returncode != 0:
        raise RuntimeError(f"claude failed (rc={proc.returncode}): {proc.stderr[-300:]}")
    outer = json.loads(proc.stdout)
    if outer.get("is_error"):
        raise RuntimeError(f"claude error: {str(outer.get('result'))[:300]}")
    return parse_json_reply(outer.get("result") or "")
