"""The first-time scan: every project's MRs of the last N days (default: the
scoring window, 90 days = the past three months), then everything derived from
them, so the very first dashboard already stands on the whole window.

    python3 scan_history.py                 # all projects in review.project_map
    python3 scan_history.py --days 120
    python3 scan_history.py --project developer/py_backend/pocketsso
    python3 scan_history.py --dry-run       # only count what would be read

`python3 -m history run` does this by itself while the db has never been
scanned (no `initial_scan_at` state), so a new machine needs no extra step.
Safe to run again: every MR is re-read and upserted, nothing is duplicated.
"""
import logging
from datetime import datetime, timedelta, timezone

from history import blame, classify, db, followups, snapshot, sync
import gitlab_client

log = logging.getLogger("mr_sentinel.history")

MAX_ROUNDS = 50          # classify / blame work in batches; stop looping on no progress


def since_for(days: int, now: datetime | None = None) -> str:
    return db.utc((now or datetime.now(timezone.utc)) - timedelta(days=days))


def _drain(step, conn, config, retries_failures: bool) -> tuple[int, int]:
    """Run a batched step until nothing is left. `retries_failures`: a failed
    item stays pending (classify), so stop at the first failure instead of
    paying for it again every round; blame records failures and moves on."""
    total = failed = 0
    for _ in range(MAX_ROUNDS):
        done, bad = step(conn, config)
        total, failed = total + done, failed + bad
        if (not done and not bad) or (retries_failures and (bad or not done)):
            break
    return total, failed


def scan(conn, config: dict, days: int | None = None, only: list[str] | None = None,
         dry_run: bool = False, progress=lambda msg: None) -> dict:
    version, cfg = db.scoring_config(conn)
    days = days or cfg["window_days"]
    since = since_for(days)
    base, token = config["gitlab_url"], config["gitlab_token"]
    targets = only or sync.projects(config)
    result: dict = {"since": since, "days": days, "projects": {}, "failed": {}}
    progress(f"掃描近 {days} 天(自 {since[:10]} 起)的 MR,共 {len(targets)} 個專案"
             + (",只計數不寫入" if dry_run else ""))
    me = None if dry_run else sync._me(config)
    if not dry_run:
        sync.resolve_roster(conn, config)
    for n, project in enumerate(targets, 1):
        try:
            mrs = gitlab_client.list_mrs(base, token, project, updated_after=since)
            if dry_run:
                result["projects"][project] = {"mrs": len(mrs)}
                progress(f"[{n}/{len(targets)}] {project}: {len(mrs)} 個 MR")
                continue
            findings = 0
            for i, mr in enumerate(mrs, 1):
                findings += sync.sync_one(conn, base, token, project, mr, me) or 0
                if i % 20 == 0:
                    progress(f"    {project}: {i}/{len(mrs)}")
            stamps = [db.utc(m["updated_at"]) for m in mrs if m.get("updated_at")]
            if stamps:          # incremental runs continue from here
                with conn:
                    db.set_state(conn, f"mrs_updated_at:{project}", max(stamps))
            result["projects"][project] = {"mrs": len(mrs), "findings": findings}
            progress(f"[{n}/{len(targets)}] {project}: {len(mrs)} 個 MR、{findings} 則 finding")
        except Exception as exc:              # one broken project must not stop the rest
            log.exception("scan failed for %s", project)
            result["failed"][project] = f"{type(exc).__name__}: {exc}"
            progress(f"[{n}/{len(targets)}] {project}: 失敗 — {exc}")
    if dry_run:
        return result

    progress("重算後續 bug…")
    result["followups"] = followups.refresh(conn, cfg.get("followup_days", 30))
    progress("AI 分類未分類的 finding…")
    result["classified"], bad = _drain(classify.classify_pending, conn, config, True)
    if bad:
        result["failed"]["classify"] = f"{bad} batch(es) failed"
    progress("git blame release MR 上的 finding…")
    result["blamed"], result["blame_failed"] = _drain(blame.blame_pending, conn, config, False)
    events = snapshot.record(conn, f"初次掃描(近 {days} 天,{len(targets)} 個專案)")
    result["score_changes"] = len(events)
    with conn:
        db.set_state(conn, "last_sync_at", db.now_iso())
        db.set_state(conn, "last_sync_failed", ",".join(result["failed"]))
        if not only and not result["failed"]:
            # only a complete scan of every project counts as the first scan
            db.set_state(conn, "initial_scan_at", db.now_iso())
            db.set_state(conn, "initial_scan_since", since)
    progress(f"完成:{sum(p['mrs'] for p in result['projects'].values())} 個 MR、"
             f"分類 {result['classified']}、blame {result['blamed']}、評分變動 {len(events)} 筆"
             + (f";失敗:{', '.join(result['failed'])}" if result["failed"] else ""))
    return result


def needed(conn) -> bool:
    return db.get_state(conn, "initial_scan_at") is None
