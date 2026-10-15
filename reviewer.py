#!/usr/bin/env python3
"""mr-sentinel reviewer: run one MR through the AI review pipeline.

Spawned detached by the poller (or run manually). Flow:
per-MR lock → idempotency check (:eyes: award emoji) → fetch context →
claim (:eyes: on the MR + optional Slack reaction) → size guard →
disposable git worktree → AI engine (scan → adversarial vet → finalize) →
post comments → optional Slack completion message (+ verdict image) → cleanup.

The user's clone is never touched: `git fetch` only updates refs/objects and
the checkout happens in a throwaway worktree that is removed afterwards.
"""
import argparse
import fcntl
import json
import logging
import subprocess
import sys
import time
from collections import Counter
from logging.handlers import RotatingFileHandler
from pathlib import Path

import blocks
import engines
from history import rate as history_rate
from history import sync as history_sync
import fetch_mr
import gitlab_client
import post_comment
import review_common
import slack_client
from sentinel_config import (SCRIPT_DIR, SOCKET_HEARTBEAT_MAX_AGE, SOCKET_HEARTBEAT_PATH,
                             load_config, load_state)

REVIEWS_DIR = SCRIPT_DIR / "reviews"
VERDICT_DIR = SCRIPT_DIR / "assets" / "verdict"

log = logging.getLogger("mr_sentinel.reviewer")


# ---------- pure helpers (unit-tested) ----------


def completion_text(project_path: str, iid, web_url, findings: list, posted: int,
                    language: str = "en", mode: str = "deep") -> str:
    c = Counter(f.get("severity") for f in findings)
    headline = "AI Review 完成!" if language.startswith("zh") else "AI review complete!"
    tag = f" [{mode}]"  # lite = 1 gate (scan only); deep = 3 gates
    text = (f":white_check_mark: {headline}{tag} {project_path} MR !{iid} — "
            f"{posted} comment(s) (🔴{c['high']} 🟠{c['medium']} 🟡{c['low']})")
    if web_url:
        text += f"\n{web_url}"
    return text


def build_signature(engine_label: str) -> str:
    return f"— 🤖 mr-sentinel AI review ({engine_label})"


# ---------- IO ----------


def socket_listener_alive(path=SOCKET_HEARTBEAT_PATH, now=None) -> bool:
    try:
        age = (time.time() if now is None else now) - path.stat().st_mtime
    except OSError:
        return False
    return age < SOCKET_HEARTBEAT_MAX_AGE


def buttons_enabled(config: dict) -> bool:
    """Buttons only work while slack_bot.py --socket is connected: it needs the
    app-level token *and* a fresh heartbeat. Otherwise a click would just show
    Slack's "app did not respond" error, so plain text is posted instead."""
    slack = config.get("slack", {})
    return bool(slack.get("app_token") and slack.get("bot_token")
                and slack.get("channel_id") and socket_listener_alive())


def _slack_say(config: dict, text: str, thread_ts: str | None = None,
               buttons: list | None = None) -> None:
    """Post a reviewer message. thread_ts (the MR notification's ts) replies in
    that thread; the webhook fallback has no ts, so it always posts top-level.
    `buttons` are attached only when the socket listener can answer them."""
    slack = config.get("slack", {})
    try:
        if slack.get("bot_token") and slack.get("channel_id"):
            extra = ({"blocks": blocks.message(text, buttons)}
                     if buttons and buttons_enabled(config) else {})
            slack_client.chat_post_message(slack["bot_token"], slack["channel_id"], text,
                                           thread_ts, **extra)
        elif slack.get("webhook_url"):
            slack_client.post_webhook(slack["webhook_url"], text)
    except Exception:
        log.exception("Slack notify failed (ignored)")


def _slack_say_verdict(config: dict, text: str, findings: list,
                       thread_ts: str | None = None, buttons: list | None = None) -> None:
    """Completion message with a random image from the verdict tier's folder. The image is
    decoration: no bot token, no matching file, or a failed upload (e.g. the app
    lacks `files:write`) all degrade to the plain text message.

    A file share can carry neither buttons nor a later chat.update, so with
    buttons on, the image goes up bare and the text follows as its own message."""
    slack = config.get("slack", {})
    folder = VERDICT_DIR / review_common.VERDICT_IMAGE_DIR[review_common.verdict_tier(findings)]
    image = review_common.pick_image([p.name for p in folder.iterdir()] if folder.is_dir() else [])
    with_buttons = bool(buttons) and buttons_enabled(config)
    if image and slack.get("bot_token") and slack.get("channel_id"):
        try:
            slack_client.upload_file(slack["bot_token"], slack["channel_id"], image,
                                     (folder / image).read_bytes(),
                                     initial_comment="" if with_buttons else text,
                                     thread_ts=thread_ts)
            if not with_buttons:
                return
        except Exception:
            log.exception("verdict image upload failed, sending text only")
    _slack_say(config, text, thread_ts, buttons)


