#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# PreToolUse[Edit|MultiEdit] advisory hook — warns when the agent is about
# to edit a file it has NOT Read in the current session.
#
# Posture: advisory, fail-open, ALWAYS exits 0. The hook's value is the
# visible stderr banner; the LLM is expected to Read the file before
# proceeding with the edit. Never blocks, never denies.
#
# Scope: delegates the read/scope/session decision to
#   context-reads.py check-file
# which is the SINGLE SOURCE OF TRUTH for (a) what path classes are advisory-
# tracked (is_in_scope_advisory: core/config, .claude/skills,
# world/knowledge/tree, world/conventions, aspirations-compact.json, AND
# core/scripts framework code — ) and (b) session-scoped tracked-set
# membership. The advisory therefore fires ONLY for files the context-reads
# manifest can actually have signal about — editing a still-out-of-scope file
# (.claude/rules, self.md, product code) stays SILENT rather than crying wolf
# (a read of those is never recorded, so a "has not been Read" warning there
# would be a guaranteed false positive that desensitizes the agent to the
# banner). core/scripts reads ARE recorded (advisory scope) but are never
# re-read-BLOCKED — the blocking dedup gate keeps the narrow is_in_scope.
#
# check-file prints the target path to STDOUT iff it is in-scope AND not
# yet read this session — exactly the warn condition. We CAPTURE that stdout
# (never letting it leak as the hook's own stdout, which Claude Code reads as
# a decision payload) and re-emit the advisory on BOTH channels: stderr for a
# human watching the terminal, and the structured payload for the model.
#
# DELIVERY (guard-1680 / ). stderr + exit 0 from a NON-BLOCKING
# PreToolUse hook reaches the user's terminal only — never the model. Only a
# DENY feeds stderr back to Claude. From 2026-05-30 to 2026-07-28 this gate
# emitted stderr alone, so even had it run it would have communicated nothing.
# The measured-arriving shape is copied verbatim from the reference emitter
# `core/scripts/trailing-echo-exit-gate.py:113-139`, which carries 's
# five-probe table. Do NOT narrow it from first principles — `allow` +
# permissionDecisionReason ALONE was probed and did not deliver. Narrowing is
# tracked in  (needs a FRESH session per probe: hook-injected context
# appears deduped per session, so a second in-session probe false-negatives).
#
# Origin: G14 "Context Sufficiency Self-Check" primitive.
# See also: .claude/rules/read-before-edit.md (behavioral rule, Layer A —
# covers ALL files; this gate is Layer B and covers the trackable subset).
set -euo pipefail

# --- Fail-open wrapper: ANY error -> silent exit 0 ---
_fail_open() { exit 0; }
trap '_fail_open' ERR

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh" 2>/dev/null || exit 0
# NOTE: `source _platform.sh` is DELIBERATELY DEFERRED until after agent
# resolution below — see the ORDER-CRITICAL block there (). Moving it
# back up here re-kills this gate on Windows. _paths.sh alone already puts the
# python shim on PATH, which is all the stdin parse below needs.

# Extract file_path and session_id from hook JSON on stdin.
# Uses python3 (not py -3): this is a .sh hook that sources _paths.sh, which
# is exactly the case python-invocation.md sanctions for python3. Matches the
# sibling context-reads hooks (context-reads-record.sh, context-reads-gate.sh)
# so the whole context-reads hook family shares one invocation pattern.
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

# No file_path extracted -> nothing to check
if [ -z "$file_path" ]; then
    exit 0
fi

