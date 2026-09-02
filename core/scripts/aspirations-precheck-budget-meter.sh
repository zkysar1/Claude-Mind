#!/usr/bin/env bash
# aspirations-precheck-budget-meter.sh — Magic Wand 2 ()
#
# Budget cap on aspirations-precheck. Drops deferrable sweeps ONLY when
# zone == tight (zone_drop_rules.tight=[deferrable]). Always-run sweeps
# NEVER drop. The former wall-clock "budget-exceeded" drop path was REMOVED
# ( — `elapsed` measured inter-tool-call LLM latency, not script
# cost, and dropped every deferrable sweep every iteration, starving the
# fresh-eyes/felt-sense/health-regression cadence rituals). elapsed-ms is
# still tracked per sweep for drop-log telemetry only. Decisions logged to
# <agent>/session/precheck-drops.jsonl.
#
# Operations:
#   start                 — snapshot start time + zone, init state file
#   check <sweep-name>    — print "run" or "drop" on stdout, log decision
#   executed <sweep-name> — record that the lane ACTUALLY RAN ()
#   end                   — finalize, log summary record
#
# `check` and `executed` measure DIFFERENT THINGS and the difference is the whole
# point of the second op. `check` records a PERMISSION DECISION — the caller asked
# and was told run/drop. It cannot observe whether the caller then did the work.
# So `sweeps_ran` conflates two states that a reader needs to tell apart:
#   (i)  the lane executed but never asked the meter (hand-run, or run by a
#        caller that skipped the check) -> contributes NOTHING to any counter
#   (ii) the lane was never invoked at all             -> contributes NOTHING
# Both render as an identical summary row, which is how 34 of 43 tier-table lanes
# went dark for 90-208h across two boxes while `sweeps_dropped: 0` read as healthy
# (: measured cc-04 dark 208h, cc-02 dark 94.3h, both fleet-confirmed).
# `executed` is emitted BY THE SCRIPT THAT RUNS THE LANE, never by an LLM reading
# a protocol file — guard-399's witness corollary: the observability of an
# LLM-elected step is itself LLM-elected, so it cannot witness the step.
#
# IT COUNTS INVOCATIONS, NOT LANES, and the two differ in practice: one registered
# lane may legitimately run more than once per iteration (aspirations-recover-recurring
# runs --source world AND --source agent under a single `check`). So a healthy row
# can read tail_executed=8 against 7 registered medium lanes. Do NOT "correct" that
# to a lane count — the question the field answers is "how much tail work actually
# happened", and collapsing two real invocations into one would understate it.
# Proven non-vacuous by the guard-5163 fixture pair in
# core/scripts/tests/test_precheck_medium_battery.py.
#
# Sweep tier table (single source of truth — keep in sync with
# core/config/aspirations.yaml `precheck:` doc-block):
#
#   always-run:  tree-debt-gate, experience-archival-gate, evolution-finalize-gate,
#                fresh-eyes-code-gate, dependency-timeout-check,
#                inbox-alert-age-check, handoff-aging-check,
#                directive-mix-check,
#                user-blocker-escalation-check
#                (the last two are the notification-age safety gates — escalate
#                aged unclaimed work to external parties, so they fire reliably;
#                . evolution-finalize-gate carries the guard-380
#                user-notification promise on material Self/Program edits — a
#                dropped sweep there means the user is never told their agent's
#                identity changed, so it can never be droppable; )
#                (dependency-timeout-check:  added it to the SKILL.md
#                tier table but NOT to this case arm, so it hit the WARN-default
#                `medium` and was DROPPABLE in a tight zone despite being declared
#                always-run; caught by test_budget_meter_sweep_tier_parity, the
#                allowlist-rot guard this drift class exists for. Fixed
#                independently on two boxes; this merge reconciles them and
#                restores the "last two" referent above, which BOTH fixes had
#                broken by appending into that pair.)
#                completed-not-closed-drain (0.5g.7): the per-iteration DRAIN
#                obligation () — bounded to a few rows, but an
#                obligation, so it must not be the first thing dropped.
#                world-script-crlf-check (0.5g.8): a *.sh carrying CR cannot
#                execute on Linux, and bash is its own parser so the GAE-2
#                CRLF-TOLERANT-PARSER answer cannot reach it. own-cloud delivers
#                CRLF into world/scripts today (positive-controlled 2026-08-24),
#                and .gitattributes does not reach a gitignored tree. The 2026-08-22
#                incident killed the fleet's outbound email transport silently for
#                17+ minutes; a dropped sweep here means the next one is silent too
#                (). Sub-second over ~800 files, zero false-positive class.
#   medium:      aspirations-recover-recurring, monitor-stale-check,
#                precheck-eval, blocker-recheck, defer-recheck,
#                precondition-defer-recheck, recurring-starvation-check
#                (recurring-starvation-check is deliberately NOT deferrable
#                despite sitting among the deferrable sweeps: it exists because
#                a 5-day recurring-lane blind spot went unnoticed, so it must
#                not be the first thing dropped; )
#   deferrable:  pending-questions-sweep, recurring-precondition-sweep,
#                parent-supersession-sweep, unblock-parent-status-sweep,
#                defer-drift-check, reason-less-blocked-check,
#                blocked-signal-resolution-check, dependency-cycle-check,
#                hypothesis-terminal-goal-check, locus-sweep,
#                self-blocked-defer-sweep, phantom-goal-audit,
#                hardcoded-scope-audit (0.5b.19/20/21 — added to the SKILL.md
#                table by  without a case arm, so they WARN-defaulted
#                to medium for the whole interval; caught by
#                test_budget_meter_sweep_tier_parity, 2026-08-27),
#                fresh-eyes-cadence, fresh-eyes-program-cadence,
#                strategic-scan-cadence,
#                felt-sense-cadence, l1-skew-cadence, scar-tissue-cadence,
#                completed-not-closed-cadence, health-regression-cadence,
#                curriculum-cadence, evolution-cadence
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
        tree-debt-gate|experience-archival-gate|evolution-finalize-gate|fresh-eyes-code-gate|dependency-timeout-check|inbox-alert-age-check|handoff-aging-check|user-blocker-escalation-check|directive-mix-check|completed-not-closed-drain|world-script-crlf-check|close-phase-skip-check)
            echo "always-run" ;;
        aspirations-recover-recurring|monitor-stale-check|precheck-eval|blocker-recheck|defer-recheck|precondition-defer-recheck|recurring-starvation-check)
            echo "medium" ;;
        pending-questions-sweep|recurring-precondition-sweep|parent-supersession-sweep|unblock-parent-status-sweep|routing-audit-target-status-sweep|credential-defer-recheck|defer-drift-check|reason-less-blocked-check|blocked-signal-resolution-check|dependency-cycle-check|hypothesis-terminal-goal-check|locus-sweep|reclaim-defer-audit|reclaim-user-participant-audit|human-blocked-defer-join|self-blocked-defer-sweep|phantom-goal-audit|hardcoded-scope-audit|closed-against-own-note-check|fresh-eyes-cadence|fresh-eyes-program-cadence|fresh-eyes-tree-cadence|strategic-scan-cadence|felt-sense-cadence|l1-skew-cadence|scar-tissue-cadence|completed-not-closed-cadence|health-regression-cadence|curriculum-cadence|evolution-cadence)
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
    GID="$AGENT_DIR" PROOT="$PROJECT_ROOT" py -3 - <<'PYEOF' 2>/dev/null || echo "15 60000 {}"
