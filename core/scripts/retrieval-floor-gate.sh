#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# PreToolUse[Edit|MultiEdit|Write] advisory hook — warns when a NON-LOOP session
# is about to write to a knowledge / ground-truth store having consulted NOTHING
# this session ( layer 1).
#
# Origin: user directive 2026-08-31, "assistant mode under-retrieves; not only
# user questions should trigger fetching". The loop path already has per-decision
# retrieval enforcement (execute Phase 4 + the Phase 9.5b learning-gate audit).
# Non-loop sessions (assistant / reader / observer) had none — the pre-edit
# context gate is advisory and asks a DIFFERENT question ("was THIS FILE read?"),
# and everything else was prose. This is the mechanical floor under
# retrieve-before-deciding.md decision point 10.
#
# Posture: ADVISORY, fail-open, ALWAYS exits 0. Never blocks, never denies. A
# knowledge write with no recorded consultation is a prompt to go check, never a
# verdict that the content is invented — see the honest-limit block below.
#
# ── WHAT COUNTS AS A CONSULTATION, AND WHY THE AUTO PRE-PASS DOES NOT ────────
# Delegated to `context-reads.py retrieval-floor`, the single source of truth
# (DELIBERATE_RETRIEVAL_KINDS). The load-bearing exclusion is `retrieval-auto`:
# user-prompt-retrieval-inject.sh runs an automatic retrieval on essentially
# every substantive user message, and counting that would make this floor
# unreachable — it would pass for every session in which a human typed a
# sentence, while measuring nothing. A gate that cannot fail is worse than no
# gate, because it reads as coverage (guard-1760).
#
# ── THE HONEST LIMIT ON THE NEGATIVE (guard-4407) ────────────────────────────
# This hook binds to the Edit / MultiEdit / Write TOOLS, and the manifest it
# reads is fed by hooks bound to Read / WebFetch / WebSearch plus retrieve.sh and
# tree-read.sh. Under a Bash-preference session ("make file changes with sed,
# heredocs, or short scripts") BOTH halves degrade together and SILENTLY: a
# knowledge write performed with `cat >` never reaches this hook at all, and a
# consultation made with `cat`/`curl` leaves no manifest entry. So:
#   - this gate's SILENCE is not evidence a session retrieved (it may never have run);
#   - this gate's FIRING is not proof a session retrieved nothing (it may have
#     used the un-instrumented path).
# It is a floor under the INSTRUMENTED path, not a proof about the session. Do
# not wire anything that REFUSES on this signal, and do not "raise yield" by
# widening it — the behavioral rule (retrieve-before-deciding.md) remains the
# only guarantee, exactly as read-before-edit.md Rule 4 says of its sibling.
#
# DELIVERY: the structured `permissionDecision: allow` payload is the ONLY
# channel that reaches the model from a non-blocking PreToolUse hook
# (guard-1680 / ). Shape copied verbatim from pre-edit-context-gate.sh,
# which carries the five-probe table. Do NOT narrow it from first principles.
set -euo pipefail

# --- Fail-open wrapper: ANY error -> silent exit 0 ---
_fail_open() { exit 0; }
trap '_fail_open' ERR

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh" 2>/dev/null || exit 0
# ORDER-CRITICAL: `source _platform.sh` is DELIBERATELY DEFERRED until AFTER the
# agent-resolution block below, and must not be "tidied" back up here.
# _platform.sh exports MSYS_NO_PATHCONV=1; under that flag Git Bash stops
# rewriting MSYS paths into Windows form for native binaries, so
# session-binding-read.sh resolves to EMPTY ( / ): the wrapper
# computes SCRIPT_DIR via cd+pwd -> /c/..., calls a NATIVE py.exe, which mangles
# it to C:\c\... and exits rc=2. The `|| true` then swallows it, AGENT_NAME
# lands empty, and this gate exits 0 SILENTLY — indistinguishable from "nothing
# to warn about". That is how the sibling pre-edit-context-gate stayed 100% inert
# on Windows even AFTER it was revived on Linux, and it hand-tests green because
# an interactive shell has no MSYS_NO_PATHCONV. _paths.sh alone already puts the
# python shim on PATH, which is all the stdin parse below needs.

