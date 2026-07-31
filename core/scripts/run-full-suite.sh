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

#  python3 resolution. _paths.sh is what puts core/scripts/.python-shim
# on PATH, and on Windows that shim IS `python3` (it is synthesized there from
# `py`/`python` when `python3 -c pass` fails — _paths.sh lines 54-68). Before this
# source existed here, the call below ran with whatever PATH the caller had: under
# any invocation the PreToolUse bash-agent-inject hook does not reach (a nohup'd or
# backgrounded run), `python3` was unresolvable and this line died rc=127 — skipping
# the ENTIRE framework half while the two halves below still ran and printed green.
#
# PLACEMENT: keep this BELOW the cygpath block above and ABOVE the call below.
# Sourcing earlier prepends .python-shim to PATH before `command -v cygpath` runs,
# which can change that probe's ANSWER — the shim dir is gitignored and per-box, so
# its contents cannot be assumed. This ordering is CONSERVATIVE, not load-bearing on
# any box measured to date, and the first revision of this comment overstated it:
# measured on cc-02 (2026-07-30) the dir holds exactly ONE file, a `cygpath` identity
# passthrough that echoes its last argument, so hoisting above the probe there would
# flip it to found with NIL effect (the passthrough returns SCRIPT_DIR unchanged —
# the same value the not-found branch assigns). On Windows that file is absent by
# construction (its own header scopes it to Linux boxes lacking real cygpath, and
# .python-shim is gitignored so it cannot travel). Keep the ordering because the
# shim dir's contents are unknowable, NOT because a live shadowing defect is known.
#
# Sourcing here is free: the domain block below already sourced this file
# unconditionally on every run, so the MIND_AGENT-unset WARN it can emit (only on
# boxes with >1 agent conf) was already being paid — this moves when it appears, it
# does not add it. The WARN is a symptom, not noise: an unset MIND_AGENT also used
# to fail 6 invisible-half files env-shaped. That half now self-resolves the bound
# agent (or SKIPs loudly) — see run-invisible-suites.sh bound-agent resolution,
# . _paths.sh sets no shell options, defines SCRIPT_DIR from its own
# BASH_SOURCE (same core/scripts dir), and is idempotent when sourced twice.
if [ -f "$SCRIPT_DIR/_paths.sh" ]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/_paths.sh"
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
# WORLD_PATH comes from the _paths.sh source hoisted above the framework call
# (); this block no longer re-sources it. When _paths.sh is absent
# WORLD_PATH stays unset and the guard below skips the hook, exactly as before.
DOMAIN_RC=0
if [ -n "${WORLD_PATH:-}" ] && [ -f "$WORLD_PATH/scripts/run-domain-tests.sh" ]; then
    echo "=== domain test suite: $WORLD_PATH/scripts/run-domain-tests.sh ==="
    bash "$WORLD_PATH/scripts/run-domain-tests.sh"
    DOMAIN_RC=$?
fi

# Exit contract preserved exactly (0 clean | 1 genuine failures | 2
# INVALID/contended | 3 setup). The framework rc WINS when non-zero, so a
# contended run (2) is never downgraded to a plain failure by a domain red.
# An invisible-suite or domain red surfaces as 1 only when the framework half
# was clean — neither may mask a contended (2) verdict, since 2 means "this
# number means NOTHING, re-measure" and must not read as a plain failure.
# Tail banner (). The exit code above is honest, but the LOG is what
# callers actually read — and when the framework half dies at its FIRST line, that
# error is line 1 of ~15 while the invisible-suite and domain halves still run below
# it and print green. A reader who reads the tail (or pipes through `tail -40`) sees
# a passing run in which ZERO framework tests executed: the same
# looks-like-coverage-delivers-none shape as rb-5650, inside the tool
# run-full-suite-after-deep-code.md calls "The ONE safe way to run the framework
# suite on this box". Restating the failure LAST puts it where the eye lands.
#
# rc=127 is called out separately because it is the one code meaning the suite did
# not RUN AT ALL (interpreter unresolvable) rather than ran-and-failed — that
# distinction decides whether a red is a regression to triage or a setup problem to
# fix, and collapsing them sends the reader hunting a bug that never executed.
if [ "$FRAMEWORK_RC" -ne 0 ]; then
    echo ""
    if [ "$FRAMEWORK_RC" -eq 127 ]; then
        echo "=== !!! FRAMEWORK HALF DID NOT RUN (rc=127, interpreter not found) !!! ==="
        echo "ZERO framework tests executed. Any green above covers the invisible-suite"
        echo "and domain halves ONLY -- it is NOT evidence about the framework suite."
        echo "This is a setup failure, not a test regression: do not triage it as a red."
    else
        echo "=== !!! FRAMEWORK HALF DID NOT PASS (rc=$FRAMEWORK_RC) !!! ==="
        echo "Any green printed above covers the invisible-suite and domain halves ONLY."
        echo "rc=1 genuine failures | rc=2 INVALID/contended (re-measure) | rc=3 setup."
    fi
fi

if [ "$FRAMEWORK_RC" -ne 0 ]; then exit "$FRAMEWORK_RC"; fi
if [ "$INVISIBLE_RC" -ne 0 ]; then exit 1; fi
if [ "$DOMAIN_RC" -ne 0 ]; then exit 1; fi
exit 0
