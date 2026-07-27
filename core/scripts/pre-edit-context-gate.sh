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
# (never letting it leak as the hook's own stdout, which Claude Code would
# interpret as a deny payload) and re-emit the advisory to STDERR.
#
# Origin: G14 "Context Sufficiency Self-Check" primitive.
# See also: .claude/rules/read-before-edit.md (behavioral rule, Layer A —
# covers ALL files; this gate is Layer B and covers the trackable subset).
set -euo pipefail

# --- Fail-open wrapper: ANY error -> silent exit 0 ---
_fail_open() { exit 0; }
trap '_fail_open' ERR

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh" 2>/dev/null || exit 0
source "$CORE_ROOT/scripts/_platform.sh" 2>/dev/null || exit 0

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

# No agent bound -> no manifest context to consult; stay silent.
if [ -z "${AGENT_DIR:-}" ]; then
    exit 0
fi

sid_arg=""
if [ -n "$session_id" ]; then
    sid_arg="--session-id $session_id"
fi

# Delegate scope + session-scoped tracked-set check to the single source of
# truth. Non-empty stdout == "in-scope AND not read this session" == warn.
# stderr from the py call is suppressed; any failure trips the ERR trap -> exit 0.
result=$(python3 "$CORE_ROOT/scripts/context-reads.py" check-file $sid_arg "$file_path" 2>/dev/null) || exit 0

if [ -n "$result" ]; then
    echo "[pre-edit-context-gate] ADVISORY: $file_path has not been Read this session — Read it before editing to avoid acting on stale context." >&2
fi

exit 0
