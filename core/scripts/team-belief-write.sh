#!/usr/bin/env bash
# Record/update a Theory-of-Mind belief ABOUT a partner agent ().
#
# Orchestrator for the WRITE half of the partner-belief loop documented in
# `core/config/conventions/coordination.md` Team State Protocol. Composes two
# existing daemon wrappers (read + set) around the pure supersede/cap compute
# in `_team_belief.py`, so the daemon stays the single canonical writer and the
# hygiene rule ("supersede the prior belief about a partner, do not grow the
# list unbounded") lives in one unit-tested place.
#
# The belief is stored under the CALLING agent's own sublist
# (agent_status.<self>.beliefs) — each agent is the sole writer of its own
# beliefs, so the read-then-set is race-free at the field level under the
# shared team-state lock (see _team_belief.py module docstring).
#
# Usage:
#   bash core/scripts/team-belief-write.sh --about <partner> --belief "<text>" \
#        [--confidence <0..1>] [--domain <focus-domain>] [--now <iso>]
#
# --domain () is the OPTIONAL structured focus-domain the belief asserts
# the partner is working in — pass the partner's observed `current_focus`
# VERBATIM. It is what makes the belief contradiction-checkable by
# aspirations-precheck Phase 0-pre.0a (`_belief_contradiction.process_all`
# evaluates ONLY beliefs carrying a checkable `domain`).
#
# THIS FLAG WAS MISSING FROM THIS CASE BLOCK UNTIL 2026-08-03 (alpha, 
# window). `_team_belief.py` has implemented it since , and
# fresh-eyes-review Phase 2.6c documents callers passing it — but the catch-all
# `*) shift;;` below silently discarded it, so every belief written through the
# ONLY documented path stored `domain: null` and the contradiction detector's
# candidate set was structurally EMPTY. Its "clean" verdict was vacuous, not
# negative. Measured: three writes with --domain (foxtrot, bravo, echo) all
# landed null; wrapper had zero occurrences of `domain`, module had six.
# (rb-538: multi-layer arg parsers silently drop unknown flags. guard-2172: a
# wrapper's accepted flags live in its case block ONLY — the SKILL.md pseudocode
# is not the whitelist.)
#
# Values are passed to _team_belief.py via argv and the current list via stdin —
# never interpolated into a `python -c` source string (guard-165 safe; the
# module is a real file, not inline source).
set -euo pipefail

_SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_SELF_DIR/../.." && pwd)"

# Sourcing _paths.sh puts the .python-shim on PATH, making `python3` resolve to
# a real interpreter inside this script (the canonical "python3 only inside a
# .sh that sources _paths.sh" pattern — see CLAUDE.md Python Invocation).
# shellcheck disable=SC1091
source "$PROJECT_ROOT/core/scripts/_paths.sh"

# Value-arg pattern: "${2-}" + safe shift; see team-state-update.sh / _runtime.sh.
ABOUT=""; BELIEF=""; CONFIDENCE="0.5"; NOW=""; DOMAIN=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --about)      ABOUT="${2-}";      shift $(( $# >= 2 ? 2 : 1 ));;
        --belief)     BELIEF="${2-}";     shift $(( $# >= 2 ? 2 : 1 ));;
        --confidence) CONFIDENCE="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --domain)     DOMAIN="${2-}";     shift $(( $# >= 2 ? 2 : 1 ));;
        --now)        NOW="${2-}";        shift $(( $# >= 2 ? 2 : 1 ));;
        *) shift;;
    esac
done

SELF="${MIND_AGENT:-}"
if [ -z "$SELF" ]; then
    echo "team-belief-write.sh: MIND_AGENT unset — cannot resolve self (the belief owner). Run via the agent-bound session or prefix MIND_AGENT=<name>." >&2
    exit 1
fi
if [ -z "$ABOUT" ] || [ -z "$BELIEF" ]; then
    echo "team-belief-write.sh: --about <partner> and --belief \"<text>\" are required." >&2
    exit 1
fi

FIELD="agent_status.${SELF}.beliefs"

# 1. READ current beliefs sublist (daemon). Missing field returns literal "null"
#    (verified 2026-06-18) with exit 0; _team_belief.py treats null/empty/non-list
#    as an empty list, so no special-casing needed here.
CURRENT="$(bash "$PROJECT_ROOT/core/scripts/team-state-read.sh" --field "$FIELD" --json)"

# 2. COMPUTE the superseded+capped list (pure module; values via argv, list via stdin).
PY_ARGS=(--about "$ABOUT" --belief "$BELIEF" --confidence "$CONFIDENCE")
[ -n "$NOW" ] && PY_ARGS+=(--now "$NOW")
[ -n "$DOMAIN" ] && PY_ARGS+=(--domain "$DOMAIN")
NEW_LIST="$(printf '%s' "$CURRENT" | python3 "$PROJECT_ROOT/core/scripts/_team_belief.py" "${PY_ARGS[@]}")"

# 3. WRITE the whole list back with operation=set (daemon). `set` replaces only
#    agent_status.<self>.beliefs; concurrent partner writes to other fields are
#    preserved under the shared lock.
bash "$PROJECT_ROOT/core/scripts/team-state-update.sh" \
    --field "$FIELD" --operation set --value "$NEW_LIST"
