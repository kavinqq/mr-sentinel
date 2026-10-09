"""Pure Slack command layer: parse a mention, check permission, render replies.

No IO lives here, so the whole conversational surface is unit-testable offline.
Everything is typed: `@bot` alone prints usage, `@bot <verb> …` runs. Any command
that needs a value the user has not supplied answers with the list of valid
values as text, so discovery costs one message and never a stateful widget.

(An emoji-reaction menu was tried first and removed: every message in a channel
can be reacted to, but only a few are "buttons", so people react to the wrong
message. Text has no such ambiguity — and no 9-number-emoji ceiling on how many
options can be shown.)
"""
import re
from collections import namedtuple
from datetime import datetime, timedelta

import overrides

PUBLIC, MEMBER, ADMIN = "public", "member", "admin"

DEFAULT_PAUSE_SECONDS = 3600
MAX_PAUSE_SECONDS = 8 * 3600
RERUN_LIMIT_PER_HOUR = 6

BOT_HANDLE = "mrnotify"

Command = namedtuple("Command", "verb args")

MENTION_RE = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")

ALIASES = {
    "help": "help", "?": "help", "h": "help", "說明": "help", "指令": "help",
    "status": "status", "stat": "status", "st": "status", "狀態": "status", "狀況": "status",
    "rerun": "rerun", "re": "rerun", "retry": "rerun", "重跑": "rerun", "重審": "rerun",
    "appeal": "appeal", "nofix": "appeal", "申訴": "appeal", "不用修": "appeal",
    "set": "set", "設定": "set",
    "reset": "reset", "還原": "reset", "重設": "reset",
    "pause": "pause", "暫停": "pause", "mute": "pause",
    "resume": "resume", "恢復": "resume", "unmute": "resume",
    "automerge": "automerge", "auto-merge": "automerge", "自動合併": "automerge",
    "projects": "projects", "project": "projects", "proj": "projects", "專案": "projects",
}

# Unmapped verbs fail closed (see authorize) — a new command is admin-only until
# its tier is declared here on purpose.
TIERS = {
    "help": PUBLIC, "status": PUBLIC, "unknown": PUBLIC,
    "rerun": MEMBER, "appeal": MEMBER,
    "set": ADMIN, "reset": ADMIN, "pause": ADMIN, "resume": ADMIN,
    "automerge": ADMIN, "projects": ADMIN,
}

value_choices = overrides.value_choices


# ---------- parsing ----------


def _normalize(text: str) -> str:
    return (text or "").replace("　", " ").replace(" ", " ")


def parse(text: str, bot_user_id: str) -> Command | None:
    """Mention of this bot -> a Command; anything else -> None (plain chatter).

    The command normally follows the mention. Text *before* it is only read as a
    command when it starts with a known verb ("status @bot"), so conversational
    tags ("謝啦 @bot") fall through to usage instead of an "unknown" scolding.
    """
    text = _normalize(text)
    mention = next((m for m in MENTION_RE.finditer(text) if m.group(1) == bot_user_id), None)
    if mention is None:
        return None
    after = MENTION_RE.sub(" ", text[mention.end():]).strip()
    before = MENTION_RE.sub(" ", text[:mention.start()]).strip()
    for candidate, strict in ((after, True), (before, False)):
        if not candidate:
            continue
        tokens = candidate.split()
        verb = ALIASES.get(tokens[0].lower())
        if verb is not None:
            return Command(verb, tokens[1:])
        if strict:
            return Command("unknown", tokens)
    return Command("help", [])          # a bare tag asks for usage


def parse_target(token: str) -> tuple[str | None, str | None]:
    """`group/proj!481` / `!481` / `481` -> (project or None, iid or None)."""
    token = (token or "").strip()
    if "!" in token:
        project, _, iid = token.partition("!")
        if iid.isdigit():
            return (project or None), iid
        return None, None
    if token.isdigit():
        return None, token
    return None, None


def parse_duration(token: str | None) -> int | None:
    """`30m` / `2h` / bare minutes -> seconds. None if junk or over the cap."""
    if token is None:
        return DEFAULT_PAUSE_SECONDS
    match = re.fullmatch(r"(\d+)\s*([mh]?)", str(token).strip().lower())
    if not match:
        return None
    seconds = int(match.group(1)) * (3600 if match.group(2) == "h" else 60)
    if not 0 < seconds <= MAX_PAUSE_SECONDS:
        return None
    return seconds


# ---------- permission ----------


def admins(slack_cfg: dict) -> list[str]:
    """Explicit admin list, else whoever gets cc'd on notifications."""
    return list(slack_cfg.get("admin_user_ids") or slack_cfg.get("mention_user_ids") or [])


def is_admin(user_id: str, slack_cfg: dict) -> bool:
    return user_id in admins(slack_cfg)


