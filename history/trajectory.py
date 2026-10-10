"""個人軌跡 — is a person's way of working changing, before they notice?

Designed and reviewed with Codex. The rules:

  * Compare the person with *their own past*: the last 28 days (R) against the
    56 days before (B). The rest of the team only stabilises small samples, as a
    weak prior — never a ranking.
  * A sample is an MR the person merged (not a release), or their own commits
    inside a release MR graded on their own (a "slice", history/rate.py) — the
    two are counted separately on the page. The 8 grades of one MR are not 8
    samples. Direct-commit batches are not samples here.
  * Change is a posterior past a *practical* threshold, never a raw difference:
      ratings    Dirichlet over the 5 grades, prior 4 × the team's distribution
      rates      Beta-Binomial, prior Beta(2·p₀, 2·(1−p₀)) from the team
      cycle time bootstrap of the ratio of medians, ±25 % both ways
    The numbers shown are "probability the change exceeds the threshold", not
    p-values; 0.949 vs 0.951 is within Monte-Carlo error.
  * Alerts (要關注 / 進步很多) only on the main metrics, only when two judgments
    in a row that each saw *new observations* (a set difference, not a count)
    say the same; a stretch of "not enough data" breaks the streak. Held ≥ 14
    days; closed after two calm judgments (the original direction's half-
    threshold probability < 0.70); then 28 days of cool-down, and a new episode
    must be built from judgments made after the close. 已檢視 records that a
    human looked — it never closes an alert; 結束追蹤 does, with a reason.
  * A high-severity finding shipped unfixed is a *case*: listed at once for 84
    days, never read as a trend.

Commit counts and MR counts are activity only: with AI writing the code they
measure how work is split, not speed. "Merge" is not "deployed": nothing here
knows when code reached production.
"""
import hashlib
import json
import math
import random
import statistics
from datetime import datetime, timedelta, timezone

from history import db, score
from history.parse import CATEGORIES

RECENT_DAYS, BASE_DAYS, BUG_MATURITY = 28, 56, 30
CASE_DAYS = RECENT_DAYS + BASE_DAYS
DRAWS = 4000
HOLD_DAYS, COOLDOWN_DAYS = 14, 28
CYCLE_MIN_HOURS = 1.0          # a faster / slower median must also move this much
SELF_MERGE_MAX = 0.8           # above this the open→merge time says nothing about flow
MODEL_VERSION = 2

METRICS = {
    "requirements": {"label": "需求符合度", "group": "主要品質訊號", "better": "higher", "alert": True,
                     "threshold": 0.5, "unit": "分", "kind": "rating",
                     "note": "規劃:MR description 有沒有把要做什麼說清楚、做對"},
    "verification": {"label": "驗證有效性", "group": "主要品質訊號", "better": "higher", "alert": True,
                     "threshold": 0.5, "unit": "分", "kind": "rating",
                     "note": "把關:測試能不能抓到問題"},
    "escape_rate": {"label": "merge 時未處理 finding 的比例", "group": "merge 與後續問題", "better": "lower",
                    "alert": True, "threshold": 0.15, "unit": "%", "kind": "rate",
                    "note": "bot review 過的 MR 中,merge 時仍有 finding 沒處理的比例(依目前討論串狀態推定)"},
    "bug_rate": {"label": "merge 後 30 天確認的後續 bug", "group": "merge 與後續問題", "better": "lower",
                 "alert": True, "threshold": 0.15, "unit": "%", "kind": "rate",
                 "note": "merge 滿 30 天的 MR 中,有人工確認後續 bug 的比例;未確認的推估不算"},
    "cycle_time": {"label": "MR 開啟至 merge 時間", "group": "MR 流程時間", "better": "lower", "alert": True,
                   "threshold": 0.25, "unit": "小時", "kind": "cycle",
                   "note": "中位數,含等待 review;self-merge 比例高或改變時不判定"},
    **{c: {"label": label, "group": "輔助品質趨勢", "better": "higher", "alert": False, "threshold": 0.5,
           "unit": "分", "kind": "rating", "note": "輔助趨勢,不單獨警示"}
       for c, label in CATEGORIES.items() if c not in ("requirements", "verification")},
    "activity": {"label": "Merged MR 數", "group": "活動與資料覆蓋", "better": None, "alert": False,
                 "threshold": None, "unit": "個/週", "kind": "activity",
                 "note": "活動量,只用來理解工作怎麼切分;AI 寫 code 時不代表速度"},
}
GROUPS = ["主要品質訊號", "merge 與後續問題", "MR 流程時間", "輔助品質趨勢", "活動與資料覆蓋"]
MIN = {"rating": (5, 8), "rate": (5, 8), "bug": (8, 10), "cycle": (5, 8)}
STATE_TEXT = {
    "insufficient": "樣本不足,暫不判定", "uncertain": "未達變化判定條件", "better": "可能改善",
    "worse": "可能變差", "observe_better": "觀察:改善", "observe_worse": "觀察:變差",
    "strong_better": "改善達門檻", "strong_worse": "變差達門檻", "activity": "活動量",
    "not_comparable": "流程不可比,暫不判定",
}


