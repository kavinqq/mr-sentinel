"""SQLite connection + versioned schema migrations (PRAGMA user_version).

The file is created on first connect. Migrations only ever append: to change
the schema, add a new entry to MIGRATIONS — never edit a shipped one, because a
db that already ran it will not run it again.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sentinel_config import SCRIPT_DIR

DEFAULT_PATH = SCRIPT_DIR / "sentinel.db"     # see resolve_path() for the overrides

# Severity weights, the security multiplier and the level thresholds live in
# the db (scoring_configs) so they can be tuned — and every change is a new
# version. This is version 1, seeded by the first migration.
DEFAULT_SCORING = {
    "window_days": 90,               # the evaluation period: the past three months
    "recent_days": 45,               # a level also needs fresh evidence (min_recent_mrs)
    "min_reviewed_mrs": 5,           # own, personal MRs; fewer -> no level ("資料不足")
    "followup_days": 30,             # a fix / new finding this soon after a feature ships counts
    # Each MR is graded 1-5 per category by the model (history/rate.py,
    # prompts/rate.md). A finding it kept caps its category's grade:
    "finding_cap": {"high": 2, "medium": 3, "low": 4},
    # what happened after the review lowers that grade again (floor 1):
    "escape_increment": {"high": 0.75, "medium": 0.5, "low": 0.25},   # still there at merge
    "followup_increment": {"fix_mr": 0.75, "ai_refind": 0.75},        # confirmed by a human only
    # a person's item = (prior_strength × prior_score + Σ grades) / (prior_strength + n):
    # with 3: 3 grades = half the score, 10 grades = 77% — enough that one or two
    # MRs cannot decide an item, not so much that everyone reads as 3
    "prior_strength": 3,
    "prior_score": 3,
    "item_max": 5,
    # without these assessed there is no level, only "資料不足"
    "required_items": ["requirements", "correctness", "verification", "maintainability"],
    # frontend and backend are scored as two tracks (by project path; a project
    # matching no list falls into the track with an empty list). The total is the
    # tracks weighted by graded MRs, plus fullstack_bonus when both tracks have
    # min_reviewed_mrs graded MRs and the weaker one still reaches fullstack_min_score.
    # The bonus is shown, never used for the level (that uses the score before it).
    "tracks": {"frontend": {"label": "前端", "match": ["/frontend/"]},
               "backend": {"label": "後端", "match": []}},
    "fullstack_bonus": 2.0,
    "fullstack_min_score": 25.5,     # the weaker track still at mid
    # total = 8 × mean(assessed items), out of 40. Best level first; every gate must hold.
    # (all 8 items at 3 = 24: mid asks for steadily better than "acceptable")
    "levels": [{"level": "senior", "min_score": 34.0, "min_item": 4.0, "min_coverage": 8,
                "min_mrs": 30, "min_recent_mrs": 2, "max_high": 0, "max_escaped": 0,
                "max_confirmed_followups": 0},
               {"level": "mid+", "min_score": 29.5, "min_item": 3.25, "min_coverage": 7,
                "min_mrs": 15, "min_recent_mrs": 2, "max_high": 0, "max_escaped": 1,
                "max_confirmed_followups": 0},
               {"level": "mid", "min_score": 25.5, "min_item": 2.7, "min_coverage": 6,
                "min_mrs": 5, "min_recent_mrs": 2, "max_high": 1},
               {"level": "junior", "min_score": None}],
}

MIGRATIONS = [
    # 1: initial schema
    """
    CREATE TABLE people (
        gitlab_id   INTEGER PRIMARY KEY,
        username    TEXT NOT NULL,
        name        TEXT
    );
    CREATE TABLE mrs (
        mr_id         INTEGER PRIMARY KEY,          -- GitLab global MR id
        project       TEXT NOT NULL,
        iid           INTEGER NOT NULL,
        author_id     INTEGER REFERENCES people(gitlab_id),
        title         TEXT,
        state         TEXT,
        source_branch TEXT,
        target_branch TEXT,
        web_url       TEXT,
        created_at    TEXT,
        merged_at     TEXT,
        updated_at    TEXT,
        is_fix        INTEGER NOT NULL DEFAULT 0,   -- title/branch says it fixes something
        reviewed      INTEGER NOT NULL DEFAULT 0,   -- our :eyes: claim or any AI finding
        files_synced  INTEGER NOT NULL DEFAULT 0,
        synced_at     TEXT,
        UNIQUE (project, iid)
    );
    CREATE TABLE mr_files (
        mr_id  INTEGER NOT NULL REFERENCES mrs(mr_id),
        path   TEXT NOT NULL,
        PRIMARY KEY (mr_id, path)
    );
    CREATE TABLE findings (
        note_id          INTEGER PRIMARY KEY,       -- the AI comment's GitLab note id
        discussion_id    TEXT,
        mr_id            INTEGER NOT NULL REFERENCES mrs(mr_id),
        severity         TEXT,
        title            TEXT,
        category         TEXT,                      -- as recorded (comment marker or classifier)
        category_source  TEXT,                      -- 'review' | 'classifier'
        file             TEXT,
        line             INTEGER,
        body             TEXT,
        created_at       TEXT,
        status           TEXT,                      -- review_common.discussion_status
        appeal_verdict   TEXT,                      -- our latest appeal reply: 'accept' | 'reject'
        present          INTEGER NOT NULL DEFAULT 1,-- 0 = gone from GitLab (rerun cleanup)
        last_seen_at     TEXT
    );
    CREATE INDEX findings_mr ON findings(mr_id);
    CREATE TABLE followups (
        feature_mr_id  INTEGER NOT NULL REFERENCES mrs(mr_id),
        kind           TEXT NOT NULL,               -- 'fix_mr' | 'ai_refind'
        source_ref     TEXT NOT NULL,               -- fix MR id, or the later finding's note id
        file           TEXT,
        days_after     REAL,
        PRIMARY KEY (feature_mr_id, kind, source_ref)
    );
    CREATE TABLE sync_state (
        key    TEXT PRIMARY KEY,
        value  TEXT
    );

    -- human tables: append-only, written by people (dashboard / CLI)
    CREATE TABLE finding_reviews (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        note_id     INTEGER NOT NULL REFERENCES findings(note_id),
        category    TEXT,                           -- NULL = keep the recorded one
        excluded    INTEGER,                        -- 1 = false positive, 0 = re-include, NULL = unchanged
        reason      TEXT,
        actor       TEXT NOT NULL,
        created_at  TEXT NOT NULL
    );
    CREATE TABLE scoring_configs (
        version     INTEGER PRIMARY KEY,
        config      TEXT NOT NULL,                  -- JSON, see DEFAULT_SCORING
        note        TEXT,
        actor       TEXT NOT NULL,
        created_at  TEXT NOT NULL
    );
    CREATE TABLE sync_requests (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        kind          TEXT NOT NULL,                -- 'sync' | 'full_sync' | 'classify'
        requested_by  TEXT NOT NULL,
        requested_at  TEXT NOT NULL,
        started_at    TEXT,
        finished_at   TEXT,
        result        TEXT
    );
    """,
    # 2: team roles — a lead is shown separately and never ranked (append-only;
    #    the latest row per person wins)
    """
    CREATE TABLE person_roles (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        gitlab_id   INTEGER NOT NULL REFERENCES people(gitlab_id),
        role        TEXT NOT NULL,                  -- 'member' | 'lead'
        actor       TEXT NOT NULL,
        created_at  TEXT NOT NULL
    );
    """,
    # 3: who actually wrote a line — git blame for findings on release MRs
    """
    ALTER TABLE mrs ADD COLUMN head_sha TEXT;
    ALTER TABLE mrs ADD COLUMN commits_synced INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE mr_commits (                       -- commits of personal MRs
        mr_id         INTEGER NOT NULL REFERENCES mrs(mr_id),
        sha           TEXT NOT NULL,
        author_email  TEXT,
        author_name   TEXT,
        PRIMARY KEY (mr_id, sha)
    );
    CREATE INDEX mr_commits_sha ON mr_commits(sha);
    CREATE TABLE finding_blame (
        note_id       INTEGER PRIMARY KEY REFERENCES findings(note_id),
        commit_sha    TEXT,
        author_email  TEXT,
        author_name   TEXT,
        error         TEXT,                         -- why it could not be blamed, if so
        blamed_at     TEXT NOT NULL
    );
    """,
    # 4: confirmed commit-email -> person (append-only, latest per email wins;
    #    gitlab_id NULL = "not one of us / ignore"), and people added by hand
    #    before they ever opened an MR
    """
    ALTER TABLE findings ADD COLUMN head_sha TEXT;   -- the commit the comment was made on
    CREATE TABLE email_aliases (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        email       TEXT NOT NULL,
        gitlab_id   INTEGER REFERENCES people(gitlab_id),
        actor       TEXT NOT NULL,
        created_at  TEXT NOT NULL
    );
    CREATE TABLE roster_additions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        username    TEXT NOT NULL,
        actor       TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        resolved_id INTEGER,                        -- set by the sync once GitLab knows them
        error       TEXT
    );
    """,
    # 5: score change log — every recompute compares with the last known state and
    #    appends one row per person (or the team, gitlab_id -1) whose score changed
    """
    CREATE TABLE score_state (
        gitlab_id     INTEGER PRIMARY KEY,           -- -1 = the team as a whole
        score         REAL,
        level         TEXT,
        reviewed_mrs  INTEGER,
        findings      INTEGER,
        updated_at    TEXT NOT NULL
    );
    CREATE TABLE score_events (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at      TEXT NOT NULL,
        gitlab_id       INTEGER NOT NULL,            -- -1 = the team
        name            TEXT,
        old_score       REAL,
        new_score       REAL,
        old_level       TEXT,
        new_level       TEXT,
        old_findings    INTEGER,
        new_findings    INTEGER,
        formula_version INTEGER,
        trigger         TEXT NOT NULL,               -- what caused the recompute
        actor           TEXT NOT NULL
    );
    CREATE INDEX score_events_person ON score_events(gitlab_id, created_at);
    """,
    # 6: data only — scores become "out of 10, higher is better" (see migrate())
    "SELECT 1;",
    # 7: the 8-way taxonomy (history/parse.py CATEGORIES). Old categories are kept
    # in category_legacy; every classifier-made one is cleared so the classifier
    # files it again under the new rules (scoring uses the legacy mapping meanwhile).
    # followup_reviews: a human confirms or rejects a guessed follow-up bug.
    """
    ALTER TABLE findings ADD COLUMN category_legacy TEXT;
    UPDATE findings SET category_legacy = category;
    UPDATE findings SET category = NULL, category_source = NULL
        WHERE category_source = 'classifier' OR category IS NULL
           OR category NOT IN ('security', 'requirements', 'correctness', 'compatibility',
                               'operability', 'performance', 'verification', 'maintainability');
    CREATE TABLE followup_reviews (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        feature_mr_id  INTEGER NOT NULL,
        kind           TEXT NOT NULL,
        source_ref     TEXT NOT NULL,
        verdict        TEXT NOT NULL,              -- 'confirmed' | 'unrelated'
        reason         TEXT,
        actor          TEXT NOT NULL,
        created_at     TEXT NOT NULL
    );
    CREATE INDEX followup_reviews_key ON followup_reviews(feature_mr_id, kind, source_ref);
    """,
    # 8: AI-written 優點 / 缺點 per person (history/evaluate.py); newest row wins
    """
    CREATE TABLE person_evaluations (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        gitlab_id        INTEGER NOT NULL,
        created_at       TEXT NOT NULL,
        formula_version  INTEGER,
        input_hash       TEXT NOT NULL,           -- the record it was written from
        summary          TEXT NOT NULL,
        strengths        TEXT NOT NULL,           -- JSON [{point, evidence}]
        weaknesses       TEXT NOT NULL,           -- JSON [{point, evidence, advice}]
        engine           TEXT,
        trigger          TEXT
    );
    CREATE INDEX person_evaluations_person ON person_evaluations(gitlab_id, created_at);
    """,
    # 9: per-MR scorecard — the AI rates every MR 1–5 per category (NULL = not
    # applicable), so a 5 is earned, not "nothing found" (history/rate.py).
    # Append-only; the newest rating per (mr, category) wins.
    """
    CREATE TABLE mr_ratings (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        mr_id      INTEGER NOT NULL REFERENCES mrs(mr_id),
        category   TEXT NOT NULL,
        score      INTEGER,                      -- 1..5, NULL = not applicable
        reason     TEXT,
        evidence   TEXT,
        head_sha   TEXT,                         -- the commit that was rated
        source     TEXT NOT NULL,                -- 'review' | 'backfill'
        engine     TEXT,
        rated_at   TEXT NOT NULL
    );
    CREATE INDEX mr_ratings_mr ON mr_ratings(mr_id, category, rated_at);
    """,
    # 10: which rubric a rating used (a new rubric re-rates); scoring from ratings
    # instead of "5 minus findings" (see migrate())
    "ALTER TABLE mr_ratings ADD COLUMN rubric_version INTEGER;",
    # 11: a rating of one person's own commits inside a release MR (NULL = the
    # whole MR); front-end / back-end tracks in the config (see migrate())
    "ALTER TABLE mr_ratings ADD COLUMN author_id INTEGER;",
    # 12: what a team lead contributes besides their own MRs (history/contrib.py):
    # who merged an MR, the human notes on it, and AI grades of a person's review
    # comments on someone else's MR
    """
    ALTER TABLE mrs ADD COLUMN merged_by INTEGER;
    CREATE TABLE mr_notes (
        note_id     INTEGER PRIMARY KEY,
        mr_id       INTEGER NOT NULL REFERENCES mrs(mr_id),
        author_id   INTEGER,
        created_at  TEXT,
        body        TEXT
    );
    CREATE INDEX mr_notes_author ON mr_notes(author_id, mr_id);
    CREATE TABLE review_ratings (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        mr_id      INTEGER NOT NULL REFERENCES mrs(mr_id),
        author_id  INTEGER NOT NULL,              -- the reviewer
        notes      INTEGER NOT NULL,              -- how many of their notes it read
        score      INTEGER,                       -- 1..5; NULL = nothing to judge
        reason     TEXT,
        engine     TEXT,
        rated_at   TEXT NOT NULL
    );
    CREATE INDEX review_ratings_mr ON review_ratings(mr_id, author_id, rated_at);
    """,
    # 13: work outside the bot-reviewed projects and outside MRs (history/discover.py):
    # every non-merge commit of the window in a project the team commits to, and
    # grades of a person's direct commits batched per project and ISO week
    """
    CREATE TABLE project_commits (
        sha           TEXT NOT NULL,
        project       TEXT NOT NULL,
        author_email  TEXT,
        author_name   TEXT,
        title         TEXT,
        created_at    TEXT,
        PRIMARY KEY (project, sha)
    );
    CREATE INDEX project_commits_author ON project_commits(author_email, created_at);
    CREATE TABLE commit_ratings (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        batch           TEXT NOT NULL,            -- project|author_id|ISO week
        project         TEXT NOT NULL,
        author_id       INTEGER NOT NULL,
        week            TEXT NOT NULL,
        shas            TEXT NOT NULL,            -- JSON list it was graded from
        category        TEXT NOT NULL,
        score           INTEGER,
        reason          TEXT,
        evidence        TEXT,
        engine          TEXT,
        rated_at        TEXT NOT NULL,
        rubric_version  INTEGER
    );
    CREATE INDEX commit_ratings_batch ON commit_ratings(batch, category, rated_at);
    """,
    # 14: 個人軌跡 (history/trajectory.py): every judgment of a metric's change, keyed
    # by the evidence it saw (a re-sync of the same data is not a second judgment),
    # and the alerts those judgments opened / closed
    """
    CREATE TABLE trajectory_judgments (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        person_id   INTEGER NOT NULL,
        metric      TEXT NOT NULL,
        as_of       TEXT NOT NULL,
        evidence    TEXT NOT NULL,              -- hash of the samples it was computed from
        n_recent    INTEGER, n_base INTEGER,
        recent      REAL, base REAL,
        p_better    REAL, p_worse REAL,
        state       TEXT NOT NULL               -- insufficient|stable|up|down|observe_*|strong_*
    );
    CREATE INDEX trajectory_judgments_key ON trajectory_judgments(person_id, metric, as_of);
    CREATE TABLE trajectory_alerts (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        person_id   INTEGER NOT NULL,
        metric      TEXT NOT NULL,
        kind        TEXT NOT NULL,              -- 'watch' (要關注) | 'improve' (進步很多)
        opened_at   TEXT NOT NULL,
        closed_at   TEXT,
        closed_by   TEXT,                       -- 'data' | a dashboard user (已檢視)
        summary     TEXT
    );
    CREATE INDEX trajectory_alerts_person ON trajectory_alerts(person_id, metric, opened_at);
    """,
    # 15: (Codex review of 個人軌跡) a judgment keeps the observations it saw, so
    # "new evidence" is a set difference, not a count; half-threshold probabilities
    # for the close rule; 已檢視 is separate from 結束追蹤; one active alert per metric
    """
    ALTER TABLE trajectory_judgments ADD COLUMN obs TEXT;
    ALTER TABLE trajectory_judgments ADD COLUMN p_better_half REAL;
    ALTER TABLE trajectory_judgments ADD COLUMN p_worse_half REAL;
    ALTER TABLE trajectory_judgments ADD COLUMN eligible INTEGER;
    ALTER TABLE trajectory_alerts ADD COLUMN acknowledged_at TEXT;
    ALTER TABLE trajectory_alerts ADD COLUMN acknowledged_by TEXT;
    ALTER TABLE trajectory_alerts ADD COLUMN note TEXT;
    ALTER TABLE trajectory_alerts ADD COLUMN close_reason TEXT;
    ALTER TABLE trajectory_alerts ADD COLUMN snapshot TEXT;
    ALTER TABLE trajectory_alerts ADD COLUMN stale INTEGER DEFAULT 0;
    CREATE UNIQUE INDEX trajectory_alerts_active ON trajectory_alerts(person_id, metric)
        WHERE closed_at IS NULL;
    """,
]
SCALE_CHANGE_NOTE = "評分改成 10 分制(越高越好、每項各自給分)"
TAXONOMY_CHANGE_NOTE = "評分改成 8 個面向、每項 5 分(滿分 40)"
RATING_CHANGE_NOTE = "評分改成每個 MR 逐項打分(5 分要掙來,沒評過的顯示未評估)"
TRACK_CHANGE_NOTE = "評分分成前端 / 後端兩條,兩邊都達標另有全端加分;release 依 commit 作者打分"
SCALE_NOTES = (SCALE_CHANGE_NOTE, TAXONOMY_CHANGE_NOTE, RATING_CHANGE_NOTE)
TEAM = -1

ROLES = {"member": "成員", "lead": "Team leader", "departed": "已離職", "external": "非成員"}
UNRANKED_ROLES = {"lead", "departed", "external"}   # never ranked, not in the team average
# not part of the team being evaluated: no grading, no evaluation, no score log
# (an "external" person is from another team, here only to help)
NOT_EVALUATED = {"departed", "external"}


def confirmed_aliases(conn) -> dict[str, int | None]:
    """email -> gitlab_id confirmed by a person (latest wins; None = ignore)."""
    out = {}
    for row in conn.execute("SELECT email, gitlab_id FROM email_aliases ORDER BY created_at, id"):
        out[row["email"].lower()] = row["gitlab_id"]
    return out


def person_roles(conn) -> dict[int, str]:
    """Current role per person (latest row wins); anyone missing is a member."""
    roles = {}
    for row in conn.execute("SELECT gitlab_id, role FROM person_roles ORDER BY created_at, id"):
        roles[row["gitlab_id"]] = row["role"]
    return roles


def now_iso() -> str:
    return utc(datetime.now(timezone.utc))


def utc(value) -> str | None:
    """Every timestamp is stored as 'YYYY-MM-DDTHH:MM:SSZ' (UTC), so SQLite's text
    comparison orders them correctly whatever offset GitLab or Python produced."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_path(config: dict | None = None) -> Path:
    """MR_SENTINEL_DB > config history.db_path > <repo>/sentinel.db — the same order
    for the CLI, the review hooks and the dashboard, so they all open one file."""
    env = os.environ.get("MR_SENTINEL_DB")
    configured = ((config or {}).get("history") or {}).get("db_path")
    path = Path(env or configured or SCRIPT_DIR / "sentinel.db")
    # relative paths are relative to the repo, never to whoever's cwd
    return path if path.is_absolute() else SCRIPT_DIR / path


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open (creating if needed) and migrate. WAL lets the dashboard read while a
    sync writes; busy_timeout covers the reviewer/listener writing at the same time."""
    path = Path(path or resolve_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
        if number == 15:
            _one_active_alert(conn)
        with conn:
            conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
        if number == 1:
            with conn:
                conn.execute("INSERT INTO scoring_configs VALUES (1, ?, ?, ?, ?)",
                             (json.dumps(DEFAULT_SCORING), "initial defaults — to be calibrated",
                              "system", now_iso()))
        if number == 6:
            _to_ten_point_scale(conn)
        if number == 7:
            _to_eight_items(conn)
        if number == 10:
            _to_ratings(conn)
        if number == 11:
            _add_tracks(conn)
    return len(MIGRATIONS)


def _one_active_alert(conn) -> None:
    """Before migration 15's unique index: keep the newest active alert per
    person / metric and close the rest (v14 has no close_reason yet, so the
    reason goes in closed_by), so the index can build."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'trajectory_alerts'").fetchone():
        return
    with conn:
        conn.execute("""UPDATE trajectory_alerts SET closed_at = opened_at, closed_by = 'data:升級時合併重複的提醒'
                        WHERE closed_at IS NULL AND id NOT IN (
                            SELECT MAX(id) FROM trajectory_alerts WHERE closed_at IS NULL
                            GROUP BY person_id, metric)""")