def authorize(cmd: Command, user_id: str, slack_cfg: dict) -> str | None:
    """None = allowed; otherwise the message to reply with."""
    if TIERS.get(cmd.verb, ADMIN) in (PUBLIC, MEMBER):
        return None
    allowed = admins(slack_cfg)
    if not allowed:
        return (":no_entry: config 沒設 `slack.admin_user_ids`(也沒有 `mention_user_ids`),"
                "為安全起見所有會改設定的指令都停用。")
    if user_id not in allowed:
        return (f":no_entry: `{cmd.verb}` 限 admin 使用。"
                f"admin: {' '.join(f'<@{uid}>' for uid in allowed)}")
    return None


def rerun_allowed(log: list, now: datetime, limit: int = RERUN_LIMIT_PER_HOUR,
                  window_seconds: int = 3600, is_admin: bool = False) -> bool:
    """Reruns burn the owner's model quota, so non-admins share an hourly budget."""
    if is_admin:
        return True
    cutoff = now - timedelta(seconds=window_seconds)
    recent = 0
    for entry in log or []:
        try:
            when = datetime.fromisoformat(str(entry))
        except ValueError:
            continue
        if when >= cutoff:
            recent += 1
    return recent < limit


# ---------- rendering ----------


def format_help(is_admin: bool, handle: str = BOT_HANDLE) -> str:
    lines = [
        f"*mr-sentinel* — tag 我加上指令就會執行(例:`@{handle} status`)",
        "",
        "*查詢*(所有人)",
        "• `status` — 目前狀態、進行中的 review、最近結果、生效中的設定",
        "• `help` — 這則說明",
        "",
        f"*重跑 review*(所有人,每小時上限 {RERUN_LIMIT_PER_HOUR} 次)",
        "• `rerun` — 列出可以重跑的 MR",
        "• `rerun !481` — 重跑(深度依 MR 大小自動決定)",
        "• `rerun !481 deep` — 強制三關對抗式審查(`lite` = 單關快掃)",
        "• `appeal !481` — 已在 GitLab 回覆「不用修」的理由,請 AI 讀回覆重新判斷",
        "  (理由成立就 resolve 討論串;和重跑共用每小時上限)",
        "  _在 MR 通知的 thread 裡回覆可以省略 MR 編號_",
    ]
    if is_admin:
        lines += [
            "",
            "*設定*(admin)",
            "• `set` — 列出所有可改的設定與目前值",
            "• `set effort high` — 改單一設定",
            "• `reset effort` / `reset all` — 回到 config.json 的值",
            "• `automerge on|off` — 乾淨時是否自動合併",
            f"• `pause 2h` / `resume` — 暫停通知與 review(最多 "
            f"{MAX_PAUSE_SECONDS // 3600} 小時,期間的 MR 會累積不會漏)",
            "",
            "*監看的專案*(admin)",
            "• `projects` — 列出目前清單",
            "• `projects add <group/專案路徑> <本機 clone 路徑>`",
            "• `projects rm <group/專案路徑>`",
        ]
    return "\n".join(lines)


def allowed_desc(key: str) -> str:
    """Human description of what a setting will accept."""
    setting = overrides.SETTABLE.get(key)
    if setting is None:
        return ""
    if setting.kind == "choice":
        return " / ".join(setting.spec)
    if setting.kind == "bool":
        return "on / off"
    if setting.kind == "int":
        return f"{setting.spec[0]}–{setting.spec[1]} 的整數"
    return "自由填(英數與 . _ - :)"


def format_settings(current: dict) -> str:
    """The whole settable surface as text — no 9-option ceiling to work around."""
    lines = ["*可改的設定* — 用 `set <key> <值>`", ""]
    for key, setting in overrides.SETTABLE.items():
        value = current.get(key, "?")
        lines.append(f"• `{key}` = *{value}*  — {setting.desc}  _({allowed_desc(key)})_")
    lines += ["", "`reset <key>` 回到 config.json 的值,`reset all` 全部還原。"]
    return "\n".join(lines)


def format_value_help(key: str, current=None) -> str:
    setting = overrides.SETTABLE.get(key)
    if setting is None:
        return (f":warning: 不認得的設定 `{key}`。可設的有: "
                + ", ".join(f"`{k}`" for k in overrides.SETTABLE))
    now = f"(目前 *{current}*)" if current is not None else ""
    return (f"`{key}` — {setting.desc} {now}\n"
            f"可以是: {allowed_desc(key)}\n"
            f"例:  `set {key} {(value_choices(key) or ('<值>',))[0]}`")


