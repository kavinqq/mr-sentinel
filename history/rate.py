"""The per-MR scorecard: the model grades one MR 1–5 on each of the 8
categories (prompts/rate.md), so a 5 has to be earned with evidence instead of
meaning "the review found nothing". Scoring (history/score.py) caps each grade
by the findings that MR kept and averages the grades per person.

One tool-less model call per MR, from the same context a review reads (diff +
MR description) plus the findings on record. Called right after a review
(reviewer.py, via `rate_mr`) and by the scheduled job for MRs not rated yet —
the back-fill of history is the same code path. Release / integration MRs are
never rated: their diff is everyone's work.

The reply is validated mechanically: all 8 keys, integer 1–5 or null, an N/A
needs a reason, and a grade the evidence cannot carry is lowered (5 needs two
pieces, 4 needs one; a truncated diff can never earn 5).
"""
import json
import logging

import engines
import engines.claude_engine
import fetch_mr
import gitlab_client
from history import db, score
from history.parse import CATEGORIES, is_release_mr
from sentinel_config import SCRIPT_DIR

log = logging.getLogger("mr_sentinel.history")

RUBRIC_VERSION = 1
WORK_DIR = SCRIPT_DIR / "reviews" / "_history"
DIFF_MAX = 120_000
TEXT_MAX = 400
EVIDENCE_NEEDED = {5: 2, 4: 1}


def _clean(text) -> str:
    return " ".join(str(text or "").split())[:TEXT_MAX]


def valid_ratings(reply: dict, truncated: bool = False) -> dict | None:
    """{category: {score, reason, evidence}} or None when the reply is unusable.
    A grade is lowered to what its evidence carries; a missing key fails all."""
    ratings = (reply or {}).get("ratings") if isinstance(reply, dict) else None
    if not isinstance(ratings, dict) or set(ratings) != set(CATEGORIES):
        return None
    out = {}
    for cat in CATEGORIES:
        r = ratings[cat]
        if not isinstance(r, dict):
            return None
        s = r.get("score")
        if s is not None and (isinstance(s, bool) or not isinstance(s, int) or not 1 <= s <= 5):
            return None
        reason = _clean(r.get("reason"))
        if s is None and not reason:
            return None                          # an N/A must say why
        evidence = [{"ref": _clean(e.get("ref")), "claim": _clean(e.get("claim"))}
                    for e in (r.get("evidence") or []) if isinstance(e, dict) and _clean(e.get("ref"))]
        if s is not None:
            if truncated:
                s = min(s, 4)
            while s in EVIDENCE_NEEDED and len(evidence) < EVIDENCE_NEEDED[s]:
                s -= 1                           # a 5 without two references is not a 5
        out[cat] = {"score": s, "reason": reason, "evidence": evidence[:5]}
    return out


def build_input(ctx: dict, findings: list[dict]) -> tuple[dict, bool]:
    """What the model sees: title, description, the (possibly cut) diff, findings."""
    diff, size = [], 0
    truncated = False
    for c in ctx.get("changes") or []:
        chunk = f"--- {c.get('old_path')}\n+++ {c.get('new_path')}\n{c.get('diff') or ''}"
        if size + len(chunk) > DIFF_MAX:
            truncated = True
            break
        diff.append(chunk)
        size += len(chunk)
    return {"title": ctx.get("title"), "description": ctx.get("description") or "",
            "source_branch": ctx.get("source_branch"), "target_branch": ctx.get("target_branch"),
            "stats": ctx.get("stats"), "diff_truncated": truncated,
            "findings": [{"severity": f.get("severity"), "category": f.get("category"),
                          "title": f.get("title"), "file": f.get("file")} for f in findings],
            "diff": "\n".join(diff)}, truncated


def store(conn, mr_id: int, ratings: dict, head_sha: str | None, source: str, engine: str,
          author_id: int | None = None) -> None:
    now = db.now_iso()
    with conn:
        conn.executemany(
            "INSERT INTO mr_ratings(mr_id, category, score, reason, evidence, head_sha, source, "
            "engine, rated_at, rubric_version, author_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(mr_id, cat, r["score"], r["reason"], json.dumps(r["evidence"], ensure_ascii=False),
              head_sha, source, engine, now, RUBRIC_VERSION, author_id)
             for cat, r in ratings.items()])


def latest(conn) -> dict[int, dict[str, dict]]:
    """mr_id -> category -> the newest rating (any head; a newer review wins)."""
    out: dict[int, dict] = {}
    for r in conn.execute("SELECT * FROM mr_ratings WHERE author_id IS NULL ORDER BY rated_at, id"):
        out.setdefault(r["mr_id"], {})[r["category"]] = dict(r)
    return out


