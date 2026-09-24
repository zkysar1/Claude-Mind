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
import json
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
    # V-6, added 2026-09-13. It was registered in SIGNALS on 2026-09-13 and NOT added
    # here, so every tuple-driven test below silently skipped it — see
    # test_every_registry_row_is_covered_by_exactly_one_test_family for the guard that
    # now makes that omission loud. It fits this family rather than the hook one because
    # capability-gate.py is rc-shaped: measured rc=1 with a populated `matches` on a
    # must-block reason, rc=0 with `matches: []` on a genuinely-human one.
    "capability-gate-trigger",
    #  unit 2: rc-shaped like the rows above, but its must-trip input is a
    # blocker JSON on stdin, so the stubs in this family accept stdin_text.
    "blocker-create-gate-schema-probe",
)


def _row(name):
    for sig in canary.SIGNALS:
        if sig["name"] == name:
            return sig
    raise AssertionError(f"registry row {name!r} is absent — it would watch nothing")


def _inject(monkeypatch, rc, out, err):
    monkeypatch.setattr(canary, "_probe", lambda argv, stdin_text=None: (rc, out, err),
                        raising=True)


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


def test_gate_row_rc_one_with_empty_stdout_is_unevaluatable_not_alive(monkeypatch):
    """A gate that CRASHES exits 1 too, and rc alone cannot tell it from a refusal.

    guard-5430 in this row's own shape: "a block code of 1 is byte-identical to a
    crash, an ImportError, or a missing gate file". Before this branch existed the
    factory answered `dead is False` — "still refuses its must-trip input" — for a
    gate that died before argparse, which is the exact silent-dead-detector defect
    this canary exists to catch, living inside the canary.

    Measured (echo/cc-03 2026-09-21): one bad import into a real gate -> rc=1 with
    ZERO stdout and 252 stderr bytes; the same gate intact -> rc=1 with 720 bytes of
    its JSON. The empty-stdout half is the only one that moves.
    """
    _inject(monkeypatch, 1, "", "ModuleNotFoundError: No module named '_no_such_zz9'")
    for name in GATE_ROWS:
        dead, detail = _row(name)["assertion"]()
        assert dead is None, f"{name}: {detail}"
        assert "EMPTY stdout" in detail and "guard-5430" in detail, f"{name}: {detail}"
        assert "ModuleNotFoundError" in detail, f"{name}: stderr must reach the reader"


def test_gate_row_rc_one_with_a_payload_is_still_alive(monkeypatch):
    """THE OTHER HALF OF THE SPLIT, in the same pass as its twin above.

    The narrowing must not convert a live row into a permanent unevaluatable — that
    would delete the detector rather than fix it. rc=1 CARRYING the gate's verdict is
    a refusal and stays alive.
    """
    # checks[] is for the blocker row, which reads its own check's entry (guard-1082);
    # the argv rows ignore the key.
    _inject(monkeypatch, 1, '{"trigger_matched": "is not built", '
                            '"checks": [{"name": "schema_probe", "passed": false}]}', "")
    for name in GATE_ROWS:
        dead, detail = _row(name)["assertion"]()
        assert dead is False, f"{name}: {detail}"
        assert "still refuses" in detail


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
    def _boom(argv, stdin_text=None):
        raise OSError("interpreter vanished")
    monkeypatch.setattr(canary, "_probe", _boom, raising=True)
    for name in GATE_ROWS:
        dead, detail = _row(name)["assertion"]()
        assert dead is None and "could not run" in detail
        assert "interpreter vanished" in detail, detail


# ---- blocker-create-gate row ( unit 2): stdin input, check-3 fixture ----

def test_argv_rows_keep_their_one_argument_probe_call(monkeypatch):
    """stdin_text defaults to None, and then the factory calls _probe(argv) exactly as
    before. A one-argument stub would raise TypeError on any added keyword."""
    monkeypatch.setattr(canary, "_probe", lambda argv: (1, '{"trigger_matched": "x"}', ""),
                        raising=True)
    dead, detail = _row("zero-count-gate-trigger")["assertion"]()
    assert dead is False, detail


def test_blocker_row_sends_its_payload_on_stdin(monkeypatch):
    seen = {}

    def _capture(argv, stdin_text=None):
        seen["stdin"] = stdin_text
        return 1, '{"checks": [{"name": "schema_probe", "passed": false}]}', ""
    monkeypatch.setattr(canary, "_probe", _capture, raising=True)
    dead, detail = _row("blocker-create-gate-schema-probe")["assertion"]()
    assert dead is False, detail
    assert canary.json.loads(seen["stdin"]) == canary.BLOCKER_STAT_NEG_PAYLOAD


def test_blocker_payload_fails_exactly_check_3_on_the_real_gate():
    """The row reads rc=1 as check 3 refusing. That holds only while NO sibling check
    also fails on this payload, so pin it against the real gate."""
    import json
    import subprocess
    r = subprocess.run([sys.executable, str(SCRIPTS / "blocker-create-gate.py")],
                       input=json.dumps(canary.BLOCKER_STAT_NEG_PAYLOAD),
                       capture_output=True, text=True, timeout=60,
                       env={**os.environ, canary.LIVENESS_PROBE_ENV: "pytest"})
    assert r.returncode == 1, (r.returncode, r.stderr[:300])
    out = json.loads(r.stdout)
    failing = [c["name"] for c in out["checks"] if not c["passed"]]
    assert failing == ["schema_probe"], failing


def test_blocker_row_is_alive_against_the_real_gate():
    dead, detail = _row("blocker-create-gate-schema-probe")["assertion"]()
    assert dead is False, detail


def test_blocker_rotted_fixture_is_unevaluatable_not_dead():
    """Driven both ways against the real gate. The rotted reason with no fixture check
    reads DEAD (the false accusation). The fixture check turns it UNEVALUATABLE."""
    import json
    rotted = dict(canary.BLOCKER_STAT_NEG_PAYLOAD,
                  failure_reason="the utilization field is rarely populated")
    bare = canary._gate_trigger_assertion("blocker-create-gate.py", (), "x",
                                          stdin_text=json.dumps(rotted))
    assert bare()[0] is True
    checked = canary._gate_trigger_assertion(
        "blocker-create-gate.py", (), "x",
        fixture_check=canary._stat_neg_fixture_check(rotted),
        stdin_text=json.dumps(rotted))
    dead, detail = checked()
    assert dead is None and "fixture is stale" in detail, detail


def _blocker_verdict(**passed):
    import json
    return json.dumps({"checks": [{"name": n, "passed": p} for n, p in passed.items()]})


def test_blocker_row_dead_when_only_a_sibling_check_refuses(monkeypatch):
    """guard-1082: rc=1 carried by multi_signal while schema_probe PASSED its must-trip
    input is a dead check 3. The coarse rc read it alive."""
    _inject(monkeypatch, 1, _blocker_verdict(multi_signal=False, schema_probe=True), "")
    dead, detail = _row("blocker-create-gate-schema-probe")["assertion"]()
    assert dead is True, detail
    assert "schema_probe PASSED" in detail and "guard-1082" in detail, detail


def test_blocker_row_alive_when_check_3_refuses_beside_a_sibling(monkeypatch):
    _inject(monkeypatch, 1, _blocker_verdict(multi_signal=False, schema_probe=False), "")
    dead, detail = _row("blocker-create-gate-schema-probe")["assertion"]()
    assert dead is False, detail


def test_blocker_row_unreadable_verdict_is_unevaluatable_not_alive(monkeypatch):
    _inject(monkeypatch, 1, "blocked, see log", "")
    dead, detail = _row("blocker-create-gate-schema-probe")["assertion"]()
    assert dead is None and "which check refused is unknown" in detail, detail


def test_blocker_row_renamed_check_is_unevaluatable_not_dead(monkeypatch):
    _inject(monkeypatch, 1, _blocker_verdict(stat_neg_probe=False), "")
    dead, detail = _row("blocker-create-gate-schema-probe")["assertion"]()
    assert dead is None and "no check named 'schema_probe'" in detail, detail


def test_refusal_check_that_raises_is_unevaluatable(monkeypatch):
    def _broken_reader(out):
        raise RuntimeError("reader broke")
    _inject(monkeypatch, 1, _blocker_verdict(schema_probe=False), "")
    assertion = canary._gate_trigger_assertion("blocker-create-gate.py", (), "x",
                                               refusal_check=_broken_reader)
    dead, detail = assertion()
    assert dead is None and "refusal check itself failed (reader broke)" in detail, detail


def test_sibling_only_refusal_both_ways_against_the_real_gate():
    """Against the real gate: one evidence entry (multi_signal fails) and a
    non-statistical reason (schema_probe passes), so rc=1 comes from a sibling. The
    coarse assertion reads that as alive; the refusal check reads it as DEAD."""
    import json
    sibling_only = dict(canary.BLOCKER_STAT_NEG_PAYLOAD,
                        failure_reason="the service returned an error",
                        evidence=canary.BLOCKER_STAT_NEG_PAYLOAD["evidence"][:1])
    coarse = canary._gate_trigger_assertion("blocker-create-gate.py", (), "x",
                                            stdin_text=json.dumps(sibling_only))
    assert coarse()[0] is False
    strict = canary._gate_trigger_assertion(
        "blocker-create-gate.py", (), "x", stdin_text=json.dumps(sibling_only),
        refusal_check=canary._named_check_refused("schema_probe"))
    dead, detail = strict()
    assert dead is True and "['multi_signal']" in detail, detail


def test_empty_stat_neg_table_stays_loud_not_stale(tmp_path, monkeypatch):
    """guard-7231: an EMPTY pattern table must not be filed as a stale fixture, or a
    dead check 3 would read unevaluatable (silent under --quiet)."""
    gates = tmp_path / "gates"
    gates.mkdir()
    (gates / "blocker_create.py").write_text("_STAT_NEG_PATTERNS = []\n")
    monkeypatch.setattr(canary, "SCRIPT_DIR", tmp_path, raising=True)
    check = canary._stat_neg_fixture_check(canary.BLOCKER_STAT_NEG_PAYLOAD)
    assert check("blocker-create-gate.py", ()) is None