def format_rerun_targets(mrs: list[dict]) -> str:
    """Copy-a-line listing, which beats a menu: no state, no expiry, no cap."""
    if not mrs:
        return ("目前沒有記錄到可以重跑的 MR。"
                "poller 每輪會更新這份清單,也可以直接打 `rerun <group/專案>!<iid>`。")
    lines = ["*可以重跑的 MR* — 複製其中一行(後面可加 `lite` / `deep`)", ""]
    for mr in mrs:
        title = (mr.get("title") or "").strip()
        lines.append(f"• `rerun {mr['project']}!{mr['iid']}`  — {title}")
    return "\n".join(lines)


def format_projects(project_map: dict) -> str:
    lines = [f"*監看中的專案({len(project_map)})*", ""]
    for path, local in sorted(project_map.items()):
        lines.append(f"• `{path}` → `{local}`")
    if not project_map:
        lines.append("_(空)_")
    lines += [
        "",
        "新增: `projects add <group/專案路徑> <本機 clone 路徑>`",
        "移除: `projects rm <group/專案路徑>`",
        "_只有清單上的專案會跑 AI review;群組輪詢抓到的其他專案只會通知。_",
    ]
    return "\n".join(lines)


def _short_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(str(iso)).astimezone().strftime("%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(iso)


TITLE_LIMIT = 70


def _short_project(path: str) -> str:
    """Last path segment: `team/backend/auth-service` -> `auth-service`.

    Repo names are unique in practice and the full path is one hover away on the
    MR link, so the short form buys readability for nothing.
    """
    return (path or "?").rstrip("/").rsplit("/", 1)[-1]


def _mr_link(info: dict) -> str:
    label = f"!{info.get('iid', '?')}"
    url = info.get("web_url")
    return f"<{url}|{label}>" if url else label


def format_review_entry(info: dict) -> list[str]:
    """One review as two lines: what it was, then who / which branches / when.

    Every field is optional: reviews recorded before a field existed, MRs whose
    identity has aged out of state.json, and projects with no review at all still
    render as much as is known.
    """
    head = f"*{_short_project(info.get('project'))}*"
    if info.get("iid"):
        head = f"{_mr_link(info)}  {head}"
    title = (info.get("title") or "").strip()
    if title:
        if len(title) > TITLE_LIMIT:
            title = title[:TITLE_LIMIT - 1] + "…"
        head += f" — {title}"

    bits = []
    if info.get("verdict"):
        bits.append(info["verdict"])
    if info.get("author"):
        bits.append(f":bust_in_silhouette: {info['author']}")
    if info.get("source_branch"):
        bits.append(f"`{info['source_branch']}` → `{info.get('target_branch') or '?'}`")
    if info.get("when"):
        bits.append(info["when"])
    return [head] + ([" · ".join(bits)] if bits else [])


def _review_block(title: str, entries: list[dict]) -> list[str]:
    lines = [f"• {title}:"]
    for info in entries:
        rendered = format_review_entry(info)
        lines.append("    ▸ " + rendered[0])
        lines += ["       " + line for line in rendered[1:]]
    return lines


def format_status(facts: dict) -> str:
    lines = [":robot_face: *mr-sentinel 狀態*"]

    if facts.get("paused_until"):
        pending = facts.get("pending_while_paused") or 0
        extra = f",已累積 {pending} 條待通知" if pending else ""
        lines.append(f"• :double_vertical_bar: *暫停中* 至 "
                     f"{_short_time(facts['paused_until'])}{extra} — `resume` 可恢復")

    if facts.get("last_poll"):
        errors = facts.get("poll_errors") or 0
        health = "正常" if not errors else f":warning: {errors} 個來源失敗"
        lines.append(f"• 最後輪詢: {_short_time(facts['last_poll'])} ({health})")

    if facts.get("opened_count") is not None:
        lines.append(f"• 目前 open 的 MR: {facts['opened_count']} 條")

    running = facts.get("running")
    if running:
        lines += _review_block("進行中的 review", running)
    elif running is not None:
        lines.append("• 進行中的 review: 無")

    if facts.get("per_project"):
        lines += _review_block("各專案最新 review", facts["per_project"])
    if facts.get("never_reviewed"):
        names = ", ".join(f"`{_short_project(p)}`" for p in facts["never_reviewed"])
        lines.append(f"    ▸ :grey_question: 還沒有 review 紀錄: {names}")

    settings = facts.get("settings")
    if settings:
        lines.append(
            f"• 設定: engine=`{settings.get('engine')}` model=`{settings.get('model')}` "
            f"effort=`{settings.get('effort')}` lang=`{settings.get('language')}` "
            f"automerge=`{'on' if settings.get('automerge') else 'off'}`")

    if facts.get("changed_keys"):
        keys = " ".join(f"`{k}`" for k in facts["changed_keys"])
        lines.append(f"    └ Slack 改過: {keys} (`reset all` 回 config.json 預設)")

    if facts.get("projects") is not None:
        lines.append(f"• 監看: {facts['projects']} 專案 / {facts.get('groups', 0)} 群組")

    return "\n".join(lines)
