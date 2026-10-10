"""Team- and project-level facts for the dashboard's 每週週報 and 專案風險: plain
counts built on the core's own rules (score.attribution decides whose finding is
whose and whether it was still open at merge; score.followup_verdicts the human
verdicts), never a new definition and never a ranking of people.

Weeks run Monday to Sunday in Taipei time. "Coverage" is the share of personal
(non-release) merged MRs the bot reviewed; "self-merge" the share merged by
their own author, out of those whose merger is known."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db, score

TAIPEI = ZoneInfo("Asia/Taipei")
COVERAGE_MIN = 0.5            # below this, a person's / project's score stands on too little review
TREND_WEEKS = 12
RISK = {                      # flag -> (test on a project window, text); each needs a minimum sample
    "coverage": "review 覆蓋率低",
    "escaped_high": "有 high 可能沒處理就 merge",
    "self_merge": "自己 merge 的比例高",
    "fix": "修 bug 的 MR 比例高",
    "pending": "待確認的後續 bug 多",
}


def _ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def week_of(day: date) -> date:
    """The Monday of the week `day` falls in."""
    return day - timedelta(days=day.weekday())


def week_bounds(monday: date) -> tuple[datetime, datetime]:
    """[Monday 00:00, next Monday 00:00) Taipei, as UTC datetimes."""
    start = datetime(monday.year, monday.month, monday.day, tzinfo=TAIPEI)
    return start.astimezone(timezone.utc), (start + timedelta(days=7)).astimezone(timezone.utc)


def this_week(now: datetime | None = None) -> date:
    return week_of((now or datetime.now(timezone.utc)).astimezone(TAIPEI).date())


def mr_facts(conn, cfg: dict, attributed: dict | None = None) -> list[dict]:
    """Every merged MR with what the reports count: was it reviewed, who merged it,
    its counted findings (high, possibly open at merge) and its live follow-ups."""
    attributed = attributed or score.attribution(conn, cfg)
    findings = attributed["findings"] + attributed["unattributed"]
    counted = [f for f in findings if not f["excluded"] and not f["appeal_accepted"]]
    uncounted = {str(f["note_id"]) for f in findings if f["excluded"] or f["appeal_accepted"]}
    by_mr: dict = {}
    for f in counted:
        by_mr.setdefault(f["mr_id"], []).append(f)
    fus: dict = {}
    for fu in score.annotated_followups(conn, findings):
        if fu["verdict"] == "unrelated" or (fu["kind"] == "ai_refind" and fu["source_ref"] in uncounted):
            continue
        fus.setdefault(fu["feature_mr_id"], []).append(fu)
    out = []
    for m in attributed["mrs"].values():
        merged = _ts(m.get("merged_at"))
        if m.get("state") != "merged" or merged is None:
            continue
        fs = by_mr.get(m["mr_id"], [])
        escaped = [f for f in fs if f.get("escaped")]
        out.append({
            "mr_id": m["mr_id"], "project": m["project"], "iid": m["iid"], "title": m["title"],
            "web_url": m["web_url"], "author_id": m["author_id"], "merged_by": m.get("merged_by"),
            "merged": merged, "release": bool(m["release"]), "reviewed": bool(m["reviewed"]),
            "is_fix": bool(m["is_fix"]),
            "self_merge": None if m.get("merged_by") is None else m["merged_by"] == m["author_id"],
            "findings": fs, "high": [f for f in fs if f.get("severity") == "high"],
            "escaped": escaped, "escaped_high": [f for f in escaped if f.get("severity") == "high"],
            "followups": fus.get(m["mr_id"], []),
        })
    return out


def _share(ms, pred):
    known = [m for m in ms if pred(m) is not None]
    return {"n": len(known), "k": sum(1 for m in known if pred(m)),
            "share": sum(1 for m in known if pred(m)) / len(known) if known else None}


def summarize(ms: list[dict]) -> dict:
    """The numbers one period shows: activity, review coverage, self-merge, what may
    have shipped with an open finding, and follow-ups."""
    personal = [m for m in ms if not m["release"]]
    return {
        "merged": len(ms), "personal": len(personal), "releases": len(ms) - len(personal),
        "coverage": _share(personal, lambda m: m["reviewed"]),
        "self_merge": _share(personal, lambda m: m["self_merge"]),
        "fix": _share(personal, lambda m: m["is_fix"]),
        "high": sum(len(m["high"]) for m in ms),
        "escaped_mrs": sum(1 for m in ms if m["escaped"]),
        "escaped_high": sum(len(m["escaped_high"]) for m in ms),
        "pending": sum(1 for m in ms for fu in m["followups"] if fu["verdict"] is None),
        "confirmed": sum(1 for m in ms for fu in m["followups"] if fu["verdict"] == "confirmed"),
    }


def _in(ms, start, end):
    return [m for m in ms if start <= m["merged"] < end]


def team_scope(conn, facts: list[dict]) -> list[dict]:
    """MRs of the current team (lead + members): an external helper's MRs are not
    the team's process. Unknown authors count as members, like everywhere else."""
    roles = db.person_roles(conn)
    return [m for m in facts if roles.get(m["author_id"], "member") not in db.NOT_EVALUATED]


