"""Tests for signal-liveness-canary.py ( outcome 3) — the THIRD member
of the canary family, sibling to cadence-stale-canary and stale-sentinel-canary.

Three layers, and the third is the one that matters most here:
  1. Registry shape — every SIGNALS row carries the keys the run loop reads and
     a callable assertion, so a malformed row fails at test time rather than
     silently never firing in production.
  2. run() counter logic, driven by an INJECTED assertion_runner + a
     monkeypatched tmp working-memory.yaml so every case is hermetic (no real
     WM, no aspirations.jsonl mutation, no subprocess).
  3. THE REAL PRODUCTION ASSERTION, exercised directly against a tmp agent dir.
     Layer 2 alone would pass with an assertion that can never return True —
     which is precisely the always-CLEAR defect this whole goal exists to catch,
     so a canary whose own liveness assertion was untested would be the joke
     version of itself. Layer 3 positive-controls BOTH directions: a fresh
     artifact reads live, an old one reads dead.

The fail-open case (assertion returns None) is asserted to NEVER fire. That is
not politeness: g-318-156 unit 1 Finding B records that always-ALARM is the same
defect as always-CLEAR, so a broken assertion manufacturing stuck counts would
make this canary an instance of the class it detects.
"""

import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location(
    "signal_liveness_canary", SCRIPTS / "signal-liveness-canary.py"
)
canary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(canary)


# ----------------------------------------------------------- helpers ----------

FAKE_SIGNALS = [
    {"name": "sig-a", "what": "test signal a", "evidence": "none",
     "assertion": lambda: (False, "live"), "remedy": "none"},
    {"name": "sig-b", "what": "test signal b", "evidence": "none",
     "assertion": lambda: (False, "live"), "remedy": "none"},
]


def _runner(dead_names=(), error_names=()):
    """Injected (signal) -> (dead|None, detail), keyed on signal name."""
    def runner(signal):
        name = signal["name"]
        if name in error_names:
            return None, f"{name}: boom"
        if name in dead_names:
            return True, f"{name}: dead"
        return False, f"{name}: live"
    return runner


@pytest.fixture
def hermetic(tmp_path, monkeypatch):
    """Point run() at a tmp WM; stub filing + dedup so no real side effects."""
    wm = tmp_path / "working-memory.yaml"
    wm.write_text(yaml.dump({"slots": {}}), encoding="utf-8")

    import wm as wm_mod  # run() does `from wm import wm_path` at call time
    monkeypatch.setattr(wm_mod, "wm_path", lambda: wm, raising=False)
    monkeypatch.setattr(canary, "AGENT_DIR", str(tmp_path), raising=False)

    filed = []
    monkeypatch.setattr(
        canary, "_file_investigate",
        lambda sig, stuck, detail, dry: (filed.append((sig["name"], stuck)), {"ok": True})[1],
    )
    # Dedup is a live-queue concern; force "no duplicate" so the fire path files.
    monkeypatch.setattr(canary, "_recent_investigate_exists", lambda name: False)
    return {"wm": wm, "filed": filed}


def _counters(wm):
    data = yaml.safe_load(wm.read_text()) or {}
    return (data.get("slots") or {}).get(canary.CANARY_SLOT) or {}


def _run(dead=(), errors=(), threshold=3, hermetic=None, times=1):
    rep = None
    for _ in range(times):
        rep = canary.run(
            threshold=threshold, dry_run=False,
            assertion_runner=_runner(dead, errors), signals=FAKE_SIGNALS,
        )
    return rep


# --------------------------------------------------------- registry -----------

def test_registry_rows_are_well_formed():
    """A malformed row would never fire in production and nothing would say so."""
    assert canary.SIGNALS, "registry must not be empty — the canary would watch nothing"
    for sig in canary.SIGNALS:
        for key in ("name", "what", "evidence", "assertion", "remedy"):
            assert key in sig, f"signal {sig.get('name')!r} missing key {key!r}"
        assert callable(sig["assertion"]), f"{sig['name']}: assertion must be callable"
    names = [s["name"] for s in canary.SIGNALS]
    assert len(names) == len(set(names)), "signal names must be unique (they key the WM counters)"