def _maybe_auto_merge(config: dict, base: str, token: str,
                      project_path: str, iid, web_url, thread_ts: str | None = None,
                      *, reviewed_sha: str) -> None:
    """Merge a clean MR, honoring the hard rails. Every path warns; none raises.

    Rails (in review_common.auto_merge_blocker): never merge a draft, a head
    other than `reviewed_sha`, an MR GitLab won't cleanly merge, or one whose
    head pipeline isn't green.
    """
    try:
        mr = gitlab_client.get_mr(base, token, project_path, iid)
        has_ci = gitlab_client.has_ci_config(base, token, project_path, reviewed_sha)
    except Exception as exc:
        log.exception("auto-merge: get_mr failed")
        _slack_say(config, f":warning: {project_path} MR !{iid}: AI clean but "
                           f"mergeability check failed ({exc}) — merge manually\n{web_url}", thread_ts)
        return

    blocker = review_common.auto_merge_blocker(mr, reviewed_sha, has_ci)
    if blocker:
        log.info("MR !%s clean but not auto-merged: %s", iid, blocker)
        retry = (blocks.rerun_buttons(project_path, iid, label="🔁 重審最新 commit")
                 if blocker == "new commits since review" else None)
        _slack_say(config, f":warning: {project_path} MR !{iid}: AI review clean but "
                           f"{blocker} — merge manually\n{web_url}", thread_ts, retry)
        try:
            gitlab_client.post_note(base, token, project_path, iid,
                f"🤖 mr-sentinel: AI review 無發現問題,但因「{blocker}」未自動合併,請手動處理。")
        except Exception:
            log.exception("auto-merge: note failed (ignored)")
        return

    try:
        gitlab_client.merge_mr(base, token, project_path, iid, sha=reviewed_sha)
    except Exception as exc:
        log.exception("auto-merge: merge failed")
        _slack_say(config, f":warning: {project_path} MR !{iid}: AI clean but "
                           f"auto-merge failed ({exc}) — merge manually\n{web_url}", thread_ts)
        return

    log.info("MR !%s auto-merged (AI review clean)", iid)
    _slack_say(config, f":white_check_mark: {project_path} MR !{iid} auto-merged "
                       f"(AI review clean)\n{web_url}", thread_ts)
    _mark_notification_merged(config, thread_ts)


MERGED_REACTIONS = ("done", "white_check_mark")


def _mark_notification_merged(config: dict, notification_ts: str | None) -> None:
    """React on the MR notification so a merged MR is visible without opening the thread.

    `:done:` is a custom emoji and may not exist in a workspace (reading the emoji
    list needs `emoji:read`, which the app doesn't have), so fall back on any failure.
    """
    slack = config.get("slack", {})
    if not (notification_ts and slack.get("bot_token") and slack.get("channel_id")):
        return
    for name in MERGED_REACTIONS:
        try:
            slack_client.add_reaction(slack["bot_token"], slack["channel_id"], notification_ts, name)
            return
        except Exception:
            log.exception("merged reaction :%s: failed", name)


def _review_with_confirmation(run, mode: str, auto_merge: bool) -> tuple[dict | None, str]:
    """Run the engine at `mode`; when auto-merge is on and a lite pass comes back
    clean, confirm with a deep pass before anything can merge.

    `run(mode)` returns the engine's result dict, or None if the engine failed.
    Returns (result, mode it came from); result is None only if the first pass
    failed. A failed deep confirmation keeps the lite result, which is never
    merge-eligible.
    """
    result = run(mode)
    if result is None or not (auto_merge and mode == "lite"
                              and review_common.is_clean_result(result)):
        return result, mode
    log.info("lite review clean; deep pass to confirm before auto-merge")
    deep = run("deep")
    if deep is None:
        log.error("deep confirmation failed; not auto-merging")
        return result, mode
    return deep, "deep"