def week_report(conn, cfg: dict, monday: date, attributed: dict | None = None,
                now: datetime | None = None) -> dict:
    """One week (Monday–Sunday, Taipei) of the team: this week vs last week, a
    12-week trend of each number, and the leader's to-do list."""
    now = now or datetime.now(timezone.utc)
    facts = team_scope(conn, mr_facts(conn, cfg, attributed))
    start, end = week_bounds(monday)
    prev_start, _ = week_bounds(monday - timedelta(days=7))
    this, prev = _in(facts, start, end), _in(facts, prev_start, start)
    trend = []
    for w in range(TREND_WEEKS - 1, -1, -1):
        m0 = monday - timedelta(days=7 * w)
        s, e = week_bounds(m0)
        trend.append({"monday": m0, **summarize(_in(facts, s, e))})
    # to-do 1: every follow-up still waiting for a human, newest first (this week's flagged)
    since = now - timedelta(days=cfg["window_days"])
    pending = []
    for m in facts:
        if m["merged"] < since:
            continue
        for fu in m["followups"]:
            if fu["verdict"] is not None:
                continue
            seen = m["merged"] + timedelta(days=fu.get("days_after") or 0)
            pending.append({"mr": m, "fu": fu, "seen": seen, "new": start <= seen < end})
    pending.sort(key=lambda p: p["seen"], reverse=True)
    # to-do 2: this week's merges that may have shipped an open finding, high first
    escaped = sorted((m for m in this if m["escaped"]),
                     key=lambda m: (-len(m["escaped_high"]), -len(m["escaped"]), m["merged"]))
    # to-do 3: this week's personal merges the bot never reviewed, by project
    unreviewed: dict = {}
    for m in this:
        if not m["release"] and not m["reviewed"]:
            unreviewed.setdefault(m["project"], []).append(m)
    return {"monday": monday, "sunday": monday + timedelta(days=6), "start": start, "end": end,
            "this": summarize(this), "prev": summarize(prev), "trend": trend,
            "pending": pending, "escaped": escaped,
            "unreviewed": sorted(unreviewed.items(), key=lambda kv: -len(kv[1])),
            "people": _people(this), "complete": end <= now}


def _people(ms: list[dict]) -> list[dict]:
    """Per author this week: activity and how much of it was reviewed (no score, no order by it)."""
    out: dict = {}
    for m in ms:
        if m["release"]:
            continue
        p = out.setdefault(m["author_id"], {"author_id": m["author_id"], "ms": []})
        p["ms"].append(m)
    return [{"author_id": p["author_id"], **summarize(p["ms"])} for p in out.values()]


def person_coverage(facts: list[dict], since: datetime) -> dict[int, dict]:
    """author -> review coverage of their personal merges since `since`."""
    out: dict = {}
    for m in facts:
        if m["release"] or m["merged"] < since:
            continue
        out.setdefault(m["author_id"], []).append(m)
    return {pid: {**_share(ms, lambda m: m["reviewed"]),
                  "low": _share(ms, lambda m: m["reviewed"])["share"] < COVERAGE_MIN}
            for pid, ms in out.items()}


def _risks(s: dict) -> list[str]:
    flags = []
    if s["coverage"]["n"] >= 3 and s["coverage"]["share"] < COVERAGE_MIN:
        flags.append("coverage")
    if s["escaped_high"]:
        flags.append("escaped_high")
    if s["self_merge"]["n"] >= 3 and s["self_merge"]["share"] >= 0.6:
        flags.append("self_merge")
    if s["fix"]["n"] >= 5 and s["fix"]["share"] >= 0.4:
        flags.append("fix")
    if s["pending"] >= 3:
        flags.append("pending")
    return flags


def project_report(conn, cfg: dict, now: datetime | None = None, days: int = 28,
                   attributed: dict | None = None) -> list[dict]:
    """Each project over the last `days` against the `days` before, with a weekly
    merge trend and the risk flags that apply; most flags first. Everyone's MRs
    count — a project's risk does not depend on who opened them."""
    now = now or datetime.now(timezone.utc)
    facts = mr_facts(conn, cfg, attributed)
    start, prev_start = now - timedelta(days=days), now - timedelta(days=2 * days)
    by_project: dict = {}
    for m in facts:
        if m["merged"] >= prev_start:
            by_project.setdefault(m["project"], []).append(m)
    out = []
    for project, ms in by_project.items():
        cur = [m for m in ms if m["merged"] >= start]
        if not cur:
            continue
        s = summarize(cur)
        weekly = []
        for w in range(TREND_WEEKS - 1, -1, -1):
            e = now - timedelta(days=7 * w)
            weekly.append(sum(1 for m in facts if m["project"] == project and e - timedelta(days=7) < m["merged"] <= e))
        flags = _risks(s)
        out.append({"project": project, "name": project.rsplit("/", 1)[-1], "now": s,
                    "prev": summarize([m for m in ms if m["merged"] < start]),
                    "authors": len({m["author_id"] for m in cur if not m["release"]}),
                    "weekly": weekly, "flags": flags,
                    "escaped": sorted((m for m in cur if m["escaped"]),
                                      key=lambda m: (-len(m["escaped_high"]), m["merged"]))})
    out.sort(key=lambda p: (-len(p["flags"]), -p["now"]["merged"], p["name"]))
    return out
