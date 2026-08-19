"""InfraComponentProbe — cadence polling of opted-in infra-health components.

WHAT THIS PROBE IS FOR. infra-health carries dozens of registered components
and, before this landed, nothing polled any of them on the watchdog's tick. A
component whose only reader is "someone happens to ask" is not monitored. This
probe polls a short opt-in list from the world overlay so a silent external
failure is caught by a clock.

WHY THE TESTS LOOK LIKE THIS. Every case below injects `_infra_health_check`
and sets `.components` by hand. None of them touch the world config, the
network, or a subprocess — and `test_probe_is_inert_under_pytest_by_default`
pins that a test which forgets to inject gets an INERT probe rather than a live
one. That is the load-bearing test in this file: the role tests in
test_agent_watchdog_worker_role.py call check() on the entire reducer set
against the REAL project root, so without the guard a unit-test run would shell
out to live probes and write the production world/infra-health.yaml.

THE THREE-OUTCOME SPLIT is what most of the rest pins. A check can say the
target is broken, say it is fine, or fail to reach the target at all. Collapsing
that last one into "broken" is the tempting simplification and it is wrong: most
boxes in a fleet have no route to any given host, so unreachable-as-failure
would alert continuously about healthy hardware. no_target is therefore silent
AND non-clearing, and both halves are pinned (cases C and C2).
"""

import importlib.util
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from _paths import PROJECT_ROOT
except Exception:  # pragma: no cover - fallback for a detached checkout
    PROJECT_ROOT = Path(__file__).resolve().parents[3]

WD_PATH = PROJECT_ROOT / "core" / "scripts" / "agent-watchdog.py"


