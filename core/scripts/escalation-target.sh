#!/usr/bin/env bash
# escalation-target.sh — bash front-end for _escalation_target.resolve() +
# source_flag(). .
#
# WHY THIS EXISTS
# ---------------
# Python canaries route their escalation aspiration through
# `_escalation_target.resolve()` so a framework file promoted downstream files
# into an aspiration that EXISTS there. The shell canaries had no equivalent, so
# they kept a literal `` — the UPSTREAM deployment's recurring-infra
# queue, present in no other deployment. Downstream, every such add-goal fails
# `aspiration_not_found`, and because these callers log the failure as data
# rather than raising, nothing escalates the escalation failure. Same defect
# class the python module was written to kill, just on the other side of the
# shell boundary.
#
# USAGE
#   bash escalation-target.sh            # "<asp-id> <source>"  (default, one line)
#   bash escalation-target.sh --asp      # "<asp-id>"
#   bash escalation-target.sh --source   # "world" | "agent"
#   bash escalation-target.sh --json     # {"aspiration","resolved_via","source_flag"}
#
# CALLER PATTERN (both values, no process substitution):
#   _et="$(bash "$SCRIPT_DIR/escalation-target.sh")"
#   ASPIRATION="${_et%% *}"; SOURCE="${_et##* }"
#
# FAIL-OPEN: any resolution error prints the upstream default (" world")
# and exits 0. That is deliberate — it reproduces the PRE-FIX behaviour exactly,
# so a broken resolver can never be worse than the literal it replaced. stderr
# is NOT suppressed: a real error stays visible rather than becoming a silent
# zero (verify-before-assuming rule 4).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MODE="pair"
case "${1:-}" in
    --asp)    MODE="asp" ;;
    --source) MODE="source" ;;
    --json)   MODE="json" ;;
    "")       ;;
    *) echo "usage: escalation-target.sh [--asp|--source|--json]" >&2; exit 2 ;;
esac

# _paths.sh puts the python3 shim on PATH (python-invocation.md). Inside a .sh
# that has sourced it, plain `python3` is the sanctioned form.
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || true

# guard-165: values reach python through the ENVIRONMENT; the source is
# single-quoted so the shell never interpolates into the program text.
_out="$(ET_MODE="$MODE" ET_DIR="$SCRIPT_DIR" python3 -c '
import json, os, sys
sys.path.insert(0, os.environ["ET_DIR"])
from _escalation_target import resolve, source_flag
from _paths import AGENT_DIR, CORE_ROOT, WORLD_DIR

asp, via = resolve(CORE_ROOT, WORLD_DIR, AGENT_DIR)
src = source_flag(asp, WORLD_DIR, AGENT_DIR)
mode = os.environ["ET_MODE"]
if mode == "asp":
    print(asp)
elif mode == "source":
    print(src)
elif mode == "json":
    print(json.dumps({"aspiration": asp, "resolved_via": via, "source_flag": src}))
else:
    print(asp, src)
')" || _out=""

if [ -z "$_out" ]; then
    case "$MODE" in
        asp)    _out="asp-115" ;;
        source) _out="world" ;;
        json)   _out='{"aspiration":"asp-115","resolved_via":"fallback:wrapper-failed","source_flag":"world"}' ;;
        *)      _out="asp-115 world" ;;
    esac
fi

# Pair mode MUST emit exactly two whitespace-separated fields. Callers split with
# ${_et%% *} / ${_et##* }, and on a ONE-token line those two expansions return the
# SAME token — so SOURCE silently becomes an aspiration id. aspirations-add-goal.sh
# does reject that (invalid_source), but every caller routes a filing failure to a
# non-fatal WARN, so the escalation would drop SILENTLY: precisely the bug this
# whole mechanism exists to fix, reintroduced one layer up. Both branches above
# already emit two fields; this guard makes that a checked postcondition rather
# than an invariant maintained by inspection. (fresh-eyes F-1, .)
if [ "$MODE" = "pair" ]; then
    # shellcheck disable=SC2086
    set -- $_out
    [ "$#" -eq 2 ] || _out="asp-115 world"
fi

printf '%s\n' "$_out"