def _ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def _rng(*parts) -> random.Random:
    return random.Random(int(hashlib.sha1(json.dumps(parts, default=str).encode()).hexdigest()[:12], 16))


def _dirichlet(rng, alpha):
    g = [rng.gammavariate(a, 1.0) if a > 0 else 0.0 for a in alpha]
    total = sum(g) or 1.0
    return [x / total for x in g]


def rating_change(recent: list[int], base: list[int], team: list[int], threshold: float, seed) -> dict:
    """Posterior of the mean grade in R and B; better = higher."""
    team_counts = [team.count(k) + 0.5 for k in range(1, 6)]
    q = [c / sum(team_counts) for c in team_counts]
    prior = [4 * x for x in q]
    rng = _rng("rating", seed)
    rc = [recent.count(k) for k in range(1, 6)]
    bc = [base.count(k) for k in range(1, 6)]
    diffs, rs, bs = [], [], []
    for _ in range(DRAWS):
        pr = _dirichlet(rng, [p + c for p, c in zip(prior, rc)])
        pb = _dirichlet(rng, [p + c for p, c in zip(prior, bc)])
        mr = sum((k + 1) * x for k, x in enumerate(pr))
        mb = sum((k + 1) * x for k, x in enumerate(pb))
        rs.append(mr), bs.append(mb), diffs.append(mr - mb)
    return _summary(diffs, rs, bs, threshold, higher_is_better=True)


def rate_change(recent: list[bool], base: list[bool], team: list[bool], threshold: float, seed) -> dict:
    """Posterior of an event rate in R and B; better = lower."""
    p0 = (sum(team) + 1) / (len(team) + 2) if team else 0.5
    a0, b0 = 2 * p0, 2 * (1 - p0)
    rng = _rng("rate", seed)
    xr, nr, xb, nb = sum(recent), len(recent), sum(base), len(base)
    diffs, rs, bs = [], [], []
    for _ in range(DRAWS):
        r = rng.betavariate(a0 + xr, b0 + nr - xr)
        b = rng.betavariate(a0 + xb, b0 + nb - xb)
        rs.append(r), bs.append(b), diffs.append(r - b)
    return _summary(diffs, rs, bs, threshold, higher_is_better=False)


