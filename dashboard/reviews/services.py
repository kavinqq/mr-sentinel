"""Everything the views need, built on the core `history` package.

Scores come from history.score — the same code the CLI report and (later) the
coding tutor use — so the dashboard can never show a number the core would not.
"""
import json
from datetime import datetime, timezone
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, connections, transaction

from history import db as hdb
from history import score, snapshot
from history.parse import CATEGORIES

from .models import (EmailAlias, Finding, FindingReview, FollowupReview, MergeRequest, Person,
                     PersonRole, RosterAddition, ScoreEvent, ScoringConfig, SyncRequest)

CATEGORY_LABELS = {**CATEGORIES, score.UNCATEGORIZED: "未分類", "needs_review": "待覆核"}
RADAR_AXES = list(score.ITEMS)
RADAR_LABELS = list(score.ITEMS.values())
_SHORT = {"requirements": "需求", "compatibility": "相容", "operability": "營運",
          "verification": "驗證", "maintainability": "維護"}
ITEM_SHORT = {k: _SHORT.get(k, v) for k, v in score.ITEMS.items()}   # column headers
SEVERITY_LABELS = {"high": "High", "medium": "Medium", "low": "Low"}
ROLES = hdb.ROLES
SEVERITY_ORDER = ("high", "medium", "low")
SYNC_KINDS = {"sync": "增量同步", "full_sync": "完整同步", "evaluate": "重新產生評價"}


def history_path() -> Path:
    # in tests Django swaps NAME for the test database; follow it. Absolute, so a
    # job started in another cwd opens the same file
    return Path(connections["history"].settings_dict["NAME"]).resolve()


def history_conn():
    """A core connection on the same file as the ORM (creates + migrates if missing)."""
    return hdb.connect(history_path())


# ---------- reading ----------


def team() -> tuple[int, dict, list[dict]]:
    version, cfg, rows, _ = team_full()
    return version, cfg, rows


def team_full() -> tuple[int, dict, list[dict], dict]:
    """Scores *and* the core's attribution (who owns which finding), computed once."""
    conn = history_conn()
    try:
        version, cfg = hdb.scoring_config(conn)
        attributed = score.attribution(conn, cfg)
        return version, cfg, score.team_report(conn, version, cfg, attributed=attributed), attributed
    finally:
        conn.close()


def record(trigger: str, actor: str) -> list[dict]:
    """Recompute every score and append the changes to the score log."""
    conn = history_conn()
    try:
        return snapshot.record(conn, trigger, actor)
    finally:
        conn.close()


def mr_records(row: dict, attributed: dict) -> list[dict]:
    """One person's MRs in the window, newest first, with the findings each one
    contributed to *their* score (blame can move a finding onto a colleague's MR)."""
    pid, mrs = row["author_id"], attributed["mrs"]
    mine = [f for f in attributed["findings"] if f["owner_author_id"] == pid and f["in_window"]]
    ids = list(dict.fromkeys([*row.get("mr_ids", []), *(f["mr_id"] for f in mine)]))
    out = []
    for mr_id in ids:
        m = mrs.get(mr_id)
        if not m and str(mr_id).startswith("commits:"):
            # a batch of direct commits (no MR): project|author|ISO week
            project, _, week = str(mr_id)[len("commits:"):].split("|")
            out.append({"mr_id": mr_id, "short": f"{project.rsplit('/', 1)[-1]} · {week}",
                        "title": "直接推上去的 commit(沒有 MR)", "url": None,
                        "created_at": score.week_start(week), "state": "pushed", "kind": "直接 commit",
                        "reviewed": True, "counts": {s: 0 for s in SEVERITY_ORDER},
                        "uncounted": 0, "total": 0, "graded_only": True})
            continue
        if not m:
            continue
        here = [f for f in mine if f["mr_id"] == mr_id]
        counted = [f for f in here if not f["excluded"] and not f["appeal_accepted"]]
        kind = ("release" if m["release"] else "個人" if m["author_id"] == pid else "他人的 MR")
        out.append({
            "mr_id": mr_id, "short": f"{m['project'].rsplit('/', 1)[-1]}!{m['iid']}",
            "title": m["title"], "url": m["web_url"], "created_at": m["created_at"],
            "state": m["state"], "kind": kind, "reviewed": m["reviewed"],
            "counts": {s: sum(1 for f in counted if f.get("severity") == s) for s in SEVERITY_ORDER},
            "uncounted": len(here) - len(counted), "total": len(counted),
        })
    out.sort(key=lambda r: r["created_at"] or "", reverse=True)
    return out


def evaluations() -> tuple[dict, set]:
    """(gitlab_id -> newest evaluation, ids whose record changed since)."""
    from history import evaluate
    conn = history_conn()
    try:
        return evaluate.latest(conn), evaluate.stale(conn)
    finally:
        conn.close()


