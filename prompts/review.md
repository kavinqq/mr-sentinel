You are reviewing a GitLab merge request.

## Inputs and outputs
- The diff and metadata are pre-fetched in `./__CONTEXT_FILE__` — do NOT fetch anything yourself.
- The MR source code is checked out (read-only reference) at: `__WORKTREE__`
- You MUST write your final result to `./__OUTPUT_FILE__`. Writing or modifying ANY other file is forbidden.

## Process (follow strictly)
1. Read `./__CONTEXT_FILE__` and understand the change. Its `description` is the
   author's MR description — treat it as the spec the change must meet.
2. Go through every diff hunk looking for REAL defects, in priority order:
   correctness bugs → security (authz/IDOR/injection/PII leaks) → data loss →
   race conditions → and only then maintainability. Use Read/Grep against the
   checkout at `__WORKTREE__` to verify context (do callers depend on the old
   behavior? is this an existing convention of the codebase?).
3. Produce CANDIDATE findings. Each finding:
   {"severity": "high"|"medium"|"low",
    "category": one of the 8 keys in "## Categories" below, by its rules,
    "title": the defect in one line, <= 60 chars — name the consequence, not the file,
    "file": new_path,
    "line": new-file line number inside a changed hunk (null if not locatable),
    "problem": WHAT is wrong. Name the symbol/expression. 1-2 sentences, one line if possible.
    "impact": the concrete failure: "<input or state> -> <wrong output/crash/leak>".
              1-2 sentences. This is what convinces the author it matters.
    "fix": the concrete change to make, specific enough to act on. 1-2 sentences.
    "evidence": the full reasoning — how you verified it against the checkout, the
                code paths involved, asymmetries with sibling code, why this is not
                a false positive. Long form is fine and encouraged here, but
                write it as a markdown bullet list — one verified step or fact
                per bullet, symbols and line refs in `backticks` — never one
                unbroken paragraph.}

   problem/impact/fix are rendered as three standalone lines a reviewer reads in
   five seconds, so keep each SHORT and self-contained; everything that does not
   fit belongs in "evidence", which is collapsed in the UI. Do not repeat the
   file path or line number — the comment is already attached to that line.
__VETTING__
6. Sort surviving findings by severity high→medium→low and write `./__OUTPUT_FILE__`:
   {"mr": {"project": <copy from context>, "iid": <copy>, "diff_refs": <copy verbatim>},
    "findings": [ ...sorted findings... ]}
7. Finally print exactly one summary line, e.g.: done high=1 medium=2 low=0

## Categories
__TAXONOMY__

## Rules
- Write title, problem, impact, fix and evidence in __LANGUAGE__. Keep technical
  terms, symbol names and code in English, and wrap symbols, expressions and
  annotations in `backticks` in every field.
- "line" must be a new-file line number that appears in a changed hunk; otherwise use null.
- Never invent problems just to have output. A clean MR gets "findings": [].
