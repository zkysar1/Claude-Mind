#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Centralized path resolution for the cognitive core.
# Source this at the top of every shell script.
#
# 4-tier architecture:
#   core/          — Framework (immutable)
#   meta/          — Meta-strategies (domain-agnostic, independent of domain data)
#   world/         — Collective domain state (shared across agents)
#   <agent-name>/  — Per-agent private state (one directory per agent)
#
# Sets: SCRIPT_DIR, CORE_ROOT, PROJECT_ROOT, CONFIG_DIR, REPO_ROOT
#       WORLD_DIR, META_DIR, AGENT_NAME, AGENT_DIR
#
# BASH_SOURCE[0] resolves to THIS file's location (not the caller's $0).
# This is critical — it anchors all paths to core/scripts/ regardless of cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"       # core/
PROJECT_ROOT="$(cd "$CORE_ROOT/.." && pwd)"      # repo root
CONFIG_DIR="$CORE_ROOT/config"
REPO_ROOT="$PROJECT_ROOT"                        # legacy alias

# --- Python 3 interpreter resolution ---
# Ensures 'python3' is available on PATH regardless of platform.
# On Windows (Git Bash): python3 may be a Microsoft Store stub — falls back to py launcher.
# On any platform: falls back to 'python' if it's Python 3.8+.
# Creates a cached shim script in .python-shim/ (gitignored). Delete to re-detect.
_PY_SHIM_DIR="$SCRIPT_DIR/.python-shim"
if [ -x "$_PY_SHIM_DIR/python3" ]; then
    export PATH="$_PY_SHIM_DIR:$PATH"
    # Backfill: older shim dirs only had python3 — also expose `python` for
    # LLM-issued bare `python` pipes (Windows Store stub eats the system one).
    if [ ! -x "$_PY_SHIM_DIR/python" ]; then
        cp "$_PY_SHIM_DIR/python3" "$_PY_SHIM_DIR/python" 2>/dev/null && \
            chmod +x "$_PY_SHIM_DIR/python"
    fi
elif ! python3 -c "pass" &>/dev/null; then
    _PY_TARGET=""
    if command -v py &>/dev/null; then
        _PY_TARGET="py"
    elif command -v python &>/dev/null && python -c "import sys;sys.exit(0 if sys.version_info>=(3,8) else 1)" &>/dev/null; then
        _PY_TARGET="python"
    fi
    if [ -n "$_PY_TARGET" ]; then
        mkdir -p "$_PY_SHIM_DIR"
        printf '#!/usr/bin/env bash\nexec %s "$@"\n' "$_PY_TARGET" > "$_PY_SHIM_DIR/python3"
        chmod +x "$_PY_SHIM_DIR/python3"
        cp "$_PY_SHIM_DIR/python3" "$_PY_SHIM_DIR/python"
        chmod +x "$_PY_SHIM_DIR/python"
        export PATH="$_PY_SHIM_DIR:$PATH"
    fi
    unset _PY_TARGET
fi
unset _PY_SHIM_DIR

# --- Centralized Python bytecode cache (2026-05-19, plan v1 step 0.20) ---
# Without this, Python writes __pycache__/ subdirs next to every .py file —
# producing scattered cache dirs across core/scripts/, mind_api/src/, tests/,
# every agent's reports/, etc. PYTHONPYCACHEPREFIX (Python 3.8+) routes ALL
# bytecode caches to a single mirrored tree under core/.pycache/, keeping
# the source tree clean. Already gitignored. Inherited by every subprocess
# that sources _paths.sh.
export PYTHONPYCACHEPREFIX="$PROJECT_ROOT/core/.pycache"

# --- Session ID ---
# MIND_SESSION_ID is set by the caller (hooks extract from stdin JSON,
# LLM Bash calls use MIND_AGENT prefix which bypasses SID resolution).
# No shared-file fallback — that path was retired with the bridge file
# (rb-386); multi-terminal race made it a single-writer design that
# broke under concurrent agents.

# --- Tier 4: Per-agent private state ---
# Resolution: MIND_AGENT env var. That's it. One path.
# The PreToolUse[Bash] hook (bash-agent-inject.sh) auto-injects this env var
# on LLM Bash calls by reading .active-agent-<SID>. Hooks called directly by
# Claude Code (SessionStart, Stop, etc.) don't have the env var — they fall
# through to the auto-detect block below.
AGENT_NAME="${MIND_AGENT:-}"