def overview() -> dict:
    version, cfg, rows, attributed = team_full()
    evals, stale = evaluations()
    for r in rows:
        r["item_list"] = [{"key": k, **v} for k, v in r["items"].items()]
        # one line per track this person actually worked on
        r["track_list"] = [{"key": k, **t, "item_list": [{"key": c, **v} for c, v in t["items"].items()]}
                           for k, t in (r.get("tracks") or {}).items() if t["own_mrs"]]
        r["lifted"] = r.get("contribution_lifted")
        r["evaluation"] = evals.get(r["author_id"])
        r["evaluation_stale"] = r["author_id"] in stale
        r["records"] = mr_records(r, attributed)
    ranked = [r for r in rows if r["ranked"]]
    leads = [r for r in rows if r["role"] == "lead"]
    departed = [r for r in rows if r["role"] == "departed"]
    external = [r for r in rows if r["role"] == "external"]
    levels = {lv["level"]: sum(1 for r in ranked if r["level"] == lv["level"])
              for lv in cfg["levels"]}
    team_items = score.team_items(rows, cfg)
    return {
        "version": version, "cfg": cfg, "ranked": ranked, "leads": leads, "departed": departed, "external": external,
        "events": list(ScoreEvent.objects.all()[:12]),
        "unknown_emails": len(attributed["unknown_emails"]),
        "kpi": {"people": len(ranked),
                "reviewed": sum(r["reviewed_mrs"] for r in ranked),
                "findings": sum(r["findings"] for r in ranked),
                "followups": sum(sum(r["followups"].values()) for r in ranked),
                "confirmed": sum(r["confirmed_followups"] for r in ranked),
                "levels": levels,
                "unrated": sum(1 for r in ranked if r["level"] is None)},
        "radar": radar_chart(score.team_average(rows, cfg), None, "團隊平均", cfg["item_max"]),
        "team_score": score.team_score(rows, cfg), "item_short": ITEM_SHORT,
        "team_items": team_items,
        "team_coverage": sum(1 for v in team_items.values() if v["score"] is not None),
        "rating": {"rated": sum(r["rated_mrs"] for r in rows if r["role"] not in hdb.NOT_EVALUATED),
                   "total": sum(r["own_mrs"] for r in rows if r["role"] not in hdb.NOT_EVALUATED)},
        "max_total": score.max_total(cfg), "categories": CATEGORY_LABELS,
        "unattributed": len(attributed["unattributed"]),
        "via_release": sum(1 for f in attributed["findings"] if f["via_release"]),
    }


def radar_chart(team_avg: dict, mine: dict | None, mine_label: str | None,
                item_max: float = 5) -> dict | None:
    """Chart.js radar config (unfold renders `.chart` canvases): each item's score
    out of 10 (farther out = better), ≤ 2 datasets so it stays readable."""
    if not team_avg:
        return None
    datasets = []
    if mine:
        datasets.append({"label": mine_label, "data": [mine.get(a, 0) for a in RADAR_AXES],
                         "borderColor": "#5E6AD2", "backgroundColor": "rgba(94,106,210,.16)",
                         "pointBackgroundColor": "#5E6AD2", "pointRadius": 3, "borderWidth": 2})
    datasets.append({"label": "團隊平均" if mine else mine_label,
                     "data": [team_avg.get(a, 0) for a in RADAR_AXES],
                     "borderColor": "#8A8F98", "backgroundColor": "rgba(138,143,152,.08)",
                     "pointBackgroundColor": "#8A8F98", "pointRadius": 2, "borderWidth": 1.5,
                     "borderDash": [6, 4] if mine else []})
    options = {
        "responsive": True, "maintainAspectRatio": False,
        "plugins": {"legend": {"position": "bottom",
                               "labels": {"font": {"size": 13}, "color": "#5F636B",
                                          "boxWidth": 14, "boxHeight": 2}},
                    "tooltip": {"enabled": True}},
        # exact numbers live in the table next to the chart; ticks only add clutter
        "scales": {"r": {"min": 0, "max": item_max,
                         "ticks": {"display": False, "stepSize": 2},
                         "pointLabels": {"font": {"size": 15}, "color": "#1B1C1F"},
                         "grid": {"color": "#E8E8EC"}, "angleLines": {"color": "#E8E8EC"}}},
    }
    return {"data": json.dumps({"labels": RADAR_LABELS, "datasets": datasets}, ensure_ascii=False),
            "options": json.dumps(options)}


def sync_status() -> dict:
    conn = history_conn()
    try:
        state = {"last_sync_at": hdb.get_state(conn, "last_sync_at"),
                 "last_sync_failed": hdb.get_state(conn, "last_sync_failed") or "",
                 "last_refresh_error": hdb.get_state(conn, "last_refresh_error") or ""}
    finally:
        conn.close()
    state["requests"] = list(SyncRequest.objects.all()[:10])
    state["pending"] = SyncRequest.objects.filter(finished_at__isnull=True).count()
    return state


