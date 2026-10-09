"""The per-MR scorecard: the model grades one MR 1–5 on each of the 8
categories (prompts/rate.md), so a 5 has to be earned with evidence instead of
meaning "the review found nothing". Scoring (history/score.py) caps each grade
by the findings that MR kept and averages the grades per person.

One tool-less model call per MR, from the same context a review reads (diff +
MR description) plus the findings on record. Called right after a review
(reviewer.py, via `rate_mr`) and by the scheduled job for MRs not rated yet —
the back-fill of history is the same code path. Release / integration MRs are
never rated: their diff is everyone's work.

The reply is validated mechanically: all 8 keys, integer 1–5 or null, an N/A
needs a reason, and a grade the evidence cannot carry is lowered (5 needs two
pieces, 4 needs one; a truncated diff can never earn 5).
"""
import json
import logging

import engines
import engines.claude_engine
import fetch_mr
from history import db, score
from history.parse import CATEGORIES, is_release_mr
from sentinel_config import SCRIPT_DIR

log = logging.getLogger("mr_sentinel.history")

RUBRIC_VERSION = 1
WORK_DIR = SCRIPT_DIR / "reviews" / "_history"
DIFF_MAX = 120_000
TEXT_MAX = 400
EVIDENCE_NEEDED = {5: 2, 4: 1}


def _clean(text) -> str:
    return " ".join(str(text or "").split())[:TEXT_MAX]


def valid_ratings(reply: dict, truncated: bool = False) -> dict | None:
    """{category: {score, reason, evidence}} or None when the reply is unusable.
    A grade is lowered to what its evidence carries; a missing key fails all."""
    ratings = (reply or {}).get("ratings") if isinstance(reply, dict) else None
    if not isinstance(ratings, dict) or set(ratings) != set(CATEGORIES):
        return None
    out = {}
    for cat in CATEGORIES:
        r = ratings[cat]
        if not isinstance(r, dict):
            return None
        s = r.get("score")
        if s is not None and (isinstance(s, bool) or not isinstance(s, int) or not 1 <= s <= 5):
            return None
        reason = _clean(r.get("reason"))
        if s is None and not reason:
            return None                          # an N/A must say why
        evidence = [{"ref": _clean(e.get("ref")), "claim": _clean(e.get("claim"))}
                    for e in (r.get("evidence") or []) if isinstance(e, dict) and _clean(e.get("ref"))]
        if s is not None:
            if truncated:
                s = min(s, 4)
            while s in EVIDENCE_NEEDED and len(evidence) < EVIDENCE_NEEDED[s]:
                s -= 1                           # a 5 without two references is not a 5
        out[cat] = {"score": s, "reason": reason, "evidence": evidence[:5]}
    return out


def build_input(ctx: dict, findings: list[dict]) -> tuple[dict, bool]:
    """What the model sees: title, description, the (possibly cut) diff, findings."""
    diff, size = [], 0
    truncated = False
    for c in ctx.get("changes") or []:
        chunk = f"--- {c.get('old_path')}\n+++ {c.get('new_path')}\n{c.get('diff') or ''}"
        if size + len(chunk) > DIFF_MAX:
            truncated = True
            break
        diff.append(chunk)
        size += len(chunk)
    return {"title": ctx.get("title"), "description": ctx.get("description") or "",
            "source_branch": ctx.get("source_branch"), "target_branch": ctx.get("target_branch"),
            "stats": ctx.get("stats"), "diff_truncated": truncated,
            "findings": [{"severity": f.get("severity"), "category": f.get("category"),
                          "title": f.get("title"), "file": f.get("file")} for f in findings],
            "diff": "\n".join(diff)}, truncated


def store(conn, mr_id: int, ratings: dict, head_sha: str | None, source: str, engine: str) -> None:
    now = db.now_iso()
    with conn:
        conn.executemany(
            "INSERT INTO mr_ratings(mr_id, category, score, reason, evidence, head_sha, source, "
            "engine, rated_at, rubric_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(mr_id, cat, r["score"], r["reason"], json.dumps(r["evidence"], ensure_ascii=False),
              head_sha, source, engine, now, RUBRIC_VERSION) for cat, r in ratings.items()])


