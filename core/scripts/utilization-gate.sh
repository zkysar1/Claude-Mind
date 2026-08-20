#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# PreToolUse[Skill] hook — programmatic utilization enforcement.
# Fires before every skill invocation. Only acts when the skill is
# aspirations-state-update. If retrieval-session.json has utilization_pending=true,
# auto-runs utilization-feedback.sh --all-unknown as a fallback (no times_noise
# pollution; just records utilization_method=all_unknown so phase-4-26-gate
# still flags the goal as needing positive signal but the retrieval-session
# is no longer pending).
#
# SCOPE (narrowed 2026-07-25,  — read this before trusting the hook):
# This covers ONLY direct Skill(aspirations-state-update) invocations — /boot,
# consolidation, and ad-hoc callers that bypass iteration-close.sh. It does NOT
# cover the autonomous loop's hot path, and a PreToolUse[Skill] matcher
# structurally CANNOT: the hot path runs `Bash: iteration-close.sh --phase
# state-update` and stopped invoking the skill. Measured across 5 agents'
# skill-invocation ledgers: aspirations-state-update fired 15 times out of
# 12,325 total invocations (0.12%); bravo 0 of 2,552. The previous header
# claimed this hook "ensures the system NEVER has zero utilization data" — that
# guarantee was false for ~all closes, and was masked only by phase-4-26-gate.py
# being independently inert ().
#
# The hot-path equivalent is iteration-close.sh `_repair_utilization_pending`,
# called from do_state_update immediately BEFORE phase-4-26-gate.sh. Keep the
# two in behavioral agreement (same tier order, same --confidence) — divergence
# means the same manifest scores differently depending on which path ran.
#
# The --all-unknown fallback replaced the original --all-noise backstop, which
# over many iterations pushed unattested-but-relevant nodes toward retirement
# (tree.py distill `has_feedback` gate consumes times_noise — see audit
# 2026-05-07 / 0/655 leaves with times_helpful>0).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
# _platform.sh converts MSYS paths to Windows paths for python3 interop.
# Without this, $PROJECT_ROOT is /c/... which Windows Python cannot open.
source "$CORE_ROOT/scripts/_platform.sh"

# Extract skill name from hook stdin JSON (same pattern as context-reads-skill-gate.sh)
skill_info=$(python3 -c "
import sys,json
d = json.load(sys.stdin)
ti = d.get('tool_input',{})
sk = ti.get('skill','')
print(sk)
" 2>/dev/null) || true

# Only act on aspirations-state-update
if [ "$skill_info" != "aspirations-state-update" ]; then
    exit 0
fi

# Check if agent is bound
if [ -z "${MIND_AGENT:-}" ]; then
    exit 0
fi

SESSION_FILE="$(retrieval_session_path "$(agent_dir "$MIND_AGENT")")"  # body-aware, 

# No session file = no retrieval happened for this goal — pass silently
if [ ! -f "$SESSION_FILE" ]; then
    exit 0
fi

# Check if utilization is still pending
pending=$(python3 -c "
import json,sys
try:
    with open(sys.argv[1],'r') as f:
        d = json.load(f)
    goal = d.get('goal_id','')
    pending = d.get('utilization_pending', False)
    print(f'{pending}|{goal}')
except:
    print('False|')
" "$SESSION_FILE" 2>/dev/null) || true

is_pending="${pending%%|*}"
goal_id="${pending#*|}"

if [ "$is_pending" != "True" ]; then
    exit 0
fi

# Utilization pending — try heuristic inference first (Phase 1 of cognitive-core curation).
# --infer classifies retrieved items as helpful/noise/unknown by matching their
# distinctive_tokens against the execution diary. Requires schema_version >= 2
# retrieval-session.json; older sessions exit 4 and fall back to --all-unknown.
# stderr is preserved — it's the only signal when the heuristic misfires.
# --confidence balanced (min_distinctive=1) matches the hot-path helper in
# iteration-close.sh. It was `conservative` (>=2) until 2026-07-25 ():
# the hot path swapped to balanced under C.2 because conservative starved
# positive signal (0 helpful across 320 active guardrails, 2026-05-09 audit),
# but this hook was never updated, so the two backstops scored the same manifest
# differently. Change both or neither.
echo "[utilization-gate] Phase 4.26 was skipped for $goal_id — attempting --infer" >&2
# : record that the --infer fallback path fired. `|| true` keeps the
# hook exit code at 0 (PreToolUse contract); stderr is preserved so real bugs
# in trigger-firings surface alongside the [utilization-gate] echos above.
bash "$CORE_ROOT/scripts/trigger-firings.sh" record utilization-gate.infer --context "{\"goal_id\":\"$goal_id\"}" || true
set +e
bash "$CORE_ROOT/scripts/utilization-feedback.sh" --goal "$goal_id" --infer --confidence balanced >/dev/null
rc=$?
set -e
if [ "$rc" -eq 4 ]; then
    echo "[utilization-gate] --infer unavailable (schema v1) — falling back to --all-unknown" >&2
    # : surface the schema-v1 fallback so we can detect lingering v1 sessions.
    bash "$CORE_ROOT/scripts/trigger-firings.sh" record utilization-gate.schema_v1 --context "{\"goal_id\":\"$goal_id\"}" || true
    bash "$CORE_ROOT/scripts/utilization-feedback.sh" --goal "$goal_id" --all-unknown >/dev/null || true
elif [ "$rc" -ne 0 ]; then
    echo "[utilization-gate] --infer exited $rc — falling back to --all-unknown" >&2
    bash "$CORE_ROOT/scripts/trigger-firings.sh" record utilization-gate.infer_error --context "{\"goal_id\":\"$goal_id\",\"rc\":$rc}" || true
    bash "$CORE_ROOT/scripts/utilization-feedback.sh" --goal "$goal_id" --all-unknown >/dev/null || true
fi

exit 0