# ---- V-5 + V-7: the hook-shaped pair, and the factory they share --------------
# THESE TWO ROWS HAD ZERO TESTS IN EITHER DIRECTION until 2026-09-13, and the way
# that was measured is the point: greping the registry names against this file gave
# 1 hit each for the four GATE_ROWS names and 0 for marker-placement-gate-deny-channel
# and capability-gate-trigger, with the four hits serving as the positive control that
# the grep discriminates. So the fleet's detector for always-reports-clear instruments
# had 2 of its 9 rows with no positive control — an instance of its own class. V-6 is
# now in GATE_ROWS and the pair below covers the hook shape.
#
# The injector is SEPARATE from _inject on purpose: the hook factory calls _probe with
# a stdin_text KEYWORD, so _inject's one-positional lambda raises TypeError against it
# — which would read as a test failure about the factory rather than about the stub.

HOOK_GATE_ROWS = (
    "marker-placement-gate-deny-channel",
    "schedule-wakeup-gate-deny-channel",
)

# Rows with their own named tests rather than a tuple-driven family.
SELF_TESTED_ROWS = (
    "agent-watchdog-tick",
    "aspirations-query-read-channel",
    "aspirations-query-refusal-channel",
    #  unit 3. rc=0 is its trip — the inverse of GATE_ROWS — and its probe is
    # two calls, so neither family's stubs fit. Tests: the pending-deploys section below.
    "pending-deploys-has-pending",
    #  unit 4. rc is 0 whether or not it finds anything, so the verdict is the
    # stdout count line and a crash is DEAD. Tests: the findings-gate section below.
    "findings-gate-signal-scan",
    #  unit 5. rc=1 is its trip, as in GATE_ROWS, but a crash is DEAD here: the
    # reverse of that family's empty-stdout test. Tests: the routing-gate section below.
    "notification-routing-gate-suppress",
    #  unit 6. The probe builds its own throwaway world, and a crash is
    # UNEVALUATABLE: do_verify reads it as a refusal. Tests: the domain-suite section below.
    "domain-suite-gate-collect-refusal",
    #  unit 7. Two probes (the engine's CLI, then its consumer confirms_dormant),
    # and a crash is DEAD: the consumer swallows it as "not idle". Tests: the
    # liveness-check section at the end of this file.
    "liveness-check-dormant-verdict",
    #  unit 8. The probe builds its own git fixture and reads two channels (the
    # --json record, then the text banner); a crash is DEAD because __main__ turns it into
    # rc=0. Tests: the product-repo-freshness section at the end of this file.
    "product-repo-freshness-behind",
    #  unit 9. rc=1 naming the file is alive; a crash is UNEVALUATABLE (the hook
    # blocks on it); rc 0 is DEAD in three shapes. Tests: the hot-path section at the end.
    "hot-path-size-gate-growth-refusal",
    #  unit 10. A child imports the selector and prints marker lines; a swallowed
    # exception is DEAD, an import failure UNEVALUATABLE. Tests: the strategic-focus section
    # at the end.
    "goal-selector-strategic-focus-inert",
)

_DENY_JSON = '{"hookSpecificOutput": {"hookEventName": "PreToolUse", ' \
             '"permissionDecision": "deny", "permissionDecisionReason": "no"}}'


def _inject_hook(monkeypatch, rc, out, err):
    monkeypatch.setattr(canary, "_probe",
                        lambda argv, stdin_text=None: (rc, out, err), raising=True)


def test_every_registry_row_is_covered_by_exactly_one_test_family():
    """THE DRIFT GUARD, and the reason it exists is measured rather than imagined.

    capability-gate-trigger (V-6) was added to SIGNALS without being added to
    GATE_ROWS, so every tuple-driven test silently skipped it and nothing said so —
    a row with no positive control inside the instrument whose whole job is finding
    signals that cannot report non-clear. This assertion makes the next such omission
    fail at test time instead of never firing in production.
    """
    registered = {s["name"] for s in canary.SIGNALS}
    covered = set(GATE_ROWS) | set(HOOK_GATE_ROWS) | set(SELF_TESTED_ROWS)
    assert registered == covered, (
        "every SIGNALS row must sit in exactly one test family. "
        f"uncovered={sorted(registered - covered)} "
        f"named-but-unregistered={sorted(covered - registered)}"
    )
    assert not (set(GATE_ROWS) & set(HOOK_GATE_ROWS)), (
        "a row cannot be both rc-shaped and stdout-shaped — the two families assert "
        "opposite things about rc"
    )


def test_hook_gate_rows_are_live_against_the_real_gates():
    """No injection: drive both real hook gates with their real must-deny payloads.

    The plumbing half of the pair — path, interpreter, stdin shape, payload keys.
    """
    for name in HOOK_GATE_ROWS:
        dead, detail = _row(name)["assertion"]()
        assert dead is False, f"{name} is not live: {detail}"
        assert "still denies" in detail


def test_hook_gate_row_dead_on_empty_stdout_at_rc_zero(monkeypatch):
    """POSITIVE CONTROL, the load-bearing direction: rc=0 with no stdout is exactly
    what an APPROVING hook emits, so a gate that has stopped denying is
    indistinguishable from a healthy one by rc alone."""
    _inject_hook(monkeypatch, 0, "", "")
    for name in HOOK_GATE_ROWS:
        dead, detail = _row(name)["assertion"]()
        assert dead is True, f"{name}: {detail}"
        assert "stdout was EMPTY" in detail


def test_hook_gate_row_dead_on_a_non_deny_decision(monkeypatch):
    """The other DEAD shape: the hook still speaks the JSON contract but now says
    allow. Distinct from empty stdout, and it must not be mistaken for a parse error."""
    _inject_hook(monkeypatch, 0,
                 '{"hookSpecificOutput": {"permissionDecision": "allow"}}', "")
    for name in HOOK_GATE_ROWS:
        dead, detail = _row(name)["assertion"]()
        assert dead is True, f"{name}: {detail}"
        assert "decision='allow'" in detail


def test_hook_gate_row_live_when_the_decision_is_deny(monkeypatch):
    """The injected ALIVE direction, so the two stubs are proven to discriminate
    rather than the row being read as live by accident of the real gate's health."""
    _inject_hook(monkeypatch, 0, _DENY_JSON, "")
    for name in HOOK_GATE_ROWS:
        dead, detail = _row(name)["assertion"]()
        assert dead is False, f"{name}: {detail}"


def test_hook_gate_row_nonzero_rc_is_unevaluatable_not_dead(monkeypatch):
    """The PreToolUse contract is exit 0 always, so a non-zero rc means the hook
    CRASHED. A crash is a real defect but not THIS row's defect; naming it DEAD would
    send the remedy reader to the wrong file."""
    _inject_hook(monkeypatch, 1, "", "Traceback ...")
    for name in HOOK_GATE_ROWS:
        dead, detail = _row(name)["assertion"]()
        assert dead is None, f"{name}: {detail}"
        assert "rc=1" in detail and "exit 0 always" in detail


def test_hook_gate_row_unparseable_stdout_is_unevaluatable_not_dead(monkeypatch):
    """The narrowing this factory introduced over the plain function it replaced,
    pinned so it cannot be quietly reverted: off-contract stdout says NOTHING about
    whether the gate would deny, so it is unevaluatable, not an approval."""
    _inject_hook(monkeypatch, 0, "not json at all", "")
    for name in HOOK_GATE_ROWS:
        dead, detail = _row(name)["assertion"]()
        assert dead is None, f"{name}: {detail}"
        assert "not the PreToolUse JSON contract" in detail


def test_hook_gate_row_absent_script_is_unevaluatable_not_dead(tmp_path, monkeypatch):
    monkeypatch.setattr(canary, "SCRIPT_DIR", tmp_path, raising=False)
    for name in HOOK_GATE_ROWS:
        dead, detail = _row(name)["assertion"]()
        assert dead is None, f"{name}: {detail}"
        assert "absent at" in detail


def test_hook_gate_row_unevaluatable_when_the_probe_raises(monkeypatch):
    def _boom(argv, stdin_text=None):
        raise OSError("interpreter vanished")
    monkeypatch.setattr(canary, "_probe", _boom, raising=True)
    for name in HOOK_GATE_ROWS:
        dead, detail = _row(name)["assertion"]()
        assert dead is None and "could not run" in detail


def test_probe_marks_its_child_as_a_liveness_probe(monkeypatch):
    """: every probed gate logs its decision through _gate_log, so an
    unmarked must-trip probe lands in the retirement evaluator's input as a real
    `block` (measured: 1502 of 1515 exhaustive-search-gate firings in two days).
    The marker must reach the CHILD, where the gate's log() call runs — and it
    must not leak into this process's own environment."""
    monkeypatch.delenv(canary.LIVENESS_PROBE_ENV, raising=False)
    rc, out, _err = canary._probe([
        sys.executable, "-c",
        f"import os, sys; sys.stdout.write(os.environ.get({canary.LIVENESS_PROBE_ENV!r}, ''))",
    ])
    assert rc == 0
    assert out == "signal-liveness-canary"
    assert canary.LIVENESS_PROBE_ENV not in os.environ


def test_probe_drop_env_removes_a_var_from_the_child_only(monkeypatch):
    """The safety half of the pending-deploys row rests on this: a dropped var must be
    absent in the CHILD, still present here, and a call without drop_env unchanged."""
    monkeypatch.setenv("MIND_AGENT", "pytest-agent")
    show = [sys.executable, "-c",
            "import os, sys; sys.stdout.write(os.environ.get('MIND_AGENT', '<absent>'))"]
    assert canary._probe(show, drop_env=("MIND_AGENT",))[1] == "<absent>"
    assert canary._probe(show)[1] == "pytest-agent"
    assert os.environ["MIND_AGENT"] == "pytest-agent"


# ---- pending-deploys row ( unit 3): rc=0 is the trip, `add` builds the fixture --

PD_ROW = "pending-deploys-has-pending"


def _inject_pd(monkeypatch, add=(0, "", ""), has_pending=(0, "", ""), write=True, calls=None):
    """Stub both legs. `write` makes the add leg leave bytes in the store it was pointed
    at, as the real `add` does — the assertion reads the FILE, not add's rc."""
    def _stub(argv, stdin_text=None, drop_env=()):
        if calls is not None:
            calls.append((list(argv), tuple(drop_env)))
        if "add" in argv:
            if write:
                Path(argv[argv.index("--store") + 1]).write_text("- {repo: a/b}\n")
            return add
        return has_pending
    monkeypatch.setattr(canary, "_probe", _stub, raising=True)