# --- Cheap NECESSARY-CONDITION pre-filter (pure bash, zero spawns) ---------
# INVARIANT: this may produce false ADMITS but MUST NEVER produce a false
# reject. `context_reads.is_in_scope_advisory` remains the SINGLE authority on
# the real scope decision; this is deliberately coarser and only short-circuits
# paths that cannot possibly be in it. Keep it a superset when either side
# changes.
#
# WHY IT EXISTS (alpha-fec-fence-unconditional-hotpath-cost-202607282010,
# measured cc-04): the sibling fence's identical agent-resolution fix converted
# a 0-spawn bail into a ~147ms unconditional cost on EVERY Edit, because the
# scope decision lived inside the python it had to spawn first. This file's
# header line 2 declares a per-Bash-call latency budget; that regression is
# exactly what spends it. The pre-filter keeps the common case (an edit to a
# path nothing tracks) at zero additional spawns, which is also why the
# script-call form of agent resolution below is affordable at all.
# SEPARATOR NORMALIZATION IS LOAD-BEARING (). Claude Code sends
# file_path in native form, so on Windows every path arrives BACKSLASHED
# (C:\...\.claude\skills\respond\SKILL.md). Matched raw against the
# forward-slash globs below, NOTHING matches and every path falls to `*) exit
# 0` — a false REJECT on 100% of Windows edits, which is precisely the one
# outcome the invariant above forbids. This pre-filter shipped 2026-07-28 as a
# latency optimization and silently re-killed the gate on Windows the same day
#  revived it, so the revival never took effect on this platform.
# Pure bash substitution: zero spawns, so the latency argument is intact. Only
# the MATCH uses the normalized copy; $file_path keeps its native form for
# context-reads.py, which normalizes internally (verified on both forms).
_fp_norm="${file_path//\\//}"
case "$_fp_norm" in
    *core/config/*|*.claude/skills/*|*knowledge/tree/*|*conventions/*|*core/scripts/*|*aspirations-compact.json) ;;
    *) exit 0 ;;
esac

# --- Constitutional-anchor exclusion (MUST precede any payload emission) ----
# The structured payload below carries `permissionDecision: "allow"`, which is
# how a non-blocking PreToolUse hook reaches the model at all (guard-1680) —
# but `allow` also short-circuits the permission system. The anchor
# (`.claude/settings.local.json` + `settings-structural-validator.{py,sh}`, see
# CLAUDE.md "CONSTITUTIONAL ANCHOR") is hard-denied at every tier, and this
# gate must never hand out an allow that could weaken that. The validator pair
# lives under core/scripts/, so it IS inside the advisory scope above and would
# otherwise reach the emitter. Advisory value here is nil anyway — the agent
# must not edit these files at all — so bail silently rather than warn.
case "$file_path" in
    *settings.local.json|*settings-structural-validator.py|*settings-structural-validator.sh) exit 0 ;;
esac

# --- Agent resolution (the  defect-1 fix) ------------------------
# MIND_AGENT is injected ONLY into PreToolUse[Bash] (bash-agent-inject.py).
# This hook is wired to PreToolUse[Edit|MultiEdit], so it never sees it, and
# _paths.sh therefore leaves AGENT_DIR empty on EVERY real invocation. The old
# `[ -z "$AGENT_DIR" ] && exit 0` bail thus fired every time — before the check
# it exists to perform — making the gate inert from its creation (ddff97349,
# 2026-05-30) until 2026-07-28. It hand-tested green because a hand-run shell
# HAS MIND_AGENT set: the only environment where it failed was the only
# environment where it ran. Byte-identical to the defect fixed in
# tree-write-fence.sh () and to rb-1958 / rb-2127; the same fallback
# is documented at context-reads-record.sh:34.
#
# Script-call form (not an inlined binding glob) so binding resolution keeps a
# single point of maintenance. It costs a spawn, but only for paths the
# pre-filter already admitted.
# ORDER-CRITICAL: this block must stay BEFORE `source _platform.sh`, which
# exports MSYS_NO_PATHCONV=1. Under that flag Git Bash stops rewriting MSYS
# paths into Windows form for native binaries, and session-binding-read.sh
# resolves to EMPTY (). Mechanism, measured 2026-07-29 ():
# the wrapper computes SCRIPT_DIR via `cd+pwd` -> /c/... and calls
# `py -3 "$SCRIPT_DIR/_session_binding.py"`; py.exe is a NATIVE binary, so with
# conversion disabled it receives the literal /c/... and mangles it to
# C:\c\... -> "can't open file", rc=2. Both `|| true`s then swallow it: the
# wrapper exits 1 with no output, AGENT_NAME lands empty, and the gate exits 0
# SILENTLY — indistinguishable from "nothing to warn about".
#
# Net effect before this fix: the gate was still 100% inert on Windows, in the
# exact PRODUCTION shape, even after  revived it on Linux. It
# hand-tested green for the same reason it did during the original 59-day
# inertia — an interactive shell has no MSYS_NO_PATHCONV, so the only
# environment where it failed remained the only environment where it ran.
#
# The three sibling hooks (context-reads-{record,gate,skill-gate}.sh) already
# carry this ordering and its comment; this gate is the newest member of the
# family and was the one that missed it. Do not "tidy" the source line back to
# the top of the file.
AGENT_NAME="${MIND_AGENT:-}"
if [ -z "$AGENT_NAME" ] && [ -n "$session_id" ]; then
    AGENT_NAME="$(bash "$CORE_ROOT/scripts/session-binding-read.sh" "$session_id" 2>/dev/null || true)"
fi
# Neither env nor binding resolved an agent -> no manifest to consult; silent.
if [ -z "$AGENT_NAME" ]; then
    exit 0
fi

# Safe to source now: agent resolution is done. context-reads.py below is
# invoked through the python3 SHIM (an MSYS script), which the siblings prove
# tolerates MSYS_NO_PATHCONV with a $CORE_ROOT path argument.
source "$CORE_ROOT/scripts/_platform.sh" 2>/dev/null || exit 0

sid_arg=""
if [ -n "$session_id" ]; then
    sid_arg="--session-id $session_id"
fi

# Delegate scope + session-scoped tracked-set check to the single source of
# truth. Non-empty stdout == "in-scope AND not read this session" == warn.
# MIND_AGENT is passed through the ENV (context-reads.py resolves AGENT_DIR /
# AGENT_NAME from _paths at import time) — env-passing, not string
# interpolation into the python source (guard-165).
# stderr from the py call is suppressed; any failure trips the ERR trap -> exit 0.
result=$(MIND_AGENT="$AGENT_NAME" python3 "$CORE_ROOT/scripts/context-reads.py" check-file --partial-aware $sid_arg "$file_path" 2>/dev/null) || exit 0

if [ -n "$result" ]; then
    # --partial-aware () splits the old single message in two. A ranged
    # read is real evidence the file was opened, so claiming it "has not been Read"
    # was simply false — and it fired on every large file, which is precisely where
    # the advisory needs to be believed. But going SILENT on a ranged read would be
    # worse than the false alarm: read-before-edit.md Rule 1 counts a partial read
    # only if it covers the region being edited, and this gate cannot know that
    # region (Edit carries old_string, never a line range). So hand the coverage
    # judgment to the only party that has it, rather than implying full context.
    if [ "${result#PARTIAL}" != "$result" ]; then
        msg="[pre-edit-context-gate] ADVISORY: $file_path was Read only in part this session (ranged read) — confirm your context covers the region you are editing."
    else
        msg="[pre-edit-context-gate] ADVISORY: $file_path has not been Read this session — Read it before editing to avoid acting on stale context."
    fi
    # stderr: what a human watching the terminal sees, and the fallback if the
    # structured channel is ever dropped. Belt and braces — the failure being
    # defended against is precisely a channel that silently carries nothing.
    echo "$msg" >&2
    # Structured channel: the ONLY one that reaches the model. Shape mirrors
    # trailing-echo-exit-gate.py's measured payload verbatim (guard-1680).
    # `allow` is explicitly NOT a deny — the edit still proceeds, nothing is
    # blocked, and this gate never wedges the loop.
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
fi

exit 0
