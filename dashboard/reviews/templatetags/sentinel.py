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


SEVERITY_LABEL = {"high": "High", "medium": "Medium", "low": "Low"}


@register.filter
def level_dot(level):
    """CSS dot class for a level; anything unrated is neutral."""
    return {"senior": "senior", "mid+": "midplus", "mid": "mid", "junior": "junior"}.get(level, "none")


@register.filter
def item_cls(value):
    """Tone for an item score out of 10: full marks fade, weak ones stand out."""
    if value is None:
        return "ms-dim"
    return "ms-item full" if value >= 10 else "ms-item weak" if value < 6 else \
        "ms-item soft" if value < 8.5 else "ms-item"


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