def test_pd_row_is_alive_against_the_real_tracker():
    dead, detail = _row(PD_ROW)["assertion"]()
    assert dead is False, detail
    assert "still sees a recorded obligation" in detail


def test_pd_rotted_fixture_is_unevaluatable_not_dead():
    """Driven both ways against the real tracker. A bare repo is refused by the gate's
    own `add` (rc=2), so the store is empty and has-pending honestly says rc=1. Without
    the fixture check that reads DEAD — the false accusation. With it: UNEVALUATABLE."""
    rotted = dict(canary.PENDING_DEPLOYS_PROBE_ENTRY, repo="probe-only-signal-liveness-canary")
    assert canary._pending_obligation_assertion(rotted, check_fixture=False)()[0] is True
    dead, detail = canary._pending_obligation_assertion(rotted)()
    assert dead is None and "fixture is stale" in detail and "rc=2" in detail, detail


@pytest.mark.parametrize("breakage", [
    # No 'Traceback' header at all — why the row keys on stderr, not on that header.
    ("syntax", lambda src: src + "\ndef broken(:\n"),
    # add exits 1 and writes NOTHING, so this also pins the arc != 1 fall-through: a
    # fixture check that bailed here would call a dead module unevaluatable.
    ("import", lambda src: src.replace("import argparse\n",
                                       "import argparse\nimport zzz_canary_missing\n", 1)),
])
def test_pd_crash_of_the_real_module_is_dead_not_unevaluatable(tmp_path, monkeypatch, breakage):
    """A crash is DEAD for this row: the gate discards stderr and reads rc=1 as nothing
    pending, so a module that cannot load closes every deploy clean, silently."""
    _name, mutate = breakage
    src = (SCRIPTS / "pending-deploys.py").read_text(encoding="utf-8")
    (tmp_path / "pending-deploys.py").write_text(mutate(src), encoding="utf-8")
    monkeypatch.setattr(canary, "SCRIPT_DIR", tmp_path, raising=True)
    dead, detail = _row(PD_ROW)["assertion"]()
    assert dead is True, detail
    assert "CRASHED" in detail, detail


def test_pd_empty_read_of_a_written_store_is_dead(monkeypatch):
    _inject_pd(monkeypatch, has_pending=(1, "", ""))
    dead, detail = _row(PD_ROW)["assertion"]()
    assert dead is True, detail
    assert "read the recorded obligation as nothing pending" in detail
    assert "had just written" in detail


def test_pd_refused_call_shape_is_dead(monkeypatch):
    """rc=2 after `add` accepted --store means argparse refused has-pending --goal-id,
    which is the gate's own call — and the gate reads rc=2 as nothing pending too."""
    _inject_pd(monkeypatch, has_pending=(2, "", "usage: pending-deploys.py ..."))
    dead, detail = _row(PD_ROW)["assertion"]()
    assert dead is True and "refused the call shape" in detail, detail


def test_pd_unexpected_rc_is_unevaluatable(monkeypatch):
    _inject_pd(monkeypatch, has_pending=(3, "", ""))
    dead, detail = _row(PD_ROW)["assertion"]()
    assert dead is None and "unexpected rc=3" in detail, detail


def test_pd_writer_that_registers_nothing_is_unevaluatable_and_skips_the_reader(monkeypatch):
    calls = []
    _inject_pd(monkeypatch, add=(0, "", ""), write=False, calls=calls)
    dead, detail = _row(PD_ROW)["assertion"]()
    assert dead is None and "fixture is stale" in detail, detail
    assert len(calls) == 1, "the reader must not run when there is nothing for it to see"


def test_pd_absent_script_is_unevaluatable(tmp_path, monkeypatch):
    monkeypatch.setattr(canary, "SCRIPT_DIR", tmp_path, raising=True)
    dead, detail = _row(PD_ROW)["assertion"]()
    assert dead is None and "absent at" in detail, detail


def test_pd_probe_that_raises_is_unevaluatable(monkeypatch):
    def _boom(argv, stdin_text=None, drop_env=()):
        raise OSError("interpreter vanished")
    monkeypatch.setattr(canary, "_probe", _boom, raising=True)
    dead, detail = _row(PD_ROW)["assertion"]()
    assert dead is None and "could not run" in detail, detail


def test_pd_both_legs_use_an_isolated_store_and_strip_agent_identity(monkeypatch):
    """guard-1006: this probe WRITES. Both legs must name --store, and neither child may
    inherit an agent identity — so a tracker that stopped honouring --store would resolve
    no agent and write nowhere, instead of into the live store."""
    calls = []
    _inject_pd(monkeypatch, calls=calls)
    _row(PD_ROW)["assertion"]()
    assert [argv[argv.index("--store") + 2] for argv, _ in calls] == ["add", "has-pending"]
    for argv, dropped in calls:
        assert {"MIND_AGENT", "MIND_SID"} <= set(dropped), dropped
    reader = calls[1][0]
    assert reader[reader.index("--goal-id") + 1] == canary.PENDING_DEPLOYS_PROBE_ENTRY["goal_id"]


def test_pd_probe_removes_its_temp_store(monkeypatch):
    made = []
    real_mkdtemp = canary.tempfile.mkdtemp

    def _record(*a, **k):
        made.append(real_mkdtemp(*a, **k))
        return made[-1]
    monkeypatch.setattr(canary.tempfile, "mkdtemp", _record, raising=True)
    dead, detail = _row(PD_ROW)["assertion"]()
    assert dead is False, detail
    assert made and not Path(made[0]).exists()


def test_pd_fixture_ids_are_probe_only_not_allocatable():
    """guard-1094: a writing probe's ids must be ones no allocator can mint."""
    import re
    entry = canary.PENDING_DEPLOYS_PROBE_ENTRY
    assert "probe-only" in entry["repo"] and "probe-only" in entry["goal_id"]
    assert not re.fullmatch(r"g-\d+-\d+", entry["goal_id"])


# ---- findings-gate row ( unit 4): rc carries nothing, the count line is the verdict

FG_ROW = "findings-gate-signal-scan"
_ROTTED_INSIGHT = "A calm sentence with nothing in it."


def _inject_fg(monkeypatch, rc, out, err="", calls=None):
    """Stub the probe. `calls` records argv, the dropped env, and the insight file's text
    as the child would have read it (the file only exists during the call)."""
    def _stub(argv, stdin_text=None, drop_env=()):
        if calls is not None:
            insight = Path(argv[argv.index("--insight-file") + 1]).read_text(encoding="utf-8")
            calls.append((list(argv), tuple(drop_env), insight))
        return rc, out, err
    monkeypatch.setattr(canary, "_probe", _stub, raising=True)


def _copy_gate(tmp_path, monkeypatch, mutate=lambda src: src):
    """Relocate a (possibly mutated) copy of the real gate. PYTHONPATH carries the real
    helpers to the child, since the copy's own dir holds nothing but the gate."""
    src = (SCRIPTS / "findings-gate.py").read_text(encoding="utf-8")
    (tmp_path / "findings-gate.py").write_text(mutate(src), encoding="utf-8")
    monkeypatch.setattr(canary, "SCRIPT_DIR", tmp_path, raising=True)
    monkeypatch.setenv("PYTHONPATH", str(SCRIPTS))


def test_fg_row_is_alive_against_the_real_gate():
    dead, detail = _row(FG_ROW)["assertion"]()
    assert dead is False, detail
    assert "findings_count=1" in detail, detail


def test_fg_rotted_fixture_is_unevaluatable_not_dead(monkeypatch):
    """Driven both ways against the real gate. An insight the table no longer matches
    makes the gate honestly print findings_count=0. With the fixture check that is
    UNEVALUATABLE. With the check stubbed out it reads DEAD, which is the false
    accusation the check exists to prevent."""
    dead, detail = canary._findings_gate_assertion(_ROTTED_INSIGHT)()
    assert dead is None and "fixture is stale" in detail, detail
    monkeypatch.setattr(canary, "_findings_fixture_check",
                        lambda insight: (lambda script_name, args: None), raising=True)
    dead, detail = canary._findings_gate_assertion(_ROTTED_INSIGHT)()
    assert dead is True and "findings_count=0" in detail, detail


def test_fg_literal_the_table_matches_but_the_scan_rejects_reads_dead():
    """By design: the fixture check reads only the TABLE, because scan_signals' own
    filters are part of what this row watches. So wording the table matches but the scan
    rejects reads DEAD, never stale, which is the loud direction (guard-7231). Measured
    against the real gate: this sentence reads findings_count=0 while the table still
    matches it. Keep FINDINGS_GATE_PROBE_INSIGHT a plain, un-negated clause."""
    wording = "A calm sentence that names no defect at all."
    assert canary._findings_fixture_check(wording)("findings-gate.py", ()) is None
    dead, detail = canary._findings_gate_assertion(wording)()
    assert dead is True and "findings_count=0" in detail, detail


def test_fg_relocated_unmutated_copy_is_alive(tmp_path, monkeypatch):
    """The positive control for the two crash tests below: the copy and its PYTHONPATH
    work, so a DEAD there comes from the mutation and not from the relocation."""
    _copy_gate(tmp_path, monkeypatch)
    dead, detail = _row(FG_ROW)["assertion"]()
    assert dead is False, detail


@pytest.mark.parametrize("breakage", [
    ("syntax", lambda src: src + "\ndef broken(:\n"),
    ("import", lambda src: src.replace("import sys\n", "import sys\nimport zzz_canary_missing\n", 1)),
])
def test_fg_crash_of_the_real_gate_is_dead_not_unevaluatable(tmp_path, monkeypatch, breakage):
    """guard-7231: the pattern table lives IN the gate file, so a gate that cannot import
    must not be excused as a stale fixture. Its callers read a missing count as a clean
    close, so this is DEAD."""
    _name, mutate = breakage
    _copy_gate(tmp_path, monkeypatch, mutate)
    dead, detail = _row(FG_ROW)["assertion"]()
    assert dead is True, detail
    assert "CRASHED" in detail, detail


def test_fg_scan_that_stops_reporting_is_dead(tmp_path, monkeypatch):
    """The table still matches the literal and the process exits 0, but scan_signals
    returns nothing. This is exactly the failure rc cannot show."""
    head = "def scan_signals(insight_text, allowed_types=None):"
    _copy_gate(tmp_path, monkeypatch, lambda src: src.replace(
        head, head + "\n    return []\n\n\ndef _canary_unused_scan_signals(insight_text, allowed_types=None):", 1))
    dead, detail = _row(FG_ROW)["assertion"]()
    assert dead is True, detail
    assert "findings_count=0" in detail and "scan_signals" in detail, detail


