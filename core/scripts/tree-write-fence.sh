#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Hook wrapper for tree_write_fence.py -- lost-update detection on knowledge-tree
# .md node bodies (g-115-3555).
#
# Two wiring points, one script:
#   record   PostToolUse[Read]                     -- snapshot what we observed
#            PostToolUse[Edit|Write|MultiEdit]     -- re-snapshot after our own write
#   check    PreToolUse[Edit|Write|MultiEdit]      -- did it move underneath us?
#
# Posture: ADVISORY, fail-open, ALWAYS exits 0. It never denies a write.
# That is deliberate, not timidity: this repo runs an autonomous loop, and a
# fail-closed per-edit gate that false-positives wedges it (the same reasoning
# aspirations-precheck Phase 0-pre6 states for its own gate). The value here is
# the stderr banner plus the durable JSONL ledger -- a silent loss becomes a
# loud one, which is the whole defect.
#
# Scope is decided in ONE place (tree_write_fence.in_scope): .md bodies under
# knowledge/tree/. _tree.yaml is excluded -- the index already writes through
# _fileops.locked_modify_yaml.
#
# See also: .claude/rules/read-before-edit.md (Layer A behavioral rule),
# core/scripts/pre-edit-context-gate.sh (sibling advisory, path-presence only --
# it answers "did I read this file?", this one answers "is what I read still
# what is on disk?").
set -euo pipefail

_fail_open() { exit 0; }
trap '_fail_open' ERR

op="${1:-check}"

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh" 2>/dev/null || exit 0

# Extract file_path AND session_id from the hook JSON in ONE stdin pass -- stdin
# is consumable exactly once, and the agent fallback below needs the session id.
# file_path goes LAST so a path containing '|' still round-trips (the split below
# takes everything after the FIRST separator). python3 (not py -3) is correct
# here: this is a .sh hook that sources _paths.sh, the case python-invocation.md
# sanctions -- matching the sibling context-reads hook family.
hook_info=$(python3 -c "
import sys,json
try:
    d = json.load(sys.stdin)
    print((d.get('session_id','') or '') + '|' + (d.get('tool_input',{}).get('file_path','') or ''))
except Exception:
    print('|')
" 2>/dev/null) || exit 0

session_id="${hook_info%%|*}"
file_path="${hook_info#*|}"

[ -z "$file_path" ] && exit 0

# Resolve the agent. AYOAI_AGENT is NOT injected into Read/Write/Edit hooks --
# only PreToolUse[Bash] gets the inject -- so the bare env var is empty here and
# _paths.sh leaves AGENT_DIR empty along with it. The previous guard was
# `[ -z "${AGENT_DIR:-}" ] && exit 0`, which therefore made this fence a silent
# no-op at EVERY one of its four wiring points from the day it landed: `record`
# never wrote a baseline, so `check` had nothing to compare against and could
# never fire. Measured 2026-07-28 (g-115-3720): 18h live, zero baselines, zero
# ledger entries, while a real lost-update went undetected at 18:21 -- the very
# incident class the fence was built for. It hand-tests GREEN from a Bash call,
# because that path DOES get the inject, which is what kept it hidden.
# The sibling context-reads-record.sh documents this exact hazard at its line 34
# and already carries this exact fallback; this is that pattern, not a new one.
AGENT_NAME="${AYOAI_AGENT:-}"
if [ -z "$AGENT_NAME" ] && [ -n "$session_id" ]; then
    AGENT_NAME="$(bash "$CORE_ROOT/scripts/session-binding-read.sh" "$session_id" 2>/dev/null || true)"
fi
[ -z "$AGENT_NAME" ] && exit 0   # genuinely no agent bound -> nothing to fence against

# The python entry point owns scope, hashing, the ledger and the banner. Its
# stdout is a JSON verdict we deliberately DISCARD: a PreToolUse hook's stdout
# is interpreted by Claude Code as a decision payload, and this gate never
# decides. The banner it prints goes to stderr, which is what surfaces.
# `env VAR=... cmd` is required, not decorative: tree_write_fence._default_paths
# resolves its baseline + ledger locations through _paths.AGENT_DIR, which reads
# AYOAI_AGENT from the ENVIRONMENT -- passing the name any other way leaves the
# python side resolving nothing and re-creates the silent no-op above.
env AYOAI_AGENT="$AGENT_NAME" python3 "$CORE_ROOT/scripts/tree_write_fence.py" "$op" "$file_path" >/dev/null 2>>/dev/stderr || exit 0

exit 0
