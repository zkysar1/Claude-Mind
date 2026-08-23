#!/usr/bin/env bash
# runner-identity-check.sh — "Am I THE runner for this agent?" gate.
#
# Purpose: a session about to (re-)enter the aspirations loop body asks
# whether THIS session is the runner by comparing its own SID ($MIND_SID,
# injected into every Bash call by bash-agent-inject.py) against the agent's
# running-session-id. This is the per-SESSION identity gate the loop's
# Phase -1.5 was missing.
#
# WHY THIS EXISTS (the multi-runner gap, 2026-05-23)
# ---------------
# session-state-get.sh reads the SHARED agent-level agent-state file. EVERY
# session of an agent reads the same "RUNNING" the real runner wrote, so the
# loop's Phase -1.5 state check answers "is this AGENT running?" — NOT "am I
# THE runner?". The loop self-re-enters every iteration via Skill(aspirations).
# When two+ terminals run the same agent (e.g. a second /start auto-recovers
# during a stale-heartbeat window and takes over running-session-id while the
# original session keeps looping), the non-runner terminals pass the state
# check forever and iterate indefinitely, confusing the real runner with
# concurrent writes to shared world/agent state. stop-hook.sh Gate 0 ALLOWS a
# non-runner to stop, but nothing ever TELLS it to. This gate is the active
# eject point.
#
# SELF-HEALING: running-session-id holds exactly one SID (session-save-id.sh
# enforces the "/start is sole first-writer" invariant). So at most one session
# matches. When a new session claims runner, the old runner fails this check on
# its very NEXT iteration and ejects itself — convergence to exactly one runner
# with no manual intervention.
#
# Inputs (env, injected by bash-agent-inject.py on every Bash call):
#   MIND_SID   — this session's SID (the authoritative current SID)
#   MIND_AGENT — the bound agent
#
# Exit codes:
#   0 — I AM the runner, OR ambiguous (FAIL-OPEN), OR the running-session-id
#       pointer is STALE but fresh write-attribution proves this session is the
#       de-facto runner () → caller CONTINUES the loop
#   1 — I am NOT the runner → caller EJECTS (no re-entry). TWO causes:
#       (a) definite SID mismatch, no fresh self write-attribution;
#       (b) SID MATCHES but a different LIVE process already holds the runner
#           role for this agent ( same-SID duplicate instance).
#
# FAIL-OPEN is deliberate and load-bearing. Exit 1 fires ONLY on a definite
# mismatch: MIND_SID non-empty AND running-session-id non-empty AND they
# differ. Every ambiguity (either SID empty/unreadable, agent unset, paths
# missing) returns 0. Rationale: running-session-id holds exactly one SID, so
# if it is ever transiently empty (a brief /start race, fresh install, or a
# crashed runner before recovery-gate repairs it), failing CLOSED would make
# EVERY session mismatch and kill the legitimate runner too. The Phase -1.5
# state!=RUNNING check plus recovery-gate are the safety net for the empty-
# runner-sid window; this gate's ONLY job is to eject confirmed non-runners.
#
# Write-attribution override (, US-09): there is ONE legitimate scenario
# where MY_SID != running-session-id and the session SHOULD continue -- the
# pointer is transiently STALE (e.g. a stop arriving mid-iteration desyncs it)
# while THIS session is the real runner. running-session-id is written only by
# /start, so a mid-loop desync has no writer to repair it until recovery. The
# override below trusts a STRONGER signal: if this session stamped itself the
# confirmed runner within WINDOW_SEC, the pointer is stale, not a takeover, and
# the session stays. It is NOT a user force-flag (still none -- no env or CLI
# escape hatch a confused observer could abuse); it is an automatic fallback
# gated on this session's OWN recent confirmed-runner evidence, which a genuine
# non-runner observer never has. A LIVE different runner overwrites the shared
# stamp with its own SID, so the override self-expires the instant a real
# takeover runs one iteration. A user who wants this terminal to be the runner
# still runs /stop on the other terminal, or /start --recover here.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_paths.sh
source "$SCRIPT_DIR/_paths.sh"

MY_SID="${MIND_SID:-}"
AGENT="${MIND_AGENT:-}"

# Fail-open on any missing input — cannot make a definite determination.
[ -n "$MY_SID" ] || exit 0
[ -n "$AGENT" ] || exit 0

# tr -d '\r\n' — strip Windows CRLF so the comparison is byte-exact (same
# idiom as sid-collision-check.sh / session-save-id.sh). cat failure (missing
# file) is suppressed; RUNNER_SID becomes empty and the next check fail-opens.
RUNNER_SID=$(cat "$(agent_dir "$AGENT")/session/running-session-id" 2>/dev/null | tr -d '\r\n')

