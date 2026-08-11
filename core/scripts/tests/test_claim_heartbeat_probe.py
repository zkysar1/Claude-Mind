"""ClaimHeartbeatProbe — the reader for claim-heartbeat-failure ().

g-306-221 shipped the WRITER: heartbeat-tick.sh records consecutive DDB
runner-claim heartbeat failures to `<agent>/session/claim-heartbeat-failure`.
Nothing READ it. Per reclaim-routed-work.md a signal with no consumer is
indistinguishable from a signal that never fires, and guard-772 says the
writer's other channel (stderr) is invisible when the tick runs inside a
backgrounded Bash call — the normal case. So on an unattended reducer the
durable marker was the only surviving evidence and no code looked at it.

The load-bearing tests here are:

  test_present_marker_is_surfaced      — verification outcome 2 literally: the
                                         test FAILS when the marker is present
                                         and the consumer stays silent.
  test_probe_never_deletes_the_marker  — reducer_self_fence.read_failure_elapsed
                                         reads an ABSENT marker as "last renewal
                                         SUCCEEDED" and returns 0. A consuming
                                         reader would reset first_failed_at every
                                         read, so elapsed could never accumulate
                                         and the sustained-renewal-gap stepdown
                                         could NEVER fire. That is the gate which
                                         stops a box acting as reducer after it
                                         has lost the claim.
  test_tick_mode_emits_once_across_two_processes
                                       — `--tick` is a FRESH PROCESS per
                                         iteration, so "emit once per transition"
                                         lives entirely in to_dict/from_dict
                                         serialization. An in-process-only dedup
                                         test passes while the real thing spams
                                         every iteration.
"""

