#!/usr/bin/env bash
# aspirations-precheck-budget-meter.sh — Magic Wand 2 ()
#
# Budget cap on aspirations-precheck wall-clock cost. Tracks elapsed-ms
# per sweep, drops deferrable sweeps when (zone == tight) OR cumulative
# elapsed exceeds budget_pct of iteration_budget_ms. Always-run sweeps
# NEVER drop. Decisions logged to <agent>/session/precheck-drops.jsonl.
#
# Operations:
#   start              — snapshot start time + zone, init state file
#   check <sweep-name> — print "run" or "drop" on stdout, log decision
#   end                — finalize, log summary record
#
# Sweep tier table (single source of truth — keep in sync with
# core/config/aspirations.yaml `precheck:` doc-block):
#
#   always-run:  tree-debt-gate, experience-archival-gate, fresh-eyes-code-gate
#   medium:      aspirations-recover-recurring, monitor-stale-check,
#                precheck-eval, blocker-recheck, defer-recheck
#   deferrable:  pending-questions-sweep, recurring-precondition-sweep,
#                parent-supersession-sweep, unblock-parent-status-sweep,
#                fresh-eyes-cadence, fresh-eyes-program-cadence,
#                felt-sense-cadence
#
# Unknown sweep names default to medium tier (conservative — less likely to
# get dropped). Add new sweeps here when introducing them.
#
# Fail-open at every layer: any error → "run" decision (never drop a sweep
# due to a meter bug). The meter's job is velocity optimization, not gating.
#
# Exit codes: 0 (always — fail-open). Decision is on stdout, not exit code.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_paths.sh"

OP="${1:-}"
SWEEP_NAME="${2:-}"

if [[ -z "$AGENT_DIR" ]]; then
    # No bound agent — fail open
    [[ "$OP" == "check" ]] && echo "run"
    exit 0
fi

STATE_FILE="$AGENT_DIR/session/precheck-budget-state.json"
DROP_LOG="$AGENT_DIR/session/precheck-drops.jsonl"

# ─────────────────────────── helpers ───────────────────────────

now_ms() {
    local result
    result=$(py -3 -c "import time; print(int(time.time() * 1000))" 2>/dev/null) || \
        result=$(python3 -c "import time; print(int(time.time() * 1000))" 2>/dev/null) || \
        result=""
    if [[ -z "$result" ]]; then
        echo "[budget-meter] WARN: now_ms() — both py -3 and python3 failed; meter elapsed will be 0 (fail-open broken)" >&2
        echo 0
    else
        echo "$result"
    fi
}

# tier lookup — case statement (POSIX, no associative arrays needed)
# SINGLE SOURCE OF TRUTH for sweep tiers. The SKILL.md tier table and the
# aspirations.yaml precheck doc-block are reflections — if they drift, the
# script wins. To add a new sweep, edit both this function AND those two
# documentation sites in the same change. Bravo fresh-eyes-code review
# (msg-20260510-045314) noted the drift risk; tracked by a verify-learning
# Section PB check that asserts the SKILL.md tier table matches.
sweep_tier() {
    case "$1" in
        tree-debt-gate|experience-archival-gate|fresh-eyes-code-gate)
            echo "always-run" ;;
        aspirations-recover-recurring|monitor-stale-check|precheck-eval|blocker-recheck|defer-recheck)
            echo "medium" ;;
        pending-questions-sweep|recurring-precondition-sweep|parent-supersession-sweep|unblock-parent-status-sweep|fresh-eyes-cadence|fresh-eyes-program-cadence|felt-sense-cadence)
            echo "deferrable" ;;
        *)
            # Unknown sweep name — surface to stderr so a missing registration
            # doesn't silently classify a new always-run sweep as droppable.
            echo "[budget-meter] WARN: unknown sweep '$1' classified as medium tier (default). Update sweep_tier() if this is a new always-run sweep." >&2
            echo "medium" ;;
    esac
}

