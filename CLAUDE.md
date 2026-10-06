# mr-sentinel — notes for AI assistants

Self-hosted GitLab MR watcher: polls for new MRs, notifies Slack, and runs an
adversarial two-model AI code review (see README.md and docs/ARCHITECTURE.md).

## Ground rules

- **Stdlib only** — no pip dependencies. `urllib`, `subprocess`, `unittest` cover everything.
- **TDD** — run `python3 -m unittest` (offline, sub-second) before and after changes.
- **Scripts do plumbing, AI does judgment** — never delegate deterministic work
  (fetching, posting, dedup) to a model. Engine contract is file-based:
  `mr_context.json` in → `final_findings.json` out (see `engines/__init__.py`).
- Commit style: `[ tag ] description` (feat/fix/test/docs/chore).

## Deployment gotchas (hard-won)

- Schedulers (launchd/cron) run with a **minimal PATH**: plists must use absolute
  interpreter paths, and external CLIs are resolved via `engines.resolve_cli()`.
- `config.json`, `state.json`, `bot_state.json`, `overrides.json`,
  `本機使用說明.html`, `deploy/local/` are gitignored (secrets / machine-local);
  never commit them.
- After hand-editing `review.project_map` or `watch.*` in `config.json`, delete
  `state.json` so the baseline rebuilds (the poller refuses to build a baseline
  from a failed poll). Adding a project **via Slack** does not need this — it
  queues a timestamped `_baseline_pending` that the poller applies per project.
- `poller.py` is the only writer of `state.json`; `slack_bot.py` uses
  `bot_state.json`. Both write `overrides.json`, so always go through
  `overrides.update()` (locked read-modify-write), never `save()` directly.
- Slack-settable config is whitelisted in `overrides.SETTABLE` — that list is a
  security boundary (a chat message must never reach a token). Adding a key there
  needs a validator, not just a path.
- Changing `slack.channel_id` invalidates `state.json`'s `slack_ts` (timestamps
  are per-channel): clear `slack_ts`, keep `seen`, or old MRs get re-notified.
