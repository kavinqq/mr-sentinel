"""Release guard: an extra AI review, on top of the normal one, for MRs into a
production branch.

The normal review is tuned to drop anything it is not sure about, which is how
test buttons, UAT hosts and debug settings passed review after review. This pass
has the opposite bias, its own prompt (prompts/release_guard.md) and no skeptic:
test leaks first, and anything else that looks strange for code about to ship,
listed as a human check when the model is not sure.

Everything about it fails closed: the diff is split into batches so every added
line is in front of the model (never a truncated sample), the model must name
every file it checked, and any malformed answer counts as a failed guard.
"""
import re

import artifact_gate

GATE_NAME = "release_guard"
DEFAULT_MAX_LINES = 3000
DEFAULT_MAX_BATCHES = 15

_SIGNALS = re.compile(
    r"localhost|127\.0\.0\.1|0\.0\.0\.0|\.test\b|\.internal\b"
    r"|\b(uat|sit|qa|stag(e|ing)|sandbox|dev|lab|beta|demo|preview|preprod|pre-prod)[-.]"
    r"|[-.](uat|sit|qa|stag(e|ing)|sandbox|dev|lab|preview|preprod)\b"
    r"|['\"](uat|sit|qa|stag(e|ing)|sandbox|dev|test|preview|preprod|pre-prod)['\"]"
    r"|\bdebug\b\s*[:=]+\s*(true|1|['\"]?on)\b"
    r"|\bmsw\b|\bmock|\bfake|\bstub"
    r"|\b(skip|bypass|disable|ignore)[_-]?(auth|login|verify|verification|sign|signature|captcha|otp|check|csrf|ssl)"
    r"|test[_-]?(mode|env|account|user|uid|id|token|key|secret|card|phone|data)"
    r"|\bif\s*\(\s*(true|1)\s*\)|\bif\s+(True|1)\s*:"
    r"|測試|假資料|寫死|白名單|先跳過|暫時",
    re.IGNORECASE,
)
# not test leaks, but out of place in code about to ship; hints only, like _SIGNALS
_STRANGE = re.compile(
    r"\bconsole\.(log|debug|trace)\b|^\s*print\(|\bvar_dump\b|\bprint_r\b|\bdebugger\b"
    r"|\b(TODO|FIXME|HACK|XXX)\b|臨時|先這樣|之後再?(改|刪|拿掉|移除)"
    r"|\b(password|passwd|secret|api[_-]?key|access[_-]?key|private[_-]?key)\b\s*[:=]"
    r"|^\s*(#|//)\s*(def|function|if|for|return|const|let|var|class|import|await)\b",
    re.IGNORECASE,
)

added_lines = artifact_gate.added_lines


MINIFIED_SUFFIXES = (".min.js", ".min.css")


def config_problem(guard_cfg: dict) -> str | None:
    """Without the team's own test hosts the guard can only guess, so it does not count as run."""
    if not [h for h in guard_cfg.get("test_hosts") or [] if str(h).strip()]:
        return "review.release_guard.test_hosts is not configured"
    for key in ("max_diff_lines", "max_batches"):
        value = guard_cfg.get(key, 1)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            return f"review.release_guard.{key} must be a positive integer"
    return None


def model_reads(path: str) -> bool:
    """A minified bundle is one huge line no model reads usefully;
    minified_host_findings and the marker rule cover it instead."""
    return not artifact_gate.never_ships(path) and not path.endswith(MINIFIED_SUFFIXES)


def minified_host_findings(changes: list[dict], test_hosts: list[str], language: str) -> list[dict]:
    """One rule finding per minified bundle whose added lines contain a test host."""
    zh = language.startswith("zh")
    hosts = [h.lower() for h in test_hosts if str(h).strip()]
    findings = []
    for c in changes:
        path = c.get("new_path") or ""
        if not path.endswith(MINIFIED_SUFFIXES):
            continue
        hits = [(n, h) for n, text in added_lines(c.get("diff")) for h in hosts if h in text.lower()]
        if not hits:
            continue
        found = ", ".join(sorted({f"`{h}`" for _, h in hits}))
        findings.append({
            "severity": "high", "category": "correctness", "confidence": "sure",
            "title": "壓縮後的 bundle 含測試主機" if zh else "Minified bundle contains a test host",
            "file": path, "line": hits[0][0],
            "problem": (f"建置產物裡有測試主機 {found}" if zh
                        else f"The build output contains test host(s) {found}"),
            "impact": ("正式使用者的瀏覽器會連到測試環境。" if zh
                       else "Production users' browsers would call a test environment."),
            "fix": ("用正式環境設定重新建置,或不要 commit 建置產物。" if zh
                    else "Rebuild with production settings, or stop committing build output."),
            "evidence": "\n".join(f"- `{path}:{n}` `{h}`" for n, h in hits),
            "gate": artifact_gate.GATE_NAME,
            "hit_lines": sorted({n for n, _ in hits}),
        })
    return findings


def candidates(changes: list[dict], test_hosts: list[str]) -> list[dict]:
    """Keyword hints for the model. Every added line is sent anyway; these only
    point at the likeliest leaks so none is skimmed over."""
    hosts = [h.lower() for h in test_hosts if h]
    out = []
    for c in changes:
        path = c.get("new_path") or ""
        if not path or not model_reads(path):
            continue
        for line_no, text in added_lines(c.get("diff")):
            why = [h for h in hosts if h in text.lower()]
            for pattern in (_SIGNALS, _STRANGE):
                signal = pattern.search(text)
                if signal:
                    why.append(signal.group(0))
            if why:
                out.append({"file": path, "line": line_no, "text": text.strip()[:240],
                            "why": ", ".join(why), "test_path": artifact_gate.is_test_path(path)})
    return out


