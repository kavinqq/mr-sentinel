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

import artifact_gate
import blocks
import commands
import engines
from history import rate as history_rate
from history import sync as history_sync
import fetch_mr
import final_gates
import gitlab_client
import post_comment
import release_guard
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


GATE_LABEL = "test-marker rule"
GUARD_LABEL = "release guard"


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
                      *, reviewed_sha: str, note_on_block: bool = True) -> None:
    """Merge a clean MR, honoring the hard rails. Every path warns; none raises.

    Rails (in review_common.auto_merge_blocker): never merge a draft, a head
    other than `reviewed_sha`, an MR GitLab won't cleanly merge, or one whose
    head pipeline isn't green. `note_on_block=False` keeps a retry someone just
    pressed from adding the same MR note again.
    """
    retry_merge = blocks.merge_buttons(project_path, iid)
    head = f":warning: `{project_path}` MR !{iid}: AI review 沒問題,但還沒自動合併"
    try:
        mr = gitlab_client.get_mr(base, token, project_path, iid)
        has_ci = gitlab_client.has_ci_config(base, token, project_path, reviewed_sha)
    except Exception as exc:
        log.exception("auto-merge: get_mr failed")
        _slack_say(config, f"{head}\n*原因*:查 GitLab 合併狀態失敗({type(exc).__name__})\n"
                           f"*下一步*:等一下再{RETRY_MERGE}\n{web_url}", thread_ts, retry_merge)
        return

    blocker = (review_common.auto_merge_blocker(mr, reviewed_sha, has_ci)
               or _release_blocker(config, base, token, project_path, mr))
    if blocker:
        log.info("MR !%s clean but not auto-merged: %s", iid, blocker)
        guard = read_review_meta(REVIEWS_DIR / str(mr.get("id"))).get("guard")
        why, next_step = blocker_text(blocker)
        details, gate = blocker_details(base, token, project_path, mr, blocker, guard,
                                        commands.admins(config.get("slack", {})))
        next_step = GATE_NEXT_STEP.get(gate, next_step)
        if blocker == "new commits since review":
            buttons = blocks.rerun_buttons(project_path, iid, label="🔁 重審最新 commit")
        else:
            buttons = ((blocks.rerun_buttons(project_path, iid, label="🔁 重審")
                        if _needs_rerun(blocker) or gate == "rerun" else [])
                       + retry_merge
                       + (blocks.waive_buttons(project_path, iid) if gate == "waivable" else []))
        _slack_say(config, f"{head}\n*原因*:{why}\n"
                           + "".join(f"{line}\n" for line in details)
                           + f"*下一步*:{next_step}\n{web_url}", thread_ts, buttons)
        if note_on_block:
            try:
                gitlab_client.post_note(base, token, project_path, iid,
                    f"🤖 mr-sentinel: AI review 無發現問題,但因「{why}」還沒自動合併,"
                    f"原因與按鈕在 Slack 討論串。")
            except Exception:
                log.exception("auto-merge: note failed (ignored)")
        return

    try:
        gitlab_client.merge_mr(base, token, project_path, iid, sha=reviewed_sha)
    except Exception as exc:
        log.exception("auto-merge: merge failed")
        _slack_say(config, f"{head}\n*原因*:GitLab 拒絕合併({str(exc)[:120]})\n"
                           f"*下一步*:到 GitLab 看 MR 狀態,處理後{RETRY_MERGE}\n{web_url}",
                   thread_ts, retry_merge)
        return

    log.info("MR !%s auto-merged (AI review clean)", iid)
    _slack_say(config, f":white_check_mark: {project_path} MR !{iid} auto-merged "
                       f"(AI review clean)\n{web_url}", thread_ts)
    _mark_notification_merged(config, thread_ts)


def without_covered(ai: list, gate_findings: list) -> list:
    """Review findings minus those on a line a final gate already reported."""
    covered = {(f["file"], n) for f in gate_findings for n in f.get("hit_lines") or [f.get("line")]}
    return [f for f in ai if (f.get("file"), f.get("line")) not in covered]