# Read precheck config from aspirations.yaml. Returns "budget_pct iteration_budget_ms zone_drop_rules_json"
read_config() {
    # MUST use `py -3` not bare `python3` (rb-370/guard-335 — the Microsoft
    # Store stub returns non-zero on bare python3, the `|| echo` fallback
    # would mask the failure and silently use hardcoded defaults regardless
    # of aspirations.yaml. Found by bravo fresh-eyes-code review msg-20260510-045254.
    GID="$AGENT_DIR" py -3 - <<'PYEOF' 2>/dev/null || echo "15 60000 {}"
import os, sys, yaml, json
from pathlib import Path
agent_dir = Path(os.environ.get("GID", ""))
# Find core config relative to agent_dir → project root → core
# AGENT_DIR is PROJECT_ROOT/<agent>, so PROJECT_ROOT = parent
proj = agent_dir.parent
cfg_path = proj / "core" / "config" / "aspirations.yaml"
try:
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f) or {}
    pc = cfg.get("precheck", {}) or {}
    if not pc.get("enabled", True):
        # disabled — emit sentinel
        print("disabled")
        sys.exit(0)
    bp = int(pc.get("budget_pct", 15))
    ib = int(pc.get("iteration_budget_ms", 60000))
    zd = pc.get("zone_drop_rules", {}) or {}
    print(f"{bp} {ib} {json.dumps(zd)}")
except Exception:
    print("15 60000 {}")
PYEOF
}

# Read current zone from context-budget.json
read_zone() {
    local cb="$AGENT_DIR/session/context-budget.json"
    if [[ ! -f "$cb" ]]; then
        echo "fresh"
        return 0
    fi
    # Use env-var pattern (not shell substitution) so paths with apostrophes
    # or special chars don't break the python source. Found by bravo fresh-
    # eyes-code review msg-20260510-045339.
    CB_E="$cb" py -3 - <<'PYEOF' 2>/dev/null || echo "fresh"
import os, json
try:
    d = json.load(open(os.environ['CB_E']))
    print(d.get('zone', 'fresh'))
except Exception:
    print('fresh')
PYEOF
}

# Append a record to the drop-log. JSON line.
append_drop_log() {
    local record="$1"
    local logdir
    logdir="$(dirname "$DROP_LOG")"
    mkdir -p "$logdir" 2>/dev/null || true
    echo "$record" >> "$DROP_LOG" 2>/dev/null || true
}

# ─────────────────────────── operations ───────────────────────────

case "$OP" in
    start)
        cfg=$(read_config)
        if [[ "$cfg" == "disabled" ]]; then
            # Disabled — write sentinel state so check always returns "run"
            mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null || true
            echo '{"disabled": true}' > "$STATE_FILE"
            exit 0
        fi
        bp=$(echo "$cfg" | awk '{print $1}')
        ib=$(echo "$cfg" | awk '{print $2}')
        zd=$(echo "$cfg" | cut -d' ' -f3-)
        zone=$(read_zone)
        start_ms=$(now_ms)
        cap_ms=$(( (bp * ib) / 100 ))
        mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null || true
        # State JSON: tracks start time, cap, zone, decisions, last sweep end.
        # guard-165 — pass values via env vars and single-quote the python
        # source so bash never interpolates into untrusted data fields.
        # Was: py -3 -c "...$bp..." (interpolation).  fix.
        START_MS_E="$start_ms" CAP_MS_E="$cap_ms" BP_E="$bp" IB_E="$ib" \
        ZONE_E="$zone" ZD_E="$zd" \
        py -3 - <<'PYEOF' > "$STATE_FILE" 2>/dev/null || echo '{"disabled":true}' > "$STATE_FILE"
import json, os
state = {
    'disabled': False,
    'start_ms': int(os.environ['START_MS_E']),
    'last_check_ms': int(os.environ['START_MS_E']),
    'cap_ms': int(os.environ['CAP_MS_E']),
    'budget_pct': int(os.environ['BP_E']),
    'iteration_budget_ms': int(os.environ['IB_E']),
    'zone': os.environ['ZONE_E'],
    'zone_drop_rules': json.loads(os.environ['ZD_E']),
    'sweeps': []
}
print(json.dumps(state))
PYEOF
        ;;

    check)
        if [[ -z "$SWEEP_NAME" ]]; then
            echo "run"  # fail-open
            exit 0
        fi
        if [[ ! -f "$STATE_FILE" ]]; then
            echo "run"  # no state → fail-open
            exit 0
        fi
        tier=$(sweep_tier "$SWEEP_NAME")
        cur_ms=$(now_ms)
        # Decision logic in Python for atomic state update
        STATE_FILE_E="$STATE_FILE" DROP_LOG_E="$DROP_LOG" SWEEP_E="$SWEEP_NAME" TIER_E="$tier" CUR_E="$cur_ms" \
        py -3 - <<'PYEOF' 2>/dev/null || echo "run"
