#!/usr/bin/env bash
# Regression tests for cleanup-stale-bindings.sh Signal 0 (residency gate, ).
#
# THE DEFECT: the Phase-2.6 sweep iterates EVERY agent dir, but both liveness
# signals in _should_delete_binding read machine_local files under
# agents/<name>/session/ (running-session-id, runner-heartbeat). Those never
# sync, so for an agent this box does NOT run they are absent BY DESIGN — and
# both absences advanced the predicate toward DELETE, which `rm -rf`s that
# agent's whole per-session dir (binding + iteration-checkpoint + scratch).
#
# Signal 0 refuses when the agent owns no local-paths.conf on this box.
# Case 1 fails if Signal 0 is removed; cases 2-5 fail if it is applied too
# broadly. Both directions are pinned deliberately — a gate that only ever
# says "keep" would pass case 1 while silently disabling the whole sweep.
#
# Tests run in an isolated sandbox under /tmp; do NOT modify project state.
set -uo pipefail

PROJECT_ROOT_REAL="$(cd "$(dirname "$0")/../../.." && pwd)"
SANDBOX="$(mktemp -d)"
trap "rm -rf '$SANDBOX'" EXIT

mkdir -p "$SANDBOX/core/scripts" "$SANDBOX/core/config"
for f in cleanup-stale-bindings.sh heartbeat-stale.sh _paths.sh _paths.py _platform.sh; do
    cp "$PROJECT_ROOT_REAL/core/scripts/$f" "$SANDBOX/core/scripts/"
done
# heartbeat-stale.sh reads runner_heartbeat.stale_minutes from this file and
# exits 1 (empty stdout) when the block is missing — copy the real one so the
# sandbox exercises the real threshold rather than a fixture's.
cp "$PROJECT_ROOT_REAL/core/config/aspirations.yaml" "$SANDBOX/core/config/"

PASS=0
FAIL=0

assert_equals() {
    local label="$1" actual="$2" expected="$3"
    if [ "$actual" = "$expected" ]; then
        echo "  PASS: $label"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $label  actual=[$actual]  expected=[$expected]"
        FAIL=$((FAIL + 1))
    fi
}

reset_sandbox() {
    rm -rf "$SANDBOX/agents"
    rm -f "$SANDBOX"/.active-agent-*
    mkdir -p "$SANDBOX/agents"
}

# Make an agent dir. $2=resident means give it a local-paths.conf (the marker
# /start writes for an agent this box actually runs).
make_agent() {
    local name="$1" resident="$2"
    mkdir -p "$SANDBOX/agents/$name/session"
    if [ "$resident" = "resident" ]; then
        {
            echo "WORLD_PATH=$SANDBOX/world"
            echo "META_PATH=$SANDBOX/meta"
        } > "$SANDBOX/agents/$name/local-paths.conf"
    fi
}

# Per-session dir with a binding.yaml backdated past the 24h signal-1 gate.
make_session_dir() {
    local name="$1" sid="$2"
    mkdir -p "$SANDBOX/agents/$name/sessions/$sid"
    printf 'agent: %s\nmode: autonomous\n' "$name" \
        > "$SANDBOX/agents/$name/sessions/$sid/binding.yaml"
    touch -d '3 days ago' "$SANDBOX/agents/$name/sessions/$sid/binding.yaml"
}

# A CLOSED-BODY ORPHAN (): the binding is gone, the other per-session
# artifacts remain. $3=summary gives it the session-summary.yaml that only a
# session-CLOSE writer produces; $3=nosummary is the abrupt-death shape, which
# must stay out of the sweep. Everything is backdated past the 24h activity gate
# so signal 1 is not what decides these cases.
make_orphan_session_dir() {
    local name="$1" sid="$2" summary="$3" d
    d="$SANDBOX/agents/$name/sessions/$sid"
    mkdir -p "$d"
    printf 'role: reducer\nbody_state: active\n' > "$d/body-manifest.yaml"
    [ "$summary" = "summary" ] && printf 'goals_completed: 3\n' > "$d/session-summary.yaml"
    find "$d" -type f -exec touch -d '3 days ago' {} +
}

# Signals 2 and 3 both set to their DELETE-advancing values, so nothing but the
# admission check and signal 1 stands between the dir and removal.
make_delete_advancing() {
    local name="$1"
    printf 'some-other-sid' > "$SANDBOX/agents/$name/session/running-session-id"
    printf '' > "$SANDBOX/agents/$name/session/runner-heartbeat"
    touch -d '3 days ago' "$SANDBOX/agents/$name/session/runner-heartbeat"
}