def _set_status(config: dict, project_path: str, sha: str, ref: str | None, web_url: str | None,
                state: str, description: str) -> None:
    """Publish the final-gate verdict on the commit; GitLab can then refuse the merge
    ("Pipelines must succeed"). A failed write is logged, never raised: the Slack and
    MR-comment signals still go out, and auto-merge has its own rail."""
    try:
        gitlab_client.set_commit_status(config["gitlab_url"], config["gitlab_token"], project_path,
                                        sha, state, final_gates.STATUS_NAME, description,
                                        web_url or "", ref or "")
    except Exception:
        log.exception("commit status %s for %s@%s not set", state, project_path, sha)


def _fail_status(config: dict, project_path: str, iid, reason: str) -> None:
    """Best effort: mark the MR's current head failed when the gates could not run."""
    try:
        mr = gitlab_client.get_mr(config["gitlab_url"], config["gitlab_token"], project_path, iid)
        _set_status(config, project_path, mr["sha"], mr.get("source_branch"), mr.get("web_url"),
                    "failed", f"正式分支額外 review 未完成:{reason}"[:255])
    except Exception:
        log.exception("could not mark MR !%s failed", iid)


def _post_gate_findings(config: dict, engine, project_path: str, iid, ctx: dict,
                        findings: list) -> int:
    """Rule hits and guard leaks go up under their own signatures."""
    base, token, review_cfg = config["gitlab_url"], config["gitlab_token"], config["review"]
    groups = [
        ([f for f in findings if f.get("gate") == artifact_gate.GATE_NAME],
         build_signature(GATE_LABEL)),
        ([f for f in findings if f.get("gate") == release_guard.GATE_NAME],
         build_signature(f"{GUARD_LABEL} · {engine.label(review_cfg, 'lite')}")),
    ]
    posted = 0
    for items, signature in groups:
        if items:
            posted += post_comment.post_findings(base, token, project_path, iid, items,
                                                 ctx["diff_refs"], signature=signature)
    return posted


def _main_branch(base: str, token: str, project_path: str) -> str | None:
    """The project's default branch, or None when GitLab cannot say (final_gates.applies
    then gates the MR anyway)."""
    try:
        return gitlab_client.get_project(base, token, project_path).get("default_branch")
    except Exception:
        log.exception("default branch of %s unavailable; gating the MR", project_path)
        return None


def _release_blocker(config: dict, base: str, token: str, project_path: str,
                     mr: dict) -> str | None:
    """Final rail before any auto-merge, including one reached through accepted
    appeals: the marker rule is re-run on the head about to merge, and the release
    guard must have passed that exact head. No AI verdict can waive either, and
    anything that cannot be confirmed fails closed."""
    review_cfg = config.get("review", {})
    if not final_gates.applies(mr.get("target_branch"), review_cfg,
                               _main_branch(base, token, project_path)):
        return None
    problem = release_guard.config_problem(review_cfg.get("release_guard", {}))
    if problem:
        return problem
    local = review_common.resolve_local_path(project_path, review_cfg)
    try:
        api = gitlab_client.get_mr_changes(base, token, project_path, mr["iid"]).get("changes") or []
    except Exception:
        log.exception("auto-merge: MR changes for the test-marker rule unavailable")
        api = None
    diff_refs = mr.get("diff_refs") or {}
    changes = artifact_gate.collect(local, diff_refs, mr.get("target_branch") or "", api)
    if changes is None:
        return "test-marker rule could not read the diff"
    exempt = artifact_gate.exempt_test_files(local, diff_refs.get("head_sha", ""),
                                             [c["new_path"] for c in changes if c.get("new_path")])
    hits = artifact_gate.scan(changes, review_cfg.get("language", "en"), exempt)
    if hits:
        return f"test-only code in {len(hits)} file(s)"
    guard = read_review_meta(REVIEWS_DIR / str(mr.get("id"))).get("guard")
    if not guard:
        return ("正式分支額外 review 沒跑過這個 MR(可能是 review 後才改成合進 "
                f"`{mr.get('target_branch')}`),需要人工確認或按重審")
    if guard.get("target_branch") != mr.get("target_branch"):
        return (f"正式分支額外 review 是針對合進 `{guard.get('target_branch')}` 跑的,"
                f"MR 現在要合進 `{mr.get('target_branch')}`,需要人工確認或按重審")
    # an admin's sign-off (merge_retry) stands in for the guard's verdict, never for
    # the marker rule above, and only on the head it was given for
    waived = bool(guard.get("waived_by"))
    if guard.get("head_sha") != mr.get("sha") or not (guard.get("ok") or waived):
        return "release guard has not passed this commit"
    if guard.get("leaks") and not waived:
        return f"release guard found {guard['leaks']} test leak(s)"
    return None


