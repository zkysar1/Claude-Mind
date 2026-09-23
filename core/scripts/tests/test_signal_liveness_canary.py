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

# Rows with their own named tests above rather than a tuple-driven family.
SELF_TESTED_ROWS = (
    "agent-watchdog-tick",
    "aspirations-query-read-channel",
    "aspirations-query-refusal-channel",
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
