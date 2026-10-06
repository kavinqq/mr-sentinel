"""Slack-settable config overrides, layered on top of config.json.

`config.json` stays the hand-edited source of truth: it holds the tokens and is
chmod 600. Everything changed from Slack lands in `overrides.json` and is
merged on top at load time, so `reset` restores the config.json value simply by
forgetting the override.

Two rules make this safe to expose to a chat channel:

- **SETTABLE is a security boundary.** Only these keys can ever be written, and
  `apply()` copies only these paths — a Slack user (or a hand-mangled
  overrides.json) can never repoint `gitlab_url` or overwrite a token.
- **Values are re-validated on read**, not just on write, so a corrupted or
  hand-edited file degrades to the config.json default instead of poisoning a
  review run.

`project_map` is stored as an add/remove *patch* rather than a snapshot, so a
project you later add to config.json by hand is not shadowed by an old
Slack-side override.
"""
import fcntl
import json
import logging
import re
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("mr_sentinel.overrides")

AUDIT_LIMIT = 50

LANGUAGES = ("en", "zh-TW", "zh-CN", "ja", "ko")
ENGINES = ("claude", "codex")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Model names churn faster than this repo does, so there is no model whitelist:
# just enough of a shape check to keep shell-looking junk out of an argv slot.
MODEL_RE = re.compile(r"^[A-Za-z0-9._:-]{2,64}$")

TRUTHY = {"on", "true", "yes", "1", "開", "是"}
FALSEY = {"off", "false", "no", "0", "關", "否"}

# path: where the value lands in the config tree
# kind: how to validate it, and how the Slack menu offers it
Setting = namedtuple("Setting", "path kind spec desc")

SETTABLE = {
    "engine":       Setting(("review", "engine"), "choice", ENGINES, "AI engine"),
    "effort":       Setting(("review", "claude", "effort"), "choice", EFFORTS, "推理強度"),
    "language":     Setting(("review", "language"), "choice", LANGUAGES, "留言語言"),
    "model":        Setting(("review", "claude", "model"), "text", MODEL_RE, "主模型"),
    "skeptic":      Setting(("review", "claude", "skeptic_model"), "text", MODEL_RE, "懷疑者模型"),
    "automerge":    Setting(("review", "auto_merge_on_clean"), "bool", None, "乾淨時自動合併"),
    "maxfiles":     Setting(("review", "max_changed_files"), "int", (1, 500), "deep 門檻·檔案數"),
    "maxlines":     Setting(("review", "max_diff_lines"), "int", (50, 50000), "deep 門檻·行數"),
    "timeout":      Setting(("review", "review_timeout_seconds"), "int", (60, 7200), "review 逾時秒數"),
    "codexmodel":   Setting(("review", "codex", "model"), "text", MODEL_RE, "codex 主模型"),
    "codexskeptic": Setting(("review", "codex", "skeptic_model"), "text", MODEL_RE, "codex 懷疑者模型"),
}


# ---------- validation ----------


def validate(key: str, raw) -> tuple[object, str | None]:
    """Coerce a raw string to the setting's type. Returns (value, error)."""
    setting = SETTABLE.get(key)
    if setting is None:
        return None, f"不認得的設定 `{key}`,可設的有: {', '.join(sorted(SETTABLE))}"
    text = str(raw).strip()
    if setting.kind == "choice":
        if text in setting.spec:
            return text, None
        return None, f"`{key}` 只能是 {' / '.join(setting.spec)}"
    if setting.kind == "bool":
        low = text.lower()
        if low in TRUTHY:
            return True, None
        if low in FALSEY:
            return False, None
        return None, f"`{key}` 只能是 on / off"
    if setting.kind == "int":
        lo, hi = setting.spec
        try:
            value = int(text)
        except ValueError:
            return None, f"`{key}` 要是整數 ({lo}–{hi})"
        if not lo <= value <= hi:
            return None, f"`{key}` 要在 {lo}–{hi} 之間"
        return value, None
    if MODEL_RE.match(text):
        return text, None
    return None, f"`{key}` 看起來不像模型名稱 (只允許英數與 . _ - :)"