def person_detail(author_id: int) -> dict | None:
    """Findings come from the core's attribution, so this page lists exactly the
    findings the score counted — including ones found in a release MR."""
    version, cfg, rows, attributed = team_full()
    evals, stale = evaluations()
    summary = next((r for r in rows if r["author_id"] == author_id), None)

    person = Person.objects.filter(gitlab_id=author_id).first()
    all_mrs = attributed["mrs"]
    own_ids = [i for i, m in all_mrs.items() if m["author_id"] == author_id and not m["release"]]
    if person is None or not (own_ids or summary):
        return None
    window_start = attributed["since"]
    mine = [f for f in attributed["findings"] if f["owner_author_id"] == author_id]
    shown_mrs = {m.mr_id: m for m in MergeRequest.objects.filter(
        mr_id__in={f["mr_id"] for f in mine} | {f["owner_mr_id"] for f in mine} | set(own_ids))}
    by_id = {i: shown_mrs[i] for i in own_ids if i in shown_mrs}

    reviews = list(FindingReview.objects.filter(note_id__in=[f["note_id"] for f in mine])
                   .values("id", "note_id", "category", "excluded", "reason", "actor",
                           "created_at"))
    history_by_note = defaultdict(list)
    for r in sorted(reviews, key=lambda r: (r["created_at"], r["id"]), reverse=True):
        history_by_note[r["note_id"]].append(r)

    findings = []
    for f in mine:
        m = shown_mrs[f["mr_id"]]                   # where the comment lives
        f.update(mr=m, owner_mr=shown_mrs.get(f["owner_mr_id"]),
                 gitlab_url=f"{m.web_url}#note_{f['note_id']}" if m.web_url else None,
                 category_label=CATEGORY_LABELS.get(f["category"], f["category"]),
                 counted=not f["excluded"] and not f["appeal_accepted"],
                 history=history_by_note.get(f["note_id"], []))
        findings.append(f)
    findings.sort(key=lambda f: f.get("created_at") or "", reverse=True)
    own_notes = {str(f["note_id"]) for f in attributed["findings"] if f["owner_author_id"] == author_id}
    # same rules as team_report: excluded / appeal-accepted, or the person's own finding
    followups = _followups(by_id, attributed["uncounted_notes"] | own_notes, window_start,
                           attributed, cfg)
    mrs = list(by_id.values())

    radar = None
    if summary and summary["reviewed_mrs"]:
        radar = radar_chart(score.team_average(rows, cfg), score.per_mr_profile(summary),
                            person.name or person.username, cfg["item_max"])
    latest = PersonRole.objects.filter(person_id=author_id).first()       # ordered newest first
    role = summary["role"] if summary else (latest.role if latest else "member")
    return {"person": person, "summary": summary, "version": version, "cfg": cfg,
            "radar": radar, "role": role, "role_label": ROLES.get(role, role),
            "findings": findings, "followups": followups,
            "trend": _trend(summary, cfg),
            "items": _items_with_tracks(summary),
            "track_cols": [{"key": k, **t} for k, t in _code_tracks(summary)],
            "lead": _lead_facts(summary),
            "window_start": window_start, "max_total": score.max_total(cfg),
            "evaluation": evals.get(author_id), "evaluation_stale": author_id in stale,
            "followup_verdicts": FOLLOWUP_VERDICTS}


def _code_tracks(summary: dict | None) -> list[tuple]:
    return [(k, t) for k, t in ((summary or {}).get("tracks") or {}).items()
            if t["own_mrs"] and t.get("kind") != "contribution"]


def _lead_facts(summary: dict | None) -> dict | None:
    """A lead's contribution facts with every MR resolved, risky merges first."""
    f = (summary or {}).get("contribution")
    if not f:
        return None
    ids = {m["mr_id"] for m in f["merges"] + f["releases"]} | {h["mr_id"] for h in f["handover"]}
    mrs = {m.mr_id: m for m in MergeRequest.objects.filter(mr_id__in=ids)}
    order = {"escaped": 0, "cleared": 1, "clean": 2}

    def rows(items):
        return sorted(({**x, "mr": mrs.get(x["mr_id"])} for x in items),
                      key=lambda x: (order.get(x.get("outcome"), 3), -(x.get("findings") or 0)))
    return {**f, "merges": rows(f["merges"]), "releases": rows(f["releases"]),
            "handover": [{**h, "mr": mrs.get(h["mr_id"])} for h in f["handover"]]}


def _items_with_tracks(summary: dict | None) -> list[dict]:
    """Rows of the items table: the combined item plus each worked-on track's."""
    if not summary:
        return []
    tracks = [t for _, t in _code_tracks(summary)]
    return [{"key": k, **v, "per_track": [t["items"][k]["score"] for t in tracks]}
            for k, v in summary["items"].items()]


