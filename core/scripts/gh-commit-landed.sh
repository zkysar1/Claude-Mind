#!/usr/bin/env bash
# core/scripts/gh-commit-landed.sh - has <sha> reached <base-ref> in a REMOTE
# GitHub repo?
#
# WHY THIS EXISTS (): "the fix is deployed" gates are commonly
# written as a raw `gh api .../compare/... | grep -q ...` inside a
# `verification.preconditions[]` command. That is not in predicate.py's
# ALLOWED_COMMAND_PREFIXES - a safety boundary a stored precondition must not
# be able to step outside - so it was never evaluated at all: the refusal
# returned passed:false, and the goal was filtered from every selector
# PERMANENTLY while both re-probe sweeps re-derived the same false every 2h.
# This wrapper is the sanctioned allowlisted surface. Do NOT add `gh` to the
# allowlist (guard-5859: the refusal is a redirect, not a capability wall).
#
# REMOTE, not local. The sibling `commit-reachability.py` answers the same
# question against a LOCAL checkout and is the better tool when one exists;
# this probe exists for repos that are not checked out on the asking box,
# which is the normal case across a fleet.
#
# THREE-WAY, and the fail direction is the whole design (rb-611, and the same
# contract commit-reachability.py holds): a false LANDED ends an investigation
# and lets a goal proceed against a build that cannot contain the fix, while a
# false NOT-LANDED costs one more look. So every probe that cannot RUN - gh
# absent, unauthenticated, network down, repo/sha unknown - resolves to
# UNVERIFIED and exits non-zero. Read the printed VERDICT, not just the code.
#
# Usage:  gh-commit-landed.sh <owner/repo> <base-ref> <sha>
#   e.g.  gh-commit-landed.sh acme/widget-service main a1b2c3d
# Exit 0 = LANDED (compare status is `identical` or `behind`, i.e. <sha> is an
#          ancestor of <base-ref>). Exit 1 = NOT LANDED. Exit 2 = UNVERIFIED
#          (could not measure) or a usage error.

set -uo pipefail

REPO="${1:-}"; BASE="${2:-}"; SHA="${3:-}"

case "${1:-}" in
  -h|--help) sed -n '1,32p' "$0"; exit 0 ;;
esac

if [ -z "$REPO" ] || [ -z "$BASE" ] || [ -z "$SHA" ]; then
  echo "gh-commit-landed: usage: gh-commit-landed.sh <owner/repo> <base-ref> <sha>" >&2
  exit 2
fi
case "$REPO" in
  */*) ;;
  *)   echo "gh-commit-landed: VERDICT=unverified reason=repo-must-be-owner/name got='$REPO'"; exit 2 ;;
esac

if ! command -v gh >/dev/null 2>&1; then
  echo "gh-commit-landed: VERDICT=unverified repo=$REPO base=$BASE sha=$SHA reason=gh-not-installed"
  exit 2
fi

# `compare/<base>...<head>` reports the head's position RELATIVE TO base:
#   identical - same commit
#   behind    - head is an ancestor of base  => it LANDED
#   ahead     - base is an ancestor of head  => not landed
#   diverged  - neither contains the other   => not landed
OUT="$(gh api "repos/$REPO/compare/$BASE...$SHA" --jq .status 2>&1)"
RC=$?
STATUS="$(printf '%s' "$OUT" | tr -d '\r' | tail -1)"

if [ "$RC" -ne 0 ]; then
  echo "gh-commit-landed: VERDICT=unverified repo=$REPO base=$BASE sha=$SHA gh_rc=$RC detail=$(printf '%s' "$OUT" | tr '\n' ' ' | cut -c1-300)"
  exit 2
fi

case "$STATUS" in
  identical|behind)
    echo "gh-commit-landed: VERDICT=landed repo=$REPO base=$BASE sha=$SHA compare_status=$STATUS"
    exit 0 ;;
  ahead|diverged)
    echo "gh-commit-landed: VERDICT=not-landed repo=$REPO base=$BASE sha=$SHA compare_status=$STATUS"
    exit 1 ;;
  *)
    echo "gh-commit-landed: VERDICT=unverified repo=$REPO base=$BASE sha=$SHA reason=unrecognised-compare-status got='$STATUS'"
    exit 2 ;;
esac
