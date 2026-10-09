"""團隊貢獻 — the third track for a team lead, whose work is mostly not their
own MRs: reviewing others, deciding what merges, taking over half-done work,
shipping releases. Four items, each a 1–5 grade per piece of evidence, shrunk
toward 3 like every other item (history/score.py item_scores):

    review    their human review comments on someone else's MR, graded by the
              model per MR (prompts/rate_review.md)
    merge     an MR of someone else they merged: 4 if every finding was dealt
              with first, 3 if there was nothing to deal with, lower when a
              finding was still there (by severity)
    handover  their own commits inside someone else's MR, graded like an MR
              (history/rate.py slices) — the mean of its category grades
    release   a release MR they owned: same rule as merge, for what shipped

Confirmed follow-up bugs on a merged / shipped MR lower its grade again.
Never raises; a failed review grade leaves the MR for the next run.
"""
import json
import logging

import engines
import engines.claude_engine
from history import db, score
from history.parse import is_release_mr
from sentinel_config import SCRIPT_DIR

log = logging.getLogger("mr_sentinel.history")

ITEMS = {"review": "Review 把關", "merge": "Merge 把關", "handover": "接手與協作",
         "release": "整合與上版"}
ESCAPE_GRADE = {"high": 1.0, "medium": 2.0, "low": 2.5}
WORK_DIR = SCRIPT_DIR / "reviews" / "_history"
NOTES_MAX = 6000


def _gate_grade(fs: list[dict], followups: list[dict], cfg: dict) -> float:
    """How well an MR was let through: the merge / release rule."""
    escaped = [f for f in fs if f.get("escaped")]
    if escaped:
        grade = min(ESCAPE_GRADE.get(f.get("severity") or "low", 2.5) for f in escaped)
    elif fs:
        grade = 4.0                   # there was something to catch, and it was dealt with
    else:
        grade = float(cfg["prior_score"])
    grade -= sum(cfg["followup_increment"].get(fu["kind"], 0) for fu in followups
                 if fu.get("verdict") == "confirmed")
    return max(1.0, grade)


def latest_review_ratings(conn) -> dict[tuple, dict]:
    out = {}
    for r in conn.execute("SELECT * FROM review_ratings ORDER BY rated_at, id"):
        out[(r["mr_id"], r["author_id"])] = dict(r)
    return out


def observations(conn, pid: int, attributed: dict, followups: list[dict], cfg: dict,
                 since: str, slices: dict) -> list[dict]:
    mrs = attributed["mrs"]
    counted = [f for f in attributed["findings"] + attributed["unattributed"]
               if not f["excluded"] and not f["appeal_accepted"]]
    by_mr: dict[int, list] = {}
    for f in counted:
        by_mr.setdefault(f["mr_id"], []).append(f)
    fus_by_mr: dict[int, list] = {}
    for fu in followups:
        fus_by_mr.setdefault(fu["feature_mr_id"], []).append(fu)
    out = []

    def add(item, m, value, detail=""):
        out.append({"category": item, "mr_id": m["mr_id"], "created_at": m.get("created_at"),
                    "value": value, "rating": value, "findings": 0, "followups": 0,
                    "detail": detail})

    for (mr_id, author), r in latest_review_ratings(conn).items():
        m = mrs.get(mr_id)
        if author == pid and m and r["score"] is not None and (m["created_at"] or "") >= since:
            add("review", m, float(r["score"]), r["reason"] or "")
    for m in mrs.values():
        if (m["created_at"] or "") < since:
            continue
        if m.get("release") and m["author_id"] == pid and m["state"] == "merged":
            add("release", m, _gate_grade(by_mr.get(m["mr_id"], []), fus_by_mr.get(m["mr_id"], []), cfg))
        elif m.get("merged_by") == pid and m["author_id"] != pid:
            add("merge", m, _gate_grade(by_mr.get(m["mr_id"], []), fus_by_mr.get(m["mr_id"], []), cfg))
        cats = slices.get((m["mr_id"], pid))
        if cats and not m.get("release") and m["author_id"] != pid:
            grades = [c["score"] for c in cats.values() if c["score"] is not None]
            if grades:
                add("handover", m, sum(grades) / len(grades))
    return out


