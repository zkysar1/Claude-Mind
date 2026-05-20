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

# Known agent dirs: any sibling of PROJECT_ROOT with self.md sentinel.
is_agent_dir=0
for d in "$PROJECT_ROOT"/*/; do
    [ -d "$d" ] || continue
    [ -f "$d/self.md" ] || continue
    a=$(basename "$d")
    if [ "$a" = "$top_seg" ]; then
        is_agent_dir=1
        break
    fi
done

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
