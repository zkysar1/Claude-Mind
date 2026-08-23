#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- runs once per gated goal per selector pass; must stay a
# single uname with no sourcing. Never add MCP or remote-service indirection.
#
# platform-check.sh --os <windows|linux|macos>
#   exit 0  the running box IS that platform
#   exit 1  it is not
#   exit 2  usage error (unknown/missing --os)
#
# Purpose (): make a platform constraint MACHINE-READABLE so
# goal-selector.py can filter on it. A goal gated to one OS states that in a
# `command_succeeds` precondition invoking this script; predicate.py runs it and
# drops the goal from candidates when it exits non-zero. Before this existed the
# constraint lived only in prose, and prose is invisible to the selector: one
# Windows-only goal was ranked #1 for five separate Linux agents in a row, each
# paying a 14k-char read plus a release.
#
# ── WHICH DETECTION IDIOM, AND WHY (the goal asks this to be recorded) ────────
# The tree already contains TWO Windows-detection spellings and they are NOT
# equivalent:
#
#   (a) _paths.sh:309   case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*)
#   (b) _platform.sh:24 [ "${MSYSTEM:-}" != "" ]
#
# This script uses (a). Three reasons, in order of weight:
#
#  1. FAIL-CLOSED BLAST RADIUS. predicate.py::_eval_command_succeeds returns
#     passed=False on ANY non-zero exit, timeout or OSError. So a detector that
#     wrongly reports "not this platform" does not degrade gracefully -- it
#     hides every gated goal on every box, permanently and silently. That
#     asymmetry says: prefer the signal with the fewest ways to go absent.
#
#  2. MSYSTEM IS AN ENVIRONMENT VARIABLE; `uname -s` IS A PROCESS FACT. An env
#     var can be scrubbed, unset, or simply not inherited across a spawn -- and
#     the caller here IS a spawn (`subprocess.run(..., shell=True)` from
#     predicate.py, which on Windows adds a cmd.exe hop). Betting a fail-closed
#     gate on an inherited env var is the exact class that killed the pre-edit
#     advisory gate twice: once on an unset MIND_AGENT the hook never provides,
#     once on MSYS_NO_PATHCONV being exported by a source line placed too early
#     (see .claude/rules/read-before-edit.md Rule 4). Both hand-tested green,
#     because an interactive shell HAS the variable.
#
#  3. COVERAGE. MSYS2/Git Bash set MSYSTEM; Cygwin does NOT. Idiom (b)
#     therefore reports a Cygwin box as non-Windows, while (a) matches it via
#     CYGWIN*. (a) is a strict superset here.
#
# Corollary, deliberate: this script sources NOTHING. Sourcing _platform.sh to
# reuse its detection would export MSYS_NO_PATHCONV=1 as a side effect, which is
# the precise ordering hazard documented above -- a detector that breaks its
# callers is worse than a duplicated case statement. The duplication is the
# cheaper of the two costs and is recorded here so it is a choice, not drift.

set -uo pipefail

usage() {
    echo "usage: platform-check.sh --os <windows|linux|macos>" >&2
}

WANT=""
while [ $# -gt 0 ]; do
    case "$1" in
        # shift $(( $# >= 2 ? 2 : 1 )), NOT bare `shift 2` (guard-1224). With a
        # trailing valueless `--os`, $#==1 and bare `shift 2` is out-of-range:
        # bash does NOT shift, so this loop re-processes the same $1 forever --
        # an infinite loop on malformed input, BEFORE any validation runs. That
        # is far worse here than a bad exit code, because the caller is
        # predicate.py, which would burn its full 30s timeout on every gated
        # goal on every selector pass and then fail closed silently.
        --os) WANT="${2:-}"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        -h|--help) usage; exit 2 ;;
        *) echo "platform-check.sh: unknown argument '$1'" >&2; usage; exit 2 ;;
    esac
done

if [ -z "$WANT" ]; then
    echo "platform-check.sh: --os is required" >&2
    usage
    exit 2
fi

# Normalize to lowercase so --os Windows and --os windows agree.
WANT="$(printf '%s' "$WANT" | tr '[:upper:]' '[:lower:]')"

# Capture uname SEPARATELY from the classification so a detection FAILURE is
# distinguishable from a platform MISMATCH. Inlining `$(uname -s 2>/dev/null)`
# into the case collapses both into HAVE=unknown -> exit 1, which reads as a
# perfectly ordinary "wrong platform" while actually meaning "I could not tell."
# On a box where uname is missing or broken that hides EVERY gated goal, on
# every agent, with the diagnostic swallowed by the 2>/dev/null and nothing
# anywhere reporting a problem. Measured on this file before the fix: with a
# failing uname, `--os linux` on a real Linux box returned 1.
UNAME_OUT="$(uname -s 2>/dev/null)"
UNAME_RC=$?
if [ "$UNAME_RC" -ne 0 ] || [ -z "$UNAME_OUT" ]; then
    echo "platform-check.sh: cannot determine platform (uname -s rc=$UNAME_RC, output empty)" >&2
    echo "platform-check.sh: refusing to answer — this is NOT a platform mismatch" >&2
    exit 2
fi

case "$UNAME_OUT" in
    MINGW*|MSYS*|CYGWIN*) HAVE="windows" ;;
    Linux*)               HAVE="linux"   ;;
    Darwin*)              HAVE="macos"   ;;
    # A REAL but unrecognized kernel (BSD, SunOS, ...). Distinct from the
    # detection failure above: uname worked, we simply do not classify it.
    # Exit 1 is right here — it genuinely is not one of the three platforms.
    *)                    HAVE="unknown" ;;
esac

case "$WANT" in
    windows|linux|macos) ;;
    *)
        echo "platform-check.sh: unknown platform '$WANT' (expected windows|linux|macos)" >&2
        exit 2
        ;;
esac

if [ "$HAVE" = "$WANT" ]; then
    exit 0
fi
exit 1
