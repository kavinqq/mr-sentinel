"""Thin Slack Web API wrapper (urllib only).

Scopes: `chat:write` + `reactions:write` to notify and claim; `channels:history`
(+ `groups:history` for private channels) and `reactions:read` for the command
listener. Reading a channel also requires the bot to be *in* it.
"""
import json
import urllib.parse
import urllib.request

HTTP_TIMEOUT = 10


def _get(method: str, token: str, params: dict) -> dict:
    """Read methods take query params, not a JSON body."""
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(
        f"https://slack.com/api/{method}?{query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def _checked_get(method: str, token: str, params: dict) -> dict:
    resp = _get(method, token, params)
    if not resp.get("ok"):
        raise RuntimeError(f"Slack {method} failed: {resp.get('error')}")
    return resp


def _post(method: str, token: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def chat_post_message(token: str, channel: str, text: str, thread_ts: str | None = None,
                      username: str | None = None, icon_emoji: str | None = None) -> str:
    """Post a message and return its ts (needed later for reactions/threading).

    thread_ts replies inside an existing message's thread instead of top-level.
    username/icon_emoji need the `chat:write.customize` scope, so they are only
    sent when explicitly configured — passing them without the scope would make
    every notification fail."""
    payload = {"channel": channel, "text": text, "unfurl_links": False}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    if username:
        payload["username"] = username
    if icon_emoji:
        payload["icon_emoji"] = icon_emoji
    resp = _post("chat.postMessage", token, payload)
    if not resp.get("ok"):
        raise RuntimeError(f"Slack chat.postMessage failed: {resp.get('error')}")
    return resp["ts"]


def post_webhook(webhook_url: str, text: str) -> None:
    """Incoming-webhook fallback: can post messages but returns no ts (so no reactions).

    username/icon override the display name on legacy custom-integration webhooks
    (so messages are branded as this tool, not the webhook's original app name);
    app-type webhooks silently ignore these fields.
    """
    req = urllib.request.Request(
        webhook_url,
        data=json.dumps({"text": text, "unfurl_links": False,
                         "username": "mr-sentinel", "icon_emoji": ":robot_face:"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        status, body = resp.status, resp.read().decode()
    if status != 200 or body != "ok":
        raise RuntimeError(f"Slack webhook failed: {status} {body}")


def add_reaction(token: str, channel: str, ts: str, name: str = "eyes") -> None:
    """already_reacted counts as success (idempotent claims)."""
    resp = _post("reactions.add", token, {"channel": channel, "timestamp": ts, "name": name})
    if not resp.get("ok") and resp.get("error") != "already_reacted":
        raise RuntimeError(f"Slack reactions.add failed: {resp.get('error')}")


# ---------- read side (command listener) ----------


def auth_test(token: str) -> dict:
    """Who we are. `user_id` is what mentions of this bot look like: <@Uxxxx>."""
    return _checked_get("auth.test", token, {})


def conversations_history(token: str, channel: str, limit: int = 50) -> list[dict]:
    """Newest-first top-level messages.

    Deliberately fetched without `oldest`: thread replies never appear in
    history, but a parent message carries `latest_reply`, so re-reading the last
    N parents is what lets one call detect activity inside old threads too.
    """
    resp = _checked_get("conversations.history", token, {"channel": channel, "limit": limit})
    return resp.get("messages") or []


def conversations_replies(token: str, channel: str, ts: str,
                          oldest: str | None = None, limit: int = 100) -> list[dict]:
    resp = _checked_get("conversations.replies", token,
                        {"channel": channel, "ts": ts, "oldest": oldest, "limit": limit})
    return resp.get("messages") or []
