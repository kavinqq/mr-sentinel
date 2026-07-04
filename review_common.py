"""Pure functions for the MR review pipeline (unit-testable, no IO)."""

NOISE_SUFFIXES = (".lock", "-lock.json", ".min.js", ".min.css", ".map", ".svg", ".png", ".jpg", ".gif")
NOISE_NAMES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock", "composer.lock", "Cargo.lock", "go.sum")
NOISE_DIR_PARTS = ("node_modules", "/dist/", "/build/", "/vendor/", "/.next/", "/coverage/")


def is_review_target(project_path: str, review_cfg: dict) -> bool:
    """Whether this project is on the review allowlist."""
    return project_path in review_cfg.get("project_map", {})


def resolve_local_path(project_path: str, review_cfg: dict) -> str | None:
    """GitLab project path -> local clone path; None if not allowlisted."""
    return review_cfg.get("project_map", {}).get(project_path)


def is_noise_path(path: str) -> bool:
    """Lockfiles / generated assets / vendored deps: skip to save AI budget."""
    name = path.rsplit("/", 1)[-1]
    if name in NOISE_NAMES:
        return True
    if any(path.endswith(sfx) for sfx in NOISE_SUFFIXES):
        return True
    probe = f"/{path}/"
    return any(part in probe for part in NOISE_DIR_PARTS)


def filter_noise_changes(changes: list[dict]) -> list[dict]:
    return [c for c in changes if not is_noise_path(c.get("new_path") or c.get("old_path") or "")]


def diff_stats(changes: list[dict]) -> tuple[int, int]:
    """Return (file count, changed line count); counts only +/- diff lines, not headers."""
    lines = 0
    for c in changes:
        for ln in (c.get("diff") or "").splitlines():
            if ln.startswith(("+++", "---")):
                continue
            if ln.startswith(("+", "-")):
                lines += 1
    return len(changes), lines


def should_skip_for_size(files: int, lines: int, review_cfg: dict) -> tuple[bool, str]:
    """Oversized MRs are skipped: one huge MR could burn the whole review budget."""
    max_files = review_cfg.get("max_changed_files", 60)
    max_lines = review_cfg.get("max_diff_lines", 3000)
    if files > max_files:
        return True, f"{files} files changed (limit {max_files})"
    if lines > max_lines:
        return True, f"{lines} lines changed (limit {max_lines})"
    return False, ""