def _followups(by_id: dict, uncounted_notes: set[str], window_start: str,
               attributed: dict, cfg: dict) -> list[dict]:
    """Same filter as history.score.team_report: an AI re-find whose finding is
    excluded / appeal-accepted does not count. Each row carries the category it
    counts under, the human verdict and what it costs — the same core helpers."""
    conn = history_conn()
    try:
        annotated = score.annotated_followups(
            conn, attributed["findings"] + attributed["unattributed"])
    finally:
        conn.close()
    rows = [r for r in annotated if r["feature_mr_id"] in by_id
            and not (r["kind"] == "ai_refind" and r["source_ref"] in uncounted_notes)]
    for r in rows:
        r["counts_under"] = CATEGORY_LABELS[score.followup_category(r)]
        # only a confirmed follow-up lowers a grade; a guess is shown, not scored
        r["increment"] = (cfg["followup_increment"].get(r["kind"], 0)
                          if r.get("verdict") == "confirmed" else 0)
    fix_ids = [int(r["source_ref"]) for r in rows if r["kind"] == "fix_mr"]
    fixes = {m.mr_id: m for m in MergeRequest.objects.filter(mr_id__in=fix_ids)}
    out = []
    for r in rows:
        r["feature"] = by_id[r["feature_mr_id"]]
        r["in_window"] = (r["feature"].created_at or "") >= window_start
        r["fix"] = fixes.get(int(r["source_ref"])) if r["kind"] == "fix_mr" else None
        out.append(r)
    out.sort(key=lambda r: r["feature"].merged_at or "", reverse=True)
    return out


def _trend(summary: dict | None, cfg: dict) -> list[dict]:
    """Per month (of the MR): how many grades, their plain average (1–5, before the
    shrink toward 3 the score applies) and the findings behind them."""
    months = defaultdict(lambda: {"n": 0, "sum": 0.0, "findings": 0, "mrs": set()})
    for o in (summary or {}).get("observations", []):
        if not o.get("created_at"):
            continue
        m = months[o["created_at"][:7]]
        m["n"] += 1
        m["sum"] += o["value"]
        m["findings"] += o["findings"]
        if o["mr_id"]:
            m["mrs"].add(o["mr_id"])
    top = cfg["item_max"]
    out = []
    for key in sorted(months):
        v = months[key]
        avg = round(v["sum"] / v["n"], 2)
        out.append({"month": key, "grades": v["n"], "mrs": len(v["mrs"]), "findings": v["findings"],
                    "avg": avg, "bar": round(100 * avg / top)})
    return out


def finding_owner(note_id: int) -> int | None:
    """Whose finding this is, after blame — where a review action should return to."""
    conn = history_conn()
    try:
        _, cfg = hdb.scoring_config(conn)
        attributed = score.attribution(conn, cfg)
    finally:
        conn.close()
    for f in attributed["findings"] + attributed["unattributed"]:
        if f["note_id"] == note_id:
            return f["owner_author_id"]
    return None


def set_role(gitlab_id: int, role: str, actor: str) -> PersonRole:
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    person = Person.objects.filter(gitlab_id=gitlab_id).first()
    if person is None:
        raise ValueError("沒有這個人")
    saved = PersonRole.objects.create(person_id=gitlab_id, role=role, actor=actor,
                                      created_at=hdb.now_iso())
    record(f"{person.name or person.username} 改為{ROLES[role]}", actor)
    return saved


def set_roles(changes: dict[int, str], actor: str) -> int:
    """Several role changes in one go: all validated first, only real changes
    stored, one rescore and one log line for the lot. Returns how many changed."""
    if any(role not in ROLES for role in changes.values()):
        raise ValueError("有不認得的身分")
    people = {p.gitlab_id: p for p in Person.objects.filter(gitlab_id__in=list(changes))}
    if set(changes) - set(people):
        raise ValueError("有不存在的人")
    current = {}
    for r in PersonRole.objects.filter(person_id__in=list(changes)).order_by("created_at", "id"):
        current[r.person_id] = r.role
    real = {pid: role for pid, role in changes.items() if current.get(pid, "member") != role}
    if not real:
        return 0
    now = hdb.now_iso()
    with transaction.atomic(using="history"):
        for pid, role in real.items():
            PersonRole.objects.create(person_id=pid, role=role, actor=actor, created_at=now)
    what = "、".join(f"{people[p].name or people[p].username}→{ROLES[r]}" for p, r in real.items())
    record(f"身分變更:{what}"[:300], actor)
    return len(real)


def members() -> dict:
    """Everyone the history knows, with their current role, plus pending additions."""
    roles = {}
    for r in PersonRole.objects.order_by("created_at", "id"):
        roles[r.person_id] = r.role
    people = []
    for p in Person.objects.all():
        role = roles.get(p.gitlab_id, "member")
        people.append({"person": p, "role": role, "role_label": ROLES.get(role, role),
                       "mrs": MergeRequest.objects.filter(author_id=p.gitlab_id).count()})
    order = {"lead": 0, "member": 1, "external": 2, "departed": 3}
    people.sort(key=lambda x: (order.get(x["role"], 1), (x["person"].name or x["person"].username)))
    return {"people": people, "pending": list(RosterAddition.objects.filter(resolved_id__isnull=True)),
            "roles": ROLES}


