#!/usr/bin/env bash
# test_check_daemon_endpoint_registry.sh — regression test for the
# daemon-endpoint-registry pre-commit gate (, ).
#
# Built for the 2026-05-16 daemon-only architecture Layer B gate. The gate
# inspects mind_api/src/endpoints/__init__.py load_all imports and verifies
# each `from . import X` resolves to an existing module file in the
# post-commit tree (staged in precommit mode, on-disk in --audit mode).
#
# Sandbox: a fresh temp git repo with a minimal endpoints/__init__.py and
# 2-3 module files. Each case mutates the staged state via git index ops,
# runs the gate, and asserts exit code + stderr substring. The real
# PROJECT_ROOT and the real endpoints/__init__.py are NEVER touched.
#
# Run: bash core/scripts/tests/test_check_daemon_endpoint_registry.sh
# Exit 0 = all pass, exit 1 = any case fails.
#
# Covers Section 6 design cases 1, 2, 3, 5, 6, 8.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REAL_GATE="$SCRIPT_DIR/../check-mind-api-endpoint-registry.py"

if [ ! -f "$REAL_GATE" ]; then
    echo "FATAL: cannot find $REAL_GATE" >&2
    exit 1
fi

# Pick the right Python invocation for the platform (mirrors pre-commit).
case "$(uname -s 2>/dev/null || echo unknown)" in
    MINGW*|MSYS*|CYGWIN*) PY="py -3" ;;
    *) PY="python3" ;;
esac

SANDBOX=$(mktemp -d -t daemon-registry-test-XXXXXX)
trap 'rm -rf "$SANDBOX"' EXIT

PASS_COUNT=0
FAIL_COUNT=0

