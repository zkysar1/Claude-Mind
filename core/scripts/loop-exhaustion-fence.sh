#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# loop-exhaustion-fence.sh — the AUTHORIZED stop-signal writer for a loop that
# CANNOT EXECUTE ().
#
# A loop with no room left to act has, until now, had exactly one legal move:
# iterate emptily.  Measured 2026-09-04 (bravo, cc-05): ~35 null iterations over
# 2h21m, execution-diary mtime frozen throughout, at roughly one full model turn
# per 40s indefinitely.  This fence gives that condition a branch.
#
# AUTHORIZATION: `.claude/rules/stop-hook-compliance.md` rule 2 names this script
# as the THIRD authorized writer of `session-signal-set.sh stop-requested`
# outside /stop (productivity-stop-gate.sh first, reducer-self-fence.sh second).
# The recovery-gate / recovery-yank pair listed in that rule move `agent-state`
# rather than setting this signal and are a separate count.  INVOKED ONLY by
# stop-hook.sh.  The LLM MUST NOT invoke this directly.
#
# The DECISION is script-gated in loop_exhaustion_fence.py::decide (pure, fully
# branch-tested) — not LLM-discretionary — so the model cannot elect a stop
# because it feels done.  This wrapper owns only the WRITE.
#
# FAIL-OPEN EVERYWHERE.  A fence that cannot decide must never stop a healthy
# loop and must never delay a hook: every failure path exits 0 without writing.
#
# WORKER MODE (FENCE_ROLE=worker, ) is the WORKER BLOCK below, called
# from stop-hook.sh's worker-net branch.  It writes NO stop signal and so adds
# nothing to the authorized-writer count above: its decisive rung PARKS the Body
# and alerts.  Decision: loop_exhaustion_fence.py::decide_worker.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

AGENT="${MIND_AGENT:-}"
[ -z "$AGENT" ] && exit 0   # not a bound context

# Idempotent: a stop is already in progress, so there is nothing to add and
# re-writing stop-target-mode could race /stop's own write.
if bash "$SCRIPT_DIR/session-signal-exists.sh" stop-requested 2>/dev/null; then
    exit 0
fi

# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || true

# The `|| true` above is load-bearing for fail-open and also lets _paths.sh fail
# SILENTLY, leaving agent_dir undefined so SESSION_DIR would become "/session"
# — under root with a writable / that path is WRITABLE, which would split
# stop-target-mode from the signal (reducer-self-fence.sh F-001, ).
# Validate before writing anything.
SESSION_DIR=""
if declare -f agent_dir >/dev/null 2>&1; then
    _AD="$(agent_dir "$AGENT" 2>/dev/null || true)"
    [ -n "$_AD" ] && [ -d "$_AD" ] && SESSION_DIR="$_AD/session"
fi
if [ -z "$SESSION_DIR" ] || [ ! -d "$SESSION_DIR" ]; then
    echo "[loop-exhaustion-fence] session dir unresolved for agent=$AGENT; holding" >&2
    exit 0
fi

# `py -3` is the Windows launcher shape and `python3` takes no -3, so the flag
# travels WITH the interpreter choice, never appended to whichever won
# (guard-335 / rb-370 — python-invocation.md).
if command -v py >/dev/null 2>&1; then
    PYRUN=(py -3)
else
    PYRUN=(python3)
fi

# --- WORKER BLOCK () ----------------------------------------------
# A worker Body must never write the agent-wide stop-requested/stop-target-mode:
# the reducer on another machine reads them (stop/SKILL.md Step 0.6).  So the
# decisive rung here is the park worker-loop Phase 1 takes -- resumable, on the
# park orbit -- plus an alert, because a Body that keeps ending turns while it
# holds no claim is a defect, not the quiet reducer-gone park.
# stdout is the BLOCK reason addendum, so every write below sends its own stdout
# to stderr, which the hook appends to its log (the recorder's notify verdict
# must not be discarded -- guard-3737).
if [ "${FENCE_ROLE:-}" = "worker" ]; then
    [ -z "${HOOK_SID:-}" ] && exit 0
    W_JSON="$("${PYRUN[@]}" "$SCRIPT_DIR/loop_exhaustion_fence.py" --role worker \
        --sid "$HOOK_SID" --log "${HOOK_LOG:-}" 2>/dev/null || true)"
    [ -z "$W_JSON" ] && exit 0
    W_VERDICT="$(printf '%s' "$W_JSON" | "${PYRUN[@]}" -c \
        "import sys,json;print(json.load(sys.stdin).get('verdict',''))" 2>/dev/null || true)"
    W_REASON="$(printf '%s' "$W_JSON" | "${PYRUN[@]}" -c \
        "import sys,json;print(json.load(sys.stdin).get('reason',''))" 2>/dev/null || true)"
    case "$W_VERDICT" in
      pause)
        # No write: the directive is the whole rung.
        echo "WORKER-STALL PAUSE: ${W_REASON}. This Body holds NO claim and keeps ending turns; a sleep is not a park, and it is what this fence counts. Your next action is Skill('worker-loop'): CLAIM the top eligible goal at Phase 1 SELECT, or, if none is eligible, take the Phase 1 PARK. If the turn-ends continue with no claim, this fence parks the Body and alerts the user."
        exit 1
        ;;
      park)
        # Park FIRST: it is the state change that ends the burn, and the alert
        # below must be true when it is sent (recovery-gate.sh's rule).
        PARK_RESULT="$("${PYRUN[@]}" "$SCRIPT_DIR/body-manifest.py" park \
            --sid "$HOOK_SID" --agent "$AGENT" || true)"
        if [ "$PARK_RESULT" != "parked" ]; then
            echo "[loop-exhaustion-fence] worker park decided (${W_REASON}) but body-manifest park returned '${PARK_RESULT}'; nothing parked or alerted" >&2
            exit 0
        fi
        # The board first: a fast write the whole fleet reads.  The recorder
        # second: it notifies, and may wait on a mail transport.
        printf '%s\n' "${AGENT} worker Body ${HOOK_SID} on $(uname -n 2>/dev/null) PARKED by the worker loop-exhaustion fence (g-115-11083): ${W_REASON}. Resumable: it re-polls on the park orbit and resumes on its next claim. A stall-park is a defect signal -- the Body kept ending turns while holding no claim." \
            | bash "$SCRIPT_DIR/board-post.sh" --channel coordination --type finding \
                --tags "worker-stall,body-parked,loop-exhaustion-fence" >&2 \
            || echo "[loop-exhaustion-fence] WARN: board post failed; the stop-reason record still alerts" >&2
        "${PYRUN[@]}" "$SCRIPT_DIR/stop-reason-record.py" --path worker-body-stall-parked \
            --agent "$AGENT" --reason "Body ${HOOK_SID}: ${W_REASON}" >&2 \
            || echo "[loop-exhaustion-fence] WARN: stop-reason recorder exited non-zero; the park may be unannounced" >&2
        echo "WORKER-STALL PARK: ${W_REASON}. This fence has PARKED this Body (body_state parked: resumable, never a close) and alerted the user and the coordination board; a script gate decided this, not you. Your next action is Skill('worker-loop'): its Phase -0 reads the parked manifest and takes the park orbit from there. Do NOT answer this with a sleep."
        exit 3
        ;;
      *)
        exit 0
        ;;
    esac
