"""團隊貢獻 — what a team lead contributes besides their own MRs, shown as facts
and never scored or compared (the lead decided that): which of someone else's
MRs they merged and whether a finding was still open then, the release MRs
they owned, their human review notes on others' MRs, and the others' MRs that
carry their commits (taking over / finishing work). The dashboard lists the
MRs behind every number, so a risky merge can be followed up.
"""
from history import score


def facts(conn, pid: int, attributed: dict, followups: list[dict], since: str) -> dict:
    """Counts and the MRs behind them — a lead is not scored or compared."""
    mrs = attributed["mrs"]
    counted = [f for f in attributed["findings"] + attributed["unattributed"]
               if not f["excluded"] and not f["appeal_accepted"]]
    by_mr: dict[int, list] = {}
    for f in counted:
        by_mr.setdefault(f["mr_id"], []).append(f)
    confirmed = {fu["feature_mr_id"] for fu in followups if fu.get("verdict") == "confirmed"}

    def gate(m):
        fs = by_mr.get(m["mr_id"], [])
        escaped = [f for f in fs if f.get("escaped")]
        return {"mr_id": m["mr_id"], "findings": len(fs), "escaped": len(escaped),
                "worst": min((f.get("severity") or "low" for f in escaped),
                             key=lambda s: {"high": 0, "medium": 1, "low": 2}.get(s, 3), default=None),
                "followup": m["mr_id"] in confirmed,
                "outcome": "escaped" if escaped else "cleared" if fs else "clean"}

    recent = [m for m in mrs.values() if (m["created_at"] or "") >= since]
    merges = [gate(m) for m in recent if m.get("merged_by") == pid and m["author_id"] != pid
              and not m.get("release")]
    # a release they opened or merged: they decided it ships
    releases = [gate(m) for m in recent if m.get("release") and m["state"] == "merged"
                and pid in (m["author_id"], m.get("merged_by"))]
    notes = conn.execute("""SELECT n.mr_id, COUNT(*) FROM mr_notes n JOIN mrs m ON m.mr_id = n.mr_id
                            WHERE n.author_id = ? AND m.author_id != ? AND m.created_at >= ?
                            GROUP BY n.mr_id""", (pid, pid, since)).fetchall()
    owners = score.email_owners(conn)
    handover: dict[int, int] = {}
    for r in conn.execute("SELECT mr_id, author_email FROM mr_commits"):
        m = mrs.get(r["mr_id"])
        if m and (m["created_at"] or "") >= since and not m.get("release") and m["author_id"] != pid \
                and owners.get((r["author_email"] or "").lower()) == pid:
            handover[r["mr_id"]] = handover.get(r["mr_id"], 0) + 1

    def tally(rows):
        return {k: sum(1 for r in rows if r["outcome"] == k) for k in ("cleared", "clean", "escaped")}

    return {"merges": merges, "merge_tally": tally(merges),
            "releases": releases, "release_tally": tally(releases),
            "review_mrs": len(notes), "review_notes": sum(r[1] for r in notes),
            "handover": [{"mr_id": k, "commits": v} for k, v in
                         sorted(handover.items(), key=lambda kv: -kv[1])]}
