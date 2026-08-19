#!/usr/bin/env bash
# cross-agent-write.sh — enforced env-prefix for cross-agent write-back.
#
#  (option A, per bravo decision msg-20260709-021804-bravo-118:
# "formalize/enforce the cross-agent env-prefix via a shared HELPER [LOW-blast]";
# NOT option B daemon-level source='cross-agent:<owner>' resolution).
#
# THE MECHANISM (already proven, part-i landed 28886f9d): a write issued with
# MIND_AGENT=<owner> becomes the X-Mind-Agent:<owner> header (_rt.py:81 /
# _runtime.sh:745); the daemon resolves source=agent via that header
# (mind_api/src/server.py:180 -> resolver.resolve(<owner>) ->
# ctx.paths.agent = agents/<owner>/) so the write lands in <owner>'s
# aspirations.jsonl (mind_api/src/endpoints/aspirations.py _resolve_paths).
#
# THE FRAGILITY option A closes: the orchestrator had to REMEMBER to hand-prefix
# `MIND_AGENT=<owner>` on every affected write during a cross-agent goal
# execution (aspirations-execute Phase 4 Setup ENV_PREFIX). Forgetting it on ANY
# one call silently wrote to the ACTOR's own queue, and the cross-pulled goal
# never landed back in the owner's aspirations.jsonl. This helper is the single
# canonical path: the owner is passed as DATA (arg 1), the prefix is applied by
# CODE, so it cannot be forgotten — and the identity/liveness-exempt scripts
# (which MUST run under the caller's own identity) are refused, so an exempt
# call cannot be accidentally swapped to the owner.
#
# Usage:  cross-agent-write.sh <owner> <script-basename> [args...]
#   <owner> : the cross-agent goal owner. "" or "-" = self/normal execution
#             (no prefix — byte-identical to calling the script directly).
#   <script-basename> : a write wrapper under core/scripts/. Identity/liveness
#             scripts (see CROSS_AGENT_EXEMPT) are REFUSED — call them directly.
#
# Exit: the wrapped script's exit code; 2 on a usage / exempt-script error.
#
# Deny-list (not allow-list) rationale: the DEFAULT for a cross-agent goal
# execution is that a write routes to the OWNER (that is the whole point). The
# exceptions are the few identity/liveness scripts that must stay under the
# caller's own identity. A deny-list means a NEW write wrapper automatically
# routes correctly (to the owner) without editing this file, while the small,
# stable exempt set is what needs explicit listing. An allow-list would silently
# refuse legitimate cross-agent writes through any wrapper not yet added to it.
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$#" -lt 2 ]; then
    echo "usage: cross-agent-write.sh <owner> <script-basename> [args...]" >&2
    echo "  <owner>: cross-agent goal owner; \"\" or \"-\" for self/normal execution" >&2
    exit 2
fi

OWNER="$1"
SCRIPT="$2"
shift $(( $# >= 2 ? 2 : 1 ))

# Identity/liveness scripts that MUST run under the CALLER's own identity and so
# must NEVER be routed to the owner. SSOT for the exempt classification that was
# previously only a doc comment in aspirations-execute/SKILL.md Phase 4 Setup:
#   - aspirations-claim.sh        : --source world is world-scoped (no agent swap)
#   - board-post.sh               : board entries are authored by the caller
#   - team-state-in-flight.sh     : world-level liveness — partners must see
#     that ALPHA claimed BRAVO's goal, not that BRAVO claimed it
#   - team-state-clear-in-flight.sh : same liveness record, clear side
#   - heartbeat-tick.sh           : ticks THIS runner's heartbeat
CROSS_AGENT_EXEMPT=(
    aspirations-claim.sh
    board-post.sh
    team-state-in-flight.sh
    team-state-clear-in-flight.sh
    heartbeat-tick.sh
)

# Normalize: accept either a bare basename or a core/scripts/-prefixed path.
SCRIPT_BASE="${SCRIPT##*/}"

for exempt in "${CROSS_AGENT_EXEMPT[@]}"; do
    if [ "$SCRIPT_BASE" = "$exempt" ]; then
        echo "cross-agent-write: '$SCRIPT_BASE' is an identity/liveness script and MUST run under the caller's own identity — call it directly, NOT through this helper. (Routing it to owner '$OWNER' would corrupt the world-level liveness/attribution record.)" >&2
        exit 2
    fi
done

if [ ! -f "$SELF_DIR/$SCRIPT_BASE" ]; then
    echo "cross-agent-write: no such script core/scripts/$SCRIPT_BASE" >&2
    exit 2
fi

# Self / normal execution: owner empty or "-" -> no prefix (pure passthrough,
# byte-identical to invoking the script directly under the caller's identity).
if [ -z "$OWNER" ] || [ "$OWNER" = "-" ]; then
    exec bash "$SELF_DIR/$SCRIPT_BASE" "$@"
fi

# Cross-agent execution: enforce the owner prefix. env(1) sets MIND_AGENT for
# the wrapped process ONLY; _rt.py / _runtime.sh turn it into the
# X-Mind-Agent:<owner> header, and the daemon routes source=agent writes to
# <owner>'s queue. This is an exec inside an already-running Bash process, so
# the PreToolUse[Bash] agent-inject hook (which fires once per Bash TOOL call)
# does not re-fire here — the explicit env wins cleanly.
exec env MIND_AGENT="$OWNER" bash "$SELF_DIR/$SCRIPT_BASE" "$@"
