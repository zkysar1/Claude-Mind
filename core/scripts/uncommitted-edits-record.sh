#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# uncommitted-edits-record.sh — Append agent's uncommitted neutral-path edits.
#
# Closes the  between-claim attribution gap: 's concurrent-
# partner filter in iteration-commit.sh only sees partner.in_flight. When a
# partner's in_flight is null (inter-claim gap, pause-then-edit window), the
# filter has no signal and partner-authored neutral-path edits get absorbed
# by the wrong agent's commit. This log records {file, mtime, edit_ts,
# goal_id} per agent so iteration-commit can distinguish self-authored from
# partner-authored neutral-path files even when in_flight is cleared.
#
# Invocation: hook payload JSON on stdin. tool_input.file_path is read and
# normalized. Standard Claude Code PostToolUse[Write|Edit|MultiEdit] payload.
# Chained from tree-sync-check.sh because the .claude/settings.json deny rule
# (Edit(*/.claude/settings*)) prevents the agent from registering a new
# top-level hook; tree-sync-check already fires on the same matcher set.
#
# Storage: <agent>/session/uncommitted-edits.jsonl, append-only. Single
# writer per agent. Read by partner iteration-commit. Cleared by
# iteration-commit.sh after a successful self-commit.
#
# Neutral-path gate: skip paths under any known agent dir (handled by
# iteration-commit's namespace filter), WORLD_DIR (changelog-tracked),
# META_DIR (rare). Only paths at neutral locations (core/, .claude/,
# top-level files, bench/, etc.) get logged.
#
# Fail-open EVERYWHERE: a record failure must never block the LLM's edit.
# No `set -e`. No pipefail. All probes guarded with `|| true` or `2>/dev/null`.

set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)" || exit 0
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || exit 0

# MIND_AGENT must be bound. Without it we have no destination log path.
if [ -z "${MIND_AGENT:-}" ]; then
    exit 0
fi

# Read hook payload JSON. Extract tool_input.file_path.
input=$(cat)
file_path=$(echo "$input" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('file_path',''))" 2>/dev/null || echo "")
if [ -z "$file_path" ]; then
    exit 0
fi

# Normalize backslashes (Windows Edit-tool payloads).
file_path="${file_path//\\//}"

# Normalize Windows drive-letter paths (C:/foo) to MSYS POSIX form (/c/foo)
# so the PROJECT_ROOT case match below works. Claude Code hooks deliver
# Windows-form paths; bash's `pwd` inside Git Bash returns POSIX-form. Before
# this normalization the case match silently failed on Windows: rel_path
# stayed absolute, the case-`/*` exit at line 62 did NOT match (C: doesn't
# start with /), top_seg resolved to literal `C:` which matches no agent
# name, and EVERY path slipped past the filter into the log with absolute
# path (alpha's log had 175 entries — including 36 agent-prefixed — all
# absolute, all wrong). The agent-dir filter underneath this block depends
# on rel_path being the project-relative form ("agents/alpha/foo"), not the
# absolute drive path.
case "$file_path" in
    [A-Za-z]:/*)
        _drive=$(printf '%s' "${file_path%%:*}" | tr '[:upper:]' '[:lower:]')
        file_path="/${_drive}${file_path#*:}"
        ;;
esac

# Convert absolute paths to repo-relative when they land under PROJECT_ROOT.
# Paths outside PROJECT_ROOT (e.g., temp files, sibling repos) stay absolute
# and will fail the neutral-path test below.
case "$file_path" in
    "$PROJECT_ROOT"/*) rel_path="${file_path#$PROJECT_ROOT/}" ;;
    /*) rel_path="$file_path" ;;
    *) rel_path="$file_path" ;;
esac

# Reject paths outside PROJECT_ROOT — iteration-commit only operates in
# PROJECT_ROOT, so cross-repo edits don't need this log.
case "$rel_path" in
    /*) exit 0 ;;
esac

# Neutral-path test: skip if first segment is a known agent dir OR if path
# starts with world/ or meta/ virtual prefixes.
top_seg="${rel_path%%/*}"

# Known agent dirs: depends on AGENTS_PARENT_DIR layout.
# Modern (AGENTS_PARENT_DIR=agents): any path under "agents/" is agent-private
# (every dir there has a self.md by construction — /start refuses agent names
# that collide with non-agent siblings under agents/).
# Legacy (AGENTS_PARENT_DIR=""): walk PROJECT_ROOT/*/ for self.md sentinels.
# Before this branch existed, the legacy loop was the only path, and under
# AGENTS_PARENT_DIR=agents it silently never matched — agent-private edits
# under agents/<name>/ landed in the neutral-path log as false-positives
# (alpha's log accumulated 36 such entries before the fix).
is_agent_dir=0
if [ -n "$AGENTS_PARENT_DIR" ]; then
    if [ "$top_seg" = "$AGENTS_PARENT_DIR" ]; then
        is_agent_dir=1
    fi
else
    for d in "$PROJECT_ROOT"/*/; do
        [ -d "$d" ] || continue
        [ -f "$d/self.md" ] || continue
        a=$(basename "$d")
        if [ "$a" = "$top_seg" ]; then
            is_agent_dir=1
            break
        fi
    done
fi

# Virtual prefixes (world/, meta/) are external owned-domain.
case "$rel_path" in
    world/*|meta/*) is_agent_dir=1 ;;
esac

if [ "$is_agent_dir" = "1" ]; then
    exit 0
fi

# File-existence guard: Edit may target a not-yet-created path (Write case
# before content is flushed) — both are still legitimate to record. But a
# deleted path produces no mtime; skip it.
if [ ! -e "$file_path" ]; then
    exit 0
fi

# Get mtime as epoch seconds. py -3 because POSIX `date -r` is not portable
# on Windows Git-Bash and would silently fail.
mtime=$(FILE_E="$file_path" py -3 - 2>/dev/null <<'PYEOF'
import os, sys
try:
    print(int(os.path.getmtime(os.environ["FILE_E"])))
except Exception:
    print(0)
PYEOF
)
if [ -z "$mtime" ] || [ "$mtime" = "0" ]; then
    exit 0
fi

now_iso=$(date +%Y-%m-%dT%H:%M:%S)

# Best-effort goal_id from team-state in_flight. NULL when no goal is claimed.
goal_id=$(bash "$SCRIPT_DIR/team-state-read.sh" --field "agent_status.${MIND_AGENT}.in_flight.goal_id" --json 2>/dev/null | tr -d '"' || echo "")
if [ "$goal_id" = "null" ] || [ -z "$goal_id" ]; then
    goal_id=""
fi

# Append the record. AGENT_DIR is resolved by _paths.sh.
log_path="$AGENT_DIR/session/uncommitted-edits.jsonl"
mkdir -p "$(dirname "$log_path")" 2>/dev/null || exit 0

# JSON-escape the rel_path (handle backslash + quote in case rare paths slip through).
esc_path="${rel_path//\\/\\\\}"
esc_path="${esc_path//\"/\\\"}"

printf '{"file":"%s","mtime":%s,"edit_ts":"%s","goal_id":"%s"}\n' \
    "$esc_path" "$mtime" "$now_iso" "$goal_id" >> "$log_path" 2>/dev/null || true

exit 0