fi

# Budget zone: RECORDED in the reason, never decisive (module docstring and
# _cause_note). Until 2026-09-15 this wrapper passed nothing, so every firing
# printed "context budget zone: unrecorded" and readers filled the blank with
# the module's NAME -- two agents (alpha 09-11, zeta 09-15) narrated "ran out
# of context" over a verdict that had disclaimed exactly that. The sensor has an
# independent writer (statusLine hook, guard-6255). Fail-open: an absent or
# unparseable sensor file passes no flag and the reason says "unrecorded".
BUDGET_ZONE="$("${PYRUN[@]}" -c \
    "import json,sys;print((json.load(open(sys.argv[1],encoding='utf-8')).get('zone') or '').strip())" \
    "$SESSION_DIR/context-budget.json" 2>/dev/null || true)"
ZONE_ARGS=()
[ -n "$BUDGET_ZONE" ] && ZONE_ARGS=(--budget-zone "$BUDGET_ZONE")

VERDICT_JSON="$("${PYRUN[@]}" "$SCRIPT_DIR/loop_exhaustion_fence.py" \
    --sid "${HOOK_SID:-}" \
    --log "${HOOK_LOG:-}" \
    --diary "$SESSION_DIR/execution-diary.jsonl" \
    ${ZONE_ARGS[@]+"${ZONE_ARGS[@]}"} \
    2>/dev/null || true)"
[ -z "$VERDICT_JSON" ] && exit 0

VERDICT="$(printf '%s' "$VERDICT_JSON" | "${PYRUN[@]}" -c \
    "import sys,json;print(json.load(sys.stdin).get('verdict',''))" 2>/dev/null || true)"
REASON="$(printf '%s' "$VERDICT_JSON" | "${PYRUN[@]}" -c \
    "import sys,json;print(json.load(sys.stdin).get('reason',''))" 2>/dev/null || true)"

case "$VERDICT" in
  pause)
    # No write.  The message is the whole rung: it tells the turn to end on a
    # REGISTERED external-wait sleep (stop-hook Gate 2.6 ALLOWs a turn-end that
    # has one) instead of re-entering immediately.  Reversible by construction —
    # if room frees up, the next wake resumes the ordinary loop.
    echo "LOOP-STALL PAUSE: ${REASON}. Do NOT re-enter the loop immediately. End this turn on 'EXTERNAL_WAIT=1 bash core/scripts/interruptible-sleep.sh 600', which registers a background job so this turn-end is ALLOWed, then resume normally."
    exit 1
    ;;
  stop)
    # ORDER CRITICAL — /stop Phase -1.4 reads stop-target-mode with no fallback,
    # so the file MUST exist before stop-requested is set.  Do NOT reorder.
    printf 'assistant' > "$SESSION_DIR/stop-target-mode" 2>/dev/null || exit 0
    if ! bash "$SCRIPT_DIR/session-signal-set.sh" stop-requested 2>/dev/null; then
        rm -f "$SESSION_DIR/stop-target-mode" 2>/dev/null || true
        echo "[loop-exhaustion-fence] WARN: stop decided (${REASON}) but session-signal-set failed; reverted stop-target-mode. Loop continues." >&2
        exit 0
    fi
    printf '%s loop-exhaustion-fence stop agent=%s sid=%s verdict=%s\n' \
        "$(date +%Y-%m-%dT%H:%M:%S)" "$AGENT" "${HOOK_SID:-}" "$VERDICT_JSON" \
        >> "$SESSION_DIR/loop-exhaustion-fence.log" 2>/dev/null || true
    echo "LOOP-STALL STOP: ${REASON}. stop-requested is now SET (target mode: assistant). Your next action is the ordinary graceful stop at Phase -1.4 — complete in-flight obligations and stop. This was decided by a script gate, not by you."
    exit 2
    ;;
  *)
    exit 0
    ;;
esac
