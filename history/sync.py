"""GitLab -> sentinel.db. Idempotent: every row is keyed by a GitLab id, so a
re-run (or two machines syncing the same projects) can never double-count.

GitLab is the source of truth because it holds what local files cannot: findings
a rerun already deleted locally, developer replies, appeal verdicts, resolves.
A finding that disappears from GitLab (rerun cleanup of an unanswered comment)
is kept with present=0 — the developer still made that mistake.
"""
import logging
from datetime import datetime, timedelta

import gitlab_client
import review_common
from history import db
from history.parse import is_fix_mr, is_release_mr, parse_comment

log = logging.getLogger("mr_sentinel.history")

DEFAULT_SINCE = "2026-07-01T00:00:00Z"     # the bot's first reviews; nothing to learn before
UPDATED_MARGIN = timedelta(hours=1)        # clock skew / same-second updates


def appeal_verdict(discussion: dict, user_id) -> str | None:
    """Our latest appeal reply in the thread, if any."""
    for note in reversed(review_common._human_notes(discussion)):
        if (note.get("author") or {}).get("id") != user_id:
            continue
        body = note.get("body") or ""
        if review_common.APPEAL_ACCEPT in body:
            return "accept"
        if review_common.APPEAL_REJECT in body:
            return "reject"
    return None


def _upsert_person(conn, user: dict | None) -> int | None:
    if not user or user.get("id") is None:
        return None
    conn.execute("INSERT INTO people(gitlab_id, username, name) VALUES (?, ?, ?) "
                 "ON CONFLICT(gitlab_id) DO UPDATE SET username = excluded.username, "
                 "name = excluded.name", (user["id"], user.get("username") or "", user.get("name")))
    return user["id"]


