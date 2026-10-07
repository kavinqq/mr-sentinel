"""Pure functions for the MR review pipeline (unit-testable, no IO)."""
import random

NOISE_SUFFIXES = (".lock", "-lock.json", ".min.js", ".min.css", ".map", ".svg", ".png", ".jpg", ".gif")
NOISE_NAMES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock", "composer.lock", "Cargo.lock", "go.sum")
NOISE_DIR_PARTS = ("node_modules", "/dist/", "/build/", "/vendor/", "/.next/", "/coverage/")


def is_review_target(project_path: str, review_cfg: dict) -> bool:
    """Whether this project is on the review allowlist."""
    return project_path in review_cfg.get("project_map", {})


def project_path_from_mr(mr: dict, gitlab_url: str) -> str:
    """Derive the full project path (group/sub/repo) from an MR's web_url."""
    path = mr["web_url"][len(gitlab_url):].lstrip("/")
    return path.split("/-/")[0]


def is_in_scope(project_path: str, path_prefixes: list) -> bool:
    """Group polling returns MRs from shared projects too; keep only real members.
    An empty prefix list means no filtering."""
    if not path_prefixes:
        return True
    return project_path.startswith(tuple(path_prefixes))


def resolve_local_path(project_path: str, review_cfg: dict) -> str | None:
    """GitLab project path -> local clone path; None if not allowlisted."""
    return review_cfg.get("project_map", {}).get(project_path)


def is_noise_path(path: str) -> bool:
    """Lockfiles / generated assets / vendored deps: skip to save AI budget."""
    name = path.rsplit("/", 1)[-1]
    if name in NOISE_NAMES:
        return True
    if any(path.endswith(sfx) for sfx in NOISE_SUFFIXES):
        return True
    probe = f"/{path}/"
    return any(part in probe for part in NOISE_DIR_PARTS)


def filter_noise_changes(changes: list[dict]) -> list[dict]:
    return [c for c in changes if not is_noise_path(c.get("new_path") or c.get("old_path") or "")]


def diff_stats(changes: list[dict]) -> tuple[int, int]:
    """Return (file count, changed line count); counts only +/- diff lines, not headers."""
    lines = 0
    for c in changes:
        for ln in (c.get("diff") or "").splitlines():
            if ln.startswith(("+++", "---")):
                continue
            if ln.startswith(("+", "-")):
                lines += 1
    return len(changes), lines


def plan_review(files: int, lines: int, review_cfg: dict) -> tuple[str, str]:
    """Pick a review depth by size, returning (mode, reason).

    The more a change touches, the more scrutiny it earns:
      - small (within both limits) -> ("lite", ""): a single scan pass.
      - large (over either limit)  -> ("deep", <which limit tripped>): three
        gates (scan -> adversarial vet -> final adjudication).
    reason is non-empty only for "deep" (used in the escalation log line).
    """
    max_files = review_cfg.get("max_changed_files", 60)
    max_lines = review_cfg.get("max_diff_lines", 3000)
    if files > max_files:
        return "deep", f"{files} files changed (limit {max_files})"
    if lines > max_lines:
        return "deep", f"{lines} lines changed (limit {max_lines})"
    return "lite", ""


# GitLab reports mergeability as "can_be_merged" (legacy merge_status) or
# "mergeable" (detailed_merge_status, GitLab >= 15.6). Anything else — including
# "unchecked"/"checking" — means "not safe to merge right now".
_MERGEABLE = {"can_be_merged", "mergeable"}


def auto_merge_blocker(mr: dict, reviewed_sha: str | None = None,
                       has_ci: bool = False) -> str | None:
    """Return why an MR must NOT be auto-merged, or None if it is safe.

    Hard rails for auto-merge-on-clean: never merge a draft, a head that moved
    since `reviewed_sha` (a push during the review would land unreviewed code),
    an MR GitLab does not consider mergeable right now, or one whose head
    pipeline is not green. A project with CI config but no pipeline in the
    payload is blocked too (skipped, not yet created, or an API hiccup); only a
    project with no CI at all lets mergeability alone decide.
    """
    if reviewed_sha and mr.get("sha") != reviewed_sha:
        return "new commits since review"
    if mr.get("draft") or mr.get("work_in_progress"):
        return "draft MR"
    status = mr.get("detailed_merge_status") or mr.get("merge_status")
    if status not in _MERGEABLE:
        return f"not mergeable ({status})"
    pipeline = mr.get("head_pipeline") or mr.get("pipeline") or {}
    pstatus = pipeline.get("status")
    if has_ci and not pstatus:
        return "pipeline missing"
    if pstatus and pstatus != "success":
        return f"pipeline {pstatus}"
    return None


def is_clean_result(result: dict) -> bool:
    """True only for an explicit empty findings list: a malformed engine output
    must never read as "clean", because clean is what triggers auto-merge."""
    return result.get("findings") == []


def auto_merge_eligible(mode: str, result: dict) -> bool:
    """Only a clean 3-gate deep review may merge; a clean lite pass is one gate."""
    return mode == "deep" and is_clean_result(result)


SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
SEVERITY_EMOJI = {"high": "🔴", "medium": "🟠", "low": "🟡"}
SEVERITY_LABEL = {"high": "High", "medium": "Medium", "low": "Low"}

DEFAULT_SIGNATURE = "— 🤖 mr-sentinel AI review"


