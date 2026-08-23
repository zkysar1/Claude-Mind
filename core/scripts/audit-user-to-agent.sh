#!/usr/bin/env bash
# Audit [user]-only goals and reclassify matches to [agent, user].
# Dry-run by default; pass --apply to mutate. See audit-user-to-agent.py docs.
# Part (1) of . Pairs with capability-gate.py --evidence (Part 2).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/audit-user-to-agent.py" "$@"
