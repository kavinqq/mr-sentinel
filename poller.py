#!/usr/bin/env python3
"""mr-sentinel poller: watch GitLab for new opened MRs on allowlisted projects.

Each run polls every project in review.project_map, notifies Slack (optional),
and spawns a detached reviewer per new MR. Scheduling (launchd/cron/systemd)
calls this script once per interval; a flock skips overlapping runs.

First run only initializes state (existing opened MRs are marked seen, no
notifications) so a fresh install never floods the channel.
"""
import argparse
import fcntl
import json
import logging
import subprocess
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import gitlab_client
import review_common
import slack_client
from sentinel_config import SCRIPT_DIR, load_config, load_state, save_state

SEEN_RETENTION_DAYS = 7

log = logging.getLogger("mr_sentinel.poller")


# ---------- pure functions (unit-tested) ----------


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def select_new(mrs: list[dict], seen: dict[str, str]) -> list[dict]:
    """Drop already-notified MRs; oldest first."""
    fresh = [mr for mr in mrs if str(mr["id"]) not in seen]
    return sorted(fresh, key=lambda mr: parse_dt(mr["created_at"]))


def prune_seen(seen: dict[str, str], opened_ids: set[str], now: datetime, days: int) -> dict[str, str]:
    """Keep entries that are still opened (would be re-notified otherwise) or recent."""
    cutoff = now - timedelta(days=days)
    return {
        mr_id: ts
        for mr_id, ts in seen.items()
        if mr_id in opened_ids or parse_dt(ts) >= cutoff
    }


def prune_slack_ts(state: dict) -> None:
    seen = state.get("seen", {})
    state["slack_ts"] = {mid: ts for mid, ts in state.get("slack_ts", {}).items() if mid in seen}


def build_notification(mr: dict, project_path: str, config: dict) -> str:
    lines = [
        f":new: <{mr['web_url']}|MR !{mr['iid']} {mr['title']}>",
        f"*project*: {project_path}",
        f"*author*: {mr['author']['name']}",
        f"*branch*: `{mr['source_branch']}` → `{mr['target_branch']}`",
    ]
    mentions = config.get("slack", {}).get("mention_user_ids", [])
    if mentions:
        lines.append("cc " + " ".join(f"<@{uid}>" for uid in mentions))
    return "\n".join(lines)


# ---------- IO seams ----------


def notify_slack(config: dict, text: str) -> str | None:
    """Bot token first (returns ts, enables reactions); webhook fallback (no ts);
    neither configured -> GitLab-only mode."""
    slack = config.get("slack", {})
    if slack.get("bot_token") and slack.get("channel_id"):
        return slack_client.chat_post_message(slack["bot_token"], slack["channel_id"], text)
    if slack.get("webhook_url"):
        slack_client.post_webhook(slack["webhook_url"], text)
    return None


def maybe_spawn_review(config: dict, mr: dict, project_path: str) -> None:
    """Detached spawn so a multi-minute review never blocks the 60s poll loop."""
    if not review_common.is_review_target(project_path, config.get("review", {})):
        return
    reviews_dir = SCRIPT_DIR / "reviews"
    reviews_dir.mkdir(exist_ok=True)
    try:
        spawn_log = open(reviews_dir / f"{mr['id']}.spawn.log", "a")
        subprocess.Popen(
            [sys.executable, str(SCRIPT_DIR / "reviewer.py"),
             "--project", project_path, "--iid", str(mr["iid"]), "--mr-id", str(mr["id"])],
            stdout=spawn_log, stderr=subprocess.STDOUT,
            start_new_session=True, cwd=str(SCRIPT_DIR),
        )
        log.info("spawned review: %s !%s", project_path, mr["iid"])
    except Exception:
        log.exception("failed to spawn review (notification unaffected): !%s", mr["iid"])


# ---------- main flow ----------


