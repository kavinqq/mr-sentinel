"""AI engine registry. An engine is any module exposing:

    run_review(work_dir, context_file, output_file, repo_dir, review_cfg,
               mode="deep") -> int
    run_appeal(work_dir, context_file, output_file, repo_dir, review_cfg) -> int
    label(review_cfg, mode="deep") -> str   # models note for the signature

    run_json(prompt, work_dir, review_cfg) -> dict          # one tool-less call

run_appeal re-judges findings a developer disputed (appeal_context.json in →
appeal_verdicts.json out, see prompts/appeal.md). run_json is for small
judgment calls with no code access (history/classify.py); it uses the cheaper
skeptic model and raises on any failure.

`mode` is "lite" (small MR: a single scan pass) or "deep" (large MR: scan +
adversarial vetting + final adjudication). The reviewer picks it by MR size.

The contract between reviewer and engine is purely file-based: the engine reads
mr_context.json and writes final_findings.json. Any headless AI CLI that can do
that can be plugged in here.
"""


import json
import os
import shutil

# launchd/cron run with a minimal PATH that misses user-level install dirs;
# resolve CLI binaries explicitly so scheduled runs behave like shell runs.
DEFAULT_EXTRA_DIRS = ("~/.local/bin", "/usr/local/bin", "/opt/homebrew/bin")


def resolve_cli(name: str, extra_dirs=DEFAULT_EXTRA_DIRS) -> str:
    found = shutil.which(name)
    if found:
        return found
    for d in extra_dirs:
        candidate = os.path.expanduser(os.path.join(d, name))
        if os.path.exists(candidate):
            return candidate
    raise FileNotFoundError(
        f"'{name}' CLI not found. Schedulers (launchd/cron) run with a minimal PATH; "
        f"searched PATH and {extra_dirs}. Install {name} or add its directory to PATH."
    )


def get_engine(name: str):
    if name == "claude":
        from engines import claude_engine
        return claude_engine
    if name == "codex":
        from engines import codex_engine
        return codex_engine
    raise SystemExit(f"unknown review engine: {name!r} (available: claude, codex)")


def parse_json_reply(text: str) -> dict:
    """Models sometimes wrap JSON in fences or prose; extract the object."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        return json.loads(text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.index("{"), text.rindex("}") + 1
        return json.loads(text[start:end])
