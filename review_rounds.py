"""Later review rounds: every round still reads the whole MR, but from the second
completed round on, a finding nobody raised before is posted only where the code
changed since that round (owner's call, 2026-11-07, after !60's gate gave 7, 8,
then 12 comments on one commit). The first round is the strict one.

Not a chained, diff-only review (that was tried and dropped): nothing here narrows
what the model reads, so a missed round, a race or a reverted fix cannot hide
code from it. Anything that does not chain cleanly — no completed round on record,
another target, a rebase, a commit the clone lacks, a deep round after a lite one —
falls back to posting everything (the caller logs why).
"""
import re
import subprocess
from dataclasses import dataclass

import artifact_gate
import review_common

# lines within this distance of a change count as changed: git's default context,
# the same hunk context the model may anchor a finding on (prompts/review.md)
TOUCH_MARGIN = 3

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_NOTE_FILE = re.compile(r"\A`([^`\n]+?)(?::\d+)?`\n")


@dataclass(frozen=True)
class Scope:
    previous: str                          # head of the last completed round
    changed: dict                          # path -> new-side lines touched, None = all of it
    carried: frozenset                     # files whose findings are not new (carried_files)


def changed_since(local: str | None, previous: str | None, head: str
                  ) -> tuple[dict | None, str]:
    """({path: touched new-side lines, or None for the whole file}, "") when `previous`
    is `head` or an ancestor of it in the clone; (None, why) otherwise."""
    if not (local and previous and head):
        return None, "no clone or no previous head"
    try:
        ancestor = artifact_gate._git(local, "merge-base", "--is-ancestor", previous, head)
        if ancestor.returncode == 1:
            return None, f"previous head {previous[:8]} is not an ancestor of {head[:8]}"
        if ancestor.returncode != 0:
            return None, f"previous head {previous[:8]} unknown to the clone"
        names = artifact_gate._git(local, "diff", "--name-only", "-z", "--no-renames",
                                   previous, head)
        patch = artifact_gate._git(local, "diff", "--no-color", "--no-ext-diff", "--no-renames",
                                   "-U0", "--src-prefix=a/", "--dst-prefix=b/", previous, head)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"git diff failed: {exc}"
    if names.returncode != 0 or patch.returncode != 0:
        return None, "git diff failed"
    lines = touched_lines(patch.stdout)
    return {name: (lines or {}).get(name) for name in names.stdout.split("\0") if name}, ""


def touched_lines(patch: str) -> dict | None:
    """`git diff -U0` -> {path: new-side lines}. A pure deletion touches the lines on
    either side of it. Files absent from the result (binary, deleted) count whole;
    None (every file whole) when a header names a quoted path this does not decode."""
    out: dict[str, set] = {}
    path, in_header = None, False
    for raw in patch.splitlines():
        if raw.startswith("diff --git "):
            path, in_header = None, True
        elif in_header and raw.startswith("+++ "):
            # header lines only: with -U0 an added line "++ x;" also reads "+++ x;"
            target = raw[4:]
            if target.startswith('"'):
                return None
            path = target[2:] if target.startswith("b/") else None
            if path is not None:
                out.setdefault(path, set())
        elif hunk := _HUNK.match(raw):
            in_header = False
            if path is not None:
                start, count = int(hunk.group(1)), int(hunk.group(2) or 1)
                out[path].update({start, start + 1} if count == 0
                                 else range(start, start + count))
    return out


def _comment_file(path: str | None, body: str | None) -> str | None:
    """The file a comment is about: its position, or the `file:line` prefix of the
    plain note a file-level finding or a rejected position falls back to."""
    match = _NOTE_FILE.match(body or "")
    return path or (match.group(1) if match else None)