def store_mr(conn, project: str, mr: dict, discussions: list, awards: list, me: int,
             files: list[str] | None, commits: list[dict] | None = None) -> int:
    """Write one MR and its AI findings (pure DB work, given the fetched GitLab data).
    Returns how many AI findings the MR currently has on GitLab."""
    now = db.now_iso()
    author = _upsert_person(conn, mr.get("author"))
    found = []
    for discussion in discussions:
        status = review_common.discussion_status(discussion, me)
        if status is None:
            continue
        first = review_common._human_notes(discussion)[0]
        position = first.get("position") or {}
        parsed = parse_comment(first.get("body", ""))
        found.append((first["id"], str(discussion.get("id")), parsed, position, first, status,
                      appeal_verdict(discussion, me)))

    # every human note on the MR (not ours, not GitLab's system notes) — a lead's
    # review comments on someone else's MR are team contribution (history/contrib.py)
    human = []
    for discussion in discussions:
        for n in discussion.get("notes") or []:
            writer = (n.get("author") or {}).get("id")
            if n.get("system") or writer in (None, me) or not (n.get("body") or "").strip():
                continue
            human.append((n["id"], writer, db.utc(n.get("created_at")), (n.get("body") or "")[:4000]))

    reviewed = int(bool(found) or review_common.has_own_award_emoji(awards, me))
    conn.execute("""
        INSERT INTO mrs(mr_id, project, iid, author_id, title, state, source_branch, target_branch,
                        web_url, created_at, merged_at, updated_at, is_fix, reviewed, synced_at,
                        head_sha, merged_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(mr_id) DO UPDATE SET
            author_id = excluded.author_id, title = excluded.title, state = excluded.state,
            source_branch = excluded.source_branch, target_branch = excluded.target_branch,
            web_url = excluded.web_url, merged_at = excluded.merged_at,
            updated_at = excluded.updated_at, is_fix = excluded.is_fix,
            -- once reviewed, always reviewed: a rerun briefly removes our :eyes:
            reviewed = MAX(mrs.reviewed, excluded.reviewed), synced_at = excluded.synced_at,
            head_sha = COALESCE(excluded.head_sha, mrs.head_sha),
            merged_by = COALESCE(excluded.merged_by, mrs.merged_by)
    """, (mr["id"], project, mr["iid"], author, mr.get("title"), mr.get("state"),
          mr.get("source_branch"), mr.get("target_branch"), mr.get("web_url"),
          db.utc(mr.get("created_at")), db.utc(mr.get("merged_at")), db.utc(mr.get("updated_at")),
          int(is_fix_mr(mr.get("title"), mr.get("source_branch"))), reviewed, now, mr.get("sha"),
          (mr.get("merged_by") or {}).get("id")))
    if (mr.get("merged_by") or {}).get("id"):
        _upsert_person(conn, mr["merged_by"])
    conn.executemany("INSERT INTO mr_notes(note_id, mr_id, author_id, created_at, body) "
                     "VALUES (?, ?, ?, ?, ?) ON CONFLICT(note_id) DO UPDATE SET body = excluded.body",
                     [(nid, mr["id"], a, at, body) for nid, a, at, body in human])

    for note_id, discussion_id, parsed, position, first, status, verdict in found:
        conn.execute("""
            INSERT INTO findings(note_id, discussion_id, mr_id, severity, title, category,
                                 category_source, file, line, body, created_at, status,
                                 appeal_verdict, present, last_seen_at, head_sha)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(note_id) DO UPDATE SET
                severity = excluded.severity, title = excluded.title, body = excluded.body,
                status = excluded.status, appeal_verdict = excluded.appeal_verdict,
                present = 1, last_seen_at = excluded.last_seen_at,
                -- a marker in the comment is authoritative; otherwise keep the classifier's
                category = COALESCE(excluded.category, findings.category),
                category_source = COALESCE(excluded.category_source, findings.category_source),
                head_sha = COALESCE(findings.head_sha, excluded.head_sha)
        """, (note_id, discussion_id, mr["id"], parsed["severity"], parsed["title"],
              parsed["category"], "review" if parsed["category"] else None,
              position.get("new_path"), position.get("new_line"), first.get("body"),
              db.utc(first.get("created_at")), status, verdict, now, position.get("head_sha")))

    seen = [f[0] for f in found]
    # careful: `NOT IN (NULL)` matches nothing, so "every finding gone" needs its own branch
    if seen:
        conn.execute(f"UPDATE findings SET present = 0 WHERE mr_id = ? "
                     f"AND note_id NOT IN ({','.join('?' * len(seen))})", (mr["id"], *seen))
    else:
        conn.execute("UPDATE findings SET present = 0 WHERE mr_id = ?", (mr["id"],))

    if files is not None:
        conn.execute("DELETE FROM mr_files WHERE mr_id = ?", (mr["id"],))
        conn.executemany("INSERT INTO mr_files(mr_id, path) VALUES (?, ?)",
                         [(mr["id"], p) for p in files if not review_common.is_noise_path(p)])
        conn.execute("UPDATE mrs SET files_synced = 1 WHERE mr_id = ?", (mr["id"],))
    if commits is not None:
        # (list, complete) from gitlab_client; a bare list (tests) counts as complete
        commits, complete = commits if isinstance(commits, tuple) else (commits, True)
        conn.executemany("INSERT OR IGNORE INTO mr_commits(mr_id, sha, author_email, author_name) "
                         "VALUES (?, ?, ?, ?)",
                         [(mr["id"], c["id"], (c.get("author_email") or "").lower() or None,
                           c.get("author_name")) for c in commits if c.get("id")])
        # 1 = complete list, 2 = truncated (credit only, never proof of who an email is)
        conn.execute("UPDATE mrs SET commits_synced = ? WHERE mr_id = ?",
                     (1 if complete else 2, mr["id"]))
    return len(found)


