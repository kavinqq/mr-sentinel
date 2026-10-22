"""Deterministic gate: test-only code (buttons, accounts, bypasses) must not ship.

A rule match, not a model judgment, so the skeptic pass can never drop it and it
still lands when the AI engine fails. Only added lines count. A file is skipped as
test code only when its path looks like a test AND nothing outside tests refers
to it at the reviewed head (exempt_test_files): a folder name proves nothing.
"""
import re
import subprocess

import review_common

GATE_NAME = "artifact"

_MARKERS = re.compile(
    r"測試(用|專用|按鈕)|(僅|只)供?測試|(暫時|臨時)(測試|debug)"
    r"|上線前.{0,4}(移除|刪除|刪掉|拿掉)"
    r"|\b(test|testing|debug)[ _-]?only\b"
    r"|\bremove\s+before\s+(merge|release|prod(uction)?|deploy)\b"
    r"|\bdo\s*not\s*merge\b",
    re.IGNORECASE,
)
# a hook that exists so the automated suite can drive production code is not a
# leftover; only the phrase is blanked, so another marker on the line still hits
_TEST_SUITE_HELPER = re.compile(r"(單元|整合|自動化|e2e)\s*測試用", re.IGNORECASE)

_TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|__mocks?__|mocks?|e2e|playwright|fixtures)/"
    r"|\.(test|spec|stories|mock)\.[^/]+$"
    r"|(^|/)test_[^/]*\.py$|_test\.(py|go)$|(^|/)conftest\.py$|(^|/)playwright\.config\.[^/]+$"
)

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

_TEXT = {
    "zh": {
        "title": "測試用程式碼會跟著上線",
        "problem": "新增的程式碼帶有測試標記 `{marker}`:`{snippet}`",
        "impact": "合進上線分支後,正式環境的使用者會看到或觸發只該存在於測試環境的東西"
                  "(按鈕、帳號、繞過檢查的路徑)。",
        "fix": "刪掉;真的要留就改成只在測試環境生效(環境判斷或 feature flag),並在 MR 說明原因。",
        "rule": "此則由測試標記規則逐字比對產生,不經 AI 判斷;確定是誤判(例如只是註解提到測試),"
                "在這則留言回覆說明,再按申訴。",
    },
    "en": {
        "title": "Test-only code would ship to production",
        "problem": "An added line carries the test marker `{marker}`: `{snippet}`",
        "impact": "Once merged into the release branch, production users see or trigger "
                  "something meant only for testing (a button, an account, a bypass).",
        "fix": "Delete it; if it must stay, gate it to non-production (environment check or "
               "feature flag) and say why in the MR description.",
        "rule": "Raised by the test-marker rule word for word, not by the AI. If it is a "
                "false positive (say, a comment that only mentions tests), reply here and appeal.",
    },
}


_TEST_DIR = re.compile(r"(^|/)(tests?|__tests__|__mocks?__|mocks?|e2e|playwright|fixtures)/")
_ENTRY_STEMS = {"index", "__init__", "main"}


_UNSHIPPED_SUFFIXES = (".lock", "-lock.json", ".map", ".png", ".jpg", ".gif")


def never_ships(path: str) -> bool:
    """Lockfiles, source maps and images: nothing in them runs as shipped code.
    Narrower than review_common.is_noise_path on purpose: build output and
    vendored code are noise to a code review, but they ship."""
    return (path.rsplit("/", 1)[-1] in review_common.NOISE_NAMES
            or path.endswith(_UNSHIPPED_SUFFIXES))


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH.search(path or ""))


def reference_tokens(path: str) -> list[str] | None:
    """Strings an import of `path` would contain; None when the file cannot be
    told apart from its folder (an index/__init__ is imported by the folder name)."""
    name = path.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0]
    if stem.split(".")[0] in _ENTRY_STEMS:
        return None
    folder = _TEST_DIR.search(path)
    if folder:
        return [f"{folder.group(2)}/{stem}", f"{folder.group(2)}.{stem}"]
    return [stem]