def _load():
    spec = importlib.util.spec_from_file_location(
        "agent_watchdog_infra_under_test", WD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wd = _load()

ONE = [{"component": "c1", "interval_minutes": 30}]


@pytest.fixture
def probe(tmp_path, monkeypatch):
    """A probe with one component and a stubbed check.

    `probe.result` is what the stubbed check returns; `probe.calls` records
    every component actually polled, so a test can assert on the ABSENCE of a
    poll (the interval gate) and not only on emitted events.
    """
    ctx = wd.WatchdogContext(agent_name="testagent", agent_dir=tmp_path,
                             project_root_path=PROJECT_ROOT)
    p = wd.InfraComponentProbe(ctx)
    p.components = [dict(c) for c in ONE]
    p.result = {}
    p.calls = []

    def _stub(root, agent, component, timeout=60.0):
        p.calls.append(component)
        return p.result

    monkeypatch.setattr(wd, "_infra_health_check", _stub)
    return p


def _ev(p):
    return [(e.event, e.severity) for e in p.check()]


def _expire(p):
    """Age every recorded poll past any interval, without sleeping."""
    for k in list(p.last_polled):
        p.last_polled[k] -= 99999


# ---------------------------------------------------------------------------
# Hermeticity — the guard that keeps the rest of the suite honest
# ---------------------------------------------------------------------------

def test_probe_is_inert_under_pytest_by_default(tmp_path):
    """A probe built under pytest polls NOTHING unless a test opts in.

    Without this, the reducer-set role tests would shell out to live infra
    probes against the real world config and write production monitoring
    state. Asserted on the CONFIG rather than on check(): an empty component
    list is the reason nothing runs, and pinning the cause survives a future
    refactor of check() that pinning the effect would not.
    """
    assert wd._watchdog_infra_components() == []
    ctx = wd.WatchdogContext(agent_name="a", agent_dir=tmp_path,
                             project_root_path=PROJECT_ROOT)
    assert wd.InfraComponentProbe(ctx).components == []


def test_opt_in_env_lets_the_real_loader_run(tmp_path, monkeypatch):
    """Positive control for the guard above — it is a gate, not a hard-coded [].

    If the loader returned [] unconditionally, the test above would pass while
    the probe was dead in production too. This proves the pytest branch is the
    only thing suppressing it.
    """
    monkeypatch.setenv("MIND_WATCHDOG_INFRA_TEST", "1")
    called = {}

    def _fake_load(name, default=None):
        called["name"] = name
        return {"components": [{"component": "x", "interval_minutes": 7}]}

    mod = type(sys)("_world_config")
    mod.load_world_config = _fake_load
    monkeypatch.setitem(sys.modules, "_world_config", mod)

    got = wd._watchdog_infra_components()
    assert called["name"] == "watchdog-infra-components"
    assert got == [{"component": "x", "interval_minutes": 7}]


def test_no_components_means_no_work(probe):
    probe.components = []
    assert probe.check() == []
    assert probe.calls == []


# ---------------------------------------------------------------------------
# Transitions — an event fires on CHANGE, not on every tick
# ---------------------------------------------------------------------------

def test_failure_alerts_once_per_episode(probe):
    probe.result = {"status": "failed", "error": "too hot"}
    assert _ev(probe) == [("infra_component_failed", "critical")]
    _expire(probe)
    assert _ev(probe) == [], "a component down for a day must not alert every tick"


def test_recovery_emits_exactly_one_info_event(probe):
    probe.result = {"status": "failed", "error": "x"}
    probe.check()
    probe.result = {"status": "ok", "detail": "fine"}
    _expire(probe)
    assert _ev(probe) == [("infra_component_recovered", "info")]
    _expire(probe)
    assert _ev(probe) == [], "recovery is a transition, not a per-tick state"


def test_healthy_component_is_silent_from_the_start(probe):
    probe.result = {"status": "ok", "detail": "fine"}
    assert _ev(probe) == []
    assert probe.calls == ["c1"], "silence must come from an ok reading, not a skipped poll"


def test_failure_payload_carries_the_reason(probe):
    probe.result = {"status": "failed", "error": "GPU 91C"}
    events = probe.check()
    assert events[0].payload["component"] == "c1"
    assert events[0].payload["error"] == "GPU 91C"
    assert "GPU 91C" in events[0].summary, "the operator reads the summary, not the payload"


# ---------------------------------------------------------------------------
# no_target — the third outcome, and the reason this probe is not two-valued
# ---------------------------------------------------------------------------

def test_no_target_is_silent(probe):
    """Unreachable is a fact about the OBSERVER, not the target."""
    probe.result = {"status": "no_target", "detail": "off-LAN"}
    assert _ev(probe) == []


def test_no_target_does_not_clear_a_prior_failure(probe):
    """Losing sight of a broken thing is not the same as it being fixed.

    The fail-safe direction: a component that failed and then went unreachable
    stays failed, so it cannot emit a false "recovered" and it cannot re-alert
    when it comes back still broken.
    """
    probe.result = {"status": "failed", "error": "x"}
    probe.check()
    probe.result = {"status": "no_target", "detail": "off-LAN"}
    _expire(probe)
    assert _ev(probe) == []
    assert probe.condition["c1"] == "failed"
    # And when it becomes visible again and IS fixed, the recovery still fires.
    probe.result = {"status": "ok"}
    _expire(probe)
    assert _ev(probe) == [("infra_component_recovered", "info")]


def test_unknown_status_is_treated_like_no_target(probe):
    """A status this probe does not recognise must not be read as either verdict."""
    probe.result = {"status": "something-new"}
    assert _ev(probe) == []
    assert "c1" not in probe.condition


# ---------------------------------------------------------------------------
# An unrunnable check is reported, but only once
# ---------------------------------------------------------------------------

def test_unrunnable_check_reports_once_then_stays_quiet(probe):
    """A misconfigured component name must not sit silently unmonitored.

    Distinct from a failure: "the prober is broken" and "the target is broken"
    need different humans, so it is info rather than critical — but it is not
    silence, because a component nobody can check is a monitoring hole that
    otherwise looks identical to a healthy one.
    """
    probe.result = {}
    assert _ev(probe) == [("infra_check_unrunnable", "info")]
    _expire(probe)
    assert _ev(probe) == []


# ---------------------------------------------------------------------------
# Cadence
# ---------------------------------------------------------------------------

def test_interval_gate_suppresses_the_poll_itself(probe):
    """The gate must skip the SUBPROCESS, not merely the event.

    Asserted on probe.calls: an implementation that polled every tick and
    filtered events afterwards would pass an event-only assertion while adding
    a subprocess to every iteration close.
    """
    probe.result = {"status": "ok"}
    probe.check()
    for _ in range(5):
        probe.check()
    assert probe.calls == ["c1"]


def test_first_ever_check_polls_immediately(probe):
    """No baseline delay — a component broken at install time is caught now."""
    probe.result = {"status": "failed", "error": "x"}
    assert _ev(probe) == [("infra_component_failed", "critical")]


def test_clock_moving_backwards_repolls_instead_of_waiting(probe):
    """An NTP step or VM resume must not park a component for a bogus interval."""
    probe.result = {"status": "ok"}
    probe.check()
    probe.last_polled["c1"] = time.time() + 10_000
    probe.check()
    assert probe.calls == ["c1", "c1"]


def test_at_most_one_component_polled_per_tick(probe):
    """Bounds what this probe can add to a single iteration close.

    With N components due at once an unbounded loop would serialise N
    subprocesses onto the loop's critical path. The most-overdue one goes
    first, and the rest follow on later ticks.
    """
    probe.components = [{"component": f"c{i}", "interval_minutes": 30}
                        for i in range(4)]
    probe.result = {"status": "ok"}
    probe.check()
    assert len(probe.calls) == 1
    probe.check()
    assert len(probe.calls) == 2
    assert len(set(probe.calls)) == 2, "each tick must advance to a DIFFERENT component"


# ---------------------------------------------------------------------------
# Tick-mode state persistence
# ---------------------------------------------------------------------------

def test_state_survives_a_tick_boundary(probe, tmp_path):
    """Tick mode builds a fresh probe per invocation — without persistence the
    condition map resets and a component down for a week re-alerts every tick.
    """
    probe.result = {"status": "failed", "error": "x"}
    probe.check()

    ctx = wd.WatchdogContext(agent_name="testagent", agent_dir=tmp_path,
                             project_root_path=PROJECT_ROOT)
    revived = wd.InfraComponentProbe(ctx)
    revived.components = [dict(c) for c in ONE]
    revived.from_dict(probe.to_dict())
    assert revived.condition == {"c1": "failed"}
    assert revived.last_polled.get("c1") == pytest.approx(probe.last_polled["c1"])


def test_from_dict_tolerates_junk(probe):
    """State files are written by a prior version and can be torn or stale."""
    for junk in ({}, None, {"last_polled": "nope", "condition": 7},
                 {"last_polled": {"c1": "not-a-number"}}):
        p = wd.InfraComponentProbe(probe.ctx)
        p.from_dict(junk)
        assert isinstance(p.last_polled, dict)
        assert isinstance(p.condition, dict)
        assert "c1" not in p.last_polled or isinstance(p.last_polled["c1"], (int, float))


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------

def test_malformed_config_entries_are_dropped_not_fatal(monkeypatch):
    """One bad row must not take the whole list — and must not take the tick."""
    monkeypatch.setenv("MIND_WATCHDOG_INFRA_TEST", "1")
    mod = type(sys)("_world_config")
    mod.load_world_config = lambda name, default=None: {"components": [
        {"component": "good", "interval_minutes": 5},
        {"component": "", "interval_minutes": 5},          # no name
        "not-a-dict",
        {"interval_minutes": 5},                            # missing component
        {"component": "bad-interval", "interval_minutes": "soon"},
        {"component": "zero", "interval_minutes": 0},       # floored to 1
    ]}
    monkeypatch.setitem(sys.modules, "_world_config", mod)

    got = {c["component"]: c["interval_minutes"]
           for c in wd._watchdog_infra_components()}
    assert set(got) == {"good", "bad-interval", "zero"}
    assert got["good"] == 5
    assert got["bad-interval"] == wd.InfraComponentProbe.DEFAULT_INTERVAL_MIN
    assert got["zero"] == 1, "a 0-minute interval would poll on every single tick"


def test_unreadable_config_yields_an_inert_probe(monkeypatch):
    """A world-resolution failure degrades THIS probe, never the watchdog."""
    monkeypatch.setenv("MIND_WATCHDOG_INFRA_TEST", "1")
    mod = type(sys)("_world_config")

    def _boom(name, default=None):
        raise RuntimeError("no world")

    mod.load_world_config = _boom
    monkeypatch.setitem(sys.modules, "_world_config", mod)
    assert wd._watchdog_infra_components() == []


def test_non_list_components_key_is_ignored(monkeypatch):
    monkeypatch.setenv("MIND_WATCHDOG_INFRA_TEST", "1")
    mod = type(sys)("_world_config")
    mod.load_world_config = lambda name, default=None: {"components": {"c1": {}}}
    monkeypatch.setitem(sys.modules, "_world_config", mod)
    assert wd._watchdog_infra_components() == []