import importlib.util
import json
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
    """Import agent-watchdog.py by path (hyphenated name is not importable).

    guard-2138: grep the target for a module-level `threading.Timer(... os._exit)`
    before importing a framework script into the pytest process. Verified absent
    from agent-watchdog.py — it arms no import-time watchdog timer, so there is
    nothing to cancel here.
    """
    spec = importlib.util.spec_from_file_location("agent_watchdog_cheartbeat", WD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


WD = _load()


@pytest.fixture
def agent_dir(tmp_path):
    d = tmp_path / "agents" / "testagent"
    (d / "session").mkdir(parents=True)
    return d


def _ctx(mod, agent_dir, role="reducer"):
    return mod.WatchdogContext(
        agent_name="testagent",
        agent_dir=agent_dir,
        project_root_path=agent_dir.parents[1],
        body_role=role,
    )


def _write_marker(agent_dir, first_failed_at, count=3, rc=1, err="daemon unreachable"):
    p = agent_dir / "session" / "claim-heartbeat-failure"
    p.write_text(
        f"first_failed_at={first_failed_at}\ncount={count}\n"
        f"last_rc={rc}\nlast_error={err}\n",
        encoding="utf-8",
    )
    return p


# ── registration ─────────────────────────────────────────────────────────────

def test_probe_is_registered(agent_dir):
    """MU2: unregistering the probe from build_probes must redden a test."""
    probes = WD.build_probes(_ctx(WD, agent_dir))
    assert "claim-heartbeat" in [p.name for p in probes]
    assert any(isinstance(p, WD.ClaimHeartbeatProbe) for p in probes)


def test_worker_body_does_not_register_the_probe(agent_dir):
    """Worker-inert BY CONSTRUCTION: heartbeat-tick refuses on IDLE before it ever
    reaches the DDB leg, so the marker cannot appear on a worker. Absence from
    WORKER_SAFE_PROBES is what makes that structural rather than incidental."""
    assert "claim-heartbeat" not in WD.WORKER_SAFE_PROBES
    probes = WD.build_probes(_ctx(WD, agent_dir, role="worker"))
    assert "claim-heartbeat" not in [p.name for p in probes]


# ── the outcome-2 test ───────────────────────────────────────────────────────

def test_present_marker_is_surfaced(agent_dir):
    """VERIFICATION OUTCOME 2. Marker present + consumer silent == FAIL.

    MU1: making check() return [] (the literal pre-fix state, where nothing read
    the marker at all) is killed here.
    """
    _write_marker(agent_dir, int(time.time()) - 60, count=4)
    probe = WD.ClaimHeartbeatProbe(_ctx(WD, agent_dir))
    events = probe.check()
    assert events, "marker present but the probe surfaced nothing"
    assert events[0].event == "claim_heartbeat_failing"
    assert events[0].payload["count"] == 4
    assert events[0].payload["elapsed_seconds"] >= 60


def test_absent_marker_is_silent(agent_dir):
    probe = WD.ClaimHeartbeatProbe(_ctx(WD, agent_dir))
    assert probe.check() == []


# ── the never-delete invariant ───────────────────────────────────────────────

def test_probe_never_deletes_the_marker(agent_dir):
    """MU3: a probe that unlinks the marker (the harmful /prime `cat + rm`
    precedent) permanently disarms reducer_self_fence's stepdown."""
    marker = _write_marker(agent_dir, int(time.time()) - 120)
    before = marker.read_text(encoding="utf-8")
    probe = WD.ClaimHeartbeatProbe(_ctx(WD, agent_dir))
    for _ in range(3):
        probe.check()
    assert marker.exists(), "probe deleted the marker — stepdown can never fire"
    assert marker.read_text(encoding="utf-8") == before, "probe mutated the marker"


def test_never_delete_preserves_the_existing_consumers_reading(agent_dir):
    """Join the invariant to the consumer it protects, rather than asserting it
    in the abstract: reducer_self_fence must still read a NON-ZERO elapsed after
    the probe has run."""
    sys.path.insert(0, str(PROJECT_ROOT / "core" / "scripts"))
    import reducer_self_fence

    now = int(time.time())
    marker = _write_marker(agent_dir, now - 300)
    probe = WD.ClaimHeartbeatProbe(_ctx(WD, agent_dir))
    probe.check()
    assert reducer_self_fence.read_failure_elapsed(marker, now) == 300


# ── escalation ───────────────────────────────────────────────────────────────

def test_stepdown_window_escalates_to_critical(agent_dir, monkeypatch):
    """MU5: disabling the escalation is killed here. The middle transition is the
    point — a lone appear-time event on a long outage leaves the reader with a
    notice from half an hour ago and silence since."""
    monkeypatch.setenv("OWNERSHIP_STALE_SECONDS", "600")
    _write_marker(agent_dir, int(time.time()) - 400)  # past 600/2
    probe = WD.ClaimHeartbeatProbe(_ctx(WD, agent_dir))
    events = probe.check()
    assert len(events) == 1
    assert events[0].event == "claim_heartbeat_stepdown_window"
    assert events[0].severity == "critical"
    assert events[0].include_processes is True


def test_below_half_window_is_not_critical(agent_dir, monkeypatch):
    monkeypatch.setenv("OWNERSHIP_STALE_SECONDS", "600")
    _write_marker(agent_dir, int(time.time()) - 60)  # under 600/2
    probe = WD.ClaimHeartbeatProbe(_ctx(WD, agent_dir))
    events = probe.check()
    assert events[0].event == "claim_heartbeat_failing"
    assert events[0].severity == "info"


def test_threshold_matches_the_writers_own_expression(monkeypatch):
    """The writer (heartbeat-tick.sh) and this reader must never disagree about
    when the stepdown window opens, so both read OWNERSHIP_STALE_SECONDS with the
    same 3900 default."""
    monkeypatch.delenv("OWNERSHIP_STALE_SECONDS", raising=False)
    assert WD._claim_stale_window_seconds() == 3900.0
    monkeypatch.setenv("OWNERSHIP_STALE_SECONDS", "1200")
    assert WD._claim_stale_window_seconds() == 1200.0
    monkeypatch.setenv("OWNERSHIP_STALE_SECONDS", "not-a-number")
    assert WD._claim_stale_window_seconds() == 3900.0


def test_summary_names_the_consequence(agent_dir, monkeypatch):
    """MU6: dropping the named consequence from the summary is killed here. The
    summary is the only thing a human sees on the stderr line."""
    monkeypatch.setenv("OWNERSHIP_STALE_SECONDS", "600")
    _write_marker(agent_dir, int(time.time()) - 400)
    probe = WD.ClaimHeartbeatProbe(_ctx(WD, agent_dir))
    summary = probe.check()[0].summary
    assert "SECOND REDUCER" in summary


# ── transitions + cross-process dedup ────────────────────────────────────────

def test_recovery_transition_is_emitted(agent_dir):
    marker = _write_marker(agent_dir, int(time.time()) - 60)
    probe = WD.ClaimHeartbeatProbe(_ctx(WD, agent_dir))
    assert probe.check()[0].event == "claim_heartbeat_failing"
    marker.unlink()
    events = probe.check()
    assert len(events) == 1
    assert events[0].event == "claim_heartbeat_recovered"
    assert probe.check() == [], "recovery must emit once, not every cycle"


def test_failing_emits_once_then_escalates_once(agent_dir, monkeypatch):
    monkeypatch.setenv("OWNERSHIP_STALE_SECONDS", "600")
    now = int(time.time())
    _write_marker(agent_dir, now - 60)
    probe = WD.ClaimHeartbeatProbe(_ctx(WD, agent_dir))
    assert probe.check()[0].event == "claim_heartbeat_failing"
    assert probe.check() == [], "same phase must not re-emit"
    _write_marker(agent_dir, now - 400)  # same episode, now past half the window
    assert probe.check()[0].event == "claim_heartbeat_stepdown_window"
    assert probe.check() == [], "stepdown must emit once per episode"


def test_tick_mode_emits_once_across_two_processes(agent_dir):
    """MU4, the informative mutant. `--tick` is a fresh PROCESS each iteration, so
    "emit once" lives ENTIRELY in to_dict/from_dict. Zeroing to_dict leaves an
    in-process dedup test GREEN while the real thing spams every iteration."""
    _write_marker(agent_dir, int(time.time()) - 60)

    p1 = WD.ClaimHeartbeatProbe(_ctx(WD, agent_dir))
    assert p1.check(), "first process must surface the marker"
    saved = json.loads(json.dumps(p1.to_dict()))  # survives a JSON round-trip

    p2 = WD.ClaimHeartbeatProbe(_ctx(WD, agent_dir))   # simulates the next tick
    p2.from_dict(saved)
    assert p2.check() == [], "second tick re-emitted — dedup is not serialized"


def test_run_tick_round_trip_does_not_re_emit(agent_dir):
    """The same property through the REAL tick entry point, not just the
    serialization helpers."""
    _write_marker(agent_dir, int(time.time()) - 60)
    log_path = agent_dir / "session" / "watchdog-events.jsonl"
    state_path = agent_dir / "session" / "watchdog-prev-state.json"

    WD.run_tick(_ctx(WD, agent_dir), log_path, state_path)
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert "claim-heartbeat" in saved, "tick did not persist the probe's state"
    first = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    assert "claim_heartbeat_failing" in first

    WD.run_tick(_ctx(WD, agent_dir), log_path, state_path)
    second = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    assert second.count("claim_heartbeat_failing") == 1, (
        "second tick re-emitted the same transition"
    )


# ── parser tolerance ─────────────────────────────────────────────────────────

def test_corrupt_marker_is_not_read_as_a_long_outage(agent_dir):
    """Mirrors reducer_self_fence.read_failure_elapsed: an unreadable duration
    must never be treated as a long one.

    This asserted `check() == []` until the fresh-eyes pass on g-306-226. Silence
    was only ever a PROXY for the real invariant, and it was the wrong one: it also
    forbade saying "I cannot read this", which is the one useful thing to say here.
    The invariant is about the SEVERITY of the reading, so assert that directly —
    no stepdown, no critical, no fabricated elapsed.
    """
    p = agent_dir / "session" / "claim-heartbeat-failure"
    p.write_text("first_failed_at=\ncount=oops\n", encoding="utf-8")
    probe = WD.ClaimHeartbeatProbe(_ctx(WD, agent_dir))
    events = probe.check()
    assert all(e.severity != "critical" for e in events)
    assert all("stepdown" not in e.event for e in events)
    assert all("elapsed_seconds" not in (e.payload or {}) for e in events), \
        "a corrupt marker must never yield a fabricated outage duration"
    assert p.exists(), "a corrupt marker must still not be deleted"


def test_parser_handles_partial_and_absent_input():
    assert WD.parse_claim_heartbeat_marker(None) is None
    assert WD.parse_claim_heartbeat_marker("") is None
    assert WD.parse_claim_heartbeat_marker("garbage") is None
    out = WD.parse_claim_heartbeat_marker("first_failed_at=100\n")
    assert out["first_failed_at"] == 100 and out["count"] == 0
    out = WD.parse_claim_heartbeat_marker(
        "first_failed_at=100\ncount=7\nlast_rc=3\nlast_error=a=b spaces\n"
    )
    assert out["count"] == 7 and out["last_rc"] == "3"
    assert out["last_error"] == "a=b spaces", "value containing '=' must survive"


# ── present-but-unreadable (fresh-eyes F1, ) ────────────────────────
#
# read_text_safe collapses "missing" and "unreadable" into the same None, so the
# first cut of this probe reported an UNREADABLE marker as claim_heartbeat_recovered
# — the single most reassuring thing it can say, emitted exactly when it cannot
# tell. That direction was inherited from reducer_self_fence, where it is correct
# because its remedy is destructive. A reporting consumer inverts it.

def _write_unreadable(agent_dir):
    """Present on disk, parse-hostile: no numeric first_failed_at."""
    p = agent_dir / "session" / "claim-heartbeat-failure"
    p.write_text("first_failed_at=\ncount=oops\n", encoding="utf-8")
    return p


def test_present_but_unreadable_is_surfaced_not_silent(agent_dir):
    _write_unreadable(agent_dir)
    probe = WD.ClaimHeartbeatProbe(_ctx(WD, agent_dir))
    events = probe.check()
    assert [e.event for e in events] == ["claim_heartbeat_unreadable"]
    # Must NOT be the reassuring reading.
    assert "recovered" not in events[0].event
    assert events[0].severity == "info"      # declared vocabulary is {critical, info}


def test_unreadable_does_not_manufacture_a_recovery(agent_dir):
    """absent -> unreadable -> absent must never emit a recovery we did not observe."""
    probe = WD.ClaimHeartbeatProbe(_ctx(WD, agent_dir))
    assert probe.check() == []               # absent
    _write_unreadable(agent_dir)
    assert [e.event for e in probe.check()] == ["claim_heartbeat_unreadable"]
    (agent_dir / "session" / "claim-heartbeat-failure").unlink()
    assert probe.check() == []               # still absent — no phantom recovery


def test_unreadable_preserves_the_failing_memory(agent_dir):
    """failing -> unreadable -> gone still reports the recovery that really happened.

    This is why last_unreadable is a SEPARATE field: folding it into last_state
    would either lose the outage (no recovery reported) or invent one.
    """
    _write_marker(agent_dir, int(time.time()) - 60)
    probe = WD.ClaimHeartbeatProbe(_ctx(WD, agent_dir))
    assert [e.event for e in probe.check()] == ["claim_heartbeat_failing"]
    _write_unreadable(agent_dir)
    assert [e.event for e in probe.check()] == ["claim_heartbeat_unreadable"]
    (agent_dir / "session" / "claim-heartbeat-failure").unlink()
    assert [e.event for e in probe.check()] == ["claim_heartbeat_recovered"]


def test_unreadable_dedup_survives_the_process_boundary(agent_dir):
    """--tick is a fresh process, so this dedup lives entirely in serialization.

    Mutation-equivalent of MU4, applied to the new field: dropping last_unreadable
    from to_dict leaves the in-process test above green while production re-warns
    every single iteration.
    """
    _write_unreadable(agent_dir)
    first = WD.ClaimHeartbeatProbe(_ctx(WD, agent_dir))
    assert [e.event for e in first.check()] == ["claim_heartbeat_unreadable"]
    carried = json.loads(json.dumps(first.to_dict()))     # through the state file
    assert carried.get("last_unreadable") is True, "last_unreadable must persist"

    second = WD.ClaimHeartbeatProbe(_ctx(WD, agent_dir))  # fresh process
    second.from_dict(carried)
    assert second.check() == [], "re-warned across the process boundary"