# init_repo — wipe sandbox, init a fresh git repo with baseline endpoints
# tree. The baseline mirrors the real __init__.py's load_all shape but uses
# only 3 modules so the test stays small and readable.
init_repo() {
    rm -rf "$SANDBOX"/*
    rm -rf "$SANDBOX"/.git 2>/dev/null || true
    (
        cd "$SANDBOX"
        git init -q
        git config user.email "test@example.com"
        git config user.name "test"
        mkdir -p mind_api/src/endpoints mind_api/src/world
        cat > mind_api/src/endpoints/__init__.py <<'PYEOF'
"""Endpoint registry — baseline test fixture."""
from __future__ import annotations

def load_all():
    from . import health as _health
    from ..world import tree as _tree
    from ..world import pipeline as _pipeline
    return [_health, _tree, _pipeline]
PYEOF
        cat > mind_api/src/endpoints/health.py <<'PYEOF'
def register(routes): pass
PYEOF
        cat > mind_api/src/world/tree.py <<'PYEOF'
def register(routes): pass
PYEOF
        cat > mind_api/src/world/pipeline.py <<'PYEOF'
def register(routes): pass
PYEOF
        # __init__.py for the world package to make the import path valid
        # for module-load semantics. Not strictly checked by the AST gate
        # (which inspects the registry's __init__.py only), but kept for
        # realism so future expansion to a smoke-test mode would work.
        : > mind_api/src/__init__.py
        : > mind_api/src/world/__init__.py
        git add -A
        git commit -q -m "baseline"
    )
}

# run_case <label> <expected_exit> <expected_stderr_substring|""> [--audit]
# Runs the gate in $SANDBOX with appropriate cwd. Captures stderr to a
# temp file and pattern-matches the substring if non-empty.
run_case() {
    local label="$1"
    local expected_exit="$2"
    local expected_stderr="$3"
    local audit_flag="${4:-}"

    local stderr_file
    stderr_file=$(mktemp -t registry-gate-stderr-XXXXXX)
    local rc=0
    (
        cd "$SANDBOX"
        if [ -n "$audit_flag" ]; then
            $PY "$REAL_GATE" --audit
        else
            $PY "$REAL_GATE"
        fi
    ) 2> "$stderr_file" || rc=$?

    local stderr_content
    stderr_content=$(cat "$stderr_file")
    rm -f "$stderr_file"

    local ok=1
    if [ "$rc" != "$expected_exit" ]; then
        ok=0
    fi
    if [ -n "$expected_stderr" ] && ! printf '%s' "$stderr_content" | grep -qF "$expected_stderr"; then
        ok=0
    fi

    if [ "$ok" = "1" ]; then
        echo "PASS $label (exit=$rc)"
        PASS_COUNT=$((PASS_COUNT + 1))
    else
        echo "FAIL $label"
        echo "  expected_exit=$expected_exit got=$rc"
        echo "  expected_stderr=\"$expected_stderr\""
        echo "  actual_stderr=\"$stderr_content\""
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
}

# ─── Case 1: Clean repo, all imports resolve → exit 0, silent ─────────────
init_repo
run_case "1-clean-repo-all-resolve" 0 ""

# ─── Case 2: Stage delete of mind_api/src/world/tree.py → exit 1, lists it ─
# git rm stages the deletion; __init__.py still has the import.
init_repo
(
    cd "$SANDBOX"
    git rm -q mind_api/src/world/tree.py
)
run_case "2-stage-delete-target" 1 "..world.tree"

# ─── Case 3: Stage delete of tree.py AND stage edit removing the import ────
init_repo
(
    cd "$SANDBOX"
    git rm -q mind_api/src/world/tree.py
    # Rewrite __init__.py without the world.tree import — stage it.
    cat > mind_api/src/endpoints/__init__.py <<'PYEOF'
"""Endpoint registry — baseline test fixture."""
from __future__ import annotations

def load_all():
    from . import health as _health
    from ..world import pipeline as _pipeline
    return [_health, _pipeline]
PYEOF
    git add mind_api/src/endpoints/__init__.py
)
run_case "3-stage-delete-and-import-removed" 0 ""

# ─── Case 5: Register new endpoint in __init__.py but forget to stage file ─
# Edit __init__.py to add `from . import newep as _newep`, stage the
# __init__.py edit, but do NOT create the newep.py file.
init_repo
(
    cd "$SANDBOX"
    cat > mind_api/src/endpoints/__init__.py <<'PYEOF'
"""Endpoint registry — baseline test fixture."""
from __future__ import annotations

def load_all():
    from . import health as _health
    from . import newep as _newep
    from ..world import tree as _tree
    from ..world import pipeline as _pipeline
    return [_health, _newep, _tree, _pipeline]
PYEOF
    git add mind_api/src/endpoints/__init__.py
)
run_case "5-add-import-without-staging-file" 1 ".newep"

# ─── Case 6: Run outside a git tree → exit 0 (fail-open) ───────────────────
# Skip git init for this case. The gate's _repo_root() returns None and the
# gate exits 0. Note we cannot use SANDBOX (which has .git from prior case);
# create a fresh dir without git.
NO_GIT_DIR=$(mktemp -d -t no-git-XXXXXX)
trap 'rm -rf "$SANDBOX" "$NO_GIT_DIR"' EXIT
stderr_file6=$(mktemp -t registry-case6-XXXXXX)
rc6=0
(
    cd "$NO_GIT_DIR"
    $PY "$REAL_GATE"
) 2> "$stderr_file6" || rc6=$?
rm -f "$stderr_file6"
if [ "$rc6" = "0" ]; then
    echo "PASS 6-no-git-tree-fail-open (exit=0)"
    PASS_COUNT=$((PASS_COUNT + 1))
else
    echo "FAIL 6-no-git-tree-fail-open: expected exit=0 got=$rc6"
    FAIL_COUNT=$((FAIL_COUNT + 1))
fi

# ─── Case 8: Introduce syntax error in __init__.py and stage ───────────────
init_repo
(
    cd "$SANDBOX"
    cat > mind_api/src/endpoints/__init__.py <<'PYEOF'
"""Endpoint registry — baseline test fixture."""
from __future__ import annotations

def load_all(:
    from . import health as _health
PYEOF
    git add mind_api/src/endpoints/__init__.py
)
run_case "8-syntax-error" 1 "syntax error"

echo ""
echo "──────────────────────────────────────────"
echo "Total: $((PASS_COUNT + FAIL_COUNT))  Pass: $PASS_COUNT  Fail: $FAIL_COUNT"

if [ "$FAIL_COUNT" -gt 0 ]; then
    echo "TEST FAIL"
    exit 1
fi
echo "TEST PASS"
exit 0