# Fail-open when running-session-id is empty/unreadable. See header rationale.
[ -n "$RUNNER_SID" ] || exit 0

# -- Owning-process identity helpers () ------------------------------
# WHY A PROCESS IDENTITY AND NOT runner-token: runner-token is a FILE in the
# agent's session dir. Two processes both read it and both get the same value,
# so it cannot distinguish them -- CLAUDE.md's signal table describes it as the
# SID-reuse detector, but as a shared file it can only detect reuse ACROSS
# /start runs, never two live readers of one token. The only identity that
# differs between two concurrent sessions is the OWNING PROCESS.
#
# Identity = "<pid>:<starttime>" of the nearest ancestor whose comm is `claude`.
# starttime (/proc/<pid>/stat field 22, boot-relative jiffies) is immutable for
# the life of the process, so the pair survives PID reuse: a recycled pid
# carries a different starttime and reads as DEAD, which is what lets a crashed
# runner be taken over instead of wedging the agent forever.
#
# Deliberately NOT /proc/<pid>/environ (guard-1582, guard-1976): that is frozen
# at exec and does not reflect later state, so an env-derived identity would go
# stale exactly when it matters.
_proc_stat_field() {  # <pid> <index into post-comm fields>: state=1 ppid=2 starttime=20
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
    # `${a%%:*}` is evaluated -- under this script's `set -u` that aborts the
    # whole gate with exit 1, i.e. EVERY runner ejects. Caught by case 17's
    # stderr assertion (the exit code alone read as a correct eject).
    local id="$1"
    local pid="${id%%:*}"
    local st="${id##*:}"
    local cur
    [ -n "$pid" ] && [ -n "$st" ] && [ "$pid" != "$st" ] || return 1
    printf '%s' "$pid" | grep -Eq '^[0-9]+$' || return 1
    cur=$(_proc_stat_field "$pid" 20) || return 1
    [ -n "$cur" ] && [ "$cur" = "$st" ]
}

# -- Write-attribution runner override (, US-09) ----------------------
# running-session-id is the file we already trust, but it is written ONLY by
# /start (guard-340 sole-first-writer). A transient desync -- e.g. zeta's "stop
# arrived mid-iteration, identity broken" near-miss (2026-05-13) -- can leave it
# pointing at a STALE non-empty SID for a window, during which the LEGITIMATE
# runner's MY_SID != RUNNER_SID and a bare compare would WRONGLY eject the real
# runner. Write-attribution is a stronger signal: the session that has been
# actively driving the loop (and thus writing <agent>/aspirations.jsonl -- claims,
# status flips, recurring closes -- every iteration) is the de-facto runner
# regardless of a momentarily-stale pointer file.
#
# Capture is at THIS gate, not at the aspirations.jsonl write path: the
# runner-identity gate is the single per-iteration chokepoint that (a) runs once
# per loop iteration (Phase -1.45), (b) always has $MIND_SID, and (c) already
# KNOWS, on a clean pass, that this SID is the confirmed loop runner (hence the
# session writing aspirations.jsonl). Instrumenting every daemon write path would
# carry far more blast radius for the same signal. This deliberately does NOT
# touch heartbeat-tick.sh (pure-mtime by design) or refresh running-session-id
# (must stay the atomic /start-only triple-write -- guard-340), so the SSOT
# contract is preserved; this is a SEPARATE, lower-priority fallback signal.
ATTRIB_FILE="$(agent_dir "$AGENT")/session/runner-write-attribution"
# Override window: a stale pointer is tolerated only while recent write evidence
# is FRESH. Past the window the desync is treated as a real takeover and the
# session ejects. Overridable for tests; defaults to 300s (the US-09 "5 min").
WINDOW_SEC="${RUNNER_WRITE_ATTRIB_WINDOW_SEC:-300}"

