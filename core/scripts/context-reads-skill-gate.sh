#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# PreToolUse[Skill] hook — gate AND record skill invocations.
# Reads JSON from stdin (tool_input.skill), checks context-reads tracker.
# Exit 0 = allow skill (and record it), Exit 2 = block (already in context).
#
# Combined gate+record because PostToolUse does not fire for the Skill tool
# (Skill injects content into the conversation stream rather than returning
# a traditional tool result).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

# Extract skill name and session_id from hook stdin JSON
skill_info=$(python3 -c "
import sys,json
d = json.load(sys.stdin)
ti = d.get('tool_input',{})
sk = ti.get('skill','')
sid = d.get('session_id','')
print(f'{sid}|{sk}')
" 2>/dev/null) || true

session_id="${skill_info%%|*}"
skill_name="${skill_info#*|}"

if [ -z "$skill_name" ]; then
    exit 0  # No skill name — allow
fi

# Loop-orchestrator exemption (): the autonomous aspirations loop
# re-invokes Skill(aspirations) — and its aspirations-* sub-skills — every
# iteration to drive the heartbeat. Once the dedup gate became functional
# ( resolves the bound agent from session_id and passes it as
# MIND_AGENT to context-reads.py below), a tracked-path hit returns exit 2 on
# that per-iteration re-invocation, which KILLS the loop ( confirmed).
# These skills MUST always be allowed to re-invoke. This is an INTENTIONAL
# scope decision — NOT a parse-error fail-open (cf. guard-487, which governs
# the error-path semantics of suppression gates, not deliberate exemptions).
#
# `worker-loop` is the BODY-side loop orchestrator (Mind/Body split, ).
# It is structurally identical to `aspirations` — Phase 5 re-enters via
# `Skill(worker-loop)` every work unit — but it was created AFTER the list
# above and was never added, so it was blocked on every re-entry. It does not
# die the way 's loop did (the model keeps following the copy already
# in context), which is exactly what made this invisible: the loop looks
# healthy while the SKILL.md on disk is never re-read. Consequence measured
# 2026-08-06 on cc-07 — a Phase -0.3 pull step committed at 03:09 had not
# fired once by 04:14 across FOUR observed re-entries, leaving that worker 22
# commits behind with no path to self-heal short of a restart. Any edit to
# worker-loop/SKILL.md was unreachable by every running worker.
orchestrator_exempt=0
case "$skill_name" in
    aspirations|aspirations-*|worker-loop) orchestrator_exempt=1 ;;
esac

# Resolve the bound agent from session_id for per-agent telemetry ().
# This PreToolUse[Skill] hook is invoked directly by Claude Code, which does NOT
# inject MIND_AGENT (only Bash *tool* calls get it via bash-agent-inject), so
# _paths.sh leaves AGENT_DIR empty and the per-agent telemetry below would
# silently no-op for every agent. The hook DOES carry session_id (parsed above),
# so resolve the bound agent via the canonical session-binding resolver.
#
# ORDER-CRITICAL: this MUST stay BEFORE `source _platform.sh`. _platform.sh
# exports MSYS_NO_PATHCONV=1, under which session-binding-read.sh resolves to
# EMPTY on Git Bash (verified : MSYS_NO_PATHCONV breaks the resolver).
# Resolving here, before that export, returns the agent correctly; setting
# AGENT_DIR now (an MSYS path) also lets _platform.sh's existing
# `if [ -n "$AGENT_DIR" ]` branch convert it to the Windows path the telemetry
# python needs to open(). Fail-open: any failure leaves AGENT_DIR empty and the
# telemetry block no-ops exactly as before.
if [ -z "${AGENT_DIR:-}" ] && [ -n "$session_id" ]; then
    _resolved_agent="$(bash "$CORE_ROOT/scripts/session-binding-read.sh" "$session_id" 2>/dev/null || true)"
    if [ -n "$_resolved_agent" ]; then
        AGENT_NAME="$_resolved_agent"
        AGENT_DIR="$(agent_dir "$_resolved_agent")"
    fi
    unset _resolved_agent
fi

# Convert MSYS paths to Windows paths BEFORE constructing skill_path.
# Python's Path.resolve() mishandles MSYS /c/... paths, producing C:/c/...
source "$CORE_ROOT/scripts/_platform.sh"

# Construct the SKILL.md path that would be injected
skill_path="$PROJECT_ROOT/.claude/skills/$skill_name/SKILL.md"

if [ ! -f "$skill_path" ]; then
    exit 0  # Skill file doesn't exist — allow (harness will handle error)
fi

sid_arg=""
if [ -n "$session_id" ]; then
    sid_arg="--session-id $session_id"
fi

# Use gate subcommand — it exits 0 (allow) for untracked AND out-of-scope paths,
# exits 2 (block) only for tracked paths. The &&/|| idiom captures exit codes
# safely under set -e (commands in &&/|| chains are exempt from errexit).
MIND_AGENT="${AGENT_NAME:-}" python3 "$CORE_ROOT/scripts/context-reads.py" gate $sid_arg "$skill_path" >/dev/null 2>&1 && gate_rc=0 || gate_rc=$?

