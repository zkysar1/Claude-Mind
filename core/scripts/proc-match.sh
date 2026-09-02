#!/usr/bin/env bash
# proc-match.sh — cross-platform "which processes match this command line?"
#
# Prints one line per match, `<pid> <full command line>`, i.e. the same shape as
# Linux `pgrep -af <pattern>`. Exit 0 when at least one process matched, 1 when
# none, 2 on a usage or plumbing error.
#
# WHY THIS EXISTS
#
# `pgrep -af "[r]un-full-suite"` was prescribed by
# .claude/rules/run-full-suite-after-deep-code.md as THE concurrency probe, and
# it does not run on two of the fleet's three platforms:
#
#   - Windows (MSYS/Git Bash): there is NO pgrep at all. Measured 2026-08-31 on
#     DESKTOP-O91DLK2 — `command -v pgrep` is empty while `pkill` IS present, so
#     the absence is not even obvious from a glance at the toolchain. The
#     recipe fails with `pgrep: command not found`, which a caller reading only
#     a count reads as "no run is live".
#   - macOS: pgrep exists but `-a` is a procps (Linux) extension that BSD pgrep
#     does not carry. NOT verified on a Mac from this box — stated as the reason
#     the POSIX arm below avoids pgrep entirely rather than as a measured claim.
#
# And the Windows fallbacks each have their own trap:
#   - MSYS `ps -ef` prints only the binary (`/usr/bin/bash`) with NO arguments,
#     so `ps | grep run-full-suite` matches nothing while the run is live —
#     measured, and it is the same fail-open direction guard-3159 warns about.
#   - `tasklist /FO CSV` took 20s+ on a Win10 dev machine (see agent-watchdog.py
#     "Why psutil and not shell tools").
#
# So: PowerShell + Win32_Process on Windows (the idiom daemon-orphan-sweep.sh,
# mind-api-start.sh and _runtime.sh already use), and POSIX `ps -eo pid=,args=`
# on Linux/macOS — which is POSIX-specified, carries full argv on both, and
# sidesteps the pgrep portability question altogether.
#
# WHY A SCRIPT AND NOT AN INLINE ONE-LINER
#
# guard-3159: `-f` matching makes the pattern match COMMAND LINES, which
# includes the caller's OWN enclosing wrapper shell — measured at 4 phantom
# hits vs 2 real processes. The `[r]un-full-suite` bracket idiom CANNOT fix
# that: it only stops the matcher matching its own argv, never a *different*
# enclosing process (guard-1238, guard-2262). That guardrail's prescription is
# to "run the probe from a FILE or as its OWN command".
#
# A file alone is still not enough, because `bash proc-match.sh run-full-suite`
# puts the pattern in the invoking shell's argv too. So this script excludes
# every process whose command line contains its own name. That is the part the
# bracket idiom structurally cannot do, and it is why callers need no bracket:
# pass the plain pattern.
#
# Consequence worth knowing: a genuine target whose command line contains
# "proc-match" is invisible to this probe. Deliberate — self-exclusion must be
# by a token the probe controls, and over-excluding one implausible name is
# strictly safer than the phantom-hit class, which manufactures a false
# "another run is already live" and aborts the launch the check was protecting.
#
# NOT FOR WAITING ON A RUN. To wait for a multi-phase wrapper to finish, watch
# the wrapper PID (`kill -0 $pid`), never a name — guard-2262: a waiter keyed on
# the inner pytest exits while the invisible and domain halves are still
# running, and the chunked half has already printed VERDICT CLEAN, so the
# premature exit looks exactly like a finished green run. On Windows, do NOT
# probe pid liveness from Python with os.kill(pid, 0) — guard-668.
#
# USAGE
#   bash core/scripts/proc-match.sh <pattern>          # pgrep -af shape
#   bash core/scripts/proc-match.sh --count <pattern>  # count only
#
# Run it as its OWN command, not inside a `$(...)` in the same command that
# also names the pattern (guard-3159's "count has no argv to inspect" case).

set -uo pipefail