def _split_hunks(diff: str) -> list[str]:
    parts: list[str] = []
    for raw in (diff or "").splitlines(keepends=True):
        if raw.startswith("@@") or not parts:
            parts.append("")
        parts[-1] += raw
    return parts


def _added_count(diff: str) -> int:
    return sum(1 for _ in added_lines(diff))


def _split_hunk(hunk: str, max_lines: int) -> list[str]:
    """A hunk with more than `max_lines` added lines as several hunks. Each later
    piece gets its own header with the new-file line it starts at, so
    added_lines() numbers every line exactly as in the original hunk."""
    header, _, body = hunk.partition("\n")
    start = artifact_gate.HUNK.match(header)
    if not start or _added_count(hunk) <= max_lines:
        return [hunk]
    line_no = int(start.group(1))
    pieces, current, added = [header + "\n"], "", 0
    for raw in body.splitlines(keepends=True):
        if raw.startswith("+") and added == max_lines:
            pieces[-1] += current
            pieces.append(f"@@ -0,0 +{line_no} @@\n")
            current, added = "", 0
        current += raw
        if raw.startswith("+"):
            added += 1
            line_no += 1
        elif raw.startswith(" "):
            line_no += 1
    pieces[-1] += current
    return pieces


def build_batches(ctx: dict, changes: list[dict], guard_cfg: dict,
                  max_lines: int = DEFAULT_MAX_LINES,
                  max_batches: int = DEFAULT_MAX_BATCHES) -> list[dict] | None:
    """Guard contexts that together carry every added line, or None when that takes
    more than `max_batches` (a human must review it; nothing is sampled)."""
    files = [c for c in changes if c.get("new_path") and model_reads(c["new_path"])]
    pieces: list[dict] = []
    for c in files:
        hunk_list = [piece for hunk in _split_hunks(c.get("diff") or "") or [""]
                     for piece in _split_hunk(hunk, max_lines)]
        chunk = ""
        for hunk in hunk_list:
            if chunk and _added_count(chunk) + _added_count(hunk) > max_lines:
                pieces.append({"new_path": c["new_path"], "diff": chunk})
                chunk = ""
            chunk += hunk
        pieces.append({"new_path": c["new_path"], "diff": chunk})

    groups: list[list[dict]] = []
    used = 0
    for piece in pieces:
        size = _added_count(piece["diff"])
        if groups and used + size <= max_lines:
            groups[-1].append(piece)
            used += size
        else:
            groups.append([piece])
            used = size
    if not groups:
        groups = [[]]
    if len(groups) > max_batches:
        return None

    hosts = guard_cfg.get("test_hosts") or []
    return [{
        "project": ctx.get("project"), "iid": ctx.get("iid"),
        "title": ctx.get("title"), "description": ctx.get("description"),
        "target_branch": ctx.get("target_branch"),
        "test_hosts": hosts,
        "environment_notes": guard_cfg.get("environment_notes", ""),
        "batch": {"index": i + 1, "total": len(groups)},
        "files": sorted({p["new_path"] for p in group}),
        "all_changed_files": [c.get("new_path") for c in changes],
        "candidates": candidates(group, hosts),
        "changes": group,
    } for i, group in enumerate(groups)]


_REQUIRED_TEXT = ("title", "problem", "impact", "fix")
HUMAN_CHECK = {"zh": "需要人工確認", "en": "Needs a human check"}


def _valid_finding(f, files: set) -> bool:
    if not isinstance(f, dict):
        return False
    if f.get("severity") not in ("high", "medium") or f.get("category") not in ("security", "correctness"):
        return False
    if f.get("confidence") not in ("sure", "unsure"):
        return False
    if f["confidence"] == "unsure" and not (isinstance(f.get("human_check"), str)
                                            and f["human_check"].strip()):
        return False
    if f.get("file") not in files:
        return False
    line = f.get("line")
    if line is not None and (not isinstance(line, int) or isinstance(line, bool) or line < 1):
        return False
    return all(isinstance(f.get(k), str) and f[k].strip() for k in _REQUIRED_TEXT)


def _with_human_check(f: dict, language: str) -> dict:
    """An unsure finding's MR comment leads with what the human must confirm."""
    if f["confidence"] != "unsure":
        return f
    label = HUMAN_CHECK["zh" if language.startswith("zh") else "en"]
    return {**f, "evidence": f"- **{label}**:{f['human_check']}\n{f.get('evidence') or ''}".rstrip()}


def normalize(result, batch_files: list[str], language: str = "en") -> list[dict] | None:
    """The batch's findings marked as the guard's, or None for anything short of a
    complete, well-formed answer: a missing list, a file the model did not claim
    to have checked, or a single malformed finding (including one without a
    `confidence`, or an unsure one without a `human_check`) fails the whole guard."""
    if not isinstance(result, dict):
        return None
    findings, checked = result.get("findings"), result.get("checked_files")
    if not isinstance(findings, list) or not isinstance(checked, list):
        return None
    if not all(isinstance(path, str) for path in checked):
        return None
    files = set(batch_files)
    if not files <= set(checked):
        return None
    if not all(_valid_finding(f, files) for f in findings):
        return None
    return [{**_with_human_check(f, language), "gate": GATE_NAME} for f in findings]
