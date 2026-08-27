#!/usr/bin/env bash
# tree-lock.sh — advisory working-tree lock for co-resident Bodies ().
#
# Wrapper over tree_lock.py. NOT daemon-routed, deliberately: its primary caller
# is iteration-push.sh, which must work when the daemon is down (that is one of
# the states it exists to recover from). A daemon round-trip here would make the
# lock unavailable exactly when the tree is most likely to be contended.
#
# Usage:
#   tree-lock.sh acquire --reason "<what holds the tree>" [--ttl N] [--force WHY]
#   tree-lock.sh release
#   tree-lock.sh check          # the GATE: rc 0 = proceed, rc 1 = someone else holds it
#   tree-lock.sh status         # human read; NEVER refuses (always rc 0)
#
# EXIT CODES: 0 = proceed / acquired / released, 1 = refused (acquire, check),
# 2 = plumbing failure. `check` never returns 2 — for the gate, an indeterminate
# state must still mean PROCEED (see tree_lock.py's fail-safe section).
#
# Identity comes from MIND_SID, not the agent name: a worker Body and its
# reducer are both the same agent, so an agent comparison cannot separate them
# (same reasoning as unit-claim.sh).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/_paths.sh"

exec python3 "$HERE/tree_lock.py" "$@"
