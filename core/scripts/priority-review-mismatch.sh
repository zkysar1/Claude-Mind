#!/usr/bin/env bash
# Wrapper for priority-review-mismatch.py.
# Usage: echo '[{"asp_id":"","priority":"MEDIUM","score":22.1}, ...]' | priority-review-mismatch.sh
# Reads JSON array from stdin, writes JSON summary to stdout.
# History file: meta/priority-review-mismatch-history.yaml
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/priority-review-mismatch.py"
