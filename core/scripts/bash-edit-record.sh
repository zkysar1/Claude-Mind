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
# to it, so a partner's iteration-commit sweeps them — the 
# over-inclusion class (events.py, knowledge-graph-build.py: 545 LOC committed
# under the wrong agent with no fresh-eyes review, 2026-06-20).
#
# This hook records the Bash-tool delta into the SAME log so the EXISTING
# filter sees it too. PURELY ADDITIVE: no change to iteration-commit.sh, no
# denylist->allowlist flip. rb-1794 warns an allowlist on an INCOMPLETE log
# silently drops self-authored work; completing the log is the safe prerequisite
# and this is that step.
#
# SIGNAL — a cursor (last scan epoch) in the agent-wide session/ dir, shared
# by every session of the agent. On each Bash PostToolUse,
# record neutral framework files whose mtime is NEWER than the cursor (i.e.
# files changed since any session of this agent last ran this hook) then
# advance the cursor. mtime-delta (NOT a cumulative `git status`) bounds
# recording to recent changes; cumulative recording would absorb pre-existing
# partner WIP into this agent's OWN log and re-include it at commit time
# (), causing the very over-inclusion this prevents. First run of a
# session sets the cursor and records nothing (no prior window to bound the delta).
#
# SESSION STAMP (, ) — each record carries `sid`, the
# session that made the change; iteration-commit.sh --session-sid keys on it.
# The cursor is shared, so a change is recorded ONCE, by the first session whose
# command ends after it, and that session is often NOT the writer: a session
# running a long command was stamped with every sibling edit made meanwhile. So
# the stamp comes from the in-flight WINDOWS in _bash_inflight.py instead.
# bash-agent-inject opens one per command, this hook closes its own, and a change
# goes to the ONE session of this agent whose command was running when the file
# changed. Several such sessions: sid "" plus `candidates` (ambiguous). None:
# sid "" (a background job, a Write edit, another agent). --session-sid stages
# neither. Rows dedup on (file, mtime): on file alone, one stale row for a file
# hid every later change to it.
#
# NO GIT — the scan is a filesystem mtime walk, never `git status`/`git
# ls-files`. Adding a git command to every Bash call across all agents would
# increase shared-index .lock contention (more commit failures, the opposite of
# the goal). A walk of core/ + .claude/ is index-lock-free.
#
# GIT-IGNORED TREES ARE NOT WALKED () — the hooks rewrite files under
# core/logs/ on every Bash call and, since rows dedup on (file, mtime), each touch
# became a row: 36k a day per agent, none committable, all re-read by every call
# (0.7 s on a 27 MB log) and re-uploaded with the log. _edit_record_skip.py names
# the trees (the .gitignore directory rules under the two scan roots), so the walk
# needs no git to leave them out.
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
# session_id|window-key. The key hashes the command text, which here is the text
# bash-agent-inject emitted; a failure to derive it leaves the key empty and the
# recorder running.
_ids=$(printf '%s' "$input" | SDIR="$SCRIPT_DIR" python3 -c "
import sys, os, json
d = json.load(sys.stdin)
try:
    sys.path.insert(0, os.environ.get('SDIR', ''))
    from _bash_inflight import command_key
    key = command_key((d.get('tool_input') or {}).get('command') or '')
except Exception:
    key = ''
print(d.get('session_id', '') or '', key, sep='|')" 2>/dev/null || echo "")
_ids="${_ids%$'\r'}"
session_id="${_ids%%|*}"
cmd_key="${_ids#*|}"

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
LOGP="$log_path" CUR="$cursor_epoch" NOWE="$now_epoch" PROOT="$PROJECT_ROOT" SID="$session_id" \
    KEY="$cmd_key" SDIR="$SCRIPT_DIR" SROOT="$(agent_sessions_root "$agent" 2>/dev/null)" \
    python3 - <<'PYEOF' 2>/dev/null || true
import os, json, re, sys, time

# A session id is a uuid; anything else is dropped rather than written.
sid = os.environ.get("SID", "").strip()  # a Windows python may leave a trailing \r
if not re.fullmatch(r"[A-Za-z0-9_-]*", sid):
    sid = ""
proot = os.environ["PROOT"]
logp = os.environ["LOGP"]
cur = int(os.environ["CUR"])
nowe = int(os.environ["NOWE"])

# Close this command's window first so its start counts below; every window
# still open is a command some session of this agent is running right now.
# Without the module nothing is credited to anyone (sid ""), never the old
# first-to-finish stamp: a missing record is listed at commit, a wrong one ships.
try:
    sys.path.insert(0, os.environ.get("SDIR", ""))
    import _bash_inflight
    attribute = _bash_inflight.attribute
    sroot = os.environ.get("SROOT", "").strip()
    key = os.environ.get("KEY", "").strip()
    own_start = (_bash_inflight.close_window(os.path.join(sroot, sid), key)
                 if sroot and sid and key else None)
    windows = _bash_inflight.open_windows(sroot) if sroot else []
    if own_start is not None:
        windows.append((sid, own_start))
except Exception:
    windows = []
    def attribute(mtime, windows):
        return "", []

# Without the module the walk skips only the names it skipped before the list
# existed: extra rows are noise, a dropped edit is not.
try:
    from _edit_record_skip import skip_dir
except Exception:
    def skip_dir(rel_dir, name):
        return name in (".git", ".python-shim", "__pycache__", "node_modules", ".pytest_cache")

scan_roots = [os.path.join(proot, "core"), os.path.join(proot, ".claude")]

# Dedup on (file, mtime) against what is already logged (the Write/Edit
# recorder + prior runs). A change already recorded, by its writer's own
# recorder, keeps that record's sid.
seen = set()
try:
    with open(logp, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                seen.add((str(e.get("file") or "").replace("\\", "/"), int(e.get("mtime"))))
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
        rel_dir = os.path.relpath(dirpath, proot).replace("\\", "/")
        dirnames[:] = [d for d in dirnames if not skip_dir(rel_dir, d)]
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
            if (rel, m) in seen:
                continue
            seen.add((rel, m))
            rec_sid, cands = attribute(m, windows)
            entry = {"file": rel, "mtime": m, "edit_ts": now_iso, "goal_id": "", "sid": rec_sid}
            if cands:
                entry["candidates"] = cands
            new_entries.append(entry)

if new_entries:
    try:
        with open(logp, "a", encoding="utf-8") as f:
            for e in new_entries:
                f.write(json.dumps(e, separators=(",", ":")) + "\n")
    except Exception:
        pass
PYEOF

exit 0
