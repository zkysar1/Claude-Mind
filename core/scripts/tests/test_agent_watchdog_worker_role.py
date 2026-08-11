"""agent-watchdog worker-Body role filtering ().

Until this landed, `agent-watchdog.py --tick` had exactly ONE caller in the
tree — iteration-close.sh:2554 — and the worker loop deliberately skips
iteration-close. So no watchdog probe had ever executed on a worker box. The
fix is a wiring change plus a role filter, and the filter is the part that
needs pinning: five of the ten probes read reducer-shaped state that a worker
deliberately does not have, so enabling the whole set would install probes that
are structurally incapable of firing — coverage in appearance only.

The load-bearing test here is `test_healthy_worker_tick_emits_no_critical_event`
(verify criterion (c)). A worker is IDLE+autonomous by design, and that exact
tuple has already fooled two other gates; a filter that let HeartbeatProbe run
would report a healthy worker as dead on every single tick.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from _paths import PROJECT_ROOT
except Exception:
    PROJECT_ROOT = Path(__file__).resolve().parents[3]

WD_PATH = PROJECT_ROOT / "core" / "scripts" / "agent-watchdog.py"


def _load():
    spec = importlib.util.spec_from_file_location("agent_watchdog_under_test", WD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wd = _load()


def _ctx(role, agent_dir):
    return wd.WatchdogContext(
        agent_name="testagent",
        agent_dir=Path(agent_dir),
        project_root_path=PROJECT_ROOT,
        body_role=role,
    )


# Every probe build_probes registers on a reducer.
#
# EXHAUSTIVE BY DESIGN — do not relax this to a count or a floor. A new probe
# MUST fail these tests, because "can a worker Body run this?" is a decision
# someone has to make deliberately; a self-updating assertion would let a
# reducer-shaped probe reach workers silently, which is the whole defect
#  exists to prevent.
#
# It has already earned that TWICE. WorkerStallProbe landed from the peer-side half
# of this same goal and reddened three tests here, forcing the classification below.
# ClaimHeartbeatProbe () then reddened the same three, and was classified
# reducer-shaped: its marker is written by heartbeat-tick.sh only when renewal of the
# cross-machine runner CLAIM lease fails, and a worker Body never holds that lease —
# so on a worker the marker is always absent and the probe is inert by construction.
# Note that probe shipped WITH a passing worker-inertness test of its own; that test
# asserted the worker side and said nothing about the reducer roster, which is exactly
# the gap this exhaustive pin covers.
ALL_REDUCER_PROBES = {
    "worker-stall", "running-sid", "heartbeat", "stalled", "background-job",
    "stop-hook-block", "daemon-health", "clock-skew", "freshness",
    "mirror-wedge", "memory-headroom", "claim-heartbeat",
}

# Excluded from workers because they read REDUCER-SHAPED STATE a worker
# deliberately does not keep: agent-state RUNNING, running-session-id,
# runner-heartbeat, the cross-machine runner CLAIM lease. On a worker these
# cannot fire, or fire falsely.
EXCLUDED_REDUCER_SHAPED = {
    "running-sid", "heartbeat", "stalled", "background-job", "stop-hook-block",
    "claim-heartbeat",
}

# Excluded for a DIFFERENT reason, kept separate so the distinction survives.
# worker-stall is the PEER-side probe: it watches OTHER boxes and belongs on the
# reducer. It is not "reducer-shaped state" — it would run fine on a worker and
# still be wrong there, because the faults it catches (process death, lost auth)
# kill the in-loop tick along with the loop. Folding it into the set above would
# lose that, and the next reader would think it was excluded for state reasons.
EXCLUDED_PEER_SIDE = {"worker-stall"}


# ---------------------------------------------------------------------------
# Role detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "env,expected",
    [
        ({"BODY_ROLE": "worker"}, True),
        ({"BODY_ROLE": "WORKER"}, True),      # hook writes lowercase; be tolerant
        ({"BODY_ROLE": " worker "}, True),
        ({"BODY_ROLE": "reducer"}, False),
        ({"BODY_ROLE": ""}, False),
        ({}, False),                          # absent => reducer, the safe default
    ],
)
def test_is_worker_body_reads_body_role(env, expected):
    assert wd.is_worker_body(env) is expected


def test_default_role_is_reducer(tmp_path):
    """An unset role must not silently narrow the reducer's probe set.

    Asserted as "default set == explicit-reducer set" rather than against a
    literal count: the invariant is that omitting the role changes NOTHING, and
    stating it that way keeps this test about the default while
    ALL_REDUCER_PROBES carries the roster.
    """
    ctx = wd.WatchdogContext(
        agent_name="a", agent_dir=tmp_path, project_root_path=PROJECT_ROOT
    )
    assert ctx.body_role == "reducer"
    default_names = {p.name for p in wd.build_probes(ctx)}
    explicit_names = {p.name for p in wd.build_probes(_ctx("reducer", tmp_path))}
    assert default_names == explicit_names
    assert default_names == ALL_REDUCER_PROBES


# ---------------------------------------------------------------------------
# The filter
# ---------------------------------------------------------------------------

def test_worker_registers_exactly_the_box_level_probes(tmp_path):
    names = {p.name for p in wd.build_probes(_ctx("worker", tmp_path))}
    assert names == set(wd.WORKER_SAFE_PROBES)
    assert len(names) == 5


def test_reducer_still_registers_every_probe(tmp_path):
    """The filter must not change the reducer path — this is the regression half."""
    names = {p.name for p in wd.build_probes(_ctx("reducer", tmp_path))}
    assert names == ALL_REDUCER_PROBES
    assert set(wd.WORKER_SAFE_PROBES) < names


def test_worker_safe_probe_names_all_exist(tmp_path):
    """Every name in the allowlist must resolve to a real probe.

    This exists because the first draft wrote the names with UNDERSCORES while
    Probe.name uses HYPHENS. Only "freshness" matched, so a worker silently
    registered 1 probe instead of 5 — and the wiring looked entirely correct
    from the call site. A typo'd allowlist and a working one are
    indistinguishable without this assertion.
    """
    real = {p.name for p in wd.build_probes(_ctx("reducer", tmp_path))}
    unknown = set(wd.WORKER_SAFE_PROBES) - real
    assert unknown == set(), f"allowlist names match no probe: {sorted(unknown)}"


def test_excluded_probes_are_accounted_for_by_reason(tmp_path):
    """Every exclusion must have a STATED reason, and the two reasons differ.

    Asserting the union alone would let a future probe be dropped from workers
    for no recorded reason — the exclusion list would still balance. Splitting
    it forces each new exclusion into one of the two buckets, or into a third
    one someone has to name.
    """
    all_names = {p.name for p in wd.build_probes(_ctx("reducer", tmp_path))}
    excluded = all_names - set(wd.WORKER_SAFE_PROBES)
    assert excluded == EXCLUDED_REDUCER_SHAPED | EXCLUDED_PEER_SIDE
    # The buckets are genuinely disjoint — a probe excluded for both reasons
    # would mean one of the two rationales is wrong about it.
    assert EXCLUDED_REDUCER_SHAPED & EXCLUDED_PEER_SIDE == set()


def test_peer_side_probe_is_not_offered_to_workers(tmp_path):
    """worker-stall must never register on a worker.

    Pinned separately from the accounting test above because this is the
    behavioural claim: a worker running the peer-side stall probe would be
    watching itself with a detector whose premise is out-of-process
    observation, and it would look like coverage.
    """
    worker_names = {p.name for p in wd.build_probes(_ctx("worker", tmp_path))}
    assert EXCLUDED_PEER_SIDE.isdisjoint(worker_names)
    # Positive control: it IS registered on the reducer, so the assertion above
    # is about the filter and not about the probe having quietly disappeared.
    reducer_names = {p.name for p in wd.build_probes(_ctx("reducer", tmp_path))}
    assert EXCLUDED_PEER_SIDE <= reducer_names


# ---------------------------------------------------------------------------
# WHY those five are excluded — pinned, not asserted in prose only
# ---------------------------------------------------------------------------

def test_classify_stalled_is_dead_on_worker_shape():
    """A worker is IDLE by design, so the stall classifier returns None.

    Both guards fail independently, which is why StalledProbe is excluded
    rather than taught the worker shape: even given a RUNNING state it would
    read `runner-heartbeat`, which a worker never writes.
    """
    # IDLE + perfectly fresh heartbeat + very stale diary — the shape that WOULD
    # be a wedge on a reducer. On a worker it must not classify at all.
    assert wd.classify_stalled("IDLE", 5.0, 99999.0, 600.0, 3600.0) is None
    # And with no heartbeat file at all (hb age None), which is the real worker case.
    assert wd.classify_stalled("IDLE", None, 99999.0, 600.0, 3600.0) is None
    # Positive control: the same inputs on a RUNNING reducer DO classify, so the
    # None above is the worker shape and not a broken classifier.
    assert wd.classify_stalled("RUNNING", 5.0, 99999.0, 600.0, 3600.0) == "stalled"


# ---------------------------------------------------------------------------
# Verify criterion (c) — the load-bearing negative
# ---------------------------------------------------------------------------

def _worker_shaped_tree(tmp_path):
    """IDLE + autonomous, body-heartbeat present, NO runner-heartbeat, NO running-sid."""
    agent_dir = tmp_path / "agents" / "testagent"
    (agent_dir / "session").mkdir(parents=True)
    (agent_dir / "session" / "agent-state").write_text("IDLE", encoding="utf-8")
    (agent_dir / "session" / "agent-mode").write_text("autonomous", encoding="utf-8")
    (agent_dir / "session" / "body-heartbeat-sid.json").write_text("{}", encoding="utf-8")
    (agent_dir / "session" / "execution-diary.jsonl").write_text(
        json.dumps({"goal_id": "g-1"}) + "\n", encoding="utf-8"
    )
    return agent_dir


def _events_for_role(agent_dir, role):
    """Every event the role's probe set emits against this tree, any severity."""
    out = []
    for probe in wd.build_probes(_ctx(role, agent_dir)):
        try:
            for ev in probe.check() or []:
                out.append((probe.name, ev.event, ev.severity))
        except Exception:
            # A probe raising on a worker-shaped tree is a separate finding; the
            # watchdog already wraps probe errors, and it is not a spurious event.
            continue
    return out