def track(obs: list[dict], cfg: dict) -> dict:
    """The 團隊貢獻 track: 4 items on the same 1–5 scale, total = 8 × mean (of 40)."""
    k, prior = cfg["prior_strength"], cfg["prior_score"]
    items = {}
    for key, label in ITEMS.items():
        mine = [o for o in obs if o["category"] == key]
        n = len(mine)
        exact = (k * prior + sum(o["value"] for o in mine)) / (k + n) if n else None
        items[key] = {"label": label, "n": n, "exact": exact,
                      "score": round(exact, 2) if n else None}
    assessed = [v["exact"] for v in items.values() if v["exact"] is not None]
    total = round(len(score.ITEMS) * sum(assessed) / len(assessed), 1) if assessed else None
    return {"label": "團隊貢獻", "score": total, "items": items, "own_mrs": len(obs),
            "rated_mrs": len(obs), "coverage": len(assessed), "findings": 0, "kind": "contribution",
            "observations": obs}


# ---------- grading review comments (the one model call here) ----------

def pending_reviews(conn, people: set[int], since: str) -> list[dict]:
    """(MR, reviewer) pairs whose notes changed since they were last graded."""
    done = {k: r["notes"] for k, r in latest_review_ratings(conn).items()}
    rows = conn.execute("""SELECT n.mr_id, n.author_id, COUNT(*) AS notes FROM mr_notes n
                           JOIN mrs m ON m.mr_id = n.mr_id
                           WHERE m.created_at >= ? AND m.author_id != n.author_id
                           GROUP BY n.mr_id, n.author_id""", (since,)).fetchall()
    return [dict(r) for r in rows if r["author_id"] in people
            and done.get((r["mr_id"], r["author_id"])) != r["notes"]]


def rate_review(conn, config: dict, item: dict) -> bool:
    review_cfg = config["review"]
    m = conn.execute("SELECT * FROM mrs WHERE mr_id = ?", (item["mr_id"],)).fetchone()
    notes = [r["body"] for r in conn.execute(
        "SELECT body FROM mr_notes WHERE mr_id = ? AND author_id = ? ORDER BY created_at",
        (item["mr_id"], item["author_id"]))]
    ai = [r["title"] for r in conn.execute("SELECT title FROM findings WHERE mr_id = ?",
                                           (item["mr_id"],)) if r["title"]]
    payload = {"title": m["title"], "release": is_release_mr(m["title"], m["source_branch"],
                                                              m["target_branch"]),
               "ai_review_findings": ai, "their_comments": "\n---\n".join(notes)[:NOTES_MAX]}
    template = (SCRIPT_DIR / "prompts" / "rate_review.md").read_text()
    prompt = template.replace("__LANGUAGE__", engines.claude_engine.language_name(
        review_cfg.get("language", "zh-TW"))) + json.dumps(payload, ensure_ascii=False)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    reply = engines.get_engine(review_cfg["engine"]).run_json(prompt, WORK_DIR, review_cfg)
    s = (reply or {}).get("score") if isinstance(reply, dict) else "bad"
    if s is not None and (isinstance(s, bool) or not isinstance(s, int) or not 1 <= s <= 5):
        return False
    with conn:
        conn.execute("INSERT INTO review_ratings(mr_id, author_id, notes, score, reason, engine, "
                     "rated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (item["mr_id"], item["author_id"], item["notes"], s,
                      " ".join(str(reply.get("reason") or "").split())[:400],
                      review_cfg["engine"], db.now_iso()))
    return True


def rate_pending(conn, config: dict, limit: int = 40, progress=lambda msg: None) -> tuple[int, int]:
    """Grade the leads' new review comments. (graded, failed)."""
    _, cfg = db.scoring_config(conn)
    leads = {pid for pid, role in db.person_roles(conn).items() if role == "lead"}
    todo = pending_reviews(conn, leads, score.window_start(cfg))[:limit]
    done = failed = 0
    for i, item in enumerate(todo, 1):
        try:
            ok = rate_review(conn, config, item)
        except Exception:
            log.exception("grading review notes on MR %s failed", item["mr_id"])
            ok = False
        done, failed = done + ok, failed + (not ok)
        progress(f"    review 評分 {i}/{len(todo)}" + ("" if ok else " 失敗"))
    return done, failed