def add_member(username: str, actor: str) -> RosterAddition:
    """Queue a GitLab username; the next history run looks it up (started now)."""
    username = (username or "").strip().lstrip("@")
    if not username or not all(c.isalnum() or c in "._-" for c in username):
        raise ValueError("請輸入 GitLab 帳號(例如 pk123)")
    if Person.objects.filter(username=username).exists():
        raise ValueError(f"{username} 已經在名單裡了")
    added = RosterAddition.objects.create(username=username, actor=actor, created_at=hdb.now_iso())
    request_sync("sync", actor)
    return added


def email_page() -> dict:
    version, cfg, rows, attributed = team_full()
    people = {p.gitlab_id: p for p in Person.objects.all()}
    owners = attributed["owners"]
    manual = set(EmailAlias.objects.values_list("email", flat=True))
    known = [{"email": e, "person": people.get(pid), "manual": e in manual}
             for e, pid in sorted(owners.items())]
    # commit emails from the projects the team works in (history/discover.py) —
    # someone we cannot name has their direct commits and MRs left out
    unknown = {e["email"]: dict(e, commits=0, projects=[]) for e in attributed["unknown_emails"]}
    conn = history_conn()
    try:
        for r in conn.execute("SELECT author_email, author_name, project, COUNT(*) AS n FROM project_commits "
                              "WHERE author_email IS NOT NULL GROUP BY author_email, project"):
            if r["author_email"] in owners:
                continue
            e = unknown.setdefault(r["author_email"], {"email": r["author_email"], "name": r["author_name"],
                                                       "findings": 0, "release_findings": 0,
                                                       "commits": 0, "projects": []})
            e["commits"] += r["n"]
            e["projects"].append(r["project"].rsplit("/", 1)[-1])
    finally:
        conn.close()
    unknown = sorted(unknown.values(), key=lambda e: (-e["findings"], -e["commits"]))
    return {"unknown": unknown, "known": known,
            "people": sorted(people.values(), key=lambda p: p.name or p.username),
            "history": list(EmailAlias.objects.select_related("person")[:30])}


def confirm_email(email: str, gitlab_id: int | None, actor: str) -> EmailAlias:
    email = (email or "").strip().lower()
    if "@" not in email:
        raise ValueError("不是 email")
    if gitlab_id is not None and not Person.objects.filter(gitlab_id=gitlab_id).exists():
        raise ValueError("沒有這個人")
    saved = EmailAlias.objects.create(email=email, person_id=gitlab_id, actor=actor,
                                      created_at=hdb.now_iso())
    record(f"email {email} 對應{'到 ' + str(saved.person) if gitlab_id else '設為忽略'}", actor)
    return saved


def score_log(gitlab_id: int | None = None, limit: int = 200) -> list:
    events = ScoreEvent.objects.all()
    if gitlab_id is not None:
        events = events.filter(gitlab_id=gitlab_id)
    return list(events[:limit])


def review_finding(note_id: int, actor: str, category: str | None = None,
                   excluded: bool | None = None, reason: str = "") -> FindingReview:
    if category is not None and category not in CATEGORIES:
        raise ValueError(f"unknown category {category!r}")
    if category is None and excluded is None:
        raise ValueError("nothing to change")
    saved = FindingReview.objects.create(
        note_id=note_id, category=category, actor=actor, created_at=hdb.now_iso(),
        excluded=None if excluded is None else int(excluded), reason=reason.strip() or None)
    what = (f"面向改為 {CATEGORIES[category]}" if category else
            "標記誤判" if excluded else "恢復計分")
    record(f"覆核 finding #{note_id}:{what}", actor)
    return saved


FOLLOWUP_VERDICTS = {"confirmed": "確認是後續 bug", "unrelated": "不是同一個問題"}


def review_followup(feature_mr_id: int, kind: str, source_ref: str, verdict: str,
                    actor: str, reason: str = ""):
    """Append a verdict on a guessed follow-up, then rescore (same chain as a
    finding review). The follow-up itself must exist — a verdict is never free text."""
    from .models import Followup
    if verdict not in FOLLOWUP_VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r}")
    if not Followup.objects.filter(feature_mr_id=feature_mr_id, kind=kind,
                                   source_ref=source_ref).exists():
        raise ValueError("沒有這筆後續 bug")
    saved = FollowupReview.objects.create(
        feature_mr_id=feature_mr_id, kind=kind, source_ref=source_ref, verdict=verdict,
        reason=reason.strip() or None, actor=actor, created_at=hdb.now_iso())
    record(f"後續 bug 覆核(MR {feature_mr_id} · {kind}):{FOLLOWUP_VERDICTS[verdict]}", actor)
    return saved


