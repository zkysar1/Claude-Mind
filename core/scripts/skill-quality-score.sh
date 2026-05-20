#!/usr/bin/env bash
# Auto-derived skill quality scoring wrapper (, rb-428).
# Thin forwarder to skill-quality-score.py. See the Python for design
# rationale and dimension derivations.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/skill-quality-score.py" "$@"