if [ "$gate_rc" -eq 2 ]; then
    if [ "$orchestrator_exempt" -eq 1 ]; then
        # Orchestrator skill already tracked — allow the re-invocation (loop
        # heartbeat) and exit WITHOUT re-recording: the path is already in the
        # tracker, and append_tracker does not dedup, so re-recording every
        # iteration would bloat the tracker with one duplicate line per cycle.
        exit 0
    fi
    echo "Skill /$skill_name instructions already in context — follow them from earlier in this conversation. Do NOT re-invoke." >&2
    exit 2
fi

# First invocation — record and allow
MIND_AGENT="${AGENT_NAME:-}" python3 "$CORE_ROOT/scripts/context-reads.py" record $sid_arg "$skill_path" 2>/dev/null || true

# Long-term invocation telemetry — append to per-agent JSONL ledger.
# Fail-open: any failure here must NOT block the skill from running.
# Knowledge tree: world/knowledge/tree/system/system-constraints-loop/skill-telemetry-signal-master-plan.md
if [ -n "${AGENT_DIR:-}" ] && [ -n "$skill_name" ]; then
    AGENT_DIR="$AGENT_DIR" AGENT_NAME="${AGENT_NAME:-}" SKILL_NAME="$skill_name" SESSION_ID="$session_id" \
        SCRIPTS_DIR="$CORE_ROOT/scripts" \
        python3 - <<'PY' 2>/dev/null || true
import sys, json, datetime, os
agent_dir = os.environ.get('AGENT_DIR', '')
agent_name = os.environ.get('AGENT_NAME', '')
skill = os.environ.get('SKILL_NAME', '')
sid = os.environ.get('SESSION_ID', '')
if agent_dir and skill:
    row = {
        'ts': datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
        'skill': skill,
        'agent': agent_name,
        'sid': sid,
        'invocation_source': 'model',
    }
    #  (G3 worker rail): skip this append when the bound Body is a
    # WORKER. This is ORTHOGONAL to *how* the append is done (bare vs locked —
    # see the note below, and do not conflate the two): the rail governs
    # WHETHER this box should write the agent-wide ledger at all. A worker Body
    # shares an agent dir with a reducer running on a different machine, so its
    # appends land in a store it does not own.
    #
    # DO NOT rewrite this as `os.environ.get('BODY_ROLE') != 'worker'`. It would
    # be 100% INERT and would hand-test GREEN. BODY_ROLE is exported by
    # bash-agent-inject.py, which rewrites the command string of BASH TOOL calls
    # ONLY — and this is a PreToolUse[Skill] hook invoked directly by Claude Code.
    # The comment at the top of this file already states the general fact for
    # MIND_AGENT ("which does NOT inject MIND_AGENT ... only Bash *tool* calls
    # get it"); BODY_ROLE travels by the identical mechanism and is absent here
    # for the identical reason. A hand-run shell HAS the var, so the only
    # environment where such a rail fails is the only environment where it runs
    # (guard-1680; the 59-day pre-edit-context-gate inertia, read-before-edit.md
    # Rule 4). The design brief for G3 credits the verifier with catching this one
    # level up at bash-agent-inject; this is the next instance of the same class.
    #
    # So derive the role LOCALLY from the already-resolved agent + session_id,
    # using the SAME predicate bash-agent-inject uses: a per-session body-WM file
    # exists ONLY for a non-reducer Body. `sessions` is inlined per this file's
    # IRREDUCIBLY LOCAL header (sourcing _paths/_session_binding here would blow
    # the hook latency budget) — it mirrors SESSIONS_DIRNAME and must be updated
    # with it (CLAUDE.md Agent-dir Resolution, inlined-copies table).
    _SDN='sessions'
    if sid and os.path.exists(
            os.path.join(agent_dir, _SDN, sid, 'working-memory.yaml')):
        raise SystemExit(0)
    # Bare O_APPEND write — see the sibling writer user-prompt-skill-record.sh
    # for the full rationale and the measurement. Short version: -e
    # routed this through _fileops.locked_append_jsonl, which under own-cloud is
    # a force-fresh GET + full-file PUT on every hook fire. That is forbidden by
    # the IRREDUCIBLY LOCAL banner at line 2, and it is not what fixed the data
    # loss — the merge REGISTRATION did.
    #
    # Fail-open is the contract here (guard-141): the enclosing
    # `2>/dev/null || true` keeps exit 0 regardless of what happens below.
    try:
        with open(os.path.join(agent_dir, 'skill-invocations.jsonl'),
                  'a', encoding='utf-8') as fh:
            fh.write(json.dumps(row) + '\n')
    except Exception:
        pass
PY
fi

exit 0
