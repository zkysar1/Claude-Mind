#!/usr/bin/env bash
# core/scripts/repo-hygiene-sweep.sh
#
# Report-only git/GitHub hygiene sweep (, Phase A). See the Python
# script's docstring for the full design rationale.
#
# Thin wrapper. All logic lives in repo-hygiene-sweep.py; this exists so the
# sweep is invoked the same way as every other core/scripts sweep and so the
# date stamp is supplied by the CALLER rather than read from the clock inside
# the analysis code (which keeps the report path deterministic for a re-run and
# for tests).
#
# Usage:
#   repo-hygiene-sweep.sh                 # full estate, fetch --prune first
#   repo-hygiene-sweep.sh --no-fetch      # fast re-run, verdicts marked STALE
#   repo-hygiene-sweep.sh --repo <name>   # limit to one clone (repeatable)
#   repo-hygiene-sweep.sh --json          # summary as JSON
#
# PHASE A: nothing is ever deleted, pruned, or pushed. There is no --apply.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"

#  fix: under Git Bash on Windows, $(cd ... && pwd) returns POSIX
# form /c/... Windows python3 misinterprets that as drive C: with a literal
# subdir c/, yielding FileNotFoundError on C:\c\...\repo-hygiene-sweep.py.
# Convert to Windows-native form before exec. Linux/macOS lack cygpath and
# fall through with SCRIPT_DIR unchanged (POSIX paths work natively).
#
# This wrapper shipped WITHOUT the block and was caught the same hour by
# test_cygpath_wrapper_pattern.py, whose wrapper list is discovered dynamically
# -- a new direct-python wrapper joins the population the moment it lands. That
# is the corpus scanner working exactly as designed, and it is why the block is
# here rather than rediscovered on a Windows box weeks later.
if command -v cygpath >/dev/null 2>&1; then
    SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR")"
else
    SCRIPT_DIR_NATIVE="$SCRIPT_DIR"
fi

DATE_STAMP="$(date +%Y-%m-%d)"

# Pass --date only when the caller did not supply one, so an explicit
# --date on the command line still wins.
for a in "$@"; do
    if [ "$a" = "--date" ]; then DATE_STAMP=""; break; fi
done

# Single python3 invocation per python-invocation.md rule 3: inside a .sh
# wrapper that has sourced _paths.sh, the shim is on PATH and python3 is
# canonical. exec replaces the bash process -- output and exit pass through.
if [ -n "$DATE_STAMP" ]; then
    exec python3 "$SCRIPT_DIR_NATIVE/repo-hygiene-sweep.py" --date "$DATE_STAMP" "$@"
fi
exec python3 "$SCRIPT_DIR_NATIVE/repo-hygiene-sweep.py" "$@"
