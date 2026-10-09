"""A written evaluation per person — 優點 / 缺點 with evidence — the second
judgment step here (after classify). The numbers come from history.score;
the model only turns one person's record into prose, and every point must cite
that record (prompts/evaluate.md).

Regenerated only when the person's record changed (the profile's hash), so the
scheduled run costs nothing on a quiet day; `force` redoes everyone. Output is
validated; a bad reply keeps the previous evaluation instead of a broken one.
"""
import hashlib
import json
import logging

import engines
import engines.claude_engine
from history import classify, db, score
from sentinel_config import SCRIPT_DIR

log = logging.getLogger("mr_sentinel.history")

WORK_DIR = SCRIPT_DIR / "reviews" / "_history"
MAX_FINDINGS = 40
LIMITS = {"strengths": 4, "weaknesses": 5}
TEXT_MAX = 400
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def grade_reasons(row: dict, ratings: dict, per_item: int = 4) -> dict:
    """category -> a few graded MRs' "score: reason", lowest grades first."""
    out = {}
    for cat in row["items"]:
        graded = [(r[cat]["score"], r[cat]["reason"]) for mid, r in ratings.items()
                  if mid in set(row["mr_ids"]) and cat in r and r[cat]["score"] is not None]
        graded.sort(key=lambda g: g[0])
        if graded:
            out[cat] = [f"{s}: {reason}" for s, reason in graded[:per_item]]
    return out


def profile(row: dict, findings: list[dict], followups: list[dict], team: dict,
            cfg: dict, mrs: dict, ratings: dict | None = None) -> dict:
    """Everything the model may say anything about — and nothing else."""
    ratings = ratings or {}

    def mr_ref(mr_id):
        m = mrs.get(mr_id) or {}
        return f"{(m.get('project') or '').rsplit('/', 1)[-1]}!{m.get('iid')}"

    shown = sorted(findings, key=lambda f: (SEVERITY_ORDER.get(f.get("severity"), 3),
                                            f.get("created_at") or ""))[:MAX_FINDINGS]
    return {
        "name": row.get("name") or row.get("username"),
        "role": row["role"],
        "score": row["score"], "max_score": row["max_score"], "item_max": cfg["item_max"],
        "level": row["level"], "next_level": row["next_level"],
        "blocks_next_level": row["next_level_misses"],
        "reviewed_mrs": row["reviewed_mrs"], "min_reviewed_mrs": cfg["min_reviewed_mrs"],
        "clean_mr_rate": row["clean_rate"],
        "severities": row["severities"],
        "escaped": row["escaped"],
        "coverage": row["coverage"], "rated_mrs": row["rated_mrs"], "own_mrs": row["own_mrs"],
        "tracks": {k: {"label": t["label"], "score": t["score"], "rated_mrs": t["rated_mrs"],
                       "weakest": min(((v["score"], v["label"]) for v in t["items"].values()
                                       if v["score"] is not None), default=None)}
                   for k, t in (row.get("tracks") or {}).items()},
        "fullstack_bonus": row.get("fullstack_bonus", 0),
        "items": {k: {"label": v["label"], "score": v["score"], "team_avg": team.get(k),
                      "grades": v["n"], "findings": v["count"], "followups": v["followups"]}
                  for k, v in row["items"].items()},
        # what the per-MR graders said, so the note can quote them
        "grade_reasons": grade_reasons(row, ratings),
        "followups": [{"kind": fu["kind"], "file": fu.get("file"),
                       "confirmed": fu.get("verdict") == "confirmed",
                       "counts_under": score.followup_category(fu)} for fu in followups],
        "findings": [{"title": f.get("title"), "category": f["category"],
                      "severity": f.get("severity"), "mr": mr_ref(f["mr_id"]),
                      "escaped": bool(f.get("escaped")), "thread": f.get("status"),
                      "excerpt": classify.excerpt(f.get("body"))[:300]} for f in shown],
    }


