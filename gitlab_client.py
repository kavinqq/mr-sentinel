"""Thin GitLab REST v4 wrapper (urllib only). Write calls need an `api`-scope token."""
import json
import urllib.error
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


def get_mr(base: str, token: str, project, iid) -> dict:
    """Single-MR read (draft flag, merge_status, head pipeline) for the merge rails."""
    return json.loads(_call(_mr(base, project, iid), token))


def merge_mr(base: str, token: str, project, iid, sha: str | None = None) -> dict:
    """Merge the MR with project defaults. Raises (HTTPError) if GitLab refuses.

    `sha` makes GitLab refuse (409) unless the head is still that commit, closing
    the race between our last check and the merge itself."""
    form = {"sha": sha} if sha else None
    return json.loads(_call(f"{_mr(base, project, iid)}/merge", token, method="PUT", form=form))


def has_ci_config(base: str, token: str, project, ref: str) -> bool:
    """Whether the project has a CI config at `ref`. Raises on anything but a 404,
    so a failed lookup can't be mistaken for "no CI"."""
    path = json.loads(_call(_project(base, project), token)).get("ci_config_path") or ".gitlab-ci.yml"
    if "@" in path or "://" in path:
        return True  # config lives in another project or a URL; we can't see it, so assume CI
    url = (f"{_project(base, project)}/repository/files/{urllib.parse.quote(path, safe='')}"
           f"?ref={urllib.parse.quote(ref, safe='')}")
    try:
        _call(url, token)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise
    return True


def get_award_emojis(base: str, token: str, project, iid) -> list:
    return json.loads(_call(f"{_mr(base, project, iid)}/award_emoji", token))


def add_award_emoji(base: str, token: str, project, iid, name: str = "eyes") -> None:
    _call(f"{_mr(base, project, iid)}/award_emoji", token, method="POST", form={"name": name})


def delete_award_emoji(base: str, token: str, project, iid, award_id) -> None:
    """Remove one of our own award emojis (ops helper: un-claim an MR to re-review it)."""
    _call(f"{_mr(base, project, iid)}/award_emoji/{award_id}", token, method="DELETE")


def get_project(base: str, token: str, project) -> dict:
    """Existence/visibility probe before adding a project to the watch list."""
    return json.loads(_call(_project(base, project), token))


def list_discussions(base: str, token: str, project, iid, per_page: int = 100,
                     max_pages: int = 50) -> list:
    """All discussions with their notes, every page (GitLab caps per_page at 100).

    Callers decide "nothing left open" from this list — a second page silently
    dropped would read as clean."""
    out: list = []
    for page in range(1, max_pages + 1):
        batch = json.loads(_call(f"{_mr(base, project, iid)}/discussions"
                                 f"?per_page={per_page}&page={page}", token))
        out += batch
        if len(batch) < per_page:
            return out
    raise RuntimeError(f"MR !{iid} has more than {per_page * max_pages} discussions")


def delete_note(base: str, token: str, project, iid, note_id) -> None:
    """Delete one of our own notes (re-review cleanup). Only note authors may."""
    _call(f"{_mr(base, project, iid)}/notes/{note_id}", token, method="DELETE")


def post_discussion(base: str, token: str, project, iid, body: str, position: dict) -> None:
    form = {"body": body, **review_common.position_form(position)}
    _call(f"{_mr(base, project, iid)}/discussions", token, method="POST", form=form)


def post_note(base: str, token: str, project, iid, body: str) -> None:
    _call(f"{_mr(base, project, iid)}/notes", token, method="POST", form={"body": body})


def reply_discussion(base: str, token: str, project, iid, discussion_id, body: str) -> None:
    """Add a note to an existing thread (appeal verdicts answer the developer in place)."""
    _call(f"{_mr(base, project, iid)}/discussions/{discussion_id}/notes", token,
          method="POST", form={"body": body})


def resolve_discussion(base: str, token: str, project, iid, discussion_id) -> None:
    """Raises (HTTPError) for a thread that is not resolvable, e.g. a plain note."""
    _call(f"{_mr(base, project, iid)}/discussions/{discussion_id}", token,
          method="PUT", form={"resolved": "true"})