read_info=$(python3 -c "
import sys,json
try:
    d = json.load(sys.stdin)
    ti = d.get('tool_input',{})
    fp = ti.get('file_path','')
    sid = d.get('session_id','')
    print(f'{sid}|{fp}')
except Exception:
    print('|')
" 2>/dev/null) || exit 0

session_id="${read_info%%|*}"
file_path="${read_info#*|}"
[ -n "$file_path" ] || exit 0

# --- Scope: knowledge / ground-truth stores ONLY --------------------------
# Deliberately NARROWER than pre-edit-context-gate's advisory scope. The
# directive is about publishing claims about the world without checking sources,
# so the scope is the stores that ASSERT such claims: knowledge-tree nodes and
# convention files. A skill or script edit is not a knowledge claim, and warning
# there would spend the banner's credibility on the wrong writes.
# Separator normalization is load-bearing on Windows (): Claude Code
# sends file_path natively, so a backslashed path matches none of these globs and
# every Windows edit would silently fall through — a false reject on 100% of one
# platform, which is the one outcome this pre-filter must never produce.
_fp_norm="${file_path//\\//}"
case "$_fp_norm" in
    *knowledge/tree/*|*/conventions/*) ;;
    *) exit 0 ;;
esac

# --- Constitutional-anchor exclusion (MUST precede any payload emission) ---
# The payload carries `permissionDecision: allow`, which short-circuits the
# permission system. The anchor is hard-denied at every tier and this gate must
# never hand out an allow that could weaken it.
case "$file_path" in
    *settings.local.json|*settings-structural-validator.py|*settings-structural-validator.sh) exit 0 ;;
esac

# --- Agent resolution ------------------------------------------------------
# MIND_AGENT is injected only into PreToolUse[Bash], never into Edit/Write, so
# this must resolve from the session binding — the defect that left the sibling
# gate inert for 59 days (), and which hand-tests green because an
# interactive shell HAS MIND_AGENT set.
AGENT_NAME="${MIND_AGENT:-}"
if [ -z "$AGENT_NAME" ] && [ -n "$session_id" ]; then
    AGENT_NAME="$(bash "$CORE_ROOT/scripts/session-binding-read.sh" "$session_id" 2>/dev/null || true)"
fi
[ -n "$AGENT_NAME" ] || exit 0

# --- NON-LOOP SESSIONS ONLY ------------------------------------------------
# The autonomous loop retrieves for itself (execute Phase 4) and is audited by
# the learning gate at Phase 9.5b; firing here would duplicate that and spend the
# banner on the one path that already has enforcement. Mirrors the sibling
# exclusion in user-prompt-retrieval-inject.sh, which skips autonomous sessions
# for the same reason. Read the mode DIRECTLY rather than via
# session-mode-get.sh — that wrapper costs ~700ms on some platforms, which is the
# whole per-call latency budget this file's header declares.
_mode_file="$(agent_dir "$AGENT_NAME")/session/agent-mode"
if [ -r "$_mode_file" ]; then
    _mode="$(tr -d ' \t\r\n' < "$_mode_file" 2>/dev/null || true)"
    [ "$_mode" = "autonomous" ] && exit 0
fi

source "$CORE_ROOT/scripts/_platform.sh" 2>/dev/null || exit 0

sid_arg=""
[ -n "$session_id" ] && sid_arg="--session-id $session_id"

# rc 0 = the session HAS consulted something -> silent. rc 1 = zero.
# The exit code IS the answer, so it must not be replaced by a pipe (guard-1150).
if MIND_AGENT="$AGENT_NAME" python3 "$CORE_ROOT/scripts/context-reads.py" \
        retrieval-floor --quiet $sid_arg >/dev/null 2>&1; then
    exit 0
fi

msg="[retrieval-floor-gate] ADVISORY: about to write $file_path — a knowledge/ground-truth store — but this session has NO recorded consultation (no retrieve.sh, tree-read, WebFetch/WebSearch). Check the stores before asserting a fact (retrieve-before-deciding.md point 10). If you consulted via cat/curl/grep, the manifest cannot see it: this is a prompt to verify, NOT a claim that your content is invented (guard-4407)."
echo "$msg" >&2

MSG="$msg" python3 -c "
import json, os
m = os.environ['MSG']
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'allow',
        'permissionDecisionReason': m,
        'additionalContext': m,
    },
    'systemMessage': m,
}))
" 2>/dev/null || true

# Telemetry (outcome 3). Best-effort, after the payload so it can never delay or
# suppress the advisory the gate exists to deliver.
bash "$CORE_ROOT/scripts/gate-log.sh" retrieval-floor-gate pass \
    --caller "PreToolUse[Edit|MultiEdit|Write]" \
    --trigger "zero-deliberate-retrievals" \
    --payload "$file_path" >/dev/null 2>&1 || true

exit 0
