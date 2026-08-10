#!/usr/bin/env bash
# One full cycle: collect -> dedup -> judge -> digest.
# Safe to run repeatedly; every stage is idempotent.
set -uo pipefail
cd "$(dirname "$0")"

# cron gets a bare environment — no API key. Load it from .env (gitignored)
# so the judge stage works unattended.
[ -f .env ] && set -a && . ./.env && set +a

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "[$(date -Is)] WARNING: ANTHROPIC_API_KEY unset — judge stage will be skipped."
fi

log() { echo "[$(date -Is)] $*"; }

log "collecting"
./run collect --all --scrolls 5 --overlap 4 --headless || log "collect had failures (continuing)"

# Discovery runs on every cycle, in small batches. Both are browsing-heavy, so
# they are paced rather than exhaustive — coverage builds up over days.
log "mining conversations"
./run replies --limit 4 --scrolls 3 || log "replies failed (continuing)"

log "harvesting follow graph"
./run suggest --seeds 2 --scrolls 5 || log "suggest failed (continuing)"

log "resolving links"
./run links --limit 40 || log "links failed"

log "counting amplification"
./run amplify || log "amplify failed"

log "stitching threads"
./run threads --apply || log "threads failed"

log "dedup"
./run dedup --apply || log "dedup failed"

log "judging"
./run judge --limit 60 || log "judge failed"

log "digest"
./run digest

log "done"
