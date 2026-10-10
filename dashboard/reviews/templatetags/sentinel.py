from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from django import template

register = template.Library()
TAIPEI = ZoneInfo("Asia/Taipei")


@register.filter
def tw(value, fmt="%Y-%m-%d %H:%M"):
    """Stored UTC text ('…Z') -> Taipei time for display."""
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TAIPEI).strftime(fmt)


@register.filter
def get(mapping, key):
    return (mapping or {}).get(key)


THREAD_LABEL = {"unanswered": "未回應", "appeal": "已回覆 · 待確認", "rejected": "已回覆 · 仍需處理",
                "accepted": "回覆已採納 · 討論串未結束", "closed": "已解決"}


# one hue per kind of label, the same everywhere (dashboard.css .c-*)
TAG_COLORS = {
    # severity
    "sev:high": "c-solid-red", "sev:medium": "c-amber", "sev:low": "c-blue",
    # the 8 categories
    "cat:security": "c-crimson", "cat:requirements": "c-violet", "cat:correctness": "c-blue",
    "cat:compatibility": "c-cyan", "cat:operability": "c-orange", "cat:performance": "c-teal",
    "cat:verification": "c-plum", "cat:maintainability": "c-brown",
    "cat:uncategorized": "c-gray", "cat:needs_review": "c-amber",
    # GitLab thread state of a finding
    "thread:unanswered": "c-amber", "thread:appeal": "c-blue", "thread:rejected": "c-orange",
    "thread:accepted": "c-teal", "thread:closed": "c-green",
    # follow-ups
    "kind:fix_mr": "c-orange", "kind:ai_refind": "c-violet",
    "verdict:confirmed": "c-red", "verdict:unrelated": "c-gray", "verdict:": "c-amber",
    # people
    "role:lead": "c-indigo", "role:member": "c-blue", "role:departed": "c-gray", "role:external": "c-brown",
    # MR records
    "mr:個人": "c-blue", "mr:release": "c-violet", "mr:他人的 MR": "c-teal", "mr:直接 commit": "c-orange",
    # states
    "state:escaped": "c-red", "state:excluded": "c-gray", "state:accepted": "c-green",
    "state:removed": "c-gray", "state:current": "c-green", "state:version": "c-indigo",
    "state:manual": "c-indigo", "state:auto": "c-teal", "state:pending": "c-amber",
    "state:level": "c-indigo",
    # how far a score can be trusted
    "sample:樣本少": "c-amber", "sample:暫定": "c-gray",
    # tracks
    "track:frontend": "c-cyan", "track:backend": "c-violet", "track:contribution": "c-teal",
    # levels
    "level:senior": "c-green", "level:mid+": "c-cyan", "level:mid": "c-indigo",
    "level:junior": "c-orange", "level:": "c-gray",
}


@register.filter
def role_label(role):
    from history.db import ROLES
    return ROLES.get(role, role)


@register.filter
def tag(value, kind):
    """CSS colour class for a tag: {{ f.category|tag:"cat" }}."""
    return TAG_COLORS.get(f"{kind}:{value or ''}", "c-gray")


@register.filter
def thread_label(status):
    """GitLab thread state of a finding, in words a reviewer reads."""
    return THREAD_LABEL.get(status, status or "—")


@register.filter
def split(value):
    return str(value).split()


SEVERITY_LABEL = {"high": "High", "medium": "Medium", "low": "Low"}


@register.filter
def level_dot(level):
    """CSS dot class for a level; anything unrated is neutral."""
    return {"senior": "senior", "mid+": "midplus", "mid": "mid", "junior": "junior"}.get(level, "none")


@register.filter
def item_cls(value, top=5):
    """Tone for an item score (out of `top`): full marks fade, weak ones stand out."""
    if value is None:
        return "ms-dim"
    # 3 of 5 is "acceptable" and reads as plain text; only real strength or
    # weakness gets a colour (and a weight, so it is not colour alone)
    ratio = value / float(top or 5)
    return "ms-item good" if ratio >= 0.8 else "ms-item" if ratio >= 0.56 else \
        "ms-item soft" if ratio >= 0.46 else "ms-item weak"