def carried_files(discussions: list[dict], user_id, head: str, recorded: list[dict],
                  changed_since, moved: dict[str, str]) -> set[str]:
    """Files where a finding is not new even on untouched code:
    - a release-gate comment for an earlier head still open: it holds only its own
      head, so this head's gate must post it again or its status goes green;
    - a comment the rerun cleanup deleted unanswered (history db `recorded` rows),
      from any earlier round: deleted is not answered, and a round in between may
      simply not have found it again;
    - a closed finding whose verdict was given on another head: accepted as fixed
      (or "unknown") — undoing the fix may touch any file; accepted on a reason —
      once its own file changed since that verdict. Measured from the verdict, not
      the last round, which may have missed the reopen.
    `changed_since(sha)`: files changed from `sha` to `head`, None when unknown;
    `moved`: renames (renames()), so a comment on an old path still matches.
    Matched by file, since titles and lines drift between rounds (!60)."""
    files = set()
    for d in discussions:
        status = review_common.discussion_status(d, user_id)
        if status is None:
            continue
        first = review_common._human_notes(d)[0]
        file = _comment_file((first.get("position") or {}).get("new_path"), first.get("body"))
        on = review_common.gate_head(d)
        if status != "closed":
            if on and on != head:
                files.add(file)
            continue
        judged = _judged_head(d, user_id)
        if judged == head:
            continue
        if review_common.settled_as(d, user_id) in ("fixed", "unknown"):
            files.add(file)
            continue
        since = changed_since(judged) if judged else None
        if since is None or follow({file}, moved) & since:
            files.add(file)
    files |= {_comment_file(r.get("file"), r.get("body")) for r in recorded
              if r.get("status") == "unanswered"}
    return {f for f in files if f}


def files_changed(local: str, since: str, head: str) -> set[str] | None:
    """Paths changed from `since` to `head` (a rename = both paths), None when git
    cannot tell (a commit the clone lacks)."""
    try:
        out = artifact_gate._git(local, "diff", "--name-only", "-z", "--no-renames", since, head)
    except (OSError, subprocess.SubprocessError):
        return None
    return {n for n in out.stdout.split("\0") if n} if out.returncode == 0 else None


def renames(local: str, start: str, head: str) -> dict[str, str] | None:
    """{old path: path at `head`} for every rename along the MR's line of commits
    (start..head), chained; None when git cannot tell. First-parent with merge diffs:
    a rename the MR took in by merging its target shows on the merge commit."""
    if not (local and start and head):
        return None
    try:
        out = artifact_gate._git(local, "log", "-M", "-m", "--first-parent", "--diff-filter=R",
                                 "--name-status", "-z", "--reverse", "--format=",
                                 f"{start}..{head}")
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    moved: dict[str, str] = {}
    fields = [f.strip("\n") for f in out.stdout.split("\0")]
    fields = [f for f in fields if f]
    for i in range(0, len(fields) - 2, 3):
        if not fields[i].startswith("R"):
            return None                          # an entry this parser does not know
        old, new = fields[i + 1], fields[i + 2]
        moved = {k: (new if v == old else v) for k, v in moved.items()}
        moved[old] = new
    return moved


def follow(files: set[str], moved: dict[str, str]) -> set[str]:
    """`files` plus the names they were renamed to."""
    return files | {moved[f] for f in files if f and f in moved}


def _judged_head(discussion: dict, user_id) -> str | None:
    """The head our latest verdict on the thread was given on, if it recorded one."""
    for note in reversed(review_common._human_notes(discussion)):
        if review_common.is_bot_note(note, user_id):
            version = review_common.judged_on(note.get("body"))
            return version.split(":", 1)[0] if version else None
    return None


def lands_on_change(finding: dict, changed: dict) -> bool:
    path = finding.get("file")
    if path not in changed:
        return False
    lines = changed[path]
    try:
        line = int(finding.get("line"))
    except (TypeError, ValueError):
        return True                         # about the file, which changed
    return lines is None or any(abs(line - n) <= TOUCH_MARGIN for n in lines)


def split(findings: list, scope: Scope) -> tuple[list, list]:
    """(posted, held back). A finding about no file at all is always posted: nothing
    ties it to a part of the code, and it may be about the change as a whole."""
    kept, held = [], []
    for f in findings:
        new_and_elsewhere = (f.get("file") and f["file"] not in scope.carried
                             and not lands_on_change(f, scope.changed))
        (held if new_and_elsewhere else kept).append(f)
    return kept, held
