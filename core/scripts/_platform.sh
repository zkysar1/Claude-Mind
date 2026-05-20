# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Cross-platform path fix for Git Bash on Windows (MSYS2).
# Source this AFTER sourcing _paths.sh, BEFORE exec python3.
#
# Problem: MSYS converts /skill-name args to C:/Program Files/Git/skill-name.
# Fix: convert all path variables to Windows paths (so python3 finds scripts),
# then disable MSYS arg conversion (so /skill-name passes through unchanged).
# On Linux/Mac: no-op (MSYSTEM is unset).
# Every variable from _paths.sh must be converted here. If you add a new
# path variable to _paths.sh, add a cygpath line below or python3 will
# receive MSYS Unix paths (/c/...) that resolve to the wrong location.

# Force Python I/O to UTF-8 for every script launched through a shell wrapper
# that sources this file. Windows defaults stdin/stdout/stderr to cp1252, which
# mojibakes non-ASCII JSON (em-dashes, smart quotes) before json.loads sees it.
# Every `-add.sh` wrapper sources _platform.sh before `exec python3`, so this
# one line covers all 23 stdin-reading scripts. rb-316 (2026-04-19) documents
# the original failure: em-dash stored as three cp1252-escape codepoints
# instead of \u2014. Do NOT remove without replacing — the stdin fix is NOT
# redundantly applied per script.
export PYTHONIOENCODING=utf-8

if [ "${MSYSTEM:-}" != "" ] && command -v cygpath &>/dev/null; then
    REPO_ROOT="$(cygpath -m "$REPO_ROOT")"
    PROJECT_ROOT="$(cygpath -m "$PROJECT_ROOT")"
    CORE_ROOT="$(cygpath -m "$CORE_ROOT")"
    CONFIG_DIR="$(cygpath -m "$CONFIG_DIR")"
    # AGENT_DIR may be empty (no agent bound); META_DIR and WORLD_DIR are always set
    if [ -n "$META_DIR" ]; then
        META_DIR="$(cygpath -m "$META_DIR")"
    fi
    if [ -n "$WORLD_DIR" ]; then
        WORLD_DIR="$(cygpath -m "$WORLD_DIR")"
    fi
    if [ -n "$AGENT_DIR" ]; then
        AGENT_DIR="$(cygpath -m "$AGENT_DIR")"
    fi
    export MSYS_NO_PATHCONV=1
fi