def _findings_of(conn, mr_id: int) -> list[dict]:
    rows = [dict(r) for r in conn.execute("SELECT * FROM findings WHERE mr_id = ?", (mr_id,))]
    reviews = [dict(r) for r in conn.execute(
        "SELECT * FROM finding_reviews WHERE note_id IN (SELECT note_id FROM findings WHERE mr_id = ?)",
        (mr_id,))]
    return [f for f in score.effective_findings(rows, reviews)
            if not f["excluded"] and not f["appeal_accepted"]]


def rate_one(conn, config: dict, mr: dict, ctx: dict | None = None,
             source: str = "backfill") -> bool:
    """Rate one MR row (mrs table). Fetches its context from GitLab unless given."""
    review_cfg = config["review"]
    if ctx is None:
        ctx = fetch_mr.build_context(config["gitlab_url"], config["gitlab_token"],
                                     mr["project"], mr["iid"])
    payload, truncated = build_input(ctx, _findings_of(conn, mr["mr_id"]))
    template = (SCRIPT_DIR / "prompts" / "rate.md").read_text()
    prompt = template.replace("__LANGUAGE__", engines.claude_engine.language_name(
        review_cfg.get("language", "zh-TW"))) + json.dumps(payload, ensure_ascii=False)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    reply = engines.get_engine(review_cfg["engine"]).run_json(prompt, WORK_DIR, review_cfg)
    ratings = valid_ratings(reply, truncated)
    if ratings is None:
        log.warning("rating of %s!%s unusable; left unrated", mr["project"], mr["iid"])
        return False
    head = ((ctx.get("diff_refs") or {}).get("head_sha")) or mr.get("head_sha")
    store(conn, mr["mr_id"], ratings, head, source, review_cfg["engine"])
    return True


def pending(conn, since: str) -> list[dict]:
    """Personal (non-release) MRs in the window with no rating for their current
    head — reviewed by the bot, or merged in a project it does not review —
    newest first, so the scores that matter fill in first."""
    rated = {(r["mr_id"], r["head_sha"]) for r in conn.execute(
        "SELECT DISTINCT mr_id, head_sha FROM mr_ratings WHERE rubric_version = ? "
        "AND author_id IS NULL", (RUBRIC_VERSION,))}
    out = []
    for r in conn.execute("SELECT * FROM mrs WHERE (reviewed = 1 OR state = 'merged') "
                          "AND created_at >= ? ORDER BY created_at DESC", (since,)):
        m = dict(r)
        if is_release_mr(m["title"], m["source_branch"], m["target_branch"]):
            continue
        if (m["mr_id"], m["head_sha"]) not in rated:
            out.append(m)
    return out


NO_OWN_COMMITS = "這個 release MR 裡沒有他自己的非 merge commit(都已在他的個人 MR 評過)"


def pending_slices(conn, since: str) -> list[dict]:
    """(release MR, person, their shas) still to grade: commits of theirs in a
    reviewed release MR of the window that no personal MR already carried — so
    the same code is never graded twice."""
    owners = score.email_owners(conn)
    in_personal = set()
    release = {}
    for r in conn.execute("SELECT * FROM mrs WHERE reviewed = 1"):
        m = dict(r)
        if is_release_mr(m["title"], m["source_branch"], m["target_branch"]):
            if (m["created_at"] or "") >= since:
                release[m["mr_id"]] = m
    for r in conn.execute("SELECT c.mr_id, c.sha FROM mr_commits c"):
        if r["mr_id"] not in release:
            in_personal.add(r["sha"])
    done = {(r["mr_id"], r["author_id"]) for r in conn.execute(
        "SELECT DISTINCT mr_id, author_id FROM mr_ratings WHERE author_id IS NOT NULL "
        "AND rubric_version = ?", (RUBRIC_VERSION,))}
    by: dict[tuple, list] = {}
    for r in conn.execute("SELECT mr_id, sha, author_email FROM mr_commits"):
        if r["mr_id"] not in release or r["sha"] in in_personal:
            continue
        pid = owners.get((r["author_email"] or "").lower())
        if pid is not None and (r["mr_id"], pid) not in done:
            by.setdefault((r["mr_id"], pid), []).append(r["sha"])
    return [{**release[mid], "author_id": pid, "shas": shas}
            for (mid, pid), shas in sorted(by.items(), key=lambda kv: release[kv[0][0]]["created_at"] or "",
                                           reverse=True)]


def _slice_findings(conn, mr_id: int, pid: int) -> list[dict]:
    """Findings on the release MR that git blame put on this person."""
    owners = score.email_owners(conn)
    blamed = {r["note_id"] for r in conn.execute(
        "SELECT note_id, author_email FROM finding_blame WHERE error IS NULL")
        if owners.get((r["author_email"] or "").lower()) == pid}
    return [f for f in _findings_of(conn, mr_id) if f["note_id"] in blamed]


