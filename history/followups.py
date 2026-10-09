"""Follow-up bugs: did a shipped feature need fixing soon after?

Two signals, recorded separately because they mean different things:
- fix_mr    — a merged fix-type MR touched the same file within N days;
- ai_refind — the AI flagged that file again (in a later MR) within N days.

Each event is pinned on the *most recent* earlier feature MR that touched the
file — the change most likely responsible — rather than on everyone who ever
edited it. Recomputed from scratch on every run, so tuning `followup_days`
(scoring config) takes effect immediately. Heuristic by nature; reports label it so.
"""
from datetime import datetime


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def compute(features: list[dict], fixes: list[dict], findings: list[dict], days: float) -> list[dict]:
    """features: merged non-fix MRs {mr_id, project, merged_at, files}
    fixes:    merged fix MRs       {mr_id, project, merged_at, files}
    findings: AI findings           {note_id, mr_id, project, file, created_at}
    -> [{feature_mr_id, kind, source_ref, file, days_after}]"""
    by_file: dict[tuple[str, str], list[tuple[datetime, int]]] = {}
    for f in features:
        shipped = _ts(f["merged_at"])
        if shipped is None:
            continue
        for path in f["files"]:
            by_file.setdefault((f["project"], path), []).append((shipped, f["mr_id"]))
    for history in by_file.values():
        history.sort()

    def culprit(project: str, path: str, when: datetime, exclude_mr: int):
        best = None
        for shipped, mr_id in by_file.get((project, path), []):
            if shipped >= when:
                break
            if mr_id != exclude_mr and (when - shipped).total_seconds() <= days * 86400:
                best = (shipped, mr_id)
        return best

    out: dict[tuple, dict] = {}

    def add(feature_mr_id, kind, ref, path, shipped, when):
        key = (feature_mr_id, kind, str(ref))
        if key not in out:
            out[key] = {"feature_mr_id": feature_mr_id, "kind": kind, "source_ref": str(ref),
                        "file": path, "days_after": round((when - shipped).total_seconds() / 86400, 1)}

    for fix in fixes:
        when = _ts(fix["merged_at"])
        if when is None:
            continue
        for path in fix["files"]:
            hit = culprit(fix["project"], path, when, fix["mr_id"])
            if hit:
                add(hit[1], "fix_mr", fix["mr_id"], path, hit[0], when)

    for finding in findings:
        when = _ts(finding["created_at"])
        if when is None or not finding.get("file"):
            continue
        hit = culprit(finding["project"], finding["file"], when, finding["mr_id"])
        if hit:
            add(hit[1], "ai_refind", finding["note_id"], finding["file"], hit[0], when)
    return list(out.values())


def refresh(conn, days: float) -> int:
    """Recompute the followups table from mrs / mr_files / findings."""
    files: dict[int, list[str]] = {}
    for row in conn.execute("SELECT mr_id, path FROM mr_files"):
        files.setdefault(row["mr_id"], []).append(row["path"])
    merged = [dict(r) for r in conn.execute(
        "SELECT mr_id, project, merged_at, is_fix FROM mrs WHERE state = 'merged'")]
    for m in merged:
        m["files"] = files.get(m["mr_id"], [])
    findings = [dict(r) for r in conn.execute(
        "SELECT f.note_id, f.mr_id, m.project, f.file, f.created_at FROM findings f "
        "JOIN mrs m ON m.mr_id = f.mr_id")]       # gone-from-GitLab ones were still found
    rows = compute([m for m in merged if not m["is_fix"]], [m for m in merged if m["is_fix"]],
                   findings, days)
    with conn:
        conn.execute("DELETE FROM followups")
        conn.executemany("INSERT INTO followups VALUES "
                         "(:feature_mr_id, :kind, :source_ref, :file, :days_after)", rows)
    return len(rows)
