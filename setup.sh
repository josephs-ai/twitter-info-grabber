#!/usr/bin/env bash
# One-time setup. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

say "1/5  Python virtual environment"
# --system-site-packages so the GTK bindings pywebview needs stay visible.
[ -d .venv ] || python3 -m venv --system-site-packages .venv
.venv/bin/pip install --quiet --upgrade pip setuptools wheel

say "2/5  Dependencies"
.venv/bin/pip install --quiet -r requirements.txt

say "3/5  Browser engine (~150MB, one time)"
.venv/bin/python -m playwright install chromium

say "4/5  API key"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "  Created .env — put your Anthropic API key in it before judging."
  echo "  Get one at https://console.anthropic.com/"
else
  echo "  .env already exists, leaving it alone."
fi

say "5/5  Database"
.venv/bin/python -c "from tracker import db; db.connect().close()" && echo "  tracker.db ready"
./run accounts import

cat <<'DONE'

Setup complete. Next:

  ./run login                 sign in to a THROWAWAY X account (not your own)
  ./run collect --all         first collection pass, ~25 min for 54 accounts
  ./daily.sh                  the full pipeline: collect, dedup, judge, digest
  ./app                       desktop app

Edit seeds.txt to change who is tracked, then ./run accounts import.
DONE
