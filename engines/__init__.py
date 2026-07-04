"""AI engine registry. An engine is any module exposing:

    run_review(work_dir, context_file, output_file, repo_dir, review_cfg) -> int
    label(review_cfg) -> str   # short human-readable models note for the signature

The contract between reviewer and engine is purely file-based: the engine reads
mr_context.json and writes final_findings.json. Any headless AI CLI that can do
that can be plugged in here.
"""


def get_engine(name: str):
    if name == "claude":
        from engines import claude_engine
        return claude_engine
    if name == "codex":
        from engines import codex_engine
        return codex_engine
    raise SystemExit(f"unknown review engine: {name!r} (available: claude, codex)")