def test_fg_missing_count_line_is_dead_even_at_rc_zero(monkeypatch):
    _inject_fg(monkeypatch, 0, "", "")
    dead, detail = _row(FG_ROW)["assertion"]()
    assert dead is True and "no findings_count line" in detail, detail


def test_fg_zero_count_is_dead(monkeypatch):
    _inject_fg(monkeypatch, 0, "findings_count=0 created=0\n")
    dead, detail = _row(FG_ROW)["assertion"]()
    assert dead is True and "findings_count=0" in detail, detail


def test_fg_absent_script_is_unevaluatable(tmp_path, monkeypatch):
    monkeypatch.setattr(canary, "SCRIPT_DIR", tmp_path, raising=True)
    dead, detail = _row(FG_ROW)["assertion"]()
    assert dead is None and "absent at" in detail, detail


def test_fg_probe_that_raises_is_unevaluatable(monkeypatch):
    def _boom(argv, stdin_text=None, drop_env=()):
        raise OSError("interpreter vanished")
    monkeypatch.setattr(canary, "_probe", _boom, raising=True)
    dead, detail = _row(FG_ROW)["assertion"]()
    assert dead is None and "could not run" in detail, detail


def test_fg_probe_is_dry_run_strips_identity_and_sends_the_one_literal(monkeypatch):
    """Side-effect free by construction: --dry-run files no goals, and with no agent
    identity the gate resolves no agent dir, so it never regenerates a live compact file.
    The insight the child reads is the SAME constant the fixture check vets."""
    calls = []
    _inject_fg(monkeypatch, 0, "findings_count=1 created=1\n", calls=calls)
    dead, detail = _row(FG_ROW)["assertion"]()
    assert dead is False, detail
    (argv, dropped, insight), = calls
    assert "--dry-run" in argv
    assert {"MIND_AGENT", "MIND_SID"} <= set(dropped), dropped
    assert argv[argv.index("--goal") + 1].startswith("probe-only")
    assert insight == canary.FINDINGS_GATE_PROBE_INSIGHT


def test_fg_probe_removes_its_temp_dir(monkeypatch):
    made = []
    real_mkdtemp = canary.tempfile.mkdtemp

    def _record(*a, **k):
        made.append(real_mkdtemp(*a, **k))
        return made[-1]
    monkeypatch.setattr(canary.tempfile, "mkdtemp", _record, raising=True)
    dead, detail = _row(FG_ROW)["assertion"]()
    assert dead is False, detail
    assert made and not Path(made[0]).exists()

# ---- notification-routing-gate-suppress ( unit 5) -------------------

NRG_ROW = "notification-routing-gate-suppress"
_NRG_FLEET_LINE = 'FLEET_HANDLEABLE_CATEGORIES = frozenset({"info", "update", "completion", "blocker"})'
_NRG_DECIDE_LINE = '    cat = (category or "").strip().lower()\n'


def _inject_nrg(monkeypatch, rc, out, err="", calls=None):
    def _stub(argv, stdin_text=None, drop_env=()):
        if calls is not None:
            calls.append(list(argv))
        return rc, out, err
    monkeypatch.setattr(canary, "_probe", _stub, raising=True)


def _copy_nrg(tmp_path, monkeypatch, mutate=lambda src: src):
    """Relocate a (possibly mutated) copy of the real gate, as _copy_gate does. Returns
    whether the mutation changed anything, so no test can pass on a no-op mutation.

    sys.path is restored after the test: the fixture check imports the copy, the copy
    prepends its own dir, and a later `import notification_routing_gate` in this process
    would otherwise load the mutated copy."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    src = (SCRIPTS / "notification_routing_gate.py").read_text(encoding="utf-8")
    mutated = mutate(src)
    (tmp_path / "notification_routing_gate.py").write_text(mutated, encoding="utf-8")
    monkeypatch.setattr(canary, "SCRIPT_DIR", tmp_path, raising=True)
    monkeypatch.setenv("PYTHONPATH", str(SCRIPTS))
    return mutated != src


def _nrg_assertion(args, fixture_check=None):
    return canary._gate_trigger_assertion(
        "notification_routing_gate.py", args, "a fleet-handleable status report",
        fixture_check=fixture_check, refusal_check=canary._routing_suppress_line,
        crash_is_dead=True)


def test_nrg_row_is_alive_against_the_real_gate():
    dead, detail = _row(NRG_ROW)["assertion"]()
    assert dead is False, detail


@pytest.mark.parametrize("rotted", [
    ("--category", "decision-needed", "--subject", "x", "--body", "y"),
    ("--category", "zzz-unknown-category", "--subject", "x", "--body", "y"),
])
def test_nrg_rotted_fixture_is_unevaluatable_not_dead(rotted):
    """Driven both ways against the real gate. A category the tables now send makes the
    gate honestly SEND. With the fixture check that is UNEVALUATABLE. Without the check it
    reads DEAD, which is the false accusation the check exists to prevent."""
    dead, detail = _nrg_assertion(rotted, canary._routing_fixture_check)()
    assert dead is None and "fixture is stale" in detail, detail
    dead, detail = _nrg_assertion(rotted)()
    assert dead is True and "rc=0" in detail, detail


def test_nrg_relocated_unmutated_copy_is_alive(tmp_path, monkeypatch):
    """The positive control for the mutation tests below: the relocation itself works, so
    a DEAD there comes from the mutation."""
    assert _copy_nrg(tmp_path, monkeypatch) is False
    dead, detail = _row(NRG_ROW)["assertion"]()
    assert dead is False, detail


@pytest.mark.parametrize("breakage", [
    ("syntax", lambda src: src + "\ndef broken(:\n"),
    ("import", lambda src: src.replace("import re\n", "import re\nimport zzz_canary_missing\n", 1)),
])
def test_nrg_crash_of_the_real_gate_is_dead_not_unevaluatable(tmp_path, monkeypatch, breakage):
    """guard-7231, measured (bravo/cc-05 2026-09-23): the shell wrapper passes a crash's
    rc=1 through as SUPPRESS, so an import-time crash turned a decision-needed
    notification into a skipped send. A crash therefore drops every shell-lane
    notification in silence, and the row must not file it as unevaluatable."""
    _name, mutate = breakage
    assert _copy_nrg(tmp_path, monkeypatch, mutate) is True
    dead, detail = _row(NRG_ROW)["assertion"]()
    assert dead is True, detail
    assert "CRASHED" in detail, detail


def test_nrg_gate_that_stops_suppressing_is_dead(tmp_path, monkeypatch):
    """An EMPTY fleet-handleable set suppresses nothing, which floods the owner's inbox.
    The fixture check must let that through to DEAD, not call it a stale fixture."""
    assert _copy_nrg(tmp_path, monkeypatch, lambda src: src.replace(
        _NRG_FLEET_LINE, "FLEET_HANDLEABLE_CATEGORIES = frozenset()", 1)) is True
    dead, detail = _row(NRG_ROW)["assertion"]()
    assert dead is True and "SEND" in detail, detail


def test_nrg_decide_that_raises_is_dead(tmp_path, monkeypatch):
    """decide_and_log SENDs when decide() raises, so rc=0 and the reason names the raise."""
    assert _copy_nrg(tmp_path, monkeypatch, lambda src: src.replace(
        _NRG_DECIDE_LINE,
        '    raise RuntimeError("canary mutation")\n' + _NRG_DECIDE_LINE, 1)) is True
    dead, detail = _row(NRG_ROW)["assertion"]()
    assert dead is True and "routing gate raised" in detail, detail


def test_nrg_rc_one_with_empty_stdout_is_dead(monkeypatch):
    _inject_nrg(monkeypatch, 1, "", "Traceback (most recent call last):\nImportError: x\n")
    dead, detail = _row(NRG_ROW)["assertion"]()
    assert dead is True and "CRASHED" in detail, detail


def test_nrg_rc_one_without_a_suppress_line_is_dead(monkeypatch):
    _inject_nrg(monkeypatch, 1, "warning: printed before dying\n", "Traceback ...\n")
    dead, detail = _row(NRG_ROW)["assertion"]()
    assert dead is True and "no 'SUPPRESS:' verdict line" in detail, detail


def test_nrg_rc_one_with_the_suppress_line_is_alive(monkeypatch):
    _inject_nrg(monkeypatch, 1, "SUPPRESS: category 'info' is a status report\n  route to: x\n")
    dead, detail = _row(NRG_ROW)["assertion"]()
    assert dead is False, detail


def test_nrg_rc_zero_is_dead(monkeypatch):
    _inject_nrg(monkeypatch, 0, "SEND: unknown category\n")
    dead, detail = _row(NRG_ROW)["assertion"]()
    assert dead is True and "rc=0" in detail, detail


def test_nrg_unexpected_rc_is_unevaluatable(monkeypatch):
    _inject_nrg(monkeypatch, 2, "", "usage: error\n")
    dead, detail = _row(NRG_ROW)["assertion"]()
    assert dead is None, detail


def test_nrg_absent_script_is_unevaluatable(tmp_path, monkeypatch):
    monkeypatch.setattr(canary, "SCRIPT_DIR", tmp_path, raising=True)
    dead, detail = _row(NRG_ROW)["assertion"]()
    assert dead is None and "absent" in detail, detail


def test_nrg_probe_that_raises_is_unevaluatable(monkeypatch):
    def _boom(argv, stdin_text=None, drop_env=()):
        raise OSError("cannot spawn")
    monkeypatch.setattr(canary, "_probe", _boom, raising=True)
    dead, detail = _row(NRG_ROW)["assertion"]()
    assert dead is None and "could not run" in detail, detail


def test_nrg_probe_sends_the_fixture_and_no_side_effect_flag(monkeypatch):
    """--breadcrumb would post to the live findings board, and --quiet would remove the
    SUPPRESS line this row reads. The probe must pass neither."""
    calls = []
    _inject_nrg(monkeypatch, 1, "SUPPRESS: x\n", calls=calls)
    _row(NRG_ROW)["assertion"]()
    assert len(calls) == 1
    argv = calls[0]
    assert "--breadcrumb" not in argv and "--quiet" not in argv
    n = len(canary.ROUTING_GATE_PROBE_ARGS)
    assert tuple(argv[-n:]) == canary.ROUTING_GATE_PROBE_ARGS


# ---- domain-suite-gate-collect-refusal ( unit 6) --------------------

DSG_ROW = "domain-suite-gate-collect-refusal"
_DSG_EVALUATE_LINE = "    scripts_dir = _scripts_dir(world_dir)\n"
_DSG_PASS_LINE = "    if rc in (0, 5):\n"


def _dsg_out(decision, reason, fixture=True):
    """One gate verdict line. fixture=False names a file the probe's fixture does not have."""
    name = canary.DOMAIN_SUITE_FIXTURE_TEST if fixture else "test_live_world.py"
    return json.dumps({"gate": "domain-suite-gate", "decision": decision,
                       "goal_id": "probe-only-signal-liveness-canary", "reason": reason,
                       "touched": [[f"tests/{name}", "2026-09-23T20:00:00"]]}) + "\n"


