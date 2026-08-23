#!/usr/bin/env bash
# check-releases-current.sh — seed-preflight publishability check #7.
#
# Verifies that RELEASES.json's newest entry matches the version SSOT
# (mind_api/src/__init__.py __version__). A promotion must not ship a version
# that release.sh never recorded — that would mean the frontier's history is
# out of sync with the code it is about to promote.
#
# NOTE (omni H2/Q1): RELEASES.json is NOT seeded downstream — this check runs at
# the SOURCE against the SOURCE's own RELEASES.json. It does not require the file
# to travel with the seed.
#
# Exit: 0 PASS / 1 FAIL / 2 script error (seed-preflight contract).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || { echo "ERROR: failed to source _paths.sh" >&2; exit 2; }
INIT_PY="$PROJECT_ROOT/mind_api/src/__init__.py"
RELEASES_JSON="$PROJECT_ROOT/RELEASES.json"

[[ -f "$INIT_PY" ]] || { echo "script error: $INIT_PY not found" >&2; exit 2; }
# `|| true` neutralizes pipefail when no __version__ line matches, so the
# empty-check below emits the exit-2 script-error diagnostic instead of a bare
# errexit abort (which the aggregator would misclassify as FAIL not ERROR).
CURRENT="$(grep -E '^__version__' "$INIT_PY" | sed -E 's/.*"([^"]+)".*/\1/' || true)"
[[ -n "$CURRENT" ]] || { echo "script error: could not read __version__" >&2; exit 2; }

if [[ ! -f "$RELEASES_JSON" ]]; then
  # RELEASES.json is a FRONTIER-ONLY artifact: release.sh writes it, and only the
  # frontier cuts releases (core/config/compatibility.yaml promotion_chain). A
  # non-frontier source — a seed mirror (no world overlay) or a provisioned
  # downstream — legitimately lacks it; the version SSOT (__version__) is
  # authoritative there. So only a FRONTIER FAILs on a missing RELEASES.json;
  # every other role treats it as N/A and PASSes. This is what lets a clean
  # seed->downstream (PPE->prod) promotion pass WITHOUT --skip-preflight. self_role
  # is read from the world overlay — the same source promote-to-upstream.sh /
  # check-upstream.sh resolve role from.
  SELF_ROLE=""
  if [[ -n "${WORLD_DIR:-}" && -f "$WORLD_DIR/config/compatibility.yaml" ]]; then
    SELF_ROLE="$(grep -E '^[[:space:]]*self_role:' "$WORLD_DIR/config/compatibility.yaml" 2>/dev/null \
      | head -1 | sed -E 's/.*self_role:[[:space:]]*"?([A-Za-z]+)"?.*/\1/' || true)"
  fi
  if [[ "$SELF_ROLE" == "frontier" ]]; then
    echo "FAIL: frontier source missing RELEASES.json (run release.sh to establish release history)"
    exit 1
  fi
  echo "PASS: non-frontier source (self_role='${SELF_ROLE:-seed-mirror}') — RELEASES.json N/A; version SSOT __version__=$CURRENT is authoritative"
  exit 0
fi

# Parse-or-fail (M1): a malformed RELEASES.json is a hard FAIL, not a silent pass.
set +e
NEWEST="$(py -3 "$SCRIPT_DIR/_release_lib.py" seed-latest "$RELEASES_JSON" 2>&1)"
RC=$?
set -e
if [[ $RC -ne 0 ]]; then
  echo "FAIL: RELEASES.json unreadable/malformed: $NEWEST"
  exit 1
fi

if [[ "$NEWEST" != "$CURRENT" ]]; then
  echo "FAIL: RELEASES.json newest entry ($NEWEST) != __version__ ($CURRENT)."
  echo "      Cut a release with release.sh so the history matches the code before promoting."
  exit 1
fi

echo "PASS: RELEASES.json newest entry matches __version__ ($CURRENT)"
exit 0
