# Security & Privacy

## Data flow

Your code moves between exactly two parties you already trust:

1. **Your GitLab** (self-hosted or gitlab.com) — via its REST API, using your token.
2. **Your AI CLI** (Claude Code / Codex) — running locally under your own account.

There is no third-party service, no telemetry, no analytics, and nothing is
uploaded anywhere else. What your AI vendor sees is governed by the CLI and
subscription you already use for daily coding.

## Secrets

- All tokens live in `config.json`, which is **gitignored**. The `.gitignore`
  is force-committed so the protection survives cloning and global-gitignore
  quirks. Never commit `config.json`; `setup.sh` enforces `chmod 600`.
- GitLab token: `api` scope is required (read MRs, award emoji, post
  comments). If you prefer splitting, use a `read_api` token for the poller
  and an `api` token for the reviewer — both read the same config key today,
  so splitting requires a small patch.
- Slack bot: `chat:write` + `reactions:write` only. Optional feature.

## Your working copies are never touched

The reviewer needs MR source code for context, but developers' clones often
hold uncommitted work. Therefore:

- Only `git fetch` runs against your clone — it updates refs/objects and
  never touches the working tree or index.
- The checkout happens in a **disposable `git worktree`** under
  `reviews/<mr-id>/wt`, removed in a `finally` block even when a review
  crashes mid-flight.

## Containing the AI

- The prompt restricts the AI to writing exactly one file (the findings
  JSON); everything it reads is the context file plus the disposable
  worktree.
- claude engine: runs with an explicit `--allowedTools` list
  (Read/Grep/Glob/Task/Write) — no Bash, no Edit, no network tools.
- codex engine: runs under `--sandbox read-only`; even the findings are
  returned via the CLI's `--output-last-message` mechanism and written to
  disk by Python.
- All GitLab/Slack writes (comments, emoji, messages) are performed by
  reviewed, unit-tested Python — the AI cannot post anything by itself.

## Blast radius of a hostile MR

Reviewing means *reading* attacker-supplied diffs with an LLM, so prompt
injection is conceivable. Mitigations: the AI has no write tools beyond one
file in a throwaway directory, no shell, and no ability to post; the worst
realistic outcome is a bad or missing review comment. The skeptic pass also
has to be convinced before anything reaches humans.

## Reporting

Found a vulnerability? Open a private issue or contact the maintainer
directly rather than filing a public report first.