def _to_ten_point_scale(conn) -> None:
    """Migration 6 once gave a 10-point successor config; superseded by 10
    (rating-based), which writes the one config a db upgraded today needs."""


def _to_eight_items(conn) -> None:
    """Migration 7's 8 × 5 successor config; superseded by 10 like 6."""


def _to_ratings(conn) -> None:
    """Successor config for rating-based scoring; the window and follow-up days
    a human tuned are kept, the rest has no equivalent in the old formula."""
    version, cfg = scoring_config(conn)
    if "finding_cap" in cfg:
        return
    keep = ("window_days", "min_reviewed_mrs", "followup_days")
    new = {**DEFAULT_SCORING, **{k: cfg[k] for k in keep if k in cfg}}
    with conn:
        conn.execute("INSERT INTO scoring_configs VALUES (?, ?, ?, ?, ?)",
                     (version + 1, json.dumps(new, ensure_ascii=False), RATING_CHANGE_NOTE,
                      "system", now_iso()))
        conn.execute("INSERT OR REPLACE INTO sync_state(key, value) VALUES ('score_note', ?)",
                     (RATING_CHANGE_NOTE,))


def _add_tracks(conn) -> None:
    """Same formula plus the two tracks; everything a human tuned is kept."""
    version, cfg = scoring_config(conn)
    if "tracks" in cfg:
        return
    new = {**cfg, **{k: DEFAULT_SCORING[k] for k in ("tracks", "fullstack_bonus",
                                                      "fullstack_min_score")}}
    with conn:
        conn.execute("INSERT INTO scoring_configs VALUES (?, ?, ?, ?, ?)",
                     (version + 1, json.dumps(new, ensure_ascii=False), TRACK_CHANGE_NOTE,
                      "system", now_iso()))
        conn.execute("INSERT OR REPLACE INTO sync_state(key, value) VALUES ('score_note', ?)",
                     (TRACK_CHANGE_NOTE,))


def export(conn, path) -> dict:
    """A single, consistent file (no -wal / -shm) to carry to another machine:
    SQLite's online backup, safe while the scheduler or the dashboard writes."""
    from pathlib import Path
    target = Path(path)
    if target.exists():
        raise FileExistsError(f"{target} already exists — pick a new name")
    dst = sqlite3.connect(target)
    try:
        conn.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
        counts = {t: dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("mrs", "findings", "mr_ratings", "commit_ratings", "person_evaluations",
                            "score_events", "email_aliases", "person_roles")}
        version = dst.execute("PRAGMA user_version").fetchone()[0]
    finally:
        dst.close()
    return {"path": str(target), "schema": version, "rows": counts,
            "bytes": target.stat().st_size}


def get_state(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn, key: str, value) -> None:
    conn.execute("INSERT INTO sync_state(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


def scoring_config(conn) -> tuple[int, dict]:
    row = conn.execute("SELECT version, config FROM scoring_configs "
                       "ORDER BY version DESC LIMIT 1").fetchone()
    return row["version"], json.loads(row["config"])
