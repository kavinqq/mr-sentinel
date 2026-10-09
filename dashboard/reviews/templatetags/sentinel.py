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
    "role:lead": "c-indigo", "role:member": "c-blue", "role:departed": "c-gray",
    # MR records
    "mr:個人": "c-blue", "mr:release": "c-violet", "mr:他人的 MR": "c-teal",
    # states
    "state:escaped": "c-red", "state:excluded": "c-gray", "state:accepted": "c-green",
    "state:removed": "c-gray", "state:current": "c-green", "state:version": "c-indigo",
    "state:manual": "c-indigo", "state:auto": "c-teal", "state:pending": "c-amber",
    "state:level": "c-indigo",
    # tracks
    "track:frontend": "c-cyan", "track:backend": "c-violet",
    # levels
    "level:senior": "c-green", "level:mid+": "c-cyan", "level:mid": "c-indigo",
    "level:junior": "c-orange", "level:": "c-gray",
}


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