run_sweep() {
    ( cd "$SANDBOX" && bash "$SANDBOX/core/scripts/cleanup-stale-bindings.sh" >/dev/null 2>&1 )
}

exists() { [ -e "$1" ] && echo yes || echo no; }

mkdir -p "$SANDBOX/world" "$SANDBOX/meta"

echo "Scenario 1: NON-RESIDENT agent, no running-session-id, no heartbeat"
echo "  (the defect: two absent-by-design files used to read as DELETE)"
reset_sandbox
make_agent foreignagent nonresident
make_session_dir foreignagent sid-foreign-001
run_sweep
assert_equals "non-resident session dir SURVIVES" \
    "$(exists "$SANDBOX/agents/foreignagent/sessions/sid-foreign-001")" "yes"

echo "Scenario 2: RESIDENT agent, running-session-id PRESENT but MISMATCHED, heartbeat stale"
echo "  (the answered branch — must still sweep exactly as before)"
reset_sandbox
make_agent localagent resident
make_session_dir localagent sid-local-002
printf 'some-other-sid' > "$SANDBOX/agents/localagent/session/running-session-id"
printf '' > "$SANDBOX/agents/localagent/session/runner-heartbeat"
touch -d '3 days ago' "$SANDBOX/agents/localagent/session/runner-heartbeat"
run_sweep
assert_equals "resident + answered-mismatch session dir IS SWEPT" \
    "$(exists "$SANDBOX/agents/localagent/sessions/sid-local-002")" "no"

echo "Scenario 3: RESIDENT agent, running-session-id MATCHES the bound SID"
echo "  (signal 2 keep — unchanged by Signal 0)"
reset_sandbox
make_agent localagent resident
make_session_dir localagent sid-local-003
printf 'sid-local-003' > "$SANDBOX/agents/localagent/session/running-session-id"
printf '' > "$SANDBOX/agents/localagent/session/runner-heartbeat"
touch -d '3 days ago' "$SANDBOX/agents/localagent/session/runner-heartbeat"
run_sweep
assert_equals "resident + matching SID session dir SURVIVES" \
    "$(exists "$SANDBOX/agents/localagent/sessions/sid-local-003")" "yes"

echo "Scenario 4: RESIDENT agent, heartbeat FRESH"
echo "  (signal 3 keep — unchanged by Signal 0)"
reset_sandbox
make_agent localagent resident
make_session_dir localagent sid-local-004
printf 'some-other-sid' > "$SANDBOX/agents/localagent/session/running-session-id"
printf '' > "$SANDBOX/agents/localagent/session/runner-heartbeat"
run_sweep
assert_equals "resident + fresh heartbeat session dir SURVIVES" \
    "$(exists "$SANDBOX/agents/localagent/sessions/sid-local-004")" "yes"

echo "Scenario 5: LEGACY .active-agent-<SID> for a NON-RESIDENT agent"
echo "  (both sweep loops share _should_delete_binding — the fix must cover both)"
reset_sandbox
make_agent foreignagent nonresident
printf 'foreignagent' > "$SANDBOX/.active-agent-sid-legacy-005"
touch -d '3 days ago' "$SANDBOX/.active-agent-sid-legacy-005"
run_sweep
assert_equals "non-resident legacy binding file SURVIVES" \
    "$(exists "$SANDBOX/.active-agent-sid-legacy-005")" "yes"

echo "Scenario 6: RESIDENT agent, stale binding.yaml but FRESH scratch in the session dir"
echo "  (Signal 1 is PER-SESSION ACTIVITY, not session START time — g-306-153)"
# The co-residency defect: an observer session emits neither signal 2 nor
# signal 3, so mtime is its only protection — and binding.yaml is written ONCE
# by /start, so that mtime is when the session BEGAN. Past 24h a working
# observer session read as stale and a co-resident partner's sweep rm -rf'd it.
# Signals 2 and 3 are both set to their DELETE-advancing values here, so
# binding.yaml mtime is the only thing standing between this dir and removal —
# exactly the observer-mode shape. This case FAILS against the pre-fix
# predicate (which looked at binding.yaml alone and swept).
reset_sandbox
make_agent localagent resident
make_session_dir localagent sid-local-006
printf 'some-other-sid' > "$SANDBOX/agents/localagent/session/running-session-id"
printf '' > "$SANDBOX/agents/localagent/session/runner-heartbeat"
touch -d '3 days ago' "$SANDBOX/agents/localagent/session/runner-heartbeat"
# The session is alive and working: sanctioned per-session scratch, written now.
printf 'scratch\n' > "$SANDBOX/agents/localagent/sessions/sid-local-006/scratch.txt"
run_sweep
assert_equals "resident + stale binding but fresh session-dir file SURVIVES" \
    "$(exists "$SANDBOX/agents/localagent/sessions/sid-local-006")" "yes"

