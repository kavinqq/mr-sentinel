#!/usr/bin/env python3
"""mr-sentinel appeal: the AI re-judges findings the developer says need no fix.

Spawned detached by the Slack listener (the 💬 button / `appeal` command). Flow:

per-MR lock (shared with reviewer.py) → threads where a human replied after the
AI's last word → disposable worktree at the MR head → engine.run_appeal (the
only AI step) → reply in each thread with the verdict, resolve the accepted
ones → Slack summary → if nothing is left open, the same hard-railed
auto-merge as a clean review.

Thread state lives on GitLab (see review_common.discussion_status), so pressing
the button again only judges replies that are new since the last verdict.
"""
import argparse
import fcntl
import json
import logging
import subprocess
import sys
import urllib.error
from logging.handlers import RotatingFileHandler

import blocks
import engines
import gitlab_client
import review_common
import reviewer
from sentinel_config import SCRIPT_DIR, load_config, load_state

REVIEWS_DIR = reviewer.REVIEWS_DIR

log = logging.getLogger("mr_sentinel.appeal")


def appeal_label(review_cfg: dict) -> str:
    engine = review_cfg.get("engine", "claude")
    model = (review_cfg.get(engine) or {}).get("model") or f"{engine} default"
    return f"judged by {model}"


def _buttons(project: str, iid) -> list[dict]:
    return (blocks.rerun_buttons(project, iid, label="🔁 修好了，重審")
            + blocks.appeal_buttons(project, iid))


def run_appeal(project: str, iid, mr_id, config: dict, state: dict) -> int:
    base, token = config["gitlab_url"], config["gitlab_token"]
    review_cfg = config["review"]
    work = REVIEWS_DIR / str(mr_id)
    work.mkdir(parents=True, exist_ok=True)
    thread_ts = review_common.slack_ts_for(state, mr_id)
    say = lambda text, buttons=None: reviewer._slack_say(config, text, thread_ts, buttons)

    me = gitlab_client.get_current_user(base, token)["id"]
    discussions = gitlab_client.list_discussions(base, token, project, iid)
    appeals, _ = review_common.collect_appeals(discussions, me)
    retries = review_common.unresolved_accepts(discussions, me)
    if not appeals and not retries:
        say(f":scales: `{project}` !{iid}: 沒有新的開發者回覆要判斷"
            f"(先在 GitLab 的 AI 留言底下回覆理由)")
        return 0
    for discussion_id in retries:          # earlier accept whose resolve was refused
        _resolve(base, token, project, iid, discussion_id)
    if not appeals:
        return _finish(config, project, iid, None, {}, me, thread_ts, work)

    mr = gitlab_client.get_mr(base, token, project, iid)
    web_url = mr.get("web_url")
    wt = work / "appeal-wt"
    local, failure = reviewer.prepare_worktree(project, iid, mr["sha"], wt, review_cfg)
    if failure:
        say(f":warning: `{project}` !{iid}: 申訴判斷無法準備程式碼 ({failure})", _buttons(project, iid))
        return 1

    try:
        ctx_path, out_path = work / "appeal_context.json", work / "appeal_verdicts.json"
        ctx_path.write_text(json.dumps({
            "mr": {"project": project, "iid": iid, "title": mr.get("title"), "web_url": web_url},
            "appeals": appeals}, ensure_ascii=False, indent=1))
        if out_path.exists():
            out_path.unlink()

        engine = engines.get_engine(review_cfg["engine"])
        if engine.run_appeal(work, ctx_path, out_path, wt, review_cfg) != 0:
            log.error("appeal engine failed for !%s", iid)
            say(f":warning: `{project}` !{iid}: 申訴判斷沒跑完,可以再按一次", _buttons(project, iid))
            return 1
        verdicts = review_common.valid_verdicts(json.loads(out_path.read_text()),
                                                [a["id"] for a in appeals])

        # the model ran for minutes: a thread that got another reply meanwhile is
        # judged on stale input, so leave it for the next press
        fresh = {str(d.get("id")): len(review_common._human_notes(d))
                 for d in gitlab_client.list_discussions(base, token, project, iid)}
        snapshot = {a["id"]: a["notes"] for a in appeals}
        for discussion_id in [d for d in verdicts if fresh.get(d) != snapshot[d]]:
            log.info("thread %s changed during the appeal; not answering it", discussion_id)
            del verdicts[discussion_id]

        # scripts post; the AI never does
        label = appeal_label(review_cfg)
        for discussion_id, (verdict, reason) in verdicts.items():
            gitlab_client.reply_discussion(base, token, project, iid, discussion_id,
                                           review_common.appeal_reply_body(verdict, reason, label))
            if verdict == "accept":
                _resolve(base, token, project, iid, discussion_id)
        return _finish(config, project, iid, web_url, verdicts, me, thread_ts, work)
    finally:
        reviewer._run_git(["git", "-C", local, "worktree", "remove", "--force", str(wt)])


