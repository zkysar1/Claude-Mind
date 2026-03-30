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
        MIND_DIR="$AGENT_DIR"
    fi
    export MSYS_NO_PATHCONV=1
fi
