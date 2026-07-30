#!/usr/bin/env bash
# run-full-suite — thin wrapper over run-full-suite.py.
#
# The ONE safe way to run the framework suite on this box. Pins
# STORAGE_BACKEND=local (guard-955), excludes daemon_integration
# (Live-Daemon Exception), and chunks into fresh processes (guard-1448).
#
# Exit: 0 clean | 1 genuine failures | 2 INVALID/contended (re-measure) | 3 setup
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

#  cygpath conversion. Under Git Bash on Windows, $(cd ... && pwd)
# returns POSIX /c/... which Windows python3 reads as drive C: plus a literal
# c/ subdir, yielding FileNotFoundError on C:\c\...\run-full-suite.py. Convert
# to Windows-native form before exec. Linux/macOS lack cygpath and fall
# through with SCRIPT_DIR unchanged (POSIX paths work natively).
if command -v cygpath >/dev/null 2>&1; then
    SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR")"
else
    SCRIPT_DIR_NATIVE="$SCRIPT_DIR"
fi

python3 "$SCRIPT_DIR_NATIVE/run-full-suite.py" "$@"
FRAMEWORK_RC=$?

# Pytest-invisible suites (). run-full-suite.py drives pytest, which
# by construction collects NEITHER main()-style .py files (no `def test_`) NOR
# any .sh file — so before this call site the framework half of a "full suite"
# run silently excluded 90 test files.
#
# This is the same defect the domain-hook comment below describes, and it was
# sitting in core: run-invisible-suites.sh had ZERO automated callers. Its only
# reference was an honor-system row in
# .claude/rules/run-full-suite-after-deep-code.md telling the agent to remember
# it by name. A runner with no call site is indistinguishable from one that
# always returns clean, and the cost was measured, not theoretical —
# test_iteration_commit.sh sat red for 77 days, and two of its cases passed
# vacuously against a self-disabled production filter.
#
# Existence-guarded for the same reason as the domain hook (a tree without the
# runner is a supported configuration, not a breakage). "$@" is deliberately
# NOT forwarded: those flags (--chunks etc.) belong to run-full-suite.py.
INVISIBLE_RC=0
if [ -f "$SCRIPT_DIR/tests/run-invisible-suites.sh" ]; then
    echo "=== pytest-invisible suites: core/scripts/tests/run-invisible-suites.sh ==="
    bash "$SCRIPT_DIR/tests/run-invisible-suites.sh"
    INVISIBLE_RC=$?
fi

# Domain-test hook slot (). Pattern B per
# core/config/conventions/domain-hooks.md: core NAMES the slot, the world
# PROVIDES the script. Before this, the domain test dir had no runner and no
# caller — its files executed only when a human or agent remembered them by
# name, which is the invisible-suite class  already paid for in core.
#
# The existence guard is NOT the guard-139 anti-pattern. guard-139 forbids
# fallbacks that MASK A BROKEN CONTRACT; a world with no domain tests is a
# supported configuration, not a breakage. Nothing here silences the runner:
# its stdout/stderr pass through untouched, and a present-but-FAILING runner
# changes this script's exit code.
DOMAIN_RC=0
if [ -f "$SCRIPT_DIR/_paths.sh" ]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/_paths.sh"
    if [ -n "${WORLD_PATH:-}" ] && [ -f "$WORLD_PATH/scripts/run-domain-tests.sh" ]; then
        echo "=== domain test suite: $WORLD_PATH/scripts/run-domain-tests.sh ==="
        bash "$WORLD_PATH/scripts/run-domain-tests.sh"
        DOMAIN_RC=$?
    fi
fi

# Exit contract preserved exactly (0 clean | 1 genuine failures | 2
# INVALID/contended | 3 setup). The framework rc WINS when non-zero, so a
# contended run (2) is never downgraded to a plain failure by a domain red.
# An invisible-suite or domain red surfaces as 1 only when the framework half
# was clean — neither may mask a contended (2) verdict, since 2 means "this
# number means NOTHING, re-measure" and must not read as a plain failure.
if [ "$FRAMEWORK_RC" -ne 0 ]; then exit "$FRAMEWORK_RC"; fi
if [ "$INVISIBLE_RC" -ne 0 ]; then exit 1; fi
if [ "$DOMAIN_RC" -ne 0 ]; then exit 1; fi
exit 0
