# Architecture

## The one rule

**Scripts do everything deterministic; the AI only judges code.**
Every step a script can perform with 100% correctness (polling, claiming,
fetching diffs, posting comments, notifications, locking, dedup) is Python.
The AI reads one file and writes one file. It never touches GitLab or Slack.

## Flow

```
scheduler (launchd / cron / systemd, every 60s)
  └─ poller.py            flock'd; polls review.project_map projects for opened MRs
       ├─ Slack notify    (optional; message ts saved to state.json)
       └─ spawn           detached reviewer.py per new MR — never blocks the poll loop
            │
            ▼
     reviewer.py           per-MR flock
       1. idempotency      our own :eyes: award emoji on the MR = already claimed → exit
       2. fetch context    fetch_mr.py → mr_context.json (noise-filtered diff + metadata)
       3. claim            :eyes: on the MR (+ Slack reaction when configured)
       4. size guard       oversized → skip + warn (protects the review budget)
       5. worktree         git fetch refs/merge-requests/<iid>/head → disposable worktree
       6. AI engine        engines/<engine>.run_review(context → findings)   ← only AI step
       7. post             post_comment.py: inline discussion first, note fallback
       8. notify           optional Slack completion message
       9. cleanup          worktree removed (finally-block, even on crash)
```

## Engine contract (the DI seam)

```python
def run_review(work_dir, context_file, output_file, repo_dir, review_cfg) -> int
def label(review_cfg) -> str
```

- `context_file` (`mr_context.json`): title, web_url, `diff_refs`
  (base/start/head SHAs), noise-filtered `changes[]`, size `stats`.
- `output_file` (`final_findings.json`):
  `{"mr": {project, iid, diff_refs}, "findings": [{severity, title, file, line, body}]}`
- The engine may read `repo_dir` (the disposable worktree) for context.
  It must not modify anything except `output_file`.

### claude engine (default)

One headless session: `claude -p <prompts/review.md> --agents <skeptic>`.
The skeptic persona (`prompts/skeptic.md`) is injected as an inline subagent
with its own cheaper model — no `~/.claude` installation required.
The scan model produces candidates, dispatches the skeptic exactly once,
drops everything refuted, and writes the findings file itself.

Note: do **not** add `--bare` — it restricts auth to `ANTHROPIC_API_KEY`,
silently moving subscription users onto metered API billing.

### codex engine (experimental)

Codex has no subagents, so the engine orchestrates:
pass 1 `codex exec` scan → candidates JSON (via `--output-last-message`),
pass 2 `codex exec` skeptic verdicts, then Python applies keep/drop
mechanically (no third AI call). `--sandbox read-only` throughout.

Both engines share `prompts/skeptic.md`, so vetting rules cannot drift.

## Why findings travel as files, not stdout

Headless sessions on real developer machines get their text output polluted
(hooks, output styles, plugins). Files are deterministic; parsing model
prose is not. The final message is used for nothing.

## State & idempotency

- `state.json`: `seen` (notified MR ids; entries still opened are never
  pruned, others age out after 7 days) and `slack_ts` (message timestamps
  for reactions).
- The review claim marker is the `:eyes:` award emoji *on GitLab itself* —
  survives state resets, visible to humans, naturally idempotent
  (GitLab rejects duplicate awards from the same user).

## Module map

| Module | Responsibility |
|---|---|
| `poller.py` | detect new MRs, notify, spawn reviews |
| `reviewer.py` | orchestrate one MR review end to end |
| `engines/` | AI engines (claude, codex) behind one contract |
| `prompts/` | review methodology + shared skeptic persona |
| `fetch_mr.py` | GitLab diff → noise-filtered context file |
| `post_comment.py` | findings file → MR comments |
| `gitlab_client.py` / `slack_client.py` | thin REST wrappers (urllib) |
| `review_common.py` | pure functions (allowlist, noise, size, sort, position, …) |
| `sentinel_config.py` | config/state IO and defaults |