def _inject_dsg(monkeypatch, rc, out, err="", calls=None):
    def _stub(argv, stdin_text=None, drop_env=(), extra_env=None):
        if calls is not None:
            calls.append((list(argv), dict(extra_env or {})))
        return rc, out, err
    monkeypatch.setattr(canary, "_probe", _stub, raising=True)


def _copy_dsg(tmp_path, monkeypatch, mutate=lambda src: src):
    """Relocate a (possibly mutated) copy of the real gate, as _copy_nrg does, and return
    whether the mutation changed anything. sys.path is restored for the same reason."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    src = (SCRIPTS / "domain-suite-gate.py").read_text(encoding="utf-8")
    mutated = mutate(src)
    (tmp_path / "domain-suite-gate.py").write_text(mutated, encoding="utf-8")
    monkeypatch.setattr(canary, "SCRIPT_DIR", tmp_path, raising=True)
    monkeypatch.setenv("PYTHONPATH", str(SCRIPTS))
    return mutated != src


def test_dsg_row_is_alive_against_the_real_gate_and_leaves_nothing_behind(monkeypatch):
    """The positive control, measured through a spy on the real _probe: the gate's
    retained log went into the probe's own dir, and that dir is gone afterwards."""
    real, seen = canary._probe, []

    def _spy(argv, stdin_text=None, drop_env=(), extra_env=None):
        rc, out, err = real(argv, stdin_text, drop_env, extra_env)
        seen.append((dict(extra_env or {}), out))
        return rc, out, err
    monkeypatch.setattr(canary, "_probe", _spy, raising=True)
    dead, detail = _row(DSG_ROW)["assertion"]()
    assert dead is False and "could not COLLECT" in detail, detail
    (env, out), = seen
    log = canary._domain_suite_verdict(out).get("log")
    assert log and Path(log).parent == Path(env["DOMAIN_SUITE_LOG_DIR"]), log
    assert not Path(env["MIND_WORLD"]).parent.exists()


def test_dsg_rotted_fixture_is_unevaluatable_not_dead(monkeypatch):
    """Driven both ways against the real gate. If the 'missing' module exists, the fixture
    suite collects and the gate honestly passes. With the fixture check that is
    UNEVALUATABLE. Without it the row reads DEAD, the false accusation the check prevents."""
    monkeypatch.setattr(canary, "DOMAIN_SUITE_UNCOLLECTABLE_MODULE", "json", raising=True)
    dead, detail = canary._domain_suite_gate_assertion()()
    assert dead is None and "fixture is stale" in detail, detail
    dead, detail = canary._domain_suite_gate_assertion(check_fixture=False)()
    assert dead is True and "passed a close" in detail, detail


def test_dsg_relocated_unmutated_copy_is_alive(tmp_path, monkeypatch):
    """The positive control for the mutation tests below."""
    assert _copy_dsg(tmp_path, monkeypatch) is False
    dead, detail = _row(DSG_ROW)["assertion"]()
    assert dead is False, detail


def test_dsg_crash_of_the_real_gate_is_unevaluatable_not_dead(tmp_path, monkeypatch):
    """A gate that cannot start exits rc=1 with no verdict line, and do_verify reads rc=1
    as REFUSED: every close fails closed with a traceback, which is loud, not silent."""
    assert _copy_dsg(tmp_path, monkeypatch, lambda src: src + "\ndef broken(:\n") is True
    dead, detail = _row(DSG_ROW)["assertion"]()
    assert dead is None and "no verdict line" in detail, detail


def test_dsg_evaluate_that_raises_is_dead(tmp_path, monkeypatch):
    """main() catches it and fails open, so every close passes with the suite unverified."""
    assert _copy_dsg(tmp_path, monkeypatch, lambda src: src.replace(
        _DSG_EVALUATE_LINE,
        '    raise RuntimeError("canary mutation")\n' + _DSG_EVALUATE_LINE, 1)) is True
    dead, detail = _row(DSG_ROW)["assertion"]()
    assert dead is True and "canary mutation" in detail, detail


def test_dsg_gate_that_passes_a_collection_error_is_dead(tmp_path, monkeypatch):
    assert _copy_dsg(tmp_path, monkeypatch, lambda src: src.replace(
        _DSG_PASS_LINE, "    if rc in (0, 2, 5):\n", 1)) is True
    dead, detail = _row(DSG_ROW)["assertion"]()
    assert dead is True and "passed a close" in detail, detail


@pytest.mark.parametrize("rc, out, verdict, needle", [
    (1, _dsg_out("block", "domain suite could not COLLECT (rc=2: x)"), False, "COLLECT"),
    (0, _dsg_out("block", "domain suite could not COLLECT (rc=2: x)"), True, "exited rc=0"),
    (0, _dsg_out("pass", "domain suite green"), True, "passed a close"),
    (0, _dsg_out("noop", "no domain test suite under world scripts"), True, "noop"),
    (0, _dsg_out("error", "gate error, fail-open: KeyError: x"), True, "failed open"),
    (0, _dsg_out("error", "domain suite exceeded 20s — not a verdict"), None, "fault branch"),
    (1, _dsg_out("block", "1 credential-shaped file(s) ... CHANGED CONTENT"), None, "tripwire"),
    (1, _dsg_out("block", "domain suite could not COLLECT", fixture=False), None, "other world"),
    (0, _dsg_out("pass", "domain suite green", fixture=False), None, "other world"),
    (1, "", None, "no verdict line"),
    (0, _dsg_out("zzz", "x"), None, "unrecognised"),
])
def test_dsg_verdict_table(monkeypatch, rc, out, verdict, needle):
    _inject_dsg(monkeypatch, rc, out, "Traceback ...\n")
    dead, detail = _row(DSG_ROW)["assertion"]()
    assert dead is verdict and needle in detail, detail


def test_dsg_absent_script_is_unevaluatable(tmp_path, monkeypatch):
    monkeypatch.setattr(canary, "SCRIPT_DIR", tmp_path, raising=True)
    dead, detail = _row(DSG_ROW)["assertion"]()
    assert dead is None and "absent" in detail, detail


def test_dsg_probe_that_raises_is_unevaluatable(monkeypatch):
    def _boom(argv, stdin_text=None, drop_env=(), extra_env=None):
        raise OSError("cannot spawn")
    monkeypatch.setattr(canary, "_probe", _boom, raising=True)
    dead, detail = _row(DSG_ROW)["assertion"]()
    assert dead is None and "could not run" in detail, detail


def test_dsg_probe_points_the_gate_at_its_own_throwaway_world(monkeypatch):
    """No --override (that would write the override ledger), the fixture args last, and
    both seams inside one temp root that is removed after the call."""
    calls = []
    _inject_dsg(monkeypatch, 1, _dsg_out("block", "domain suite could not COLLECT"), calls=calls)
    _row(DSG_ROW)["assertion"]()
    (argv, env), = calls
    assert "--override" not in argv
    n = len(canary.DOMAIN_SUITE_PROBE_ARGS)
    assert tuple(argv[-n:]) == canary.DOMAIN_SUITE_PROBE_ARGS
    world, logs = Path(env["MIND_WORLD"]), Path(env["DOMAIN_SUITE_LOG_DIR"])
    assert world.name == "world" and logs.parent == world.parent
    assert env["STORAGE_BACKEND"] == "local"
    assert not world.parent.exists()


def test_probe_extra_env_reaches_the_child_but_cannot_unset_the_liveness_marker():
    rc, out, _err = canary._probe(
        [sys.executable, "-c",
         f"import os; print(os.environ.get('ZZZ_CANARY_X'), os.environ.get('{canary.LIVENESS_PROBE_ENV}'))"],
        extra_env={"ZZZ_CANARY_X": "1", canary.LIVENESS_PROBE_ENV: "overridden"})
    assert rc == 0 and out.split() == ["1", "signal-liveness-canary"], out


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


# ---- liveness-check-dormant-verdict ( unit 7) -----------------------

LCK_ROW = "liveness-check-dormant-verdict"
_LCK_DORMANT_RETURN = '        return {"verdict": "dormant", "signal": None,\n'
_LCK_SIGNATURE = "threshold_hours=DEFAULT_THRESHOLD_HOURS, now=None,"


def _lck_cli(verdict, reason="r"):
    return json.dumps({"verdict": verdict, "reason": reason,
                       "agent": canary.LIVENESS_PROBE_AGENT}) + "\n"


def _inject_lck(monkeypatch, results, calls=None):
    """Stub _probe with one (rc, out, err) per call, in call order."""
    queue = list(results)

    def _stub(argv, stdin_text=None, drop_env=(), extra_env=None):
        if calls is not None:
            calls.append((list(argv), dict(extra_env or {})))
        return queue.pop(0)
    monkeypatch.setattr(canary, "_probe", _stub, raising=True)


