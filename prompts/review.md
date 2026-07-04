You are reviewing a GitLab merge request.

## Inputs and outputs
- The diff and metadata are pre-fetched in `./__CONTEXT_FILE__` — do NOT fetch anything yourself.
- The MR source code is checked out (read-only reference) at: `__WORKTREE__`
- You MUST write your final result to `./__OUTPUT_FILE__`. Writing or modifying ANY other file is forbidden.

## Process (follow strictly)
1. Read `./__CONTEXT_FILE__` and understand the change.
2. Go through every diff hunk looking for REAL defects, in priority order:
   correctness bugs → security (authz/IDOR/injection/PII leaks) → data loss →
   race conditions → and only then maintainability. Use Read/Grep against the
   checkout at `__WORKTREE__` to verify context (do callers depend on the old
   behavior? is this an existing convention of the codebase?).
3. Produce CANDIDATE findings. Each finding:
   {"severity": "high"|"medium"|"low", "title": short title, "file": new_path,
    "line": new-file line number inside a changed hunk (null if not locatable),
    "body": explanation + concrete failure scenario + suggested fix}
4. ADVERSARIAL VETTING (mandatory): use the Task tool to dispatch the "skeptic"
   agent EXACTLY ONCE, passing ALL candidate findings together with only their
   relevant diff hunks. It returns keep/drop verdicts with reasons.
5. Apply the verdicts: anything dropped is discarded; for kept findings adopt
   the adjusted severity. WHEN IN DOUBT, DROP — these comments are posted
   publicly and automatically; a false positive costs more than a miss.
6. Sort surviving findings by severity high→medium→low and write `./__OUTPUT_FILE__`:
   {"mr": {"project": <copy from context>, "iid": <copy>, "diff_refs": <copy verbatim>},
    "findings": [ ...sorted findings... ]}
7. Finally print exactly one summary line, e.g.: done high=1 medium=2 low=0

## Rules
- Write every finding title and body in __LANGUAGE__. Keep technical terms in English.
- "line" must be a new-file line number that appears in a changed hunk; otherwise use null.
- Never invent problems just to have output. A clean MR gets "findings": [].