def rate_slice(conn, config: dict, s: dict) -> bool:
    """Grade one person's own (non-merge) commits inside a release MR."""
    base, token = config["gitlab_url"], config["gitlab_token"]
    review_cfg = config["review"]
    titles, changes = [], []
    for sha in s["shas"]:
        commit = gitlab_client.get_commit(base, token, s["project"], sha)
        if len(commit.get("parent_ids") or []) > 1:
            continue                                   # a merge carries other work
        titles.append(commit.get("title") or "")
        changes += gitlab_client.get_commit_diff(base, token, s["project"], sha)
    if not changes:
        store(conn, s["mr_id"], {c: {"score": None, "reason": NO_OWN_COMMITS, "evidence": []}
                                 for c in CATEGORIES}, s.get("head_sha"), "backfill",
              review_cfg["engine"], s["author_id"])
        return True
    name = (conn.execute("SELECT name, username FROM people WHERE gitlab_id = ?",
                         (s["author_id"],)).fetchone() or {"name": "", "username": ""})
    ctx = {"title": f"{s['title']} — {name['name'] or name['username']} 自己的 commit",
           "description": ("這是 release MR 裡屬於這位成員的 commit(個人 MR 已評過的不在內);"
                           "沒有獨立的 MR description,commit 訊息如下:\n- " + "\n- ".join(titles)),
           "source_branch": s["source_branch"], "target_branch": s["target_branch"],
           "changes": changes, "stats": {"files": len(changes), "commits": len(titles)}}
    payload, truncated = build_input(ctx, _slice_findings(conn, s["mr_id"], s["author_id"]))
    template = (SCRIPT_DIR / "prompts" / "rate.md").read_text()
    prompt = template.replace("__LANGUAGE__", engines.claude_engine.language_name(
        review_cfg.get("language", "zh-TW"))) + json.dumps(payload, ensure_ascii=False)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    ratings = valid_ratings(engines.get_engine(review_cfg["engine"]).run_json(
        prompt, WORK_DIR, review_cfg), truncated)
    if ratings is None:
        return False
    store(conn, s["mr_id"], ratings, s.get("head_sha"), "backfill", review_cfg["engine"],
          s["author_id"])
    return True


def _week(ts: str) -> str:
    from datetime import datetime
    d = datetime.fromisoformat((ts or "1970-01-01T00:00:00Z").replace("Z", "+00:00"))
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def pending_commit_batches(conn, since: str) -> list[dict]:
    """A person's commits that never went through an MR, batched per project and
    ISO week — the unit graded in place of an MR. A batch is graded again only
    when new commits joined it."""
    owners = score.email_owners(conn)
    in_mr = {r[0] for r in conn.execute("SELECT sha FROM mr_commits")}
    graded = {}
    for r in conn.execute("SELECT batch, shas FROM commit_ratings WHERE rubric_version = ? "
                          "ORDER BY rated_at, id", (RUBRIC_VERSION,)):
        graded[r["batch"]] = r["shas"]
    batches: dict[str, dict] = {}
    for r in conn.execute("SELECT * FROM project_commits WHERE created_at >= ? ORDER BY created_at",
                          (since,)):
        pid = owners.get((r["author_email"] or "").lower())
        if pid is None or r["sha"] in in_mr:
            continue
        week = _week(r["created_at"])
        key = f"{r['project']}|{pid}|{week}"
        b = batches.setdefault(key, {"batch": key, "project": r["project"], "author_id": pid,
                                     "week": week, "shas": [], "titles": [],
                                     "created_at": r["created_at"], "iid": week})
        b["shas"].append(r["sha"])
        b["titles"].append(r["title"] or "")
    return [b for b in sorted(batches.values(), key=lambda b: b["created_at"], reverse=True)
            if graded.get(b["batch"]) != json.dumps(sorted(b["shas"]))]


