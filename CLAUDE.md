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
- Buttons need `slack_bot.py --socket` (Socket Mode, `slack.app_token`) running;
  `reviewer.buttons_enabled()` keys off that token so no unanswerable button is
  posted. Only one socket listener per Slack app — Slack load-balances events
  across connections, so a second machine silently eats clicks. Button values
  are user-controllable: they only ever name a verb in `slack_bot.BUTTON_VERBS`
  and still pass `commands.authorize`.
- `sentinel.db` (review history) is per-person data: gitignored, never commit.
  Its schema changes only by appending to `history/db.py` `MIGRATIONS` — never
  edit a shipped migration. Raw tables are written only by `history/`, from
  GitLab; human input goes through the append-only `finding_reviews` /
  `scoring_configs` / `sync_requests` tables. Tests must never touch the real
  db (`BotHarness` mocks `history_sync.sync_mr`; use a temp path elsewhere).
- Scores are **out of 10, higher is better** (`history/score.py`): each item
  (5 finding categories + follow-ups) starts at 10, the total is 10 − Σ item
  deductions, and a level is a gate (`min_score` plus optional `max_high` /
  `max_fix_mr` / `min_clean_rate`). The formula lives in versioned
  `scoring_configs`; change it there (dashboard 評分設定), not in code.
  Every recompute goes through `history.snapshot.record` so it lands in the
  score log. A db that was never fully scanned gets `history/scan.py` (past
  window, every project) on its first `run`; `scan_history.py` runs it by hand.
- `dashboard/` is the one place pip dependencies are allowed (own venv,
  `dashboard/requirements.txt`). It never migrates `sentinel.db` (models are
  `managed=False`, see `routers.py`), raw models are read-only and human models
  append-only in `models.py`, and scores always come from `history.score`.
  Run its tests from `dashboard/`: `.venv/bin/python manage.py test reviews`.
- Changing `slack.channel_id` invalidates `state.json`'s `slack_ts` (timestamps
  are per-channel): clear `slack_ts`, keep `seen`, or old MRs get re-notified.