def value_choices(key: str) -> tuple:
    """Values a menu can offer for this key; empty means "must be typed"."""
    setting = SETTABLE.get(key)
    if setting is None:
        return ()
    if setting.kind == "choice":
        return tuple(setting.spec)
    if setting.kind == "bool":
        return ("on", "off")
    return ()


# ---------- pure edits on the overrides dict ----------


def _clone(ov: dict) -> dict:
    out = dict(ov)
    out["set"] = dict(ov.get("set", {}))
    projects = ov.get("projects", {})
    out["projects"] = {"add": dict(projects.get("add", {})),
                       "remove": list(projects.get("remove", []))}
    out["_audit"] = list(ov.get("_audit", []))
    out["_baseline_pending"] = dict(ov.get("_baseline_pending", {}))
    return out


def _tidy(ov: dict) -> dict:
    """Drop empty containers so `ov.get(...)` reads falsy and the file stays small."""
    if not ov.get("set"):
        ov.pop("set", None)
    projects = ov.get("projects") or {}
    if not projects.get("add"):
        projects.pop("add", None)
    if not projects.get("remove"):
        projects.pop("remove", None)
    if not projects:
        ov.pop("projects", None)
    if not ov.get("_baseline_pending"):
        ov.pop("_baseline_pending", None)
    if not ov.get("_audit"):
        ov.pop("_audit", None)
    return ov


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _audit(ov: dict, by: str, change: str, now: datetime | None = None) -> None:
    ov.setdefault("_audit", []).append(
        {"at": _now(now).isoformat(), "by": by, "change": change})
    del ov["_audit"][:-AUDIT_LIMIT]


def set_value(ov: dict, key: str, raw, by: str,
              now: datetime | None = None) -> tuple[dict, str, str | None]:
    """Returns (overrides, message, error). On error the overrides are unchanged."""
    value, error = validate(key, raw)
    if error:
        return ov, "", error
    out = _clone(ov)
    out["set"][key] = value
    shown = _render_value(value)
    _audit(out, by, f"{key} = {shown}", now)
    return _tidy(out), f"`{key}` = *{shown}* ({SETTABLE[key].desc})", None


def _render_value(value) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


def reset(ov: dict, key: str, by: str,
          now: datetime | None = None) -> tuple[dict, str, str | None]:
    out = _clone(ov)
    if key == "all":
        out["set"] = {}
        out["projects"] = {"add": {}, "remove": []}
        out.pop("paused_until", None)
        out["_baseline_pending"] = {}
        _audit(out, by, "reset all", now)
        return _tidy(out), "已清掉所有 Slack 端的設定,回到 config.json 的值", None
    if key == "projects":
        out["projects"] = {"add": {}, "remove": []}
        _audit(out, by, "reset projects", now)
        return _tidy(out), "專案清單回到 config.json 的值", None
    if key not in SETTABLE:
        return ov, "", f"不認得的設定 `{key}`,可 reset 的有: {', '.join(sorted(SETTABLE))} / projects / all"
    if key not in out["set"]:
        return _tidy(out), f"`{key}` 本來就沒有被改過", None
    del out["set"][key]
    _audit(out, by, f"reset {key}", now)
    return _tidy(out), f"`{key}` 回到 config.json 的值", None


def changed_keys(ov: dict) -> list[str]:
    return sorted(k for k in ov.get("set", {}) if k in SETTABLE)


# ---------- project map patch ----------


def add_project(ov: dict, gitlab_path: str, local_path: str, by: str,
                now: datetime | None = None) -> dict:
    """Add a project and queue a baseline so its existing MRs are not re-notified.

    The baseline is a timestamped note for the poller (the single writer of
    state.json) rather than a direct state edit: it already holds the lock and
    already has the MR list. Timestamping makes it idempotent *and* bounded — a
    stale flag can only ever silence MRs created before that instant.
    """
    out = _clone(ov)
    out["projects"]["add"][gitlab_path] = local_path
    if gitlab_path in out["projects"]["remove"]:
        out["projects"]["remove"].remove(gitlab_path)
    out["_baseline_pending"][gitlab_path] = _now(now).isoformat()
    _audit(out, by, f"projects add {gitlab_path} -> {local_path}", now)
    return _tidy(out)


