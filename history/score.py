"""Per-person aspects and the junior/mid/senior level — pure, from plain rows.

A team lead (history.db.person_roles) is reported but never ranked.

Out of 10, higher is better, like a test paper: everyone starts at 10 and
loses points per item. The formula is deliberately simple and printed with
every result:

    item deduction = deduction_per_weight × Σ weight of that item / reviewed MRs
    item score     = 10 − item deduction            (0 – 10, per item)
    score          = 10 − Σ item deductions         (0 – 10)

    finding weight   = severity_weight[severity] × category_multiplier[category]
    follow-up weight = followup_weight[kind]

Items: the five finding categories plus 後續 bug (follow-ups); an
uncategorized finding counts under code quality until it is classified. Not counted: findings a human excluded as false positives,
and findings whose appeal the AI accepted (the developer was right). Under
`min_reviewed_mrs` reviewed MRs there is no level ("資料不足") — a handful of
MRs says more about luck than about skill.

The code is AI-written; the person plans, prompts, reviews and decides what
merges. So every finding that survived the adversarial review is something
they let through, and a level is a gate, not just an average: on top of
`min_score` a level may require `max_high` (high findings in the window),
`max_fix_mr` (fix MRs soon after shipping) and `min_clean_rate` (share of
reviewed MRs with no counted finding of theirs). All must hold — a low
average cannot buy back a shipped high-severity bug.
"""
from datetime import datetime, timedelta, timezone

from history import db
from history.parse import CATEGORIES, is_release_mr

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


def uncounted_notes(raw: list[dict], reviews: list[dict]) -> set[str]:
    """Note ids (as followups.source_ref strings) that do not count anywhere:
    excluded by a human or appeal-accepted. Global, not per author — an AI
    re-find is pinned on one person but the finding itself may be someone else's."""
    return {str(f["note_id"]) for f in effective_findings(raw, reviews)
            if f["excluded"] or f["appeal_accepted"]}


GATES = {  # level key -> (stat, holds(stat, limit), why-not text)
    "min_score": ("score", lambda v, lim: v >= lim, "總分 {v} < {lim}"),
    "max_high": ("high", lambda v, lim: v <= lim, "high finding {v} 則 > {lim}"),
    "max_fix_mr": ("fix_mr", lambda v, lim: v <= lim, "上線後被 fix {v} 次 > {lim}"),
    "min_clean_rate": ("clean_rate", lambda v, lim: v >= lim,
                       "乾淨 MR {v:.0%} < {lim:.0%}"),
}


def level_misses(stats: dict, lv: dict) -> list[str]:
    """Why `stats` does not meet level `lv` (empty = it does). Unset gates pass."""
    out = []
    for key, (stat, holds, why) in GATES.items():
        lim = lv.get(key)
        if lim is not None and not holds(stats[stat], lim):
            out.append(why.format(v=stats[stat], lim=lim))
    return out


def level_for(score: float, levels: list[dict], stats: dict | None = None) -> str:
    """The best level whose every gate holds; levels are ordered best first."""
    stats = {"high": 0, "fix_mr": 0, "clean_rate": 1.0, **(stats or {}), "score": score}
    for lv in levels:
        if not level_misses(stats, lv):
            return lv["level"]
    return levels[-1]["level"]


ITEMS = {**CATEGORIES, "followups": "後續 bug"}


def item_scores(weights: dict, n: int, cfg: dict) -> dict:
    """{item: {score, deduction}} from per-item weight sums over `n` reviewed MRs."""
    k = cfg["deduction_per_weight"]
    out = {}
    for item in ITEMS:
        deduction = k * weights.get(item, 0.0) / n
        out[item] = {"label": ITEMS[item], "deduction": round(deduction, 2),
                     "score": round(max(0.0, 10 - deduction), 1)}
    return out


def total_score(weights: dict, n: int, cfg: dict) -> float:
    return round(max(0.0, 10 - cfg["deduction_per_weight"] * sum(weights.values()) / n), 1)


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
    weights = {c: v["weight"] for c, v in aspects.items()}
    weights["code_quality"] += weights.pop(UNCATEGORIZED)
    weights["followups"] = follow_weight
    items = item_scores(weights, n, cfg) if n else {}
    for c, v in aspects.items():
        if c in items:
            items[c]["count"] = v["count"]
    if n:
        items["code_quality"]["count"] += aspects[UNCATEGORIZED]["count"]
        items["followups"]["count"] = sum(follow_counts.values())
    score = total_score(weights, n, cfg) if n else None
    dirty = {f.get("mr_id") for f in counted}
    clean = sum(1 for m in reviewed if m["mr_id"] not in dirty)
    stats = {"score": score, "high": severities["high"], "fix_mr": follow_counts["fix_mr"],
             "clean_rate": clean / n if n else 0.0}
    level = level_for(score, cfg["levels"], stats) if n >= cfg["min_reviewed_mrs"] else None
    # what stands between them and the next level up — shown, so a level is never a mystery
    names = [lv["level"] for lv in cfg["levels"]]
    above = names.index(level) - 1 if level in names else -1
    next_level = cfg["levels"][above] if above >= 0 else None
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
        "items": items,
        "weights": {k: round(v, 2) for k, v in weights.items()},
        "level": level,
        "clean_mrs": clean,
        "clean_rate": round(clean / n, 2) if n else None,
        "next_level": next_level and next_level["level"],
        "next_level_misses": level_misses(stats, next_level) if next_level else [],
        "formula_version": version,
        "explain": ("10 − " + " − ".join(f"{v['label']} {v['deduction']:.1f}"
                                          for v in items.values() if v["deduction"])
                    + f" = {score}" if n and score < 10 else
                    "10(沒有任何扣分)" if n else "沒有被 review 過的 MR"),
    }


