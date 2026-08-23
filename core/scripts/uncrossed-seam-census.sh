#!/usr/bin/env bash
# uncrossed-seam-census.sh — which producer/consumer payload seams does NO test cross?
#
# Thin wrapper over uncrossed_seam_census.py (gap-117). Framework placement, not
# world/scripts: it takes a repo path and touches no domain resource, no named
# service and no credential — domain-free by the domain-leak-check.sh test — so
# it is git-tracked and rides the repo's commit flow like backend-cat.sh.
#
# Exit codes are the contract, so a caller never has to parse prose:
#   0  census ran and the positive control is alive — the verdict is meaningful
#   3  positive control DEAD (no test file names 2+ production classes): every
#      pair would read uncrossed, so the run is a parser artifact, not a finding
#   1  usage / plumbing error (missing source roots) — never a verdict
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# : under Git Bash on Windows, $(cd ... && pwd) returns POSIX form
# /c/... which Windows python3 misreads as drive C: plus a literal subdir c/,
# yielding FileNotFoundError on C:\c\...\uncrossed_seam_census.py. Convert to
# Windows-native form before exec. Linux/macOS lack cygpath and fall through
# with SCRIPT_DIR unchanged (POSIX paths work natively).
if command -v cygpath >/dev/null 2>&1; then
    SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR")"
else
    SCRIPT_DIR_NATIVE="$SCRIPT_DIR"
fi

exec python3 "$SCRIPT_DIR_NATIVE/uncrossed_seam_census.py" "$@"