def _run_git(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def prepare_worktree(project_path: str, iid, sha: str, wt: Path,
                     review_cfg: dict) -> tuple[str | None, str | None]:
    """Fetch the MR ref into the user's clone and check `sha` out into a
    throwaway worktree. Returns (local clone, None) or (local, failure) where
    failure is "no_clone" / "fetch" / "worktree". Shared with appeal.py."""
    local = review_common.resolve_local_path(project_path, review_cfg)
    if not local or not Path(local).exists():
        log.error("local clone not found for %s", project_path)
        return local, "no_clone"
    fetch = _run_git(["git", "-C", local, "fetch", "-q", "origin",
                      f"+refs/merge-requests/{iid}/head:refs/mr-sentinel/{iid}"])
    if fetch.returncode != 0:
        log.error("git fetch failed: %s", fetch.stderr)
        return local, "fetch"
    _run_git(["git", "-C", local, "worktree", "remove", "--force", str(wt)])  # clear leftovers
    add = _run_git(["git", "-C", local, "worktree", "add", "--detach", "-q", str(wt), sha])
    if add.returncode != 0:
        log.error("worktree add failed: %s", add.stderr)
        return local, "worktree"
    return local, None


def _remove_worktree(local: str, wt: Path) -> None:
    """Cleanup runs after the completion message, so a git timeout here must not
    turn a finished review into a crash (main() would redo it)."""
    try:
        _run_git(["git", "-C", local, "worktree", "remove", "--force", str(wt)])
    except Exception:
        log.exception("worktree cleanup failed: %s", wt)


REVIEW_META = "review_meta.json"


def write_review_meta(work: Path, mode: str, head_sha: str, findings: int) -> None:
    """What the last review actually covered. appeal.py reads it: merging after
    accepted appeals must keep the same rails as a clean review (deep, and the
    reviewed sha — never code pushed after the review)."""
    (work / REVIEW_META).write_text(json.dumps(
        {"mode": mode, "head_sha": head_sha, "findings": findings}, indent=1))


def read_review_meta(work: Path) -> dict:
    try:
        meta = json.loads((work / REVIEW_META).read_text())
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def spawn_detached(project_path: str, iid, mr_id, mode: str = "auto", attempt: int = 0) -> None:
    """Launch one review in its own session.

    Shared by the poller (must not block its 60s loop) and the Slack listener
    (must answer a `rerun` within its 15s tick), so neither ever waits on a
    multi-minute model run.
    """
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    # Popen dups the fd for the child, so the parent's copy can be closed right
    # away instead of leaking for the life of the caller
    with open(REVIEWS_DIR / f"{mr_id}.spawn.log", "a") as spawn_log:
        subprocess.Popen(
            [sys.executable, str(SCRIPT_DIR / "reviewer.py"),
             "--project", project_path, "--iid", str(iid), "--mr-id", str(mr_id),
             "--mode", mode, "--attempt", str(attempt)],
            stdout=spawn_log, stderr=subprocess.STDOUT,
            start_new_session=True, cwd=str(SCRIPT_DIR),
        )


def run_review(project_path: str, iid, mr_id, config: dict, state: dict, dry_run: bool,
               force_mode: str = "auto") -> int:
    base = config["gitlab_url"]
    token = config["gitlab_token"]
    review_cfg = config["review"]
    language = review_cfg["language"]
    work = REVIEWS_DIR / str(mr_id)
    work.mkdir(parents=True, exist_ok=True)

    # every Slack message about this MR replies in the original notification's
    # thread; None (webhook mode, or an MR seen before ts tracking) -> top-level
    thread_ts = review_common.slack_ts_for(state, mr_id)

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
        slack = config.get("slack", {})
        if thread_ts and slack.get("bot_token") and slack.get("channel_id"):
            try:
                slack_client.add_reaction(slack["bot_token"], slack["channel_id"], thread_ts, "eyes")
            except Exception:
                log.exception("Slack reaction failed (ignored)")

    # 4. size guard sets the review depth: small -> "lite" (1 gate), large ->
    #    "deep" (3 gates). No MR is dropped; a giant MR that can't finish in time
    #    falls through to the "review did not finish" warning below.
    mode, reason = review_common.plan_review(
        ctx["stats"]["files"], ctx["stats"]["lines"], review_cfg)
    if force_mode in ("lite", "deep"):
        # a human asked for this depth from Slack; the size guard is advisory then
        log.info("MR !%s: %s review (forced, size guard said %s)", iid, force_mode, mode)
        mode = force_mode
    elif mode == "deep":
        log.info("MR !%s large (%s): deep 3-gate review", iid, reason)
    else:
        log.info("MR !%s: lite single-pass review", iid)

    # 5. local clone + fetch MR ref + disposable worktree
    wt = work / "wt"
    local, failure = prepare_worktree(project_path, iid, head_sha, wt, review_cfg)
    if failure == "no_clone":
        if not dry_run:
            _slack_say(config, f":warning: local clone not found for {project_path}, "
                               f"MR !{iid} skipped", thread_ts)
        return 1
    if failure == "fetch":
        if not dry_run:
            _slack_say(config, f":warning: git fetch failed for {project_path} MR !{iid}", thread_ts)
        return 1
    if failure == "worktree":
        if not dry_run:
            # the MR is already claimed (:eyes:) and will never be retried;
            # every failure branch must produce a human-visible signal
            _slack_say(config, f":warning: worktree setup failed for {project_path} MR !{iid}, "
                               f"please review manually\n{ctx.get('web_url')}", thread_ts)
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
            print("engine   :", review_cfg["engine"], f"({engine.label(review_cfg, mode)})")
            print("mode     :", mode)
            print("worktree :", wt)
            print("stats    :", ctx["stats"])
            return 0

        def run(m: str) -> dict | None:
            if out_path.exists():
                out_path.unlink()
            rc = engine.run_review(work, ctx_path, out_path, wt, review_cfg, m)
            if rc != 0:
                log.error("engine failed for MR !%s (%s, rc=%s)", iid, m, rc)
                return None
            return json.loads(out_path.read_text())

        auto_merge = bool(review_cfg.get("auto_merge_on_clean"))
        result, mode = _review_with_confirmation(run, mode, auto_merge)
        if result is None:
            _slack_say(config, f":warning: {project_path} MR !{iid} review did not finish, "
                               f"please review manually\n{ctx.get('web_url')}", thread_ts,
                       blocks.rerun_buttons(project_path, iid, label="🔁 再試一次"))
            return 1

        # 7. post comments (scripts post; the AI never does)
        findings = result.get("findings", [])
        posted = post_comment.post_findings(
            base, token, project_path, iid, findings, ctx["diff_refs"],
            signature=build_signature(engine.label(review_cfg, mode)))

        write_review_meta(work, mode, head_sha, len(findings))
        # into the db -> blame -> follow-ups -> every score -> change log (never raises)
        history_sync.sync_mr(config, project_path, iid,
                             trigger=f"review 完成 {project_path.rsplit('/', 1)[-1]}!{iid}"
                                     f"({len(findings)} 則)")
        # the scorecard: 1-5 per category for this MR, from the same context (never raises)
        history_rate.rate_mr(config, project_path, iid, ctx=ctx)

        # 8. completion message
        buttons = blocks.rerun_buttons(project_path, iid,
                                       label="🔁 修好了，重審" if findings else "🔁 重審")
        if posted:
            buttons += blocks.appeal_buttons(project_path, iid)
        text = completion_text(project_path, iid, ctx.get("web_url"),
                               findings, posted, language, mode)
        if posted < len(findings):
            text += (f"\n:warning: {len(findings) - posted} 則留言沒發成功"
                     f"(GitLab 回錯誤),可以按重審補上")
        _slack_say_verdict(config, text, findings, thread_ts, buttons)
        log.info("MR !%s reviewed: %s comment(s) posted", iid, posted)

        # 9. auto-merge on a clean deep review (opt-in; hard-railed). The review is
        #    announced by now: a crash here must not read as a failed review, which
        #    main() would answer by deleting the comments and reviewing again.
        try:
            if auto_merge and review_common.auto_merge_eligible(mode, result):
                _maybe_auto_merge(config, base, token, project_path, iid, ctx.get("web_url"),
                                  thread_ts, reviewed_sha=ctx["diff_refs"]["head_sha"])
        except Exception:
            log.exception("auto-merge step of MR !%s crashed after the review was announced", iid)
        return 0
    finally:
        _remove_worktree(local, wt)


AUTO_RETRY_LIMIT = 1


def reset_for_rerun(config: dict, project_path: str, iid) -> bool:
    """Give a claimed MR back to the reviewer: drop our :eyes: and our unanswered
    comments (a rerun would otherwise stack a second round on the first).

    Same steps as slack_bot's rerun, minus Slack. Comments are deleted only once the
    history db has them. Without that record nothing is touched and False comes
    back: keeping the comments but rerunning would post a second round on top.
    """
    base, token = config["gitlab_url"], config["gitlab_token"]
    me = gitlab_client.get_current_user(base, token)["id"]
    if not history_sync.sync_mr(config, project_path, iid,
                                trigger=f"自動重試前紀錄 {project_path.rsplit('/', 1)[-1]}!{iid}"):
        return False
    for emoji in gitlab_client.get_award_emojis(base, token, project_path, iid):
        if emoji.get("name") == "eyes" and (emoji.get("user") or {}).get("id") == me:
            gitlab_client.delete_award_emoji(base, token, project_path, iid, emoji["id"])
    discussions = gitlab_client.list_discussions(base, token, project_path, iid)
    for note_id in review_common.deletable_ai_notes(discussions, me):
        gitlab_client.delete_note(base, token, project_path, iid, note_id)
    return True


def _retry_automatically(config: dict, state: dict, project_path: str, iid, mr_id,
                         attempt: int, exc: Exception, mode: str = "auto") -> bool:
    """One more try for a crash, so a transient GitLab/network error does not wait
    for a human. Returns False when out of attempts or the reset itself failed;
    the caller then reports the crash with a manual retry button."""
    if attempt >= AUTO_RETRY_LIMIT:
        return False
    try:
        if not reset_for_rerun(config, project_path, iid):
            log.error("automatic retry of MR !%s skipped: review history could not be written", iid)
            return False
        spawn_detached(project_path, iid, mr_id, mode, attempt=attempt + 1)
    except Exception:
        log.exception("automatic retry of MR !%s could not start", iid)
        return False
    _slack_say(config, f":repeat: {project_path} MR !{iid} review 中途失敗 "
                       f"({type(exc).__name__}: {str(exc)[:120]}),自動重試中",
               review_common.slack_ts_for(state, mr_id))
    return True


SPAWN_LOCK_WAIT = 10
RETRY_LOCK_WAIT = 60


def take_lock(lock_file, wait: float = 0) -> bool:
    """Take the per-MR lock, polling for up to `wait` seconds.

    A review is spawned while its spawner (the Slack rerun, or a crashed review's
    automatic retry) still holds the lock around its reset, so the child waits for
    that to end instead of mistaking the spawner for a review already running."""
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.5)


