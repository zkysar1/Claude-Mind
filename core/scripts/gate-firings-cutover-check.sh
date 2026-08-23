#!/usr/bin/env bash
# Ordering gate for the gate-firings segmentation cutover ().
#
#   bash core/scripts/gate-firings-cutover-check.sh            # is the flip safe?
#   bash core/scripts/gate-firings-cutover-check.sh --attest   # this box carries the seam
#
# Exit 0 = SAFE (every live agent attested) · 2 = UNSAFE · 3 = error.
# Fail-CLOSED: anything unreadable reports UNSAFE, never SAFE.
#
# : this is now a THIN WRAPPER over the parametrized engine — the
# 279-line gate-firings-cutover-check.py it used to exec is retired, and its
# constants live in store-cutover-check.py STORES["gate_firings"]. The CLI here
# is unchanged (same flags, same exit codes), so every caller and every prose
# reference to this path keeps working; it is kept rather than folded away for
# exactly that reason.
#
# WHAT THE MIGRATION ADDS: --attest stops being the only path. A box that has
# committed an iteration after pulling the seam is now proven from git ancestry
# + consumer byte-identity, with the hand-stamp kept only as the fallback. This
# cutover is the one whose hand-stamp chore starved 3 days (,
# rb-8202) — the incident that motivated derived attestation, on the one
# cutover the tool did not yet cover.
#
# WHAT IT DOES NOT LOSE: this gate's local predicate was never "match
# origin/main", it was "the three consumers CALL firings_paths()" — strictly
# stronger, and not expressible as byte-identity. It travels as
# STORES["gate_firings"]["seam_symbols"] (a one-element set since  —
# widened for `utilization`, whose consumers use different parts of one reader
# API; "calls >= 1" over one element is the singular predicate, unchanged) and
# is enforced in two places (working-tree files locally, origin/main fleet-wide).
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"

# : under Git Bash on Windows, `$(cd ... && pwd)` yields POSIX /c/...
# which Windows python3 reads as drive C: plus a literal `c/` subdir, so the
# script path resolves to C:\c\...\<name>.py and dies FileNotFoundError.
# Convert to Windows-native form before exec. Linux/macOS have no cygpath and
# fall through unchanged (POSIX paths work natively there).
if command -v cygpath >/dev/null 2>&1; then
    SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR")"
else
    SCRIPT_DIR_NATIVE="$SCRIPT_DIR"
fi

exec python3 "$SCRIPT_DIR_NATIVE/store-cutover-check.py" \
    --store gate_firings "$@"
