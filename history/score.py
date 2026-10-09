"""Per-person aspects and the junior/mid/senior level — pure, from plain rows.

A team lead (history.db.person_roles) is reported but never ranked.

Higher is better, like a test paper: 8 items (history/parse.py CATEGORIES),
each out of `item_max` (5), the total is their sum (40). The formula is
deliberately simple and printed with every result:

    item deduction = deduction_per_weight × Σ weight of that item / reviewed MRs
    item score     = item_max − item deduction        (floored at 0)
    score          = Σ item scores

    finding weight   = severity_weight[severity] × category_multiplier[category]
                       × (1 + escape_multiplier if still there at merge)
    follow-up weight = followup_weight[kind] × (1 confirmed | 0 unrelated |
                       unconfirmed_followup_factor), counted under a category

A finding with no category yet (or "needs_review") is listed but not scored;
one filed under the old 5-way taxonomy uses its mapped category until the
classifier files it again. Not counted: findings a human excluded as false positives,
and findings whose appeal the AI accepted (the developer was right). Under
`min_reviewed_mrs` reviewed MRs there is no level ("資料不足") — a handful of
MRs says more about luck than about skill.

The code is AI-written; the person plans, prompts, reviews and decides what
merges. So every finding that survived the adversarial review is something
they let through, and a level is a gate, not just an average: on top of
`min_score` a level may require `max_high` (high findings in the window),
`max_fix_mr` (fix MRs soon after shipping), `max_escaped` (findings still
there at merge), `min_item` (the weakest of the 8 items — strong items cannot
hide a weak one) and `min_clean_rate` (share of reviewed MRs with no counted
finding of theirs). All must hold — a high total cannot buy back a shipped
high-severity bug.
"""
from datetime import datetime, timedelta, timezone

from history import db
from history.parse import CATEGORIES, LEGACY_CATEGORIES, is_release_mr

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
        provisional = LEGACY_CATEGORIES.get(f.get("category_legacy") or "")
        decided = latest_cat.get(f["note_id"]) or f.get("category")
        f["category"] = decided or provisional or UNCATEGORIZED
        # filed by the old 5-way mapping, waiting for the classifier
        f["category_provisional"] = not decided and bool(provisional)
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
    "max_escaped": ("escaped", lambda v, lim: v <= lim, "merge 時沒修 {v} 則 > {lim}"),
    "min_item": ("min_item", lambda v, lim: v >= lim, "最弱一項 {v} < {lim}"),
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
    stats = {"high": 0, "fix_mr": 0, "escaped": 0, "clean_rate": 1.0, "min_item": 99.0,
             **(stats or {}),
             "score": score}
    for lv in levels:
        if not level_misses(stats, lv):
            return lv["level"]
    return levels[-1]["level"]


ITEMS = CATEGORIES          # one item per category, each out of cfg["item_max"]
ESCAPE_STATUSES = {"unanswered", "appeal", "rejected", "accepted"}   # not closed on GitLab


def finding_weight(f: dict, cfg: dict) -> float:
    """What one counted finding costs: severity × category multiplier, and that
    again (× escape_multiplier) if it was still there when the MR merged."""
    base = (cfg["severity_weight"].get(f.get("severity") or "low", 0.0)
            * cfg["category_multiplier"].get(f["category"], 1.0))
    return base * (1 + cfg.get("escape_multiplier", 0.0)) if f.get("escaped") else base


def followup_weight(fu: dict, cfg: dict) -> float:
    """A guessed follow-up counts `unconfirmed_followup_factor`, a confirmed one
    in full, one a human marked unrelated not at all."""
    factor = {"confirmed": 1.0, "unrelated": 0.0}.get(
        fu.get("verdict"), cfg.get("unconfirmed_followup_factor", 1.0))
    return cfg["followup_weight"].get(fu["kind"], 0.0) * factor


def followup_category(fu: dict) -> str:
    """Follow-ups count under a category: an AI re-find under the re-found
    finding's, a fix MR (a bug that shipped) under correctness."""
    category = fu.get("category")
    return category if category in CATEGORIES else "correctness"


def item_scores(weights: dict, n: int, cfg: dict) -> dict:
    """{item: {label, score, deduction}} from per-item weight sums over `n`
    reviewed MRs. The deduction shown is capped at the item's max, so that
    item_max × items − Σ deductions is exactly the total."""
    k, top = cfg["deduction_per_weight"], cfg["item_max"]
    out = {}
    for item in ITEMS:
        deduction = min(top, k * weights.get(item, 0.0) / n)
        out[item] = {"label": ITEMS[item], "deduction": round(deduction, 2),
                     "score": round(top - deduction, 1)}
    return out


def max_total(cfg: dict) -> float:
    return cfg["item_max"] * len(ITEMS)


def total_score(weights: dict, n: int, cfg: dict) -> float:
    return round(sum(v["score"] for v in item_scores(weights, n, cfg).values()), 1)