if [ "$MY_SID" = "$RUNNER_SID" ]; then
    # -- Same-SID duplicate-instance eject () -----------------------
    # THE GAP THIS CLOSES: everything above ejects on a definite SID MISMATCH,
    # so two processes carrying the SAME $MIND_SID both reach here and both
    # continue, every iteration, forever. Measured on cc-02 2026-08-22: two live
    # `claude` processes under one SID duplicated a full /replay pass and, later
    # that day, one OVERWROTE the other's 6,930-char outcome_note on a
    # product-lane goal with a note belonging to a different goal -- silent data
    # loss on the durable evidence trail, which the close path's never-clobber
    # rule then PROTECTED. Every ownership predicate in the framework reads the
    # (claimed_by, claimed_by_sid) pair, so none of them can see this case
    # (guard-4806, guard-1460).
    #
    # BLAST RADIUS IS BOUNDED BY PLACEMENT: this runs only inside the SID-MATCH
    # branch -- i.e. only among sessions that ALREADY pass -- so it can add
    # ejections only where two sessions both claim to be THE one runner. A
    # session with a different SID already ejected below; a worker Body never
    # reaches here.
    #
    # FIRST STAMPER WINS, and a DEAD owner is taken over. Which of two live
    # instances survives is arbitrary but deterministic and self-healing; the
    # ejected one prints how to reclaim. Fail-open is preserved at every
    # unknown: unresolvable identity skips the check entirely, so non-Linux
    # boxes and any sandbox without a `claude` ancestor behave exactly as before.
    MY_PROC=$(_resolve_owner_proc 2>/dev/null || true)
    if [ -n "$MY_PROC" ]; then
        PROC_FILE="$(agent_dir "$AGENT")/session/runner-proc"
        STAMPED_PROC=$(cat "$PROC_FILE" 2>/dev/null | tr -d '\r\n' | head -n1)
        if [ -n "$STAMPED_PROC" ] && [ "$STAMPED_PROC" != "$MY_PROC" ]; then
            if _owner_alive "$STAMPED_PROC"; then
                echo "[runner-identity-check] SAME-SID DUPLICATE INSTANCE for agent '$AGENT': running-session-id ($RUNNER_SID) matches this session, but the runner role is held by a DIFFERENT LIVE PROCESS ($STAMPED_PROC; this one is $MY_PROC). Two processes sharing one SID are invisible to every SID-based ownership check (g-115-7195), and concurrent execution has caused silent outcome_note loss. This session will exit the loop. To make THIS terminal the runner: /stop $AGENT in the other terminal, or /start $AGENT --recover here." >&2
                exit 1
            fi
            # Stamped owner is gone (exited, or its pid was recycled and now
            # carries a different starttime). Take over rather than wedge.
        fi
        if [ "$STAMPED_PROC" != "$MY_PROC" ]; then
            TMP_PROC="$PROC_FILE.tmp.$$"
            { printf '%s\n' "$MY_PROC" > "$TMP_PROC" && mv -f "$TMP_PROC" "$PROC_FILE"; } || true
        fi
    fi

    # Confirmed runner via the trusted pointer. Stamp SID + epoch as the
    # write-attribution evidence for any later stale-pointer iteration. Stamping
    # ONLY on a confirmed match (never on an override) is load-bearing: it lets a
    # frozen timestamp age out so a genuine takeover converges to one runner.
    # Best-effort (|| true) -- a stamp failure must never block a confirmed runner.
    NOW_EPOCH=$(date +%s)
    TMP="$ATTRIB_FILE.tmp.$$"
    { printf '%s %s\n' "$MY_SID" "$NOW_EPOCH" > "$TMP" && mv -f "$TMP" "$ATTRIB_FILE"; } || true
    exit 0
fi

# Mismatch: MY_SID != a non-empty RUNNER_SID. Before ejecting, consult
# write-attribution. If THIS session stamped itself the confirmed runner within
# the window, the pointer is transiently stale -- trust the stronger signal and
# stay. The shared single attribution file converges takeovers fast: once a NEW
# runner passes its own clean check it OVERWRITES this stamp with its SID, so the
# old runner's next read finds a non-matching SID and ejects immediately.
STAMP=$(cat "$ATTRIB_FILE" 2>/dev/null | tr -d '\r' | head -n1)
STAMP_SID="${STAMP%% *}"
STAMP_EPOCH="${STAMP##* }"
if [ -n "$STAMP_SID" ] && [ "$STAMP_SID" = "$MY_SID" ] \
   && printf '%s' "$STAMP_EPOCH" | grep -Eq '^[0-9]+$'; then
    AGE=$(( $(date +%s) - STAMP_EPOCH ))
    if [ "$AGE" -ge 0 ] && [ "$AGE" -le "$WINDOW_SEC" ]; then
        echo "[runner-identity-check] running-session-id ($RUNNER_SID) != this session ($MY_SID) for agent '$AGENT', BUT write-attribution shows this session was the confirmed runner ${AGE}s ago (<= ${WINDOW_SEC}s). Treating the pointer as transiently stale (g-305-09) -- NOT ejecting." >&2
        exit 0
    fi
fi

echo "[runner-identity-check] This session ($MY_SID) is NOT the runner for agent '$AGENT' (runner is $RUNNER_SID). Another terminal owns the autonomous loop — this session will exit the loop. Use /stop $AGENT or /start $AGENT --mode reader here." >&2
exit 1
