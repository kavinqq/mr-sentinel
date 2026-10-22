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
import fetch_mr
import gitlab_client
import review_common
import reviewer
from history import sync as history_sync
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
        return _finish(config, project, iid, web_url, verdicts, me, thread_ts, work,
                       deep_gate=lambda: _deep_gate(config, project, iid, work, wt, mr["sha"]))
    finally:
        reviewer._run_git(["git", "-C", local, "worktree", "remove", "--force", str(wt)])


def _resolve(base, token, project, iid, discussion_id) -> None:
    try:
        gitlab_client.resolve_discussion(base, token, project, iid, discussion_id)
    except urllib.error.HTTPError as exc:
        # never trusted: _finish re-reads GitLab, and a thread GitLab still shows
        # open stays "accepted" (= open, retried on the next press, blocks merging)
        log.warning("could not resolve %s on !%s: %s", discussion_id, iid, exc)


DEEP_GATE_CONTEXT, DEEP_GATE_FINDINGS = "confirm_context.json", "confirm_findings.json"


def _deep_gate(config, project, iid, work, wt, wt_sha) -> tuple[str, dict] | None:
    """A deep pass over the MR as it is now: (head sha it covered, engine result).

    None when it did not run to completion, or when the head moved since `wt` was
    checked out (the pass would then cover code other than what the appeals judged).
    Nothing is posted: the model has not seen the appeals, so a finding it repeats
    would only stack a second round on threads the developer already settled."""
    base, token, review_cfg = config["gitlab_url"], config["gitlab_token"], config["review"]
    ctx = fetch_mr.build_context(base, token, project, iid)
    head_sha = ctx["diff_refs"]["head_sha"]
    if head_sha != wt_sha:
        return None
    ctx_path, out_path = work / DEEP_GATE_CONTEXT, work / DEEP_GATE_FINDINGS
    fetch_mr.write_context(ctx_path, ctx)
    if out_path.exists():
        out_path.unlink()
    engine = engines.get_engine(review_cfg["engine"])
    if engine.run_review(work, ctx_path, out_path, wt, review_cfg, "deep") != 0:
        return None
    return head_sha, json.loads(out_path.read_text())


def _finish(config, project, iid, web_url, verdicts, me, thread_ts, work, deep_gate=None) -> int:
    """Report from what GitLab says *now*, not from what we asked it to do, in one
    message: the verdicts, and once nothing is left open, whether it merged and why not.

    `deep_gate` runs the confirmation a lite review needs before it may merge; None
    (no worktree at hand) leaves such an MR for a manual merge."""
    base, token, review_cfg = config["gitlab_url"], config["gitlab_token"], config["review"]
    # accepted appeals stop counting: re-record the MR and re-score everyone
    history_sync.sync_mr(config, project, iid,
                         trigger=f"申訴判斷 {project.rsplit('/', 1)[-1]}!{iid}")
    _, after = review_common.collect_appeals(
        gitlab_client.list_discussions(base, token, project, iid), me)
    still_open = review_common.open_findings(after)
    log.info("appeal on !%s: %s judged, %s still open %s", iid, len(verdicts), still_open,
             dict(after))

    # the release status follows only its own comments, whatever else is still open
    reviewer.sync_release_status(config, base, token, project,
                                 gitlab_client.get_mr(base, token, project, iid))
    merged, report, buttons = False, None, None
    if still_open:
        buttons = _buttons(project, iid) + blocks.waive_buttons(project, iid)
    elif not review_cfg.get("auto_merge_on_clean"):
        report = "*原因*:自動合併功能沒開\n*下一步*:到 GitLab 手動合併"
    else:
        merged, report, buttons = _merge_outcome(config, base, token, project, iid,
                                                 deep_gate, work)
    reviewer._slack_say(config, review_common.appeal_summary_text(
        project, iid, web_url, verdicts, after, merged=merged, merge_report=report),
        thread_ts, buttons or None)
    if merged:
        reviewer._mark_notification_merged(config, thread_ts)
    return 0


def _merge_outcome(config, base, token, project, iid, deep_gate, work
                   ) -> tuple[bool, str | None, list]:
    """Merge once nothing is left open, by the same rule as a clean review: the last
    review deep and on the head being merged. Returns (merged, report, buttons)."""
    meta = reviewer.read_review_meta(work)
    if meta.get("mode") == "deep" and meta.get("head_sha"):
        merged, report, buttons, _ = reviewer.try_auto_merge(
            config, base, token, project, iid, reviewed_sha=meta["head_sha"])
        return merged, report, buttons
    if meta.get("mode") == "lite" and deep_gate:
        return _merge_after_deep_gate(config, base, token, project, iid, deep_gate, work)
    mode = {"lite": "快速審查(lite)"}.get(meta.get("mode"), meta.get("mode") or "未知的審查")
    return (False, f"*原因*:上次是{mode},只有完整審查(deep)通過才會自動合併\n"
                   f"*下一步*:按「🔁 重審」,或到 GitLab 手動合併",
            blocks.rerun_buttons(project, iid, label="🔁 重審"))


def _merge_after_deep_gate(config, base, token, project, iid, deep_gate, work
                           ) -> tuple[bool, str | None, list]:
    """Appeals cleared a lite review's findings. Lite is one gate, so the same rule as a
    clean lite review applies: a deep pass must agree before anything merges."""
    try:
        gate = deep_gate()
    except Exception:
        log.exception("deep confirmation for !%s crashed", iid)
        gate = None
    retry = blocks.rerun_buttons(project, iid, label="🔁 重審")
    if gate is not None and not (isinstance(gate[1], dict)
                                 and isinstance(gate[1].get("findings"), list)):
        gate = None                       # malformed output: neither clean nor a list to show
    if gate is None:
        return (False, "*原因*:合併前的完整審查(deep)確認沒跑完,或 MR 又有新 commit\n"
                       "*下一步*:按「🔁 重審」,或到 GitLab 手動合併", retry)
    head_sha, result = gate
    if not review_common.is_clean_result(result):
        found = [f for f in result["findings"] if isinstance(f, dict)]
        listing = "".join(f"• {review_common.SEVERITY_EMOJI.get(f.get('severity'), '')} "
                          f"{f.get('title', '?')} ({f.get('file', '?')}:{f.get('line') or '?'})\n"
                          for f in found[:5])
        return (False, f"*原因*:合併前的完整審查(deep)確認又發現 {len(found)} 個問題\n"
                       f"{listing}*下一步*:確認後修好按「🔁 重審」,或到 GitLab 手動合併", retry)
    # a later 🔀 retry (merge_retry) needs to know this head passed a deep review
    meta = reviewer.read_review_meta(work)
    reviewer.save_review_meta(work, {**meta, "mode": "deep", "head_sha": head_sha})
    merged, report, buttons, _ = reviewer.try_auto_merge(
        config, base, token, project, iid, reviewed_sha=head_sha)
    return merged, report, buttons


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
