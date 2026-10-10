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


THREAD_LABEL = {"unanswered": "未回應", "appeal": "已回覆 · 待判斷", "rejected": "已回覆 · 仍要修",
                "accepted": "申訴成立 · 未關閉", "closed": "已解決"}


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
    if len(pts) < 2:
        return mark_safe('<span class="ms-spark-empty">資料太少</span>')
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
    out = [f'<svg class="ms-spark" viewBox="0 0 {w} {h}" preserveAspectRatio="none" role="img" '
           f'aria-label="{escape(metric.get("label", ""))} 近 12 週走勢">',
           f'<rect x="{band:.1f}" y="0" width="{w - band:.1f}" height="{h}" class="band"/>']
    for seg in segs:
        if len(seg) > 1:
            out.append('<polyline points="' + " ".join(f"{a:.1f},{b:.1f}" for a, b in seg) + '"/>')
    fmt = (lambda v: f"{v * 100:.0f}%") if unit == "%" else (lambda v: f"{v:g}")
    for i, v in pts:
        out.append(f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="2.2"><title>{12 - i} 週前:{fmt(v)}</title></circle>')
    out.append("</svg>")
    return mark_safe("".join(out))


@register.filter
def pct(value):
    return "—" if value is None else f"{value * 100:.0f}%"


@register.filter
def grade_tag(value, unit):
    """Colour of one evidence value: a grade (≤2 red, 3 gray, ≥4 green), an event (red / green)."""
    if unit == "分":
        return "c-red" if value is not None and value <= 2 else "c-green" if value and value >= 4 else "c-gray"
    if unit == "%":
        return "c-red" if value else "c-green"
    return "c-gray"