def validate_scoring(cfg) -> list[str]:
    """Same shape as history.db.DEFAULT_SCORING, sane numbers, ordered levels."""
    errors = []
    if not isinstance(cfg, dict):
        return ["設定必須是 JSON 物件"]
    missing = set(hdb.DEFAULT_SCORING) - set(cfg)
    if missing:
        errors.append(f"缺少欄位: {', '.join(sorted(missing))}")
    unknown = set(cfg) - set(hdb.DEFAULT_SCORING)
    if unknown:
        errors.append(f"不認得的欄位: {', '.join(sorted(unknown))}(舊版公式的欄位已不使用)")

    def number(v, lo=None, hi=None):
        return isinstance(v, (int, float)) and not isinstance(v, bool) and \
            (lo is None or v >= lo) and (hi is None or v <= hi)

    for key in ("window_days", "recent_days", "min_reviewed_mrs", "followup_days", "item_max"):
        if key in cfg and not number(cfg[key], 1):
            errors.append(f"{key} 必須是 ≥ 1 的數字")
    if "prior_strength" in cfg and not number(cfg["prior_strength"], 0):
        errors.append("prior_strength 必須是非負數")
    top = cfg["item_max"] if number(cfg.get("item_max"), 1) else 5
    if "prior_score" in cfg and not number(cfg["prior_score"], 1, top):
        errors.append(f"prior_score 必須在 1 到 {top:g} 之間")
    for key, names in (("finding_cap", {"high", "medium", "low"}),
                       ("escape_increment", {"high", "medium", "low"}),
                       ("followup_increment", {"fix_mr", "ai_refind"})):
        values = cfg.get(key)
        if key in cfg and (not isinstance(values, dict) or set(values) != names
                           or not all(number(v, 0, top) for v in values.values())):
            errors.append(f"{key} 必須剛好是 {' / '.join(sorted(names))} → 0~{top:g} 的數字")
    tracks = cfg.get("tracks")
    if "tracks" in cfg:
        if not isinstance(tracks, dict) or not tracks or not all(
                isinstance(t, dict) and isinstance(t.get("label"), str)
                and isinstance(t.get("match"), list) and all(isinstance(m, str) for m in t["match"])
                for t in tracks.values()):
            errors.append('tracks 必須是 {key: {"label": "前端", "match": ["/frontend/"]}}')
        elif sum(1 for t in tracks.values() if not t["match"]) != 1:
            errors.append("tracks 要剛好一條 match 是空的(對不到任何規則的專案歸到那條)")
    if "fullstack_bonus" in cfg and not number(cfg["fullstack_bonus"], 0, 10):
        errors.append("fullstack_bonus 必須在 0 到 10 之間")
    if "fullstack_min_score" in cfg and not number(cfg["fullstack_min_score"], 0, top * len(score.ITEMS)):
        errors.append("fullstack_min_score 必須在 0 到總分上限之間")
    req = cfg.get("required_items")
    if "required_items" in cfg and (not isinstance(req, list) or set(req) - set(CATEGORIES)):
        errors.append(f"required_items 只能列: {', '.join(CATEGORIES)}")
    levels = cfg.get("levels")
    if "levels" in cfg:
        if not isinstance(levels, list) or not levels or not all(
                isinstance(lv, dict) and isinstance(lv.get("level"), str) for lv in levels):
            errors.append("levels 必須是 [{level, min_score, ...}, …]")
        else:
            total = top * len(score.ITEMS)
            floors = [lv.get("min_score") for lv in levels]
            if not all(c is None or number(c, 0, total) for c in floors):
                errors.append(f"min_score 必須是 0~{total:g} 或 null")
            elif floors[-1] is not None or any(c is None for c in floors[:-1]):
                errors.append("只有最後一級的 min_score 可以是 null(其餘級距都要有下限)")
            elif floors[:-1] != sorted(floors[:-1], reverse=True) or \
                    len(set(floors[:-1])) != len(floors) - 1:
                errors.append("min_score 必須由高到低遞減(最好的等級寫在最前面)")
            names = [lv["level"] for lv in levels]
            if len(set(names)) != len(names):
                errors.append("level 名稱不能重複")
            for lv in levels:
                bad = set(lv) - {"level", *score.GATES}
                if bad:
                    errors.append(f"{lv['level']}: 不認得的欄位 {', '.join(sorted(bad))}"
                                  f"(可用: {', '.join(score.GATES)})")
                for key in ("max_high", "max_escaped", "max_confirmed_followups", "min_coverage",
                            "min_mrs", "min_recent_mrs"):
                    v = lv.get(key)
                    if v is not None and not (isinstance(v, int) and not isinstance(v, bool)
                                              and v >= 0):
                        errors.append(f"{lv['level']}: {key} 必須是非負整數或省略")
                if lv.get("min_coverage") is not None and isinstance(lv["min_coverage"], int) \
                        and lv["min_coverage"] > len(score.ITEMS):
                    errors.append(f"{lv['level']}: min_coverage 最多 {len(score.ITEMS)}")
                v = lv.get("min_item")
                if v is not None and not number(v, 0, top):
                    errors.append(f"{lv['level']}: min_item 必須在 0 到 item_max 之間")
                v = lv.get("min_clean_rate")
                if v is not None and not number(v, 0, 1):
                    errors.append(f"{lv['level']}: min_clean_rate 必須在 0 到 1 之間(0.9 = 90%)")
    return errors