def sort_findings(findings: list[dict]) -> list[dict]:
    """Stable sort by severity high->low; unknown severities sink to the bottom."""
    return sorted(findings, key=lambda f: SEVERITY_RANK.get(f.get("severity"), 99))


FIELD_LABELS = (("problem", "問題"), ("impact", "後果"), ("fix", "修正"))
EVIDENCE_SUMMARY = "完整依據與失敗情境"


VERDICT_IMAGE_DIR = {"clean": "no_bug", "minor": "high_grade",
                     "moderate": "medium_grade", "major": "low_grade"}
IMAGE_SUFFIXES = (".jpeg", ".jpg", ".png", ".gif", ".webp")


def verdict_tier(findings: list[dict]) -> str:
    """clean / minor / moderate / major, decided by the single worst finding."""
    if not findings:
        return "clean"
    worst = min(SEVERITY_RANK.get(f.get("severity"), 2) for f in findings)
    return ("major", "moderate", "minor")[worst]


def pick_image(names: list[str], choice=random.choice) -> str | None:
    """Random image among a folder's file names; non-images (.DS_Store etc.) are skipped."""
    candidates = sorted(n for n in names if n.lower().endswith(IMAGE_SUFFIXES))
    return choice(candidates) if candidates else None


def format_comment_body(finding: dict, signature: str = DEFAULT_SIGNATURE) -> str:
    """One finding -> one comment body. Signature is caller-built (engine models vary).

    Three scannable lines (問題 / 後果 / 修正) with the full reasoning folded into a
    `<details>` block: a reviewer sees the verdict at a glance, and the evidence is
    one click away for when they want to argue with it.

    The layout is guaranteed here rather than requested from the model — the engine
    fills separate fields, so no amount of prose drift can bury the suggested fix
    in paragraph five. A finding carrying only the older freeform `body` still
    renders, so findings files written before the split can be re-posted.
    """
    sev = finding.get("severity", "low")
    emoji = SEVERITY_EMOJI.get(sev, "🟡")
    label = SEVERITY_LABEL.get(sev, "Low")
    title = (finding.get("title") or "").strip()

    parts = [f"{emoji} **{label}** · {title}", ""]

    rows = [f"**{zh}**　{(finding.get(key) or '').strip()}"
            for key, zh in FIELD_LABELS if (finding.get(key) or "").strip()]
    parts.append("\n\n".join(rows) if rows else (finding.get("body") or "").strip())

    evidence = (finding.get("evidence") or "").strip()
    if evidence:
        # GitLab needs the blank lines for markdown inside <details> to render
        parts += ["", f"<details>\n<summary>{EVIDENCE_SUMMARY}</summary>\n\n"
                      f"{evidence}\n\n</details>"]

    parts += ["", signature]
    return "\n".join(parts)


def build_position(finding: dict, diff_refs: dict) -> dict | None:
    """Build a GitLab inline-discussion position; None when no line (falls back to a note).

    GitLab requires BOTH old_path and new_path for position_type=text; omitting
    old_path 400s every inline discussion. Defaulting old_path to the file path
    covers the non-rename case; renames can pass an explicit old_path.
    """
    line = finding.get("line")
    if line is None:
        return None
    return {
        "base_sha": diff_refs["base_sha"],
        "start_sha": diff_refs["start_sha"],
        "head_sha": diff_refs["head_sha"],
        "position_type": "text",
        "new_path": finding["file"],
        "old_path": finding.get("old_path") or finding["file"],
        "new_line": line,
    }


def position_form(position: dict) -> dict:
    """position dict -> GitLab form fields position[key]=value."""
    return {f"position[{k}]": v for k, v in position.items()}


def has_own_award_emoji(emojis: list[dict], user_id: int, name: str = "eyes") -> bool:
    """Idempotency check: our own :eyes: on the MR means it is already claimed."""
    return any(e.get("name") == name and e.get("user", {}).get("id") == user_id for e in emojis)


def slack_ts_for(state: dict, mr_id) -> str | None:
    return state.get("slack_ts", {}).get(str(mr_id))


# Every AI *finding* comment carries this marker (see DEFAULT_SIGNATURE /
# build_signature), which is what makes our own previous round identifiable on a
# re-review. Deliberately narrower than "🤖 mr-sentinel": operational notes we
# also post start "🤖 mr-sentinel: ..." (e.g. the auto-merge explanation) and
# must survive a cleanup — they are records, not review noise.
SIGNATURE_MARKER = "🤖 mr-sentinel AI review"


def deletable_ai_notes(discussions: list[dict], user_id: int,
                       marker: str = SIGNATURE_MARKER) -> list:
    """Note ids of our own AI comments that nobody has replied to.

    Re-reviewing an MR would otherwise stack a second round of comments on top of
    the first. Two rails keep the cleanup from destroying context:
      - a discussion any *other* author has joined is left completely alone, so a
        human's reply (and the comment it answers) never disappears;
      - only notes carrying the AI signature are deleted, so operational notes
        and anything hand-written by us survive.
    """
    deletable = []
    for discussion in discussions:
        notes = [n for n in (discussion.get("notes") or []) if not n.get("system")]
        if not notes:
            continue
        authors = {(n.get("author") or {}).get("id") for n in notes}
        if authors != {user_id}:
            continue
        deletable += [n["id"] for n in notes if marker in (n.get("body") or "")]
    return deletable
