#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# PostToolUse[*] advisory hook — the ZERO-RETRIEVAL PULSE ( layer 2).
#
# Layer 1 (retrieval-floor-gate.sh) asks one question at one moment: "did this
# session consult anything before writing to a knowledge store?" That leaves the
# mid-task INTERIOR uncovered — a non-loop session can run for dozens of
# substantive tool calls, drift far from whatever it last checked, and never
# touch a knowledge store, so the floor never fires and nothing else is looking.
#
# This is the interior counter. After N consecutive tool calls with no NEW
# deliberate consultation it emits one advisory naming the count and the nearest
# retrieve-before-deciding decision points, then resets.
#
# Posture: ADVISORY, fail-open, ALWAYS exits 0. Never blocks (a PostToolUse hook
# structurally cannot — the tool has already run). Identical honest-limit caveat
# to layer 1 (guard-4407): a consultation made with cat/curl/grep leaves no
# manifest entry, so a firing is a prompt to check, never proof the session
# retrieved nothing.
#
# ── WHY IT DELEGATES THE COUNT (the layer-1 author's recorded constraint) ────
# The streak resets on `context-reads.py retrieval-pulse`, which calls
# count_deliberate_retrievals — the SAME predicate layer 1 uses. It must never
# re-count the manifest here. `retrieval-auto` (the UserPromptSubmit automatic
# pre-pass) fires on essentially every substantive user message; a pulse that
# reset on those would reset constantly and measure nothing, which is the
# unreachable-gate failure DELIBERATE_RETRIEVAL_KINDS exists to prevent
# (guard-1760).
#
# ── A PLAIN Read DOES NOT RESET THE PULSE, AND THAT IS DELIBERATE ────────────
# The manifest has two lanes: the provenance lane (url / search / node / board /
# retrieval) and a file-path lane fed by the Read hook. count_deliberate_retrievals
# reads ONLY the provenance lane, so reading a tree node with the Read tool does
# not reset this counter — retrieve.sh, tree-read.sh, WebFetch and WebSearch do.
# Inherited from layer 1's predicate ON PURPOSE: the two layers must agree, and
# the error direction is one extra advisory rather than a silent pass.
#
# DELIVERY: hookSpecificOutput.additionalContext is the PostToolUse channel
# (same one iteration-close-reminder.py uses). Empty stdout + exit 0 = "nothing
# injected" per the hook contract.
set -euo pipefail

# --- Fail-open wrapper: ANY error -> silent exit 0 ---
_fail_open() { exit 0; }
trap '_fail_open' ERR

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh" 2>/dev/null || exit 0
# ORDER-CRITICAL: `source _platform.sh` is DELIBERATELY DEFERRED until AFTER the
# agent-resolution block below, and must not be "tidied" up here. _platform.sh
# exports MSYS_NO_PATHCONV=1; under that flag session-binding-read.sh resolves to
# EMPTY on Git Bash ( / ), AGENT_NAME lands empty, and this hook
# exits 0 SILENTLY — indistinguishable from "nothing to warn about". That is how
# the sibling pre-edit-context-gate stayed inert on Windows for 59 days, and it
# hand-tests green because an interactive shell has no MSYS_NO_PATHCONV.

session_id=$(python3 -c "
import sys,json
try:
    print(json.load(sys.stdin).get('session_id',''))
except Exception:
    print('')
" 2>/dev/null) || exit 0

# --- Agent resolution ------------------------------------------------------
# PostToolUse[Bash] hooks receive NO MIND_AGENT in env (bash-agent-inject.py
# injects only into PreToolUse[Bash] commands), and with matcher '*' this fires
# for every tool, so the binding is the only reliable source.
AGENT_NAME="${MIND_AGENT:-}"
if [ -z "$AGENT_NAME" ] && [ -n "$session_id" ]; then
    AGENT_NAME="$(bash "$CORE_ROOT/scripts/session-binding-read.sh" "$session_id" 2>/dev/null || true)"
fi
[ -n "$AGENT_NAME" ] || exit 0

# --- NON-LOOP SESSIONS ONLY (outcome 4) ------------------------------------
# The autonomous loop retrieves for itself (execute Phase 4) and is audited at
# Phase 9.5b; a pulse there would spend the banner on the one path that already
# has enforcement, and would fire constantly on a loop whose tool calls are
# mostly store writes. Mirrors layer 1 and user-prompt-retrieval-inject.sh. Read
# the mode file DIRECTLY — session-mode-get.sh costs ~700ms on some platforms,
# which is the whole per-call budget this file's header declares.
_mode_file="$(agent_dir "$AGENT_NAME")/session/agent-mode"
if [ -r "$_mode_file" ]; then
    _mode="$(tr -d ' \t\r\n' < "$_mode_file" 2>/dev/null || true)"
    [ "$_mode" = "autonomous" ] && exit 0
fi

source "$CORE_ROOT/scripts/_platform.sh" 2>/dev/null || exit 0

sid_arg=""
[ -n "$session_id" ] && sid_arg="--session-id $session_id"

# rc 0 = the pulse fired, and stdout carries the advisory. rc 1 = silent.
# Captured with $(...) so the exit code survives: a pipe would replace it
# (guard-1150) and every tick would read as "fired".
set +e
msg="$(MIND_AGENT="$AGENT_NAME" python3 "$CORE_ROOT/scripts/context-reads.py" \
        retrieval-pulse $sid_arg 2>/dev/null)"
pulse_rc=$?
set -e
[ "$pulse_rc" -eq 0 ] || exit 0
[ -n "$msg" ] || exit 0

echo "$msg" >&2

MSG="$msg" python3 -c "
import json, os
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PostToolUse',
        'additionalContext': os.environ['MSG'],
    }
}))
" 2>/dev/null || true

# Telemetry (outcome 3). Best-effort, AFTER the payload so it can never delay or
# suppress the advisory this hook exists to deliver.
bash "$CORE_ROOT/scripts/gate-log.sh" retrieval-pulse-hook pass \
    --caller "PostToolUse[*]" \
    --trigger "zero-deliberate-retrieval-streak" \
    --payload "$session_id" >/dev/null 2>&1 || true

exit 0
