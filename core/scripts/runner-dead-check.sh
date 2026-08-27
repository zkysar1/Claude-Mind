#!/usr/bin/env bash
# runner-dead-check.sh — Canonical 6-condition liveness gate ()
#
# Purpose: report whether an agent's autonomous runner is structurally dead
# (all 6 liveness signals confirm the runner is not advancing). This is the
# SINGLE SOURCE OF TRUTH for the 6-condition gate that recovery-gate.sh
# (SessionStart hook) and /start SKILL.md auto-recovery branch also rely on.
#
# Created 2026-05-19 () to fix the silent-loop-death failure mode
# where /start --recover used only heartbeat-stale as the liveness signal.
# A non-runner session that ran --recover with stale heartbeat (common
# between iterations) could stomp the live runner's running-session-id —
# canonical incident: alpha session 4334f019 vs d964c395 on 2026-05-18.
#
# Inputs:
#   MIND_AGENT (env, required) — the agent whose runner is being probed
#   DIARY_STALE_MINUTES (env, optional, default 15) — Condition 2.7 threshold
#
# Outputs:
#   stdout: JSON state report (always emitted) with per-condition booleans + messages
#   stderr: human-readable per-condition summary (6 lines)
#   exit 0: runner is DEAD — all 6 conditions met (safe to recover)
#   exit 1: runner is ALIVE — at least one liveness signal positive
#   exit 2: script error — fail-open conservative (treat as alive, suppress recovery)
#
# The 6 conditions (must ALL hold for dead-runner verdict):
#   (1)   agent-state == RUNNING
#   (2)   heartbeat-stale.sh returns "stale" (mtime > threshold)
#   (2.5) runner-recent-block.sh returns 1 (no .stop-hook-log BLOCK in 5 min)
#   (2.7) execution-diary.jsonl mtime > DIARY_STALE_MINUTES (default 15 min)
#   (3)   session-signal-exists.sh stop-requested returns 1 (signal absent)
#   (4)   background-jobs.sh has-pending returns 1 (no Tier-A jobs)
#
# Exit-code semantics for sub-probes (rb-762): only rc=1 is "continue".
# rc=0 (positive signal) AND rc=2+ (script error) BOTH suppress recovery.
# Conservative: a broken probe must NEVER stomp a possibly-live runner.
#
# Callers (parallel implementations — keep this helper in sync with all):
#   - /start SKILL.md Step 0.7 (--recover branch;  made it call this)
#   - /start SKILL.md auto-recovery (RUNNING + autonomous; LLM-orchestrated copy
#     kept inline because user-facing messaging needs per-condition branching;
#     the inline must mirror this helper)
#   - core/scripts/recovery-gate.sh run_gate_for_agent (SessionStart hook;
#      made it delegate to this helper)
#
# Any change to the 6 conditions or probe scripts MUST update:
#   - This helper (canonical)
#   - /start SKILL.md auto-recovery section (inline LLM copy)
# The other two callers defer to this helper.

set -u

