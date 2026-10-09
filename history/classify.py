"""Give every recorded finding a category (面向) — the one judgment step here.

New reviews tag their comments themselves (category marker), so this only
fills in findings recorded without one: the backlog from before categories
existed. Batched; each batch is one cheap model call. Output is validated —
an unknown category or a missing id leaves the finding uncategorized for the
next run instead of guessing.
"""
import json
import logging
import re

import engines
from history.parse import CATEGORIES
from sentinel_config import SCRIPT_DIR

log = logging.getLogger("mr_sentinel.history")

WORK_DIR = SCRIPT_DIR / "reviews" / "_history"
BATCH = 20
EXCERPT = 600

PROMPT = """You file code-review findings under exactly one category each.

Categories:
- security: authorization/IDOR, injection, secrets, PII or data leaks, unsafe input handling
- correctness: logic bugs, wrong results, crashes, races, data loss or corruption
- performance: N+1 queries, unbounded loops/queries, needless heavy work
- code_quality: error handling, validation, unhandled edge cases, testability
- code_smell: duplication, dead code, misleading names, design or structure smells

Pick the category of the *consequence* described (a missing permission check that
leaks data is security, not correctness). Reply with ONLY this JSON, one entry per id:
{"categories": [{"id": <id>, "category": "<one of the categories above>"}]}

Findings:
"""


def excerpt(body: str) -> str:
    """Headline + 問題/後果 rows; the <details> evidence is long and not needed."""
    body = (body or "").split("<details>")[0]
    body = re.sub(r"\n{2,}", "\n", body).strip()
    return body[:EXCERPT]


def valid_categories(reply: dict, ids) -> dict:
    wanted = {str(i) for i in ids}
    out = {}
    for item in (reply or {}).get("categories") or []:
        if isinstance(item, dict) and str(item.get("id")) in wanted \
                and item.get("category") in CATEGORIES:
            out[int(item["id"])] = item["category"]
    return out


def classify_pending(conn, config: dict, limit: int = 200) -> tuple[int, int]:
    """Returns (findings classified, batches that failed)."""
    rows = conn.execute("SELECT note_id, title, body FROM findings WHERE category IS NULL "
                        "ORDER BY created_at LIMIT ?", (limit,)).fetchall()
    if not rows:
        return 0, 0
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    review_cfg = config["review"]
    engine = engines.get_engine(review_cfg["engine"])
    done = failed = 0
    for start in range(0, len(rows), BATCH):
        batch = rows[start:start + BATCH]
        items = [{"id": r["note_id"], "finding": excerpt(r["body"])} for r in batch]
        try:
            reply = engine.run_json(PROMPT + json.dumps(items, ensure_ascii=False),
                                    WORK_DIR, review_cfg)
        except Exception:
            log.exception("classifier batch failed; those findings stay uncategorized")
            failed += 1
            continue
        categories = valid_categories(reply, [r["note_id"] for r in batch])
        with conn:
            for note_id, category in categories.items():
                # never overwrite a category the review itself recorded
                done += conn.execute("UPDATE findings SET category = ?, category_source = "
                                     "'classifier' WHERE note_id = ? AND category IS NULL",
                                     (category, note_id)).rowcount
    return done, failed
