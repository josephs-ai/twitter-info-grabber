#!/usr/bin/env bash
# Unix convenience wrapper. The portable path is: ./run daily
# (or on Windows: run.cmd daily)
set -uo pipefail
cd "$(dirname "$0")"
exec ./run daily "$@"