# --- Agent-dir resolution (Phase 2.5.C/D) ---
# Empty string = agent dirs at PROJECT_ROOT (legacy layout, pre-2026-05-19).
# Currently "agents" — agent dirs live at PROJECT_ROOT/agents/<name>.
# AGENTS_PARENT_DIR MUST stay in sync across all 3 resolver layers:
#   core/scripts/_paths.py
#   core/scripts/_paths.sh       (this file)
#   mind_api/src/agent_paths.py
# Plus 2 import-cycle-proof inlined copies:
#   core/scripts/_agents.py
#   core/scripts/path-resolution-hook.py
# See CLAUDE.md "Agent-dir Resolution" section.
AGENTS_PARENT_DIR="agents"

agents_root() {
    if [ -n "$AGENTS_PARENT_DIR" ]; then
        printf '%s/%s' "$PROJECT_ROOT" "$AGENTS_PARENT_DIR"
    else
        printf '%s' "$PROJECT_ROOT"
    fi
}

agent_dir() {
    if [ -n "$AGENTS_PARENT_DIR" ]; then
        printf '%s/%s/%s' "$PROJECT_ROOT" "$AGENTS_PARENT_DIR" "$1"
    else
        printf '%s/%s' "$PROJECT_ROOT" "$1"
    fi
}

# --- Per-session dirs (Phase 2.6) ---
# Each agent has agents/<name>/sessions/<SID>/ for that session's snapshot
# state (binding, runner-token, scratch, iteration-checkpoint, etc.).
# Cross-session state stays in agents/<name>/session/ (singular).
SESSIONS_DIRNAME="sessions"
SESSION_DIRNAME="session"

agent_sessions_root() {
    # Parent dir for all per-session dirs for one agent.
    # Args: $1=agent_name
    printf '%s/%s' "$(agent_dir "$1")" "$SESSIONS_DIRNAME"
}

agent_session_dir() {
    # Per-session dir for one (agent, sid). Dir name IS the sid.
    # Args: $1=agent_name, $2=sid
    printf '%s/%s/%s' "$(agent_dir "$1")" "$SESSIONS_DIRNAME" "$2"
}

agent_state_dir() {
    # Cross-session state dir (the singular 'session/' dir).
    # Holds files that survive across sessions (agent-state, agent-mode,
    # persona-active, handoff.yaml, pending-questions.yaml, etc.).
    # Args: $1=agent_name
    printf '%s/%s' "$(agent_dir "$1")" "$SESSION_DIRNAME"
}

# --- External path configuration ---
# world/ and meta/ live at user-supplied external paths (shared drive, NAS, etc.).
# Each agent stores its own config in <agent>/local-paths.conf.
#
# Plan v1 step 0.1 (2026-05-19) — HARD CUT: NO fallback to PROJECT_ROOT/world
# or PROJECT_ROOT/meta when unconfigured. See _paths.py header for the full
# rationale. The shell variant mirrors the Python behavior: WORLD_DIR/META_DIR
# resolve to empty strings when no conf is found, and `$WORLD_DIR/foo`
# expansions produce `/foo` — clearly broken paths that fail loudly on the
# next filesystem operation rather than silently writing into PROJECT_ROOT.
if [ -n "$AGENT_NAME" ] && [ -f "$(agent_dir "$AGENT_NAME")/local-paths.conf" ]; then
    source "$(agent_dir "$AGENT_NAME")/local-paths.conf"