def test_healthy_worker_tick_reports_nothing_about_worker_shaped_state(tmp_path):
    """A HEALTHY worker must not report events about state it does not keep.

    Verify criterion (c), and the goal calls it load-bearing: a worker is IDLE +
    autonomous by design, and that exact tuple has already fooled two other
    gates this week.

    Asserted on EVENTS OF ANY SEVERITY, not on criticals. The first draft
    filtered `severity == "critical"` and passed trivially — the spurious
    heartbeat event is severity `info`, so the assertion was true for a reason
    that had nothing to do with the filter under test. Its paired control
    (below) is what exposed that, which is the entire reason the control exists.
    """
    agent_dir = _worker_shaped_tree(tmp_path)
    events = _events_for_role(agent_dir, "worker")
    assert events == [], f"healthy worker reported: {events}"


def test_unfiltered_set_does_report_on_the_same_worker_tree(tmp_path):
    """Positive control: the filter is what makes the test above meaningful.

    Same tree, reducer probe set. If this were empty, the assertion above would
    be proving that nothing fires anywhere rather than that the filter works —
    a green that means nothing (rb-245 / guard-2421, with the sign flipped).

    Measured on the live cc-08 worker before the filter existed: a reducer-mode
    tick emitted `heartbeat: heartbeat_missing (state=IDLE age=-s)` against a
    perfectly healthy worker.
    """
    agent_dir = _worker_shaped_tree(tmp_path)
    events = _events_for_role(agent_dir, "reducer")
    assert events, (
        "expected the unfiltered reducer set to report on a worker-shaped tree; "
        "an empty result here would make the filter test above vacuous"
    )
    reported = {name for name, _, _ in events}
    assert reported - set(wd.WORKER_SAFE_PROBES), (
        "the reducer-only events should come from the EXCLUDED probes; got %r" % (events,)
    )


