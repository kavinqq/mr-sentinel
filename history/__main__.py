"""python3 -m history <command>

    run                  the scheduled job: queued requests + sync + follow-ups + classify + blame
                         (the first time: a full scan instead of the sync, see history/scan.py)
    scan [--days N] [--project P] [--dry-run]
                         every project's MRs of the last N days (default: scoring window)
    blame [--retry]      git blame findings on release MRs (who wrote the flagged line)
    sync [--full]        GitLab -> db (incremental by default)
    classify [--limit N] fill in missing categories with the model
    followups            recompute follow-up bugs
    rate [--limit N]     AI grades reviewed MRs 1-5 per category (the back-fill; needs GitLab)
    export PATH          one consistent copy of sentinel.db to take to another machine
    evaluate [--force] [--person ID]
                         AI-written 優點 / 缺點 per person (only changed records unless --force)
    report [--json]      per-person aspects and levels
"""
import argparse
import fcntl
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

from history import blame, classify, db, discover, evaluate, trajectory, followups, rate, scan, score, snapshot, sync
from history.parse import CATEGORIES
from sentinel_config import SCRIPT_DIR, load_config

log = logging.getLogger("mr_sentinel.history")


STALE_CLAIM_HOURS = 6


def _requested(conn) -> tuple[set[str], list[int]]:
    """Claim queued dashboard requests: (kinds wanted, claimed request ids).
    A claim older than STALE_CLAIM_HOURS without a finish (the job was killed)
    is claimed again, so no request can be stuck forever."""
    stale = db.utc(datetime.now(timezone.utc) - timedelta(hours=STALE_CLAIM_HOURS))
    rows = conn.execute("SELECT id, kind FROM sync_requests WHERE finished_at IS NULL "
                        "AND (started_at IS NULL OR started_at < ?)", (stale,)).fetchall()
    if rows:
        with conn:
            conn.executemany("UPDATE sync_requests SET started_at = ? WHERE id = ?",
                             [(db.now_iso(), r["id"]) for r in rows])
    return {r["kind"] for r in rows}, [r["id"] for r in rows]


def _evaluate(conn, config: dict, result: dict, force: bool) -> None:
    result["evaluated"], bad = evaluate.evaluate_pending(
        conn, config, force=force, trigger="dashboard 請求" if force else "排程")
    if bad:
        result["failed"]["evaluate"] = f"{bad} person(s) failed"


