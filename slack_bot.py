#!/usr/bin/env python3
"""mr-sentinel Slack listener: turn channel mentions into actions.

Two ways to run, both under their own flock so a Slack outage can never slow
the 60s MR poll loop:

- default: one tick from a scheduler (~15s) that reads channel history;
- `--socket`: a long-running Socket Mode connection (needs `slack.app_token`).
  Slack pushes @mentions and *button clicks* down a WebSocket we dialled out,
  so no public Request URL is needed. On every (re)connect one history tick
  runs first, catching mentions sent while the listener was down.

    @bot              -> usage
    @bot <command>    -> run it

Buttons (socket mode) sit on the bot's own messages — review complete / failed —
and only ever encode a command: a click is turned back into a `Command` and goes
through the same `authorize` + rate limit as a typed one. (An emoji-reaction
menu was tried before and removed: any message can be reacted to, so people
reacted to the wrong one. Block Kit buttons belong to one message, so they
cannot be misaimed.)

Bookkeeping lives in bot_state.json, never state.json: the poller is the single
writer of that file and the two processes hold independent locks.

Failure policy is at-most-once — the read cursor advances *before* anything is
executed. A rerun that runs twice would burn model quota twice, and a message
that crashes the dispatcher would otherwise be retried forever, spamming the
channel.
"""
import argparse
import fcntl
import json
import logging
import sys
import time
import urllib.error
from collections import Counter
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import blocks
import appeal
import commands
import gitlab_client
import overrides
import review_common
import reviewer
import slack_client
import socket_mode
from sentinel_config import (OVERRIDES_PATH, SCRIPT_DIR, SOCKET_HEARTBEAT_PATH, load_config,
                             load_state)

BOT_STATE_PATH = SCRIPT_DIR / "bot_state.json"
HISTORY_LIMIT = 50
# reviews/ grows one directory per reviewed MR forever; bound how far back a
# single status command will read looking for a project's last review
PROJECT_SCAN_LIMIT = 400
# verbs a button may carry; anything else in a (forged) button value is refused
BUTTON_VERBS = {"rerun", "appeal"}
CLICK_LOG_LIMIT = 200
HANDLED_LIMIT = 500

log = logging.getLogger("mr_sentinel.slack_bot")


# ---------- pure helpers ----------


def _gt(a, b) -> bool:
    """Slack timestamps are decimal strings; compare them numerically."""
    try:
        return float(a) > float(b)
    except (TypeError, ValueError):
        return False


def collect_events(messages: list[dict], cursor: str, fetch_replies) -> tuple[list[dict], str]:
    """New messages since `cursor`, including replies inside older threads.

    Thread replies never appear in `conversations.history`, but a parent message
    carries `latest_reply` — so re-reading the last N parents (no `oldest`) is
    what lets a single call notice activity in a thread from three days ago.
    """
    newest = cursor
    picked: dict[str, dict] = {}
    for message in messages:
        ts = message.get("ts") or "0"
        if _gt(ts, newest):
            newest = ts
        if _gt(ts, cursor):
            picked[ts] = message
        latest = message.get("latest_reply")
        if latest and _gt(latest, cursor):
            for reply in fetch_replies(message["ts"], cursor):
                rts = reply.get("ts") or "0"
                if _gt(rts, newest):
                    newest = rts
                if _gt(rts, cursor) and rts != message.get("ts"):
                    picked[rts] = reply
    return [picked[ts] for ts in sorted(picked, key=float)], newest


def is_actionable(message: dict, bot_user_id: str) -> bool:
    """Skip our own messages, other apps, and channel-join noise.

    Skipping anything with a `bot_id` is what stops a menu we posted from being
    read back as a command — an infinite self-trigger loop.
    """
    if message.get("bot_id") or message.get("user") == bot_user_id:
        return False
    return message.get("subtype") in (None, "thread_broadcast", "file_share")


def _is_locked(path: Path) -> bool:
    """A flock we cannot take means a reviewer is holding it right now."""
    try:
        with open(path, "r") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle, fcntl.LOCK_UN)
                return False
            except BlockingIOError:
                return True
    except OSError:
        return False


