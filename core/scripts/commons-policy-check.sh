#!/usr/bin/env bash
# core/scripts/commons-policy-check.sh - report the RESOLVED COMMONS_POLICY
# knowledge-egress dial, and gate on it.
#
# WHY THIS EXISTS (): the dial is the governing gate on several
# goals, and a `verification.preconditions[]` entry can only express it through
# predicate.py's ALLOWED_COMMAND_PREFIXES allowlist - a safety boundary that a
# stored precondition must not be able to step outside. A precondition written
# as `py -3 -c "import _paths; ..."` is NOT allowlisted, so it was never
# evaluated at all: the refusal returned passed:false and froze its goal in
# every selector permanently. This wrapper is the sanctioned allowlisted
# surface. Do NOT widen the allowlist to admit `py -3 -c` (guard-5859).
#
# RESOLVED, NOT DECLARED. _paths.COMMONS_POLICY reads the environment, then
# .env.local, then normalises: any value outside (private|selective|public)
# FAILS CLOSED to 'private' per world-contract.md Rule 4. So a grep of
# .env.local can disagree with what the framework actually enforces
# (bring-up-doctor.sh treats declared-vs-resolved divergence as a hard finding).
# Always ask _paths; never re-derive the chain.
#
# Usage:  commons-policy-check.sh [--permits-egress]
#   (no flag)         print the resolved policy; exit 0 if it resolved at all
#   --permits-egress  exit 0 only when the dial is 'selective' or 'public'
# Exit 2 = the policy could not be resolved (fails closed for the gate form).

set -uo pipefail

MODE="report"
case "${1:-}" in
  --permits-egress) MODE="gate" ;;
  -h|--help) sed -n '1,25p' "$0"; exit 0 ;;
  "") ;;
  *) echo "commons-policy-check: unknown flag '$1'" >&2; exit 2 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="py -3"
command -v py >/dev/null 2>&1 || PY="python3"

# cd into the script's own dir instead of exporting PYTHONPATH. MEASURED
# 2026-09-05 on Windows/Git-Bash: `PYTHONPATH=/c/...` (an MSYS path) reaches
# native python VERBATIM under the environment predicate-eval.sh builds
# (source _paths.sh + _platform.sh), and `import _paths` dies
# ModuleNotFoundError -- while the SAME script run from an interactive shell
# resolves fine. That asymmetry is the trap probe-with-canonical-code-path.md
# names: the canonical BINARY is not the canonical INVOCATION. For `-c`,
# python puts CWD on sys.path, so this needs no env var and no path
# translation on any platform.
POLICY="$(cd "$SCRIPT_DIR" && $PY -c 'import _paths; print(_paths.COMMONS_POLICY)' 2>/dev/null | tr -d '\r\n')"

if [ -z "$POLICY" ]; then
  echo "commons-policy-check: VERDICT=unresolved reason=could-not-import-_paths"
  exit 2
fi

case "$POLICY" in
  selective|public) PERMITS=yes ;;
  *)                PERMITS=no  ;;
esac

echo "commons-policy-check: VERDICT=resolved policy=$POLICY permits_egress=$PERMITS"

if [ "$MODE" = "gate" ]; then
  [ "$PERMITS" = "yes" ] && exit 0 || exit 1
fi
exit 0
