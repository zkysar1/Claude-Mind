#!/usr/bin/env bash
# Census of standing owner/user directives held as guardrails: which name an enforcing
# gate in the optional `enforced_by` field and which are honor-system ().
# Read-only. The population definition, verdicts and the cited-by hint are documented
# in guardrail_enforcement_census.py and core/config/conventions/reasoning-guardrails.md.
#
# Usage: guardrail-enforcement-census.sh [--json] [--honor-system-only]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"

# Capture straight into a file, THEN check rc. A pipe would replace the reader's exit
# code with the pipe's (guard-1150/guard-696), turning a daemon failure into a
# confident census of nothing. NO `2>&1` and no `2>/dev/null` (guard-1963/guard-659):
# the file is a payload another program parses, so stderr must neither poison it nor
# be hidden -- the reader's diagnostics reach the terminal unmerged and the rc decides.
ACTIVE_FILE="$(mktemp)"
trap 'rm -f "$ACTIVE_FILE"' EXIT

if ! bash "$SCRIPT_DIR/guardrails-read.sh" --active > "$ACTIVE_FILE"; then
    echo "[guardrail-enforcement-census] guardrails-read.sh --active FAILED -- not printing a census. Its own diagnostics are on stderr above, unmerged." >&2
    exit 1
fi

if [ ! -s "$ACTIVE_FILE" ]; then
    echo "[guardrail-enforcement-census] guardrails-read.sh --active returned EMPTY with rc=0 -- refusing to print a census (an empty read reads as a clean one)." >&2
    exit 1
fi

python3 "$SCRIPT_DIR/guardrail_enforcement_census.py" --guardrails-json-file "$ACTIVE_FILE" "$@"