def waive_problem(guard: dict | None, mr: dict) -> str | None:
    """Why an admin may not sign off the release gate's hold on this MR, or None.

    Only what the gate was unsure about, or could not finish, is a human's call: a
    sure finding needs a fix, and a verdict on another head or target a rerun. A
    record from before `sure` was kept cannot tell the two apart, so it refuses."""
    if not guard or guard.get("head_sha") != mr.get("sha") \
            or guard.get("target_branch") != mr.get("target_branch"):
        return "正式分支額外 review 的結果不是針對目前這個 commit 和目標分支,請按重審"
    if guard.get("sure") is None:
        return "這筆正式分支額外 review 紀錄分不出確定和不確定的問題,請按重審"
    if guard["sure"]:
        return f"正式分支額外 review 有 {guard['sure']} 個確定的問題,不能放行,要修好再重審"
    return None


def gate_waivable(guard: dict | None, mr: dict) -> bool:
    """Whether to offer the admin sign-off button: the gate holds this head, and
    nothing it found is sure."""
    return (waive_problem(guard, mr) is None and not guard.get("waived_by")
            and (not guard.get("ok") or bool(guard.get("leaks"))))


RETRY_MERGE = f"按「{blocks.MERGE_LABEL}」"
RERUN_HINT = "按「🔁 重審」"


def blocker_text(blocker: str) -> tuple[str, str]:
    """(why, what to do next) for an auto-merge blocker, in words a developer reads.
    The blocker codes stay as they are for logs and tests; unknown ones pass through."""
    if blocker == "new commits since review":
        return "review 之後又推了新的 commit", "按「🔁 重審最新 commit」"
    if blocker == "draft MR":
        return "MR 還是草稿(Draft)", f"到 GitLab 取消 Draft,再{RETRY_MERGE}"
    if blocker.startswith("not mergeable"):
        return (f"GitLab 現在不讓合併({blocker[len('not mergeable'):].strip()},"
                f"常見是有衝突,或 GitLab 還在計算)", f"解掉衝突或等一下,再{RETRY_MERGE}")
    if blocker == "pipeline missing":
        return "這個 commit 還沒有 CI 結果", f"等 CI 跑完,再{RETRY_MERGE}"
    if blocker == "pipeline is for an older commit":
        return "CI 結果是舊 commit 的", f"等這個 commit 的 CI 跑完,再{RETRY_MERGE}"
    if blocker.startswith("pipeline "):
        status = blocker[len("pipeline "):]
        if status in ("pending", "running", "created", "waiting_for_resource", "preparing"):
            return f"CI 還在跑({status})", f"等跑完,再{RETRY_MERGE}"
        return f"CI / 檢查沒過({status})", f"看上面沒過的項目,修好或重跑成功後{RETRY_MERGE}"
    if blocker == "test-marker rule could not read the diff":
        return "合併前的測試字樣檢查讀不到完整 diff", f"{RERUN_HINT};還是不行請找 admin"
    if blocker.startswith("test-only code in"):
        return (f"合併前檢查發現{blocker[len('test-only code in'):]}有測試字樣"
                f"(例如「測試用」「DO NOT MERGE」)", "拿掉後推新的 commit")
    if blocker == "release guard has not passed this commit":
        return "正式分支額外 review 沒有通過目前這個 commit", f"{RERUN_HINT},或請 admin 確認後放行"
    if blocker.startswith("release guard found"):
        return "正式分支額外 review 有項目要處理", "看下面的項目"
    if "請按重審" in blocker or "按重審" in blocker:
        return blocker, RERUN_HINT
    return blocker, f"處理後{RETRY_MERGE}"


def _needs_rerun(blocker: str) -> bool:
    return blocker in ("test-marker rule could not read the diff",
                       "release guard has not passed this commit") or "重審" in blocker