def cycle_change(recent: list[float], base: list[float], threshold: float, seed) -> dict:
    """Bootstrap of median(R) / median(B); better = lower (faster), ±threshold both
    ways (0.75 / 1.25), and the medians must also differ by CYCLE_MIN_HOURS."""
    rng = _rng("cycle", seed)
    ratios, gaps = [], []
    for _ in range(DRAWS // 2):
        r = statistics.median(rng.choices(recent, k=len(recent)))
        b = statistics.median(rng.choices(base, k=len(base)))
        ratios.append(math.log(max(r, 0.01) / max(b, 0.01)))
        gaps.append(r - b)
    better_full, worse_full = math.log(1 - threshold), math.log(1 + threshold)
    better_half, worse_half = math.log(1 - threshold / 2), math.log(1 + threshold / 2)
    n = len(ratios)
    pairs = list(zip(ratios, gaps))

    def frac(pred):
        return sum(1 for x, g in pairs if pred(x, g)) / n
    s = sorted(ratios)
    return {"recent": round(statistics.median(recent), 1), "base": round(statistics.median(base), 1),
            "diff": round(math.exp(statistics.median(ratios)) - 1, 2),
            "interval": [round(math.exp(s[int(n * .05)]) - 1, 2), round(math.exp(s[int(n * .95)]) - 1, 2)],
            "p_better": frac(lambda x, g: x <= better_full and -g >= CYCLE_MIN_HOURS),
            "p_worse": frac(lambda x, g: x >= worse_full and g >= CYCLE_MIN_HOURS),
            "p_better_half": frac(lambda x, g: x <= better_half),
            "p_worse_half": frac(lambda x, g: x >= worse_half)}


def _summary(diffs, rs, bs, threshold, higher_is_better: bool) -> dict:
    diffs.sort()
    n = len(diffs)
    sign = 1 if higher_is_better else -1
    return {"recent": round(statistics.mean(rs), 3), "base": round(statistics.mean(bs), 3),
            "diff": round(statistics.median(diffs), 3),
            "interval": [round(diffs[int(n * .05)], 3), round(diffs[int(n * .95)], 3)],
            "p_better": sum(sign * d >= threshold for d in diffs) / n,
            "p_worse": sum(sign * d <= -threshold for d in diffs) / n,
            "p_better_half": sum(sign * d >= threshold / 2 for d in diffs) / n,
            "p_worse_half": sum(sign * d <= -threshold / 2 for d in diffs) / n}


def state_of(c: dict) -> str:
    if c["p_better"] >= 0.95:
        return "strong_better"
    if c["p_worse"] >= 0.95:
        return "strong_worse"
    if c["p_better"] >= 0.90:
        return "observe_better"
    if c["p_worse"] >= 0.90:
        return "observe_worse"
    if c["p_better_half"] >= 0.75:
        return "better"
    if c["p_worse_half"] >= 0.75:
        return "worse"
    return "uncertain"              # not "stable": a wide interval allows both


# ---------- samples ----------

def _bug_eligible(m: dict) -> bool:
    """One cohort rule for the person and for the team prior."""
    return not m.get("slice") and not m["is_fix"]


def _samples(conn, cfg: dict, now: datetime) -> dict:
    """Per person: what each merged MR (or release slice) says."""
    attributed = score.attribution(conn, cfg, now)
    ratings = score.latest_ratings(conn)
    followups = score.annotated_followups(conn, attributed["findings"] + attributed["unattributed"])
    confirmed, pending = {}, {}
    for fu in followups:
        within = (fu.get("days_after") or 0) <= BUG_MATURITY
        if fu.get("verdict") == "confirmed" and within:
            confirmed[fu["feature_mr_id"]] = confirmed.get(fu["feature_mr_id"], 0) + 1
        elif fu.get("verdict") is None and within:
            pending[fu["feature_mr_id"]] = pending.get(fu["feature_mr_id"], 0) + 1
    escaped, cases = {}, {}
    for f in attributed["findings"] + attributed["unattributed"]:
        if f["excluded"] or f["appeal_accepted"] or not f.get("escaped"):
            continue
        escaped[f["mr_id"]] = True
        if f.get("severity") == "high":
            cases.setdefault(f.get("owner_author_id"), []).append(f)
    files = {r[0]: r[1] for r in conn.execute("SELECT mr_id, COUNT(*) FROM mr_files GROUP BY mr_id")}
    out: dict[int, list] = {}
    mrs = attributed["mrs"]
    for (mr_id, pid), cats in score.slice_ratings(conn).items():
        m = mrs.get(mr_id)
        grades = {c: (cats.get(c) or {}).get("score") for c in CATEGORIES}
        if not m or not m.get("release") or m["state"] != "merged" or not m["merged_at"] \
                or all(g is None for g in grades.values()):
            continue
        out.setdefault(pid, []).append({
            "mr_id": mr_id, "project": m["project"], "iid": m["iid"], "title": m["title"],
            "web_url": m["web_url"], "merged_at": _ts(m["merged_at"]), "reviewed": False,
            "cycle_hours": None, "self_merge": None, "grades": grades,
            "reasons": {c: (cats.get(c) or {}).get("reason") for c in CATEGORIES},
            "escaped": False, "confirmed_bugs": 0, "pending_bugs": 0, "is_fix": None,
            "files": None, "track": score.track_of(m["project"], cfg), "slice": True})
    for m in mrs.values():
        if m.get("release") or m["state"] != "merged" or not m["merged_at"] or m["author_id"] is None:
            continue
        created, merged = _ts(m["created_at"]), _ts(m["merged_at"])
        hours = (merged - created).total_seconds() / 3600 if created else None
        out.setdefault(m["author_id"], []).append({
            "mr_id": m["mr_id"], "project": m["project"], "iid": m["iid"], "title": m["title"],
            "web_url": m["web_url"], "merged_at": merged, "reviewed": bool(m["reviewed"]),
            "cycle_hours": hours if hours is not None and hours >= 0 else None,   # <0 = bad data
            "self_merge": (m.get("merged_by") == m["author_id"]) if m.get("merged_by") else None,
            "grades": {c: (ratings.get(m["mr_id"], {}).get(c) or {}).get("score") for c in CATEGORIES},
            "reasons": {c: (ratings.get(m["mr_id"], {}).get(c) or {}).get("reason") for c in CATEGORIES},
            "escaped": escaped.get(m["mr_id"], False), "confirmed_bugs": confirmed.get(m["mr_id"], 0),
            "pending_bugs": pending.get(m["mr_id"], 0), "is_fix": bool(m["is_fix"]),
            "files": files.get(m["mr_id"]), "track": score.track_of(m["project"], cfg), "slice": False})
    return {"by_person": out, "cases": cases, "mrs": mrs}


def _windows(now: datetime, shift: int = 0):
    end = now - timedelta(days=shift)
    r0 = end - timedelta(days=RECENT_DAYS)
    b0 = r0 - timedelta(days=BASE_DAYS)
    return (lambda t: r0 < t <= end), (lambda t: b0 < t <= r0), (b0, r0, end)


def _weekly(mrs: list[dict], now: datetime, fn) -> list:
    """12 seven-day bins ending now — raw values, None where there is no sample."""
    out = []
    for w in range(11, -1, -1):
        end = now - timedelta(days=7 * w)
        start = end - timedelta(days=7)
        out.append(fn([m for m in mrs if start < m["merged_at"] <= end]))
    return out


def _obs(tag: str, items) -> list[str]:
    """Canonical observation ids: what a judgment saw, sorted."""
    return sorted(f"{tag}:{i}" for i in items)


def analyze_person(mrs: list[dict], team: list[dict], now: datetime, pid, cases=()) -> dict:
    in_r, in_b, (b0, r0, end) = _windows(now)
    in_rb, in_bb, bug_bounds = _windows(now, BUG_MATURITY)
    R = [m for m in mrs if in_r(m["merged_at"])]
    B = [m for m in mrs if in_b(m["merged_at"])]
    T = [m for m in team if in_r(m["merged_at"]) or in_b(m["merged_at"])]
    metrics = []

    def add(key, change, n_r, n_b, need, obs, evidence, weekly, extra=None):
        meta = METRICS[key]
        enough = n_r >= need[0] and n_b >= need[1]
        state = state_of(change) if enough and change else "insufficient"
        row = {"key": key, **meta, "n_recent": n_r, "n_base": n_b, "need": need, "state": state,
               "obs": obs, "eligible": True, "blocked": None, **(change or {}), "weekly": weekly,
               "evidence": evidence, **(extra or {})}
        row["state_text"] = STATE_TEXT.get(state, state)
        metrics.append(row)
        return row

    for c in CATEGORIES:
        r = [(m, m["grades"][c]) for m in R if m["grades"][c] is not None]
        b = [(m, m["grades"][c]) for m in B if m["grades"][c] is not None]
        t = [m["grades"][c] for m in T if m["grades"][c] is not None]
        rv, bv = [g for _, g in r], [g for _, g in b]
        ch = rating_change(rv, bv, t, METRICS[c]["threshold"], (pid, c, rv, bv)) if r and b else None
        obs = _obs("R", (f"{m['mr_id']}{'s' if m['slice'] else ''}={g}" for m, g in r)) + \
            _obs("B", (f"{m['mr_id']}{'s' if m['slice'] else ''}={g}" for m, g in b))
        support = sorted(r, key=lambda x: x[1])
        add(c, ch, len(r), len(b), MIN["rating"], obs,
            {"recent": [{"mr": m, "value": g, "why": m["reasons"][c]} for m, g in support],
             "base": [{"mr": m, "value": g, "why": m["reasons"][c]} for m, g in sorted(b, key=lambda x: -x[1])]},
            _weekly(mrs, now, lambda ms, c=c: round(statistics.mean(g), 2)
                    if (g := [m["grades"][c] for m in ms if m["grades"][c] is not None]) else None),
            {"slices_recent": sum(1 for m, _ in r if m["slice"])})

    rr = [m for m in R if m["reviewed"] and not m["slice"]]
    bb = [m for m in B if m["reviewed"] and not m["slice"]]
    tt = [m["escaped"] for m in T if m["reviewed"] and not m["slice"]]
    rv, bv = [m["escaped"] for m in rr], [m["escaped"] for m in bb]
    ch = rate_change(rv, bv, tt, METRICS["escape_rate"]["threshold"], (pid, "esc", rv, bv)) if rr and bb else None
    add("escape_rate", ch, len(rr), len(bb), MIN["rate"],
        _obs("R", (f"{m['mr_id']}={int(m['escaped'])}" for m in rr)) +
        _obs("B", (f"{m['mr_id']}={int(m['escaped'])}" for m in bb)),
        {"recent": sorted(({"mr": m, "value": m["escaped"]} for m in rr), key=lambda e: not e["value"]),
         "base": sorted(({"mr": m, "value": m["escaped"]} for m in bb), key=lambda e: not e["value"])},
        _weekly([m for m in mrs if m["reviewed"] and not m["slice"]], now,
                lambda ms: round(sum(m["escaped"] for m in ms) / len(ms), 2) if ms else None),
        {"x_recent": sum(rv), "x_base": sum(bv)})

    mr_ = [m for m in mrs if _bug_eligible(m) and in_rb(m["merged_at"])]
    mb_ = [m for m in mrs if _bug_eligible(m) and in_bb(m["merged_at"])]
    tb = [m["confirmed_bugs"] > 0 for m in team if _bug_eligible(m)
          and (in_rb(m["merged_at"]) or in_bb(m["merged_at"]))]
    rv, bv = [m["confirmed_bugs"] > 0 for m in mr_], [m["confirmed_bugs"] > 0 for m in mb_]
    ch = rate_change(rv, bv, tb, METRICS["bug_rate"]["threshold"], (pid, "bug", rv, bv)) if mr_ and mb_ else None
    row = add("bug_rate", ch, len(mr_), len(mb_), MIN["bug"],
              _obs("R", (f"{m['mr_id']}={m['confirmed_bugs']}" for m in mr_)) +
              _obs("B", (f"{m['mr_id']}={m['confirmed_bugs']}" for m in mb_)),
              {"recent": sorted(({"mr": m, "value": m["confirmed_bugs"]} for m in mr_), key=lambda e: -e["value"]),
               "base": sorted(({"mr": m, "value": m["confirmed_bugs"]} for m in mb_), key=lambda e: -e["value"])},
              [], {"x_recent": sum(rv), "x_base": sum(bv), "pending": sum(m["pending_bugs"] for m in mr_ + mb_),
                   "cohort": [d.strftime("%m-%d") for d in bug_bounds]})
    if sum(rv) < 2:                       # a rate alert needs two affected MRs, not one
        row.update(eligible=False, blocked="最近 cohort 確認的後續 bug 不到 2 個 MR")

    rc = [m for m in R if m["cycle_hours"] is not None]
    bc = [m for m in B if m["cycle_hours"] is not None]
    rv, bv = [m["cycle_hours"] for m in rc], [m["cycle_hours"] for m in bc]
    ch = cycle_change(rv, bv, METRICS["cycle_time"]["threshold"], (pid, "cyc", rv, bv)) \
        if len(rc) >= 2 and len(bc) >= 2 else None

    def self_share(ms):
        known = [m["self_merge"] for m in ms if m["self_merge"] is not None]
        return round(sum(known) / len(known), 2) if known else None
    sr, sb = self_share(rc), self_share(bc)
    row = add("cycle_time", ch, len(rc), len(bc), MIN["cycle"],
              _obs("R", (f"{m['mr_id']}={m['cycle_hours']:.2f}" for m in rc)) +
              _obs("B", (f"{m['mr_id']}={m['cycle_hours']:.2f}" for m in bc)),
              {"recent": sorted(({"mr": m, "value": round(m["cycle_hours"], 1)} for m in rc), key=lambda e: -e["value"]),
               "base": sorted(({"mr": m, "value": round(m["cycle_hours"], 1)} for m in bc), key=lambda e: e["value"])},
              _weekly([m for m in mrs if m["cycle_hours"] is not None], now,
                      lambda ms: round(statistics.median(m["cycle_hours"] for m in ms), 1) if ms else None),
              {"self_recent": sr, "self_base": sb})
    if row["state"] != "insufficient" and sr is not None and sb is not None and \
            (max(sr, sb) > SELF_MERGE_MAX or abs(sr - sb) >= 0.2):
        row.update(state="not_comparable", state_text=STATE_TEXT["not_comparable"], eligible=False,
                   blocked=f"self-merge 比例 {sb:.0%} → {sr:.0%}")

    pr = [m for m in R if not m["slice"]]
    pb = [m for m in B if not m["slice"]]
    act = add("activity", {"recent": round(len(pr) / (RECENT_DAYS / 7), 1), "base": round(len(pb) / (BASE_DAYS / 7), 1),
                           "diff": round(len(pr) / (RECENT_DAYS / 7) - len(pb) / (BASE_DAYS / 7), 1), "interval": None,
                           "p_better": 0, "p_worse": 0, "p_better_half": 0, "p_worse_half": 0},
              len(pr), len(pb), (0, 0), [], {}, _weekly([m for m in mrs if not m["slice"]], now, len))
    act.update(state="activity", state_text=STATE_TEXT["activity"])

    def share(ms, pred):
        known = [m for m in ms if pred(m) is not None]
        return round(sum(1 for m in known if pred(m)) / len(known), 2) if known else None

    def med_files(ms):
        f = [m["files"] for m in ms if m["files"]]
        return statistics.median(f) if f else None

    def projects(ms):
        counts = {}
        for m in ms:
            counts[m["project"].rsplit("/", 1)[-1]] = counts.get(m["project"].rsplit("/", 1)[-1], 0) + 1
        return sorted(counts.items(), key=lambda kv: -kv[1])
    context = {"frontend": (share(B, lambda m: m["track"] == "frontend"), share(R, lambda m: m["track"] == "frontend")),
               "fix": (share(pb, lambda m: m["is_fix"]), share(pr, lambda m: m["is_fix"])),
               "files": (med_files(pb), med_files(pr)),
               "self_merge": (self_share(pb), self_share(pr)),
               "slices": (sum(1 for m in B if m["slice"]), sum(1 for m in R if m["slice"])),
               "personal": (len(pb), len(pr)),
               "projects": (projects(B), projects(R))}
    shifted = []
    for key, label in (("frontend", "前端比例"), ("fix", "fix MR 比例"), ("self_merge", "self-merge 比例")):
        b_, a = context[key]
        if a is not None and b_ is not None and abs(a - b_) >= 0.2:
            shifted.append(f"{label} {b_:.0%} → {a:.0%}")
    b_, a = context["files"]
    if a and b_ and (a >= 1.5 * b_ or a <= b_ / 1.5):
        shifted.append(f"MR 大小(改動檔案中位數){b_:g} → {a:g}")
    by = {x["key"]: x for x in metrics}
    together = (by["cycle_time"]["state"] in ("strong_better", "observe_better", "better")
                and by["escape_rate"]["state"] in ("strong_worse", "observe_worse"))
    case_cut = now - timedelta(days=CASE_DAYS)
    my_cases = []
    for f in cases:
        mr = next((m for m in mrs if m["mr_id"] == f["mr_id"]), None)
        when = _ts(f.get("created_at"))
        if when and when > case_cut:
            my_cases.append({"finding": f, "mr": mr})
    return {"metrics": metrics, "context": context, "shifted": shifted, "together": together,
            "cases": my_cases, "n_recent": len(R), "n_base": len(B),
            "windows": {"base": (b0, r0), "recent": (r0, end), "bug": bug_bounds}}


def analyze(conn, cfg: dict, now: datetime | None = None) -> dict:
    """{person_id: analysis} for every member being evaluated."""
    now = now or datetime.now(timezone.utc)
    data = _samples(conn, cfg, now)
    roles = db.person_roles(conn)
    members = [pid for pid in data["by_person"] if roles.get(pid, "member") == "member"]
    out = {}
    for pid in members:
        team = [m for other in members if other != pid for m in data["by_person"][other]]
        out[pid] = analyze_person(data["by_person"][pid], team, now, pid, data["cases"].get(pid, []))
    return out


# ---------- judgments and alerts ----------

def _fingerprint(m: dict) -> str:
    return hashlib.sha1(json.dumps([MODEL_VERSION, m["state"], m["obs"]]).encode()).hexdigest()


def _new_obs(a: dict, b: dict) -> int:
    """Observations of the recent window in judgment `a` that judgment `b` had not seen."""
    seen = set(json.loads(b["obs"] or "[]"))
    return sum(1 for o in json.loads(a["obs"] or "[]") if o.startswith("R:") and o not in seen)


def _judge(conn, pid, m, as_of) -> bool:
    """Store a judgment when what it saw changed; True if a new one was written."""
    ev = _fingerprint(m)
    last = conn.execute("SELECT evidence FROM trajectory_judgments WHERE person_id = ? AND metric = ? "
                        "ORDER BY as_of DESC, id DESC LIMIT 1", (pid, m["key"])).fetchone()
    if last and last["evidence"] == ev:
        return False
    conn.execute("INSERT INTO trajectory_judgments(person_id, metric, as_of, evidence, n_recent, n_base, recent, "
                 "base, p_better, p_worse, p_better_half, p_worse_half, state, obs, eligible) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (pid, m["key"], as_of, ev, m["n_recent"], m["n_base"], m.get("recent"), m.get("base"),
                  m.get("p_better"), m.get("p_worse"), m.get("p_better_half"), m.get("p_worse_half"),
                  m["state"], json.dumps(m["obs"]), int(m["eligible"])))
    return True