@register.filter
def sev_dot(severity):
    return severity if severity in SEVERITY_LABEL else "low"


@register.filter
def sev_label(severity):
    return SEVERITY_LABEL.get(severity, "Low")


@register.filter
def initial(name):
    """Avatar letter: the given name's first character (CJK: the last one)."""
    name = (name or "?").strip()
    return name[-1] if name and ord(name[-1]) > 0x2E80 else name[:1].upper()


@register.filter
def short_mr(mr):
    """'developer/py_backend/pocketms-backend' !289 -> 'pocketms-backend!289'."""
    if not mr:
        return "—"
    return f"{str(mr.project).rsplit('/', 1)[-1]}!{mr.iid}"


@register.filter
def spark(metric):
    """12 weekly raw values as an inline SVG: gaps stay gaps (no line across a
    week without samples, never a zero), the last 4 weeks (the recent window)
    on a tinted band; each point carries a <title> for hover / screen readers."""
    from django.utils.html import escape
    from django.utils.safestring import mark_safe
    values = (metric or {}).get("weekly") or []
    pts = [(i, v) for i, v in enumerate(values) if v is not None]
    w, h, pad = 132, 32, 3
    if not values:
        return mark_safe('<span class="ms-spark-empty">不提供週走勢</span>')
    if len(pts) < 2:
        return mark_safe('<span class="ms-spark-empty">資料不足</span>')
    unit = metric.get("unit")
    lo, hi = (1, 5) if unit == "分" else (0, 1) if unit == "%" else (min(v for _, v in pts), max(v for _, v in pts))
    if hi == lo:
        hi = lo + 1
    x = lambda i: pad + i * (w - 2 * pad) / max(1, len(values) - 1)
    y = lambda v: h - pad - (v - lo) * (h - 2 * pad) / (hi - lo)
    segs, cur = [], []
    for i, v in enumerate(values):
        if v is None:
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append((x(i), y(v)))
    if cur:
        segs.append(cur)
    band = x(len(values) - 4) - 2
    summary = "、".join("無" if v is None else (f"{v * 100:.0f}%" if unit == "%" else f"{v:g}") for v in values)
    out = [f'<svg class="ms-spark" viewBox="0 0 {w} {h}" preserveAspectRatio="none" role="img" '
           f'aria-label="{escape(metric.get("label", ""))} 近 12 週每週原始值(舊到新,底色為最近 28 天):{escape(summary)}">',
           f'<rect x="{band:.1f}" y="0" width="{w - band:.1f}" height="{h}" class="band"/>']
    for seg in segs:
        if len(seg) > 1:
            out.append('<polyline points="' + " ".join(f"{a:.1f},{b:.1f}" for a, b in seg) + '"/>')
    from datetime import datetime, timedelta, timezone
    fmt = (lambda v: f"{v * 100:.0f}%") if unit == "%" else (lambda v: f"{v:g}")
    today = (metric.get("as_of") or datetime.now(timezone.utc)).date()   # the bins end at the analysis time
    def span(i):
        end = today - timedelta(days=7 * (len(values) - 1 - i))
        return f"{(end - timedelta(days=6)):%m/%d}–{end:%m/%d}"
    for i, v in pts:
        out.append(f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="2.2"><title>{span(i)}:{fmt(v)}</title></circle>')
    out.append("</svg>")
    return mark_safe("".join(out))


@register.filter
def pct(value):
    """A probability / share as a percentage; anything missing or not a number is —."""
    from numbers import Real
    return f"{float(value) * 100:.0f}%" if isinstance(value, Real) and not isinstance(value, bool) else "—"


@register.filter
def grade_tag(value, unit):
    """Colour of one evidence value: a grade (≤2 red, 3 gray, ≥4 green), an event (red / green)."""
    if unit == "分":
        return "c-red" if value is not None and value <= 2 else "c-green" if value and value >= 4 else "c-gray"
    if unit == "%":
        return "c-red" if value else "c-green"
    return "c-gray"


GAUGE_SPAN = 3          # the axis runs to ±3 thresholds; beyond that the bar is clipped with an arrow
THRESHOLD_TEXT = {"分": "±0.5 分", "%": "±15 個百分點", "小時": "±25% 且至少 1 小時"}


@register.filter
def gauge(metric, size="card"):
    """The estimated change as a picture: one axis for every metric, in units of its
    threshold and turned so right is always better. A dashed line marks the threshold,
    the bar the 90% interval, the dot the estimate; numbers go in the accessible name."""
    from django.utils.html import escape
    from django.utils.safestring import mark_safe
    m = metric or {}
    w, h, pad = (260, 30, 8) if size == "wide" else (200, 26, 7)
    x = lambda v: pad + (max(-GAUGE_SPAN, min(GAUGE_SPAN, v)) + GAUGE_SPAN) / (2 * GAUGE_SPAN) * (w - 2 * pad)
    mid, top, bh = h / 2, h / 2 - 5, 10
    track = (f'<rect class="zw" x="{x(-GAUGE_SPAN):.1f}" y="{top}" width="{x(-1) - x(-GAUGE_SPAN):.1f}" height="{bh}" rx="3"/>'
             f'<rect class="zn" x="{x(-1):.1f}" y="{top}" width="{x(1) - x(-1):.1f}" height="{bh}"/>'
             f'<rect class="zb" x="{x(1):.1f}" y="{top}" width="{x(GAUGE_SPAN) - x(1):.1f}" height="{bh}" rx="3"/>'
             f'<line class="t" x1="{x(-1):.1f}" x2="{x(-1):.1f}" y1="2" y2="{h - 2}"/>'
             f'<line class="t" x1="{x(1):.1f}" x2="{x(1):.1f}" y1="2" y2="{h - 2}"/>'
             f'<line class="z" x1="{x(0):.1f}" x2="{x(0):.1f}" y1="{top - 1}" y2="{top + bh + 1}"/>')
    label = escape(m.get("label", ""))
    t, diff, iv = m.get("threshold"), m.get("diff"), m.get("interval")
    if m.get("kind") == "activity" or not t:
        return mark_safe('<span class="ms-gauge-na">活動量,不評估變化</span>')
    if m.get("state") in ("insufficient", "not_comparable") or diff is None or not iv:
        why = "比較條件不同" if m.get("state") == "not_comparable" else "資料不足"
        return mark_safe(f'<svg class="ms-gauge is-empty" viewBox="0 0 {w} {h}" role="img" aria-label="{label}:{why},暫不評估">'
                         f'{track}<text x="{w / 2:.0f}" y="{mid + 4:.0f}" text-anchor="middle">{why}</text></svg>')
    sign = 1 if m.get("better") == "higher" else -1
    v = sign * diff / t
    lo, hi = sorted((sign * iv[0] / t, sign * iv[1] / t))
    tone = m.get("tone", "flat")
    name = (f'{label}:估計變化 {escape(m.get("diff_text", ""))},估計範圍(90%){escape(m.get("interval_text") or "—")},'
            f'門檻 {THRESHOLD_TEXT.get(m.get("unit"), "")};右側為變好')
    out = [f'<svg class="ms-gauge tone-{tone}" viewBox="0 0 {w} {h}" role="img" aria-label="{name}"><title>{name}</title>', track,
           f'<line class="iv" x1="{x(lo):.1f}" x2="{x(hi):.1f}" y1="{mid}" y2="{mid}"/>']
    if lo < -GAUGE_SPAN:
        out.append(f'<path class="clip" d="M{pad - 5} {mid} l5 -4 v8 z"/>')
    if hi > GAUGE_SPAN:
        out.append(f'<path class="clip" d="M{w - pad + 5} {mid} l-5 -4 v8 z"/>')
    out.append(f'<circle class="pt" cx="{x(v):.1f}" cy="{mid}" r="{5 if size == "wide" else 4.5}"/></svg>')
    return mark_safe("".join(out))


@register.simple_tag
def gauge_key():
    """The legend every gauge shares."""
    from django.utils.safestring import mark_safe
    return mark_safe(
        '<span class="ms-gauge-key" aria-hidden="true">'
        '<span class="k-w">← 變差</span>'
        '<svg viewBox="0 0 120 16"><rect class="zw" x="2" y="3" width="38" height="10" rx="3"/><rect class="zn" x="40" y="3" width="40" height="10"/>'
        '<rect class="zb" x="80" y="3" width="38" height="10" rx="3"/><line class="t" x1="40" x2="40" y1="0" y2="16"/>'
        '<line class="t" x1="80" x2="80" y1="0" y2="16"/><line class="iv" x1="52" x2="96" y1="8" y2="8"/><circle class="pt" cx="74" cy="8" r="4"/></svg>'
        '<span class="k-b">變好 →</span>'
        '<span class="k-t">點 = 估計變化 · 線 = 估計範圍(90%) · 虛線 = 門檻</span></span>')


@register.filter
def profile(metrics):
    """The 8 graded categories as a dumbbell chart on the 1–5 scale: hollow = base,
    filled = recent (raw means), the connector coloured by the metric's state."""
    from django.utils.html import escape
    from django.utils.safestring import mark_safe
    rows = [m for m in metrics or [] if m.get("kind") == "rating"]
    if not rows:
        return ""
    lw, w, rh, top = 96, 380, 26, 18
    x = lambda v: lw + (v - 1) / 4 * (w - lw - 12)
    h = top + rh * len(rows) + 4
    out = [f'<svg class="ms-profile" viewBox="0 0 {w} {h}" role="img" aria-label="八個面向的評分,前 56 天與最近 28 天的實際平均">']
    for g in range(1, 6):
        out.append(f'<line class="grid" x1="{x(g):.1f}" x2="{x(g):.1f}" y1="{top - 4}" y2="{h - 4}"/>'
                   f'<text class="tick" x="{x(g):.1f}" y="11" text-anchor="middle">{g}</text>')
    for i, m in enumerate(rows):
        y = top + rh * i + rh / 2
        b, r = m.get("raw_base"), m.get("raw_recent")
        tone = m.get("tone", "flat")
        desc = f'{escape(m["label"])}:前 56 天 {"—" if b is None else f"{b:.2f}"},最近 {"—" if r is None else f"{r:.2f}"},{escape(m.get("state_text", ""))}'
        out.append(f'<g class="row tone-{tone}"><title>{desc}</title>'
                   f'<text class="lab" x="{lw - 10}" y="{y + 4:.1f}" text-anchor="end">{escape(m["label"])}</text>')
        if b is not None and r is not None:
            out.append(f'<line class="conn" x1="{x(b):.1f}" x2="{x(r):.1f}" y1="{y}" y2="{y}"/>')
        if b is not None:
            out.append(f'<circle class="b" cx="{x(b):.1f}" cy="{y}" r="4.5"/>')
        if r is not None:
            out.append(f'<circle class="r" cx="{x(r):.1f}" cy="{y}" r="5"/>')
        if b is None and r is None:
            out.append(f'<text class="none" x="{x(3):.1f}" y="{y + 4:.1f}" text-anchor="middle">沒有評分</text>')
        out.append("</g>")
    out.append("</svg>")
    return mark_safe("".join(out))


@register.simple_tag
def shift_bar(pair, kind="share", shifted=False):
    """One work-mix measure, base → recent, as a dumbbell on its track: a share on
    0–100%, a count on 0 to 1.25 × the larger side."""
    from django.utils.safestring import mark_safe
    b, r = (pair or (None, None))[:2]
    nums = [float(v) for v in (b, r) if v is not None]
    if not nums:
        return mark_safe('<span class="ms-dim">—</span>')
    top = 1 if kind == "share" else max(max(nums) * 1.25, 1)
    w, h, pad = 140, 18, 6
    x = lambda v: pad + float(v) / top * (w - 2 * pad)
    text = (lambda v: f"{float(v) * 100:.0f}%") if kind == "share" else (lambda v: f"{float(v):g}")
    show = lambda v: "—" if v is None else text(v)
    out = [f'<span class="ms-shift{" is-shifted" if shifted else ""}"><svg viewBox="0 0 {w} {h}" aria-hidden="true">'
           f'<line class="track" x1="{pad}" x2="{w - pad}" y1="{h / 2}" y2="{h / 2}"/>']
    if b is not None and r is not None:
        out.append(f'<line class="conn" x1="{x(b):.1f}" x2="{x(r):.1f}" y1="{h / 2}" y2="{h / 2}"/>')
    if b is not None:
        out.append(f'<circle class="b" cx="{x(b):.1f}" cy="{h / 2}" r="4"/>')
    if r is not None:
        out.append(f'<circle class="r" cx="{x(r):.1f}" cy="{h / 2}" r="4.5"/>')
    out.append(f'</svg><span class="v">{show(b)} → <strong>{show(r)}</strong></span></span>')
    return mark_safe("".join(out))



@register.simple_tag
def person_tag(pid, name, size=""):
    """A person's name as a tag in their own colour (the same colour on every page)."""
    from django.utils.html import escape
    from django.utils.safestring import mark_safe
    from ..services import person_color
    return mark_safe(f'<span class="ms-ptag pc{person_color(pid)}{" is-" + size if size else ""}">'
                     f'<i aria-hidden="true"></i>{escape(name)}</span>')


def _week_span(metric, i, total):
    from datetime import timedelta
    today = (metric.get("as_of") or datetime.now(timezone.utc)).date()   # the bins end at the analysis time
    end = today - timedelta(days=7 * (total - 1 - i))
    return f"{(end - timedelta(days=6)):%m/%d}–{end:%m/%d}"


def _fmt_value(v, unit):
    return (f"{v * 100:.0f}%" if unit == "%" else f"{v:.2f} 分" if unit == "分"
            else f"{v:g} 小時" if unit == "小時" else f"{v:g} 個")


def week_deltas(metric) -> list[dict]:
    """Each week against the last earlier week that had data: the raw change, and the
    change in thresholds turned so + is better (time: the ratio; activity: no direction)."""
    m = metric or {}
    values, counts = m.get("weekly") or [], m.get("weekly_n") or []
    unit, t = m.get("unit"), m.get("threshold")
    sign = 1 if m.get("better") == "higher" else -1 if m.get("better") == "lower" else 0
    out, prev = [], None
    for i, v in enumerate(values):
        n = counts[i] if i < len(counts) else None
        d = {"i": i, "value": v, "n": n, "prev": prev, "delta": None, "norm": None, "tone": "none"}
        if v is not None and prev is not None:
            pv = prev["value"]
            if unit == "小時":
                raw = (max(v, .01) / max(pv, .01)) - 1
                strong = abs(raw) >= t and abs(v - pv) >= 1
            else:
                raw = v - pv
                strong = t is not None and abs(raw) >= t - 1e-9
            d["delta"] = raw
            if sign and t:
                d["norm"] = sign * raw / t
                d["tone"] = ("up" if d["norm"] > 0 else "down" if d["norm"] < 0 else "flat") + ("-strong" if strong else "")
            else:
                d["tone"] = "act"
        out.append(d)
        if v is not None:
            prev = d
    return out


def _fmt_delta(d, unit):
    raw = d["delta"]
    return (f"{raw * 100:+.0f} 個百分點" if unit == "%" else f"{raw:+.2f} 分" if unit == "分"
            else f"{raw * 100:+.0f}%" if unit == "小時" else f"{raw:+g} 個")


@register.filter
def week_summary(metric):
    """This week in words: its value, how many samples, and the change from the last week with data."""
    m = metric or {}
    weeks = week_deltas(m)
    if not weeks:
        return {}
    last = weeks[-1]
    return {"value": None if last["value"] is None else _fmt_value(last["value"], m.get("unit")),
            "n": last["n"], "tone": last["tone"],
            "delta": None if last["delta"] is None else _fmt_delta(last, m.get("unit")),
            "prev_span": _week_span(m, last["prev"]["i"], len(weeks)) if last["prev"] else None}


def _tone_class(tone):
    return {"up-strong": "up s", "up": "up", "down-strong": "down s", "down": "down", "flat": "flat", "act": "act"}.get(tone, "none")


@register.filter
def weekchart(metric):
    """12 weeks, two panels: each week's raw value (dots), and under it the change from
    the previous week with data — bars in thresholds, up = better, dashed = threshold;
    a faded bar is a week with one sample. The figure under each bar is its sample count."""
    from django.utils.html import escape
    from django.utils.safestring import mark_safe
    m = metric or {}
    weeks = week_deltas(m)
    if not weeks:
        return mark_safe('<p class="ms-week-empty">不提供每週數字(要等 merge 滿 30 天才能確認後續 bug)</p>')
    if sum(1 for w in weeks if w["value"] is not None) < 2:
        return mark_safe('<p class="ms-week-empty">這 12 週有資料的週數太少,還畫不出每週變化</p>')
    unit, total = m.get("unit"), len(weeks)
    W, padl, padr = 320, 8, 8
    step = (W - padl - padr) / total
    cx = lambda i: padl + step * (i + .5)
    vals = [w["value"] for w in weeks if w["value"] is not None]
    lo, hi = (1, 5) if unit == "分" else (0, 1) if unit == "%" else (0, max(vals) * 1.15 or 1)
    vy = lambda v: 60 - (v - lo) / (hi - lo) * 50          # value panel: y 10..60
    base, unit_h = 104, 11                                   # change panel: 0 at y=104, threshold = 11 px
    norms = [abs(w["norm"]) for w in weeks if w["norm"] is not None]
    act_top = max([abs(w["delta"]) for w in weeks if w["delta"] is not None] or [1]) or 1
    label = escape(m.get("label", ""))
    out = [f'<svg class="ms-week" viewBox="0 0 {W} 150" role="img" aria-label="{label}:最近 12 週每週的數字,以及和前一個有資料的週相比的變化">',
           f'<rect class="this" x="{cx(total - 1) - step / 2:.1f}" y="2" width="{step:.1f}" height="134" rx="4"/>',
           f'<line class="grid" x1="{padl}" x2="{W - padr}" y1="{vy(lo):.1f}" y2="{vy(lo):.1f}"/>',
           f'<line class="zero" x1="{padl}" x2="{W - padr}" y1="{base}" y2="{base}"/>']
    if m.get("threshold"):
        for s in (-1, 1):
            out.append(f'<line class="thr" x1="{padl}" x2="{W - padr}" y1="{base - s * unit_h}" y2="{base - s * unit_h}"/>')
    out.append(f'<text class="axis" x="{padl}" y="8">每週數字</text>'
               f'<text class="axis" x="{padl}" y="78">比前一週{"(上 = 變好)" if m.get("threshold") else ""}</text>')
    pts = [(cx(w["i"]), vy(w["value"])) for w in weeks if w["value"] is not None]
    out.append('<polyline class="vline" points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y in pts) + '"/>')
    for w in weeks:
        x, span = cx(w["i"]), _week_span(m, w["i"], total)
        thin = " thin" if (w["n"] or 0) <= 1 else ""
        if w["value"] is not None:
            tip = f'{span}:{_fmt_value(w["value"], unit)}({w["n"]} 筆)'
            if w["delta"] is not None:
                tip += f',比 {_week_span(m, w["prev"]["i"], total)} {_fmt_delta(w, unit)}'
            out.append(f'<g class="wk"><title>{escape(tip)}</title>'
                       f'<circle class="v{thin}" cx="{x:.1f}" cy="{vy(w["value"]):.1f}" r="3.2"/>')
            if w["delta"] is not None:
                if w["norm"] is not None:
                    h = max(-2.2, min(2.2, w["norm"])) * unit_h
                else:
                    h = w["delta"] / act_top * 2 * unit_h
                top, hh = (base - h, h) if h >= 0 else (base, -h)
                out.append(f'<rect class="bar {_tone_class(w["tone"])}{thin}" x="{x - step * .3:.1f}" y="{top:.1f}" '
                           f'width="{step * .6:.1f}" height="{max(hh, 1.2):.1f}" rx="1.5"/>')
            out.append("</g>")
        out.append(f'<text class="n" x="{x:.1f}" y="{base + 32}" text-anchor="middle">{"" if not w["n"] else w["n"]}</text>')
    for i in (0, total // 2, total - 1):
        txt = "本週" if i == total - 1 else _week_span(m, i, total).split("–")[0]
        out.append(f'<text class="date" x="{cx(i):.1f}" y="148" text-anchor="middle">{txt}</text>')
    out.append("</svg>")
    return mark_safe("".join(out))


@register.filter
def weekstrip(metric):
    """The weekly change, small, for a card: 12 bars around a zero line, up = better."""
    from django.utils.html import escape
    from django.utils.safestring import mark_safe
    m = metric or {}
    weeks = week_deltas(m)
    W, H, mid, u = 200, 26, 13, 5
    if not weeks or sum(1 for w in weeks if w["delta"] is not None) == 0:
        why = "不提供每週數字" if not weeks else "每週資料不足"
        return mark_safe(f'<svg class="ms-wstrip is-empty" viewBox="0 0 {W} {H}" role="img" aria-label="{escape(m.get("label", ""))}:{why}">'
                         f'<line class="zero" x1="2" x2="{W - 2}" y1="{mid}" y2="{mid}"/>'
                         f'<text x="{W / 2:.0f}" y="{mid + 4}" text-anchor="middle">{why}</text></svg>')
    step = (W - 4) / len(weeks)
    act_top = max([abs(w["delta"]) for w in weeks if w["delta"] is not None] or [1]) or 1
    s = week_summary(m)
    name = f'{escape(m.get("label", ""))}:每週變化(上 = 變好);本週 {s.get("value") or "沒有資料"}' + (f',比前一週 {s["delta"]}' if s.get("delta") else "")
    out = [f'<svg class="ms-wstrip" viewBox="0 0 {W} {H}" role="img" aria-label="{name}"><title>{name}</title>',
           f'<rect class="this" x="{2 + step * (len(weeks) - 1):.1f}" y="0" width="{step:.1f}" height="{H}" rx="2"/>',
           f'<line class="zero" x1="2" x2="{W - 2}" y1="{mid}" y2="{mid}"/>']
    for w in weeks:
        x = 2 + step * w["i"] + step * .2
        if w["delta"] is None:
            if w["value"] is not None:
                out.append(f'<circle class="dot" cx="{x + step * .3:.1f}" cy="{mid}" r="1.6"/>')
            continue
        h = (max(-2.2, min(2.2, w["norm"])) if w["norm"] is not None else w["delta"] / act_top * 2) * u
        top, hh = (mid - h, h) if h >= 0 else (mid, -h)
        thin = " thin" if (w["n"] or 0) <= 1 else ""
        out.append(f'<rect class="bar {_tone_class(w["tone"])}{thin}" x="{x:.1f}" y="{top:.1f}" width="{step * .6:.1f}" height="{max(hh, 1):.1f}" rx="1"/>')
    out.append("</svg>")
    return mark_safe("".join(out))
