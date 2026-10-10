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
       4. size guard       pick depth by size: small → "lite" (1 gate), large → "deep" (3 gates)
       5. worktree         git fetch refs/merge-requests/<iid>/head → disposable worktree
       6. AI engine        engines/<engine>.run_review(context, mode → findings)   ← only AI step
       7. post             post_comment.py: inline discussion first, note fallback
       8. notify           optional Slack completion message
       9. auto-merge       if deep-clean + auto_merge_on_clean (a clean lite pass is re-run deep first):
                           merge pinned to the reviewed sha (unless draft/head moved/unmergeable/
                           CI not green/CI config but no pipeline), then :done: on the notification
      10. cleanup          worktree removed (finally-block, even on crash)

scheduler (second entry, every 15s)          — or long-lived with --socket
  └─ slack_bot.py         own flock (.lock-bot); polls Slack for mentions
       │                  (--socket: Socket Mode WebSocket — @mentions and button
       │                  clicks pushed in; a history tick on each connect catches up)
       ├─ buttons         click → Command → same authorize/rate limit → chat.update
       └─ appeal          💬 button → appeal.py (detached, shares the per-MR lock):
                          threads a human answered → engine.run_appeal → reply +
                          resolve accepted → summary → auto-merge rails if none open
       ├─ usage           bare mention -> the command list for that user
       ├─ settings        writes overrides.json (whitelisted keys only)
       └─ rerun           un-claim → delete its own unanswered comments → spawn
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

One headless session, whose depth the reviewer picks by MR size via the
`mode` argument (`prompts/review.md` carries a `__VETTING__` token the engine
fills per mode):

- **lite** (small MR, 1 gate): `claude -p <review.md>` with no subagent. The
  Opus scan self-vets and writes the findings file. Cheaper, slightly noisier.
- **deep** (large MR, 3 gates): `claude -p <review.md> --agents <skeptic>`.
  Gate 1 Opus scans → gate 2 dispatches the Sonnet skeptic subagent
  (`prompts/skeptic.md`, injected inline, no `~/.claude` install) exactly once
  → gate 3 the Opus session adjudicates the verdicts (rescuing real bugs the
  skeptic over-dropped) and writes the findings file itself.

The `skeptic_model` sits in the middle because only gate 1 (recall) and gate 3
(the final public decision) need the strongest model; the middle filter can be
cheap.

Note: do **not** add `--bare` — it restricts auth to `ANTHROPIC_API_KEY`,
silently moving subscription users onto metered API billing.

### codex engine (experimental)

Codex has no subagents, so the engine orchestrates:
pass 1 `codex exec` scan → candidates JSON (via `--output-last-message`),
pass 2 `codex exec` skeptic verdicts, then Python applies keep/drop
mechanically (no third AI call). `--sandbox read-only` throughout.

Both engines share `prompts/skeptic.md`, so vetting rules cannot drift.

## Slack control plane

Slack is polled, not subscribed to. Buttons (Block Kit), slash commands and the
Events API all require Slack to reach a public Request URL; a watcher living on
a laptop behind a VPN has none. So the interface is typed text:

- **A bare mention prints usage; anything else runs.** A command missing its
  value replies with the valid values, so discovery costs one message and no
  state. `commands.py` holds all of it as pure functions.
- **An emoji-reaction menu was built first and deleted.** It worked, but two
  things sank it: in a channel *every* message can be reacted to while only a few
  are buttons, so users react to the wrong message; and with nine number emoji
  the settings menu had to be truncated to nine of eleven keys. Removing it also
  deleted menu state, ownership, TTL/expiry, click de-duplication, and a
  reaction-ordering workaround (`reactions.add` acks before the append is
  visible, so back-to-back calls landed as 1️⃣3️⃣2️⃣4️⃣ and needed spacing out).
  Text has none of those failure modes.
- **One history call sees thread replies too.** `conversations.history` never
  returns replies, but a parent message carries `latest_reply`. Reading the last
  50 parents without `oldest` and following only the ones whose `latest_reply`
  moved keeps the steady state at one API call per tick.
