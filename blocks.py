"""Pure Block Kit builders for interactive Slack buttons (no IO, unit-tested).

A button never carries authority: its `value` only names a command, and a click
is turned back into a `commands.Command` that goes through the same
`authorize` / rate-limit path as a typed `@bot rerun`. So a forged or replayed
payload can do nothing a typed message could not.
"""
import json

import commands

# action_ids are unique within one message (Slack rejects duplicates)
RERUN_AUTO = "rerun:auto"
APPEAL = "appeal"
ACTION_PREFIX = "mrs:"          # namespace, so foreign actions are ignored


def _button(action_id: str, label: str, value: dict, style: str | None = None) -> dict:
    button = {"type": "button", "action_id": ACTION_PREFIX + action_id,
              "text": {"type": "plain_text", "text": label, "emoji": True},
              "value": json.dumps(value, separators=(",", ":"))}
    if style:
        button["style"] = style
    return button


def rerun_buttons(project: str, iid, label: str = "🔁 修好了，重審") -> list[dict]:
    """One button on purpose: `auto` lets MR size pick lite/deep, same as the
    poller. (`@bot rerun !N deep` still forces the 3-gate review by hand.)"""
    return [_button(RERUN_AUTO, label,
                    {"verb": "rerun", "args": [f"{project}!{iid}", "auto"]}, style="primary")]


def appeal_buttons(project: str, iid) -> list[dict]:
    """The developer has answered the AI comments on GitLab: ask the AI to weigh
    those replies (appeal.py). Accepted threads get resolved."""
    return [_button(APPEAL, "💬 已留言,我覺得不用修",
                    {"verb": "appeal", "args": [f"{project}!{iid}"]})]


def message(text: str, buttons: list[dict]) -> list[dict]:
    """The text as one mrkdwn section, buttons underneath."""
    out = [{"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}}]
    if buttons:
        out.append({"type": "actions", "elements": buttons})
    return out


def resolved(original: list[dict] | None, note: str) -> list[dict]:
    """Replace the buttons of a clicked message with a one-line outcome, so the
    same message cannot be clicked twice and the channel shows who did what."""
    kept = [b for b in (original or []) if b.get("type") != "actions"]
    kept.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})
    return kept


def parse_click(payload: dict) -> dict | None:
    """block_actions payload -> the bits the bot acts on, or None if it is not ours.

    Returns {command, action_id, user, channel, message_ts, thread_ts, blocks, text}.
    """
    if payload.get("type") != "block_actions":
        return None
    actions = payload.get("actions") or []
    if not actions:
        return None
    action = actions[0]
    action_id = action.get("action_id") or ""
    if not action_id.startswith(ACTION_PREFIX):
        return None
    try:
        value = json.loads(action.get("value") or "")
    except ValueError:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("verb"), str):
        return None
    args = value.get("args") or []
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return None
    msg = payload.get("message") or {}
    container = payload.get("container") or {}
    return {
        "command": commands.Command(value["verb"], args),
        "action_id": action_id[len(ACTION_PREFIX):],
        "user": (payload.get("user") or {}).get("id"),
        "channel": (payload.get("channel") or {}).get("id") or container.get("channel_id"),
        "message_ts": container.get("message_ts") or msg.get("ts"),
        "thread_ts": msg.get("thread_ts") or container.get("thread_ts"),
        "blocks": msg.get("blocks"),
        "text": msg.get("text", ""),
    }
