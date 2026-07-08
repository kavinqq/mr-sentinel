"""Thin GitLab REST v4 wrapper (urllib only). Write calls need an `api`-scope token."""
import json
import urllib.parse
import urllib.request

import review_common

HTTP_TIMEOUT = 15


def _call(url: str, token: str, method: str = "GET", form: dict | None = None) -> str:
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"PRIVATE-TOKEN": token})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read().decode()


def _project(base: str, project) -> str:
    """Accept a numeric id or a 'group/name' path (URL-encoded per GitLab API)."""
    return f"{base}/api/v4/projects/{urllib.parse.quote(str(project), safe='')}"


def _mr(base: str, project, iid) -> str:
    return f"{_project(base, project)}/merge_requests/{iid}"


def get_current_user(base: str, token: str) -> dict:
    return json.loads(_call(f"{base}/api/v4/user", token))


def list_opened_mrs(base: str, token: str, project, per_page: int = 100) -> list:
    params = urllib.parse.urlencode(
        {"state": "opened", "per_page": str(per_page), "order_by": "created_at", "sort": "asc"}
    )
    return json.loads(_call(f"{_project(base, project)}/merge_requests?{params}", token))


def list_group_opened_mrs(base: str, token: str, group_id, per_page: int = 100) -> list:
    """All opened MRs across a group (subgroups included) — one call covers many projects."""
    params = urllib.parse.urlencode(
        {"state": "opened", "scope": "all", "per_page": str(per_page),
         "order_by": "created_at", "sort": "asc"}
    )
    return json.loads(_call(f"{base}/api/v4/groups/{group_id}/merge_requests?{params}", token))


def get_mr_changes(base: str, token: str, project, iid) -> dict:
    return json.loads(_call(f"{_mr(base, project, iid)}/changes", token))


def get_award_emojis(base: str, token: str, project, iid) -> list:
    return json.loads(_call(f"{_mr(base, project, iid)}/award_emoji", token))


def add_award_emoji(base: str, token: str, project, iid, name: str = "eyes") -> None:
    _call(f"{_mr(base, project, iid)}/award_emoji", token, method="POST", form={"name": name})


def delete_award_emoji(base: str, token: str, project, iid, award_id) -> None:
    """Remove one of our own award emojis (ops helper: un-claim an MR to re-review it)."""
    _call(f"{_mr(base, project, iid)}/award_emoji/{award_id}", token, method="DELETE")


def post_discussion(base: str, token: str, project, iid, body: str, position: dict) -> None:
    form = {"body": body, **review_common.position_form(position)}
    _call(f"{_mr(base, project, iid)}/discussions", token, method="POST", form=form)


def post_note(base: str, token: str, project, iid, body: str) -> None:
    _call(f"{_mr(base, project, iid)}/notes", token, method="POST", form={"body": body})
