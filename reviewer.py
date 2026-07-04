#!/usr/bin/env python3
"""mr-sentinel reviewer: run one MR through the AI review pipeline.

Spawned detached by the poller (or run manually). Flow:
per-MR lock → idempotency check (:eyes: award emoji) → fetch context →
claim (:eyes: on the MR + optional Slack reaction) → size guard →
disposable git worktree → AI engine (scan → adversarial vet → finalize) →
post comments → optional Slack completion message → cleanup.

The user's clone is never touched: `git fetch` only updates refs/objects and
the checkout happens in a throwaway worktree that is removed afterwards.
"""
import argparse
import fcntl
import json
import logging
import subprocess
import sys
import urllib.error
from collections import Counter
from logging.handlers import RotatingFileHandler
from pathlib import Path

import engines
import fetch_mr
import gitlab_client
import post_comment
import review_common
import slack_client
from sentinel_config import SCRIPT_DIR, load_config, load_state

REVIEWS_DIR = SCRIPT_DIR / "reviews"

log = logging.getLogger("mr_sentinel.reviewer")


# ---------- pure helpers (unit-tested) ----------


def completion_text(project_path: str, iid, web_url, findings: list, posted: int,
                    language: str = "en") -> str:
    c = Counter(f.get("severity") for f in findings)
    headline = "AI Review 完成!" if language.startswith("zh") else "AI review complete!"
    text = (f":white_check_mark: {headline} {project_path} MR !{iid} — "
            f"{posted} comment(s) (🔴{c['high']} 🟠{c['medium']} 🟡{c['low']})")
    if web_url:
        text += f"\n{web_url}"
    return text


def build_signature(engine_label: str) -> str:
    return f"— 🤖 mr-sentinel AI review ({engine_label})"


# ---------- IO ----------


def _slack_say(config: dict, text: str) -> None:
    slack = config.get("slack", {})
    if slack.get("bot_token") and slack.get("channel_id"):
        try:
            slack_client.chat_post_message(slack["bot_token"], slack["channel_id"], text)
        except Exception:
            log.exception("Slack notify failed (ignored)")