def blocker_details(base: str, token: str, project_path: str, mr: dict, blocker: str,
                    guard: dict | None, admins: list[str] = ()) -> tuple[list[str], str | None]:
    """Lines spelling out an auto-merge blocker: each failed job or external status
    behind a red pipeline, and what the release gate wants a human to confirm.

    Returns (lines, gate): gate is None when the release gate is not what holds
    the MR, else "waivable" (an admin may sign off), "waived", or "rerun" (the
    gate verdict cannot be signed off as it stands). Never raises; a failed
    lookup says so instead."""
    lines = []
    red_pipeline = blocker.startswith("pipeline ") and blocker not in (
        "pipeline missing", "pipeline is for an older commit")
    if red_pipeline and not blocker_text(blocker)[0].startswith("CI 還在跑"):
        try:
            statuses = gitlab_client.list_commit_statuses(base, token, project_path, mr["sha"])
        except Exception as exc:
            log.exception("auto-merge: commit statuses of !%s unavailable", mr.get("iid"))
            statuses = []
            lines.append(f"• 查不到是哪一項沒過({type(exc).__name__})")
        for s in statuses:
            if s.get("status") in ("success", "skipped", "manual", "running", "pending", "created"):
                continue
            if s.get("name") == final_gates.STATUS_NAME:
                description = s.get("description") or ""
                lines.append(f"• {description}" if description.startswith("正式分支")
                             else f"• 正式分支額外 review 沒過:{description}")
            else:
                lines.append(f"• CI job `{s.get('name')}` {s.get('status')}"
                             + (f":{s['description']}" if s.get("description") else "")
                             + (f" {s['target_url']}" if s.get("target_url") else ""))
        if not any(line.startswith(("• CI job", "• 正式分支")) for line in lines):
            pipeline = mr.get("head_pipeline") or mr.get("pipeline") or {}
            lines.append(f"• GitLab 說 CI 沒過,但沒列出是哪一項,請打開 pipeline 看:"
                         f"{pipeline.get('web_url') or mr.get('web_url') or ''}")
    held_by_gate = any(line.startswith("• 正式分支") for line in lines) \
        or blocker.startswith("release guard")
    if not held_by_gate:
        return lines, None
    problem = waive_problem(guard, mr)
    if problem:
        lines.append(f"_{problem}_")
        return lines, "rerun"
    checks = guard.get("human_checks") or []
    if checks:
        lines.append(f":warning: *需要人工確認({len(checks)})*:")
        lines += [f"• {c}" for c in checks]
    if guard.get("waived_by"):
        return lines, "waived"
    lines.append(waive_hint(project_path, mr.get("iid"), admins))
    return lines, "waivable"


GATE_NEXT_STEP = {
    "rerun": RERUN_HINT + ",讓正式分支額外 review 重新檢查目前這個 commit",
    "waivable": f"請 admin 確認上面的項目後按「{blocks.WAIVE_LABEL}」",
    "waived": f"已有人工放行,等 GitLab 更新狀態後{RETRY_MERGE}",
}


def waive_hint(project_path: str, iid, admins) -> str:
    who = " ".join(f"<@{a}>" for a in admins) or "admin"
    return (f"_請 admin({who})確認上面的項目後按「{blocks.WAIVE_LABEL}」;"
            f"看不到按鈕就在這裡輸入 `@{commands.BOT_HANDLE} waive {project_path}!{iid}`_")


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


def write_review_meta(work: Path, mode: str, head_sha: str, findings: int,
                      guard: dict | None = None) -> None:
    """What the last review actually covered. appeal.py reads it: merging after
    accepted appeals must keep the same rails as a clean review (deep, and the
    reviewed sha — never code pushed after the review). `guard` is the release
    guard's verdict (final_gates.Outcome.record, plus `waived_by` once an admin
    signs it off in merge_retry) that _release_blocker requires."""
    (work / REVIEW_META).write_text(json.dumps(
        {"mode": mode, "head_sha": head_sha, "findings": findings, "guard": guard}, indent=1))


