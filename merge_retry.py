#!/usr/bin/env python3
"""mr-sentinel merge retry: the 🔀 / ✅ buttons on an MR that did not merge.

Spawned detached by the Slack listener, under the reviewer's per-MR lock. The merge
itself is reviewer._maybe_auto_merge with every rail; this only decides whether the
last review entitles the MR to one, by the rule appeal.py applies: every AI thread
closed, and a deep review on the head being merged.

`--waived-by` (✅, admin only) first overrides what is still open, for when the
developer says a comment needs no fix and the AI keeps disagreeing: every open AI
thread is answered and resolved in the admin's name, the override is bound to this
head and target in review_meta.json, and a note on the MR says who released what.
"""
import argparse
import fcntl
import logging
import sys
import subprocess
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

import blocks
import commands
import gitlab_client
from history import parse as history_parse
import review_common
import reviewer
import slack_client
from sentinel_config import SCRIPT_DIR, load_config, load_state

REVIEWS_DIR = reviewer.REVIEWS_DIR


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


def _override_threads(base: str, token: str, project: str, iid, name: str) -> list[str]:
    """Answer every AI thread still open with the admin's override and resolve it;
    returns one audit line per thread touched.

    Marked accepted the way an appeal is, so a thread GitLab fails to resolve stays
    "accepted" (open, blocking) and is retried by the next press."""
    me = gitlab_client.get_current_user(base, token)["id"]
    lines = []
    for discussion in gitlab_client.list_discussions(base, token, project, iid):
        state = review_common.discussion_status(discussion, me)
        if state in (None, "closed"):
            continue
        did = str(discussion.get("id"))
        title = history_parse.parse_comment(
            review_common._human_notes(discussion)[0].get("body")).get("title") or did
        try:
            if state != "accepted":
                gitlab_client.reply_discussion(base, token, project, iid, did,
                    f"✅ **{name} 裁定放行**,這則不用改。\n\n{review_common.APPEAL_ACCEPT}")
            gitlab_client.resolve_discussion(base, token, project, iid, did)
            lines.append(f"- {title}(原本:{STATE_WORDS.get(state, state)})")
        except Exception:
            log.exception("override: thread %s on !%s not closed", did, iid)
            lines.append(f"- {title}(沒關成功,下次按會再試)")
    return lines


STATE_WORDS = {"unanswered": "沒人回覆", "rejected": "申訴被駁回",
               "appeal": "有回覆還沒判", "accepted": "已接受但沒關成功"}


def _override(config: dict, project: str, mr: dict, work, user_id: str, say) -> bool:
    """Answer what is still open in an admin's name. False (after saying why) when it
    may not be given; the merge must not follow then.

    The Slack listener already authorized the click; the admin check is repeated
    here because this script can be started by hand with any `--waived-by`. Only a
    review that finished on this head and target can be overridden: the override
    closes comments, and never stands in for a check that did not run
    (reviewer._release_blocker re-checks everything at merge)."""
    base, token = config["gitlab_url"], config["gitlab_token"]
    if not commands.is_admin(user_id, config.get("slack", {})):
        say(f":no_entry: `{project}` !{mr['iid']}:<@{user_id}> 不是 admin,不能裁定放行")
        return False
    meta = reviewer.read_review_meta(work)
    guard = meta.get("guard")
    if meta.get("head_sha") != mr.get("sha") or (
            guard is not None and not (reviewer._same_change(guard, mr) and guard.get("ok"))):
        say(f":no_entry: `{project}` !{mr['iid']}:最新的 commit 還沒完整 review 過,"
            f"按「🔁 重審」後再看", blocks.rerun_buttons(project, mr["iid"], label="🔁 重審"))
        return False
    name = slack_name(config, user_id)
    reviewer.save_review_meta(work, {**meta, "override": {
        "head_sha": mr["sha"], "target_branch": mr.get("target_branch"), "by": name,
        "by_id": user_id, "at": datetime.now(timezone.utc).isoformat()}})
    lines = _override_threads(base, token, project, mr["iid"], name)
    try:
        gitlab_client.post_note(base, token, project, mr["iid"],
            f"🤖 mr-sentinel: {name}(Slack `{user_id}`)裁定放行 commit `{mr['sha']}` "
            f"合進 `{mr.get('target_branch')}`,以下 AI 留言標成不用改:\n\n"
            + ("\n".join(lines) or "- (沒有還開著的留言)"))
        noted = ""
    except Exception:
        log.exception("override: MR note failed (the meta holds it)")
        noted = "\n:warning: GitLab 上的放行紀錄留言沒寫成功(本機紀錄有)"
    say(f":white_check_mark: 已記錄 <@{user_id}> 裁定放行 `{project}` !{mr['iid']}"
        f"({len(lines)} 則留言)。*還沒合併*,接著檢查 CI 和 GitLab 的合併條件{noted}")
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
    if waived_by and not _override(config, project, mr, work, waived_by, say):
        return 0
    web_url = mr.get("web_url")
    override = reviewer.read_review_meta(work).get("override") or {}
    if not waived_by and override.get("head_sha") == mr.get("sha"):
        # retried on every press: a thread GitLab failed to close must not hold forever
        _override_threads(base, token, project, iid, override["by"])
    # the release status follows only its own comments, whatever else is still open
    reviewer.sync_release_status(config, base, token, project, mr)
    me = gitlab_client.get_current_user(base, token)["id"]
    _, counts = review_common.collect_appeals(
        gitlab_client.list_discussions(base, token, project, iid), me)
    still_open = review_common.open_findings(counts)
    if still_open:
        say(f":warning: `{project}` !{iid} 還有 {still_open} 則 AI 留言沒處理完,不合併 — "
            f"修好就按重審,不用修就回覆後按「{blocks.APPEAL_LABEL}」\n{web_url or ''}",
            blocks.rerun_buttons(project, iid, label="🔁 修好了，重審")
            + blocks.appeal_buttons(project, iid) + blocks.waive_buttons(project, iid))
        return 0
    if not config["review"].get("auto_merge_on_clean"):
        say(f":information_source: `{project}` !{iid}: 自動合併功能沒開,請 admin 開啟"
            f"(`automerge on`)或到 GitLab 手動合併\n{web_url or ''}")
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