agent="${MIND_AGENT:-}"
if [[ -z "$agent" ]]; then
    printf '{"error": "MIND_AGENT not set", "dead": false}\n'
    echo "ERROR: MIND_AGENT not set — cannot check runner liveness" >&2
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Source _paths.sh for canonical PROJECT_ROOT resolution (handles all
# the platform-specific path normalization; same idiom as recovery-gate.sh:87-89).
# shellcheck source=_paths.sh
source "$SCRIPT_DIR/_paths.sh"
agent_dir="$(agent_dir "$agent")"

if [[ ! -d "$agent_dir/session" ]]; then
    printf '{"error": "agent session dir not initialized", "agent": "%s", "dead": false}\n' "$agent"
    echo "ERROR: agent dir '$agent_dir/session' not initialized" >&2
    exit 2
fi

# ─── Condition 1: agent-state == RUNNING ──────────────────────────
state="$(MIND_AGENT="$agent" bash "$SCRIPT_DIR/session-state-get.sh" 2>/dev/null || echo "")"
c1_pass=0
if [[ "$state" == "RUNNING" ]]; then
    c1_pass=1
    c1_msg="state=RUNNING"
else
    c1_msg="state=${state:-MISSING} (not RUNNING — no autonomous runner to recover)"
fi

# ─── Condition 2: heartbeat stale ─────────────────────────────────
# heartbeat-stale.sh returns fresh/stale based purely on file mtime.
# interruptible-sleep.sh ticks the heartbeat every 60s during B7 waits,
# so a stale mtime means nobody is running the loop at all.
hb="$(MIND_AGENT="$agent" bash "$SCRIPT_DIR/heartbeat-stale.sh" 2>/dev/null || echo "fresh")"
c2_pass=0
if [[ "$hb" == "stale" ]]; then
    c2_pass=1
    c2_msg="heartbeat=stale"
else
    c2_msg="heartbeat=fresh (last tick within staleness threshold)"
fi

# ─── Condition 2.5: NO recent stop-hook BLOCK () ─────────
# Multi-signal liveness — heartbeat alone is too weak when transient
# platform issues (e.g., Claude Code 2.1.133 stop-hook timeouts) make
# heartbeat staleness a false-positive zombie signal. A recent BLOCK in
# .stop-hook-log proves stop-hook fired AND the loop re-entered.
MIND_AGENT="$agent" bash "$SCRIPT_DIR/runner-recent-block.sh" "$agent" >/dev/null 2>&1
rb_rc=$?
c25_pass=0
if [[ "$rb_rc" -eq 1 ]]; then
    c25_pass=1
    c25_msg="no recent BLOCK (last 5 min clean)"
elif [[ "$rb_rc" -eq 0 ]]; then
    c25_msg="recent BLOCK in .stop-hook-log within last 5 min (runner alive — transient hook timeout suspected if heartbeat=stale)"
else
    c25_msg="runner-recent-block probe error (rc=$rb_rc — fail-open conservative, treat as alive)"
fi

# ─── Condition 2.7: execution-diary stale () ─────────────
# Second multi-signal liveness probe. execution-diary.jsonl is appended at
# every Phase start/end (sub-minute granularity) AND survives stop-hook
# interruptions because phase-end writes BEFORE the LLM yields the turn.
# Threshold rationale (DIARY_STALE_MINUTES default 15): smaller than
# heartbeat's 30 min to be more protective of live runners; large enough
# that a genuine zombie whose diary went silent ≥15min ago still recovers.
diary="$agent_dir/session/execution-diary.jsonl"
diary_stale_min="${DIARY_STALE_MINUTES:-15}"
diary_age_min=999
if [[ -f "$diary" ]]; then
    diary_age_min=$(DIARY_E="$diary" py -3 - <<'PYEOF' 2>/dev/null || echo "999"
import os, time
try:
    age_s = time.time() - os.path.getmtime(os.environ['DIARY_E'])
    print(int(age_s / 60))
except Exception:
    print(999)
PYEOF
)
fi
c27_pass=0
if [[ "$diary_age_min" -ge "$diary_stale_min" ]] 2>/dev/null; then
    c27_pass=1
    if [[ ! -f "$diary" ]]; then
        c27_msg="execution-diary absent (fresh-install — fail-open: gate by other signals)"
    else
        c27_msg="execution-diary stale (${diary_age_min}min ago, threshold ${diary_stale_min}min)"
    fi
else
    c27_msg="execution-diary fresh (${diary_age_min}min ago, threshold ${diary_stale_min}min — runner alive)"
fi

# ─── Condition 3: stop-requested NOT set ──────────────────────────
# Exit-code semantics MUST mirror Cond 2.5/Cond 4 (rb-762): rc=1 is ONLY
# "continue". rc=0 (signal exists) AND rc=2+ (script error) both suppress.
MIND_AGENT="$agent" bash "$SCRIPT_DIR/session-signal-exists.sh" stop-requested >/dev/null 2>&1
sr_rc=$?
c3_pass=0
if [[ "$sr_rc" -eq 1 ]]; then
    c3_pass=1
    c3_msg="stop-requested NOT set"
elif [[ "$sr_rc" -eq 0 ]]; then
    c3_msg="stop-requested signal is SET (graceful stop in flight — re-run /stop to finalize)"
else
    c3_msg="stop-requested probe error (rc=$sr_rc — fail-open conservative)"
fi

# ─── Condition 4: no Tier-A registered background job ─────────────
MIND_AGENT="$agent" bash "$SCRIPT_DIR/background-jobs.sh" has-pending >/dev/null 2>&1
hp_rc=$?
c4_pass=0
if [[ "$hp_rc" -eq 1 ]]; then
    c4_pass=1
    c4_msg="no background-jobs pending"
elif [[ "$hp_rc" -eq 0 ]]; then
    c4_msg="background-jobs registered (Tier-A long-running tasks — collect/kill via background-jobs.yaml before recovery)"
else
    c4_msg="background-jobs probe error (rc=$hp_rc — fail-open conservative)"
fi

# Determine overall dead/alive — AND all 6 conditions
dead=1
for v in "$c1_pass" "$c2_pass" "$c25_pass" "$c27_pass" "$c3_pass" "$c4_pass"; do
    if [[ "$v" -eq 0 ]]; then
        dead=0
        break
    fi
done

# Emit JSON to stdout
# (Manual JSON construction — bash native, no jq dependency.)
_bool() { if [[ "$1" -eq 1 ]]; then echo "true"; else echo "false"; fi; }
_esc() { printf '%s' "$1" | python3 -c 'import sys,json; sys.stdout.write(json.dumps(sys.stdin.read()))' 2>/dev/null || printf '"%s"' "${1//\"/\\\"}"; }

cat <<JSON
{
  "agent": $(_esc "$agent"),
  "dead": $(_bool "$dead"),
  "conditions": {
    "state_running": $(_bool "$c1_pass"),
    "heartbeat_stale": $(_bool "$c2_pass"),
    "no_recent_block": $(_bool "$c25_pass"),
    "diary_stale": $(_bool "$c27_pass"),
    "no_stop_requested": $(_bool "$c3_pass"),
    "no_background_jobs": $(_bool "$c4_pass")
  },
  "messages": {
    "state_running": $(_esc "$c1_msg"),
    "heartbeat_stale": $(_esc "$c2_msg"),
    "no_recent_block": $(_esc "$c25_msg"),
    "diary_stale": $(_esc "$c27_msg"),
    "no_stop_requested": $(_esc "$c3_msg"),
    "no_background_jobs": $(_esc "$c4_msg")
  },
  "diary_age_min": $diary_age_min,
  "diary_stale_threshold_min": $diary_stale_min
}
JSON

# Emit stderr per-condition summary
if [[ "$dead" -eq 1 ]]; then
    verdict_label="DEAD"
else
    verdict_label="ALIVE"
fi
{
    echo "runner-dead-check: agent=$agent verdict=$verdict_label"
    echo "  [1]   $c1_msg"
    echo "  [2]   $c2_msg"
    echo "  [2.5] $c25_msg"
    echo "  [2.7] $c27_msg"
    echo "  [3]   $c3_msg"
    echo "  [4]   $c4_msg"
} >&2

if [[ "$dead" -eq 1 ]]; then
    exit 0
else
    exit 1
fi
