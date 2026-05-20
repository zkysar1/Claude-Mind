#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# sid-collision-check.sh — refuse if SID is already claimed by a LIVE runner
# whose binding the caller would stomp (cross-agent OR same-agent observer
# binding when the agent's runner is alive on a different SID). Single source
# of truth for the SID-collision gate; called from /start IDLE Step 0, /start
# observer Step 0, and session-save-id.sh compact path.
#
# WHY THIS EXISTS
# ---------------
# Claude Code reuses session_id under two distinct conditions; this gate
# defends against BOTH at the binding-write boundary.
#
# (1) Cross-agent: a second terminal runs `claude --continue` / `--resume`
#     against a still-active session of a DIFFERENT agent. The default
#     /start write to .active-agent-$SID then silently overwrites the
#     original agent's binding; the victim's stop-hook starts routing to
#     the wrong agent and the victim's autonomous loop dies on the next
#     sid-mismatch ALLOW. (2026-05-12 incident: .active-agent-069a35b6
#     ping-ponged between zeta and bravo for hours; both windows shared
#     one 78MB transcript.)
#
# (2) Same-agent: at autocompact, Claude Code may rotate the post-compact
#     SID to a value already bound to an observer of the SAME agent. The
#     OLD_SID witness chain in session-save-id.sh validates pre-compact
#     state (lock_sid == breadcrumb_sid, content witnesses all consistent),
#     so the post-compact writer rewrites running-session-id to the
#     observer's SID — flipping the live runner to observer and the
#     observer to runner. (2026-05-13 zeta incident: 88e2f940 stomped
#     8be5683c's runner binding at 11:26:52; the loop transferred to the
#     wrong terminal for 4+ hours.) Claude Code does NOT surface
#     previous_session_id at compact, so this gate is the only defense.
#
# The platform's --fork-session flag exists for case (1) but is not the
# default. Case (2) has no platform-level escape.
#
# CONTRACT
# --------
# Usage: bash sid-collision-check.sh <my-agent> <sid>
# Exit codes:
#   0 — safe to write .active-agent-<SID>. Any of: file missing, content
#       empty, the existing-bound agent is not RUNNING, MY_AGENT's runner
#       SID equals $SID (runner reconnecting), running-session-id missing,
#       or heartbeat stale (crashed runner — allow recovery rebind).
#   2 — COLLISION. Two failure modes:
#       (a) Cross-agent — stderr: "ERROR:SID_COLLISION ..."
#           .active-agent-<SID> binds to a DIFFERENT agent whose runner
#           is alive (agent-state=RUNNING + heartbeat fresh).
#       (b) Same-agent — stderr: "ERROR:SID_COLLISION_SAME_AGENT ..."
#           .active-agent-<SID> binds to MY_AGENT, but MY_AGENT's
#           running-session-id is a DIFFERENT SID and that runner is
#           alive (state=RUNNING + heartbeat fresh). Binding would stomp
#           the runner's binding via post-autocompact SID rotation.
#       Caller MUST refuse in either case.
#   1 — script/usage error
#
# DO NOT ADD a force/override flag. There is no legitimate scenario where two
# Claude Code windows should share a session_id — the platform offers
# --fork-session for the user's explicit "I want a separate session" intent.
# If the user wants to rebind a SID, they /stop the other agent first.
#
# DO NOT REMOVE the heartbeat-stale check. A binding pointing to an agent
# whose runner crashed without /stop is stale — refusing on a stale binding
# would block legitimate rebinds and recovery flows.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/_paths.sh"

MY_AGENT="${1:-}"
SID="${2:-}"
if [ -z "$MY_AGENT" ] || [ -z "$SID" ]; then
    echo "usage: sid-collision-check.sh <agent> <sid>" >&2
    exit 1
fi

# Phase 2.6: resolve via the canonical binding resolver. It tries the new
# agents/<name>/sessions/<SID>/binding.yaml layout first, then falls back to
# the legacy .active-agent-<SID> file at PROJECT_ROOT.
# Use py -3 first (Windows Git Bash launcher), fall back to python3 (WSL/POSIX).
EXISTING=$(
    py -3 "$SCRIPT_DIR/_resolve_agent_from_sid.py" "$SID" 2>/dev/null \
        || python3 "$SCRIPT_DIR/_resolve_agent_from_sid.py" "$SID" 2>/dev/null \
        || true
)
EXISTING=$(printf '%s' "$EXISTING" | tr -d '\r\n')
[ -n "$EXISTING" ] || exit 0

# Same-agent branch. The pre-fix line `[ "$EXISTING" = "$MY_AGENT" ] && exit 0`
# was an unconditional fast-path that missed case (2) in the header (post-
# autocompact SID rotation against a same-agent observer binding). Replace
# with a runner-vs-observer check using the same state+heartbeat signals as
# the cross-agent branch below — a bind is safe only when the agent's live
# runner is either THIS SID or not present.
if [ "$EXISTING" = "$MY_AGENT" ]; then
    SAME_STATE=$(cat "$(agent_dir "$MY_AGENT")/session/agent-state" 2>/dev/null | tr -d '\r\n')
    [ "$SAME_STATE" = "RUNNING" ] || exit 0
    SAME_RUNNING_SID=$(cat "$(agent_dir "$MY_AGENT")/session/running-session-id" 2>/dev/null | tr -d '\r\n')
    [ -n "$SAME_RUNNING_SID" ] || exit 0
    # Load-bearing: $SID IS the runner → self-rebind (e.g., post-compact
    # reconnect when Claude Code does NOT rotate SID, or /start re-run in the
    # same terminal). DO NOT REMOVE — without it, every same-agent BIND_FILE-
    # exists case is falsely flagged as collision and the runner can never
    # reconnect to its own SID.
    [ "$SAME_RUNNING_SID" != "$SID" ] || exit 0
    SAME_HB=$(MIND_AGENT="$MY_AGENT" bash "$(dirname "$0")/heartbeat-stale.sh" 2>/dev/null || echo "stale")
    [ "$SAME_HB" = "fresh" ] || exit 0
    echo "ERROR:SID_COLLISION_SAME_AGENT sid=$SID bound_to=$EXISTING runner=$SAME_RUNNING_SID attempting=$MY_AGENT state=$SAME_STATE heartbeat=$SAME_HB" >&2
    exit 2
fi

# Cross-agent branch. Reaching here means EXISTING != MY_AGENT.
OTHER_DIR="$(agent_dir "$EXISTING")"
[ -d "$OTHER_DIR" ] || exit 0

OTHER_STATE=$(cat "$OTHER_DIR/session/agent-state" 2>/dev/null | tr -d '\r\n')
[ "$OTHER_STATE" = "RUNNING" ] || exit 0

HB_STATUS=$(MIND_AGENT="$EXISTING" bash "$(dirname "$0")/heartbeat-stale.sh" 2>/dev/null || echo "stale")
[ "$HB_STATUS" = "fresh" ] || exit 0

# COLLISION CONFIRMED. Loud stderr so /start's halt instruction can display it.
echo "ERROR:SID_COLLISION sid=$SID bound_to=$EXISTING attempting=$MY_AGENT state=$OTHER_STATE heartbeat=$HB_STATUS" >&2
exit 2
