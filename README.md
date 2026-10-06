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
                      └─ 💬  and you can drive it from that Slack channel:
                             re-review, change settings, pause, add projects
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
- Optional: a Slack bot token for notifications (`chat:write`, `reactions:write`)
  and, to also *control* it from Slack, `channels:history` (or `groups:history`
  for a private channel) + `reactions:read` — plus the bot invited to the channel

## Quick start

```bash
git clone https://github.com/kavinqq/mr-sentinel.git && cd mr-sentinel

cp config.example.json config.json
$EDITOR config.json           # gitlab_url, gitlab_token, review.project_map
chmod 600 config.json

python3 -m unittest           # sanity: 229 tests, no network needed
python3 poller.py             # first run: marks existing MRs seen, notifies nothing

# try one MR end to end (nothing posted with --dry-run):
python3 reviewer.py --project your-group/your-repo --iid 123 --mr-id 456 --dry-run

# then schedule the poller every minute — see deploy/ for
# launchd (macOS), cron, and systemd templates.

# optional: also schedule slack_bot.py (~15s) to take commands from Slack
python3 slack_bot.py          # first run: baselines the read cursor, runs nothing
```

## Configuration

Everything lives in `config.json` (gitignored). Minimal setup is GitLab-only;
leave the `slack` block empty to disable notifications entirely.

| Key | Meaning |
|---|---|
| `gitlab_url` | Your GitLab base URL (self-hosted or gitlab.com) |
| `gitlab_token` | PAT with `api` scope (read MRs, post comments, award emoji) |
| `slack.bot_token` / `channel_id` | Optional; enables new-MR messages, 👀 reactions, completion pings, and the command listener |
| `slack.webhook_url` | Simpler Slack alternative (incoming webhook): messages work, reactions don't, commands don't. Bot token wins when both are set |
| `slack.admin_user_ids` | Who may change settings from Slack. Empty falls back to `mention_user_ids` — set it explicitly, or cc'ing a teammate on notifications silently grants them admin |
| `slack.display_name` / `icon_emoji` | Optional branding for bot messages; needs the `chat:write.customize` scope, and is only sent when set |
| `watch.group_ids` / `path_prefixes` | Optional: poll whole GitLab groups (one API call each) and notify for every member project; reviews still run only for `project_map` entries |
| `review.project_map` | **The review allowlist**: `"group/project": "/local/clone/path"` — only mapped projects are reviewed |
| `review.language` | Language for review comments (`en`, `zh-TW`, `ja`, …) |
| `review.engine` | `claude` (default) or `codex` (experimental) |
| `review.max_changed_files` / `max_diff_lines` | Tier boundary: within both limits an MR gets a 1-gate `lite` review (scan only); over either limit it escalates to a 3-gate `deep` review (scan → vet → adjudicate) |
| `review.auto_merge_on_clean` | `false` (default) or `true`: when a review finds **zero** problems, auto-merge the MR — but never a draft, a non-mergeable MR, or one with a non-green pipeline |
| `review.claude.model` / `skeptic_model` / `effort` | Scanner + adjudicator model (gates 1 & 3), skeptic subagent model (gate 2), reasoning effort |
| `review.codex.model` / `skeptic_model` | Codex models (empty = CLI default) |

## Driving it from Slack

Tag the bot in the notification channel. A bare tag prints usage; anything else
runs. A command missing a value answers with the valid values, so you never have
to remember them.

```
you:  @mr-sentinel
bot:  *mr-sentinel* — tag 我加上指令就會執行
      • `status` — 目前狀態、進行中的 review、最近結果
      • `rerun !481 deep` — 重跑 review
      • `set effort high` — 改設定 …

you:  @mr-sentinel set effort
bot:  `effort` — 推理強度 (目前 *medium*)
      可以是: low / medium / high / xhigh / max

you:  @mr-sentinel rerun !481 deep
bot:  ♻️ 重跑中: g/app !481 (deep)
      (清掉 1 則沒人回覆的舊留言)
```

| Command | Who | What |
|---|---|---|
| `status` | anyone | Last poll, reviews running right now, recent results, effective settings |
| `rerun [<project>!<iid>] [auto\|lite\|deep]` | anyone (rate-limited) | Un-claim and review again. Inside an MR's notification thread the target is implied |
| `set <key> <value>` | admin | `engine`, `effort`, `language`, `model`, `skeptic`, `automerge`, `maxfiles`, `maxlines`, `timeout` |
| `reset <key>\|projects\|all` | admin | Forget the Slack-side value, back to `config.json` |
| `pause [30m\|2h]` / `resume` | admin | Hold notifications and reviews; the backlog is delivered on resume, nothing is dropped |
| `projects [add\|rm] …` | admin | Change the watch list; `add` verifies both the GitLab path and your local clone |

Notes on how this works, because it shapes what is possible:

- **It polls Slack; Slack never calls in.** Buttons, slash commands and the
  Events API all need a public Request URL, which a laptop-hosted watcher has
  not got — so the interface is plain text.
- An emoji-reaction menu was built first and removed. Two problems killed it:
  every message in a channel can be reacted to but only a few are "buttons", so
  people react to the wrong message; and there are only nine number emoji, which
  capped how many settings could be offered. Text has neither limit — `set` now
  lists all eleven settings, and `rerun` lists every open MR as a copy-a-line
  command with no widget state, no owner, and no expiry to manage.
- Settings changed from Slack live in `overrides.json`, layered on top of
  `config.json` at load time. `config.json` stays the hand-edited source of
  truth (it holds the tokens), and a whitelist means no Slack message can ever
  reach a token or `gitlab_url`.
- The listener is a **second** scheduler entry (`slack_bot.py`, ~15s) with its
  own lock, so a Slack outage cannot slow the MR poll loop.

## How the engines work

| | `claude` (default) | `codex` (experimental) |
|---|---|---|
| Mechanism | One headless session; a `deep` review injects the skeptic as an inline subagent via `--agents` (a `lite` review skips it) | `codex exec` passes: scan (+ skeptic for `deep`); verdicts applied mechanically in Python |
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
> — 🤖 mr-sentinel AI review (scanned by claude-opus-4-8, vetted by sonnet, adjudicated by claude-opus-4-8)

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
On subscription plans: no extra money, just usage quota. Cost scales with the
change: a small MR gets a 1-gate `lite` review (one Opus scan). A large MR
(> `max_changed_files` or `max_diff_lines`) escalates to a 3-gate `deep` review
— Opus scan → Sonnet vet → Opus adjudication — because a bigger change hides
more bugs. A giant MR that can't finish in `review_timeout_seconds` reports
"review did not finish" and is left for a human.

**Does it re-review when new commits are pushed?**
Not automatically — one review per MR, claimed via the 👀 emoji (idempotent).
But you can ask for another round from Slack (`@bot rerun`), which drops the
claim, deletes its own previous comments that nobody replied to, and runs again.
Automatic re-review on push is on the roadmap.

## Roadmap

- `post_mode: draft` (draft notes a human publishes)
- Automatic re-review on new pushes
- Per-project config overrides
- GitHub PR support (the GitLab client is already isolated)

## License

[MIT](LICENSE)
