#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# bash-edit-record.sh — PostToolUse[Bash] companion to uncommitted-edits-record.sh.
#
# Closes the rb-1761 Bash-blind gap. uncommitted-edits-record.sh fires only on
# PostToolUse[Write|Edit|MultiEdit], so neutral-path framework files an agent
# CREATES or MODIFIES via the Bash tool (heredoc `cat >`, `cp`, `sed -i`, a
# build script that writes a file, a formatter, codegen) never land in
# <agent>/session/uncommitted-edits.jsonl. The partner-uncommitted-log filter
# in iteration-commit.sh () reads that log to drop partner-authored
# neutral files from a committer's `git add`. Bash-created files are invisible
# to it, so a partner's iteration-commit sweeps them — the 6
# over-inclusion class (events.py, knowledge-graph-build.py: 545 LOC committed
# under the wrong agent with no fresh-eyes review, 2026-06-20).
#
# This hook records the Bash-tool delta into the SAME log so the EXISTING
# filter sees it too. PURELY ADDITIVE: no change to iteration-commit.sh, no
# denylist->allowlist flip. rb-1794 warns an allowlist on an INCOMPLETE log
# silently drops self-authored work; completing the log is the safe prerequisite
# and this is that step.
#
# SIGNAL — per-session cursor (last scan epoch). On each Bash PostToolUse,
# record neutral framework files whose mtime is NEWER than the cursor (i.e.
# files this agent's just-finished command touched) then advance the cursor.
# mtime-delta (NOT a cumulative `git status`) bounds attribution to THIS
# command's window; cumulative recording would absorb pre-existing partner WIP
# into this agent's OWN log and re-include it at commit time (),
# causing the very over-inclusion this prevents. First run of a session sets
# the cursor and records nothing (no prior window to bound the delta).
#
# NO GIT — the scan is a filesystem mtime walk, never `git status`/`git
# ls-files`. Adding a git command to every Bash call across all agents would
# increase shared-index .lock contention (more commit failures, the opposite of
# the goal). A walk of core/ + .claude/ is index-lock-free.
#
# Fail-open EVERYWHERE (guard-141): a record failure must NEVER block the LLM's
# command. No `set -e`. No `set -o pipefail`. Every probe guarded.

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)" || exit 0
# _paths.sh MUST precede any python3 call (puts core/scripts/.python-shim on
# PATH so `python3` dispatches to `py -3`, not the Windows Store stub) AND it
# exports PROJECT_ROOT, AGENTS_PARENT_DIR, and the agent_dir() helper.
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || exit 0

# PostToolUse[Bash] hooks receive NO MIND_AGENT in env — PreToolUse[Bash]'s
# bash-agent-inject stamps the COMMAND env, not the hook env. Resolve the agent
# from the payload's session_id exactly like tree-sync-check.sh does.
input=$(cat 2>/dev/null) || exit 0
session_id=$(printf '%s' "$input" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")

agent="${MIND_AGENT:-}"
if [ -z "$agent" ] && [ -n "$session_id" ]; then
    # Phase 2.6 binding (preferred): agents/<name>/sessions/<SID>/binding.yaml
    for _bf in "$PROJECT_ROOT/${AGENTS_PARENT_DIR}"/*/sessions/"$session_id"/binding.yaml; do
        [ -f "$_bf" ] || continue
        _bd="${_bf%/sessions/*}"
        agent="${_bd##*/}"
        break
    done
    # Legacy fallback: pre-Phase-2.6 .active-agent-<SID>
    if [ -z "$agent" ]; then
        _binding="$PROJECT_ROOT/.active-agent-$session_id"
        if [ -f "$_binding" ]; then
            read -r agent < "$_binding" 2>/dev/null || true
            agent="${agent//[[:space:]]/}"
        fi
    fi
fi
[ -n "$agent" ] || exit 0

# Agent session dir via the _paths.sh SSOT helper — never hand-join
# PROJECT_ROOT/agent (path-resolution.md "Agent Paths").
_asd="$(agent_dir "$agent" 2>/dev/null)/session"
[ -d "$_asd" ] || exit 0
log_path="$_asd/uncommitted-edits.jsonl"
cursor_path="$_asd/.bash-edit-cursor"

now_epoch=$(date +%s 2>/dev/null || echo "")
case "$now_epoch" in ''|*[!0-9]*) exit 0 ;; esac

# First run of a session: set the cursor, record nothing. Without a prior
# window the delta is unbounded and would over-capture pre-existing partner WIP
# as this agent's (the cumulative-mis-attribution failure mode noted above).
if [ ! -f "$cursor_path" ]; then
    printf '%s\n' "$now_epoch" > "$cursor_path" 2>/dev/null || true
    exit 0
fi
cursor_epoch=$(cat "$cursor_path" 2>/dev/null || echo "")
case "$cursor_epoch" in
    ''|*[!0-9]*) cursor_epoch=$((now_epoch - 120)) ;;  # corrupt -> small window
esac

# Advance the cursor BEFORE scanning so the next Bash call bounds its own
# window from here; same-window re-records are deduped against the log below.
printf '%s\n' "$now_epoch" > "$cursor_path" 2>/dev/null || true

# Record neutral framework files with mtime in (cursor, now]. Python for
# portable mtime + JSONL. Scoped to core/ + .claude/ (where over-inclusion-prone
# framework files live: core/scripts/*.py|*.sh, core/config/*, .claude/skills,
# .claude/rules). goal_id is left empty: the partner filter keys only on `file`;
# a team-state round-trip per Bash call is not worth the hot-path latency.
LOGP="$log_path" CUR="$cursor_epoch" NOWE="$now_epoch" PROOT="$PROJECT_ROOT" \
    python3 - <<'PYEOF' 2>/dev/null || true
import os, json, time

proot = os.environ["PROOT"]
logp = os.environ["LOGP"]
cur = int(os.environ["CUR"])
nowe = int(os.environ["NOWE"])

scan_roots = [os.path.join(proot, "core"), os.path.join(proot, ".claude")]
SKIP_DIRS = {".git", ".python-shim", "__pycache__", "node_modules", ".pytest_cache"}

# Dedup against what is already logged (the Write/Edit recorder + prior runs).
seen = set()
try:
    with open(logp, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                seen.add(json.loads(line).get("file"))
            except Exception:
                pass
except FileNotFoundError:
    pass
except Exception:
    pass

now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
new_entries = []
for root in scan_roots:
    if not os.path.isdir(root):
        continue
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            try:
                m = int(os.path.getmtime(fp))
            except Exception:
                continue
            # (cursor, now]; +2s slack absorbs same-second clock granularity.
            if m <= cur or m > nowe + 2:
                continue
            rel = os.path.relpath(fp, proot).replace("\\", "/")
            if rel in seen:
                continue
            seen.add(rel)
            new_entries.append(
                {"file": rel, "mtime": m, "edit_ts": now_iso, "goal_id": ""}
            )

if new_entries:
    try:
        with open(logp, "a", encoding="utf-8") as f:
            for e in new_entries:
                f.write(json.dumps(e, separators=(",", ":")) + "\n")
    except Exception:
        pass
PYEOF

exit 0