def _copy_lck(tmp_path, monkeypatch, mutate=lambda src: src):
    """Relocate a (possibly mutated) copy of the real engine, as _copy_dsg does. The
    consumer child puts SCRIPT_DIR first on sys.path, so confirms_dormant imports the COPY,
    while gates/ and _team_state still resolve through PYTHONPATH."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    src = (SCRIPTS / "liveness_check.py").read_text(encoding="utf-8")
    mutated = mutate(src)
    (tmp_path / "liveness_check.py").write_text(mutated, encoding="utf-8")
    monkeypatch.setattr(canary, "SCRIPT_DIR", tmp_path, raising=True)
    monkeypatch.setenv("PYTHONPATH", str(SCRIPTS))
    return mutated != src


def test_lck_row_is_alive_against_the_real_engine_and_leaves_nothing_behind(monkeypatch):
    """The positive control, through a spy on the real _probe: two children (the CLI, then
    the consumer), both pinned to the local backend, both reading the same throwaway world
    while its fixture shard exists, and that world is gone afterwards."""
    real, seen = canary._probe, []

    def _spy(argv, stdin_text=None, drop_env=(), extra_env=None):
        world = argv[argv.index("--world-dir") + 1] if "--world-dir" in argv else argv[-1]
        shard = Path(world) / "team-state" / "agents" / f"{canary.LIVENESS_PROBE_AGENT}.yaml"
        seen.append((list(argv), dict(extra_env or {}), world, shard.is_file()))
        return real(argv, stdin_text, drop_env, extra_env)
    monkeypatch.setattr(canary, "_probe", _spy, raising=True)
    dead, detail = _row(LCK_ROW)["assertion"]()
    assert dead is False and "confirms_dormant agreed" in detail, detail
    assert len(seen) == 2, seen
    (cli_argv, cli_env, cli_world, cli_fixture), (con_argv, con_env, con_world, con_fixture) = seen
    assert cli_argv[1].endswith("liveness_check.py") and "--json" in cli_argv
    assert con_argv[1] == "-c" and "confirms_dormant" in con_argv[2]
    assert cli_env == con_env == {"STORAGE_BACKEND": "local"}
    assert cli_world == con_world and cli_fixture and con_fixture
    assert not Path(cli_world).exists()


def test_lck_rotted_fixture_is_unevaluatable_not_dead(monkeypatch):
    """Driven both ways against the real engine. A fixture stamped an hour ago is honestly
    NOT dormant. With the fixture check that is UNEVALUATABLE; without it the row reads
    DEAD, the false accusation the check exists to prevent."""
    import datetime as _dt
    recent = (_dt.datetime.now() - _dt.timedelta(hours=1)).isoformat(timespec="seconds")
    monkeypatch.setattr(canary, "LIVENESS_PROBE_STALE", recent, raising=True)
    dead, detail = canary._liveness_dormant_assertion()()
    assert dead is None and "fixture is stale" in detail, detail
    dead, detail = canary._liveness_dormant_assertion(check_fixture=False)()
    assert dead is True and "can no longer conclude dormant" in detail, detail


def test_lck_relocated_unmutated_copy_is_alive(tmp_path, monkeypatch):
    """The positive control for the mutation tests below."""
    assert _copy_lck(tmp_path, monkeypatch) is False
    dead, detail = _row(LCK_ROW)["assertion"]()
    assert dead is False, detail


@pytest.mark.parametrize("mutate", [
    lambda src: src + "\ndef broken(:\n",
    lambda src: "import zzz_canary_missing_module  # noqa: F401\n" + src,
])
def test_lck_crash_of_the_real_engine_is_dead_not_unevaluatable(tmp_path, monkeypatch, mutate):
    """confirms_dormant swallows the same crash as 'not idle', so a crash is silent where it
    decides anything: DEAD here (guard-7231)."""
    assert _copy_lck(tmp_path, monkeypatch, mutate) is True
    dead, detail = _row(LCK_ROW)["assertion"]()
    assert dead is True and "CRASHED" in detail, detail


def test_lck_engine_that_stops_concluding_dormant_is_dead(tmp_path, monkeypatch):
    assert _copy_lck(tmp_path, monkeypatch, lambda src: src.replace(
        _LCK_DORMANT_RETURN, _LCK_DORMANT_RETURN.replace('"dormant"', '"unknown"'), 1)) is True
    dead, detail = _row(LCK_ROW)["assertion"]()
    assert dead is True and "'unknown'" in detail, detail


def test_lck_consumer_that_swallows_an_error_is_dead_while_the_cli_stays_dormant(tmp_path, monkeypatch):
    """The silent death this row exists for. decide_liveness's threshold becomes
    positional-only: main() passes it positionally and keeps saying dormant, while
    confirms_dormant passes threshold_hours= by keyword, raises TypeError inside its own
    try, and returns False for every agent from then on."""
    assert _copy_lck(tmp_path, monkeypatch, lambda src: src.replace(
        _LCK_SIGNATURE, "threshold_hours=DEFAULT_THRESHOLD_HOURS, /, now=None,", 1)) is True
    dead, detail = _row(LCK_ROW)["assertion"]()
    assert dead is True and "swallowing an error" in detail, detail


@pytest.mark.parametrize("results,verdict,needle", [
    ([(1, "", "Traceback\nNameError: x\n")], True, "CRASHED"),
    ([(0, "not json\n", "")], True, "CRASHED"),
    ([(0, _lck_cli("unknown"), "")], True, "'unknown'"),
    ([(0, _lck_cli("alive"), "")], True, "'alive'"),
    ([(0, _lck_cli("dormant"), ""), (1, "", "ModuleNotFoundError: gates\n")], True, "could not run"),
    ([(0, _lck_cli("dormant"), ""), (0, "", "")], True, "could not run"),
    ([(0, _lck_cli("dormant"), ""), (0, "CONFIRMS_DORMANT=False\n", "")], True, "swallowing an error"),
    ([(0, _lck_cli("dormant"), ""), (0, "CONFIRMS_DORMANT=True\n", "")], False, "agreed"),
])
def test_lck_verdict_table(monkeypatch, results, verdict, needle):
    _inject_lck(monkeypatch, results)
    dead, detail = _row(LCK_ROW)["assertion"]()
    assert dead is verdict and needle in detail, detail


def test_lck_consumer_runs_only_after_the_cli_concludes_dormant(monkeypatch):
    calls = []
    _inject_lck(monkeypatch, [(0, _lck_cli("unknown"), "")], calls)
    _row(LCK_ROW)["assertion"]()
    assert len(calls) == 1 and calls[0][0][1].endswith("liveness_check.py"), calls


def test_lck_absent_script_is_unevaluatable(tmp_path, monkeypatch):
    monkeypatch.setattr(canary, "SCRIPT_DIR", tmp_path, raising=True)
    dead, detail = _row(LCK_ROW)["assertion"]()
    assert dead is None and "absent" in detail, detail


def test_lck_probe_that_raises_is_unevaluatable(monkeypatch):
    def _boom(argv, stdin_text=None, drop_env=(), extra_env=None):
        raise OSError("canary spawn failure")
    monkeypatch.setattr(canary, "_probe", _boom, raising=True)
    dead, detail = _row(LCK_ROW)["assertion"]()
    assert dead is None and "could not run" in detail, detail


def test_lck_fixture_check_does_not_call_an_unimportable_engine_rot(tmp_path, monkeypatch):
    """An engine that cannot import cannot judge liveness either, so the check returns None
    and lets the probe report the crash as DEAD (guard-7231)."""
    import datetime as _dt
    (tmp_path / "liveness_check.py").write_text("def broken(:\n", encoding="utf-8")
    monkeypatch.setattr(canary, "SCRIPT_DIR", tmp_path, raising=True)
    assert canary._liveness_fixture_check(tmp_path, _dt.datetime.now()) is None


# ---- product-repo-freshness-behind ( unit 8) ------------------------

PRF_ROW = "product-repo-freshness-behind"
_PRF_REC = {"name": canary.FRESHNESS_PROBE_REPO, "behind": canary.FRESHNESS_PROBE_BEHIND,
            "ahead": 0, "verdict": "behind", "detail": ""}
_PRF_BANNER = ("[product-repo-freshness] 1 of 1 repo(s) need attention BEFORE you read or "
               f"edit them:\n  BEHIND  {canary.FRESHNESS_PROBE_REPO} by 2 commit(s) on main\n")
# The four source anchors the mutants below rewrite, each unique in the gate.
_PRF_MAIN_RECORDS = "    records = [freshness(r, do_fetch=not args.no_fetch) for r in selected]\n"
_PRF_RANGE = '"%s...HEAD" % upstream'
_PRF_COUNTS = "behind, ahead = (int(x) for x in counts.split())"
_PRF_CLEAN = 'CLEAN_VERDICTS = ("in-sync", "ahead-topological")'


def _prf_json(behind=canary.FRESHNESS_PROBE_BEHIND, cannot_check=False, records=None, reason=None):
    if records is None:
        records = [dict(_PRF_REC, behind=behind, verdict="behind" if behind else "in-sync")]
    return json.dumps({"enumerated_count": 0 if cannot_check else 1,
                       "selected_count": len(records), "goal_lookup_ok": True,
                       "cannot_check": cannot_check, "cannot_check_reason": reason,
                       "records": records}, indent=2) + "\n"


def _copy_prf(tmp_path, monkeypatch, mutate=lambda src: src):
    """Relocate a (possibly mutated) copy of the real gate. Its own sys.path insert puts the
    copy's directory first, so _path_roots and _paths resolve through PYTHONPATH; the fixture
    check's in-process import makes the same insert, hence the sys.path snapshot."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    src = (SCRIPTS / "product-repo-freshness.py").read_text(encoding="utf-8")
    mutated = mutate(src)
    (tmp_path / "product-repo-freshness.py").write_text(mutated, encoding="utf-8")
    monkeypatch.setattr(canary, "SCRIPT_DIR", tmp_path, raising=True)
    monkeypatch.setenv("PYTHONPATH", str(SCRIPTS))
    return mutated != src


def test_prf_row_is_alive_against_the_real_gate_and_leaves_nothing_behind(monkeypatch):
    """The positive control, through a spy on the real _probe: two children in the production
    call shape (the --json record, then the text banner), neither pinned nor stripped of any
    variable, both reading the same fixture while it exists, and the fixture is gone after."""
    real, seen = canary._probe, []

    def _spy(argv, stdin_text=None, drop_env=(), extra_env=None):
        repo = Path(argv[argv.index("--repo") + 1])
        seen.append((list(argv), dict(extra_env or {}), tuple(drop_env), repo,
                     (repo / ".git").is_dir()))
        return real(argv, stdin_text, drop_env, extra_env)
    monkeypatch.setattr(canary, "_probe", _spy, raising=True)
    dead, detail = _row(PRF_ROW)["assertion"]()
    assert dead is False and "named it on the text banner" in detail, detail
    assert len(seen) == 2, seen
    (j_argv, j_env, j_drop, j_repo, j_fix), (t_argv, t_env, t_drop, t_repo, t_fix) = seen
    assert j_argv[1].endswith("product-repo-freshness.py") and j_argv[-1] == "--json"
    assert "--json" not in t_argv and "--no-fetch" not in j_argv + t_argv
    assert j_env == t_env == {} and j_drop == t_drop == ()
    assert j_repo == t_repo and j_repo.name == canary.FRESHNESS_PROBE_REPO and j_fix and t_fix
    assert not j_repo.parent.exists()


