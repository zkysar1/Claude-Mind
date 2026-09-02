#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Auto-derived skill quality scoring wrapper (Step 8.76).
#
# Subcommands:
#   score  — derive 4 dimensions + accept cost_awareness, write under lock.
#            Daemon: POST /v1/skill-quality/score (JSON body).
#   derive — print auto-derived grades without writing.
#            Daemon: GET  /v1/skill-quality/derive (query params).
#
# Both endpoints return CLI-byte-compat stdout; the response body is
# emitted verbatim (no translation).
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Dispatch subcommand -----------------------------------------------------
SUBCMD="${1-}"
if [ -z "$SUBCMD" ]; then
    echo "Error: subcommand required (score | derive)" >&2
    exit 1
fi
shift

# --- Parse args (shared signal args + score-specific args) -------------------
SKILL=""; GOAL=""
OUTCOMES_MET=""; OUTCOMES_TOTAL=""
EPISODE_CHAIN_COUNT=""; GUARDRAIL_VIOLATIONS=""
COST_AWARENESS=""
SAFETY_OVERRIDE=""; COMPLETENESS_OVERRIDE=""
EXECUTABILITY_OVERRIDE=""; MAINTAINABILITY_OVERRIDE=""
DRY_RUN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skill)                    SKILL="${2-}";                    shift $(( $# >= 2 ? 2 : 1 ));;
        --goal)                     GOAL="${2-}";                     shift $(( $# >= 2 ? 2 : 1 ));;
        --outcomes-met)             OUTCOMES_MET="${2-}";             shift $(( $# >= 2 ? 2 : 1 ));;
        --outcomes-total)           OUTCOMES_TOTAL="${2-}";           shift $(( $# >= 2 ? 2 : 1 ));;
        --episode-chain-count)      EPISODE_CHAIN_COUNT="${2-}";      shift $(( $# >= 2 ? 2 : 1 ));;
        --guardrail-violations)     GUARDRAIL_VIOLATIONS="${2-}";     shift $(( $# >= 2 ? 2 : 1 ));;
        --cost-awareness)           COST_AWARENESS="${2-}";           shift $(( $# >= 2 ? 2 : 1 ));;
        --safety-override)          SAFETY_OVERRIDE="${2-}";          shift $(( $# >= 2 ? 2 : 1 ));;
        --completeness-override)    COMPLETENESS_OVERRIDE="${2-}";    shift $(( $# >= 2 ? 2 : 1 ));;
        --executability-override)   EXECUTABILITY_OVERRIDE="${2-}";   shift $(( $# >= 2 ? 2 : 1 ));;
        --maintainability-override) MAINTAINABILITY_OVERRIDE="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --dry-run)                  DRY_RUN=1;                       shift;;
        *) shift;;
    esac
done

# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

# =============================================================================
# derive — GET /v1/skill-quality/derive
# =============================================================================
if [ "$SUBCMD" = "derive" ]; then
    QUERY=""
    _append_q() { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="$1"; }
    [ -n "$SKILL" ]                && _append_q "skill=$(rt_url_encode "$SKILL")"
    [ -n "$GOAL" ]                 && _append_q "goal=$(rt_url_encode "$GOAL")"
    [ -n "$OUTCOMES_MET" ]         && _append_q "outcomes_met=$(rt_url_encode "$OUTCOMES_MET")"
    [ -n "$OUTCOMES_TOTAL" ]       && _append_q "outcomes_total=$(rt_url_encode "$OUTCOMES_TOTAL")"
    [ -n "$EPISODE_CHAIN_COUNT" ]  && _append_q "episode_chain_count=$(rt_url_encode "$EPISODE_CHAIN_COUNT")"
    [ -n "$GUARDRAIL_VIOLATIONS" ] && _append_q "guardrail_violations=$(rt_url_encode "$GUARDRAIL_VIOLATIONS")"

    rc=0
    rt_call GET /v1/skill-quality/derive --query "$QUERY" || rc=$?
    case $rc in
        0) exit 0;;
        2) exit 1;;
        3)
            if rt_try_autospawn; then
                rc=0
                rt_call GET /v1/skill-quality/derive --query "$QUERY" || rc=$?
                if [ "$rc" = "0" ]; then exit 0; fi
            fi
            rt_no_daemon_error "skill-quality-score.sh";;
        *) exit $rc;;
    esac

# =============================================================================
# score — POST /v1/skill-quality/score
# =============================================================================
elif [ "$SUBCMD" = "score" ]; then
    # Judge provenance is resolved HERE, in the judge's own process, and
    # forwarded in the body. This is the Step 8.76 per-goal call site, so it is
    # the DOMINANT producer of skill-quality evaluations; the daemon writer it
    # reaches (skill_evaluate._score_write) cannot read these from its own
    # environment without stamping its spawning session's values on every
    # record fleet-wide (guard-2480, ).
    rt_judge_provenance

    # Build JSON body from parsed args. Pass values via env (guard-165:
    # never interpolate bash variables into python source text).
    BODY="$(_SQS_SKILL="$SKILL" \
        _SQS_JUDGE="$RT_JUDGE_MODEL" \
        _SQS_HARNESS="$RT_JUDGE_HARNESS" \
        _SQS_GOAL="$GOAL" \
        _SQS_COST="$COST_AWARENESS" \
        _SQS_OMET="$OUTCOMES_MET" \
        _SQS_OTOT="$OUTCOMES_TOTAL" \
        _SQS_ECC="$EPISODE_CHAIN_COUNT" \
        _SQS_GV="$GUARDRAIL_VIOLATIONS" \
        _SQS_SO="$SAFETY_OVERRIDE" \
        _SQS_CO="$COMPLETENESS_OVERRIDE" \
        _SQS_EO="$EXECUTABILITY_OVERRIDE" \
        _SQS_MO="$MAINTAINABILITY_OVERRIDE" \
        _SQS_DRY="$DRY_RUN" \
        $(rt_python_launcher) -c '
import json, os
body = {}
def s(k): return os.environ.get(k, "")
def i(k):
    v = s(k)
    return int(v) if v else None
if s("_SQS_SKILL"): body["skill"] = s("_SQS_SKILL")
if s("_SQS_GOAL"):  body["goal"]  = s("_SQS_GOAL")
if s("_SQS_COST"):  body["cost_awareness"] = s("_SQS_COST")
v = i("_SQS_OMET")
if v is not None: body["outcomes_met"] = v
v = i("_SQS_OTOT")
if v is not None: body["outcomes_total"] = v
v = i("_SQS_ECC")
if v is not None: body["episode_chain_count"] = v
v = i("_SQS_GV")
if v is not None: body["guardrail_violations"] = v
if s("_SQS_SO"): body["safety_override"] = s("_SQS_SO")
if s("_SQS_CO"): body["completeness_override"] = s("_SQS_CO")
if s("_SQS_EO"): body["executability_override"] = s("_SQS_EO")
if s("_SQS_MO"): body["maintainability_override"] = s("_SQS_MO")
if s("_SQS_JUDGE"):   body["judge_model"] = s("_SQS_JUDGE")
if s("_SQS_HARNESS"): body["harness"]     = s("_SQS_HARNESS")
if s("_SQS_DRY") == "1": body["dry_run"] = True
print(json.dumps(body))
')"

    rc=0
    rt_call POST /v1/skill-quality/score --body-string "$BODY" || rc=$?
    case $rc in
        0) exit 0;;
        2) exit 1;;
        3)
            if rt_try_autospawn; then
                rc=0
                rt_call POST /v1/skill-quality/score --body-string "$BODY" || rc=$?
                if [ "$rc" = "0" ]; then exit 0; fi
            fi
            rt_no_daemon_error "skill-quality-score.sh";;
        *) exit $rc;;
    esac
else
    echo "Error: unknown subcommand '$SUBCMD' (expected score | derive)" >&2
    exit 1
fi