import os, sys, yaml, json
from pathlib import Path
# Resolve PROJECT_ROOT from the _paths.sh-forwarded PROOT (single source of
# truth — this script sources _paths.sh at the top, which sets PROJECT_ROOT).
# Fall back to agent_dir.parent.parent for the AGENTS_PARENT_DIR=agents layout
# (AGENT_DIR = PROJECT_ROOT/agents/<agent>). Pre-Phase-2.5.D this was a single
# agent_dir.parent (AGENT_DIR=PROJECT_ROOT/<agent>); the relocation moved the
# project root two levels up, silently 404'ing the config and pinning cap_ms to
# the 9000ms fallback regardless of the configured 90000ms ().
proot = os.environ.get("PROOT", "")
agent_dir = Path(os.environ.get("GID", ""))
proj = Path(proot) if proot else agent_dir.parent.parent
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
        # Increment the cross-iteration retrospection tracker ().
        # Persistent ACROSS iterations (NOT deleted on `end`, unlike STATE_FILE)
        # -- tracks a DISCRETE per-iteration counter (incremented once per
        # `meter start`) plus the iteration when a retrospection-class sweep last
        # ran. guard-784: a discrete counter, never wall-clock.
        TRACKER_FILE="$AGENT_DIR/session/precheck-retrospection-tracker.json"
        TRACKER_E="$TRACKER_FILE" py -3 - <<'PYEOF' 2>/dev/null || true