def test_prf_rotted_fixture_is_unevaluatable_not_dead():
    """Driven both ways against the real gate. A fixture built in sync is honestly NOT behind.
    With the fixture check that is UNEVALUATABLE; without it the row reads DEAD, the false
    accusation the check exists to prevent."""
    dead, detail = canary._freshness_behind_assertion(build_behind=0)()
    assert dead is None and "fixture is stale" in detail, detail
    dead, detail = canary._freshness_behind_assertion(check_fixture=False, build_behind=0)()
    assert dead is True and "behind=0" in detail, detail


def test_prf_relocated_unmutated_copy_is_alive(tmp_path, monkeypatch):
    """The positive control for the mutation tests below."""
    assert _copy_prf(tmp_path, monkeypatch) is False
    dead, detail = _row(PRF_ROW)["assertion"]()
    assert dead is False, detail


@pytest.mark.parametrize("mutate", [
    lambda src: src + "\ndef broken(:\n",
    lambda src: src.replace(_PRF_MAIN_RECORDS,
                            '    raise RuntimeError("canary mutant")\n', 1),
])
def test_prf_crash_of_the_real_gate_is_dead_not_unevaluatable(tmp_path, monkeypatch, mutate):
    """The second mutant is the silent one: __main__ catches the exception, prints one
    advisory line to stderr and exits 0, so the text channel reads 'every repo in sync'."""
    assert _copy_prf(tmp_path, monkeypatch, mutate) is True
    dead, detail = _row(PRF_ROW)["assertion"]()
    assert dead is True and "CRASHED" in detail, detail


def test_prf_reversed_rev_list_range_is_dead(tmp_path, monkeypatch):
    assert _copy_prf(tmp_path, monkeypatch, lambda src: src.replace(
        _PRF_RANGE, '"HEAD...%s" % upstream', 1)) is True
    dead, detail = _row(PRF_ROW)["assertion"]()
    assert dead is True and "behind=0" in detail, detail


def test_prf_boolean_count_is_dead(tmp_path, monkeypatch):
    """Why FRESHNESS_PROBE_BEHIND is 2: a count collapsed to a boolean reads 1 and must fail."""
    assert _copy_prf(tmp_path, monkeypatch, lambda src: src.replace(
        _PRF_COUNTS, "behind, ahead = (int(bool(int(x))) for x in counts.split())", 1)) is True
    dead, detail = _row(PRF_ROW)["assertion"]()
    assert dead is True and "behind=1" in detail, detail


def test_prf_silent_banner_is_dead_while_the_json_record_stays_right(tmp_path, monkeypatch):
    """The text channel's own death: render() starts counting 'behind' as clean, so the
    banner goes silent on a stale checkout while the --json record still says behind 2."""
    assert _copy_prf(tmp_path, monkeypatch, lambda src: src.replace(
        _PRF_CLEAN, 'CLEAN_VERDICTS = ("in-sync", "ahead-topological", "behind")', 1)) is True
    dead, detail = _row(PRF_ROW)["assertion"]()
    assert dead is True and "text banner" in detail and "does not name it" in detail, detail


@pytest.mark.parametrize("results,verdict,needle", [
    ([(1, "", "Traceback\nSyntaxError: x\n")], True, "CRASHED"),
    ([(0, "", "[product-repo-freshness] advisory probe failed (non-fatal): RuntimeError: x\n")],
     True, "CRASHED"),
    ([(0, _prf_json(records=[]), "")], True, "examined nothing"),
    ([(0, _prf_json(records=[], cannot_check=True, reason="no repos were enumerated"), "")],
     None, "said so"),
    ([(0, _prf_json(behind=0), "")], True, "behind=0"),
    ([(0, _prf_json(behind=0, cannot_check=True, reason="r"), "")], True, "behind=0"),
    ([(0, _prf_json(behind=1), "")], True, "behind=1"),
    ([(0, _prf_json(records=[_PRF_REC, _PRF_REC]), "")], True, "2 records"),
    ([(0, _prf_json(), ""), (0, "", "")], True, "does not name it"),
    ([(0, _prf_json(), ""), (0, _PRF_BANNER, "")], False, "named it"),
    ([(0, _prf_json(cannot_check=True, reason="r"), ""), (0, _PRF_BANNER, "")],
     False, "cannot_check was true"),
])
def test_prf_verdict_table(monkeypatch, results, verdict, needle):
    _inject_lck(monkeypatch, results)
    dead, detail = _row(PRF_ROW)["assertion"]()
    assert dead is verdict and needle in detail, detail


def test_prf_banner_probe_runs_only_after_the_json_record_is_right(monkeypatch):
    calls = []
    _inject_lck(monkeypatch, [(0, _prf_json(behind=0), "")], calls)
    _row(PRF_ROW)["assertion"]()
    assert len(calls) == 1 and calls[0][0][-1] == "--json", calls


def test_prf_absent_script_is_unevaluatable(tmp_path, monkeypatch):
    monkeypatch.setattr(canary, "SCRIPT_DIR", tmp_path, raising=True)
    dead, detail = _row(PRF_ROW)["assertion"]()
    assert dead is None and "absent" in detail, detail


def test_prf_probe_that_raises_is_unevaluatable(monkeypatch):
    def _boom(argv, stdin_text=None, drop_env=(), extra_env=None):
        raise OSError("canary spawn failure")
    monkeypatch.setattr(canary, "_probe", _boom, raising=True)
    dead, detail = _row(PRF_ROW)["assertion"]()
    assert dead is None and "could not run" in detail, detail


def test_prf_unbuildable_fixture_is_unevaluatable(monkeypatch):
    """No git on the box, or a git too old for `init -b`: nothing is known about the gate."""
    def _no_git(root, env, behind):
        raise RuntimeError("git: not found")
    monkeypatch.setattr(canary, "_freshness_fixture_build", _no_git, raising=True)
    dead, detail = _row(PRF_ROW)["assertion"]()
    assert dead is None and "could not be built" in detail, detail


def test_prf_fixture_check_does_not_call_an_unimportable_gate_rot(tmp_path, monkeypatch):
    """A gate that cannot import cannot judge freshness either, so the check returns None and
    lets the probe report the crash as DEAD."""
    (tmp_path / "product-repo-freshness.py").write_text("def broken(:\n", encoding="utf-8")
    monkeypatch.setattr(canary, "SCRIPT_DIR", tmp_path, raising=True)
    assert canary._freshness_fixture_check(tmp_path, {}, 2) is None


def test_prf_check_read_cannot_reach_a_temp_fixture(tmp_path):
    """THE NAMED NON-REGISTRATION'S PREMISE, pinned. --check-read judges only paths inside a
    member of the bound agent's AGENT_WRITE_PATH enumeration, so on a temp fixture it
    answers safe / not-a-product-repo and can never reach its READ-HAZARD branch. If this
    starts failing, --check-read can see a hermetic fixture after all: register a row for it
    and delete this test, with the reason, in the same change."""
    env = canary._freshness_fixture_env(tmp_path)
    repo = canary._freshness_fixture_build(tmp_path, env, canary.FRESHNESS_PROBE_BEHIND)
    rc, out, _err = canary._probe([sys.executable, (SCRIPTS / "product-repo-freshness.py").as_posix(),
                                   "--check-read", (repo / "probe.txt").as_posix(), "--json"])
    doc = json.loads(out)
    assert rc == 0 and doc["safe"] is True and doc["verdict"] == "not-a-product-repo", doc


# ---- hot-path-size-gate-growth-refusal ( unit 9) --------------------

HPS_ROW = "hot-path-size-gate-growth-refusal"
_HPS_REFUSAL = (f"[hot-path-size-gate] REFUSED — hot-path files may not grow (g-115-6470):\n"
                f"  {canary.HOTPATH_PROBE_PATH}  2 → 4 B (+2; cap = size at HEAD)\n")
# Mutation anchors, each unique in the gate (checked 2026-09-24).
_HPS_DECIDE = 'return ("grew" if staged_size > head_size else "ok", head_size)'
_HPS_LOADER = "    import yaml  # local: keep the hook's happy path import-light\n"
_HPS_TRAILER_RX = 'rx = re.compile(r"^\\s*" + re.escape(trailer) + r"\\s*(.*?)\\s*$", re.IGNORECASE)'