def _lifecycle(conn, pid, m, now, as_of) -> tuple[int, int]:
    """Open / close / mark stale — runs on every update, new judgment or not."""
    opened = closed = 0
    key = m["key"]
    alert = conn.execute("SELECT * FROM trajectory_alerts WHERE person_id = ? AND metric = ? "
                         "AND closed_at IS NULL", (pid, key)).fetchone()
    if alert:
        since_open = conn.execute("SELECT * FROM trajectory_judgments WHERE person_id = ? AND metric = ? "
                                  "AND as_of > ? ORDER BY as_of DESC, id DESC LIMIT 2",
                                  (pid, key, alert["opened_at"])).fetchall()
        stale = int(m["state"] in ("insufficient", "not_comparable"))
        if stale != (alert["stale"] or 0):
            conn.execute("UPDATE trajectory_alerts SET stale = ? WHERE id = ?", (stale, alert["id"]))
        held = now - _ts(alert["opened_at"]) >= timedelta(days=HOLD_DAYS)
        side = "p_worse_half" if alert["kind"] == "watch" else "p_better_half"
        calm = [j for j in since_open if j["state"] not in ("insufficient", "not_comparable")
                and (j[side] or 0) < 0.70]
        flipped = since_open and since_open[0]["state"] == ("strong_better" if alert["kind"] == "watch"
                                                             else "strong_worse")
        if held and (len(calm) == 2 and len(since_open) == 2 or flipped):
            conn.execute("UPDATE trajectory_alerts SET closed_at = ?, closed_by = 'data', close_reason = ? "
                         "WHERE id = ?", (as_of, "方向反轉" if flipped else "連續兩次未再超過一半門檻",
                                          alert["id"]))
            closed += 1
        return opened, closed
    if not m["alert"]:
        return opened, closed
    last_closed = conn.execute("SELECT closed_at FROM trajectory_alerts WHERE person_id = ? AND metric = ? "
                               "AND closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT 1", (pid, key)).fetchone()
    after = last_closed[0] if last_closed else ""
    if last_closed and now - _ts(last_closed[0]) < timedelta(days=COOLDOWN_DAYS):
        return opened, closed
    two = conn.execute("SELECT * FROM trajectory_judgments WHERE person_id = ? AND metric = ? AND as_of > ? "
                       "ORDER BY as_of DESC, id DESC LIMIT 2", (pid, key, after)).fetchall()
    if len(two) < 2 or two[0]["state"] != two[1]["state"] or \
            two[0]["state"] not in ("strong_worse", "strong_better") or not (two[0]["eligible"] and two[1]["eligible"]):
        return opened, closed
    fresh = _new_obs(two[0], two[1])
    apart = _ts(two[0]["as_of"]) - _ts(two[1]["as_of"])
    if fresh < 2 and not (fresh >= 1 and apart >= timedelta(days=14)):
        return opened, closed                         # not enough *new* evidence yet
    kind = "watch" if two[0]["state"] == "strong_worse" else "improve"
    snap = {k: m.get(k) for k in ("base", "recent", "diff", "interval", "p_better", "p_worse",
                                  "n_base", "n_recent", "threshold")}
    snap.update(model=MODEL_VERSION, judgments=[two[1]["id"], two[0]["id"]], new_obs=fresh)
    conn.execute("INSERT INTO trajectory_alerts(person_id, metric, kind, opened_at, summary, snapshot) "
                 "VALUES (?, ?, ?, ?, ?, ?)",
                 (pid, key, kind, as_of, f"{m['label']} {m.get('base')} → {m.get('recent')}",
                  json.dumps(snap, default=str)))
    return opened + 1, closed