def sync_one(conn, base: str, token: str, project: str, mr: dict, me: int) -> int:
    """Fetch and write one MR inside a single write transaction. BEGIN IMMEDIATE
    takes SQLite's write lock *before* reading GitLab, so the scheduled job and a
    reviewer/rerun hook syncing the same MR run one after the other — a slower,
    older snapshot can never land on top of a newer one."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        # re-read the MR itself under the lock too: the copy we were handed may
        # have gone stale while we waited for another writer to finish
        mr = gitlab_client.get_mr(base, token, project, mr["iid"])
        discussions = gitlab_client.list_discussions(base, token, project, mr["iid"])
        awards = gitlab_client.get_award_emojis(base, token, project, mr["iid"])
        files = commits = None
        if mr.get("state") == "merged":
            row = conn.execute("SELECT files_synced, commits_synced FROM mrs WHERE mr_id = ?",
                               (mr["id"],)).fetchone()
            if not (row and row["files_synced"]):
                files = gitlab_client.get_mr_files(base, token, project, mr["iid"])
            # commits prove who an email is (personal MRs only, see score.email_owners)
            # and who took part in a release MR (its reviewed-MR credit, see team_report)
            if not (row and row["commits_synced"]):
                commits = gitlab_client.get_mr_commits(base, token, project, mr["iid"])
        found = store_mr(conn, project, mr, discussions, awards, me, files, commits)
        conn.commit()
        return found
    except BaseException:
        conn.rollback()
        raise


def projects(config: dict) -> list[str]:
    return sorted((config.get("review") or {}).get("project_map") or {})


def _me(config: dict) -> int:
    return gitlab_client.get_current_user(config["gitlab_url"], config["gitlab_token"])["id"]


def sync_project(conn, config: dict, project: str, me: int, full: bool = False) -> int:
    """Incremental by MR updated_at (a new note bumps it); `full` re-reads everything
    touched since `history.since`. Bounded by *update* time, not creation: an MR
    opened before the bot existed but reviewed after must not be skipped."""
    base, token = config["gitlab_url"], config["gitlab_token"]
    since = (config.get("history") or {}).get("since", DEFAULT_SINCE)
    key = f"mrs_updated_at:{project}"
    last = None if full else db.get_state(conn, key)
    after = since
    if last:
        after = db.utc(datetime.fromisoformat(last.replace("Z", "+00:00")) - UPDATED_MARGIN)
    mrs = gitlab_client.list_mrs(base, token, project, updated_after=after)
    for mr in mrs:
        sync_one(conn, base, token, project, mr, me)
    stamps = [db.utc(m.get("updated_at")) for m in mrs if m.get("updated_at")]
    if stamps:                                   # normalised first: compare like with like
        with conn:
            db.set_state(conn, key, max(stamps))
    return len(mrs)


def resolve_roster(conn, config: dict) -> int:
    """People added by hand (dashboard 新增成員) become `people` rows once GitLab
    confirms the username — so they show up before their first MR."""
    rows = conn.execute("SELECT id, username FROM roster_additions "
                        "WHERE resolved_id IS NULL AND error IS NULL").fetchall()
    done = 0
    for row in rows:
        try:
            user = gitlab_client.find_user(config["gitlab_url"], config["gitlab_token"],
                                           row["username"])
        except Exception as exc:
            log.warning("roster lookup of %s failed: %s", row["username"], exc)
            continue                                   # transient: try again next run
        with conn:
            if user:
                _upsert_person(conn, user)
                conn.execute("UPDATE roster_additions SET resolved_id = ? WHERE id = ?",
                             (user["id"], row["id"]))
                done += 1
            else:
                conn.execute("UPDATE roster_additions SET error = ? WHERE id = ?",
                             ("GitLab 上找不到這個帳號", row["id"]))
    return done


def sync_all(conn, config: dict, full: bool = False) -> dict:
    me = _me(config)
    done, failed = {}, {}
    resolve_roster(conn, config)
    for project in projects(config):
        try:
            done[project] = sync_project(conn, config, project, me, full)
        except Exception as exc:                  # one broken project must not stop the rest
            log.exception("history sync failed for %s", project)
            failed[project] = f"{type(exc).__name__}: {exc}"
    with conn:
        db.set_state(conn, "last_sync_at", db.now_iso())
        db.set_state(conn, "last_sync_failed", ",".join(failed) or "")
    return {"synced": done, "failed": failed}


def sync_mr(config: dict, project: str, iid, conn=None, trigger: str | None = None) -> bool:
    """Single-MR sync for the reviewer / listener hooks: right after a review
    posts, and right before a rerun deletes unanswered comments, so even a
    finding that lives for minutes is on record. Never raises; returns whether
    the MR is now on record (the rerun refuses to delete comments otherwise).

    Then the rest of the chain: blame, follow-ups, every score, the change log
    (history.snapshot.refresh) — a failure there is logged but does not undo
    the sync, which is what the caller depends on."""
    try:
        own = conn is None
        conn = conn or db.connect(db.resolve_path(config))
        try:
            base, token = config["gitlab_url"], config["gitlab_token"]
            mr = gitlab_client.get_mr(base, token, project, iid)
            sync_one(conn, base, token, project, mr, _me(config))
            try:
                from history import snapshot
                snapshot.refresh(conn, config, trigger or f"{project.rsplit('/', 1)[-1]}!{iid} 更新",
                                 mr_id=mr["id"])
                with conn:
                    db.set_state(conn, "last_refresh_error", "")
            except Exception as exc:
                # the MR *is* recorded (what the rerun's cleanup depends on); the derived
                # data catches up on the next run — but the failure must be visible
                log.exception("score refresh after %s!%s failed (the MR itself is recorded)",
                              project, iid)
                with conn:
                    db.set_state(conn, "last_refresh_error",
                                 f"{db.now_iso()} {project}!{iid}: {type(exc).__name__}: {exc}"[:300])
            return True
        finally:
            if own:
                conn.close()
    except Exception:
        log.exception("history sync of %s!%s failed", project, iid)
        return False
