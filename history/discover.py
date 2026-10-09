"""Find where the team actually works. The bot reviews only `review.project_map`,
but people ship in many more projects — and sometimes straight to a branch,
with no MR at all. This lists every project GitLab shows active in the window,
keeps the ones with commits by someone we know (a confirmed / proven email),
and records their non-merge commits, so:

  - those projects' MRs are synced and graded like any other (history/rate.py
    grades a diff; it does not need the bot to have reviewed it);
  - commits that never went through an MR are graded in batches per person,
    project and ISO week (history/rate.py `rate_commit_batches`).

The project list is kept in sync_state `history_projects` (JSON); sync.projects
merges it with project_map. Commit emails no one is known for show up on the
dashboard's Email 對應 page, like blame emails do.
"""
import json
import logging

import gitlab_client
from history import db, score

log = logging.getLogger("mr_sentinel.history")

STATE_KEY = "history_projects"
MAX_PAGES = 30


def active_projects(base: str, token: str, since: str) -> list[str]:
    out = []
    for page in range(1, 20):
        batch = json.loads(gitlab_client._call(
            f"{base}/api/v4/projects?membership=true&simple=true&per_page=100&page={page}"
            f"&last_activity_after={since}", token))
        out += [p["path_with_namespace"] for p in batch]
        if len(batch) < 100:
            break
    return out


def project_commits(base: str, token: str, project: str, since: str) -> list[dict]:
    """Every non-merge commit on any branch since `since` (deduplicated)."""
    seen, out = set(), []
    for page in range(1, MAX_PAGES + 1):
        batch = json.loads(gitlab_client._call(
            f"{gitlab_client._project(base, project)}/repository/commits"
            f"?since={since}&all=true&per_page=100&page={page}", token))
        for c in batch:
            if c["id"] not in seen and len(c.get("parent_ids") or []) <= 1:
                seen.add(c["id"])
                out.append(c)
        if len(batch) < 100:
            break
    return out


def discover(conn, config: dict, since: str, progress=lambda msg: None) -> dict:
    """Record team commits in every active project; returns {projects, commits}."""
    base, token = config["gitlab_url"], config["gitlab_token"]
    owners = score.email_owners(conn)
    reviewed = set((config.get("review") or {}).get("project_map") or {})
    found, total = [], 0
    projects = active_projects(base, token, since)
    progress(f"在 {len(projects)} 個近期有活動的專案裡找團隊的 commit…")
    for project in projects:
        try:
            commits = project_commits(base, token, project, since)
        except Exception as exc:
            log.warning("listing commits of %s failed: %s", project, exc)
            continue
        ours = [c for c in commits if (c.get("author_email") or "").lower() in owners]
        if not ours and project not in reviewed:
            continue
        with conn:
            conn.executemany(
                "INSERT INTO project_commits(sha, project, author_email, author_name, title, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(project, sha) DO NOTHING",
                [(c["id"], project, (c.get("author_email") or "").lower() or None, c.get("author_name"),
                  (c.get("title") or "")[:300], db.utc(c.get("created_at"))) for c in commits])
        if ours:
            found.append(project)
            total += len(ours)
            progress(f"    {project}: 團隊 commit {len(ours)} 個")
    keep = sorted(set(found) - reviewed)
    with conn:
        db.set_state(conn, STATE_KEY, json.dumps(keep))
    return {"projects": keep, "commits": total}


def tracked(conn) -> list[str]:
    try:
        return json.loads(db.get_state(conn, STATE_KEY) or "[]")
    except ValueError:
        return []
