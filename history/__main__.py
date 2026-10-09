"""python3 -m history <command>

    run                  the scheduled job: queued requests + sync + follow-ups + classify
    sync [--full]        GitLab -> db (incremental by default)
    classify [--limit N] fill in missing categories with the model
    followups            recompute follow-up bugs
    report [--json]      per-person aspects and levels
"""
import argparse
import fcntl
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

from history import classify, db, followups, score, sync
from history.parse import CATEGORIES
from sentinel_config import SCRIPT_DIR, load_config

log = logging.getLogger("mr_sentinel.history")


STALE_CLAIM_HOURS = 6


def _requested(conn) -> tuple[bool, list[int]]:
    """Claim queued dashboard requests: (full sync wanted, claimed request ids).
    A claim older than STALE_CLAIM_HOURS without a finish (the job was killed)
    is claimed again, so no request can be stuck forever."""
    stale = db.utc(datetime.now(timezone.utc) - timedelta(hours=STALE_CLAIM_HOURS))
    rows = conn.execute("SELECT id, kind FROM sync_requests WHERE finished_at IS NULL "
                        "AND (started_at IS NULL OR started_at < ?)", (stale,)).fetchall()
    if rows:
        with conn:
            conn.executemany("UPDATE sync_requests SET started_at = ? WHERE id = ?",
                             [(db.now_iso(), r["id"]) for r in rows])
    return any(r["kind"] == "full_sync" for r in rows), [r["id"] for r in rows]


def run(conn, config: dict, full: bool = False) -> dict:
    wanted_full, request_ids = _requested(conn)
    result: dict = {"failed": {}}
    try:
        result.update(sync.sync_all(conn, config, full=full or wanted_full))
        version, cfg = db.scoring_config(conn)
        result["followups"] = followups.refresh(conn, cfg.get("followup_days", 30))
        result["classified"], bad_batches = classify.classify_pending(conn, config)
        if bad_batches:
            result["failed"]["classify"] = f"{bad_batches} batch(es) failed"
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
    names = {**CATEGORIES, score.UNCATEGORIZED: "未分類"}
    print(f"評分公式 v{version}:(Σ finding 權重 + Σ 後續 bug 權重) / 被 review 的 MR 數,"
          f"越低越好;近 {cfg['window_days']} 天;少於 {cfg['min_reviewed_mrs']} 個 MR 不給等級")
    print(f"門檻:" + " / ".join(f"{lv['level']} ≤ {lv['max_score']}" if lv["max_score"] is not None
                              else f"{lv['level']} 其餘" for lv in cfg["levels"]))
    print("後續 bug 為推估值(30 天內同檔案的 fix MR / AI 再次抓到)\n")
    for r in rows:
        level = r["level"] or "資料不足"
        print(f"■ {r['name'] or r['username']} ({r['username']}) — {level}"
              f"  score={r['score']}  [{r['explain']}]")
        aspects = ", ".join(f"{names.get(k, k)} {v['count']}" for k, v in r["aspects"].items())
        print(f"   面向: {aspects or '—'}")
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
    p = sub.add_parser("classify")
    p.add_argument("--limit", type=int, default=200)
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
    conn = db.connect((config.get("history") or {}).get("db_path"))
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
        elif args.cmd == "sync":
            result = sync.sync_all(conn, config, full=args.full)
        elif args.cmd == "followups":
            result = {"followups": followups.refresh(conn, db.scoring_config(conn)[1]
                                                     .get("followup_days", 30))}
        else:
            done, bad = classify.classify_pending(conn, config, args.limit)
            result = {"classified": done, "failed": {"classify": f"{bad} batch(es)"} if bad else {}}
    log.info("history %s: %s", args.cmd, result)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 1 if result.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