import os, json
tf = os.environ['TRACKER_E']
try:
    with open(tf) as f:
        t = json.load(f)
except Exception:
    t = {}
t['iter'] = int(t.get('iter', 0)) + 1
t.setdefault('last_retrospection_run_iter', 0)
try:
    with open(tf, 'w') as f:
        json.dump(t, f)
except Exception:
    pass
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
        SCRIPT_DIR_E="$SCRIPT_DIR" TRACKER_FILE_E="$AGENT_DIR/session/precheck-retrospection-tracker.json" \
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
# NOTE (): the former `elif elapsed > cap_ms and tier == 'deferrable'`
# wall-clock budget-drop path was REMOVED. `elapsed` is wall-clock since
# `meter start` (the FIRST precheck phase), dominated by inter-tool-call
# LLM+daemon latency BETWEEN sweeps, NOT script execution cost. Measured
# 130k-1400k ms vs the 90s cap across ALL six agents at zone=fresh, so it
# dropped EVERY deferrable sweep every iteration — permanently starving the
# fresh-eyes (25-goal), felt-sense (75-goal), and health-regression cadence
# rituals (they could never fire from precheck). A bash meter sampled only at
# discrete `check` points cannot separate script time from LLM latency, so the
# wall-clock proxy is unfixable in-place; a correct script-time meter would
# essentially never drop (sweeps are sub-second) anyway. The zone-drop path
# above is the correct, sufficient protection: drop deferrables only under
# context-tight (zone_drop_rules.tight=[deferrable]). `elapsed`/`cap_ms` are
# retained below for drop-log telemetry only.

# Retrospection-budget reservation (, bravo session-66 #4): override a
# tight-zone DROP of a retrospection-class sweep to RUN when no retrospection
# sweep has run in >= threshold iterations -- so the sweeps that catch boxed-in
# patterns are NOT the first throttled under context pressure. Single-sourced in
# _precheck_budget_reserve.reserve_decision (do NOT inline+duplicate the logic
# here AND in the test -- that is the  duplicate-allowlist rot class).
# guard-784: keyed on a DISCRETE iteration counter (the persistent tracker),
# never wall-clock. Fail-open: any import/IO error leaves the base decision intact.
try:
    import sys as _sys
    _sys.path.insert(0, os.environ.get('SCRIPT_DIR_E', ''))
    from _precheck_budget_reserve import reserve_decision
    _tracker_file = os.environ.get('TRACKER_FILE_E', '')
    try:
        with open(_tracker_file) as _tf:
            _tr = json.load(_tf)
    except Exception:
        _tr = {}
    _cur_iter = int(_tr.get('iter', 0))
    _last_retro = int(_tr.get('last_retrospection_run_iter', 0))
    decision, reason, _new_last = reserve_decision(
        decision, reason, sweep, _cur_iter, _last_retro)
    if _new_last != _last_retro:
        _tr['last_retrospection_run_iter'] = _new_last
        try:
            with open(_tracker_file, 'w') as _tf:
                json.dump(_tr, _tf)
        except Exception:
            pass
except Exception:
    pass  # fail-open -- the reservation must never break the meter

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

    executed)
        # Record that a lane ACTUALLY RAN. No decision logic, no drop-log write:
        # this op never gates anything, it only witnesses. Fail-open like every
        # other arm — a meter bug must never break the caller that just did real
        # work (and unlike `check`, there is nothing to fall back TO: the work is
        # already done, so the only correct failure is a silent one).
        if [[ -z "$SWEEP_NAME" ]] || [[ ! -f "$STATE_FILE" ]]; then
            exit 0
        fi
        tier=$(sweep_tier "$SWEEP_NAME")
        STATE_FILE_E="$STATE_FILE" SWEEP_E="$SWEEP_NAME" TIER_E="$tier" \
        py -3 - <<'PYEOF' 2>/dev/null || true
