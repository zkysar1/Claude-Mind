#!/usr/bin/env bash
# PostToolUse[Write,Edit] hook — tree consistency check + Layer A front-matter sync.
#
# When a tree node .md file is written or edited:
#   1. Advisory: warn if _tree.yaml has no entry for the file
#   2. Layer A mutation: auto-update mechanical front-matter fields
#      (last_updated, last_update_trigger.session, last_update_trigger.source)
#      via tree-front-matter-sync.py. Surgical line replacement preserves
#      unrelated lines byte-for-byte and keeps the original line endings.
#
# Layer A NEVER touches semantic fields (topic, summary, trigger.type) —
# those need LLM judgment and belong to /tree edit Step 4 (Layer B).
# guard-503 (Layer C) reminds the agent to run /tree edit when a direct
# Edit was materially semantic.
#
# CRITICAL — DO NOT add `set -e` or `set -o pipefail` here. Per guard-141,
# Claude Code hooks MUST fail open on every error path. A `cd` to a missing
# directory or a piped-command failure must not abort the hook — that would
# block the user's edit. Use explicit `|| true` / conditional checks instead.
#
# TIMEOUT BUDGET (2026-05-20, observed Windows + python-shim): the full hook
# chain (json-parse + binding-lookup + uncommitted-edits-record chained call
# + _tree.yaml grep + tree-front-matter-sync.py with file lock) measures
# ~8 seconds on Windows. The `.claude/settings.json` timeout for this hook
# is set to 30s as a safety margin. The previous 5s timeout caused silent
# kills before tree-front-matter-sync.py could run, leaving _tree.yaml's
# last_updated stale relative to .md front matter (observed across multiple
# nodes during Phase 3.6 / 2026-05-20). If you add work to the chain,
# re-measure and bump the timeout — `5` is no longer safe.
set -u

# CRITICAL — `_paths.sh` MUST be sourced before any python3 call below.
# It puts core/scripts/.python-shim/python3 on PATH. Without that, Windows
# `python3` resolves to the Microsoft Store stub and the python3 JSON parses
# below fail silently, emptying file_path so the hook exits doing nothing.
# DO NOT move this source statement below the python3 calls — that was the
# original bug. See CLAUDE.md "Python Invocation (Windows)".
SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)" || exit 0
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || exit 0
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_platform.sh" 2>/dev/null || true

# Read hook stdin JSON. Extract:
#   - tool_input.file_path → which file was just edited
#   - session_id           → for MIND_SID export so Layer A's mutation can
#                             record the current SID (PostToolUse hooks do NOT
#                             receive MIND_SID via env — that injection only
#                             happens for PreToolUse[Bash] via bash-agent-inject)
input=$(cat)
file_path=$(echo "$input" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('file_path',''))" 2>/dev/null || echo "")
session_id=$(echo "$input" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")

# Resolve MIND_AGENT from session-binding file BEFORE the chained call below.
# Hooks do NOT receive MIND_AGENT via env (PreToolUse[Bash]'s bash-agent-inject
# is the only injector, and it only fires on Bash tool calls — not Edit/Write).
# uncommitted-edits-record.sh exits early if MIND_AGENT is unbound, leaving
# alpha/session/uncommitted-edits.jsonl empty and breaking iteration-commit.sh's
# concurrent-partner first-person-authorship signal ( incident,
# alpha session 2026-05-17 commit 892e748). Resolve agent once here and prefix
# the chained call so the record script sees the same binding bash-agent-inject
# would have stamped on a direct LLM Bash call. The case-block resolution lower
# down (around line ~95) ALSO needs $agent for tree-front-matter-sync.py, and
# we hoist it here to share the lookup.
agent=""
if [ -n "$session_id" ]; then
    # Phase 2.6 binding (preferred): agents/<name>/sessions/<SID>/binding.yaml.
    # Without this, every Phase 2.6 session's tree-node front-matter
    # auto-sync silently fails (last_updated, trigger.session stay stale)
    # AND uncommitted-edits-record chained call receives MIND_AGENT=""
    # → breaks cross-agent attribution.
    for _bf in "$PROJECT_ROOT/${AGENTS_PARENT_DIR}"/*/sessions/"$session_id"/binding.yaml; do
        [ -f "$_bf" ] || continue
        _bd="${_bf%/sessions/*}"
        agent="${_bd##*/}"
        break
    done
    # Legacy fallback: pre-Phase-2.6 .active-agent-<SID>.
    if [ -z "$agent" ]; then
        binding="$PROJECT_ROOT/.active-agent-$session_id"
        if [ -f "$binding" ]; then
            read -r agent < "$binding" || true
            agent="${agent//[[:space:]]/}"
        fi
    fi
fi

# Chained: capture neutral-path edits for cross-agent attribution ().
# The record script gates internally on neutral-path + MIND_AGENT bound;
# we pass the original hook payload so it can extract file_path itself, AND
# we env-inject MIND_AGENT="$agent" so its empty-MIND_AGENT guard at line 39
# does not silently exit ( fix). Cannot register a separate PostToolUse
# hook because .claude/settings.json is deny-listed for agent edits — chaining
# here is the SSOT compromise.
echo "$input" | MIND_AGENT="$agent" bash "$SCRIPT_DIR/uncommitted-edits-record.sh" >/dev/null 2>&1 || true

if [ -z "$file_path" ]; then
    exit 0
fi

# Normalize to forward slashes
file_path="${file_path//\\//}"

# Only check .md files under knowledge/tree/
case "$file_path" in
    *knowledge/tree/*.md)
        # Skip _tree.yaml, _summary.json, and other _ prefixed files
        basename=$(basename "$file_path")
        if [[ "$basename" == _* ]]; then
            exit 0
        fi

        # ${WORLD_DIR:-} defends against `set -u` aborting if _paths.sh
        # silently failed to define WORLD_DIR. The [ ! -f ] check below
        # then catches the missing path naturally.
        TREE_YAML="${WORLD_DIR:-}/knowledge/tree/_tree.yaml"
        if [ ! -f "$TREE_YAML" ]; then
            exit 0
        fi

        # Extract the virtual path suffix (everything after knowledge/tree/)
        virtual_suffix="${file_path##*knowledge/tree/}"
        virtual_path="world/knowledge/tree/$virtual_suffix"

        # Quick string check: does this virtual path appear in _tree.yaml?
        if ! grep -qF "$virtual_path" "$TREE_YAML" 2>/dev/null; then
            echo "TREE SYNC: File '$virtual_path' was written but has no entry in _tree.yaml. Register with: bash core/scripts/tree-update.sh --add-child <parent> (with JSON on stdin), or update the file field if this is a renamed/moved file."
            # Skip Layer A — file is unregistered, _tree.yaml bump would no-op
            exit 0
        fi

        # Layer A: mutate mechanical front-matter fields (fail-open).
        # $agent was already resolved above (hoisted for the chained
        # uncommitted-edits-record.sh call, ). The script can look up
        # the in-flight goal for trigger.source via $agent. Each step is
        # conditional — never aborts the hook.

        MIND_SID="$session_id" MIND_AGENT="$agent" \
            python3 "$CORE_ROOT/scripts/tree-front-matter-sync.py" \
            --file "$file_path" \
            --virtual-path "$virtual_path" >/dev/null 2>&1
        ;;
esac

exit 0