def attribution(conn, cfg: dict, now: datetime | None = None) -> dict:
    """Whose finding is whose — the one place this is decided.

    Evidence, strongest first:
      1. git blame of the flagged line (history/blame.py) -> author email -> a
         person, via an email alias that is either confirmed by a human
         (email_aliases) or proven: the email alone wrote every commit of one of
         that person's own MRs ("pure MR").
      2. Otherwise a finding on a personal MR belongs to that MR's author.
      3. A finding on a release MR (pre-prod -> master, version bump) without a
         known email is unattributed — counted for nobody, listed for review.
    No votes, no guessing: in this team a personal merge MR can carry other
    people's commits, so "the email appears in X's MR" proves nothing.

    Returns {mrs, findings: [effective + owner_author_id, owner_mr_id, how,
    blame, via_release, in_window], unattributed, unknown_emails, since}."""
    now = now or datetime.now(timezone.utc)
    since = db.utc(now - timedelta(days=cfg["window_days"]))      # same format as stored
    mrs = {r["mr_id"]: dict(r) for r in conn.execute(
        "SELECT m.*, p.username, p.name FROM mrs m LEFT JOIN people p ON p.gitlab_id = m.author_id")}
    for m in mrs.values():
        m["release"] = is_release_mr(m["title"], m["source_branch"], m["target_branch"])
    blames = {r["note_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM finding_blame WHERE error IS NULL")}
    owners = email_owners(conn, mrs)

    raw = [dict(r) for r in conn.execute("SELECT * FROM findings")]
    reviews = [dict(r) for r in conn.execute("SELECT * FROM finding_reviews")]
    out, unattributed, unknown = [], [], {}
    for f in effective_findings(raw, reviews):
        home = mrs.get(f["mr_id"])
        if home is None:
            continue
        b = blames.get(f["note_id"])
        email = (b or {}).get("author_email")
        by_blame = owners.get(email) if email else None
        # one window rule for every finding, the same one the release credit uses:
        # the creation time of the MR the comment sits on
        f.update(blame=b, via_release=home["release"],
                 in_window=(home["created_at"] or "") >= since)
        if by_blame:
            f.update(how="blame", owner_author_id=by_blame,
                     owner_mr_id=f["mr_id"] if home["author_id"] == by_blame else None)
        elif not home["release"]:
            f.update(how="direct", owner_author_id=home["author_id"], owner_mr_id=f["mr_id"])
        else:
            f.update(how=None, owner_author_id=None, owner_mr_id=None)
        if email and email not in owners:
            entry = unknown.setdefault(email, {"email": email, "name": (b or {}).get("author_name"),
                                               "findings": 0, "release_findings": 0})
            entry["findings"] += 1
            entry["release_findings"] += int(home["release"])
        (out if f["owner_author_id"] else unattributed).append(f)
    return {"mrs": mrs, "findings": out, "unattributed": unattributed, "since": since,
            "owners": owners,
            "unknown_emails": sorted(unknown.values(), key=lambda e: -e["findings"]),
            "uncounted_notes": uncounted_notes(raw, reviews)}


def email_owners(conn, mrs: dict | None = None) -> dict[str, int]:
    """email -> gitlab_id. Proven pure-MR aliases, overridden by confirmed ones
    (a confirmed None means "ignore this email")."""
    if mrs is None:
        mrs = {r["mr_id"]: dict(r) for r in conn.execute("SELECT * FROM mrs")}
        for m in mrs.values():
            m["release"] = is_release_mr(m["title"], m["source_branch"], m["target_branch"])
    per_mr: dict[int, set] = {}
    complete = {r["mr_id"] for r in conn.execute("SELECT mr_id FROM mrs WHERE commits_synced = 1")}
    for row in conn.execute("SELECT mr_id, author_email FROM mr_commits"):
        if row["mr_id"] not in complete:
            continue                               # a truncated list proves nothing
        per_mr.setdefault(row["mr_id"], set()).add((row["author_email"] or "").lower())
    proven: dict[str, set] = {}
    for mr_id, emails in per_mr.items():
        m = mrs.get(mr_id)
        if m and not m["release"] and m["author_id"] is not None and len(emails) == 1:
            (email,) = emails
            if email:
                proven.setdefault(email, set()).add(m["author_id"])
    owners = {e: next(iter(ids)) for e, ids in proven.items() if len(ids) == 1}  # unambiguous only
    for email, gitlab_id in db.confirmed_aliases(conn).items():
        if gitlab_id is None:
            owners.pop(email, None)
        else:
            owners[email] = gitlab_id
    return owners


