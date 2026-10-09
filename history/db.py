"""SQLite connection + versioned schema migrations (PRAGMA user_version).

The file is created on first connect. Migrations only ever append: to change
the schema, add a new entry to MIGRATIONS — never edit a shipped one, because a
db that already ran it will not run it again.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sentinel_config import SCRIPT_DIR

DEFAULT_PATH = SCRIPT_DIR / "sentinel.db"

# Severity weights, the security multiplier and the level thresholds live in
# the db (scoring_configs) so they can be tuned — and every change is a new
# version. This is version 1, seeded by the first migration.
DEFAULT_SCORING = {
    "window_days": 90,
    "min_reviewed_mrs": 5,
    "severity_weight": {"high": 5.0, "medium": 2.0, "low": 0.5},
    "category_multiplier": {"security": 1.5},
    "followup_weight": {"fix_mr": 3.0, "ai_refind": 1.0},
    "followup_days": 30,             # a fix / new finding this soon after a feature ships counts
    # score = weighted problems per reviewed MR; lower is better
    "levels": [{"level": "senior", "max_score": 0.8},
               {"level": "mid", "max_score": 2.5},
               {"level": "junior", "max_score": None}],
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
]


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


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open (creating if needed) and migrate. WAL lets the dashboard read while a
    sync writes; busy_timeout covers the reviewer/listener writing at the same time."""
    path = Path(path or DEFAULT_PATH)
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
        with conn:
            conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
        if number == 1:
            with conn:
                conn.execute("INSERT INTO scoring_configs VALUES (1, ?, ?, ?, ?)",
                             (json.dumps(DEFAULT_SCORING), "initial defaults — to be calibrated",
                              "system", now_iso()))
    return len(MIGRATIONS)


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