def _run_git(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def run_review(project_path: str, iid, mr_id, config: dict, state: dict, dry_run: bool) -> int:
    base = config["gitlab_url"]
    token = config["gitlab_token"]
    review_cfg = config["review"]
    language = review_cfg["language"]
    work = REVIEWS_DIR / str(mr_id)
    work.mkdir(parents=True, exist_ok=True)

    # 1. idempotency: our own :eyes: on the MR means it was already claimed
    me = gitlab_client.get_current_user(base, token)
    emojis = gitlab_client.get_award_emojis(base, token, project_path, iid)
    if review_common.has_own_award_emoji(emojis, me["id"]):
        log.info("MR !%s already has our :eyes:, skipping", iid)
        return 0

    # 2. fetch context (also yields head_sha / web_url)
    ctx = fetch_mr.build_context(base, token, project_path, iid)
    head_sha = ctx["diff_refs"]["head_sha"]

    # 3. claim: :eyes: on the MR + optional Slack reaction on the notification
    if not dry_run:
        gitlab_client.add_award_emoji(base, token, project_path, iid, "eyes")
        ts = review_common.slack_ts_for(state, mr_id)
        slack = config.get("slack", {})
        if ts and slack.get("bot_token") and slack.get("channel_id"):
            try:
                slack_client.add_reaction(slack["bot_token"], slack["channel_id"], ts, "eyes")
            except Exception:
                log.exception("Slack reaction failed (ignored)")

    # 4. size guard
    skip, reason = review_common.should_skip_for_size(
        ctx["stats"]["files"], ctx["stats"]["lines"], review_cfg)
    if skip:
        log.info("MR !%s skipped: %s", iid, reason)
        if not dry_run:
            _slack_say(config, f":warning: {project_path} MR !{iid} skipped ({reason}), "
                               f"please review manually\n{ctx.get('web_url')}")
        return 0

    # 5. local clone + fetch MR ref + disposable worktree
    local = review_common.resolve_local_path(project_path, review_cfg)
    if not local or not Path(local).exists():
        log.error("local clone not found for %s", project_path)
        if not dry_run:
            _slack_say(config, f":warning: local clone not found for {project_path}, "
                               f"MR !{iid} skipped")
        return 1
    fetch = _run_git(["git", "-C", local, "fetch", "-q", "origin",
                      f"+refs/merge-requests/{iid}/head:refs/mr-sentinel/{iid}"])
    if fetch.returncode != 0:
        log.error("git fetch failed: %s", fetch.stderr)
        if not dry_run:
            _slack_say(config, f":warning: git fetch failed for {project_path} MR !{iid}")
        return 1

    wt = work / "wt"
    _run_git(["git", "-C", local, "worktree", "remove", "--force", str(wt)])  # clear leftovers
    add = _run_git(["git", "-C", local, "worktree", "add", "--detach", "-q", str(wt), head_sha])
    if add.returncode != 0:
        log.error("worktree add failed: %s", add.stderr)
        if not dry_run:
            # the MR is already claimed (:eyes:) and will never be retried;
            # every failure branch must produce a human-visible signal
            _slack_say(config, f":warning: worktree setup failed for {project_path} MR !{iid}, "
                               f"please review manually\n{ctx.get('web_url')}")
        return 1

    try:
        # 6. hand off to the AI engine (file-based contract)
        ctx_path = work / "mr_context.json"
        out_path = work / "final_findings.json"
        fetch_mr.write_context(ctx_path, ctx)
        if out_path.exists():
            out_path.unlink()

        engine = engines.get_engine(review_cfg["engine"])
        if dry_run:
            print("--- dry-run ---")
            print("work dir :", work)
            print("engine   :", review_cfg["engine"], f"({engine.label(review_cfg)})")
            print("worktree :", wt)
            print("stats    :", ctx["stats"])
            return 0

        rc = engine.run_review(work, ctx_path, out_path, wt, review_cfg)
        if rc != 0:
            log.error("engine failed for MR !%s (rc=%s)", iid, rc)
            _slack_say(config, f":warning: {project_path} MR !{iid} review did not finish, "
                               f"please review manually\n{ctx.get('web_url')}")
            return 1

        # 7. post comments (scripts post; the AI never does)
        findings = json.loads(out_path.read_text()).get("findings", [])
        posted = post_comment.post_findings(
            base, token, project_path, iid, findings, ctx["diff_refs"],
            signature=build_signature(engine.label(review_cfg)))

        # 8. completion message
        _slack_say(config, completion_text(project_path, iid, ctx.get("web_url"),
                                           findings, posted, language))
        log.info("MR !%s reviewed: %s comment(s) posted", iid, posted)
        return 0
    finally:
        _run_git(["git", "-C", local, "worktree", "remove", "--force", str(wt)])


def main() -> int:
    ap = argparse.ArgumentParser(description="mr-sentinel single-MR reviewer")
    ap.add_argument("--project", required=True, help="project path (group/name)")
    ap.add_argument("--iid", required=True)
    ap.add_argument("--mr-id", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    REVIEWS_DIR.mkdir(exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(SCRIPT_DIR / "reviewer.log", maxBytes=1_000_000,
                            backupCount=2, encoding="utf-8")
    ]
    if sys.stderr.isatty():
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)

    config = load_config()
    state = load_state() or {}

    lock_path = REVIEWS_DIR / f".lock-{args.mr_id}"
    with open(lock_path, "w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("review for MR !%s already running, skipping", args.iid)
            return 0
        try:
            return run_review(args.project, args.iid, args.mr_id, config, state, args.dry_run)
        except urllib.error.HTTPError as exc:
            log.error("API error (token scope?): %s", exc)
            return 1
        except Exception:
            log.exception("unexpected error for MR !%s", args.iid)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
