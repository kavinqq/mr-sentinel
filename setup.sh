#!/usr/bin/env bash
# mr-sentinel guided setup: config, sanity checks, first run, scheduling hints.
set -euo pipefail
cd "$(dirname "$0")"

echo "== mr-sentinel setup =="

# 1. config
if [ ! -f config.json ]; then
  cp config.example.json config.json
  chmod 600 config.json
  echo "-> created config.json (chmod 600)."
  echo "   Edit it now: gitlab_url, gitlab_token, review.project_map"
  echo "   Then re-run this script."
  exit 0
fi
chmod 600 config.json
echo "-> config.json present."

# 2. prerequisites
command -v python3 >/dev/null || { echo "!! python3 not found"; exit 1; }
command -v git >/dev/null     || { echo "!! git not found"; exit 1; }
ENGINE=$(python3 -c "import sentinel_config;print(sentinel_config.load_config()['review']['engine'])")
if [ "$ENGINE" = "claude" ]; then
  command -v claude >/dev/null || { echo "!! claude CLI not found (default engine)"; exit 1; }
  echo "-> claude CLI: $(claude --version 2>/dev/null | head -1)"
elif [ "$ENGINE" = "codex" ]; then
  command -v codex >/dev/null || { echo "!! codex CLI not found (engine=codex)"; exit 1; }
  echo "-> codex CLI: $(codex --version 2>/dev/null | head -1)"
fi

# 3. tests (offline)
echo "-> running unit tests..."
python3 -m unittest -q

# 4. token + clones sanity
python3 - <<'EOF'
import pathlib, sys
import gitlab_client, sentinel_config
cfg = sentinel_config.load_config()
user = gitlab_client.get_current_user(cfg["gitlab_url"], cfg["gitlab_token"])
print(f"-> GitLab token OK (user: {user['username']})")
missing = [p for p, local in cfg["review"]["project_map"].items()
           if not pathlib.Path(local).exists()]
if missing:
    print("!! local clone missing for:", ", ".join(missing)); sys.exit(1)
print(f"-> {len(cfg['review']['project_map'])} project clone(s) found")
EOF

# 5. initialize state (first run notifies nothing)
python3 poller.py
echo "-> state initialized."

# 6. scheduling hint
case "$(uname -s)" in
  Darwin) echo "Next: schedule with launchd — see deploy/launchd/ (60s interval)";;
  Linux)  echo "Next: schedule with cron or systemd — see deploy/cron/ and deploy/systemd/";;
  *)      echo "Next: schedule 'python3 poller.py' every minute with your scheduler";;
esac
echo "== setup complete =="