def _report_crash(config: dict, state: dict, project_path: str, iid, mr_id, exc: Exception) -> None:
    _slack_say(config, f":boom: {project_path} MR !{iid} review 中途失敗 "
                       f"({type(exc).__name__}: {str(exc)[:120]}),已完成的部分保留在 GitLab;"
                       f"可以按重審再試",
               review_common.slack_ts_for(state, mr_id),
               blocks.rerun_buttons(project_path, iid, label="🔁 再試一次"))


def main() -> int:
    ap = argparse.ArgumentParser(description="mr-sentinel single-MR reviewer")
    ap.add_argument("--project", required=True, help="project path (group/name)")
    ap.add_argument("--iid", required=True)
    ap.add_argument("--mr-id", required=True)
    ap.add_argument("--mode", choices=("auto", "lite", "deep"), default="auto",
                    help="force review depth (default: pick by MR size)")
    ap.add_argument("--attempt", type=lambda v: max(int(v), 0), default=0,
                    help="automatic retries already spent on this MR (set by the reviewer itself)")
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

    try:
        config = load_config()
    except (Exception, SystemExit):
        # without a usable config there is no Slack token either; reviewer.log is the only trace
        log.exception("config unusable, review of MR !%s not started", args.iid)
        return 1
    try:
        state = load_state() or {}
    except (OSError, ValueError):
        # only costs us the Slack thread; the crash warning below must still reach someone
        log.exception("state.json unreadable; replying top-level")
        state = {}

    lock_path = REVIEWS_DIR / f".lock-{args.mr_id}"
    with open(lock_path, "w") as lock_file:
        if not take_lock(lock_file, RETRY_LOCK_WAIT if args.attempt > 0 else SPAWN_LOCK_WAIT):
            log.info("review for MR !%s already running, skipping", args.iid)
            return 0
        try:
            return run_review(args.project, args.iid, args.mr_id, config, state, args.dry_run,
                              force_mode=args.mode)
        except Exception as exc:
            # the MR may already carry our :eyes: (claimed = never retried by the
            # poller), so a log-only failure would leave it silently unreviewed
            log.exception("review of MR !%s failed", args.iid)
            if args.dry_run:
                return 1
            # reset + spawn stay under the lock; a Slack rerun refuses while it is held
            if not _retry_automatically(config, state, args.project, args.iid, args.mr_id,
                                        args.attempt, exc, args.mode):
                _report_crash(config, state, args.project, args.iid, args.mr_id, exc)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