def rate_commit_batch(conn, config: dict, b: dict) -> bool:
    base, token = config["gitlab_url"], config["gitlab_token"]
    review_cfg = config["review"]
    changes = []
    for sha in b["shas"]:
        changes += gitlab_client.get_commit_diff(base, token, b["project"], sha)
    name = conn.execute("SELECT name, username FROM people WHERE gitlab_id = ?",
                        (b["author_id"],)).fetchone()
    who = (name["name"] or name["username"]) if name else str(b["author_id"])
    ctx = {"title": f"{b['project'].rsplit('/', 1)[-1]} · {b['week']} — {who} 直接推上去的 commit(沒有 MR)",
           "description": ("這批 commit 沒有走 MR,所以沒有 MR description 也沒有 review;"
                           "commit 訊息如下:\n- " + "\n- ".join(b["titles"])),
           "changes": changes, "stats": {"files": len(changes), "commits": len(b["shas"])}}
    payload, truncated = build_input(ctx, [])
    template = (SCRIPT_DIR / "prompts" / "rate.md").read_text()
    prompt = template.replace("__LANGUAGE__", engines.claude_engine.language_name(
        review_cfg.get("language", "zh-TW"))) + json.dumps(payload, ensure_ascii=False)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    ratings = valid_ratings(engines.get_engine(review_cfg["engine"]).run_json(
        prompt, WORK_DIR, review_cfg), truncated) if changes else None
    if ratings is None:
        return False
    now, shas = db.now_iso(), json.dumps(sorted(b["shas"]))
    with conn:
        conn.executemany(
            "INSERT INTO commit_ratings(batch, project, author_id, week, shas, category, score, "
            "reason, evidence, engine, rated_at, rubric_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(b["batch"], b["project"], b["author_id"], b["week"], shas, cat, r["score"], r["reason"],
              json.dumps(r["evidence"], ensure_ascii=False), review_cfg["engine"], now, RUBRIC_VERSION)
             for cat, r in ratings.items()])
    return True


def _skip_authors(conn) -> set[int]:
    """People not being evaluated: their work is not graded at all."""
    return {pid for pid, role in db.person_roles(conn).items() if role == "external"}


def rate_pending(conn, config: dict, limit: int = 40, progress=lambda msg: None,
                 workers: int = 1) -> tuple[int, int]:
    """(rated, failed): whole personal MRs first, then people's slices of release
    MRs, then batches of direct commits. Bounded per run; a failure leaves the
    item for next time. `workers` > 1 grades in parallel, each on its own
    connection (every item is graded once; writes are short transactions)."""
    version, cfg = db.scoring_config(conn)
    since = score.window_start(cfg)
    todo = [("mr", m) for m in pending(conn, since)]
    todo += [("slice", s) for s in pending_slices(conn, since)]
    todo += [("commits", b) for b in pending_commit_batches(conn, since)]
    skip = _skip_authors(conn)
    todo = [(k, i) for k, i in todo if i.get("author_id") not in skip][:limit]
    done = failed = 0

    def grade(kind, item, c):
        try:
            return (rate_one(c, config, item) if kind == "mr" else
                    rate_slice(c, config, item) if kind == "slice" else
                    rate_commit_batch(c, config, item))
        except Exception:
            log.exception("rating %s!%s failed", item["project"], item["iid"])
            return False

    if workers > 1:
        import threading
        from concurrent.futures import ThreadPoolExecutor
        local, path = threading.local(), conn.execute("PRAGMA database_list").fetchone()[2]

        def run(job):
            if not hasattr(local, "conn"):
                local.conn = db.connect(path)
            return job, grade(*job, local.conn)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i, ((kind, item), ok) in enumerate(pool.map(run, todo), 1):
                done, failed = done + ok, failed + (not ok)
                progress(f"    評分 {i}/{len(todo)} {item['project'].rsplit('/', 1)[-1]}!{item['iid']}"
                         + ("" if ok else " 失敗"))
        return done, failed
    for i, (kind, item) in enumerate(todo, 1):
        ok = grade(kind, item, conn)
        done, failed = done + ok, failed + (not ok)
        what = ("" if kind == "mr" else f"(release 中 #{item['author_id']} 的 commit)"
                if kind == "slice" else f"(#{item['author_id']} 直接 commit {len(item['shas'])} 個)")
        progress(f"    評分 {i}/{len(todo)} {item['project'].rsplit('/', 1)[-1]}!{item['iid']}{what}"
                 + ("" if ok else " 失敗"))
    return done, failed


def rate_mr(config: dict, project: str, iid, ctx: dict | None = None) -> bool:
    """Reviewer hook, right after the review is on record: never raises."""
    try:
        conn = db.connect(db.resolve_path(config))
        try:
            row = conn.execute("SELECT * FROM mrs WHERE project = ? AND iid = ?",
                               (project, int(iid))).fetchone()
            if row is None or is_release_mr(row["title"], row["source_branch"], row["target_branch"]):
                return False
            ok = rate_one(conn, config, dict(row), ctx=ctx, source="review")
            if ok:
                from history import snapshot
                snapshot.record(conn, f"MR 評分 {project.rsplit('/', 1)[-1]}!{iid}")
            return ok
        finally:
            conn.close()
    except Exception:
        log.exception("rating of %s!%s failed", project, iid)
        return False
