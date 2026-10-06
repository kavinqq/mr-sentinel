"""Config and state loading shared by poller, reviewer and CLI helpers."""
import json
import logging
from pathlib import Path

import overrides

SCRIPT_DIR = Path(__file__).resolve().parent
OVERRIDES_PATH = SCRIPT_DIR / "overrides.json"

log = logging.getLogger("mr_sentinel")


def load_config(path: Path | None = None, with_overrides: bool = True) -> dict:
    path = path or SCRIPT_DIR / "config.json"
    if not path.exists():
        raise SystemExit(f"config not found at {path}; copy config.example.json and fill it in")
    if path.stat().st_mode & 0o077:
        log.warning("config permissions too open; run: chmod 600 %s", path)
    config = json.loads(path.read_text())
    missing = {"gitlab_url", "gitlab_token"} - set(config)
    if missing:
        raise SystemExit(f"config missing required keys: {sorted(missing)}")
    config["gitlab_url"] = config["gitlab_url"].rstrip("/")

    # slack is entirely optional (GitLab-only mode)
    slack = config.setdefault("slack", {})
    slack.setdefault("admin_user_ids", [])      # empty -> falls back to mention_user_ids

    review = config.setdefault("review", {})
    review.setdefault("project_map", {})
    review.setdefault("language", "en")
    review.setdefault("engine", "claude")
    review.setdefault("max_changed_files", 60)
    review.setdefault("max_diff_lines", 3000)
    review.setdefault("review_timeout_seconds", 900)
    claude_cfg = review.setdefault("claude", {})
    claude_cfg.setdefault("model", "claude-opus-4-8")
    claude_cfg.setdefault("skeptic_model", "sonnet")
    claude_cfg.setdefault("effort", "medium")
    codex_cfg = review.setdefault("codex", {})
    codex_cfg.setdefault("model", "")           # empty = codex CLI default
    codex_cfg.setdefault("skeptic_model", "")

    watch = config.setdefault("watch", {})
    watch.setdefault("group_ids", [])           # set -> group polling mode (notify whole groups)
    watch.setdefault("path_prefixes", [])       # group mode: keep only real member projects

    # Slack-side edits land last so they win over the defaults above; the
    # whitelist in overrides.py keeps them away from tokens and urls.
    if with_overrides:
        config = overrides.apply(config, overrides.load(OVERRIDES_PATH))
    return config


def load_state(path: Path | None = None) -> dict | None:
    path = path or SCRIPT_DIR / "state.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def save_state(state: dict, path: Path | None = None) -> None:
    path = path or SCRIPT_DIR / "state.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    tmp.replace(path)