def save_review_meta(work: Path, meta: dict) -> None:
    """Write back a meta from read_review_meta after changing some of its fields."""
    (work / REVIEW_META).write_text(json.dumps(meta, indent=1))


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
    ref, web_url = ctx.get("source_branch"), ctx.get("web_url")
    # the final gates guard what is about to be released: MRs into a main branch only
    gated = final_gates.applies(ctx.get("target_branch"), review_cfg,
                                _main_branch(base, token, project_path))
    if gated and not dry_run:
        _set_status(config, project_path, head_sha, ref, web_url, "pending", "正式分支額外 review 進行中")
    local, failure = prepare_worktree(project_path, iid, head_sha, wt, review_cfg)
    if failure:
        if not dry_run:
            reason = {"no_clone": f"local clone not found for {project_path}",
                      "fetch": "git fetch failed",
                      "worktree": "worktree setup failed"}[failure]
            # the MR is already claimed (:eyes:) and will never be retried;
            # every failure branch must produce a human-visible signal
            if gated:
                _set_status(config, project_path, head_sha, ref, web_url, "failed",
                            f"正式分支額外 review 未完成:{reason}")
                reason += "。正式分支額外 review 也沒跑,*需要人工確認*沒有測試內容或可疑的地方"
            _slack_say(config, f":warning: {project_path} MR !{iid}: {reason}, "
                               f"please review manually\n{web_url}", thread_ts)
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

        # 6b. the final gates run whatever the review said: they are the last check
        #     before release, so a failed review must not skip them
        outcome = (final_gates.run(engine, work, ctx, local, wt, review_cfg) if gated
                   else final_gates.NOT_APPLICABLE)
        gate_findings = outcome.findings
        # an ungated review leaves no verdict: if the MR is retargeted to a main
        # branch later, _release_blocker must not read "not applicable" as "passed"
        guard_record = outcome.record(head_sha, ctx.get("target_branch")) if gated else None
        if gated:
            _set_status(config, project_path, head_sha, ref, web_url, *final_gates.status(outcome))
        if not outcome.passed:
            gate_buttons = [] if outcome.ok else blocks.rerun_buttons(project_path, iid,
                                                                      label="🔁 重審")
            gate_text = final_gates.slack_text(project_path, iid, web_url,
                                               ctx.get("target_branch"), outcome)
            if gate_waivable(guard_record, {"sha": head_sha,
                                            "target_branch": ctx.get("target_branch")}):
                gate_buttons += blocks.waive_buttons(project_path, iid)
                gate_text += "\n" + waive_hint(project_path, iid,
                                                commands.admins(config.get("slack", {})))
            _slack_say(config, gate_text, thread_ts, gate_buttons or None)

        if result is None:
            _post_gate_findings(config, engine, project_path, iid, ctx, gate_findings)
            write_review_meta(work, mode, head_sha, len(gate_findings), guard_record)
            _slack_say(config, f":warning: {project_path} MR !{iid} review did not finish, "
                               f"please review manually\n{web_url}", thread_ts,
                       blocks.rerun_buttons(project_path, iid, label="🔁 再試一次"))
            return 1

        # 7. post comments (scripts post; the AI never does)
        ai_findings = without_covered(result.get("findings", []), gate_findings)
        findings = gate_findings + ai_findings
        result["findings"] = findings
        posted = _post_gate_findings(config, engine, project_path, iid, ctx, gate_findings)
        posted += post_comment.post_findings(
            base, token, project_path, iid, ai_findings, ctx["diff_refs"],
            signature=build_signature(engine.label(review_cfg, mode)))

        write_review_meta(work, mode, head_sha, len(findings), guard_record)
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
        text = completion_text(project_path, iid, web_url, findings, posted, language, mode)
        if posted < len(findings):
            text += (f"\n:warning: {len(findings) - posted} 則留言沒發成功"
                     f"(GitLab 回錯誤),可以按重審補上")
        _slack_say_verdict(config, text, findings, thread_ts, buttons)
        log.info("MR !%s reviewed: %s comment(s) posted", iid, posted)

        # 9. auto-merge on a clean deep review (opt-in; hard-railed). The review is
        #    announced by now: a crash here must not read as a failed review, which
        #    main() would answer by deleting the comments and reviewing again.
        try:
            if auto_merge and outcome.passed and review_common.auto_merge_eligible(mode, result):
                _maybe_auto_merge(config, base, token, project_path, iid, web_url,
                                  thread_ts, reviewed_sha=head_sha)
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
            _fail_status(config, args.project, args.iid, f"{type(exc).__name__}")
            if not _retry_automatically(config, state, args.project, args.iid, args.mr_id,
                                        args.attempt, exc, args.mode):
                _report_crash(config, state, args.project, args.iid, args.mr_id, exc)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
