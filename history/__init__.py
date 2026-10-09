"""Review history: every AI finding, per developer, in a local SQLite file.

The foundation for per-person statistics (security / correctness / code quality
/ code smell / performance), follow-up bugs on shipped features, a transparent
junior/mid/senior score, and later the coding-tutor feature.

Ownership rules (the dashboard in dashboard/ must respect them):
- this package (stdlib only) owns the schema and its migrations (db.py);
- *raw* tables (people, mrs, mr_files, findings, followups, sync_state) are
  written only by this package, from GitLab — the source of truth, so any
  machine can rebuild the db and nothing depends on local reviews/ dirs;
- *human* tables (finding_reviews, scoring_configs, sync_requests) are where
  people act: overrides are appended, never edited in place, and raw rows are
  never modified by them — so every number can be traced back.

    python3 -m history run       # the scheduled job: requests + sync + followups + classify
    python3 -m history report    # per-person summary in the terminal
"""