import os, json, sys
state_file = os.environ['STATE_FILE_E']
drop_log = os.environ['DROP_LOG_E']
sweep = os.environ['SWEEP_E']
tier = os.environ['TIER_E']
cur_ms = int(os.environ['CUR_E'])
try:
    with open(state_file) as f:
        state = json.load(f)
except Exception:
    print('run')
    sys.exit(0)
if state.get('disabled'):
    print('run')
    sys.exit(0)

elapsed = cur_ms - state.get('start_ms', cur_ms)
state['last_check_ms'] = cur_ms
zone = state.get('zone', 'fresh')
zone_rules = state.get('zone_drop_rules', {}) or {}
zone_drops = zone_rules.get(zone, []) or []
cap_ms = state.get('cap_ms', 9000)

decision = 'run'
reason = 'within-budget'

# Always-run NEVER drops
if tier == 'always-run':
    decision = 'run'
    reason = 'always-run-tier'
elif tier in zone_drops:
    decision = 'drop'
    reason = f'zone-drop:{zone}'
elif elapsed > cap_ms and tier == 'deferrable':
    decision = 'drop'
    reason = f'budget-exceeded:{elapsed}ms>{cap_ms}ms'

# Append to state.sweeps (list of {sweep, tier, decision, reason, elapsed_at_decision_ms})
state.setdefault('sweeps', []).append({
    'sweep': sweep,
    'tier': tier,
    'decision': decision,
    'reason': reason,
    'elapsed_ms': elapsed,
})

# Write state back
try:
    with open(state_file, 'w') as f:
        json.dump(state, f)
except Exception:
    pass

# If dropped, append immediately to drop log
if decision == 'drop':
    rec = {
        'ts': cur_ms,
        'sweep': sweep,
        'tier': tier,
        'reason': reason,
        'elapsed_ms': elapsed,
        'cap_ms': cap_ms,
        'zone': zone,
    }
    try:
        os.makedirs(os.path.dirname(drop_log), exist_ok=True)
    except Exception:
        pass
    try:
        with open(drop_log, 'a') as f:
            f.write(json.dumps(rec) + '\n')
    except Exception:
        pass

print(decision)
PYEOF
        ;;

    end)
        if [[ ! -f "$STATE_FILE" ]]; then
            exit 0
        fi
        cur_ms=$(now_ms)
        STATE_FILE_E="$STATE_FILE" DROP_LOG_E="$DROP_LOG" CUR_E="$cur_ms" \
        py -3 - <<'PYEOF' 2>/dev/null || true
import os, json
state_file = os.environ['STATE_FILE_E']
drop_log = os.environ['DROP_LOG_E']
cur_ms = int(os.environ['CUR_E'])
try:
    with open(state_file) as f:
        state = json.load(f)
except Exception:
    pass
else:
    if not state.get('disabled'):
        sweeps = state.get('sweeps', [])
        ran = sum(1 for s in sweeps if s.get('decision') == 'run')
        dropped = sum(1 for s in sweeps if s.get('decision') == 'drop')
        elapsed = cur_ms - state.get('start_ms', cur_ms)
        summary = {
            'ts': cur_ms,
            'event': 'precheck-end',
            'total_elapsed_ms': elapsed,
            'cap_ms': state.get('cap_ms'),
            'zone': state.get('zone'),
            'sweeps_ran': ran,
            'sweeps_dropped': dropped,
            'always_run_count': sum(1 for s in sweeps if s.get('tier') == 'always-run'),
        }
        try:
            os.makedirs(os.path.dirname(drop_log), exist_ok=True)
        except Exception:
            pass
        try:
            with open(drop_log, 'a') as f:
                f.write(json.dumps(summary) + '\n')
        except Exception:
            pass
# Clean up state file (one-shot per iteration)
try:
    os.unlink(state_file)
except Exception:
    pass
PYEOF
        ;;

    *)
        echo "usage: $0 {start|check <sweep-name>|end}" >&2
        exit 2
        ;;
esac

exit 0