def person_report(mrs: list[dict], findings: list[dict], followups: list[dict],
                  cfg: dict, version: int, now: datetime | None = None) -> dict:
    """One author's MRs (in window), their effective findings and their followups."""
    now = now or datetime.now(timezone.utc)
    reviewed = [m for m in mrs if m["reviewed"]]
    counted = [f for f in findings if not f["excluded"] and not f["appeal_accepted"]]
    scored = [f for f in counted if f["category"] in CATEGORIES]

    weights = {c: 0.0 for c in CATEGORIES}
    counts = {c: 0 for c in CATEGORIES}
    severities = {"high": 0, "medium": 0, "low": 0}
    for f in scored:
        weights[f["category"]] += finding_weight(f, cfg)
        counts[f["category"]] += 1
        sev = f.get("severity") or "low"
        severities[sev] = severities.get(sev, 0) + 1
    findings_w = sum(weights.values())

    follow_counts = {"fix_mr": 0, "ai_refind": 0}
    follow_w = 0.0
    live = [fu for fu in followups if fu.get("verdict") != "unrelated"]
    for fu in live:
        follow_counts[fu["kind"]] = follow_counts.get(fu["kind"], 0) + 1
        w = followup_weight(fu, cfg)
        weights[followup_category(fu)] += w
        follow_w += w

    n = len(reviewed)
    items = item_scores(weights, n, cfg) if n else {}
    for c in items:
        items[c]["count"] = counts[c]
        items[c]["followups"] = sum(1 for fu in live if followup_category(fu) == c)
    score = total_score(weights, n, cfg) if n else None
    dirty = {f.get("mr_id") for f in counted}
    clean = sum(1 for m in reviewed if m["mr_id"] not in dirty)
    escaped = sum(1 for f in scored if f.get("escaped"))
    stats = {"score": score, "high": severities["high"], "fix_mr": follow_counts["fix_mr"],
             "escaped": escaped, "clean_rate": clean / n if n else 0.0,
             "min_item": min((v["score"] for v in items.values()), default=0.0)}
    level = level_for(score, cfg["levels"], stats) if n >= cfg["min_reviewed_mrs"] else None
    # what stands between them and the next level up — shown, so a level is never a mystery
    names = [lv["level"] for lv in cfg["levels"]]
    above = names.index(level) - 1 if level in names else -1
    next_level = cfg["levels"][above] if above >= 0 else None
    top = max_total(cfg)
    return {
        "reviewed_mrs": n,
        "findings": len(counted),
        "unclassified": len(counted) - len(scored),
        "escaped": escaped,
        "excluded": sum(1 for f in findings if f["excluded"]),
        "appeal_accepted": sum(1 for f in findings if f["appeal_accepted"] and not f["excluded"]),
        "severities": severities,
        "followups": follow_counts,
        "finding_weight": round(findings_w, 2),
        "followup_weight": round(follow_w, 2),
        "score": score,
        "max_score": top,
        "items": items,
        "weights": {k: round(v, 2) for k, v in weights.items()},
        "level": level,
        "clean_mrs": clean,
        "clean_rate": round(clean / n, 2) if n else None,
        "next_level": next_level and next_level["level"],
        "next_level_misses": level_misses(stats, next_level) if next_level else [],
        "formula_version": version,
        "explain": (f"{top:g} − " + " − ".join(f"{v['label']} {v['deduction']:.1f}"
                                               for v in items.values() if v["deduction"])
                    + f" = {score}" if n and score < top else
                    f"{top:g}(沒有任何扣分)" if n else "沒有被 review 過的 MR"),
    }


def escaped(f: dict, mr: dict) -> bool:
    """Still there when the MR merged: the merged code is the very commit that was
    reviewed (nothing changed after the finding — resolving the thread alone is
    not a fix), or the thread was never closed. Only findings still on GitLab:
    one a rerun replaced is not shipped twice."""
    if mr.get("state") != "merged" or not f.get("present", 1):
        return False
    same_code = bool(f.get("head_sha")) and f.get("head_sha") == mr.get("head_sha")
    return same_code or f.get("status") in ESCAPE_STATUSES


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
                 in_window=(home["created_at"] or "") >= since, escaped=escaped(f, home))
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


def followup_verdicts(conn) -> dict[tuple, str]:
    """(feature_mr_id, kind, source_ref) -> latest human verdict."""
    out = {}
    for r in conn.execute("SELECT * FROM followup_reviews ORDER BY created_at, id"):
        out[(r["feature_mr_id"], r["kind"], r["source_ref"])] = r["verdict"]
    return out


def annotated_followups(conn, findings: list[dict]) -> list[dict]:
    """Follow-up rows + the category they count under + a human verdict, if any."""
    category = {str(f["note_id"]): f["category"] for f in findings}
    verdicts = followup_verdicts(conn)
    out = []
    for r in conn.execute("SELECT * FROM followups"):
        fu = dict(r)
        fu["category"] = category.get(fu["source_ref"]) if fu["kind"] == "ai_refind" else None
        fu["verdict"] = verdicts.get((fu["feature_mr_id"], fu["kind"], fu["source_ref"]))
        out.append(fu)
    return out


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
    followups = annotated_followups(conn, attributed["findings"] + attributed["unattributed"])

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
    rank = {lv["level"]: i for i, lv in enumerate(cfg["levels"])}     # best level first
    out.sort(key=lambda r: (not r["ranked"], r["level"] is None, rank.get(r["level"], 99),
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
    """{item: score} for the team — the radar's comparison line."""
    weights, mrs = team_pool(rows)
    return {k: v["score"] for k, v in item_scores(weights, mrs, cfg).items()} if mrs else {}


def per_mr_profile(row: dict) -> dict:
    """One person's {item: score} (same axes as team_average)."""
    return {k: v["score"] for k, v in row["items"].items()}
