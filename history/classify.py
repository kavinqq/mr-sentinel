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
import engines.claude_engine
from history.parse import CATEGORIES
from sentinel_config import SCRIPT_DIR

log = logging.getLogger("mr_sentinel.history")

WORK_DIR = SCRIPT_DIR / "reviews" / "_history"
BATCH = 20
EXCERPT = 1200

NEEDS_REVIEW = "needs_review"      # the model could not tell: a human decides, not scored
# needs evidence the classifier never sees (the MR description): only a review
# with the context, or a human, may file a finding there
BLIND_CATEGORIES = {"requirements"}

PROMPT_HEAD = """You file code-review findings under exactly one category each.

"""
PROMPT_TAIL = """

You do NOT see the MR description, so never answer "requirements" — file the
defect under the category of its consequence instead. If the text does not let
you tell which category applies, answer "needs_review" — never guess.
Reply with ONLY this JSON, one entry per id:
{"categories": [{"id": <id>, "category": "<one of the 8 keys, or needs_review>"}]}

Findings:
"""


def prompt() -> str:
    return PROMPT_HEAD + engines.claude_engine.taxonomy() + PROMPT_TAIL


def excerpt(body: str) -> str:
    """Headline + 問題/後果/修正 rows, then as much of the evidence as fits:
    the category often hinges on the failure path the evidence describes."""
    body = re.sub(r"</?(details|summary)>", "", body or "")
    body = re.sub(r"\n{2,}", "\n", body).strip()
    return body[:EXCERPT]


def valid_categories(reply: dict, ids) -> dict:
    wanted = {str(i) for i in ids}
    out = {}
    for item in (reply or {}).get("categories") or []:
        if isinstance(item, dict) and str(item.get("id")) in wanted \
                and (item.get("category") in CATEGORIES or item.get("category") == NEEDS_REVIEW) \
                and item.get("category") not in BLIND_CATEGORIES:
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
            reply = engine.run_json(prompt() + json.dumps(items, ensure_ascii=False),
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