def remove_project(ov: dict, gitlab_path: str, by: str,
                   now: datetime | None = None) -> dict:
    out = _clone(ov)
    if gitlab_path in out["projects"]["add"]:
        del out["projects"]["add"][gitlab_path]        # it only ever existed as an override
    elif gitlab_path not in out["projects"]["remove"]:
        out["projects"]["remove"].append(gitlab_path)  # shadow a config.json entry
    out["_baseline_pending"].pop(gitlab_path, None)
    _audit(out, by, f"projects rm {gitlab_path}", now)
    return _tidy(out)


def baseline_pending(ov: dict) -> dict:
    return dict(ov.get("_baseline_pending", {}))


def clear_baseline_pending(ov: dict, paths) -> dict:
    out = _clone(ov)
    for path in paths:
        out["_baseline_pending"].pop(path, None)
    return _tidy(out)


# ---------- pause ----------


def set_pause(ov: dict, seconds: int, by: str, now: datetime | None = None) -> dict:
    out = _clone(ov)
    deadline = _now(now) + timedelta(seconds=seconds)
    out["paused_until"] = deadline.isoformat()
    _audit(out, by, f"pause until {deadline.isoformat(timespec='minutes')}", now)
    return _tidy(out)


def resume(ov: dict, by: str, now: datetime | None = None) -> dict:
    out = _clone(ov)
    out.pop("paused_until", None)
    _audit(out, by, "resume", now)
    return _tidy(out)


def paused_until(ov: dict) -> datetime | None:
    raw = ov.get("paused_until")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        log.warning("overrides.paused_until is not a timestamp: %r", raw)
        return None


def is_paused(ov: dict, now: datetime) -> bool:
    deadline = paused_until(ov)
    return deadline is not None and now < deadline


# ---------- merge into config ----------


def _assign(config: dict, path: tuple, value) -> None:
    node = config
    for part in path[:-1]:
        node = node.setdefault(part, {})
    node[path[-1]] = value


def apply(config: dict, ov: dict) -> dict:
    """Layer overrides onto a loaded config, in place, whitelist only."""
    for key, raw in (ov.get("set") or {}).items():
        setting = SETTABLE.get(key)
        if setting is None:
            log.warning("overrides: ignoring unknown key %r", key)
            continue
        value, error = validate(key, raw)
        if error:
            log.warning("overrides: ignoring invalid %s=%r (%s)", key, raw, error)
            continue
        _assign(config, setting.path, value)

    patch = ov.get("projects") or {}
    if patch.get("add") or patch.get("remove"):
        review = config.setdefault("review", {})
        project_map = dict(review.get("project_map", {}))
        project_map.update(patch.get("add", {}))
        for path in patch.get("remove", []):
            project_map.pop(path, None)
        review["project_map"] = project_map

    if ov.get("paused_until"):
        config["paused_until"] = ov["paused_until"]
    return config


# ---------- file IO ----------


def load(path: Path) -> dict:
    """Missing or corrupt file reads as "no overrides" — never take the poller down."""
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as exc:
        log.warning("overrides.json unreadable (%s); ignoring it", exc)
        return {}


def save(ov: dict, path: Path) -> None:
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(ov, ensure_ascii=False, indent=1))
    tmp.replace(path)


def update(path: Path, mutate) -> dict:
    """Read-modify-write under a lock, returning the stored result.

    Two processes edit this file: the Slack bot (settings, pause, projects) and
    the poller (clearing `_baseline_pending` once it has baselined a new
    project). Without the lock, the poller could write back a copy that predates
    a setting the bot just saved, silently reverting it. `mutate` returning None
    means "no change" and skips the write.
    """
    path = Path(path)
    with open(path.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        updated = mutate(load(path))
        if updated is not None:
            save(updated, path)
        return updated