def _copy_hps(tmp_path, monkeypatch, mutate=lambda src: src):
    """Relocate a (possibly mutated) copy of the real gate; _paths/_fileops resolve through
    PYTHONPATH. sys.path is snapshotted because the fixture check imports the copy."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    src = (SCRIPTS / "hot-path-size-gate.py").read_text(encoding="utf-8")
    mutated = mutate(src)
    (tmp_path / "hot-path-size-gate.py").write_text(mutated, encoding="utf-8")
    monkeypatch.setattr(canary, "SCRIPT_DIR", tmp_path, raising=True)
    monkeypatch.setenv("PYTHONPATH", str(SCRIPTS))
    return mutated != src


def test_hps_row_is_alive_against_the_real_gate_and_pins_its_writes(monkeypatch):
    """The positive control through a spy: ONE child, in the hook's --commit-msg-file shape,
    with MIND_WORLD at a temp world and the local backend pinned, so an override-branch
    regression can only write there. The fixture existed during the call and is gone after."""
    real, seen = canary._probe, []

    def _spy(argv, stdin_text=None, drop_env=(), extra_env=None):
        repo = Path(argv[argv.index("--repo") + 1])
        seen.append((list(argv), dict(extra_env or {}), repo,
                     (repo / canary.HOTPATH_PROBE_PATH).is_file()))
        return real(argv, stdin_text, drop_env, extra_env)
    monkeypatch.setattr(canary, "_probe", _spy, raising=True)
    dead, detail = _row(HPS_ROW)["assertion"]()
    assert dead is False and "named probe/hot.md" in detail, detail
    assert len(seen) == 1, seen
    argv, env, repo, existed = seen[0]
    assert argv[1].endswith("hot-path-size-gate.py") and "--commit-msg-file" in argv
    assert env["STORAGE_BACKEND"] == "local" and Path(env["MIND_WORLD"]).parent == repo.parent
    assert existed and not repo.exists()


def test_hps_rotted_fixture_is_unevaluatable_not_dead():
    """Driven both ways against the real gate: a fixture that did not grow honestly passes."""
    dead, detail = canary._hotpath_growth_assertion(grow=False)()
    assert dead is None and "fixture is stale" in detail, detail
    dead, detail = canary._hotpath_growth_assertion(check_fixture=False, grow=False)()
    assert dead is True and "no longer sees" in detail, detail


def test_hps_relocated_unmutated_copy_is_alive(tmp_path, monkeypatch):
    assert _copy_hps(tmp_path, monkeypatch) is False
    dead, detail = _row(HPS_ROW)["assertion"]()
    assert dead is False, detail


@pytest.mark.parametrize("mutate,needle", [
    (lambda src: src.replace(_HPS_DECIDE, _HPS_DECIDE.replace(">", "<"), 1), "no longer sees"),
    (lambda src: src.replace(_HPS_LOADER, '    raise ValueError("canary mutant")\n', 1),
     "FAILS OPEN"),
    (lambda src: src.replace(_HPS_TRAILER_RX, 'rx = re.compile(r"^(.{12,})$")', 1), "override"),
])
def test_hps_silent_deaths_of_the_real_gate_are_dead(tmp_path, monkeypatch, mutate, needle):
    """A flipped comparison (the fixture check sees the flip and steps aside), a loader that
    raises (the live-registry control steps aside, the gate fails open), and an override
    parser that matches any line. Each passes the growth at rc 0."""
    assert _copy_hps(tmp_path, monkeypatch, mutate) is True
    dead, detail = _row(HPS_ROW)["assertion"]()
    assert dead is True and needle in detail, detail


def test_hps_crash_is_unevaluatable_because_the_hook_blocks_on_it(tmp_path, monkeypatch):
    assert _copy_hps(tmp_path, monkeypatch, lambda src: src + "\ndef broken(:\n") is True
    dead, detail = _row(HPS_ROW)["assertion"]()
    assert dead is None and "blocks every commit" in detail, detail


@pytest.mark.parametrize("results,verdict,needle", [
    ([(1, _HPS_REFUSAL, "")], False, "named probe/hot.md"),
    ([(1, "", "Traceback\nNameError: x\n")], None, "blocks every commit"),
    ([(2, "", "usage: hot-path-size-gate.py\n")], None, "blocks every commit"),
    ([(0, "[hot-path-size-gate] WARN: budget unreadable (x) — allowing commit; fix the registry\n",
       "")], True, "FAILS OPEN"),
    ([(0, "[hot-path-size-gate] OVERRIDE accepted — probe/hot.md +2 B\n", "")], True, "override"),
    ([(0, "", "")], True, "no longer sees"),
])
def test_hps_verdict_table(monkeypatch, results, verdict, needle):
    _inject_lck(monkeypatch, results)
    dead, detail = _row(HPS_ROW)["assertion"]()
    assert dead is verdict and needle in detail, detail


def test_hps_absent_script_is_unevaluatable(tmp_path, monkeypatch):
    monkeypatch.setattr(canary, "SCRIPT_DIR", tmp_path, raising=True)
    dead, detail = _row(HPS_ROW)["assertion"]()
    assert dead is None and "absent" in detail, detail


def test_hps_probe_that_raises_is_unevaluatable(monkeypatch):
    def _boom(argv, stdin_text=None, drop_env=(), extra_env=None):
        raise OSError("canary spawn failure")
    monkeypatch.setattr(canary, "_probe", _boom, raising=True)
    dead, detail = _row(HPS_ROW)["assertion"]()
    assert dead is None and "could not run" in detail, detail


def test_hps_unbuildable_fixture_is_unevaluatable(monkeypatch):
    def _no_git(root, env, grow=True):
        raise RuntimeError("git: not found")
    monkeypatch.setattr(canary, "_hotpath_fixture_build", _no_git, raising=True)
    dead, detail = _row(HPS_ROW)["assertion"]()
    assert dead is None and "could not be built" in detail, detail


def test_hps_fixture_check_does_not_call_an_unimportable_gate_rot(tmp_path, monkeypatch):
    (tmp_path / "hot-path-size-gate.py").write_text("def broken(:\n", encoding="utf-8")
    monkeypatch.setattr(canary, "SCRIPT_DIR", tmp_path, raising=True)
    assert canary._hotpath_fixture_check(tmp_path, {}) is None


# ─── goal-selector STRATEGIC-FOCUS INERT banner ( unit 10) ─────────

SFI_ROW = "goal-selector-strategic-focus-inert"
# A synthetic two-lane directive, so no test depends on the live one.
_SFI_DIRECTIVE = {"strategic_focus": {"primary": "close asp-901, then asp-902"}}
# Source anchors in goal-selector.py: the banner's own predicate, the floor's lane count
# and the banner's first body line. Each mutant asserts its substitution landed, so a
# drifted source fails loudly instead of testing an unmutated copy.
_SFI_BANNER_TEST = 'if not lanes or status.get("pool_lane_rows"):'
_SFI_LANE_ROWS = 'lane_rows = [s for s in scored if s.get("aspiration_id") in lanes]'
_SFI_BANNER_BODY = 'Returns the emitted warnings for tests. Never raises."""'


def _sfi(check_fixture=True, team_state=_SFI_DIRECTIVE):
    return canary._strategic_focus_inert_assertion(
        check_fixture=check_fixture, team_state=team_state)()


def _copy_sfi(tmp_path, monkeypatch, mutate=lambda src: src):
    """Relocate goal-selector.py into tmp_path, mutated. Returns whether it changed."""
    src = (canary.SCRIPT_DIR / "goal-selector.py").read_text(encoding="utf-8")
    out = mutate(src)
    (tmp_path / "goal-selector.py").write_text(out, encoding="utf-8")
    monkeypatch.setattr(canary, "SCRIPT_DIR", tmp_path, raising=True)
    return out != src


def test_sfi_row_is_registered_with_its_assertion():
    row = _row(SFI_ROW)
    assert callable(row["assertion"]) and row["remedy"] and row["evidence"]


def test_sfi_real_selector_with_a_synthetic_directive_is_alive():
    dead, detail = _sfi()
    assert dead is False and "2 directive lane(s)" in detail, detail


def test_sfi_wording_that_names_no_lane_is_unevaluatable_and_dead_without_the_check():
    """Outcome 2, both ways in one process: prose that stops naming asp-NNN leaves the
    selector no lanes, so silence is CORRECT and the row says UNEVALUATABLE. The same
    wording with the fixture check off reads DEAD, which is the false accusation the check
    exists to prevent."""
    rotted = {"strategic_focus": {"primary": "close asp 901, then asp 902"}}
    dead, detail = _sfi(team_state=rotted)
    assert dead is None and "fixture is stale" in detail, detail
    dead, detail = _sfi(check_fixture=False, team_state=rotted)
    assert dead is True and "stayed silent" in detail, detail


def test_sfi_no_directive_is_unevaluatable():
    dead, detail = _sfi(team_state={})
    assert dead is None and "no asp-NNN lane" in detail, detail


def test_sfi_probe_row_inside_a_lane_is_unevaluatable():
    dead, detail = _sfi(team_state={"strategic_focus": {"primary": canary.INERT_PROBE_ASP}})
    assert dead is None and "INERT_PROBE_ASP" in detail, detail


def test_sfi_relocated_unmutated_copy_is_alive(tmp_path, monkeypatch):
    """The positive control for the mutation tests below."""
    assert _copy_sfi(tmp_path, monkeypatch) is False
    dead, detail = _sfi()
    assert dead is False, detail


def test_sfi_silenced_banner_is_dead(tmp_path, monkeypatch):
    assert _copy_sfi(tmp_path, monkeypatch,
                     lambda src: src.replace(_SFI_BANNER_TEST, "if True:", 1)) is True
    dead, detail = _sfi()
    assert dead is True and "stayed silent" in detail, detail


def test_sfi_banner_that_ignores_the_pool_is_dead(tmp_path, monkeypatch):
    assert _copy_sfi(tmp_path, monkeypatch,
                     lambda src: src.replace(_SFI_BANNER_TEST, "if not lanes:", 1)) is True
    dead, detail = _sfi()
    assert dead is True and "no longer tells" in detail, detail


def test_sfi_floor_that_counts_every_row_as_a_lane_row_is_dead(tmp_path, monkeypatch):
    assert _copy_sfi(tmp_path, monkeypatch, lambda src: src.replace(
        _SFI_LANE_ROWS, "lane_rows = list(scored)", 1)) is True
    dead, detail = _sfi()
    assert dead is True and "held 1 lane row(s)" in detail, detail


def test_sfi_banner_that_raises_is_dead_not_unevaluatable(tmp_path, monkeypatch):
    """The selector's own try/except swallows this, so it is silent in production."""
    assert _copy_sfi(tmp_path, monkeypatch, lambda src: src.replace(
        _SFI_BANNER_BODY, _SFI_BANNER_BODY + '\n    raise RuntimeError("canary mutant")',
        1)) is True
    dead, detail = _sfi()
    assert dead is True and "RuntimeError" in detail, detail


def test_sfi_unimportable_selector_is_unevaluatable(tmp_path, monkeypatch):
    """Selection itself stops on this, which is loud (rb-11742)."""
    assert _copy_sfi(tmp_path, monkeypatch, lambda src: src + "\ndef broken(:\n") is True
    dead, detail = _sfi()
    assert dead is None and "could not be imported" in detail, detail


def test_sfi_absent_script_is_unevaluatable(tmp_path, monkeypatch):
    monkeypatch.setattr(canary, "SCRIPT_DIR", tmp_path, raising=True)
    dead, detail = _sfi()
    assert dead is None and "absent" in detail, detail


def test_sfi_probe_that_raises_is_unevaluatable(monkeypatch):
    def _boom(argv, stdin_text=None, drop_env=(), extra_env=None):
        raise OSError("canary spawn failure")
    monkeypatch.setattr(canary, "_probe", _boom, raising=True)
    dead, detail = _sfi()
    assert dead is None and "could not run" in detail, detail
