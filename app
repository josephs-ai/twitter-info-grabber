#!/usr/bin/env bash
# Launch the desktop app.
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
exec .venv/bin/python -c "import sys; from tracker.app import main; sys.exit(main())" "$@"