def new_scoring_version(cfg: dict, note: str, actor: str) -> ScoringConfig:
    errors = validate_scoring(cfg)
    if errors:
        raise ValueError("; ".join(errors))
    for _ in range(5):     # two saves at once pick the same number; the loser retries
        latest = ScoringConfig.objects.order_by("-version").first()
        try:
            with transaction.atomic(using="history"):
                saved = ScoringConfig.objects.create(
                    version=(latest.version if latest else 0) + 1,
                    config=json.dumps(cfg, ensure_ascii=False), note=note.strip() or None,
                    actor=actor, created_at=hdb.now_iso())
        except IntegrityError:
            continue
        record(f"評分公式改為 v{saved.version}" + (f"({saved.note})" if saved.note else ""), actor)
        return saved
    raise ValueError("同時有太多人在改評分設定,請重試")


def request_sync(kind: str, actor: str) -> SyncRequest:
    """Queue it, then start the core job detached — the web request never runs a
    sync itself, and the job's own lock means a second click cannot double it."""
    if kind not in SYNC_KINDS:
        raise ValueError(f"unknown sync kind {kind!r}")
    req = SyncRequest.objects.create(kind=kind, requested_by=actor, requested_at=hdb.now_iso())
    try:
        kick_history_job()
    except OSError as exc:
        # the request must not sit "queued" forever: close it with the reason
        # (the core owns the request lifecycle columns, so it writes them)
        conn = history_conn()
        with conn:
            conn.execute("UPDATE sync_requests SET started_at = ?, finished_at = ?, result = ? "
                         "WHERE id = ?", (hdb.now_iso(), hdb.now_iso(),
                                          json.dumps({"failed": {"start": str(exc)}}), req.id))
        conn.close()
        raise SyncStartError(f"背景同步啟動失敗:{exc}") from exc
    return req


class SyncStartError(Exception):
    pass


def kick_history_job() -> None:
    """The job gets the dashboard's db path explicitly, so it always writes the
    file this page shows (whatever config.json or the shell environment says)."""
    repo = settings.REPO_ROOT
    env = {**os.environ, "MR_SENTINEL_DB": str(history_path())}
    with open(repo / "history-dashboard.spawn.log", "a") as log:
        subprocess.Popen([sys.executable, "-m", "history", "run"], cwd=str(repo), env=env,
                         stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True)


# ---------- 個人軌跡 ----------

def _fmt(value, unit):
    if value is None:
        return "—"
    if unit == "%":
        return f"{value * 100:.0f}%"
    if unit == "小時":
        return f"{value:.1f} h"
    if unit == "個/週":
        return f"{value:.1f}"
    return f"{value:.2f}"


def _fmt_diff(m):
    d, unit = m.get("diff"), m["unit"]
    if d is None:
        return "—"
    if unit == "%":
        return f"{d * 100:+.0f} pp"
    if unit == "小時":
        return f"{d * 100:+.0f}%"
    if unit == "個/週":
        return f"{d:+.1f}"
    return f"{d:+.2f}"


def _fmt_interval(m):
    iv, unit = m.get("interval"), m["unit"]
    if not iv:
        return None
    if unit == "%":
        return f"{iv[0] * 100:+.0f} ~ {iv[1] * 100:+.0f} pp"
    if unit == "小時":
        return f"{iv[0] * 100:+.0f}% ~ {iv[1] * 100:+.0f}%"
    return f"{iv[0]:+.2f} ~ {iv[1]:+.2f}"


TONE = {"strong_worse": "watch", "observe_worse": "watch-soft", "worse": "down",
        "strong_better": "improve", "observe_better": "improve-soft", "better": "up",
        "uncertain": "flat", "insufficient": "none", "not_comparable": "none", "activity": "flat"}


