#!/usr/bin/env bash
# state-mismatch-landing.sh — graceful landing when agent-state is no longer
# RUNNING under a loop that is still executing ().
#
# WHO CALLS IT: iteration-close.sh (productivity-check) and recurring-close.sh,
# immediately BEFORE they print the `═══ ITERATION COMPLETE ═══` imperative.
#
# WHY: the 2026-09-01 fleet incident demoted LIVE reducers RUNNING->IDLE while
# their loops were mid-iteration. The loop then reached its close, printed
# ITERATION COMPLETE, re-entered Skill(aspirations), and the entry gate refused
# at IDLE — the turn ended on refusal text with NOTHING consolidated, and the
# stop hook (correctly) allowed the turn-end. Two things are wrong with that:
# a FALSE demotion of this very session is reversible (recovery-yank-reverse.sh),
# and when it is not, the iteration's learning deserves a consolidation pass
# before the loop lands.
#
# rc 0 = LANDING: the directive below was printed; the caller MUST NOT print
#        ITERATION COMPLETE (it would re-enter a loop that refuses at IDLE).
# rc 1 = NO MISMATCH: agent-state is RUNNING — either it always was, or the
#        yank reversal just restored it. Caller continues as normal.
# Never exits any other way; every probe failure resolves to rc=1 (the caller's
# unchanged behaviour) — a landing must be EARNED by a read state, never
# manufactured by a broken probe (guard-4220).
#
# Usage: state-mismatch-landing.sh --agent <name> [--sid <sid>]
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_paths.sh
source "$SCRIPT_DIR/_paths.sh"

AGENT=""; SID=""
while [ $# -gt 0 ]; do
    case "$1" in
        --agent) AGENT="${2:-}"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --sid)   SID="${2:-}";   shift $(( $# >= 2 ? 2 : 1 )) ;;
        *) shift ;;
    esac
done
[ -n "$AGENT" ] || exit 1
export MIND_AGENT="$AGENT"

# A WORKER Body is agent-state=IDLE BY DESIGN, so the mismatch this script
# exists to catch CANNOT occur on one — guard-5821: agent-state is a
# REDUCER-OWNED, per-agent signal that exactly one Body writes, at /start and
# /stop; a worker NEVER writes it, so on every box but the reducer's own it
# reads IDLE while a worker loop runs flat out. The header above is explicit
# that the incident this script was built for demoted "LIVE reducers
# RUNNING->IDLE"; a worker never held RUNNING to lose.
#
# Without this guard the landing fired at EVERY worker close (measured on cc-08,
# 2026-09-04, : exit 0 on a healthy worker whose reducer was LIVE on
# cc-04) and emitted an emphatic "STOP EXECUTING" directive whose step 1 is
# "invoke /aspirations-consolidate" — a reducer-only phase a worker MUST NOT run
# over its own unmerged state (the Nth-reducer defect). Firing wrongly here is
# strictly worse than not firing: it stops a healthy loop AND prescribes a
# forbidden action.
#
# BODY_ROLE is injected by the PreToolUse bash hook (bash-agent-inject.py) and
# is inherited by child scripts, so it reaches here through both production
# callers; it is the SAME signal agent-watchdog.py::is_worker_body reads, not a
# new one. Unset/anything-else is the reducer shape and falls through unchanged,
# so this can only ever SUPPRESS a landing on a worker — never manufacture one
# (guard-4220: a landing must be EARNED).
if [ "$(printf '%s' "${BODY_ROLE:-}" | tr '[:upper:]' '[:lower:]')" = "worker" ]; then
    exit 1
fi

state="$(bash "$SCRIPT_DIR/session-state-get.sh" 2>/dev/null | tr -d '\r\n' || echo "")"
case "$state" in
    RUNNING) exit 1 ;;
    IDLE) ;;
    *) exit 1 ;;   # UNINITIALIZED / NO_AGENT / unreadable: not this script's call
esac

SESS="$(agent_dir "$AGENT")/session"
reversal_note=""
if [ -n "$SID" ] && [ -f "$SESS/recovery-log.jsonl" ] && [ -f "$SCRIPT_DIR/recovery-yank-reverse.sh" ]; then
    # stderr goes to a file, not a `2>&1` capture: the guard-659 detector
    # (check-stderr-json-merge.py) reads any 2>&1-captured var near a JSON
    # parse as the silent-zero shape, and stdout is discarded here anyway.
    rev_errf="$SESS/.landing-reverse-stderr.$$"
    bash "$SCRIPT_DIR/recovery-yank-reverse.sh" --agent "$AGENT" --sid "$SID" >/dev/null 2>"$rev_errf"
    rev_rc=$?
    if [ "$rev_rc" -eq 0 ]; then
        rm -f "$rev_errf"
        echo "[state-mismatch-landing] agent-state was IDLE under a live loop: the recovery-gate yank of this session was REVERSED and RUNNING restored — continuing normally." >&2
        exit 1
    fi
    reversal_note="$(tr -d '\r' < "$rev_errf" 2>/dev/null | tail -n 3)"
    rm -f "$rev_errf"
fi

# Classify what took the state away (same vocabulary the worker's park uses).
verdict_json="$(python3 "$SCRIPT_DIR/recovery_yank.py" check --agent "$AGENT" --no-team-state --json 2>/dev/null)"
verdict="$(printf '%s' "$verdict_json" | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
y = d.get("yank") or {}
print("%s|%s|%s|%s" % (d.get("verdict") or "none", y.get("ts") or "", y.get("path") or "", (y.get("cause") or "")[:200]))' 2>/dev/null)"
v_kind="${verdict%%|*}"; rest="${verdict#*|}"
v_ts="${rest%%|*}"; rest="${rest#*|}"
v_path="${rest%%|*}"; v_cause="${rest#*|}"

echo ""
echo "[state-mismatch-landing] ═══ STATE MISMATCH — LANDING ═══"
echo "agent-state is IDLE while this loop was still executing (session ${SID:-<unbound>})."
if [ -f "$SESS/stop-requested" ] || [ -f "$SESS/stop-loop" ]; then
    echo "A stop signal is present: a /stop completed from another window. Nothing to reverse."
elif [ "$v_kind" = "recovery-yank" ]; then
    echo "recovery-gate.sh path ${v_path:-?} demoted this agent at ${v_ts:-?}: ${v_cause:-?}"
    echo "Reversal for this session was declined: ${reversal_note:-no session id to reverse for}"
elif [ "$v_kind" = "user-stop" ]; then
    echo "A user stop post-dates the last recovery-gate action (${v_ts:-?}); this is a completed /stop, not a yank."
else
    echo "No recovery-gate yank on record — the state was changed by /stop from another window or by hand."
fi
echo "NEXT ACTION REQUIRED — land gracefully; do NOT call Skill(aspirations) (its entry gate refuses at IDLE):"
echo "  1. Invoke /aspirations-consolidate in stop mode — handoff + working-memory flush, so this iteration's learning survives the landing."
echo "  2. END the turn on a Bash echo. The stop hook allows a turn-end at IDLE; a human /start relaunches the loop."
exit 0
