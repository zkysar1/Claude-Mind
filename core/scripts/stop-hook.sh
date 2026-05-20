#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Entry sentinel for hook-fire-audit () — FIRST executable line,
# bash-builtin only, fail-open. mtime of core/logs/hook-fires/stop-hook
# = last fire of this hook.
{ _HF_DIR="${BASH_SOURCE[0]%/*}/../.." ; mkdir -p "$_HF_DIR/core/logs/hook-fires" 2>/dev/null && : > "$_HF_DIR/core/logs/hook-fires/stop-hook" 2>/dev/null ; unset _HF_DIR ; } 2>/dev/null || true

# Stop hook — keeps the autonomous loop alive
#
# Gates: allow stop when appropriate (SID mismatch, not RUNNING, stop-loop, pending agents)
# Otherwise: BLOCK unconditionally. No counter. No tiers. No safety valve.
# The user has /stop and Ctrl+C. The hook's job is to keep the loop alive.

# `set -e` deliberately OMITTED. Under -e, a transient append to `$LOG`
# failure (disk full, OneDrive contention, antivirus scan) kills the script
# with a non-zero exit. Claude Code treats non-zero exit as ALLOW → loop dies
# on the very next turn-end. The old comment at the historical line 28-29
# acknowledged this risk but kept -e anyway, betting that "if the filesystem
# is broken, blocking would be worse." That bet is wrong for the random-stops
# problem we are trying to solve: a transient OneDrive lock is NOT a broken
# filesystem, and the cost of one missed log entry is FAR lower than the cost
# of the loop dying. Match session-save-id.sh and recovery-gate.sh's
# convention: -u + pipefail with explicit per-command `|| true` on log
# appends. DO NOT REINTRODUCE `set -e` here — see 2026-05-12 hardening pass.
set -uo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"

# --- Per-step timing instrumentation () ---
# Captures msec-resolution timestamps at each major step. On BLOCK path we emit
# a single JSONL record to core/logs/stop-hook-timing.jsonl. The investigation
# question is WHY 2.1.133 kills hooks at 8.4s when standalone runs are 2.5s —
# without per-step timings we can't localize which step ballooned. Cheap (5
# timestamp captures) and runs unconditionally, but only emits on BLOCK path
# (the slow case we're investigating). Fail-open: any timing-write failure must
# not block the BLOCK decision.
_T0=$(date +%s%3N)

# --- Audit log (persistent across sessions — diagnose why sessions die) ---
# NOTE: Under set -e, a failed >> append kills the script (= fail open, allows stop).
# This is acceptable — if the filesystem is broken, blocking would be worse.
# 2026-05-19 (plan v1 step 0.15-0.16): relocated from PROJECT_ROOT/ to
# core/logs/ canonical telemetry sink. `mkdir -p` makes the first-call write
# safe on a fresh deployment. Legacy paths still readable by analysis scripts
# during transition (see stop-hook-analyze.sh, runner-recent-block.sh).
mkdir -p "$PROJECT_ROOT/core/logs" 2>/dev/null || true
LOG="$PROJECT_ROOT/core/logs/stop-hook.log"
TIMING_LOG="$PROJECT_ROOT/core/logs/stop-hook-timing.jsonl"

# --- Log rotation (2026-05-19, plan v1 step 0.5) ---
# Cap LOG and TIMING_LOG at ~500 KB each by truncating to the last 1000 lines.
# Without rotation the LOG grew to 434 KB and would keep growing forever —
# consumers tail recent records, so older entries have zero practical value
# once a session has aged out. 1000 lines preserves multi-day BLOCK history
# for diagnosis. Truncation uses tail+mv (race-tolerant — only one stop hook
# fires per turn-end). Truncation failure is swallowed so it never blocks the
# BLOCK decision (the hook's primary job).
for _RFILE in "$LOG" "$TIMING_LOG"; do
    [ -f "$_RFILE" ] || continue
    _RSIZE=$(stat -c%s "$_RFILE" 2>/dev/null || echo 0)
    if [ "$_RSIZE" -gt 500000 ]; then
        _RTMP="$_RFILE.rot.$$"
        tail -n 1000 "$_RFILE" > "$_RTMP" 2>/dev/null && mv "$_RTMP" "$_RFILE" 2>/dev/null || rm -f "$_RTMP" 2>/dev/null || true
    fi