def trajectory_page(view: str = "todo", person: int | None = None) -> dict:
    """Everything the 個人軌跡 page shows, formatted; the numbers come from
    history.trajectory, the alert states from its tables."""
    from history import trajectory as tj
    conn = history_conn()
    analysis_as_of = datetime.now(timezone.utc)
    try:
        conn.execute("BEGIN")                          # one consistent snapshot for every read below
        _, cfg = hdb.scoring_config(conn)
        analyses = tj.analyze(conn, cfg, analysis_as_of, only=person)
        open_ = tj.open_alerts(conn)
        as_of = hdb.get_state(conn, "trajectory_as_of")
        closed = [dict(r) for r in conn.execute(
            "SELECT * FROM trajectory_alerts WHERE closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT 50")]
        last_sync = hdb.get_state(conn, "last_sync_at")
        members = [pid for pid, role in hdb.person_roles(conn).items() if role == "member"]
        conn.rollback()
    finally:
        conn.close()
    people = {p.gitlab_id: p for p in Person.objects.filter(
        gitlab_id__in=set(analyses) | set(members) | {a["person_id"] for a in closed})}
    rows, queue = [], []
    for pid, a in analyses.items():
        p = people.get(pid)
        metrics = []
        for m in a["metrics"]:
            if m["unit"] == "%":          # a rate: the raw count, never the prior-pulled estimate
                base_text = f"{m.get('x_base', 0)}/{m['n_base']}" if m["n_base"] else "—"
                recent_text = f"{m.get('x_recent', 0)}/{m['n_recent']}" if m["n_recent"] else "—"
            elif m["unit"] in ("分", "小時"):
                base_text, recent_text = _fmt(m.get("raw_base"), m["unit"]), _fmt(m.get("raw_recent"), m["unit"])
            else:
                base_text, recent_text = _fmt(m.get("base"), m["unit"]), _fmt(m.get("recent"), m["unit"])
            metrics.append({**m, "tone": TONE.get(m["state"], "flat"), "as_of": a["as_of"],
                            "base_text": base_text, "recent_text": recent_text,
                            "diff_text": _fmt_diff(m), "interval_text": _fmt_interval(m),
                            "need_text": f"需 {m['need'][1]}/{m['need'][0]}" if m["need"][0] else ""})
        by_key = {m["key"]: m for m in metrics}
        alerts = []
        for al in open_.get(pid, []):
            m = by_key.get(al["metric"])
            if m:
                snap = json.loads(al["snapshot"] or "{}")
                fmt_unit = m["unit"]
                if fmt_unit == "%":
                    opened = f"{snap.get('x_base', 0)}/{snap.get('n_base', 0)} → {snap.get('x_recent', 0)}/{snap.get('n_recent', 0)}"
                else:
                    opened = f"{_fmt(snap.get('raw_base', snap.get('base')), fmt_unit)} → {_fmt(snap.get('raw_recent', snap.get('recent')), fmt_unit)}"
                p_open = snap.get("p_worse") if al["kind"] == "watch" else snap.get("p_better")
                alerts.append({**al, "m": m, "snap": snap, "opened_text": opened, "p_open": p_open,
                               "now_unknown": m["state"] in ("insufficient", "not_comparable"),   # from now, not the saved flag
                               "now_matches": m["state"] == ("strong_worse" if al["kind"] == "watch" else "strong_better")})
        main_insufficient = all(by_key[k]["state"] == "insufficient"
                                for k in ("requirements", "verification", "escape_rate", "bug_rate", "cycle_time"))
        row = {"pid": pid, "person": p, "name": (p.name or p.username) if p else str(pid), "a": a,
               "groups": [(g, [m for m in metrics if m["group"] == g]) for g in tj.GROUPS],
               "alerts": alerts, "insufficient": main_insufficient}
        rows.append(row)
        for c in a["cases"]:
            c["category_label"] = CATEGORY_LABELS.get(c["finding"].get("category"), "")
            queue.append({"type": "case", "rank": 0, "row": row, "case": c})
        for al in alerts:
            rank = (1 if not al["acknowledged_at"] else 2) if al["kind"] == "watch" else 3
            queue.append({"type": "alert", "rank": rank, "row": row, "alert": al})
        if a["together"]:
            queue.append({"type": "together", "rank": 1, "row": row})
    queue.sort(key=lambda q: (q["rank"], q["row"]["name"]))
    counts = {"cases": sum(1 for q in queue if q["type"] == "case"),
              "todo": sum(1 for q in queue if q["type"] in ("alert", "together") and q["rank"] == 1),
              "tracking": sum(1 for q in queue if q["type"] == "alert" and q["rank"] == 2),
              "positive": sum(1 for q in queue if q["type"] == "alert" and q["rank"] == 3),
              "insufficient": sum(1 for r in rows if r["insufficient"])}
    if view == "positive":
        shown = [q for q in queue if q["rank"] == 3]
    elif view == "todo":
        shown = [q for q in queue if q["rank"] < 3]
    else:
        shown = []
    rows.sort(key=lambda r: (not r["alerts"] and not r["a"]["cases"], r["name"]))
    windows = next(iter(analyses.values()))["windows"] if analyses else None
    for c in closed:
        c["name"] = (people[c["person_id"]].name or people[c["person_id"]].username) if c["person_id"] in people else c["person_id"]
        c["label"] = tj.METRICS.get(c["metric"], {}).get("label", c["metric"])
    return {"view": view, "rows": rows, "queue": shown, "counts": counts, "closed": closed if view == "closed" else [],
            "windows": windows, "last_sync": last_sync, "person_filter": person, "as_of": as_of,
            "analysis_as_of": analysis_as_of,
            "all_people": sorted(((r["pid"], r["name"]) for r in rows), key=lambda x: x[1]) if not person else
            sorted(((pid, (people[pid].name or people[pid].username) if pid in people else pid)
                    for pid in set(analyses) | set(members)), key=lambda x: str(x[1]))}   # switch to anyone


def trajectory_ack(alert_id: int, actor: str, note: str = "") -> bool:
    from history import trajectory as tj
    conn = history_conn()
    try:
        return tj.acknowledge(conn, alert_id, actor, note)
    finally:
        conn.close()


def trajectory_end(alert_id: int, actor: str, reason: str) -> bool:
    from history import trajectory as tj
    conn = history_conn()
    try:
        return tj.end_tracking(conn, alert_id, actor, reason)
    finally:
        conn.close()
