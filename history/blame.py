"""git blame for every finding: who actually wrote the flagged line.

The MR a comment sits on is not proof of authorship: a release MR (pre-prod ->
master) is everyone's work, and in this team even a personal merge branch can
carry other people's commits. The line is: blame it at the exact commit the
comment was made on (the position's head_sha) in the local clone the reviewer
already uses, and record the commit + author email. score.attribution maps the
email to a person — only through a confirmed alias, never a guess.

Pure plumbing — no model. Results (or the reason it could not be done) are
stored in finding_blame, so each finding is blamed once.
"""
import logging
import re
import subprocess

import review_common
from history import db

log = logging.getLogger("mr_sentinel.history")

GIT_TIMEOUT = 60


def parse_porcelain(output: str) -> dict:
    """`git blame --porcelain -L n,n` -> {sha, email, name}."""
    lines = output.splitlines()
    if not lines or len(lines[0].split()) < 3:
        raise ValueError("unexpected git blame output")
    out = {"sha": lines[0].split()[0], "email": None, "name": None}
    for line in lines[1:]:
        if line.startswith("author-mail "):
            out["email"] = line[len("author-mail "):].strip().strip("<>").lower() or None
        elif line.startswith("author "):
            out["name"] = line[len("author "):].strip()
    return out


def _git(local: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", local, *args], capture_output=True, text=True,
                          timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL)


def blame_line(local: str, iid, sha: str, path: str, line: int) -> dict:
    """Blame one line at `sha`, fetching the MR ref first if the commit is missing.

    Arguments come from GitLab data, so they are validated before reaching git:
    no shell is involved (argv list), the sha must be hex, the line an int, the
    path is passed after `--`, and the ref is built from an int iid."""
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", sha or ""):
        raise ValueError("not a commit sha")
    line, iid = int(line), int(iid)
    if _git(local, "cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
        fetched = _git(local, "fetch", "-q", "origin",
                       f"+refs/merge-requests/{iid}/head:refs/mr-sentinel/{iid}")
        if fetched.returncode != 0:
            raise RuntimeError(f"git fetch failed: {fetched.stderr.strip()[:200]}")
    blamed = _git(local, "blame", "--porcelain", "-L", f"{line},{line}", sha, "--", path)
    if blamed.returncode != 0:
        raise RuntimeError(f"git blame failed: {blamed.stderr.strip()[:200]}")
    return parse_porcelain(blamed.stdout)


def pending(conn, mr_id: int | None = None) -> list[dict]:
    rows = conn.execute("""
        SELECT f.note_id, f.file, f.line, m.project, m.iid,
               COALESCE(f.head_sha, m.head_sha) AS head_sha
        FROM findings f JOIN mrs m ON m.mr_id = f.mr_id
        LEFT JOIN finding_blame b ON b.note_id = f.note_id
        WHERE b.note_id IS NULL AND (? IS NULL OR f.mr_id = ?)""", (mr_id, mr_id)).fetchall()
    return [dict(r) for r in rows]


def blame_pending(conn, config: dict, limit: int = 500, mr_id: int | None = None) -> tuple[int, int]:
    """Blame every not-yet-blamed finding. Returns (blamed, failed);
    a failure is recorded with its reason and not retried (see `retry_failed`)."""
    review_cfg = config.get("review") or {}
    done = failed = 0
    for row in pending(conn, mr_id)[:limit]:
        result, error = {}, None
        local = review_common.resolve_local_path(row["project"], review_cfg)
        if not row["file"] or not row["line"]:
            error = "finding has no file/line"
        elif not row["head_sha"]:
            error = "commit unknown (run a full sync)"
        elif not local:
            error = f"no local clone for {row['project']} in review.project_map"
        else:
            try:
                result = blame_line(local, row["iid"], row["head_sha"], row["file"], row["line"])
            except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
                error = str(exc)
        with conn:
            conn.execute("INSERT OR REPLACE INTO finding_blame(note_id, commit_sha, author_email, "
                         "author_name, error, blamed_at) VALUES (?, ?, ?, ?, ?, ?)",
                         (row["note_id"], result.get("sha"), result.get("email"),
                          result.get("name"), error, db.now_iso()))
        if error:
            failed += 1
            log.warning("blame of finding %s failed: %s", row["note_id"], error)
        else:
            done += 1
    return done, failed


def retry_failed(conn) -> int:
    """Forget recorded failures so the next run tries them again (e.g. after a clone
    was added to project_map)."""
    with conn:
        return conn.execute("DELETE FROM finding_blame WHERE error IS NOT NULL").rowcount