SELF_TOKEN="proc-match"

usage() {
    echo "usage: proc-match.sh [--count] <pattern>" >&2
    echo "  <pattern> is a regex matched against full command lines." >&2
    exit 2
}

COUNT_ONLY=0
PATTERN=""
while [ $# -gt 0 ]; do
    case "$1" in
        --count) COUNT_ONLY=1; shift ;;
        -h|--help) usage ;;
        -*) echo "proc-match.sh: unknown flag: $1" >&2; usage ;;
        *)
            [ -n "$PATTERN" ] && { echo "proc-match.sh: only one pattern allowed" >&2; usage; }
            PATTERN="$1"; shift ;;
    esac
done
[ -n "$PATTERN" ] || usage

case "$(uname -s 2>/dev/null || echo unknown)" in
    MINGW*|MSYS*|CYGWIN*) PLATFORM="windows" ;;
    *)                    PLATFORM="posix" ;;
esac

raw=""
if [ "$PLATFORM" = "windows" ]; then
    # Get-CimInstance over the whole table: unlike daemon-orphan-sweep.sh we
    # cannot pre-filter on Name, because the caller's target is typically a
    # SCRIPT whose process name is its interpreter (`bash`, `python`) — the
    # exact case guard-3159 is about.
    #
    # The pattern crosses into PowerShell as a single-quoted literal, so a
    # single quote in it would break out; PowerShell escapes those by doubling.
    ps_pattern="${PATTERN//\'/\'\'}"
    # `-ne $PID` is load-bearing, not defensive. PowerShell receives the pattern
    # INSIDE its own -Command text, so its own command line contains the pattern
    # and it matches itself — measured on the first run of this script: a
    # deliberately impossible pattern returned one hit, the powershell.exe
    # invocation itself, and exited 0. That is exactly guard-3159's phantom-hit
    # class (a false "something is already running" that aborts the launch the
    # probe was written to protect), reproduced inside the fix for it. The
    # SELF_TOKEN filter below cannot catch this one: the PowerShell child's
    # command line carries the pattern but not this script's name.
    raw="$(powershell -NoProfile -NonInteractive -Command "
        \$ErrorActionPreference = 'SilentlyContinue'
        Get-CimInstance Win32_Process |
            Where-Object { \$_.ProcessId -ne \$PID -and \$_.CommandLine -and \$_.CommandLine -match '$ps_pattern' } |
            ForEach-Object { '{0} {1}' -f \$_.ProcessId, \$_.CommandLine }
    " 2>/dev/null)" || raw=""
else
    # POSIX (Linux + macOS). `ps -eo pid=,args=` is POSIX-specified and carries
    # the full argv on both; the `=` suffixes suppress headers so no header line
    # can ever be mistaken for a match.
    #
    # SNAPSHOT FIRST, THEN MATCH — do not pipe ps directly into grep. In a
    # pipeline both run concurrently, so ps can capture the grep, whose argv
    # contains the pattern; that is the same self-match the bracket idiom exists
    # to paper over, and it is a RACE, so it appears intermittently. Letting ps
    # exit before grep starts removes the possibility instead of hiding it.
    snapshot="$(ps -eo pid=,args= 2>/dev/null)" || snapshot=""
    raw="$(printf '%s\n' "$snapshot" | grep -E -- "$PATTERN" 2>/dev/null)" || raw=""
fi

# Self-exclusion (see header): drop this script, the shell that invoked it, and
# any command-substitution subshell carrying the pattern in its argv. Also drop
# blank lines so an empty result counts as 0 rather than 1.
matches="$(printf '%s\n' "$raw" | grep -v -- "$SELF_TOKEN" | sed '/^[[:space:]]*$/d')" || matches=""

if [ "$COUNT_ONLY" = "1" ]; then
    printf '%s\n' "$matches" | sed '/^[[:space:]]*$/d' | wc -l | tr -d ' '
else
    [ -n "$matches" ] && printf '%s\n' "$matches"
fi

[ -n "$matches" ] && exit 0 || exit 1