def latest(conn) -> dict[int, dict[str, dict]]:
    """mr_id -> category -> the newest rating (any head; a newer review wins)."""
    out: dict[int, dict] = {}
    for r in conn.execute("SELECT * FROM mr_ratings ORDER BY rated_at, id"):
        out.setdefault(r["mr_id"], {})[r["category"]] = dict(r)
    return out


def _findings_of(conn, mr_id: int) -> list[dict]:
    rows = [dict(r) for r in conn.execute("SELECT * FROM findings WHERE mr_id = ?", (mr_id,))]
    reviews = [dict(r) for r in conn.execute(
        "SELECT * FROM finding_reviews WHERE note_id IN (SELECT note_id FROM findings WHERE mr_id = ?)",
        (mr_id,))]
    return [f for f in score.effective_findings(rows, reviews)
            if not f["excluded"] and not f["appeal_accepted"]]


def rate_one(conn, config: dict, mr: dict, ctx: dict | None = None,
             source: str = "backfill") -> bool:
    """Rate one MR row (mrs table). Fetches its context from GitLab unless given."""
    review_cfg = config["review"]
    if ctx is None:
        ctx = fetch_mr.build_context(config["gitlab_url"], config["gitlab_token"],
                                     mr["project"], mr["iid"])
    payload, truncated = build_input(ctx, _findings_of(conn, mr["mr_id"]))
    template = (SCRIPT_DIR / "prompts" / "rate.md").read_text()
    prompt = template.replace("__LANGUAGE__", engines.claude_engine.language_name(
        review_cfg.get("language", "zh-TW"))) + json.dumps(payload, ensure_ascii=False)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    reply = engines.get_engine(review_cfg["engine"]).run_json(prompt, WORK_DIR, review_cfg)
    ratings = valid_ratings(reply, truncated)
    if ratings is None:
        log.warning("rating of %s!%s unusable; left unrated", mr["project"], mr["iid"])
        return False
    head = ((ctx.get("diff_refs") or {}).get("head_sha")) or mr.get("head_sha")
    store(conn, mr["mr_id"], ratings, head, source, review_cfg["engine"])
    return True


def pending(conn, since: str) -> list[dict]:
    """Reviewed, personal (non-release) MRs in the window with no rating for
    their current head — newest first, so the scores that matter fill in first."""
    rated = {(r["mr_id"], r["head_sha"]) for r in conn.execute(
        "SELECT DISTINCT mr_id, head_sha FROM mr_ratings WHERE rubric_version = ?",
        (RUBRIC_VERSION,))}
    out = []
    for r in conn.execute("SELECT * FROM mrs WHERE reviewed = 1 AND created_at >= ? "
                          "ORDER BY created_at DESC", (since,)):
        m = dict(r)
        if is_release_mr(m["title"], m["source_branch"], m["target_branch"]):
            continue
        if (m["mr_id"], m["head_sha"]) not in rated:
            out.append(m)
    return out


def rate_pending(conn, config: dict, limit: int = 40, progress=lambda msg: None) -> tuple[int, int]:
    """(rated, failed). Bounded per run; a failure leaves the MR for next time."""
    version, cfg = db.scoring_config(conn)
    todo = pending(conn, score.window_start(cfg))[:limit]
    done = failed = 0
    for i, mr in enumerate(todo, 1):
        try:
            ok = rate_one(conn, config, mr)
        except Exception:
            log.exception("rating %s!%s failed", mr["project"], mr["iid"])
            ok = False
        done, failed = done + ok, failed + (not ok)
        progress(f"    評分 {i}/{len(todo)} {mr['project'].rsplit('/', 1)[-1]}!{mr['iid']}"
                 + ("" if ok else " 失敗"))
    return done, failed


def rate_mr(config: dict, project: str, iid, ctx: dict | None = None) -> bool:
    """Reviewer hook, right after the review is on record: never raises."""
    try:
        conn = db.connect(db.resolve_path(config))
        try:
            row = conn.execute("SELECT * FROM mrs WHERE project = ? AND iid = ?",
                               (project, int(iid))).fetchone()
            if row is None or is_release_mr(row["title"], row["source_branch"], row["target_branch"]):
                return False
            ok = rate_one(conn, config, dict(row), ctx=ctx, source="review")
            if ok:
                from history import snapshot
                snapshot.record(conn, f"MR 評分 {project.rsplit('/', 1)[-1]}!{iid}")
            return ok
        finally:
            conn.close()
    except Exception:
        log.exception("rating of %s!%s failed", project, iid)
        return False
