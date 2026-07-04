# Contributing

## Ground rules

- **Stdlib only.** No pip dependencies — `urllib`, `json`, `subprocess`,
  `unittest` cover everything this tool needs. PRs adding dependencies need a
  very good reason.
- **TDD.** Every behavior change lands with a failing test first. Run the
  suite with `python3 -m unittest` (offline, sub-second).
- **Scripts do plumbing, AI does judgment.** Anything a script can do with
  100% correctness (fetching, posting, dedup, formatting) must not be
  delegated to a model. Keep the engine contract file-based:
  `mr_context.json` in, `final_findings.json` out.
- **Small focused files.** One responsibility per module; pure functions go
  in `review_common.py` where they are trivially testable.

## Adding an engine

Create `engines/your_engine.py` exposing:

```python
def run_review(work_dir, context_file, output_file, repo_dir, review_cfg) -> int: ...
def label(review_cfg) -> str: ...
```

Register it in `engines/__init__.py`, reuse `prompts/skeptic.md` for the
vetting pass so the keep/drop rules stay consistent, and add tests with the
subprocess layer mocked (see `test_codex_engine.py` for the pattern).

## Commit style

`[ tag ] short description` — tags: `feat` / `fix` / `test` / `docs` / `ver`.