def exempt_test_files(local: str | None, head: str, paths: list[str]) -> set[str]:
    """Test-looking paths that nothing outside tests references at `head`.

    Fails toward scanning: no clone, a git error or an entry-point file all mean
    the file is not exempt."""
    exempt: set[str] = set()
    if not local:
        return exempt
    for path in paths:
        tokens = reference_tokens(path) if is_test_path(path) else None
        if not tokens:
            continue
        args = ["grep", "-l", "-F"] + [x for t in tokens for x in ("-e", t)] + [head, "--"]
        try:
            out = _git(local, *args)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode not in (0, 1):
            continue
        referrers = [line.split(":", 1)[1] for line in out.stdout.splitlines() if ":" in line]
        if not [r for r in referrers if r != path and not is_test_path(r)]:
            exempt.add(path)
    return exempt


def marker_in(line: str) -> str | None:
    match = _MARKERS.search(_TEST_SUITE_HELPER.sub("", line))
    return match.group(0) if match else None


def added_lines(diff: str):
    """Yield (new-file line number, text) for every added line of one file's unified diff."""
    line_no = None
    for raw in (diff or "").splitlines():
        hunk = HUNK.match(raw)
        if hunk:
            line_no = int(hunk.group(1))
        elif line_no is None or raw.startswith("\\"):
            continue
        elif raw.startswith("+"):
            yield line_no, raw[1:]
            line_no += 1
        elif raw.startswith(" "):
            line_no += 1


def scan(changes: list[dict], language: str, exempt: set[str] = frozenset()) -> list[dict]:
    """One high finding per file with added test-marker lines, anchored on the first hit.
    `exempt`: verified test-only files (exempt_test_files)."""
    text = _TEXT["zh" if language.startswith("zh") else "en"]
    findings = []
    for c in changes:
        path = c.get("new_path") or ""
        if not path or path in exempt or never_ships(path):
            continue
        hits = [(n, line.strip(), m) for n, line in added_lines(c.get("diff"))
                if (m := marker_in(line))]
        if not hits:
            continue
        first_line, snippet, marker = hits[0]
        evidence = [f"- `{path}:{n}` `{s[:160]}`" for n, s, _ in hits] + [f"- {text['rule']}"]
        findings.append({
            "severity": "high",
            "category": "correctness",
            "title": text["title"],
            "file": path,
            "line": first_line,
            "problem": text["problem"].format(marker=marker, snippet=snippet[:120]),
            "impact": text["impact"],
            "fix": text["fix"],
            "evidence": "\n".join(evidence),
            "gate": GATE_NAME,
            "confidence": "sure",
            "hit_lines": [n for n, _, _ in hits],
        })
    return findings


def _git(local: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", local, "-c", "core.quotepath=false", *args],
                          capture_output=True, text=True, timeout=120)


def _has_commit(local: str, sha: str) -> bool:
    return _git(local, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def git_changes(local: str, diff_refs: dict, target_branch: str) -> list[dict] | None:
    """The MR diff straight from the clone, or None when it cannot be produced.

    Preferred over the API diff because GitLab hands back empty diffs for large
    MRs. The MR head is already fetched by the reviewer; the start commit may not
    be, so the target branch is fetched once before giving up.
    """
    start, head = diff_refs.get("start_sha"), diff_refs.get("head_sha")
    if not (local and start and head):
        return None
    try:
        if not _has_commit(local, start) and target_branch:
            _git(local, "fetch", "-q", "origin", target_branch)
        if not (_has_commit(local, start) and _has_commit(local, head)):
            return None
        out = _git(local, "diff", "--no-color", "--no-ext-diff", "-U0", start, head)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    changes: list[dict] = []
    for raw in out.stdout.splitlines(keepends=True):
        if raw.startswith("diff --git "):
            changes.append({"new_path": None, "diff": ""})
        elif not changes:
            continue
        elif raw.startswith("+++ "):
            target = raw[4:].rstrip("\n")
            changes[-1]["new_path"] = target[2:] if target.startswith("b/") else None
        else:
            changes[-1]["diff"] += raw
    return [c for c in changes if c["new_path"]]


def collect(local: str | None, diff_refs: dict, target_branch: str,
            api_changes: list[dict] | None) -> list[dict] | None:
    """Changes to scan, or None when no trustworthy diff exists (callers fail closed)."""
    changes = git_changes(local, diff_refs, target_branch) if local else None
    if changes is not None:
        return changes
    if not api_changes or any(not c.get("diff") for c in api_changes):
        return None
    return api_changes