done
unset _RFILE _RSIZE _RTMP

# --- Read stdin ONCE (sole Stop hook — no stdin sharing, no race) ---
STDIN_JSON=$(cat)

# --- Resolve agent for THIS session (not from shared files) ---
# .active-agent-$SID is the only per-session binding; written by /start.
# No shared-file fallback exists — that path was retired with the bridge file
# (rb-386). Use the per-session binding directly.
HOOK_SID=$(printf '%s' "$STDIN_JSON" | py -3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")

# Can't identify this session — don't risk blocking the wrong window
if [ -z "$HOOK_SID" ]; then
    echo "$(date +%Y-%m-%dT%H:%M:%S) ALLOW gate=no-sid" >> "$LOG" 2>/dev/null || true
    exit 0
fi

HOOK_AGENT=""
if [ -f "$PROJECT_ROOT/.active-agent-$HOOK_SID" ]; then
    HOOK_AGENT=$(cat "$PROJECT_ROOT/.active-agent-$HOOK_SID" 2>/dev/null | tr -d '\r\n')
fi

# Do NOT fall back to $AGENT_NAME from _paths.sh here. _paths.sh resolves
# via MIND_AGENT env, which may be unset at hook time. Historically a
# shared-file fallback caused cross-agent contamination (a terminal whose
# binding file was missing silently adopted another agent's identity and
# was told to run that agent's aspirations loop; rb-386). Strict-match via
# binding file OR reverse-lookup is the only safe path. If neither resolves,
# exit 0 with gate=no-agent — identical to the canonical resolver's
# contract in _resolve_agent_from_sid.py and idle-tick.sh:40.

# Fallback: reverse-lookup — the agent name is in the path
if [ -z "$HOOK_AGENT" ]; then
    for _RSF in "$PROJECT_ROOT"/*/session/running-session-id; do
        [ -f "$_RSF" ] || continue
        _RSID=$(cat "$_RSF" 2>/dev/null | tr -d '\r\n')
        if [ "$_RSID" = "$HOOK_SID" ]; then
            HOOK_AGENT=$(basename "$(dirname "$(dirname "$_RSF")")")
            # Self-heal: recreate the binding so next time is O(1)
            echo "$HOOK_AGENT" > "$PROJECT_ROOT/.active-agent-$HOOK_SID"
            break
        fi
    done
fi

# No agent resolved — nothing to block for
if [ -z "$HOOK_AGENT" ]; then
    echo "$(date +%Y-%m-%dT%H:%M:%S) ALLOW gate=no-agent sid=$HOOK_SID" >> "$LOG" 2>/dev/null || true
    exit 0
fi

HOOK_AGENT_DIR="$(agent_dir "$HOOK_AGENT")"

# --- Read runner-token (framework-owned UUID for SID-collision audit) ---
# 2026-05-12 hardening Tier 3a. Logged in every BLOCK / sid-mismatch ALLOW so
# a Claude Code SID collision shows up as "same SID, different runner_token"
# in core/logs/stop-hook.log. WITHOUT this signal, the 2026-05-12 zeta-bravo cross-
# binding was invisible until forensic analysis. Read-only; empty token logs
# as "runner_token=" (clean signal that the agent started before tokens were
# introduced, OR token-write failed at /start).
RUNNER_TOKEN_LOG=""
if [ -f "$HOOK_AGENT_DIR/session/runner-token" ]; then
    RUNNER_TOKEN_LOG=$(cat "$HOOK_AGENT_DIR/session/runner-token" 2>/dev/null | tr -d '\r\n')
fi

# --- Insight capture (non-critical — must not affect blocking decision) ---
printf '%s' "$STDIN_JSON" | MIND_AGENT="$HOOK_AGENT" py -3 "$CORE_ROOT/scripts/capture-insights.py" 2>/dev/null || true
_T_AFTER_INSIGHTS=$(date +%s%3N)

# --- Housekeeping: stale session files + legacy artifacts ---
# DO NOT REMOVE the touch — refreshes own-binding mtime so observer modes
# (assistant/reader) which emit no other liveness signal aren't deleted by
# the helper below. See cleanup-stale-bindings.sh OBSERVER-MODE NOTE.
touch -c "$PROJECT_ROOT/.active-agent-$HOOK_SID" 2>/dev/null || true
bash "$CORE_ROOT/scripts/cleanup-stale-bindings.sh" 2>/dev/null || true
rm -f "$PROJECT_ROOT/.stop-hook-stdin.json"
_T_AFTER_HOUSEKEEPING=$(date +%s%3N)

# --- Gate 0: Session identity — only block the runner session ---
# running-session-id is set by /start (autonomous mode) and kept in sync by
# session-save-id.sh (on compact). If missing, no loop is running — allow stop.
RUNNER_FILE="$HOOK_AGENT_DIR/session/running-session-id"
if [ ! -f "$RUNNER_FILE" ]; then
    echo "$(date +%Y-%m-%dT%H:%M:%S) ALLOW gate=no-runner sid=$HOOK_SID agent=$HOOK_AGENT" >> "$LOG" 2>/dev/null || true
    exit 0
fi
RUNNER_SID=$(cat "$RUNNER_FILE" 2>/dev/null | tr -d '\r\n' || echo "")
if [ -n "$RUNNER_SID" ] && [ "$HOOK_SID" != "$RUNNER_SID" ]; then
    echo "$(date +%Y-%m-%dT%H:%M:%S) ALLOW gate=sid-mismatch sid=$HOOK_SID runner=$RUNNER_SID agent=$HOOK_AGENT runner_token=$RUNNER_TOKEN_LOG" >> "$LOG" 2>/dev/null || true
    exit 0  # Different session — not the autonomous loop runner, allow stop
fi

# DO NOT use "$_A bash ..." — variable expansion is not recognized as an env
# assignment prefix by bash. Use export so all child processes inherit the agent.
export MIND_AGENT="$HOOK_AGENT"

# --- Gate 1: Not RUNNING → allow stop ---
STATE=$(bash "$CORE_ROOT/scripts/session-state-get.sh" 2>/dev/null || echo "UNINITIALIZED")
if [ "$STATE" != "RUNNING" ]; then
    echo "$(date +%Y-%m-%dT%H:%M:%S) ALLOW gate=not-running sid=$HOOK_SID agent=$HOOK_AGENT state=$STATE runner_token=$RUNNER_TOKEN_LOG" >> "$LOG" 2>/dev/null || true
    exit 0
fi

# --- Gate 2: stop-loop signal (set by /stop) → allow stop + cleanup ---
if bash "$CORE_ROOT/scripts/session-signal-exists.sh" stop-loop 2>/dev/null; then
    bash "$CORE_ROOT/scripts/session-signal-clear.sh" stop-loop
    echo "$(date +%Y-%m-%dT%H:%M:%S) ALLOW gate=stop-loop sid=$HOOK_SID agent=$HOOK_AGENT runner_token=$RUNNER_TOKEN_LOG" >> "$LOG" 2>/dev/null || true
    exit 0
fi

# --- Gate 2.5: Pending background agents → allow stop ---
if bash "$CORE_ROOT/scripts/pending-agents.sh" has-pending 2>/dev/null; then
    echo "$(date +%Y-%m-%dT%H:%M:%S) ALLOW gate=pending-agents sid=$HOOK_SID agent=$HOOK_AGENT runner_token=$RUNNER_TOKEN_LOG" >> "$LOG" 2>/dev/null || true
    exit 0
fi

# --- Gate 2.6: Pending long-running background jobs → allow stop ---
# pending-agents.sh (Gate 2.5) tracks short-lived Claude sub-agents (~10-min).
# background-jobs.sh tracks long-running OS processes (hours: SSH tunnels,
# Processor runs, etc.). They are SEPARATE data stores with SEPARATE schemas;
# checking only one was the asymmetry that let stop-hook BLOCK during a live
# long-running job while recovery-gate's Condition 4 (Path A) checked the
# correct one. Mirror recovery-gate so both surfaces agree.
# Fail-open via 2>/dev/null + exit-1-fallthrough: any script error treated as
# "no pending jobs" (BLOCK proceeds) — never the wrong direction for liveness.
if bash "$CORE_ROOT/scripts/background-jobs.sh" has-pending 2>/dev/null; then
    echo "$(date +%Y-%m-%dT%H:%M:%S) ALLOW gate=background-jobs sid=$HOOK_SID agent=$HOOK_AGENT runner_token=$RUNNER_TOKEN_LOG" >> "$LOG" 2>/dev/null || true
    exit 0
fi
_T_AFTER_GATES=$(date +%s%3N)

# --- BLOCK: Agent is RUNNING, no stop signal — keep the loop alive ---
# WHY this file: Autocompact changes the session ID. session-save-id.sh (the
# SessionStart hook) needs to know which agent just compacted so it can update
# running-session-id with the new SID. This file contains the OLD SID — if it
# matches running-session-id, session-save-id.sh knows this agent just compacted.
# Lives in the agent's session dir (not project root) to avoid multi-agent races.
echo "$HOOK_SID" > "$HOOK_AGENT_DIR/session/compact-pending"
echo "$(date +%Y-%m-%dT%H:%M:%S) BLOCK sid=$HOOK_SID agent=$HOOK_AGENT runner_token=$RUNNER_TOKEN_LOG" >> "$LOG" 2>/dev/null || true

# --- Trailing-text detection (Layer-C, ) — fail-open observability ---
# Run detector on the Stop-hook event to identify trailing-prose patterns that
# would have killed the loop if the hook had not fired (rb-629, guard-454).
# On severity=high, enrich BLOCK message with a marker diagnostic AND append
# a record to loop-death-detections.jsonl for post-hoc analysis. Detector
# errors must not break Stop hook (fail-open via 2>/dev/null + || true).
TTD_MARKER=""
TTD_SEVERITY=""
TTD_EVIDENCE=""
TTD_DETECTOR="$WORLD_DIR/scripts/trailing-text-detector.py"
if [ -f "$TTD_DETECTOR" ]; then
    # Hard timeout (, sq-011 forward-prediction). A hanging detector
    # (slow disk read on transcript_path, infinite loop on malformed JSONL,
    # network mount latency) would hang the Stop hook itself, so the agent's
    # Stop event never resolves and the loop dies a different way. SIGTERM at
    # 2s; SIGKILL 1s later if SIGTERM didn't take. timeout exit=124 → empty
    # TTD_RESULT → fail-open (no marker emitted, BLOCK still fires correctly).
    TTD_RESULT=$(printf '%s' "$STDIN_JSON" | timeout --kill-after=1 2 py -3 "$TTD_DETECTOR" --stop-hook 2>/dev/null || true)
    if [ -n "$TTD_RESULT" ]; then
        TTD_MARKER=$(printf '%s' "$TTD_RESULT" | py -3 -c "import sys,json;d=json.loads(sys.stdin.read() or '{}');print(d.get('marker') or '')" 2>/dev/null || echo "")
        TTD_SEVERITY=$(printf '%s' "$TTD_RESULT" | py -3 -c "import sys,json;d=json.loads(sys.stdin.read() or '{}');print(d.get('severity') or '')" 2>/dev/null || echo "")
        TTD_EVIDENCE=$(printf '%s' "$TTD_RESULT" | py -3 -c "import sys,json;d=json.loads(sys.stdin.read() or '{}');print(d.get('evidence') or '')" 2>/dev/null || echo "")
    fi
fi

# Append to detection log on high severity (fail-open)
# Cross-agent concurrency: alpha and bravo stop-hooks can fire simultaneously
# (e.g., parallel autocompact). Bare open(a) interleaves records and corrupts
# the line-delimited format. Route through _fileops.locked_append_jsonl which
# acquires .lock + snapshots history + atomic-writes. Fix per alpha F-001
# finding (msg-20260504-223622-alpha-727,  cross-agent fresh-eyes).
if [ "$TTD_SEVERITY" = "high" ] && [ -n "$TTD_MARKER" ]; then
    TTD_NOW="$(date +%Y-%m-%dT%H:%M:%S)"
    TTD_MARKER="$TTD_MARKER" TTD_SEVERITY="$TTD_SEVERITY" TTD_EVIDENCE="$TTD_EVIDENCE" \
    TTD_NOW="$TTD_NOW" TTD_AGENT="$HOOK_AGENT" TTD_SID="$HOOK_SID" \
    TTD_LOG="$WORLD_DIR/loop-death-detections.jsonl" \
    TTD_CORE_SCRIPTS="$PROJECT_ROOT/core/scripts" \
    py -3 -c "
import json, os, sys
sys.path.insert(0, os.environ['TTD_CORE_SCRIPTS'])
from _fileops import locked_append_jsonl
rec = {
    'timestamp': os.environ['TTD_NOW'],
    'agent': os.environ['TTD_AGENT'],
    'session_id': os.environ['TTD_SID'],
    'marker': os.environ['TTD_MARKER'],
    'evidence_snippet': os.environ.get('TTD_EVIDENCE', ''),
    'severity': os.environ['TTD_SEVERITY'],
}
locked_append_jsonl(os.environ['TTD_LOG'], rec)
" 2>/dev/null || true
fi
_T_AFTER_TTD=$(date +%s%3N)

# Context-aware BLOCK payload (session 58 alpha-stopping follow-up). Stop
# fires for two distinct reasons: (1) autocompact end-of-turn, (2) a text
# summary terminated the turn without Skill(aspirations). Both need the same
# re-entry instruction, but the checkpoint snapshot helps the LLM reconcile
# phase_completed without re-reading the file.
#
# CRITICAL — DO NOT add per-phase hint strings here (tried and reverted
# 2026-04-24). phase_completed only takes three values: verify, state_update,
# learning_gate — written by _checkpoint_refresh in iteration-close.sh at
# lines 202/446/720. Anything else (productivity, selected, unknown) is not
# produced by this codebase. Per-phase messaging (a) implied coverage of
# non-existent states and (b) added cognitive load for future readers. The
# uniform "call Skill(aspirations); here is the checkpoint" form is enough —
# the LLM resolves phase semantics from the checkpoint + Phase -1 logic.
#
# Python generates the full JSON so string content from the checkpoint (goal
# IDs, phase values) cannot corrupt the decision payload via shell escaping.
HOOK_AGENT_DIR="$HOOK_AGENT_DIR" HOOK_AGENT="$HOOK_AGENT" \
TTD_MARKER="$TTD_MARKER" TTD_SEVERITY="$TTD_SEVERITY" \
py -3 - <<'PYEOF'
import json, os, pathlib

agent_dir = pathlib.Path(os.environ["HOOK_AGENT_DIR"])
agent     = os.environ["HOOK_AGENT"]

# Checkpoint context — just the raw goal_id + phase_completed. Let the LLM
# interpret; do not add per-phase narrative. Fail-open on corrupt checkpoint.
cp_context = ""
cp_path = agent_dir / "session" / "iteration-checkpoint.json"
if cp_path.exists():
    try:
        cp = json.loads(cp_path.read_text(encoding="utf-8"))
        gid = cp.get("goal_id", "")
        pc  = cp.get("phase_completed", "") or "unknown"
        if gid:
            cp_context = f" Last checkpoint: goal={gid} phase_completed={pc}."
    except Exception:
        pass

# Compact-checkpoint note — pre-existing behavior preserved.
compact_msg = ""
if (agent_dir / "session" / "compact-checkpoint.yaml").exists():
    compact_msg = " Encoding checkpoint saved -- Phase -0.5c will process it on re-entry."

# Trailing-text detector diagnostic (, rb-629, guard-454, rb-670, guard-462).
# Layer-C observability — surface the specific anti-pattern marker the
# detector flagged in the assistant turn that triggered this Stop. Marker
# names live in world/scripts/trailing-text-detector.py:
# user_directive_rationalization (Shape α′, 2026-05-01 — see guard-462),
# insight_block, phase_summary, next_step_narration, trailing_prose
# (the original four — see guard-454). Only emitted on
# severity=high so low/medium signals stay out of the BLOCK message.
ttd_msg = ""
ttd_marker = os.environ.get("TTD_MARKER", "")
ttd_severity = os.environ.get("TTD_SEVERITY", "")
if ttd_severity == "high" and ttd_marker:
    ttd_msg = f" TRAILING-TEXT PATTERN: {ttd_marker} -- see guard-454."

reason = (
    "Turn ended without a Skill(aspirations) re-entry (autocompact OR a text "
    "summary terminated the turn). Your FIRST action MUST be: Skill('aspirations') "
    "with args='loop'. Do NOT manually select goals. Do NOT run Bash commands "
    "first. Call the Skill tool IMMEDIATELY."
    + cp_context
    + f" Agent: {agent}. Prefix all Bash with MIND_AGENT={agent}."
    + compact_msg
    + ttd_msg
)

print(json.dumps({"decision": "block", "reason": reason}))
PYEOF

# --- Emit timing record (, fail-open) ---
# One JSONL record per BLOCK invocation. Step deltas localize the slow step
# under Claude Code 2.1.133 (8.4s observed vs 2.5s on standalone runs). The
# decision payload above has already been printed to stdout via PYEOF, so
# the hook's decision is committed before this fires. Failure here is
# silent (2>/dev/null + || true) and cannot affect the BLOCK decision.
_T_END=$(date +%s%3N)
T0="$_T0" T_AI="${_T_AFTER_INSIGHTS:-0}" T_AH="${_T_AFTER_HOUSEKEEPING:-0}" \
T_AG="${_T_AFTER_GATES:-0}" T_AT="${_T_AFTER_TTD:-0}" T_END="$_T_END" \
TIMING_LOG="$TIMING_LOG" HOOK_AGENT="$HOOK_AGENT" HOOK_SID="$HOOK_SID" \
TTD_SEVERITY="${TTD_SEVERITY:-}" TTD_MARKER="${TTD_MARKER:-}" \
py -3 -c "
import json, os, datetime
def _i(k):
    try: return int(os.environ.get(k,'0') or 0)
    except: return 0
T0=_i('T0'); T_AI=_i('T_AI'); T_AH=_i('T_AH'); T_AG=_i('T_AG')
T_AT=_i('T_AT'); T_END=_i('T_END')
rec = {
    'timestamp': datetime.datetime.now().isoformat(timespec='seconds'),
    'agent': os.environ.get('HOOK_AGENT',''),
    'session_id': os.environ.get('HOOK_SID',''),
    'decision': 'block',
    'ttd_severity': (os.environ.get('TTD_SEVERITY','') or None),
    'ttd_marker': (os.environ.get('TTD_MARKER','') or None),
    'capture_insights_ms': (T_AI - T0) if T_AI else None,
    'housekeeping_ms': (T_AH - T_AI) if (T_AH and T_AI) else None,
    'gates_ms': (T_AG - T_AH) if (T_AG and T_AH) else None,
    'ttd_ms': (T_AT - T_AG) if (T_AT and T_AG) else None,
    'decision_payload_ms': (T_END - T_AT) if T_AT else None,
    'total_ms': T_END - T0,
}
with open(os.environ['TIMING_LOG'], 'a', encoding='utf-8') as f:
    f.write(json.dumps(rec) + '\n')
" 2>/dev/null || true

exit 0