def run(conn, config: dict, full: bool = False) -> dict:
    kinds, request_ids = _requested(conn)
    wanted_full = "full_sync" in kinds
    result: dict = {"failed": {}}
    try:
        if scan.needed(conn):         # a new db / machine: the whole window first
            result.update(scan.scan(conn, config, progress=log.info))
            _evaluate(conn, config, result, force=False)
            result["trajectory"] = trajectory.update(conn, db.scoring_config(conn)[1])
            return result
        try:                          # where the team works, beyond the reviewed projects
            _, cfg0 = db.scoring_config(conn)
            result["discovered"] = len(discover.discover(conn, config, score.window_start(cfg0))["projects"])
        except Exception as exc:
            log.exception("project discovery failed")
            result["failed"]["discover"] = f"{type(exc).__name__}: {exc}"
        result.update(sync.sync_all(conn, config, full=full or wanted_full))
        version, cfg = db.scoring_config(conn)
        result["followups"] = followups.refresh(conn, cfg.get("followup_days", 30))
        result["classified"], bad_batches = classify.classify_pending(conn, config)
        if bad_batches:
            result["failed"]["classify"] = f"{bad_batches} batch(es) failed"
        result["blamed"], result["blame_failed"] = blame.blame_pending(conn, config)
        result["rated"], bad = rate.rate_pending(conn, config)
        if bad:
            result["failed"]["rate"] = f"{bad} MR(s) not rated"
        result["score_changes"] = len(snapshot.record(
            conn, "dashboard 同步請求" if request_ids else "排程同步"))
        result["trajectory"] = trajectory.update(conn, cfg)
        _evaluate(conn, config, result, force="evaluate" in kinds)
    except Exception as exc:
        result["failed"]["run"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        # a claimed request always gets an outcome — success or the error
        if request_ids:
            summary = json.dumps(result, ensure_ascii=False, default=str)[:2000]
            with conn:
                conn.executemany("UPDATE sync_requests SET finished_at = ?, result = ? "
                                 "WHERE id = ?", [(db.now_iso(), summary, i) for i in request_ids])
    return result


def print_report(rows: list[dict], version: int, cfg: dict) -> None:
    top = score.max_total(cfg)
    print(f"評分公式 v{version}:{len(score.ITEMS)} 項各 {cfg['item_max']:g} 分、滿分 {top:g},越高越好;"
          f"每項扣 {cfg['deduction_per_weight']:g} × (該項加權問題 / 被 review 的 MR 數);"
          f"近 {cfg['window_days']} 天;少於 {cfg['min_reviewed_mrs']} 個 MR 不給等級")
    print(f"門檻:" + " / ".join(f"{lv['level']} ≥ {lv['min_score']}" if lv["min_score"] is not None
                              else f"{lv['level']} 其餘" for lv in cfg["levels"]))
    print("後續 bug 為推估值(30 天內同檔案的 fix MR / AI 再次抓到),未確認的算一半\n")
    for r in rows:
        level = "Team leader(不排入評分)" if not r["ranked"] else (r["level"] or "資料不足")
        print(f"■ {r['name'] or r['username']} ({r['username']}) — {level}"
              f"  {r['score']} / {top:g}  [{r['explain']}]")
        items = "  ".join(f"{v['label']} {v['score']}" for v in r["items"].values())
        print(f"   各項: {items or '—'}")
        if r["escaped"] or r["unclassified"]:
            print(f"   merge 時沒修 {r['escaped']} 則 · 待分類 {r['unclassified']} 則")
        sev = r["severities"]
        print(f"   嚴重度: 🔴{sev['high']} 🟠{sev['medium']} 🟡{sev['low']}"
              f"   後續 bug: fix MR {r['followups'].get('fix_mr', 0)} · AI 再抓到 "
              f"{r['followups'].get('ai_refind', 0)}"
              f"   不計: 誤判 {r['excluded']} · 申訴成立 {r['appeal_accepted']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m history")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "sync"):
        p = sub.add_parser(name)
        p.add_argument("--full", action="store_true", help="re-read every MR, not just updated ones")
    sub.add_parser("followups")
    p = sub.add_parser("blame", help="git blame findings on release MRs")
    p.add_argument("--retry", action="store_true", help="retry the ones that failed before")
    p = sub.add_parser("classify")
    p.add_argument("--limit", type=int, default=200)
    p = sub.add_parser("scan", help="first-time scan of every project's recent MRs")
    p.add_argument("--days", type=int, help="how far back (default: scoring window_days)")
    p.add_argument("--project", action="append", help="only this project (repeatable)")
    p.add_argument("--dry-run", action="store_true", help="only count the MRs, write nothing")
    p = sub.add_parser("export", help="a consistent single-file copy of the db")
    p.add_argument("path")
    p = sub.add_parser("rate", help="AI grades reviewed MRs per category (back-fill)")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--workers", type=int, default=1, help="grade this many in parallel")
    p = sub.add_parser("evaluate", help="AI-written strengths / weaknesses per person")
    p.add_argument("--force", action="store_true", help="redo everyone, not just changed records")
    p.add_argument("--person", type=int, action="append", help="only this GitLab user id")
    p = sub.add_parser("report")
    p.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    handlers: list[logging.Handler] = [RotatingFileHandler(
        SCRIPT_DIR / "history.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")]
    if sys.stderr.isatty():
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=handlers)

    config = load_config()
    conn = db.connect(db.resolve_path(config))
    if args.cmd == "export":
        print(json.dumps(db.export(conn, args.path), ensure_ascii=False, indent=1))
        return 0
    if args.cmd == "report":
        version, cfg = db.scoring_config(conn)
        rows = score.team_report(conn, version, cfg)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
        else:
            print_report(rows, version, cfg)
        return 0

    # writers take a lock so the schedule and a manual run never overlap
    with open(SCRIPT_DIR / ".lock-history", "w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("history job already running; skipping")
            return 0
        if args.cmd == "run":
            result = run(conn, config, full=args.full)
        elif args.cmd == "rate":
            done, bad = rate.rate_pending(conn, config, limit=args.limit,
                                          progress=lambda m: print(m, flush=True),
                                          workers=args.workers)
            snapshot.record(conn, f"MR 評分補齊({done} 個)")
            result = {"rated": done, "failed": {"rate": f"{bad} MR(s)"} if bad else {}}
        elif args.cmd == "evaluate":
            done, bad = evaluate.evaluate_pending(conn, config, force=args.force,
                                                  only=set(args.person) if args.person else None,
                                                  trigger="手動")
            result = {"evaluated": done, "failed": {"evaluate": f"{bad} person(s)"} if bad else {}}
        elif args.cmd == "scan":
            result = scan.scan(conn, config, days=args.days, only=args.project,
                               dry_run=args.dry_run, progress=lambda m: print(m, flush=True))
        elif args.cmd == "sync":
            result = sync.sync_all(conn, config, full=args.full)
        elif args.cmd == "blame":
            if args.retry:
                blame.retry_failed(conn)
            done, bad = blame.blame_pending(conn, config)
            result = {"blamed": done, "blame_failed": bad}
        elif args.cmd == "followups":
            result = {"followups": followups.refresh(conn, db.scoring_config(conn)[1]
                                                     .get("followup_days", 30))}
        else:
            done, bad = classify.classify_pending(conn, config, args.limit)
            result = {"classified": done, "failed": {"classify": f"{bad} batch(es)"} if bad else {}}
    log.info("history %s: %s", args.cmd, result)
    if args.cmd != "scan":                       # scan already printed its progress
        print(json.dumps(result, ensure_ascii=False, indent=1))
    return 1 if result.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