def digest(p: dict) -> str:
    return hashlib.sha256(json.dumps(p, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _clean(text) -> str:
    return " ".join(str(text or "").split())[:TEXT_MAX]


def valid_reply(reply: dict) -> dict | None:
    """{summary, strengths, weaknesses} or None when the reply is unusable."""
    if not isinstance(reply, dict):
        return None
    out = {"summary": _clean(reply.get("summary"))}
    for key, limit in LIMITS.items():
        items = []
        for item in reply.get(key) or []:
            if isinstance(item, dict) and _clean(item.get("point")):
                kept = {"point": _clean(item["point"]), "evidence": _clean(item.get("evidence"))}
                if key == "weaknesses":
                    kept["advice"] = _clean(item.get("advice"))
                items.append(kept)
        out[key] = items[:limit]
    return out if out["summary"] and (out["strengths"] or out["weaknesses"]) else None


def latest(conn) -> dict[int, dict]:
    """gitlab_id -> the newest evaluation."""
    out = {}
    for r in conn.execute("SELECT * FROM person_evaluations ORDER BY created_at, id"):
        e = dict(r)
        e["strengths"], e["weaknesses"] = json.loads(e["strengths"]), json.loads(e["weaknesses"])
        out[e["gitlab_id"]] = e
    return out


def profiles(conn) -> dict[int, dict]:
    """gitlab_id -> profile, for everyone worth evaluating (has a reviewed MR,
    not departed)."""
    version, cfg = db.scoring_config(conn)
    attributed = score.attribution(conn, cfg)
    rows = score.team_report(conn, version, cfg, attributed=attributed)
    team = score.team_average(rows, cfg)
    ratings = score.latest_ratings(conn)
    followups = score.annotated_followups(conn, attributed["findings"] + attributed["unattributed"])
    out = {}
    for row in rows:
        if not row["reviewed_mrs"] or row["role"] == "departed":
            continue
        pid = row["author_id"]
        mine = [f for f in attributed["findings"] if f["owner_author_id"] == pid and f["in_window"]
                and not f["excluded"] and not f["appeal_accepted"]]
        own = set(row["mr_ids"])
        fus = [fu for fu in followups if fu["feature_mr_id"] in own and fu.get("verdict") != "unrelated"]
        out[pid] = profile(row, mine, fus, team, cfg, attributed["mrs"], ratings)
    return out


def evaluate_pending(conn, config: dict, force: bool = False,
                     only: set[int] | None = None, trigger: str = "排程") -> tuple[int, int]:
    """(people evaluated, failures). Unchanged records are skipped unless `force`."""
    current = latest(conn)
    todo = {pid: p for pid, p in profiles(conn).items()
            if (only is None or pid in only)
            and (force or (current.get(pid) or {}).get("input_hash") != digest(p))}
    if not todo:
        return 0, 0
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    review_cfg = config["review"]
    engine = engines.get_engine(review_cfg["engine"])
    template = (SCRIPT_DIR / "prompts" / "evaluate.md").read_text()
    head = template.replace("__LANGUAGE__", engines.claude_engine.language_name(
        review_cfg.get("language", "zh-TW")))
    version = db.scoring_config(conn)[0]
    done = failed = 0
    for pid, p in todo.items():
        try:
            reply = valid_reply(engine.run_json(head + json.dumps(p, ensure_ascii=False, indent=1),
                                                WORK_DIR, review_cfg))
        except Exception:
            log.exception("evaluation of %s failed; keeping the previous one", p["name"])
            reply = None
        if reply is None:
            failed += 1
            continue
        with conn:
            conn.execute("""INSERT INTO person_evaluations(gitlab_id, created_at, formula_version,
                            input_hash, summary, strengths, weaknesses, engine, trigger)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                         (pid, db.now_iso(), version, digest(p), reply["summary"],
                          json.dumps(reply["strengths"], ensure_ascii=False),
                          json.dumps(reply["weaknesses"], ensure_ascii=False),
                          review_cfg["engine"], trigger))
        done += 1
    return done, failed


def stale(conn) -> set[int]:
    """People whose newest evaluation no longer matches their record."""
    current = latest(conn)
    return {pid for pid, p in profiles(conn).items()
            if (current.get(pid) or {}).get("input_hash") != digest(p)}

