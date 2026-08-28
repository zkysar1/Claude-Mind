#!/usr/bin/env bash
# Load state-update scripted-audit digest — returns path only if not already
# in context. Follows load-iteration-close-digest.sh pattern for context-reads
# integration. The digest (core/config/state-update-audit-digest.md) carries
# Steps 8.8-8.10 of aspirations-state-update, extracted () to keep
# that hot-path SKILL.md under the 65,536 B injection budget.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"

python3 "$CORE_ROOT/scripts/context-reads.py" check-file \
    "$CONFIG_DIR/state-update-audit-digest.md"
