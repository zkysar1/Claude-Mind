#!/usr/bin/env bash
# domain-leak-exempt: Gate D injection wrapper — references the GATE_D_* flag
# namespace and the _gate_d.py companion by design (functional experiment seam).
#
# gate-d-inject.sh — Gate D arm assignment + commons-pattern retrieval wrapper.
# Binding spec: Gate D methodology §4.2 / §9.3 (RATIFIED 2026-06-10). Thin
# wrapper over _gate_d.py: resolves paths, forwards --goal-id / --goal-text /
# --category, and GUARANTEES a single JSON object on stdout. On ANY failure it
# emits the fail-closed control record {"arm":"A","status":"error","patterns":[]}
# so the seam (aspirations-execute Step 5e) never breaks goal execution.
# ALWAYS exits 0. _gate_d.py reads GATE_D_CORPUS_PATH from the environment.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Source _paths.sh for path/env consistency with the rest of the framework
# (exports PROJECT_ROOT etc). Non-fatal if it cannot load — _gate_d.py only
# needs GATE_D_CORPUS_PATH, which is set in the environment by .claude/settings.json.
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || true

GOAL_ID=""; GOAL_TEXT=""; CATEGORY=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --goal-id)   GOAL_ID="${2:-}";   shift $(( $# >= 2 ? 2 : 1 )) ;;
    --goal-text) GOAL_TEXT="${2:-}"; shift $(( $# >= 2 ? 2 : 1 )) ;;
    --category)  CATEGORY="${2:-}";  shift $(( $# >= 2 ? 2 : 1 )) ;;
    *) shift ;;
  esac
done

# Pass every value as argv (NOT interpolated into Python source) — guard-165.
# py -3 launches the file directly (guard-368: no heredoc; guard-581: pass the
# .py path explicitly). On Windows the bash-agent-inject hook supplies PATH.
OUT="$(py -3 "$SCRIPT_DIR/_gate_d.py" --goal-id "$GOAL_ID" --goal-text "$GOAL_TEXT" --category "$CATEGORY" 2>/dev/null)"
if [[ -z "$OUT" ]]; then
  echo '{"arm":"A","status":"error","patterns":[]}'
else
  printf '%s\n' "$OUT"
fi
