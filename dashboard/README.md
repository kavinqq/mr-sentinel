# mr-sentinel dashboard

Local Django app over the review history (`sentinel.db`). Per-person performance
data, so it binds to `127.0.0.1` and every page needs a login.

```bash
dashboard/run.sh createsuperuser   # once: venv + Django db + your account
dashboard/run.sh                   # http://127.0.0.1:8765
```

| Page | What |
|---|---|
| 總覽 `/` | everyone's level, score, aspects, follow-ups; sync status; 增量 / 完整同步 buttons |
| 個人 `/people/<id>/` | score breakdown, aspect bars, monthly trend, follow-up bugs, every finding (links to the GitLab comment) with 改面向 / 標記誤判 / 恢復計分 |
| 評分設定 `/scoring/` | edit weights / thresholds as JSON → validated → new version, in force immediately |
| 原始資料 `/admin/` | browse every table (view-only) |

## Boundaries (why it is built this way)

- **Two databases.** Django's users/sessions/admin log live in `dashboard/dashboard.db`.
  `sentinel.db` belongs to the stdlib `history` package; Django never migrates it
  (`managed=False` + `routers.py`).
- **Raw tables are read-only, human tables append-only** — enforced in `models.py`.
  An override never edits a finding; it adds a `finding_reviews` row (who, when, why),
  and the latest one wins. Every scoring change is a new `scoring_configs` version.
- **Scores come from the core** (`history.score`), so the dashboard, the CLI report
  and the coding tutor can never disagree.
- **Sync buttons queue a request and start `python3 -m history run` detached**;
  the web request never syncs itself, and the job's lock prevents doubles.
- `MR_SENTINEL_DB=/path/to/sentinel.db` points both the dashboard and the jobs it
  starts at another file.

Tests: `dashboard/.venv/bin/python dashboard/manage.py test reviews`
