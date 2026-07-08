"""AI engine registry. An engine is any module exposing:

    run_review(work_dir, context_file, output_file, repo_dir, review_cfg) -> int
    label(review_cfg) -> str   # short human-readable models note for the signature

The contract between reviewer and engine is purely file-based: the engine reads
mr_context.json and writes final_findings.json. Any headless AI CLI that can do
that can be plugged in here.
"""


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