echo "Scenario 7: same shape, but EVERY file in the session dir is stale"
echo "  (the keep must come from ACTIVITY — a dir with no fresh file is still swept)"
# Guards the opposite direction: scenario 6 must not pass merely because the
# predicate went permissive. `-type f` is load-bearing here — the session DIR's
# own mtime is refreshed by mkdir/entry-creation at fixture time, so a check
# that counted directories would keep this too and the sweep would be inert.
reset_sandbox
make_agent localagent resident
make_session_dir localagent sid-local-007
printf 'some-other-sid' > "$SANDBOX/agents/localagent/session/running-session-id"
printf '' > "$SANDBOX/agents/localagent/session/runner-heartbeat"
touch -d '3 days ago' "$SANDBOX/agents/localagent/session/runner-heartbeat"
printf 'scratch\n' > "$SANDBOX/agents/localagent/sessions/sid-local-007/scratch.txt"
touch -d '3 days ago' "$SANDBOX/agents/localagent/sessions/sid-local-007/scratch.txt"
run_sweep
assert_equals "resident + all session-dir files stale IS SWEPT" \
    "$(exists "$SANDBOX/agents/localagent/sessions/sid-local-007")" "no"

INERT_FLAG="$SANDBOX/core/logs/stale-binding-sweep-inert"

# Captures stderr, which run_sweep discards. Both production callers invoke the
# script as `... 2>/dev/null || true`, so stderr is the HAND-RUN channel only —
# scenario 10 asserts the flag FILE for exactly that reason.
run_sweep_capture() {
    ( cd "$SANDBOX" && bash "$SANDBOX/core/scripts/cleanup-stale-bindings.sh" 2>&1 >/dev/null )
}

echo "Scenario 8: RESIDENT agent whose local-paths.conf was renamed to .bak"
echo "  (the g-306-154 defect: migrate-to-mind-data.sh does this to EVERY agent by default)"
# FAILS against the pre-fix predicate: with the conf renamed aside, Signal 0
# refused, the dir survived, and the script still exited 0 printing nothing.
# session/agent-state is the surviving marker — machine_local, written by /start
# in every mode.
reset_sandbox
make_agent localagent resident
mv "$SANDBOX/agents/localagent/local-paths.conf" \
   "$SANDBOX/agents/localagent/local-paths.conf.bak"
printf 'IDLE' > "$SANDBOX/agents/localagent/session/agent-state"
make_session_dir localagent sid-local-008
printf 'some-other-sid' > "$SANDBOX/agents/localagent/session/running-session-id"
printf '' > "$SANDBOX/agents/localagent/session/runner-heartbeat"
touch -d '3 days ago' "$SANDBOX/agents/localagent/session/runner-heartbeat"
run_sweep
assert_equals "conf renamed to .bak + agent-state present IS SWEPT" \
    "$(exists "$SANDBOX/agents/localagent/sessions/sid-local-008")" "no"

echo "Scenario 9: RESIDENT agent that never had a conf at all (.mind-data storage)"
echo "  (measured on cc-04: bravo, resident and in_flight, carries no conf and zero .bak files exist)"
# The population is WIDER than the migration: /start under self-contained
# .mind-data storage never writes a conf in the first place, so this shape needs
# no rename to occur. agent-mode alone must be sufficient.
reset_sandbox
make_agent localagent nonresident
printf 'autonomous' > "$SANDBOX/agents/localagent/session/agent-mode"
make_session_dir localagent sid-local-009
printf 'some-other-sid' > "$SANDBOX/agents/localagent/session/running-session-id"
printf '' > "$SANDBOX/agents/localagent/session/runner-heartbeat"
touch -d '3 days ago' "$SANDBOX/agents/localagent/session/runner-heartbeat"
run_sweep
assert_equals "no conf ever + agent-mode present IS SWEPT" \
    "$(exists "$SANDBOX/agents/localagent/sessions/sid-local-009")" "no"

echo "Scenario 10: ZERO agents on the box carry any residency marker"
echo "  (structural inertness must be OBSERVABLE, not identical to nothing-to-sweep)"
# Scenario 1 already pins that a marker-less agent SURVIVES. This pins that the
# survival is now announced: pre-fix, 'inert' and 'idle' were both exit 0 with
# no output, which is what let the regression hide for a full promotion cycle.
reset_sandbox
rm -f "$INERT_FLAG"
make_agent foreignagent nonresident
make_session_dir foreignagent sid-foreign-010
STDERR="$(run_sweep_capture)"
assert_equals "inert flag file WRITTEN when zero agents qualify" \
    "$(exists "$INERT_FLAG")" "yes"
