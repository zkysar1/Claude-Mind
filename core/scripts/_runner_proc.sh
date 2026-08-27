#!/usr/bin/env bash
# _runner_proc.sh — shared owning-PROCESS identity predicate for the runner role.
#
# WHY THIS EXISTS ()
#  taught the framework to tell apart two processes that share one SID,
# via the identity "<pid>:<starttime>" of the nearest `claude` ancestor, and wired
# it into runner-identity-check.sh — which EJECTS the non-owner from the loop. It
# was NOT wired into stop-hook.sh Gate 0, which ALLOWs a turn-end only on a SID
# MISMATCH. When two processes share one SID, HOOK_SID == RUNNER_SID for BOTH, so
# Gate 0 never fires for the non-owner: the pid-based gate ejects it on every
# re-entry while the SID-based hook BLOCKs its every turn-end. The process can
# neither iterate nor stop. Measured on zeta / cc-02 2026-08-22 over 3 consecutive
# turns; it does not self-resolve.
#
# WHY SHARED RATHER THAN COPIED
# The wedge IS a disagreement between a pid-based predicate and a SID-based one.
# Closing it with a second copy of the pid predicate would leave the two consumers
# free to drift apart again — the same defect one level down, and the goal that
# filed this named it as the care point. So the predicate lives here once and both
# callers source it.
#
# ORDERING (guard-1885): source this AFTER _paths.sh — runner_proc_foreign_live
# calls agent_dir(), which _paths.sh defines. It is otherwise side-effect free: it
# sets no shell options, exports nothing, mutates no paths, and calls no python, so
# it is safe to source from a latency-critical hook.

# <pid> <index into post-comm fields>: state=1 ppid=2 starttime=20
_proc_stat_field() {
    local pid="$1" idx="$2" line rest
    line=$(cat "/proc/$pid/stat" 2>/dev/null) || return 1
    [ -n "$line" ] || return 1
    # Strip through the LAST ')' -- comm is parenthesized and may contain both
    # spaces and parens ("tmux: server"), so positional parsing of the raw line
    # is wrong. Longest-match ## is what makes this comm-safe.
    rest="${line##*)}"
    printf '%s' "$rest" | awk -v i="$idx" '{print $i}'
}

# Emits "<pid>:<starttime>", or returns 1 when it cannot be determined.
#
# starttime (/proc/<pid>/stat field 22, boot-relative jiffies) is immutable for
# the life of the process, so the pair survives PID reuse: a recycled pid carries
# a different starttime and reads as DEAD, which is what lets a crashed runner be
# taken over instead of wedging the agent forever.
#
# Deliberately NOT /proc/<pid>/environ (guard-1582, guard-1976): that is frozen at
# exec and does not reflect later state, so an env-derived identity would go stale
# exactly when it matters.
#
# RUNNER_PROC_ID overrides for tests (no claude ancestor exists in a sandbox).
_resolve_owner_proc() {
    if [ -n "${RUNNER_PROC_ID:-}" ]; then printf '%s' "$RUNNER_PROC_ID"; return 0; fi
    [ -d /proc ] || return 1
    local pid=$$ depth=0 comm st ppid
    while [ "$depth" -lt 12 ] && [ -n "$pid" ] && [ "$pid" != "0" ] && [ "$pid" != "1" ]; do
        comm=$(cat "/proc/$pid/comm" 2>/dev/null) || return 1
        if [ "$comm" = "claude" ]; then
            st=$(_proc_stat_field "$pid" 20) || return 1
            [ -n "$st" ] || return 1
            printf '%s:%s' "$pid" "$st"
            return 0
        fi
        ppid=$(_proc_stat_field "$pid" 2) || return 1
        pid="$ppid"
        depth=$((depth + 1))
    done
    return 1
}

# True only when <pid>:<starttime> names a process that is STILL the same one.
_owner_alive() {
    # SPLIT DECLARATIONS ARE LOAD-BEARING, NOT STYLE. `local a="$1" b="${a%%:*}"`
    # expands ALL arguments before `local` runs, so `a` is still unset when
    # `${a%%:*}` is evaluated -- under a `set -u` caller that aborts the whole
    # gate with exit 1, i.e. EVERY runner ejects. Caught by case 17's stderr
    # assertion (the exit code alone read as a correct eject).
    local id="$1"
    local pid="${id%%:*}"
    local st="${id##*:}"
    local cur
    [ -n "$pid" ] && [ -n "$st" ] && [ "$pid" != "$st" ] || return 1
    printf '%s' "$pid" | grep -Eq '^[0-9]+$' || return 1
    cur=$(_proc_stat_field "$pid" 20) || return 1
    [ -n "$cur" ] && [ "$cur" = "$st" ]
}

# runner_proc_foreign_live <agent> — exit 0 ONLY when the runner-proc stamp for
# <agent> names a DIFFERENT process that is STILL LIVE. Every other outcome is 1.
#
# <agent> IS A REQUIRED POSITIONAL AND IS NEVER DEFAULTED (guard-2601). This
# predicate is consumed from two callers with DIFFERENT scopes — an in-loop gate
# holding $MIND_AGENT and a hook holding $HOOK_AGENT — and the stamp it reads is
# per-agent, resolved through agent_dir(). An optional-with-default agent would
# let a future caller silently probe the wrong agent's session dir and get an
# always-false answer indistinguishable from a legitimate negative.
#
# FAIL-CLOSED TOWARD THE STATUS QUO, which is the whole safety argument for
# consuming it in a hook: an unresolvable identity, an absent stamp, a dead
# stamped owner, or a stamp naming THIS process all return 1. A caller that uses
# this to ADD an allowance therefore keeps its pre-existing behaviour on every
# input it cannot answer, and only ever adds the allowance on positive evidence
# that some OTHER live process holds the role.
#
# CHEAPEST TEST FIRST, and it is ordered that way deliberately: the stamp read is
# one open(); the /proc ancestor walk is up to 12. Gate 0 runs on EVERY turn-end,
# and on a box that has never stamped (every worker box, and any agent that has
# not hit the same-SID branch) this returns after the single read.
runner_proc_foreign_live() {
    local agent="${1:-}"
    [ -n "$agent" ] || return 1
    local stamped
    stamped=$(cat "$(agent_dir "$agent")/session/runner-proc" 2>/dev/null | tr -d '\r\n' | head -n1)
    [ -n "$stamped" ] || return 1
    local mine
    mine=$(_resolve_owner_proc 2>/dev/null) || return 1
    [ -n "$mine" ] || return 1
    [ "$stamped" != "$mine" ] || return 1
    _owner_alive "$stamped"
}