import os, json
state_file = os.environ['STATE_FILE_E']
try:
    with open(state_file) as f:
        state = json.load(f)
except Exception:
    raise SystemExit(0)
if state.get('disabled'):
    raise SystemExit(0)
state.setdefault('sweeps', []).append({
    'sweep': os.environ['SWEEP_E'],
    'tier': os.environ['TIER_E'],
    'decision': 'executed',
    'reason': 'lane-completed',
})
try:
    with open(state_file, 'w') as f:
        json.dump(state, f)
except Exception:
    pass
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
        # : EXECUTION, not permission. `ran` counts run DECISIONS and
        # is left exactly as it was — a reader comparing old rows to new ones must
        # see the same quantity. These two are additive and answer the question
        # `ran` cannot: did the medium/deferrable tail actually happen?
        executed = [s for s in sweeps if s.get('decision') == 'executed']
        tail_executed = sum(1 for s in executed if s.get('tier') != 'always-run')
        elapsed = cur_ms - state.get('start_ms', cur_ms)
        # : sweeps_dropped==0 is AMBIGUOUS on its own -- it reads
        # identically whether the medium/deferrable tail ran and fit the budget,
        # or was never invoked at all. tail_considered counts the non-always-run
        # sweeps that ASKED this meter for permission; zero means nothing ever
        # asked, which is a routing gap, not a healthy budget.
        #
        # SCOPED TO DECISION RECORDS ('run'/'drop') WHEN THE TWO SIDES OF
        #  WERE MERGED (), and the scoping is load-bearing,
        # not cosmetic. The sibling implementation added an `executed` op that
        # appends MORE rows to this same `sweeps` list, so the original
        # `len(sweeps) - always` would count one medium lane TWICE -- once when it
        # asked, once when it reported completion -- inflating a metric whose only
        # job is to separate zero from non-zero. Keeping the two orthogonal is why
        # both exist: tail_considered answers "did the tail ASK", tail_executed
        # answers "did the tail RUN", and a lane can do either without the other
        # (that asymmetry is exactly what the `executed` op was added to expose).
        always_checked = sum(1 for s in sweeps
                             if s.get('tier') == 'always-run'
                             and s.get('decision') in ('run', 'drop'))
        tail_considered = sum(1 for s in sweeps
                              if s.get('tier') != 'always-run'
                              and s.get('decision') in ('run', 'drop'))
        summary = {
            'ts': cur_ms,
            'event': 'precheck-end',
            'total_elapsed_ms': elapsed,
            'cap_ms': state.get('cap_ms'),
            'zone': state.get('zone'),
            'sweeps_ran': ran,
            'sweeps_dropped': dropped,
            # UNION of both  implementations (merged ).
            # They are complementary, not duplicates: the *_considered/reached
            # pair measures PERMISSION (did the tail ask), the *_executed pair
            # measures EXECUTION (did the tail run). always_run_count keeps the
            # sibling's unscoped expression verbatim so old rows stay comparable.
            'always_run_count': sum(1 for s in sweeps if s.get('tier') == 'always-run'),
            'tail_considered': tail_considered,
            'tail_reached': tail_considered > 0,
            'sweeps_executed': len(executed),
            'tail_executed': tail_executed,
        }
        if tail_considered == 0:
            # stdout, not stderr: the heredoc runs under 2>/dev/null, so a
            # stderr warning here would be discarded -- which is exactly how
            # this gap stayed invisible.
            print('[precheck-meter] TAIL NEVER INVOKED: %d always-run sweeps '
                  'reached the meter and ZERO medium/deferrable ones did. '
                  'sweeps_dropped=%d does NOT mean the budget was fine -- it '
                  'means nothing asked. The medium tier is wired into loop entry '
                  'via precheck-medium-battery; if this fires, that battery did '
                  'not run either. Check it, then resume aspirations-precheck at '
                  'its first deferrable sweep (g-115-7847).'
                  % (always_checked, dropped))
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
        echo "usage: $0 {start|check <sweep-name>|executed <sweep-name>|end}" >&2
        exit 2
        ;;
esac

exit 0
