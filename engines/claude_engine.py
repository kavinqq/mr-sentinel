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
                  context_file: str, output_file: str) -> str:
    """Token replacement, not str.format(): the templates are full of JSON braces."""
    return (template
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


def build_cmd(prompt: str, agents_json: str, worktree: str, review_cfg: dict) -> list[str]:
    claude_cfg = review_cfg["claude"]
    return [
        "claude", "-p", prompt,
        "--model", claude_cfg["model"],
        "--effort", claude_cfg["effort"],
        "--agents", agents_json,
        "--add-dir", worktree,
        "--allowedTools", ALLOWED_TOOLS,
        "--output-format", "json",
    ]


def label(review_cfg: dict) -> str:
    c = review_cfg["claude"]
    return f"scanned by {c['model']}, vetted by {c['skeptic_model']}"


def run_review(work_dir: Path, context_file: Path, output_file: Path,
               repo_dir, review_cfg: dict) -> int:
    """Engine contract: read context_file, write findings to output_file, return rc."""
    review_tpl = (PROMPTS_DIR / "review.md").read_text()
    skeptic_tpl = (PROMPTS_DIR / "skeptic.md").read_text()
    language = review_cfg.get("language", "en")

    prompt = render_prompt(review_tpl, language, str(repo_dir),
                           context_file.name, output_file.name)
    skeptic = render_prompt(skeptic_tpl, language, str(repo_dir),
                            context_file.name, output_file.name)
    cmd = build_cmd(prompt, build_agents_json(skeptic, review_cfg), str(repo_dir), review_cfg)

    try:
        with open(work_dir / "claude.log", "w") as logf:
            proc = subprocess.run(cmd, cwd=str(work_dir), stdout=logf, stderr=subprocess.STDOUT,
                                  timeout=review_cfg["review_timeout_seconds"])
    except subprocess.TimeoutExpired:
        # must not raise past the engine contract, or the reviewer's
        # "review did not finish" warning path never fires
        return 1
    if proc.returncode != 0 or not output_file.exists():
        return 1
    return 0
