"""Thin Slack Web API wrapper (urllib only). Bot token needs chat:write + reactions:write."""
import json
import urllib.request

HTTP_TIMEOUT = 10


def _post(method: str, token: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def chat_post_message(token: str, channel: str, text: str) -> str:
    """Post a message and return its ts (needed later for reactions/threading)."""
    resp = _post("chat.postMessage", token, {"channel": channel, "text": text, "unfurl_links": False})
    if not resp.get("ok"):
        raise RuntimeError(f"Slack chat.postMessage failed: {resp.get('error')}")
    return resp["ts"]


def post_webhook(webhook_url: str, text: str) -> None:
    """Incoming-webhook fallback: can post messages but returns no ts (so no reactions)."""
    req = urllib.request.Request(
        webhook_url,
        data=json.dumps({"text": text, "unfurl_links": False}).encode(),
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