def test_slot_name_is_distinct_from_siblings():
    """Sharing a sibling's slot would silently cross-wire two canaries' counters."""
    assert canary.CANARY_SLOT == "signal_liveness_canary"


# ------------------------------------------------- run() counter logic --------

def test_all_live_no_fire(hermetic):
    rep = _run(hermetic=hermetic)
    assert rep["investigate_goals_filed"] == []
    assert all(e["new_stuck_count"] == 0 for e in rep["signals"].values())
    assert hermetic["filed"] == []


def test_dead_for_threshold_consecutive_runs_fires_then_resets(hermetic):
    rep = _run(dead=("sig-a",), threshold=3, hermetic=hermetic, times=3)
    assert hermetic["filed"] == [("sig-a", 3)], hermetic["filed"]
    assert rep["signals"]["sig-a"]["fired"] is True
    # post-fire reset so the next death re-starts counting rather than re-filing
    assert _counters(hermetic["wm"])["sig-a"] == 0
    assert rep["signals"]["sig-b"]["fired"] is False


def test_dead_then_live_resets_and_never_fires(hermetic):
    _run(dead=("sig-a",), threshold=3, hermetic=hermetic, times=2)
    assert _counters(hermetic["wm"])["sig-a"] == 2
    rep = _run(dead=(), threshold=3, hermetic=hermetic)      # recovers
    assert _counters(hermetic["wm"])["sig-a"] == 0
    assert rep["investigate_goals_filed"] == []
    assert hermetic["filed"] == []


def test_unevaluatable_assertion_fails_open_and_never_fires(hermetic):
    """A broken assertion must never manufacture a stuck count (Finding B)."""
    rep = _run(errors=("sig-a",), threshold=3, hermetic=hermetic, times=5)
    assert hermetic["filed"] == []
    assert _counters(hermetic["wm"])["sig-a"] == 0
    assert rep["signals"]["sig-a"]["dead"] is None
    assert "boom" in rep["signals"]["sig-a"]["detail"]


def test_threshold_is_honored(hermetic):
    """At threshold 4, three consecutive dead runs must NOT fire."""
    _run(dead=("sig-a",), threshold=4, hermetic=hermetic, times=3)
    assert hermetic["filed"] == []
    assert _counters(hermetic["wm"])["sig-a"] == 3


def test_signals_are_tracked_independently(hermetic):
    _run(dead=("sig-a",), threshold=3, hermetic=hermetic, times=2)
    c = _counters(hermetic["wm"])
    assert c["sig-a"] == 2 and c["sig-b"] == 0


def test_dry_run_does_not_persist_counters(hermetic):
    canary.run(threshold=3, dry_run=True,
               assertion_runner=_runner(("sig-a",)), signals=FAKE_SIGNALS)
    assert _counters(hermetic["wm"]) == {}, "dry-run must not write the WM slot"


# ------------------------- the REAL production assertion (layer 3) ------------

def _watchdog_snapshot(tmp_path, age_minutes):
    sess = tmp_path / "session"
    sess.mkdir(parents=True, exist_ok=True)
    snap = sess / "watchdog-prev-state.json"
    snap.write_text("{}", encoding="utf-8")
    when = time.time() - age_minutes * 60.0
    os.utime(snap, (when, when))
    return snap


def test_real_assertion_reads_fresh_snapshot_as_live(tmp_path, monkeypatch):
    monkeypatch.setattr(canary, "AGENT_DIR", str(tmp_path), raising=False)
    _watchdog_snapshot(tmp_path, age_minutes=5)
    dead, detail = canary._assert_watchdog_prev_state()
    assert dead is False, detail
    assert "live" in detail


def test_real_assertion_reads_stale_snapshot_as_dead(tmp_path, monkeypatch):
    """The positive control: the assertion CAN return dead=True.

    Without this, every other test in this file would still pass against an
    assertion hardwired to 'live' — an always-CLEAR detector, which is the exact
    class g-318-156 exists to find.
    """
    monkeypatch.setattr(canary, "AGENT_DIR", str(tmp_path), raising=False)
    _watchdog_snapshot(tmp_path, age_minutes=canary.DEFAULT_STALE_MINUTES + 60)
    dead, detail = canary._assert_watchdog_prev_state()
    assert dead is True, detail
    assert "no --tick has completed" in detail


