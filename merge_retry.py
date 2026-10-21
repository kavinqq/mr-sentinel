#!/usr/bin/env python3
"""mr-sentinel merge retry: the 🔀 / ✅ buttons on a clean MR that did not merge.

Spawned detached by the Slack listener, under the reviewer's per-MR lock. The merge
itself is reviewer._maybe_auto_merge with every rail; this only decides whether the
last review entitles the MR to one, by the rule appeal.py applies: every AI thread
closed, and a deep review on the head being merged.

`--waived-by` first records an admin's sign-off on what the release gate was unsure
about (reviewer.waive_problem says when that is allowed). The sign-off is bound to
the head it was given for, becomes the gate's commit status, and is left on the MR
as a note, so a waived release always says who released it.
"""
import argparse
import fcntl
import logging
import sys
import subprocess
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

import blocks
import commands
import final_gates
import gitlab_client
import release_guard
import review_common
import reviewer
import slack_client
from sentinel_config import SCRIPT_DIR, load_config, load_state

REVIEWS_DIR = reviewer.REVIEWS_DIR
# GitLab recomputes an external pipeline from its statuses asynchronously
PIPELINE_SETTLE_TRIES = 5
PIPELINE_SETTLE_SECONDS = 2

log = logging.getLogger("mr_sentinel.merge_retry")


def _buttons(project: str, iid) -> list[dict]:
    return blocks.rerun_buttons(project, iid, label="🔁 重審") + blocks.merge_buttons(project, iid)


def slack_name(config: dict, user_id: str) -> str:
    """Display name for the MR note; the bare id when Slack cannot say (users.info
    needs `users:read`, which the app may lack)."""
    try:
        user = slack_client.users_info(config["slack"]["bot_token"], user_id)
        return (user.get("profile") or {}).get("display_name") or user.get("real_name") or user_id
    except Exception:
        log.exception("users.info for %s failed; using the id", user_id)
        return user_id


def is_human_check_thread(discussion: dict, me) -> bool:
    """One of the release gate's unsure items, posted as an MR thread."""
    notes = review_common._human_notes(discussion)
    body = (notes[0].get("body") or "") if notes else ""
    return (review_common.discussion_status(discussion, me) not in (None, "closed")
            and f"{reviewer.GUARD_LABEL} · " in body
            and any(f"**{label}**" in body for label in release_guard.HUMAN_CHECK.values()))


def _close_signed_off_threads(base: str, token: str, project: str, iid, name: str) -> int:
    """The sign-off answers the threads of the items it covers, so they stop holding
    the merge. Each is marked accepted the way an appeal is: a thread GitLab fails
    to resolve stays "accepted" and is retried by the next appeal press."""
    me = gitlab_client.get_current_user(base, token)["id"]
    closed = 0
    for discussion in gitlab_client.list_discussions(base, token, project, iid):
        if not is_human_check_thread(discussion, me):
            continue
        did = str(discussion.get("id"))
        try:
            gitlab_client.reply_discussion(base, token, project, iid, did,
                f"✅ **{name} 已在 Slack 人工確認這一項**,放行。\n\n{review_common.APPEAL_ACCEPT}")
            gitlab_client.resolve_discussion(base, token, project, iid, did)
            closed += 1
        except Exception:
            log.exception("waive: thread %s on !%s not closed", did, iid)
    return closed


def _waive(config: dict, project: str, mr: dict, work, user_id: str, say) -> bool:
    """Record the sign-off. False (after saying why) when it may not be given or did
    not stick; the merge must not follow then.

    The Slack listener already authorized the click; the admin check is repeated
    here because this script can be started by hand with any `--waived-by`."""
    base, token = config["gitlab_url"], config["gitlab_token"]
    if not commands.is_admin(user_id, config.get("slack", {})):
        say(f":no_entry: `{project}` !{mr['iid']} 不能放行:<@{user_id}> 不是 admin")
        return False
    meta = reviewer.read_review_meta(work)
    guard = meta.get("guard")
    problem = reviewer.waive_problem(guard, mr)
    if problem:
        say(f":no_entry: `{project}` !{mr['iid']} 不能放行:{problem}",
            blocks.rerun_buttons(project, mr["iid"], label="🔁 重審"))
        return False
    if not reviewer.gate_waivable(guard, mr):
        return True                          # passed, or signed off already
    name = slack_name(config, user_id)
    try:
        gitlab_client.set_commit_status(base, token, project, mr["sha"], "success",
                                        final_gates.STATUS_NAME,
                                        f"人工確認放行({name})"[:255],
                                        mr.get("web_url") or "", mr.get("source_branch") or "")
    except Exception as exc:
        log.exception("waive: commit status of !%s not set", mr["iid"])
        say(f":boom: `{project}` !{mr['iid']} 放行失敗,GitLab 狀態沒改成"
            f"({type(exc).__name__}),可以再按一次", blocks.waive_buttons(project, mr["iid"]))
        return False
    reviewer.save_review_meta(work, {**meta, "guard": {
        **guard, "waived_by": name, "waived_by_id": user_id,
        "waived_at": datetime.now(timezone.utc).isoformat()}})
    checks = guard.get("human_checks") or []
    try:
        gitlab_client.post_note(base, token, project, mr["iid"],
            f"🤖 mr-sentinel: {name}(Slack `{user_id}`)在 Slack 確認了正式分支額外 review "
            f"需要人工確認的項目,放行 commit `{mr['sha']}` 合進 `{mr.get('target_branch')}`。\n"
            + "".join(f"\n- {c}" for c in checks))
        noted = ""
    except Exception:
        log.exception("waive: MR note failed (the status and meta hold it)")
        noted = "\n:warning: GitLab 上的放行紀錄留言沒寫成功(commit 狀態和本機紀錄都有)"
    say(f":white_check_mark: 已記錄 <@{user_id}> 人工確認 `{project}` !{mr['iid']} 的正式分支額外 review"
        f"({len(checks)} 項),放行 commit `{mr['sha'][:8]}`。*還沒合併*,接著檢查 AI 留言、"
        f"CI 和 GitLab 的合併條件{noted}")
    for _ in range(PIPELINE_SETTLE_TRIES):
        pipeline = gitlab_client.get_mr(base, token, project, mr["iid"]).get("head_pipeline") or {}
        if pipeline.get("status") != "failed":
            break
        time.sleep(PIPELINE_SETTLE_SECONDS)
    return True