def poll_opened(config: dict) -> tuple[dict[str, list[dict]], int]:
    """Poll opened MRs and return ({project_path: [mrs]}, error_count).

    Two watch modes:
    - watch.group_ids set: one API call per group covers every project inside
      (path_prefixes filters out projects merely shared into the group).
    - otherwise: poll each project in review.project_map individually.
    A failing source logs, bumps error_count and is skipped.
    """
    base, token = config["gitlab_url"], config["gitlab_token"]
    watch = config.get("watch", {})
    result: dict[str, list[dict]] = {}
    errors = 0
    if watch.get("group_ids"):
        for group_id in watch["group_ids"]:
            try:
                mrs = gitlab_client.list_group_opened_mrs(base, token, group_id)
            except (urllib.error.URLError, OSError) as exc:
                log.warning("poll failed for group %s: %s", group_id, exc)
                errors += 1
                continue
            if len(mrs) >= 100:
                log.warning("group %s returned a full page; some MRs may be missed", group_id)
            for mr in mrs:
                path = review_common.project_path_from_mr(mr, base)
                if review_common.is_in_scope(path, watch.get("path_prefixes", [])):
                    result.setdefault(path, []).append(mr)
    else:
        for project_path in config["review"]["project_map"]:
            try:
                result[project_path] = gitlab_client.list_opened_mrs(base, token, project_path)
            except (urllib.error.URLError, OSError) as exc:
                log.warning("poll failed for %s: %s", project_path, exc)
                errors += 1
    return result, errors


def run_once(config: dict, state_path: Path, dry_run: bool) -> int:
    now = datetime.now(timezone.utc)
    opened_by_project, poll_errors = poll_opened(config)
    opened_ids = {str(mr["id"]) for mrs in opened_by_project.values() for mr in mrs}

    state = load_state(state_path)
    if state is None:
        if poll_errors:
            # never build a baseline from a failed poll: an empty/partial baseline
            # would flag every existing MR as "new" once the network recovers
            log.warning("initialization aborted: %s source(s) failed, retrying next run",
                        poll_errors)
            return 1
        seen = {str(mr["id"]): mr["created_at"]
                for mrs in opened_by_project.values() for mr in mrs}
        save_state({"seen": seen, "slack_ts": {}}, state_path)
        log.info("initialized: %s existing opened MR(s) marked seen, none notified", len(seen))
        return 0
    state.setdefault("slack_ts", {})

    failed = False
    for project_path, mrs in opened_by_project.items():
        for mr in select_new(mrs, state["seen"]):
            text = build_notification(mr, project_path, config)
            if dry_run:
                print(f"--- dry-run (not sent) ---\n{text}\n")
                continue
            try:
                ts = notify_slack(config, text)
            except (urllib.error.URLError, RuntimeError):
                log.exception("notify failed, retrying next run: MR !%s", mr["iid"])
                failed = True
                break
            log.info("notified: %s !%s %s", project_path, mr["iid"], mr["title"])
            state["seen"][str(mr["id"])] = mr["created_at"]
            if ts:
                state["slack_ts"][str(mr["id"])] = ts
            save_state(state, state_path)
            maybe_spawn_review(config, mr, project_path)

    if dry_run:
        return 0
    if not poll_errors:
        # prune only on a fully-successful poll: a failed source would make its
        # still-opened MRs look gone and eligible for premature pruning
        state["seen"] = prune_seen(state["seen"], opened_ids, now, SEEN_RETENTION_DAYS)
        prune_slack_ts(state)
    save_state(state, state_path)
    return 1 if failed or poll_errors else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="mr-sentinel poller")
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--dry-run", action="store_true", help="print messages instead of sending")
    args = ap.parse_args()

    handlers: list[logging.Handler] = [
        RotatingFileHandler(SCRIPT_DIR / "poller.log", maxBytes=1_000_000,
                            backupCount=2, encoding="utf-8")
    ]
    if sys.stderr.isatty():
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)

    lock_path = SCRIPT_DIR / ".lock"
    with open(lock_path, "w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("previous run still in progress; skipping")
            return 0
        try:
            return run_once(load_config(args.config), SCRIPT_DIR / "state.json", args.dry_run)
        except urllib.error.HTTPError as exc:
            log.error("API error (token expired or missing scope?): %s", exc)
            return 1
        except urllib.error.URLError as exc:
            log.error("connection failed (VPN / network down?): %s", exc)
            return 1
        except Exception:
            log.exception("unexpected error")
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
