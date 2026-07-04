# mr-sentinel

**Self-hosted AI code review sentinel for GitLab MRs.**

mr-sentinel watches your GitLab for new merge requests and reviews them with AI —
on your machine, on your existing AI subscription, with an adversarial two-model
pipeline that kills false positives before anything gets posted.

```
new MR opened
  └─ 👀  claims the MR (award emoji, so authors know a review is coming)
      └─ scan    : a strong model reads the diff + the checked-out source
          └─ vet : an independent skeptic model tries to REFUTE every finding
              └─ post : surviving findings become MR comments, severity-sorted,
                        one problem per comment — scripts post, the AI never does
                  └─ ✅  optional Slack notification when done
```

## Why this instead of an AI-review SaaS?

- **Your code never leaves your infra.** The pipeline talks only to *your*
  GitLab and *your* local AI CLI. No third-party review service, no telemetry.
- **Runs on the subscription you already pay for.** The default engine drives
  the Claude Code CLI (`claude -p`), so reviews consume your existing
  Claude subscription — not a separately metered API. A Codex CLI engine
  (ChatGPT subscription) is included as an experimental alternative.
- **Adversarial vetting, not AI monologue.** Every candidate finding is
  cross-examined by a second, independent model prompted to *refute* it
  ("when in doubt, drop"). Published false positives are the most annoying
  failure mode of AI review — this is the guard against it.
- **Two tokens and go.** A GitLab token (+ optionally a Slack bot token).
  No skills to install, no plugins, no database: prompts live in this repo
  and are injected inline at runtime.
- **Read-only by construction.** Reviews run in a disposable `git worktree`;
  your working copies are never checked out, modified, or even touched.

## Requirements

- Python 3.10+ (stdlib only — zero pip dependencies)
- `git`, and a local clone of every project you want reviewed
- [Claude Code CLI](https://claude.com/claude-code) logged in (default engine),
  and/or the Codex CLI for the experimental `codex` engine
- A GitLab personal access token with `api` scope
- Optional: a Slack bot token (`chat:write`, `reactions:write`) for notifications

## Quick start

```bash
git clone https://github.com/YOU/mr-sentinel.git && cd mr-sentinel

cp config.example.json config.json
$EDITOR config.json           # gitlab_url, gitlab_token, review.project_map
chmod 600 config.json

python3 -m unittest           # sanity: 54 tests, no network needed
python3 poller.py             # first run: marks existing MRs seen, notifies nothing

# try one MR end to end (nothing posted with --dry-run):
python3 reviewer.py --project your-group/your-repo --iid 123 --mr-id 456 --dry-run

# then schedule the poller every minute — see deploy/ for
# launchd (macOS), cron, and systemd templates.
```

## Configuration

Everything lives in `config.json` (gitignored). Minimal setup is GitLab-only;
leave the `slack` block empty to disable notifications entirely.

| Key | Meaning |
|---|---|
| `gitlab_url` | Your GitLab base URL (self-hosted or gitlab.com) |
| `gitlab_token` | PAT with `api` scope (read MRs, post comments, award emoji) |
| `slack.bot_token` / `channel_id` | Optional; enables new-MR messages, 👀 reactions, completion pings |
| `review.project_map` | **The allowlist**: `"group/project": "/local/clone/path"` — only mapped projects are reviewed |
| `review.language` | Language for review comments (`en`, `zh-TW`, `ja`, …) |
| `review.engine` | `claude` (default) or `codex` (experimental) |
| `review.max_changed_files` / `max_diff_lines` | Size guard: oversized MRs are skipped with a warning instead of burning budget |
| `review.claude.model` / `skeptic_model` / `effort` | Scanner model, skeptic subagent model, reasoning effort |
| `review.codex.model` / `skeptic_model` | Codex models (empty = CLI default) |

## How the engines work

| | `claude` (default) | `codex` (experimental) |
|---|---|---|
| Mechanism | One headless session; the skeptic runs as an inline subagent via `--agents` | Two `codex exec` passes; verdicts applied mechanically in Python |
| Billing | Claude subscription | ChatGPT/Codex subscription |
| Sandboxing | Allowed-tools list + disposable worktree | `--sandbox read-only` |

Both engines implement one contract: read `mr_context.json`, write
`final_findings.json`. Anything that can do that can be an engine —
see `engines/__init__.py`.

The vetting rules live in a single file (`prompts/skeptic.md`) shared by both
engines, so "what counts as a real finding" never drifts between them.

## What a comment looks like

> 🔴 [High] Portfolio lookup keyed by rank collides on ties
>
> `buildPortfolios` keys the map by `row.rank`, but ranks are not guaranteed
> unique (equal PnL rates share a rank). With two rows at rank 7, the later one
> overwrites the earlier — the "view portfolio" modal then shows **another
> participant's holdings**. Suggest keying by participant id instead.
>
> — 🤖 mr-sentinel AI review (scanned by claude-opus-4-8, vetted by sonnet)

One problem per comment, severity-sorted (🔴 high → 🟠 medium → 🟡 low),
inline on the exact diff line whenever the position resolves.

## Security & privacy

- `config.json` (all tokens) is gitignored; the `.gitignore` itself is
  force-committed so the protection travels with the repo.
- Your clones are sacred: the reviewer only ever runs `git fetch`
  (refs/objects only) and checks out into a throwaway worktree that is
  removed afterwards — even if the review crashes.
- The AI is told to write exactly one file (the findings JSON) and runs with
  a restricted tool set; the scripts do all GitLab/Slack writes.
- No analytics, no phoning home. Read `docs/SECURITY.md` for details.

## FAQ

**It posted nothing on my MR — is it broken?**
Probably not: a clean MR *should* produce zero comments. The skeptic drops
anything it can refute, and "when in doubt, drop" is by design. Check
`reviews/<mr-id>/final_findings.json` to see what was considered.

**How much does a review cost?**
On subscription plans: no extra money, just usage quota. A ~100-line MR takes
roughly 5 minutes and one Opus + one Sonnet pass. Oversized MRs are skipped
by the size guard.

**Does it re-review when new commits are pushed?**
Not yet — one review per MR, claimed via the 👀 emoji (idempotent). Re-review
on push is on the roadmap.

## Roadmap

- `post_mode: draft` (draft notes a human publishes)
- Re-review on new pushes
- Per-project config overrides
- GitHub PR support (the GitLab client is already isolated)

## License

[MIT](LICENSE)