- **At-most-once.** The read cursor advances *before* a command executes. A
  rerun that ran twice would burn model quota twice, and a message that crashes
  the dispatcher would otherwise be retried forever, spamming the channel.
- Usage text is rendered per permission, so a non-admin is never shown a command
  that would be refused.

### Two writers, one state file

`poller.py` is the **single writer** of `state.json`; the listener keeps its own
`bot_state.json` (cursor, rerun log). Independent flocks mean an
unsynchronised read-modify-write would otherwise let the bot clobber a `seen`
entry the poller just recorded, re-notifying or losing an MR.

The one place they must cooperate is adding a project: the bot cannot baseline
the new project's existing MRs without writing `state.json`. Instead it leaves a
timestamped `_baseline_pending` note in `overrides.json`, and the poller — which
already holds the lock and already has the MR list — does it on the next tick.
Timestamping makes the handoff idempotent *and* bounded: a note that fails to
clear can only re-silence MRs created before that instant, never a new one.
`overrides.json` itself has two writers, so all edits go through
`overrides.update()`, a locked read-modify-write.

### Overrides as a security boundary

`config.json` holds the tokens and stays hand-edited. Slack-side changes land in
`overrides.json` and are merged on top at load time, so `reset` is just
forgetting an override. `overrides.SETTABLE` whitelists both the settable keys
*and* the config paths they may touch, and values are re-validated on read, not
only on write — a chat message (or a mangled overrides file) can never reach
`gitlab_token`, repoint `gitlab_url`, or smuggle junk into a model argv slot.

## Why findings travel as files, not stdout

Headless sessions on real developer machines get their text output polluted
(hooks, output styles, plugins). Files are deterministic; parsing model
prose is not. The final message is used for nothing.

## State & idempotency

- `state.json` (poller-owned): `seen` (notified MR ids; entries still opened are
  never pruned, others age out after 7 days), `slack_ts` (message timestamps for
  reactions/threading), `mrs` (id → project/iid/title, so a Slack command can act
  on an MR — `slack_ts` alone cannot address one), and the last-poll facts
  `status` reports without touching GitLab.
- `bot_state.json` (listener-owned): read cursor, rerun log.
- `overrides.json`: Slack-side config changes + `_baseline_pending` + an audit
  trail of who changed what.
- The review claim marker is the `:eyes:` award emoji *on GitLab itself* —
  survives state resets, visible to humans, naturally idempotent
  (GitLab rejects duplicate awards from the same user).

## Module map

| Module | Responsibility |
|---|---|
| `poller.py` | detect new MRs, notify, spawn reviews |
| `reviewer.py` | orchestrate one MR review end to end (+ `spawn_detached`) |
| `slack_bot.py` | poll Slack (or listen on Socket Mode) for mentions and clicks, dispatch commands (IO only) |
| `socket_mode.py` | stdlib WebSocket client + Socket Mode envelope loop (ack-first, reconnect) |
| `history/` | review history in SQLite: schema+migrations (`db`), GitLab sync, follow-ups, scoring, AI category backlog (`python3 -m history`), 個人軌跡 (`trajectory`), team weekly / project risk numbers (`reports`) |
| `appeal.py` | re-judge findings the developer disputed; verdict replies, resolve, summary |
| `blocks.py` | Block Kit buttons: build, retire after a click, parse a click back to a Command (pure) |
| `commands.py` | parse / authorize / render usage, settings, status (pure) |
| `overrides.py` | Slack-settable config layer: whitelist, validate, merge |
| `engines/` | AI engines (claude, codex) behind one contract |
| `prompts/` | review methodology + shared skeptic persona |
| `fetch_mr.py` | GitLab diff → noise-filtered context file |
| `post_comment.py` | findings file → MR comments |
| `gitlab_client.py` / `slack_client.py` | thin REST wrappers (urllib) |
| `review_common.py` | pure functions (allowlist, noise, size, sort, position, …) |
| `sentinel_config.py` | config/state IO and defaults |
