"""Everything the views need, built on the core `history` package.

Scores come from history.score — the same code the CLI report and (later) the
coding tutor use — so the dashboard can never show a number the core would not.
"""
import json
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
        r["evaluation"] = evals.get(r["author_id"])
        r["evaluation_stale"] = r["author_id"] in stale
        r["records"] = mr_records(r, attributed)
    ranked = [r for r in rows if r["ranked"]]
    leads = [r for r in rows if r["role"] == "lead"]
    departed = [r for r in rows if r["role"] == "departed"]
    levels = {lv["level"]: sum(1 for r in ranked if r["level"] == lv["level"])
              for lv in cfg["levels"]}
    return {
        "version": version, "cfg": cfg, "ranked": ranked, "leads": leads, "departed": departed,
        "events": list(ScoreEvent.objects.all()[:12]),
        "unknown_emails": len(attributed["unknown_emails"]),
        "kpi": {"people": len(ranked),
                "reviewed": sum(r["reviewed_mrs"] for r in ranked),
                "findings": sum(r["findings"] for r in ranked),
                "followups": sum(sum(r["followups"].values()) for r in ranked),
                "levels": levels,
                "unrated": sum(1 for r in ranked if r["level"] is None)},
        "radar": radar_chart(score.team_average(rows, cfg), None, "團隊平均", cfg["item_max"]),
        "team_score": score.team_score(rows, cfg), "item_short": ITEM_SHORT,
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
            "trend": _trend(mrs, findings, followups, cfg),
            "items": [{"key": k, **v} for k, v in summary["items"].items()] if summary else [],
            "window_start": window_start, "max_total": score.max_total(cfg),
            "evaluation": evals.get(author_id), "evaluation_stale": author_id in stale,
            "followup_verdicts": FOLLOWUP_VERDICTS}


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
        r["weight"] = round(score.followup_weight(r, cfg), 2)
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


def _trend(mrs: list, findings: list[dict], followups: list[dict], cfg: dict) -> list[dict]:
    """Per month (of the MR): reviewed MRs, counted findings, and that month's
    total — findings + follow-ups per item, the same formula as the score."""
    months = defaultdict(lambda: {"mrs": 0, "findings": 0, "weight": 0.0,
                                  "weights": defaultdict(float)})
    for m in mrs:
        if m.reviewed and m.created_at:
            months[m.created_at[:7]]["mrs"] += 1
    for f in findings:
        if f["counted"] and f["category"] in CATEGORIES and f["mr"].created_at:
            month = months[f["mr"].created_at[:7]]
            month["findings"] += 1
            w = score.finding_weight(f, cfg)
            month["weight"] += w
            month["weights"][f["category"]] += w
    for fu in followups:
        if fu["feature"].created_at and fu.get("verdict") != "unrelated":
            month = months[fu["feature"].created_at[:7]]
            month["weight"] += fu["weight"]
            month["weights"][score.followup_category(fu)] += fu["weight"]
    top = score.max_total(cfg)
    out = []
    for key in sorted(months):
        v = months[key]
        month_score = score.total_score(v["weights"], v["mrs"], cfg) if v["mrs"] else None
        out.append({"month": key, "mrs": v["mrs"], "findings": v["findings"],
                    "weight": round(v["weight"], 2), "score": month_score,
                    "bar": round(100 * month_score / top) if month_score is not None else 0})
    return out


# ---------- writing (append-only) ----------


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
    order = {"lead": 0, "member": 1, "departed": 2}
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
    known = [{"email": e, "person": people.get(pid)} for e, pid in sorted(owners.items())]
    return {"unknown": attributed["unknown_emails"], "known": known,
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
    for key in ("window_days", "min_reviewed_mrs", "followup_days", "deduction_per_weight",
                "item_max"):
        if key in cfg and not (isinstance(cfg[key], (int, float)) and cfg[key] > 0):
            errors.append(f"{key} 必須是正數")
    for key in ("escape_multiplier", "unconfirmed_followup_factor"):
        if key in cfg and not (isinstance(cfg[key], (int, float)) and cfg[key] >= 0):
            errors.append(f"{key} 必須是非負數")
    if isinstance(cfg.get("unconfirmed_followup_factor"), (int, float)) and \
            cfg["unconfirmed_followup_factor"] > 1:
        errors.append("unconfirmed_followup_factor 不能大於 1(未確認的不該比確認的重)")
    for key in ("severity_weight", "category_multiplier", "followup_weight"):
        values = cfg.get(key)
        if key in cfg and (not isinstance(values, dict) or not all(
                isinstance(v, (int, float)) and v >= 0 for v in values.values())):
            errors.append(f"{key} 必須是「名稱 → 非負數」")
    if isinstance(cfg.get("severity_weight"), dict) and \
            set(cfg["severity_weight"]) != {"high", "medium", "low"}:
        errors.append("severity_weight 必須剛好是 high / medium / low")
    if isinstance(cfg.get("followup_weight"), dict) and \
            set(cfg["followup_weight"]) != {"fix_mr", "ai_refind"}:
        errors.append("followup_weight 必須剛好是 fix_mr / ai_refind")
    if set((cfg.get("category_multiplier") or {})) - set(CATEGORIES):
        errors.append(f"category_multiplier 只能用: {', '.join(CATEGORIES)}")
    levels = cfg.get("levels")
    if "levels" in cfg:
        if not isinstance(levels, list) or not levels or not all(
                isinstance(lv, dict) and isinstance(lv.get("level"), str) for lv in levels):
            errors.append("levels 必須是 [{level, min_score}, …]")
        else:
            floors = [lv.get("min_score") for lv in levels]
            top = (cfg["item_max"] * len(score.ITEMS)
                   if isinstance(cfg.get("item_max"), (int, float)) else 40)
            if not all(c is None or (isinstance(c, (int, float)) and 0 <= c <= top)
                       for c in floors):
                errors.append(f"min_score 必須是 0~{top:g} 或 null")
            elif floors[-1] is not None or any(c is None for c in floors[:-1]):
                errors.append("只有最後一級的 min_score 可以是 null(其餘級距都要有下限)")
            elif floors[:-1] != sorted(floors[:-1], reverse=True) or \
                    len(set(floors[:-1])) != len(floors) - 1:
                errors.append("min_score 必須由高到低遞減(最好的等級寫在最前面)")
            names = [lv["level"] for lv in levels]
            if len(set(names)) != len(names):
                errors.append("level 名稱不能重複")
            for lv in levels:
                unknown = set(lv) - {"level", *score.GATES}
                if unknown:
                    errors.append(f"{lv['level']}: 不認得的欄位 {', '.join(sorted(unknown))}"
                                  f"(可用: {', '.join(score.GATES)})")
                for key in ("max_high", "max_fix_mr", "max_escaped"):
                    v = lv.get(key)
                    if v is not None and not (isinstance(v, int) and not isinstance(v, bool)
                                              and v >= 0):
                        errors.append(f"{lv['level']}: {key} 必須是非負整數或省略")
                v, cap = lv.get("min_item"), cfg.get("item_max", 5)
                if v is not None and not (isinstance(v, (int, float)) and 0 <= v <= cap):
                    errors.append(f"{lv['level']}: min_item 必須在 0 到 item_max 之間")
                v = lv.get("min_clean_rate")
                if v is not None and not (isinstance(v, (int, float)) and 0 <= v <= 1):
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
