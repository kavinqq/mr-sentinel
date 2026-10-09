"""Recompute everyone's score and log what changed — the end of every chain.

    new MR / rerun after a fix / appeal / a human override
      -> db (sync.sync_mr or a human table)
      -> refresh(): blame new findings, recompute follow-ups,
         recompute every person and the team (score.team_report)
      -> compare with score_state; one score_events row per change

The whole team is recomputed every time, not just "the person on this MR": a
finding can move to someone else by blame, and the team average (and with it
nothing else, but the radar) depends on everyone. It is milliseconds.
"""
import logging

from history import blame, db, followups, score

log = logging.getLogger("mr_sentinel.history")


def _team_row(rows: list[dict], cfg: dict) -> dict:
    """The team as one row: pooled over ranked people (same rule as the radar)."""
    ranked = [r for r in rows if r["ranked"] and r["reviewed_mrs"]]
    return {"author_id": db.TEAM, "name": "團隊", "score": score.team_score(rows, cfg),
            "level": None, "reviewed_mrs": sum(r["reviewed_mrs"] for r in ranked),
            "findings": sum(r["findings"] for r in ranked)}


def record(conn, trigger: str, actor: str = "system") -> list[dict]:
    """Recompute and append a score_events row for every changed score / level.
    Returns the events written (empty when nothing moved)."""
    version, cfg = db.scoring_config(conn)
    rows = score.team_report(conn, version, cfg)
    now = db.now_iso()
    state = {r["gitlab_id"]: dict(r) for r in conn.execute("SELECT * FROM score_state")}
    note = db.get_state(conn, "score_note")       # a pending scale change names itself
    if note:
        trigger = f"{note}(由「{trigger}」觸發重算)"
    events = []
    with conn:
        if note:
            conn.execute("DELETE FROM sync_state WHERE key = 'score_note'")
        for r in [*rows, _team_row(rows, cfg)]:
            pid = r["author_id"]
            before = state.get(pid)
            new = (r["score"], r["level"], r["reviewed_mrs"], r["findings"])
            if before and (before["score"], before["level"], before["reviewed_mrs"],
                           before["findings"]) == new:
                continue
            if before is None and r["score"] is None and not r["findings"]:
                continue                          # nothing to report yet
            event = {"gitlab_id": pid, "name": r.get("name") or r.get("username"),
                     "old_score": before and before["score"], "new_score": r["score"],
                     "old_level": before and before["level"], "new_level": r["level"],
                     "old_findings": before and before["findings"], "new_findings": r["findings"]}
            conn.execute("""INSERT INTO score_events(created_at, gitlab_id, name, old_score,
                            new_score, old_level, new_level, old_findings, new_findings,
                            formula_version, trigger, actor)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                         (now, pid, event["name"], event["old_score"], event["new_score"],
                          event["old_level"], event["new_level"], event["old_findings"],
                          event["new_findings"], version, trigger, actor))
            conn.execute("""INSERT INTO score_state(gitlab_id, score, level, reviewed_mrs, findings,
                            updated_at) VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT(gitlab_id) DO UPDATE SET score = excluded.score,
                            level = excluded.level, reviewed_mrs = excluded.reviewed_mrs,
                            findings = excluded.findings, updated_at = excluded.updated_at""",
                         (pid, *new, now))
            events.append(event)
    if events:
        log.info("score changes (%s): %s", trigger,
                 ", ".join(f"{e['name']} {e['old_score']}→{e['new_score']}" for e in events))
    return events


def refresh(conn, config: dict, trigger: str, actor: str = "system",
            mr_id: int | None = None) -> list[dict]:
    """Everything derived after new data landed, then the log. Each step is cheap
    and local (git + sqlite). From a hook (`mr_id`), only that MR's findings are
    blamed — the scheduled run picks up any backlog."""
    blame.blame_pending(conn, config, mr_id=mr_id)
    followups.refresh(conn, db.scoring_config(conn)[1].get("followup_days", 30))
    return record(conn, trigger, actor)
