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
- Optional: a Slack bot token for notifications (`chat:write`, `reactions:write`,
  `files:write` for verdict images)
  and, to also *control* it from Slack, `channels:history` (or `groups:history`
  for a private channel) + `reactions:read` — plus the bot invited to the channel
- Optional, for **buttons**: Socket Mode on, an app-level token (`xapp-…`,
  `connections:write`), Interactivity on, and the `app_mentions:read` scope with
  the `app_mention` bot event subscribed (see *Buttons* below)

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
| `history.since` / `history.db_path` | Optional: how far back the review history reads (default `2026-07-01T00:00:00Z`) and where `sentinel.db` lives (default: repo dir) |
| `slack.app_token` | Optional `xapp-` token; turns on buttons. Its presence is the switch: without it no button is ever posted (a click nobody answers just errors) |
| `slack.webhook_url` | Simpler Slack alternative (incoming webhook): messages work, reactions don't, commands don't. Bot token wins when both are set |
| `slack.admin_user_ids` | Who may change settings from Slack. Empty falls back to `mention_user_ids` — set it explicitly, or cc'ing a teammate on notifications silently grants them admin |
| `assets/verdict/<tier>/` | Optional fun: the completion message attaches a random image from one folder — `no_bug/` (0 findings), `high_grade/` (worst is low), `medium_grade/` (worst is medium), `low_grade/` (any high). Drop in any jpg/png/gif/webp to add more. Needs a bot token with `files:write`; an empty folder or missing scope falls back to text |
| `slack.display_name` / `icon_emoji` | Optional branding for bot messages; needs the `chat:write.customize` scope, and is only sent when set |
| `watch.group_ids` / `path_prefixes` | Optional: poll whole GitLab groups (one API call each) and notify for every member project; reviews still run only for `project_map` entries |
| `review.project_map` | **The review allowlist**: `"group/project": "/local/clone/path"` — only mapped projects are reviewed |
| `review.language` | Language for review comments (`en`, `zh-TW`, `ja`, …) |
| `review.engine` | `claude` (default) or `codex` (experimental) |
| `review.max_changed_files` / `max_diff_lines` | Tier boundary: within both limits an MR gets a 1-gate `lite` review (scan only); over either limit it escalates to a 3-gate `deep` review (scan → vet → adjudicate) |
| `review.auto_merge_on_clean` | `false` (default) or `true`: when a **deep** review finds **zero** problems, auto-merge the MR and mark the Slack notification `:done:` (falls back to `:white_check_mark:`). A clean lite review is first confirmed by a deep pass. Never merges a draft, a head that moved since the review, a non-mergeable MR, a non-green pipeline, or a project with CI config but no pipeline |
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

### Buttons (Socket Mode)

With `slack.app_token` set and `slack_bot.py --socket` connected (it keeps a
`.socket-alive` heartbeat fresh; without one, messages go out as plain text), the bot's own
messages carry buttons, so the common case needs no typing at all:

| Message | Buttons |
|---|---|
| AI Review 完成 | 🔁 修好了，重審 (depth picked by MR size; type `rerun !N deep` to force deep) · 💬 已留言,我覺得不用修 (only when comments were posted) |
| 申訴結果 (findings still open) | 🔁 修好了，重審 · 💬 已留言,我覺得不用修 |
| review did not finish | 🔁 再試一次 |
| auto-merge blocked by new commits | 🔁 重審最新 commit |

A click is turned back into the typed command (`rerun g/app!481 deep`) and goes
through the same permission check and hourly rerun budget. A permission refusal
is shown only to the person who clicked; a rerun refusal (budget, unknown MR) is
answered in the thread exactly like the typed command. Once it runs, the buttons are replaced by
"♻️ @who 已觸發重審", so a message can only be pressed once. Typed commands keep
working, and arrive instantly over the same socket.

**💬 已留言,我覺得不用修 (appeal).** Reply under an AI comment on GitLab with
why it needs no fix, then press the button (or `@bot appeal !N`). The AI reads
every thread where someone replied after its last word, checks the argument
against the code, and answers in that thread: ✅ accepted → the thread is
resolved; ⚠️ rejected → the reason, thread stays open (reply again and press
again). Unclear cases are accepted — the developer has context the model lacks.
When nothing is left open and `auto_merge_on_clean` is on, the normal auto-merge
rails apply, pinned to the sha the last *deep* review covered (a lite review,
or code pushed after the review, is never merged this way). Appeals share the
hourly rerun budget.

Slack app setup (once): **Socket Mode** → enable, generate an app-level token
with `connections:write` → `slack.app_token`. **Interactivity & Shortcuts** →
on (no URL needed in Socket Mode). **Event Subscriptions** → on, bot event
`app_mention`; add scope `app_mentions:read`; reinstall the app. Then run the
listener as a long-lived process (`deploy/launchd/com.example.mr-sentinel-bot-socket.plist`)
*instead of* the 15s one. Run only one listener per app: Slack spreads socket
events across every open connection.

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

## Review history (per-person stats)

`python3 -m history` keeps every AI finding in a local SQLite file
(`sentinel.db`, created on first run, gitignored — it is per-person data):

```bash
python3 scan_history.py          # first time: every project's MRs of the past 90 days
python3 scan_history.py --dry-run   # only count what it would read
python3 -m history rate         # AI grades each reviewed MR 1-5 per category (needs GitLab)
python3 -m history evaluate     # AI-written strengths / weaknesses per person
python3 -m history report       # per person: 8 items × 5, total out of 40, level
```

- **The first scheduled `run` on a db that was never scanned does the scan
  itself** (state `initial_scan_at`), so a new machine needs no extra step;
  after that, runs are incremental from each project's cursor.
- **Source of truth is GitLab**, not local files: findings a rerun deleted,
  developer replies, appeal verdicts and resolves are all there, so any
  machine can rebuild the db. A review and a rerun also record their MR
  immediately; `deploy/launchd/com.example.mr-sentinel-history.plist` runs the
  full job daily as the safety net.
- **面向 (category)**: security / correctness / performance / code_quality /
  code_smell. New reviews tag each comment with an invisible marker; older
  findings are filed by a cheap batched model call (`history classify`).
- **Follow-up bugs** (heuristic, labelled as such): a fix-type MR touching the
  same file within 30 days, or the AI flagging that file again — pinned on the
  most recent earlier feature MR that touched it.
- **Level**: `(Σ severity weight × category multiplier + Σ follow-up weight) / reviewed MRs`,
  lower is better; appeal-accepted and human-excluded findings do not count;
  under 5 reviewed MRs there is no level. Weights and thresholds are versioned
  rows in `scoring_configs` (v1 defaults are uncalibrated placeholders).
- Ownership: `history/` owns the schema (append-only migrations). People act
  only through the append-only `finding_reviews`, `scoring_configs` and
  `sync_requests` tables — the dashboard writes nothing else.

**Dashboard** (`dashboard/`, Django 5.2 LTS in its own venv — the core stays
stdlib-only): `dashboard/run.sh createsuperuser` once, then `dashboard/run.sh`
→ <http://127.0.0.1:8765>. Team overview, per-person pages (aspects, monthly
trend, follow-ups, every finding linked to its GitLab comment), 改面向 / 標記誤判
overrides with an audit trail, versioned scoring config, sync buttons. See
`dashboard/README.md`.

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