# ---------------------------------------------------------------------------
# Verify criterion (b) — "induce a stall on a worker and show the probe fires"
# ---------------------------------------------------------------------------

def test_induced_box_fault_DOES_fire_on_a_worker(tmp_path, monkeypatch):
    """The delivered coverage is not vacuous: a real fault reaches a worker.

    Criterion (b) asks to induce a stall and show a probe fires. The STALL half
    is not satisfiable here and is reported as such in the close — StalledProbe
    is excluded by design, and the measurement behind that (diary gaps of
    34/56/92/28/15 min on cc-08, one entry per goal) says no threshold can
    separate a stall from a long work unit on a worker.

    But the same question applies to the five probes that ARE enabled, and it is
    the exact defect this goal is about: shipping probes that never fire reads as
    coverage and is not. Without this test the suite proves only that a healthy
    worker stays quiet — which a completely inert probe set would also satisfy.
    So: induce a genuine box-level fault and require the WORKER set to report it.

    memory-headroom is the inducible one — it reads no agent state at all, only
    /proc, so it is a pure function of two module helpers and a threshold.
    """
    agent_dir = _worker_shaped_tree(tmp_path)
    # 8 GiB box, one Claude process holding 7 GiB => 87.5%, over the default.
    monkeypatch.setattr(wd, "_mem_total_kb", lambda: 8 * 1024 * 1024)
    monkeypatch.setattr(wd, "_claude_rss_kb", lambda: [(4242, "claude", 7 * 1024 * 1024)])

    events = _events_for_role(agent_dir, "worker")
    assert events, "induced memory pressure produced NO event on a worker"
    names = {name for name, _, _ in events}
    assert "memory-headroom" in names, f"expected memory-headroom; got {events}"
    sev = {ev: s for _, ev, s in events}
    assert sev.get("memory_pressure") == "critical", (
        "the induced fault must reach the operator as critical, not info: %r" % (events,)
    )


def test_that_induced_fault_is_absent_when_the_box_is_healthy(tmp_path, monkeypatch):
    """Negative control for the test above — the fault is induced, not ambient.

    If memory-headroom fired regardless of the reading, the test above would be
    proving nothing about induction. Same tree, same worker set, healthy numbers.
    """
    agent_dir = _worker_shaped_tree(tmp_path)
    monkeypatch.setattr(wd, "_mem_total_kb", lambda: 8 * 1024 * 1024)
    monkeypatch.setattr(wd, "_claude_rss_kb", lambda: [(4242, "claude", 256 * 1024)])
    assert _events_for_role(agent_dir, "worker") == []
