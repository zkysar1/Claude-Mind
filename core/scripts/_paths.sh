#!/usr/bin/env bash
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
        export PATH="$_PY_SHIM_DIR:$PATH"
    fi
    unset _PY_TARGET
fi
unset _PY_SHIM_DIR

# --- Session ID ---
# AYOAI_SESSION_ID is set by the caller (hooks extract from stdin JSON,
# LLM Bash calls use AYOAI_AGENT prefix which bypasses SID resolution).
# No shared-file fallback — .latest-session-id was a single-writer design
# that broke when multiple terminals ran in the same directory.

# --- Tier 4: Per-agent private state ---
# Resolution: AYOAI_AGENT env var. That's it. One path.
# The LLM sets this on every Bash call. Hooks don't have it — they
# fall through to the auto-detect block below.
AGENT_NAME="${AYOAI_AGENT:-}"

# --- External path configuration ---
# world/ and meta/ live at user-supplied external paths (shared drive, NAS, etc.).
# Each agent stores its own config in <agent>/local-paths.conf.
# Falls back to PROJECT_ROOT/world and PROJECT_ROOT/meta when unconfigured —
# this keeps all scripts importable before /start.
if [ -n "$AGENT_NAME" ] && [ -f "$PROJECT_ROOT/$AGENT_NAME/local-paths.conf" ]; then
    source "$PROJECT_ROOT/$AGENT_NAME/local-paths.conf"
else
    # AYOAI_AGENT unset — use first available conf (hooks don't have the env var)
    for _CONF in "$PROJECT_ROOT"/*/local-paths.conf; do
        [ -f "$_CONF" ] && source "$_CONF" && break
    done
    unset _CONF
fi

# --- Tier 2: Meta-strategies ---
# Priority: env var > config file > PROJECT_ROOT/meta
META_DIR="${AYOAI_META:-${META_PATH:-$PROJECT_ROOT/meta}}"

# --- Tier 3: Collective domain state ---
# Priority: env var > config file > PROJECT_ROOT/world
WORLD_DIR="${AYOAI_WORLD:-${WORLD_PATH:-$PROJECT_ROOT/world}}"
if [ -n "$AGENT_NAME" ]; then
    AGENT_DIR="$PROJECT_ROOT/$AGENT_NAME"
else
    AGENT_DIR=""
fi