else
    # MIND_AGENT unset — use first available conf (hooks don't have the env var).
    # Fall-through STAYS (prevents PROJECT_ROOT/world,meta cruft, ); the
    # warning makes the silent wrong-agent read visible (6 root cause B).
    # The two PreToolUse hooks that source this file (bash-agent-inject.sh,
    # path-resolution-hook.sh) use `2>/dev/null`, which suppresses stderr emitted
    # during sourcing — so the warning fires only for LLM Bash tool calls where
    # the inject hook actually missed (MIND_AGENT genuinely empty at use time).
    for _CONF in "$(agents_root)"/*/local-paths.conf; do
        if [ -f "$_CONF" ]; then
            source "$_CONF"
            if [ -z "$AGENT_NAME" ]; then
                echo "[_paths] WARN: MIND_AGENT unset, falling through to first agent: $(basename "$(dirname "$_CONF")"). This is usually a hook miss -- check core/logs/bash-inject-misses.jsonl for the SID (g-115-1146)." >&2
            fi
            break
        fi
    done
    unset _CONF
fi

# --- Tier 2: Meta-strategies ---
# Priority: env var MIND_META > _local-conf META_PATH > empty.
# Plan v1 step 0.1 (2026-05-19): removed PROJECT_ROOT/meta fallback.
META_DIR="${MIND_META:-${META_PATH:-}}"

# --- Tier 3: Collective domain state ---
# Priority: env var MIND_WORLD > _local-conf WORLD_PATH > empty.
# Plan v1 step 0.1 (2026-05-19): removed PROJECT_ROOT/world fallback.
WORLD_DIR="${MIND_WORLD:-${WORLD_PATH:-}}"
if [ -n "$AGENT_NAME" ]; then
    AGENT_DIR="$(agent_dir "$AGENT_NAME")"
else
    AGENT_DIR=""
fi

# --- Windows shell auto-detect ---
# Python subprocess helpers (state-update-audit.py, precheck-eval.py, etc.) read
# MIND_SHELL with fallback to 'bash'. On Windows the default /usr/bin/bash
# under MINGW64/MSYS doesn't accept `C:/...` drive-letter paths when invoked
# as a bare name via PATH lookup, causing silent "No such file or directory"
# failures for helper .sh calls. Fix: auto-detect and export MIND_SHELL as
# the Windows-style absolute path of the local bash via `cygpath -m`. Same
# pattern as background-jobs.sh — when `bash_path` is absolute, Python
# subprocess invokes it directly (no PATH lookup) and the MSYS/Git-Bash
# binary parses `C:/...` script args correctly. See
# world/conventions/windows-shell-config.md.
# --- git_available — cached probe (Phase 2.2 packaging cleanup) -------------
# Returns 0 if git is installed AND PROJECT_ROOT is a git work tree; 1 else.
# Cached per-process via MIND_GIT_AVAILABLE so repeat callers don't re-fork
# `git rev-parse`. Framework degrades gracefully without git — iteration-commit.sh
# soft-exits, pre-commit gates don't fire (hooks don't exist), post-commit
# daemon recycle is lost. Learning loop is git-independent.
git_available() {
    if [ -n "${MIND_GIT_AVAILABLE:-}" ]; then
        [ "$MIND_GIT_AVAILABLE" = "1" ]
        return $?
    fi
    if ! command -v git >/dev/null 2>&1; then
        export MIND_GIT_AVAILABLE=0
        return 1
    fi
    if ! (cd "$PROJECT_ROOT" && git rev-parse --git-dir >/dev/null 2>&1); then
        export MIND_GIT_AVAILABLE=0
        return 1
    fi
    export MIND_GIT_AVAILABLE=1
    return 0
}

case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*)
        if [ -z "${MIND_SHELL:-}" ] && command -v cygpath &>/dev/null; then
            _bash_abs="$(cygpath -m "$(command -v bash)" 2>/dev/null)"
            # rb-1472: prefer the login-launcher bin/bash.exe over the raw
            # usr/bin/bash.exe. `command -v bash` yields /usr/bin/bash inside
            # Git Bash, but the raw usr/bin binary does NOT self-configure its
            # PATH when a Windows-process parent (a daemon, or pytest from a
            # clean PATH) re-spawns it, so coreutils (dirname/sed/tr) go missing
            # and wrappers computing SCRIPT_DIR via $(cd "$(dirname …)" && pwd)
            # mis-resolve -> nested `bash $SCRIPT_DIR/sub.sh` rc=127. Substitute
            # usr/bin -> bin when the sibling launcher exists (this MIND_SHELL
            # is the SSOT every Python resolver honors first).
            _bash_bin="${_bash_abs/usr\/bin/bin}"
            if [ "$_bash_bin" != "$_bash_abs" ] && [ -x "$(cygpath -u "$_bash_bin" 2>/dev/null)" ]; then
                _bash_abs="$_bash_bin"
            fi
            if [ -n "$_bash_abs" ] && [ -x "$(cygpath -u "$_bash_abs" 2>/dev/null)" ]; then
                export MIND_SHELL="$_bash_abs"
            fi
            unset _bash_abs _bash_bin
        fi
        # If auto-detect failed AND stderr is a TTY, surface to the user
        # once per interactive shell. Subprocess invocations (stderr
        # captured, not a TTY) stay silent to avoid the thousand-plus
        # stderr spam per aspirations-loop iteration.
        if [ -z "${MIND_SHELL:-}" ] && [ -t 2 ]; then
            echo "WARN: MIND_SHELL unset on Windows and auto-detect failed — Python subprocess helpers may fail silently. See world/conventions/windows-shell-config.md" >&2
        fi
        ;;
esac
