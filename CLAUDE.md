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
- `config.json`, `state.json`, `本機使用說明.html`, `deploy/local/` are gitignored
  (secrets / machine-local); never commit them.
- After changing `review.project_map` or `watch.*`, delete `state.json` so the
  baseline rebuilds (the poller refuses to build a baseline from a failed poll).