def team_report(conn, version: int, cfg: dict, now: datetime | None = None,
                attributed: dict | None = None) -> list[dict]:
    """Every author with a (non-release) MR in the window, best score first."""
    now = now or datetime.now(timezone.utc)
    attributed = attributed or attribution(conn, cfg, now)
    since, all_mrs = attributed["since"], attributed["mrs"]
    mrs = [m for m in all_mrs.values()
           if (m["created_at"] or "") >= since and not m["release"] and m["author_id"] is not None]
    # a release MR counts as one reviewed MR for everyone whose commits are in it:
    # findings blamed onto a person need the matching amount of work beside them
    owners = attributed["owners"]
    release_credit: dict[int, list] = {}
    for row in conn.execute("SELECT DISTINCT mr_id, author_email FROM mr_commits"):
        m = all_mrs.get(row["mr_id"])
        person = owners.get((row["author_email"] or "").lower())
        if m and m["release"] and m["reviewed"] and person and (m["created_at"] or "") >= since:
            if m not in release_credit.setdefault(person, []):
                release_credit[person].append(m)
    findings = [f for f in attributed["findings"] if f["in_window"]]
    excluded_notes = attributed["uncounted_notes"]
    followups = [dict(r) for r in conn.execute("SELECT * FROM followups")]

    people: dict[int, dict] = {}
    # people added by hand appear even before their first MR
    for row in conn.execute("SELECT p.gitlab_id, p.username, p.name FROM roster_additions r "
                            "JOIN people p ON p.gitlab_id = r.resolved_id"):
        people.setdefault(row["gitlab_id"], {"author_id": row["gitlab_id"],
                                             "username": row["username"], "name": row["name"],
                                             "mrs": []})
    for m in mrs:
        people.setdefault(m["author_id"], {"author_id": m["author_id"], "username": m["username"],
                                           "name": m["name"], "mrs": []})["mrs"].append(m)
    for f in findings:            # blamed onto someone with no MR of their own in the window
        if f["owner_author_id"] is not None:
            release_credit.setdefault(f["owner_author_id"], release_credit.get(f["owner_author_id"], []))
    for person_id, credited in release_credit.items():
        if person_id not in people:
            row = conn.execute("SELECT username, name FROM people WHERE gitlab_id = ?",
                               (person_id,)).fetchone()
            if row is None:
                continue
            people[person_id] = {"author_id": person_id, "username": row["username"],
                                 "name": row["name"], "mrs": []}
        people[person_id]["release_mrs"] = credited
    roles = db.person_roles(conn)
    out = []
    finding_owner = {str(f["note_id"]): f["owner_author_id"] for f in attributed["findings"]}
    for person in people.values():
        ids = {m["mr_id"] for m in person["mrs"]}
        mine = [f for f in findings if f["owner_author_id"] == person["author_id"]]
        # an AI re-find that is this person's own finding is already counted above
        fus = [fu for fu in followups if fu["feature_mr_id"] in ids
               and not (fu["kind"] == "ai_refind" and (
                   fu["source_ref"] in excluded_notes
                   or finding_owner.get(fu["source_ref"]) == person["author_id"]))]
        report = person_report(person["mrs"] + person.get("release_mrs", []), mine, fus, cfg,
                               version, now)
        report["release_mrs"] = len(person.get("release_mrs", []))
        # which MRs the score stood on (own + credited releases), for the drill-down
        report["mr_ids"] = [m["mr_id"] for m in person["mrs"] + person.get("release_mrs", [])]
        role = roles.get(person["author_id"], "member")
        if role in db.UNRANKED_ROLES:
            report.update(level=None, next_level=None, next_level_misses=[])  # never ranked
        out.append({"username": person["username"], "name": person["name"],
                    "author_id": person["author_id"], "role": role,
                    "ranked": role not in db.UNRANKED_ROLES, **report})
    out.sort(key=lambda r: (not r["ranked"], r["level"] is None,
                            -(r["score"] if r["score"] is not None else -1)))
    return out


def team_pool(rows: list[dict]) -> tuple[dict, int]:
    """Σ per-item weight and Σ reviewed MRs over ranked people. Pooled, so one
    person with two MRs cannot skew the team."""
    ranked = [r for r in rows if r["ranked"] and r["reviewed_mrs"]]
    weights = {item: sum(r["weights"].get(item, 0) for r in ranked) for item in ITEMS}
    return weights, sum(r["reviewed_mrs"] for r in ranked)


def team_score(rows: list[dict], cfg: dict) -> float | None:
    weights, mrs = team_pool(rows)
    return total_score(weights, mrs, cfg) if mrs else None


def team_average(rows: list[dict], cfg: dict) -> dict:
    """{item: score 0–10} for the team — the radar's comparison line."""
    weights, mrs = team_pool(rows)
    return {k: v["score"] for k, v in item_scores(weights, mrs, cfg).items()} if mrs else {}


def per_mr_profile(row: dict) -> dict:
    """One person's {item: score 0–10} (same axes as team_average)."""
    return {k: v["score"] for k, v in row["items"].items()}
