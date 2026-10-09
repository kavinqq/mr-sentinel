"""Per-person aspects and the junior/mid/senior level — pure, from plain rows.

The formula is deliberately simple and printed with every result:

    score = ( Σ finding weight + Σ follow-up weight ) / reviewed MRs

    finding weight  = severity_weight[severity] × category_multiplier[category]
    follow-up weight = followup_weight[kind]

Lower is better. Not counted: findings a human excluded as false positives,
and findings whose appeal the AI accepted (the developer was right). Under
`min_reviewed_mrs` reviewed MRs there is no level ("資料不足") — a handful of
MRs says more about luck than about skill.
"""
from datetime import datetime, timedelta, timezone

from history import db
from history.parse import CATEGORIES

UNCATEGORIZED = "uncategorized"


def effective_findings(findings: list[dict], reviews: list[dict]) -> list[dict]:
    """Apply human overrides (latest wins, per field) on top of the raw rows."""
    latest_cat, latest_excl = {}, {}
    for r in sorted(reviews, key=lambda r: (r["created_at"], r["id"])):
        if r.get("category"):
            latest_cat[r["note_id"]] = r["category"]
        if r.get("excluded") is not None:
            latest_excl[r["note_id"]] = bool(r["excluded"])
    out = []
    for f in findings:
        f = dict(f)
        f["category"] = latest_cat.get(f["note_id"], f.get("category")) or UNCATEGORIZED
        f["excluded"] = latest_excl.get(f["note_id"], False)
        f["appeal_accepted"] = f.get("appeal_verdict") == "accept"
        out.append(f)
    return out


def level_for(score: float, levels: list[dict]) -> str:
    for lv in levels:
        if lv["max_score"] is None or score <= lv["max_score"]:
            return lv["level"]
    return levels[-1]["level"]


def person_report(mrs: list[dict], findings: list[dict], followups: list[dict],
                  cfg: dict, version: int, now: datetime | None = None) -> dict:
    """One author's MRs (in window), their effective findings and their followups."""
    now = now or datetime.now(timezone.utc)
    reviewed = [m for m in mrs if m["reviewed"]]
    counted = [f for f in findings if not f["excluded"] and not f["appeal_accepted"]]

    aspects = {c: {"count": 0, "weight": 0.0} for c in [*CATEGORIES, UNCATEGORIZED]}
    severities = {"high": 0, "medium": 0, "low": 0}
    finding_weight = 0.0
    for f in counted:
        w = (cfg["severity_weight"].get(f.get("severity") or "low", 0.0)
             * cfg["category_multiplier"].get(f["category"], 1.0))
        aspects[f["category"]]["count"] += 1
        aspects[f["category"]]["weight"] += w
        severities[f.get("severity") or "low"] = severities.get(f.get("severity") or "low", 0) + 1
        finding_weight += w

    follow_counts = {"fix_mr": 0, "ai_refind": 0}
    follow_weight = 0.0
    for fu in followups:
        follow_counts[fu["kind"]] = follow_counts.get(fu["kind"], 0) + 1
        follow_weight += cfg["followup_weight"].get(fu["kind"], 0.0)

    n = len(reviewed)
    score = round((finding_weight + follow_weight) / n, 2) if n else None
    level = level_for(score, cfg["levels"]) if n >= cfg["min_reviewed_mrs"] else None
    return {
        "reviewed_mrs": n,
        "findings": len(counted),
        "excluded": sum(1 for f in findings if f["excluded"]),
        "appeal_accepted": sum(1 for f in findings if f["appeal_accepted"] and not f["excluded"]),
        "severities": severities,
        "aspects": {k: {"count": v["count"], "weight": round(v["weight"], 2)}
                    for k, v in aspects.items() if v["count"]},
        "followups": follow_counts,
        "finding_weight": round(finding_weight, 2),
        "followup_weight": round(follow_weight, 2),
        "score": score,
        "level": level,
        "formula_version": version,
        "explain": (f"({finding_weight:.2f} findings + {follow_weight:.2f} follow-ups) / {n} "
                    f"reviewed MRs = {score}" if n else "沒有被 review 過的 MR"),
    }


def team_report(conn, version: int, cfg: dict, now: datetime | None = None) -> list[dict]:
    """Every author with an MR in the window, best score first; no level last."""
    now = now or datetime.now(timezone.utc)
    since = db.utc(now - timedelta(days=cfg["window_days"]))      # same format as stored
    mrs = [dict(r) for r in conn.execute(
        "SELECT m.*, p.username, p.name FROM mrs m JOIN people p ON p.gitlab_id = m.author_id "
        "WHERE m.created_at >= ?", (since,))]
    in_window = {m["mr_id"] for m in mrs}
    raw = [dict(r) for r in conn.execute("SELECT * FROM findings")]
    reviews = [dict(r) for r in conn.execute("SELECT * FROM finding_reviews")]
    findings = [f for f in effective_findings(raw, reviews) if f["mr_id"] in in_window]
    excluded_notes = {str(f["note_id"]) for f in effective_findings(raw, reviews)
                      if f["excluded"] or f["appeal_accepted"]}
    followups = [dict(r) for r in conn.execute("SELECT * FROM followups")]

    people: dict[int, dict] = {}
    for m in mrs:
        people.setdefault(m["author_id"], {"author_id": m["author_id"], "username": m["username"],
                                           "name": m["name"], "mrs": []})["mrs"].append(m)
    out = []
    for person in people.values():
        ids = {m["mr_id"] for m in person["mrs"]}
        mine = [f for f in findings if f["mr_id"] in ids]
        fus = [fu for fu in followups if fu["feature_mr_id"] in ids
               and not (fu["kind"] == "ai_refind" and fu["source_ref"] in excluded_notes)]
        report = person_report(person["mrs"], mine, fus, cfg, version, now)
        out.append({"username": person["username"], "name": person["name"],
                    "author_id": person["author_id"], **report})
    out.sort(key=lambda r: (r["level"] is None, r["score"] if r["score"] is not None else 1e9))
    return out
