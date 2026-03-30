#!/usr/bin/env bash
# meta-log-append.sh — Append JSON from stdin to meta/meta-log.jsonl
# Usage: echo '{"event":"..."}' | meta-log-append.sh
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/meta-yaml.py" log
