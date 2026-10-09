"""Pure parsing helpers (no IO): AI comment bodies, fix-MR detection, categories."""
import re

# The 面向 every finding is filed under. Order = display order.
CATEGORIES = {
    "security": "資安",            # authz/IDOR, injection, secrets, PII leaks, unsafe input
    "correctness": "正確性",       # logic bugs, wrong results, crashes, races, data loss
    "performance": "效能",         # N+1, unbounded loops/queries, needless heavy work
    "code_quality": "程式品質",    # error handling, validation, edge cases, testability
    "code_smell": "Code smell",    # duplication, dead code, misleading names, design smells
}

CATEGORY_MARKER_RE = re.compile(r"<!--\s*mr-sentinel:category=([a-z_]+)\s*-->")

# Two comment layouts have shipped: "🟠 **Medium** · title" (current) and the
# older "🟠 [Medium] title". Both carry the severity word; the emoji is a backup.
_HEAD_RE = re.compile(r"^\s*(?:🔴|🟠|🟡)?\s*(?:\*\*(High|Medium|Low)\*\*\s*·|\[(High|Medium|Low)\])\s*(.+?)\s*$",
                      re.M)
_EMOJI_SEVERITY = {"🔴": "high", "🟠": "medium", "🟡": "low"}

_FIX_RE = re.compile(r"(?<![a-z])(fix|hotfix|bugfix|bug)(?![a-z])|修正|修復|修bug|修 bug|bug修",
                     re.I)
# a version-bump / release title: "ver: 版號更新 1.0.1", "[ ver ] 版本更新", "版號 0.0.14"
_VERSION_BUMP_RE = re.compile(r"^\s*(\[\s*ver(sion)?\s*\]|ver(sion)?\s*[:：])|版號|版本更新|更新版本",
                              re.I)
# integration branches: an MR *from* one of these carries other people's work
RELEASE_SOURCES = {"dev", "develop", "development", "uat", "sit", "staging", "stage",
                   "pre-prod", "preprod", "prod", "production", "main", "master"}


def category_marker(category: str | None) -> str:
    """Invisible tag appended to a new AI comment, so a sync can file it without AI."""
    return f"<!-- mr-sentinel:category={category} -->" if category in CATEGORIES else ""


def parse_comment(body: str) -> dict:
    """AI comment body -> {severity, title, category}. Missing pieces are None."""
    body = body or ""
    severity = title = None
    match = _HEAD_RE.search(body)
    if match:
        severity = (match.group(1) or match.group(2)).lower()
        title = match.group(3).strip().strip("`").strip()
    else:
        first = body.strip().splitlines()[0] if body.strip() else ""
        severity = _EMOJI_SEVERITY.get(first[:1])
        title = first[1:].strip() if severity else None
    marker = CATEGORY_MARKER_RE.search(body)
    category = marker.group(1) if marker and marker.group(1) in CATEGORIES else None
    return {"severity": severity, "title": title, "category": category}


PRODUCTION_TARGETS = {"main", "master", "prod", "production"}


def is_release_mr(title: str | None, source_branch: str | None,
                  target_branch: str | None = None) -> bool:
    """A release / integration MR: from an integration branch (pre-prod -> master)
    or a version bump. Its diff is everyone's work, so it is nobody's personal MR:
    the scoring hands its findings back to the feature MRs that wrote that code.

    Deliberately NOT a release: a personal merge branch to master
    ("new-merge-branch -> master | [ feat ] 第三方登入改版") — in this team that is
    how one person ships their own feature, so it stays theirs."""
    source = (source_branch or "").strip().lower()
    if source in RELEASE_SOURCES or source.startswith(("release/", "release-", "hotfix-release")):
        return True
    return bool(_VERSION_BUMP_RE.search(title or "")) and not _FIX_RE.search(source)


def is_fix_mr(title: str | None, source_branch: str | None) -> bool:
    """A fix-type MR (counts as a possible follow-up bug of an earlier feature).
    Version bumps are excluded even when their title says 'fix'."""
    text = f"{title or ''} {source_branch or ''}"
    if _VERSION_BUMP_RE.search(title or "") and not _FIX_RE.search(source_branch or ""):
        return False
    return bool(_FIX_RE.search(text))