case "$STDERR" in
    *INERT*) assert_equals "stderr announces INERT (hand-run channel)" "yes" "yes" ;;
    *)       assert_equals "stderr announces INERT (hand-run channel)" "no"  "yes" ;;
esac
assert_equals "and the marker-less dir still SURVIVES (refusal unchanged)" \
    "$(exists "$SANDBOX/agents/foreignagent/sessions/sid-foreign-010")" "yes"

echo "Scenario 11: at least one agent qualifies — the inert flag is CLEARED"
echo "  (self-healing: a stale flag would misreport a healthy box as disabled)"
reset_sandbox
mkdir -p "$SANDBOX/core/logs"
printf 'stale-from-a-previous-run\n' > "$INERT_FLAG"
make_agent localagent resident
make_session_dir localagent sid-local-011
printf 'sid-local-011' > "$SANDBOX/agents/localagent/session/running-session-id"
run_sweep
assert_equals "inert flag REMOVED once an agent qualifies" \
    "$(exists "$INERT_FLAG")" "no"

echo "Scenario 12: CLOSED-BODY ORPHAN — binding.yaml gone, session-summary.yaml present"
echo "  (the g-306-236 defect: the binding existence test ran BEFORE the predicate)"
# FAILS against the pre-fix admission check, which was `[ -f binding.yaml ] ||
# continue` — so a dir in this shape never reached the predicate and was skipped
# on EVERY sweep, permanently. Measured live on cc-04: 29 days (702h) with all
# three reap signals firing the whole time. session-summary.yaml is written ONLY
# at session close (graceful-stop D6.5 and the zak-code SessionEnd hook), so its
# presence alongside a missing binding is proof the Body ran to a clean exit.
reset_sandbox
make_agent localagent resident
make_orphan_session_dir localagent sid-orphan-012 summary
make_delete_advancing localagent
run_sweep
assert_equals "binding-less dir WITH session-summary IS SWEPT" \
    "$(exists "$SANDBOX/agents/localagent/sessions/sid-orphan-012")" "no"

echo "Scenario 13: same orphan shape, but a FRESH file in the session dir"
echo "  (signal 1 still applies to admitted dirs — admission is not a bypass)"
# The load-bearing negative for scenario 12. Without it, widening the admission
# to `[ -d ]` would pass 12 just as well while deleting live session dirs — the
# exact over-sweep the header's risk asymmetry forbids.
reset_sandbox
make_agent localagent resident
make_orphan_session_dir localagent sid-orphan-013 summary
printf 'scratch\n' > "$SANDBOX/agents/localagent/sessions/sid-orphan-013/scratch.txt"
make_delete_advancing localagent
run_sweep
assert_equals "binding-less dir with a FRESH file SURVIVES" \
    "$(exists "$SANDBOX/agents/localagent/sessions/sid-orphan-013")" "yes"

echo "Scenario 14: binding-less dir with NO session-summary.yaml (abrupt-death shape)"
echo "  (pins the SCOPE of the widening — this was deliberately left out)"
# Everything here is identical to scenario 12 except the closure proof, so the
# pair isolates exactly one variable. A future 'simplification' to admit every
# binding-less dir would pass 12 and 13 and fail only here.
reset_sandbox
make_agent localagent resident
make_orphan_session_dir localagent sid-orphan-014 nosummary
make_delete_advancing localagent
run_sweep
assert_equals "binding-less dir WITHOUT session-summary SURVIVES" \
    "$(exists "$SANDBOX/agents/localagent/sessions/sid-orphan-014")" "yes"

echo "Scenario 15: closed-body orphan belonging to a NON-RESIDENT agent"
echo "  (admission must not leak past Signal 0 — the g-306-146 refusal still rules)"
# The widened admission decides which dirs reach the predicate; Signal 0 decides
# whether the liveness question is answerable at all. A foreign agent's orphan
# must still survive, because this box cannot answer it.
reset_sandbox
make_agent foreignagent nonresident
make_agent localagent resident   # so the box is not INERT — isolates Signal 0
make_orphan_session_dir foreignagent sid-orphan-015 summary
run_sweep
assert_equals "non-resident closed-body orphan SURVIVES" \
    "$(exists "$SANDBOX/agents/foreignagent/sessions/sid-orphan-015")" "yes"

# verify (c) — "the binding.yaml-present path is unchanged" — is already pinned
# in BOTH directions by scenarios 2/8/9 (present + answered => swept) and
# 3/4/6 (present + any live signal => survives). Not re-asserted here; a fourth
# copy of an existing pin adds no discriminating power.

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