def update(conn, cfg: dict, now: datetime | None = None, analyses: dict | None = None) -> dict:
    """Judge every metric whose observations changed, then run every alert's lifecycle."""
    now = now or datetime.now(timezone.utc)
    as_of = db.utc(now)
    analyses = analyses if analyses is not None else analyze(conn, cfg, now)
    judged = opened = closed = 0
    with conn:
        for pid, a in analyses.items():
            for m in a["metrics"]:
                if m["state"] == "activity":
                    continue
                judged += _judge(conn, pid, m, as_of)      # insufficient too: it breaks a streak
                o, c = _lifecycle(conn, pid, m, now, as_of)
                opened, closed = opened + o, closed + c
        # people no longer evaluated (left, other team): their alerts end, the record stays
        for row in conn.execute("SELECT id, person_id FROM trajectory_alerts WHERE closed_at IS NULL").fetchall():
            if row["person_id"] not in analyses:
                conn.execute("UPDATE trajectory_alerts SET closed_at = ?, closed_by = 'data', "
                             "close_reason = '不再評估此人' WHERE id = ?", (as_of, row["id"]))
                closed += 1
    return {"judged": judged, "opened": opened, "closed": closed}


def open_alerts(conn) -> dict[int, list[dict]]:
    out: dict[int, list] = {}
    for r in conn.execute("SELECT * FROM trajectory_alerts WHERE closed_at IS NULL ORDER BY opened_at"):
        out.setdefault(r["person_id"], []).append(dict(r))
    return out


def acknowledge(conn, alert_id: int, actor: str, note: str = "", now: datetime | None = None) -> None:
    """已檢視: a human looked. The alert keeps being tracked; nothing is closed."""
    with conn:
        conn.execute("UPDATE trajectory_alerts SET acknowledged_at = ?, acknowledged_by = ?, note = ? "
                     "WHERE id = ? AND closed_at IS NULL",
                     (db.utc(now) if now else db.now_iso(), actor, note.strip() or None, alert_id))


def end_tracking(conn, alert_id: int, actor: str, reason: str, now: datetime | None = None) -> None:
    """結束追蹤: a human closes it, with a reason; the 28-day cool-down starts."""
    if not reason.strip():
        raise ValueError("結束追蹤要寫原因")
    with conn:
        conn.execute("UPDATE trajectory_alerts SET closed_at = ?, closed_by = ?, close_reason = ? "
                     "WHERE id = ? AND closed_at IS NULL",
                     (db.utc(now) if now else db.now_iso(), actor, reason.strip(), alert_id))