def _ago(seconds: float) -> str:
    minutes, secs = divmod(int(max(seconds, 0)), 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def load_bot_state(path: Path = BOT_STATE_PATH) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as exc:
        log.warning("bot_state.json unreadable (%s); starting fresh", exc)
        return {}


def save_bot_state(bot_state: dict, path: Path = BOT_STATE_PATH) -> None:
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(bot_state, ensure_ascii=False, indent=1))
    tmp.replace(path)


# ---------- the bot ----------


class Bot:
    def __init__(self, config: dict, state: dict, bot_state: dict,
                 bot_state_path: Path = BOT_STATE_PATH,
                 overrides_path: Path = OVERRIDES_PATH):
        self.config = config
        self.state = state
        self.bot_state = bot_state
        self.bot_state_path = bot_state_path
        self.overrides_path = overrides_path
        self.slack = config.get("slack", {})
        self.token = self.slack.get("bot_token")
        self.channel = self.slack.get("channel_id")
        self._gitlab_user = None

    # --- plumbing ---

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def save(self) -> None:
        save_bot_state(self.bot_state, self.bot_state_path)

    def say(self, text: str, thread_ts: str | None = None) -> str | None:
        try:
            return slack_client.chat_post_message(
                self.token, self.channel, text, thread_ts,
                self.slack.get("display_name"), self.slack.get("icon_emoji"))
        except Exception:
            log.exception("reply failed (dropped)")
            return None

    def is_admin(self, user_id: str) -> bool:
        return commands.is_admin(user_id, self.slack)

    def edit_overrides(self, mutate):
        return overrides.update(self.overrides_path, mutate)

    def gitlab_user_id(self):
        if self._gitlab_user is None:
            self._gitlab_user = gitlab_client.get_current_user(
                self.config["gitlab_url"], self.config["gitlab_token"])["id"]
        return self._gitlab_user

    def ensure_identity(self) -> str:
        if not self.bot_state.get("bot_user_id"):
            info = slack_client.auth_test(self.token)
            self.bot_state["bot_user_id"] = info["user_id"]
            self.bot_state["bot_handle"] = info.get("user", commands.BOT_HANDLE)
            self.save()
            log.info("identified as %s (%s)", info.get("user"), info["user_id"])
        return self.bot_state["bot_user_id"]

    # --- the tick ---

    def tick(self) -> None:
        bot_user_id = self.ensure_identity()
        messages = slack_client.conversations_history(self.token, self.channel, HISTORY_LIMIT)
        cursor = self.bot_state.get("cursor")
        if cursor is None:
            # same rule as the poller's first run: build a baseline, act on
            # nothing, so a fresh install (or a deleted state file) never
            # replays days of old messages as commands
            self.bot_state["cursor"] = max((m.get("ts", "0") for m in messages),
                                           key=float, default="0")
            self.save()
            log.info("initialized cursor at %s; no commands executed",
                     self.bot_state["cursor"])
            return

        def fetch(parent_ts, oldest):
            return slack_client.conversations_replies(self.token, self.channel,
                                                      parent_ts, oldest=oldest)

        events, newest = collect_events(messages, cursor, fetch)
        self.bot_state["cursor"] = newest
        events = [m for m in events
                  if is_actionable(m, bot_user_id) and self.claim(m.get("ts"))]
        self.save()                        # advance before acting: at-most-once

        for message in events:
            try:
                self.handle_message(message, bot_user_id)
            except Exception:
                log.exception("command failed: %r", message.get("text"))
                self.say(":boom: 這個指令執行失敗了,詳細錯誤在 `slack_bot.log`",
                         message.get("thread_ts") or message.get("ts"))

    def handle_message(self, message: dict, bot_user_id: str) -> None:
        cmd = commands.parse(message.get("text", ""), bot_user_id)
        if cmd is None:
            return
        user = message.get("user")
        thread_ts = message.get("thread_ts") or message.get("ts")
        log.info("command from %s: %s %s", user, cmd.verb, cmd.args)
        denial = commands.authorize(cmd, user, self.slack)
        if denial:
            self.say(denial, thread_ts)
            return
        self.dispatch(cmd, user, thread_ts)

    def claim(self, ts: str | None) -> bool:
        """Mark a message as handled; False if it already was. Shared by the
        history tick and the socket, which can both see the same mention (and the
        socket may deliver them out of order, so a cursor alone is not enough).
        The caller saves."""
        if not ts:
            return False
        handled = self.bot_state.setdefault("handled", [])
        if ts in handled:
            return False
        handled.append(ts)
        del handled[:-HANDLED_LIMIT]
        return True

    # --- socket mode ---

    def on_socket_event(self, kind: str, payload: dict) -> None:
        if kind == "hello":
            self.tick()                         # catch up on mentions missed while down
        elif kind == "events_api":
            self.handle_event(payload.get("event") or {})
        elif kind == "interactive":
            click = blocks.parse_click(payload)
            if click:
                self.handle_click(click)

    def handle_event(self, event: dict) -> None:
        """An @mention pushed over the socket: same path as one read from history.

        `claim` keeps the two paths from running a message twice; the cursor is
        only nudged forward so the next catch-up tick has less to re-read."""
        if event.get("type") != "app_mention" or event.get("channel") != self.channel:
            return
        bot_user_id = self.ensure_identity()
        ts = event.get("ts")
        if not is_actionable(event, bot_user_id) or not self.claim(ts):
            return
        cursor = self.bot_state.get("cursor")
        if cursor is not None and _gt(ts, cursor):
            self.bot_state["cursor"] = ts
        self.save()                            # claim before acting: at-most-once
        try:
            self.handle_message(event, bot_user_id)
        except Exception:
            log.exception("command failed: %r", event.get("text"))
            self.say(":boom: 這個指令執行失敗了,詳細錯誤在 `slack_bot.log`",
                     event.get("thread_ts") or ts)

    def whisper(self, user: str, text: str, thread_ts: str | None = None) -> None:
        try:
            slack_client.post_ephemeral(self.token, self.channel, user, text, thread_ts)
        except Exception:
            log.exception("ephemeral reply failed (dropped)")

    def handle_click(self, click: dict) -> None:
        """A button press: authorize like a typed command, run it once, then swap
        the buttons for who-did-what so the message cannot be pressed again."""
        cmd, user = click["command"], click["user"]
        thread_ts, message_ts = click["thread_ts"], click["message_ts"]
        if click["channel"] != self.channel or not user or not message_ts:
            return
        if cmd.verb not in BUTTON_VERBS:
            log.warning("button with unexpected verb %r from %s refused", cmd.verb, user)
            return
        clicks = self.bot_state.setdefault("clicks", {})
        if message_ts in clicks or message_ts in self.bot_state.get("stale_buttons", []):
            self.whisper(user, "這個按鈕已經有人按過了 🙂", thread_ts)
            return
        denial = commands.authorize(cmd, user, self.slack)
        if denial:
            self.whisper(user, denial, thread_ts)
            return
        clicks[message_ts] = self.now().isoformat()        # claim before acting
        for old in sorted(clicks, key=clicks.get)[:-CLICK_LOG_LIMIT]:
            del clicks[old]
        self.save()
        log.info("button %s from %s: %s %s", click["action_id"], user, cmd.verb, cmd.args)
        try:
            done = self.dispatch(cmd, user, thread_ts)
        except Exception:
            log.exception("button %s failed", click["action_id"])
            done = False
            self.say(":boom: 按鈕執行失敗了,詳細錯誤在 `slack_bot.log`", thread_ts)
        if not done:
            # refused (rate limit, unknown MR…) — the reason is already in the
            # thread; free the message so it can be pressed again later
            self.bot_state.get("clicks", {}).pop(message_ts, None)
            self.save()
            return
        if cmd.verb == "appeal":
            note = f":scales: <@{user}> 已送出「不用修」,AI 正在讀回覆"
        else:
            note = f":recycle: <@{user}> 已觸發重審(`{cmd.args[-1] if cmd.args else 'auto'}`)"
        new_blocks = blocks.resolved(click["blocks"], note)
        for attempt in (1, 2):
            try:
                slack_client.chat_update(self.token, self.channel, message_ts, click["text"],
                                         new_blocks)
                return
            except Exception:
                log.exception("chat.update of clicked message failed (attempt %s)", attempt)
        # the job did run, so a second press must still be refused — but say so
        # in the thread, since the stale button would otherwise look unpressed.
        # A button still on screen must stay refused for good, so it is kept out
        # of the bounded `clicks` log (it only grows on a Slack failure: tiny).
        self.bot_state.setdefault("stale_buttons", []).append(message_ts)
        self.save()
        self.say(f"{note}\n_(按鈕沒能更新,再按一次不會重複執行)_", thread_ts)

    # --- dispatch ---

    def dispatch(self, cmd: commands.Command, user: str, thread_ts: str | None) -> None:
        handler = {
            "help": self.do_help, "status": self.do_status, "rerun": self.do_rerun,
            "appeal": self.do_appeal,
            "set": self.do_set, "reset": self.do_reset, "automerge": self.do_automerge,
            "pause": self.do_pause, "resume": self.do_resume,
            "projects": self.do_projects, "unknown": self.do_unknown,
        }.get(cmd.verb)
        if handler is None:
            self.say(commands.format_help(self.is_admin(user), self.handle()), thread_ts)
            return None
        return handler(cmd, user, thread_ts)

    def handle(self) -> str:
        return self.bot_state.get("bot_handle", commands.BOT_HANDLE)

    def do_help(self, cmd, user, thread_ts) -> None:
        self.say(commands.format_help(self.is_admin(user), self.handle()), thread_ts)

    def do_unknown(self, cmd, user, thread_ts) -> None:
        word = cmd.args[0] if cmd.args else ""
        self.say(f"不認得 `{word}`。\n" + commands.format_help(self.is_admin(user), self.handle()),
                 thread_ts)

    # --- status ---

    IDENTITY_FIELDS = ("project", "iid", "title", "web_url", "author",
                       "source_branch", "target_branch")

    def review_identity(self, mr_id) -> dict:
        """Everything known about an MR id, for describing a review afterwards.

        The poller's identity map is preferred (it is refreshed every poll), with
        gaps filled from the review's own `mr_context.json` — that file outlives
        the 7-day pruning of state.json, so a month-old review can still say which
        project, author and branches it was about.
        """
        info = dict((self.state.get("mrs") or {}).get(str(mr_id)) or {})
        try:
            ctx = json.loads((reviewer.REVIEWS_DIR / str(mr_id) / "mr_context.json").read_text())
        except (OSError, ValueError):
            ctx = {}
        for key in self.IDENTITY_FIELDS:
            if not info.get(key) and ctx.get(key):
                info[key] = ctx[key]
        info.setdefault("iid", str(mr_id))
        return info

    def running_reviews(self) -> list[dict]:
        out = []
        for lock in sorted(reviewer.REVIEWS_DIR.glob(".lock-*")):
            if not _is_locked(lock):
                continue
            info = self.review_identity(lock.name[len(".lock-"):])
            info["verdict"] = f"已跑 {_ago(time.time() - lock.stat().st_mtime)}"
            out.append(info)
        return out

    def latest_per_project(self) -> tuple[list[dict], list[str]]:
        """The newest review of each watched project, plus the ones never reviewed.

        Preferred over "the newest N reviews", which bunch up on whichever project
        happened to be busy and silently hide a project nobody has looked at. The
        universe is `review.project_map` — group-polled projects outside it are only
        notified, never reviewed, so listing them would be permanent noise.
        """
        watched = set(self.config.get("review", {}).get("project_map", {}))
        try:
            files = sorted(reviewer.REVIEWS_DIR.glob("*/final_findings.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return [], sorted(watched)
        entries, seen = [], set()
        for path in files[:PROJECT_SCAN_LIMIT]:
            try:
                data = json.loads(path.read_text())
            except (ValueError, OSError):
                continue
            info = self.review_identity(path.parent.name)
            project = (data.get("mr") or {}).get("project") or info.get("project")
            if project not in watched or project in seen:
                continue
            seen.add(project)
            findings = data.get("findings") or []
            counts = Counter(f.get("severity") for f in findings)
            info["project"] = project
            info["verdict"] = ("✅ 無發現" if not findings else
                               f"🔴{counts['high']} 🟠{counts['medium']} 🟡{counts['low']}")
            info["when"] = datetime.fromtimestamp(path.stat().st_mtime).strftime("%m-%d %H:%M")
            entries.append(info)
            if watched <= seen:
                break                       # every project covered; stop reading files
        return entries, sorted(watched - seen)

    def do_status(self, cmd, user, thread_ts) -> None:
        review = self.config.get("review", {})
        claude = review.get("claude", {})
        ov = overrides.load(self.overrides_path)
        paused = overrides.is_paused(ov, self.now())
        per_project, never = self.latest_per_project()
        self.say(commands.format_status({
            "last_poll": self.state.get("last_poll"),
            "poll_errors": self.state.get("poll_errors"),
            "opened_count": self.state.get("opened_count"),
            "running": self.running_reviews(),
            "per_project": per_project,
            "never_reviewed": never,
            "settings": {"engine": review.get("engine"), "model": claude.get("model"),
                         "effort": claude.get("effort"), "language": review.get("language"),
                         "automerge": review.get("auto_merge_on_clean")},
            "changed_keys": overrides.changed_keys(ov),
            "paused_until": ov.get("paused_until") if paused else None,
            "pending_while_paused": self.state.get("held_while_paused"),
            "projects": len(review.get("project_map", {})),
            "groups": len(self.config.get("watch", {}).get("group_ids", [])),
        }), thread_ts)

    # --- settings ---

    def current_settings(self) -> dict:
        review = self.config.get("review", {})
        claude = review.get("claude", {})
        return {
            "engine": review.get("engine"), "effort": claude.get("effort"),
            "language": review.get("language"), "model": claude.get("model"),
            "skeptic": claude.get("skeptic_model"),
            "automerge": "on" if review.get("auto_merge_on_clean") else "off",
            "maxfiles": review.get("max_changed_files"),
            "maxlines": review.get("max_diff_lines"),
            "timeout": review.get("review_timeout_seconds"),
        }

    def do_set(self, cmd, user, thread_ts) -> None:
        if not cmd.args:
            self.say(commands.format_settings(self.current_settings()), thread_ts)
            return
        key = cmd.args[0].lower()
        if len(cmd.args) == 1:
            # a missing value answers with the valid values, so discovery is one message
            self.say(commands.format_value_help(key, self.current_settings().get(key)),
                     thread_ts)
            return
        outcome = {}

        def mutate(ov):
            updated, message, error = overrides.set_value(ov, key, " ".join(cmd.args[1:]), by=user)
            outcome.update(message=message, error=error)
            return None if error else updated

        self.edit_overrides(mutate)
        if outcome.get("error"):
            self.say(f":warning: {outcome['error']}", thread_ts)
            return
        self.say(f":white_check_mark: {outcome['message']}\n"
                 f"_下一條 MR(或下次 rerun)開始生效_", thread_ts)

    def do_automerge(self, cmd, user, thread_ts) -> None:
        if not cmd.args:
            self.say(commands.format_value_help("automerge",
                                                self.current_settings().get("automerge")),
                     thread_ts)
            return
        self.do_set(commands.Command("set", ["automerge"] + cmd.args), user, thread_ts)

    def do_reset(self, cmd, user, thread_ts) -> None:
        key = cmd.args[0].lower() if cmd.args else "all"
        outcome = {}

        def mutate(ov):
            updated, message, error = overrides.reset(ov, key, by=user)
            outcome.update(message=message, error=error)
            return None if error else updated

        self.edit_overrides(mutate)
        if outcome.get("error"):
            self.say(f":warning: {outcome['error']}", thread_ts)
            return
        self.say(f":white_check_mark: {outcome['message']}", thread_ts)

    def do_pause(self, cmd, user, thread_ts) -> None:
        seconds = commands.parse_duration(cmd.args[0] if cmd.args else None)
        if seconds is None:
            self.say(f":warning: 時間看不懂。用 `pause 30m` / `pause 2h`"
                     f"(最多 {commands.MAX_PAUSE_SECONDS // 3600} 小時)", thread_ts)
            return
        self.edit_overrides(lambda ov: overrides.set_pause(ov, seconds, by=user))
        until = overrides.paused_until(overrides.load(self.overrides_path))
        self.say(f":double_vertical_bar: 已暫停通知與 review 至 "
                 f"*{until.astimezone().strftime('%m-%d %H:%M')}*。\n"
                 f"_期間的新 MR 會累積,`resume` 後補送(不會漏掉)_", thread_ts)

    def do_resume(self, cmd, user, thread_ts) -> None:
        self.edit_overrides(lambda ov: overrides.resume(ov, by=user))
        held = self.state.get("held_while_paused") or 0
        extra = f",累積的 {held} 條會在下一輪輪詢補送" if held else ""
        self.say(f":arrow_forward: 已恢復{extra}", thread_ts)

    # --- projects ---

    def do_projects(self, cmd, user, thread_ts) -> None:
        project_map = self.config.get("review", {}).get("project_map", {})
        if not cmd.args:
            self.say(commands.format_projects(project_map), thread_ts)
            return
        action = cmd.args[0].lower()
        if action in ("add", "新增") and len(cmd.args) >= 3:
            self.add_project(cmd.args[1], cmd.args[2], user, thread_ts)
        elif action in ("rm", "remove", "del", "移除") and len(cmd.args) >= 2:
            self.remove_project(cmd.args[1], user, thread_ts)
        else:
            self.say("用法:\n• `projects` 列出\n"
                     "• `projects add <group/專案路徑> <本機 clone 路徑>`\n"
                     "• `projects rm <group/專案路徑>`", thread_ts)

    def add_project(self, gitlab_path: str, local_path: str, user: str,
                    thread_ts: str | None) -> None:
        """Both ends are verified first: a project whose clone is missing would
        just queue reviews that fail at `git fetch`, and nothing here can clone
        for you."""
        local = Path(local_path).expanduser()
        if not local.is_dir():
            self.say(f":warning: 本機路徑不存在: `{local}`\n"
                     f"_請先自己 clone 好,我不會幫你 clone_", thread_ts)
            return
        if not (local / ".git").exists():
            self.say(f":warning: `{local}` 不是 git repo(找不到 `.git`)", thread_ts)
            return
        try:
            project = gitlab_client.get_project(self.config["gitlab_url"],
                                                self.config["gitlab_token"], gitlab_path)
        except urllib.error.HTTPError as exc:
            self.say(f":warning: GitLab 找不到 `{gitlab_path}`(HTTP {exc.code})"
                     f",路徑要像 `group/subgroup/repo`", thread_ts)
            return
        self.edit_overrides(
            lambda ov: overrides.add_project(ov, gitlab_path, str(local), by=user))
        self.say(f":white_check_mark: 已加入監看: `{project.get('path_with_namespace', gitlab_path)}` "
                 f"→ `{local}`\n_現有的 open MR 會在下一輪輪詢標記為已見(不會補通知),"
                 f"之後的新 MR 才會通知_", thread_ts)

    def remove_project(self, gitlab_path: str, user: str, thread_ts: str | None) -> None:
        project_map = self.config.get("review", {}).get("project_map", {})
        if gitlab_path not in project_map:
            self.say(f":warning: `{gitlab_path}` 不在監看清單裡", thread_ts)
            return
        self.edit_overrides(lambda ov: overrides.remove_project(ov, gitlab_path, by=user))
        self.say(f":white_check_mark: 已移除監看: `{gitlab_path}`\n"
                 f"_`reset projects` 可以還原成 config.json 的清單_", thread_ts)

    # --- rerun ---

    def mr_from_thread(self, thread_ts: str | None) -> tuple[str, dict] | tuple[None, None]:
        """Reverse-lookup the MR whose notification started this thread."""
        if not thread_ts:
            return None, None
        for mr_id, ts in (self.state.get("slack_ts") or {}).items():
            if ts == thread_ts:
                info = (self.state.get("mrs") or {}).get(mr_id)
                if info:
                    return mr_id, info
        return None, None

    def open_mrs(self) -> list[dict]:
        seen = self.state.get("seen") or {}
        return [{"mr_id": mr_id, **info}
                for mr_id, info in sorted((self.state.get("mrs") or {}).items(),
                                          key=lambda kv: kv[0], reverse=True)
                if mr_id in seen]

    def resolve_target(self, cmd, user, thread_ts, verb: str = "rerun"):
        """Shared front half of rerun/appeal: which MR, is it ours, is there budget.

        Returns (project, iid, mr_id), or None after telling the user why not.
        Both burn model quota, so they share the hourly rerun budget."""
        target = None
        for arg in cmd.args:
            project, iid = commands.parse_target(arg)
            if iid:
                target = (project, iid)
        if target is None:
            mr_id, info = self.mr_from_thread(thread_ts)
            if info:
                target = (info["project"], str(info["iid"]))
            else:
                self.say(commands.format_rerun_targets(self.open_mrs()), thread_ts)
                return None

        project, iid = target
        if project is None:
            matches = [m for m in self.open_mrs() if str(m["iid"]) == iid]
            if not matches:
                self.say(f":warning: 找不到 !{iid},請用 `{verb} <group/專案>!{iid}`", thread_ts)
                return None
            if len(matches) > 1:
                listing = "\n".join(f"• `{verb} {m['project']}!{m['iid']}`" for m in matches)
                self.say(f"有好幾個專案都有 !{iid},請指定:\n{listing}", thread_ts)
                return None
            project = matches[0]["project"]

        if not review_common.is_review_target(project, self.config.get("review", {})):
            self.say(f":warning: `{project}` 不在 review 清單裡"
                     f"(`projects add` 可以加)", thread_ts)
            return None

        rerun_log = self.bot_state.setdefault("rerun_log", [])
        if not commands.rerun_allowed(rerun_log, self.now(), is_admin=self.is_admin(user)):
            self.say(f":warning: 重跑太頻繁了(每小時上限 {commands.RERUN_LIMIT_PER_HOUR} 次,"
                     f"admin 不受限)。每次重跑都會消耗模型額度,等一下再試。", thread_ts)
            return None

        base, token = self.config["gitlab_url"], self.config["gitlab_token"]
        mr_id = self.mr_id_for(project, iid, base, token)
        if mr_id is None:
            self.say(f":warning: GitLab 上找不到 `{project}!{iid}`", thread_ts)
            return None
        return project, iid, mr_id

    def spend_budget(self) -> None:
        rerun_log = self.bot_state.setdefault("rerun_log", [])
        rerun_log.append(self.now().isoformat())
        del rerun_log[:-50]
        self.save()

    def do_appeal(self, cmd, user, thread_ts):
        """💬 "replied, no fix needed": hand the developer's replies to the AI."""
        resolved = self.resolve_target(cmd, user, thread_ts, verb="appeal")
        if resolved is None:
            return None
        project, iid, mr_id = resolved
        base, token = self.config["gitlab_url"], self.config["gitlab_token"]
        discussions = gitlab_client.list_discussions(base, token, project, iid)
        appeals, counts = review_common.collect_appeals(discussions, self.gitlab_user_id())
        if not appeals and review_common.unresolved_accepts(discussions, self.gitlab_user_id()):
            # nothing new to judge, but an earlier accept never got resolved: retry it
            appeal.spawn_detached(project, iid, mr_id)
            self.say(f":scales: `{project}` !{iid}: 重試 resolve 之前判定成立的討論串", thread_ts)
            return True
        if not appeals:
            waiting = counts.get("unanswered", 0) + counts.get("rejected", 0)
            self.say(f":speech_balloon: `{project}` !{iid} 還沒看到新的回覆 — 先在 GitLab 的 "
                     f"AI 留言底下回覆「為什麼不用修」,再按一次"
                     + (f"(目前有 {waiting} 則等回覆)" if waiting else ""), thread_ts)
            return None
        appeal.spawn_detached(project, iid, mr_id)
        self.spend_budget()
        self.say(f":scales: 讀取 `{project}` !{iid} 的 {len(appeals)} 則回覆中,"
                 f"判斷完會回在 GitLab 討論串", thread_ts)
        return True

    def do_rerun(self, cmd, user, thread_ts):
        mode = next((a.lower() for a in cmd.args if a.lower() in ("auto", "lite", "deep")), "auto")
        resolved = self.resolve_target(cmd, user, thread_ts)
        if resolved is None:
            return None
        project, iid, mr_id = resolved
        base, token = self.config["gitlab_url"], self.config["gitlab_token"]

        unclaimed = self.unclaim(base, token, project, iid)
        deleted = self.clear_previous_comments(base, token, project, iid)

        reviewer.spawn_detached(project, iid, mr_id, mode)
        self.spend_budget()

        detail = []
        if not unclaimed:
            detail.append("本來就沒有認領標記")
        if deleted:
            detail.append(f"清掉 {deleted} 則沒人回覆的舊留言")
        suffix = f"\n_({', '.join(detail)})_" if detail else ""
        self.say(f":recycle: 重跑中: `{project}` !{iid} (`{mode}`){suffix}", thread_ts)
        return True

    def mr_id_for(self, project: str, iid: str, base: str, token: str):
        for mr_id, info in (self.state.get("mrs") or {}).items():
            if info.get("project") == project and str(info.get("iid")) == str(iid):
                return mr_id
        try:
            return gitlab_client.get_mr(base, token, project, iid)["id"]
        except (urllib.error.HTTPError, KeyError):
            return None

    def unclaim(self, base, token, project, iid) -> bool:
        """Drop our :eyes: so the reviewer's idempotency check lets it run again."""
        emojis = gitlab_client.get_award_emojis(base, token, project, iid)
        me = self.gitlab_user_id()
        removed = False
        for emoji in emojis:
            if emoji.get("name") == "eyes" and (emoji.get("user") or {}).get("id") == me:
                gitlab_client.delete_award_emoji(base, token, project, iid, emoji["id"])
                removed = True
        return removed

    def clear_previous_comments(self, base, token, project, iid) -> int:
        discussions = gitlab_client.list_discussions(base, token, project, iid)
        note_ids = review_common.deletable_ai_notes(discussions, self.gitlab_user_id())
        deleted = 0
        for note_id in note_ids:
            try:
                gitlab_client.delete_note(base, token, project, iid, note_id)
                deleted += 1
            except urllib.error.HTTPError:
                log.warning("could not delete note %s on !%s", note_id, iid)
        return deleted


# ---------- entry point ----------


def run_socket(config: dict, config_path: Path | None) -> int:
    app_token = config["slack"].get("app_token")
    if not app_token:
        log.error("--socket needs slack.app_token (xapp-…, scope connections:write)")
        return 1

    def on_event(kind: str, payload: dict) -> None:
        # rebuilt per event: this process lives for days, while the poller keeps
        # rewriting state.json and `set` keeps changing overrides.json
        bot = Bot(load_config(config_path), load_state() or {}, load_bot_state())
        bot.on_socket_event(kind, payload)

    def heartbeat() -> None:
        try:
            SOCKET_HEARTBEAT_PATH.touch()
        except OSError:
            log.warning("could not touch %s", SOCKET_HEARTBEAT_PATH)

    log.info("socket mode listener starting")
    socket_mode.run_forever(app_token, on_event, heartbeat=heartbeat)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="mr-sentinel Slack command listener")
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--socket", action="store_true",
                    help="stay connected over Socket Mode (buttons + instant mentions)")
    args = ap.parse_args()

    handlers: list[logging.Handler] = [
        RotatingFileHandler(SCRIPT_DIR / "slack_bot.log", maxBytes=1_000_000,
                            backupCount=2, encoding="utf-8")
    ]
    if sys.stderr.isatty():
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)

    with open(SCRIPT_DIR / ".lock-bot", "w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("previous tick still running; skipping")
            return 0
        config = load_config(args.config)
        slack = config.get("slack", {})
        if not (slack.get("bot_token") and slack.get("channel_id")):
            log.info("slack.bot_token / channel_id not configured; listener disabled")
            return 0
        if args.socket:
            return run_socket(config, args.config)
        bot = Bot(config, load_state() or {}, load_bot_state())
        try:
            bot.tick()
            return 0
        except urllib.error.HTTPError as exc:
            log.error("Slack HTTP error: %s", exc)
        except urllib.error.URLError as exc:
            log.error("network down? %s", exc)
        except RuntimeError as exc:
            # scope / membership problems land here with Slack's own error code
            log.error("%s", exc)
        except Exception:
            log.exception("unexpected error")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