def _resolve(base, token, project, iid, discussion_id) -> None:
    try:
        gitlab_client.resolve_discussion(base, token, project, iid, discussion_id)
    except urllib.error.HTTPError as exc:
        # never trusted: _finish re-reads GitLab, and a thread GitLab still shows
        # open stays "accepted" (= open, retried on the next press, blocks merging)
        log.warning("could not resolve %s on !%s: %s", discussion_id, iid, exc)


def _finish(config, project, iid, web_url, verdicts, me, thread_ts, work) -> int:
    """Report from what GitLab says *now*, not from what we asked it to do."""
    base, token, review_cfg = config["gitlab_url"], config["gitlab_token"], config["review"]
    say = lambda text, buttons=None: reviewer._slack_say(config, text, thread_ts, buttons)
    _, after = review_common.collect_appeals(
        gitlab_client.list_discussions(base, token, project, iid), me)
    still_open = review_common.open_findings(after)
    say(review_common.appeal_summary_text(project, iid, web_url, verdicts, after),
        _buttons(project, iid) if still_open else None)
    log.info("appeal on !%s: %s judged, %s still open %s", iid, len(verdicts), still_open,
             dict(after))

    if not still_open and review_cfg.get("auto_merge_on_clean"):
        meta = reviewer.read_review_meta(work)
        if meta.get("mode") == "deep" and meta.get("head_sha"):
            reviewer._maybe_auto_merge(config, base, token, project, iid, web_url, thread_ts,
                                       reviewed_sha=meta["head_sha"])
        else:
            say(f":information_source: `{project}` !{iid}: 理由都成立,但上次是 "
                f"{meta.get('mode') or '未知'} review,依規則只有 deep review 才自動合併 — "
                f"請手動合併\n{web_url or ''}")
    return 0


def spawn_detached(project: str, iid, mr_id) -> None:
    """Same shape as reviewer.spawn_detached: the listener must never wait on a model."""
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    with open(REVIEWS_DIR / f"{mr_id}.spawn.log", "a") as spawn_log:
        subprocess.Popen(
            [sys.executable, str(SCRIPT_DIR / "appeal.py"),
             "--project", project, "--iid", str(iid), "--mr-id", str(mr_id)],
            stdout=spawn_log, stderr=subprocess.STDOUT,
            start_new_session=True, cwd=str(SCRIPT_DIR),
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="mr-sentinel appeal: re-judge disputed findings")
    ap.add_argument("--project", required=True)
    ap.add_argument("--iid", required=True)
    ap.add_argument("--mr-id", required=True)
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

    # same lock file as reviewer.py: never judge while the MR is being reviewed
    with open(REVIEWS_DIR / f".lock-{args.mr_id}", "w") as lock_file:
        config = load_config()
        try:
            state = load_state() or {}
        except (OSError, ValueError):
            # only costs us the Slack thread; the warning below must still reach someone
            log.exception("state.json unreadable; replying top-level")
            state = {}
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # the click already retired its button, so say so rather than vanish
            log.info("MR !%s busy (review or appeal running), skipping", args.iid)
            reviewer._slack_say(config, f":hourglass: `{args.project}` !{args.iid} 正在 review "
                                        f"或申訴中,跑完後再按一次",
                                review_common.slack_ts_for(state, args.mr_id),
                                _buttons(args.project, args.iid))
            return 0
        try:
            return run_appeal(args.project, args.iid, args.mr_id, config, state)
        except Exception as exc:
            # the click already retired its button: a silent crash would leave
            # the user waiting forever, so every failure surfaces with a retry
            log.exception("appeal for MR !%s failed", args.iid)
            reviewer._slack_say(config, f":boom: `{args.project}` !{args.iid} 申訴中途失敗 "
                                        f"({type(exc).__name__}: {str(exc)[:120]}),"
                                        f"已完成的部分保留在 GitLab;可以再按一次",
                                review_common.slack_ts_for(state, args.mr_id),
                                _buttons(args.project, args.iid))
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