def test_real_assertion_treats_absent_snapshot_as_unevaluatable_not_dead(tmp_path, monkeypatch):
    """ABSENT is not DEAD — a fresh agent dir must not alarm on its first closes."""
    monkeypatch.setattr(canary, "AGENT_DIR", str(tmp_path), raising=False)
    (tmp_path / "session").mkdir(parents=True, exist_ok=True)
    dead, detail = canary._assert_watchdog_prev_state()
    assert dead is None, detail


def test_real_assertion_is_unevaluatable_with_no_agent_bound(monkeypatch):
    monkeypatch.setattr(canary, "AGENT_DIR", None, raising=False)
    dead, detail = canary._assert_watchdog_prev_state()
    assert dead is None and "no agent bound" in detail


# ------------------------------------------------------------------------------
# The "clear covers never-examined" family ( unit 6).
#
# Each assertion is driven in ALL THREE directions — live, dead, unevaluatable —
# by injecting the (rc, stdout, stderr) triple the real instrument produced when it
# was measured on alpha/cc-08 2026-09-13. The DEAD direction is the load-bearing
# half: without it every other test here would still pass against an assertion
# hardwired to "live", which is an always-CLEAR detector and the exact class this
# goal exists to find.
# ------------------------------------------------------------------------------

GATE_ROWS = (
    "exhaustive-search-gate-trigger",
    "verify-before-assuming-gate-trigger",
    "zero-count-gate-trigger",
    "positive-state-gate-trigger",
)


def _row(name):
    for sig in canary.SIGNALS:
        if sig["name"] == name:
            return sig
    raise AssertionError(f"registry row {name!r} is absent — it would watch nothing")


def _inject(monkeypatch, rc, out, err):
    monkeypatch.setattr(canary, "_probe", lambda argv: (rc, out, err), raising=True)


# ---- S-3 read channel: the discriminator is BYTES, not ROWS -------------------

def test_read_channel_live_on_empty_result_set(monkeypatch):
    """A legitimately EMPTY answer is still an answer. '[]' is 2 bytes and 0 rows.

    This is why the predicate counts bytes: a row-count predicate would call every
    legitimately-empty query DEAD, which is the always-ALARM twin of the defect.
    """
    _inject(monkeypatch, 0, "[]", "")
    dead, detail = canary._assert_query_read_channel()
    assert dead is False, detail
    assert "2 byte" in detail


def test_read_channel_dead_on_zero_bytes_at_rc_zero(monkeypatch):
    """POSITIVE CONTROL: the assertion CAN return dead=True.

    rc=0 with no bytes is the S-3 incident exactly — a parser read it as
    "0 blocked goals" while the call had been refused.
    """
    _inject(monkeypatch, 0, "", "")
    dead, detail = canary._assert_query_read_channel()
    assert dead is True, detail
    assert "ZERO bytes" in detail


def test_read_channel_treats_a_refusal_as_unevaluatable_not_dead(monkeypatch):
    """A refusal belongs to the OTHER row. Counting it here would report one fault
    as two dead signals and double the stuck counters."""
    _inject(monkeypatch, 2, "", "aspirations-query.sh: unknown option — refusing.")
    dead, detail = canary._assert_query_read_channel()
    assert dead is None, detail


def test_read_channel_unevaluatable_when_the_probe_raises(monkeypatch):
    def _boom(argv):
        raise OSError("no interpreter")
    monkeypatch.setattr(canary, "_probe", _boom, raising=True)
    dead, detail = canary._assert_query_read_channel()
    assert dead is None and "could not run" in detail


# ---- S-3 refusal channel: two independent ways to be dead --------------------

def test_refusal_channel_live_when_a_bad_flag_still_fails_loudly(monkeypatch):
    _inject(monkeypatch, 2, "", "x" * 429)
    dead, detail = canary._assert_query_refusal_channel()
    assert dead is False, detail
    assert "429 byte" in detail


def test_refusal_channel_dead_when_a_never_valid_flag_succeeds(monkeypatch):
    """POSITIVE CONTROL 1: rc=0 on a flag that can never be valid is the
    pre-g-115-4733 passthrough regression — the wrong value written at exit 0."""
    _inject(monkeypatch, 0, "[]", "")
    dead, detail = canary._assert_query_refusal_channel()
    assert dead is True, detail
    assert "g-115-4733" in detail