def run_merge(project: str, iid, mr_id, config: dict, state: dict,
              waived_by: str | None = None) -> int:
    base, token = config["gitlab_url"], config["gitlab_token"]
    work = REVIEWS_DIR / str(mr_id)
    work.mkdir(parents=True, exist_ok=True)
    thread_ts = review_common.slack_ts_for(state, mr_id)
    say = lambda text, buttons=None: reviewer._slack_say(config, text, thread_ts, buttons)

    mr = gitlab_client.get_mr(base, token, project, iid)
    if mr.get("state") != "opened":
        say(f":information_source: `{project}` !{iid} 已經是 {mr.get('state')},不用再合併")
        return 0
    if waived_by and not _waive(config, project, mr, work, waived_by, say):
        return 0
    web_url = mr.get("web_url")
    guard = reviewer.read_review_meta(work).get("guard") or {}
    if guard.get("waived_by") and guard.get("head_sha") == mr.get("sha"):
        # retried on every press: a thread GitLab failed to close must not hold forever
        closed = _close_signed_off_threads(base, token, project, iid, guard["waived_by"])
        if closed:
            say(f":white_check_mark: GitLab 上 {closed} 則「需要人工確認」的留言已標成已確認")
    if not config["review"].get("auto_merge_on_clean"):
        say(f":information_source: `{project}` !{iid}: 自動合併功能沒開,請 admin 開啟"
            f"(`automerge on`)或到 GitLab 手動合併\n{web_url or ''}")
        return 0
    me = gitlab_client.get_current_user(base, token)["id"]
    _, counts = review_common.collect_appeals(
        gitlab_client.list_discussions(base, token, project, iid), me)
    still_open = review_common.open_findings(counts)
    if still_open:
        say(f":warning: `{project}` !{iid} 還有 {still_open} 則 AI 留言沒處理完,不合併 — "
            f"修好就按重審,不用修就回覆後按「{blocks.APPEAL_LABEL}」\n{web_url or ''}",
            blocks.rerun_buttons(project, iid, label="🔁 修好了，重審")
            + blocks.appeal_buttons(project, iid))
        return 0
    meta = reviewer.read_review_meta(work)
    if meta.get("mode") != "deep" or not meta.get("head_sha"):
        mode = {"lite": "快速審查(lite)"}.get(meta.get("mode"), meta.get("mode") or "未知的審查")
        say(f":information_source: `{project}` !{iid}: 上次是{mode},只有完整審查(deep)"
            f"通過才會自動合併 — 按「🔁 重審」再跑一次\n{web_url or ''}",
            blocks.rerun_buttons(project, iid, label="🔁 重審"))
        return 0
    reviewer._maybe_auto_merge(config, base, token, project, iid, web_url, thread_ts,
                               reviewed_sha=meta["head_sha"], note_on_block=False)
    return 0


def spawn_detached(project: str, iid, mr_id, waived_by: str | None = None) -> None:
    """Same shape as reviewer.spawn_detached: the listener must never wait on GitLab."""
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    args = [sys.executable, str(SCRIPT_DIR / "merge_retry.py"),
            "--project", project, "--iid", str(iid), "--mr-id", str(mr_id)]
    if waived_by:
        args += ["--waived-by", waived_by]
    with open(REVIEWS_DIR / f"{mr_id}.spawn.log", "a") as spawn_log:
        subprocess.Popen(args, stdout=spawn_log, stderr=subprocess.STDOUT,
                         start_new_session=True, cwd=str(SCRIPT_DIR))


def main() -> int:
    ap = argparse.ArgumentParser(description="mr-sentinel: retry the merge of a clean MR")
    ap.add_argument("--project", required=True)
    ap.add_argument("--iid", required=True)
    ap.add_argument("--mr-id", required=True)
    ap.add_argument("--waived-by", help="Slack user id signing off the release gate first")
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

    # same lock file as reviewer.py / appeal.py: a review may be rewriting the meta
    with open(REVIEWS_DIR / f".lock-{args.mr_id}", "w") as lock_file:
        config = load_config()
        try:
            state = load_state() or {}
        except (OSError, ValueError):
            log.exception("state.json unreadable; replying top-level")
            state = {}
        thread_ts = review_common.slack_ts_for(state, args.mr_id)
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("MR !%s busy (review or appeal running), skipping", args.iid)
            reviewer._slack_say(config, f":hourglass: `{args.project}` !{args.iid} 正在 review "
                                        f"或申訴中,跑完後再按一次", thread_ts,
                                _buttons(args.project, args.iid))
            return 0
        try:
            return run_merge(args.project, args.iid, args.mr_id, config, state, args.waived_by)
        except Exception as exc:
            log.exception("merge retry for MR !%s failed", args.iid)
            reviewer._slack_say(config, f":boom: `{args.project}` !{args.iid} 重新合併中途失敗"
                                        f"({type(exc).__name__}: {str(exc)[:120]}),可以再按一次",
                                thread_ts, _buttons(args.project, args.iid))
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
