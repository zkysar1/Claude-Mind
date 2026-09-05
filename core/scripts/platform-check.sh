#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- runs once per gated goal per selector pass; must stay a
# single uname with no sourcing. Never add MCP or remote-service indirection.
#
# platform-check.sh --os <windows|linux|macos>
# platform-check.sh --machine <machine-id>
#   exit 0  the running box IS that platform / IS that machine
#   exit 1  it is not
#   exit 2  usage error, or identity could not be determined (NOT a mismatch)
#
# Both flags are independent constraints; pass either, or both (AND).
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
# ── WHY --machine EXISTS, AND WHY IT MATCHES ON *EITHER* SIGNAL () ──
# --os is a COARSE gate: it filters correctly only when the target box is the
# only one of its OS. Measured 2026-09-04 (alpha, cc-07):  must run on
# cc-04, and cc-04 and cc-07 are BOTH Linux — so `--os linux` filters nothing and
# the constraint stayed prose-only, which is invisible to the selector. That goal
# was ranked #1-2 on cc-07 and burned a top-of-queue selection slot on three
# separate Bodies (DESKTOP-O91DLK2 03:5x, cc-07 11:2x, cc-07 18:2x) before this
# flag existed.  hit the same wall from the other side and recorded it
# verbatim: "platform-check.sh supports --os ONLY, no hostname predicate, and
# building one would be speculative." That was correct at n=1. It is n=3 now
# ( +  both want DESKTOP-O91DLK2 and settled for --os
# windows;  cannot be expressed by --os at all).
#
# THE SIGNAL IS `uname -n` OR $MACHINE_ID, AND THE *OR* IS THE DESIGN. Reason 1
# above (fail-closed blast radius) sets the asymmetry: a wrong "not this box"
# HIDES the goal on every box including the only one that can run it, while a
# wrong "yes this box" merely costs one selection slot — exactly the status quo
# this flag improves on. So the two errors are not symmetric and the gate must
# lean toward answering YES.
#   * `uname -n` is a PROCESS FACT, and reason 2 above is why it leads: the
#     caller is a spawn (predicate.py subprocess.run(shell=True), plus a cmd.exe
#     hop on Windows) and an env var is the signal most likely to go absent
#     across one.
#   * $MACHINE_ID is nonetheless authoritative when set, because the fleet SSOT
#     (_session_telemetry._machine_id) prefers it over the nodename — a box whose
#     MACHINE_ID is deliberately not its nodename would be misjudged by `uname -n`
#     alone, and that misjudgement is the goal-hiding direction.
# Matching either therefore minimises the only error that destroys work. Do NOT
# "tighten" this to a single signal: each one alone reintroduces one of the two
# hiding modes above.
#
# Corollary, deliberate: this script sources NOTHING. Sourcing _platform.sh to
# reuse its detection would export MSYS_NO_PATHCONV=1 as a side effect, which is
# the precise ordering hazard documented above -- a detector that breaks its
# callers is worse than a duplicated case statement. The duplication is the
# cheaper of the two costs and is recorded here so it is a choice, not drift.

set -uo pipefail

usage() {
    echo "usage: platform-check.sh [--os <windows|linux|macos>] [--machine <machine-id>]" >&2
}

WANT=""
WANT_MACHINE=""
while [ $# -gt 0 ]; do
    case "$1" in
        # Same guard-1224 shift form as --os below, for the same reason.
        --machine) WANT_MACHINE="${2:-}"; shift $(( $# >= 2 ? 2 : 1 )) ;;
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

if [ -z "$WANT" ] && [ -z "$WANT_MACHINE" ]; then
    echo "platform-check.sh: one of --os or --machine is required" >&2
    usage
    exit 2
fi

# --machine is evaluated FIRST and independently of --os: it needs no uname -s
# classification, and a box that fails the machine test is already the answer.
if [ -n "$WANT_MACHINE" ]; then
    # Hostnames are conventionally case-insensitive, and the live fleet mixes
    # cases (cc-04 vs DESKTOP-O91DLK2), so fold both sides.
    WANT_MACHINE="$(printf '%s' "$WANT_MACHINE" | tr '[:upper:]' '[:lower:]')"

    # Same separate-capture discipline as uname -s below: a detection FAILURE
    # must stay distinguishable from a machine MISMATCH.
    NODE_OUT="$(uname -n 2>/dev/null)"
    NODE_RC=$?
    if [ "$NODE_RC" -ne 0 ]; then
        NODE_OUT=""
    fi
    NODE_LC="$(printf '%s' "$NODE_OUT" | tr '[:upper:]' '[:lower:]')"
    ENV_LC="$(printf '%s' "${MACHINE_ID:-}" | tr '[:upper:]' '[:lower:]')"

    if [ -z "$NODE_LC" ] && [ -z "$ENV_LC" ]; then
        echo "platform-check.sh: cannot determine machine identity (uname -n rc=$NODE_RC, output empty; MACHINE_ID unset)" >&2
        echo "platform-check.sh: refusing to answer — this is NOT a machine mismatch" >&2
        exit 2
    fi

    # OR, deliberately — see "WHY --machine EXISTS" in the header. An unset
    # MACHINE_ID yields an empty ENV_LC, which cannot match a non-empty target,
    # so the absent signal never produces a false YES.
    if [ "$NODE_LC" != "$WANT_MACHINE" ] && [ "$ENV_LC" != "$WANT_MACHINE" ]; then
        exit 1
    fi
fi

if [ -z "$WANT" ]; then
    exit 0
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