def test_refusal_channel_dead_when_it_fails_without_saying_why(monkeypatch):
    """POSITIVE CONTROL 2: a non-zero rc with EMPTY stderr. The call fails, but the
    diagnosis is gone even for a caller that IS reading stderr."""
    _inject(monkeypatch, 2, "", "   ")
    dead, detail = canary._assert_query_refusal_channel()
    assert dead is True, detail
    assert "stderr is EMPTY" in detail


# ---- V-1..V-4: one shape, four rows ------------------------------------------

def test_every_gate_row_is_registered():
    """A gate with no row is a gate with no positive control anywhere."""
    for name in GATE_ROWS:
        _row(name)


def test_every_gate_row_names_a_script_that_exists():
    """guard-3448 class: an assertion pointing at a missing script is permanently
    unevaluatable, and an unevaluatable row resets its counter forever — it reads
    exactly like a healthy signal."""
    for name in GATE_ROWS:
        row = _row(name)
        target = row["evidence"]
        assert target, f"{name}: evidence must name what the assertion reads"
        dead, detail = row["assertion"]()
        assert "absent at" not in detail, f"{name}: {detail}"


def test_gate_rows_are_live_against_the_real_scripts():
    """No injection: drive the four real gates with their real must-trip inputs.

    This is the other half of the pair — the injected tests prove the PREDICATE is
    right, this proves the PLUMBING (path, interpreter, arg shape) is right too.
    Measured ~35 ms per gate.
    """
    for name in GATE_ROWS:
        dead, detail = _row(name)["assertion"]()
        assert dead is False, f"{name} is not live: {detail}"
        assert "still refuses" in detail


def test_gate_row_dead_when_a_must_trip_input_returns_rc_zero(monkeypatch):
    """POSITIVE CONTROL: rc=0 on an input engineered to trip the gate means the gate
    has stopped matching what it exists to match — and rc=0 is exactly what a caller
    records as 'evidence sufficient'."""
    _inject(monkeypatch, 0, '{"trigger_matched": null}', "")
    for name in GATE_ROWS:
        dead, detail = _row(name)["assertion"]()
        assert dead is True, f"{name}: {detail}"
        assert "evidence sufficient" in detail


def test_gate_row_unexpected_rc_is_unevaluatable_not_dead(monkeypatch):
    """rc=2 is usage, not a verdict. A broken invocation must never manufacture a
    stuck count — that would make this canary the always-ALARM defect."""
    _inject(monkeypatch, 2, "", "usage: ...")
    for name in GATE_ROWS:
        dead, detail = _row(name)["assertion"]()
        assert dead is None, f"{name}: {detail}"
        assert "unexpected rc=2" in detail


def test_gate_row_absent_script_is_unevaluatable_not_dead(tmp_path, monkeypatch):
    """A merge-wedged box legitimately lacks a recently-added script. Alarming there
    would report a tree problem as a dead detector."""
    monkeypatch.setattr(canary, "SCRIPT_DIR", tmp_path, raising=False)
    for name in GATE_ROWS:
        dead, detail = _row(name)["assertion"]()
        assert dead is None, f"{name}: {detail}"
        assert "absent at" in detail


def test_gate_row_unevaluatable_when_the_probe_raises(monkeypatch):
    def _boom(argv):
        raise OSError("interpreter vanished")
    monkeypatch.setattr(canary, "_probe", _boom, raising=True)
    for name in GATE_ROWS:
        dead, detail = _row(name)["assertion"]()
        assert dead is None and "could not run" in detail


# ---- the two inventory members deliberately NOT registered -------------------

def test_the_unregistered_inventory_members_stay_unregistered():
    """S-4 (an AWS call needing live credentials every iteration) and S-5/V-5
    (q4-provenance-sample read by rc, a stable DESIGN property that never degrades)
    are recorded in the inventory and deliberately absent here. If someone adds
    either as a canary row, this test should be deleted WITH a written reason —
    a network-dependent row flakes into the always-ALARM twin of this defect, and a
    row for something that cannot degrade counts a constant as a failure.
    """
    names = {s["name"] for s in canary.SIGNALS}
    for forbidden in ("s3-list-objects-denied", "q4-provenance-sample-rc"):
        assert forbidden not in names
