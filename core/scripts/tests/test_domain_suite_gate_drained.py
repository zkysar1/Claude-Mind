"""Pins the domain-suite gate's DRAINED close ().

The completed-not-closed drain now closes another session's finished unit through
iteration-close.sh --phase verify --drain, and do_verify passes --drain to this gate. The
closing session ran none of that unit, so the gate counts only the claim's sessions as the
unit's. Before, the closer's own recent domain writes were charged to the drained close and
the suite ran for them (up to 900 s per close, up to three closes per drain cycle).

Pinned, each against its control:
  1. unit_sessions(drained=True) drops the closer's id and keeps the claim's.
  2. evaluate(drained=True): the closer's own write no longer runs the suite (the same inputs
     without --drain do run it); the unit's own write still runs it; a record naming no session
     keeps the wide trigger.
  3. Telemetry: every firing of a drained evaluate carries extra.drained, and a firing after a
     run carries extra.seconds, both in clear for the cost measurement. Reset per evaluate.
  4. main() forwards --drain.

Skips and runs are observed through the runner hook's marker, not inferred from rc
(guard-1082). The gate runs in-process with its telemetry stubbed, so no test writes a
live gate-firings row.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_domain_suite_gate_own_writes import (  # noqa: E402
    SID,
    SID_CLAIM,
    SID_PEER,
    _evaluate,
    _gate,
    _ledger,
    _row,
    _tree,
)

DRAINED_NOTE = "(a drained close: the unit's sessions are the claim's, not the closer's)"


def _claimed(g, *, sids=True):
    """The drained row: claimed 30 minutes ago by a session that is not the closer."""
    claimed_at = (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S")
    rec = {"claimed_at": claimed_at}
    if sids:
        rec.update(claimed_by_sid=SID_CLAIM, executed_by_sid=SID_CLAIM)
    g.claim_record = lambda goal, source: {"id": goal, **rec}


def _run(g, world, root, ledger, capsys, *, drained):
    """evaluate() exactly as main() calls it, with the claim read from the record."""
    rc = g.evaluate("g-999-01", "world", None, None, 60, world, ledger=ledger,
                    project_root=root, drained=drained)
    out = capsys.readouterr()
    return rc, out.err


# ─── 1. whose sessions ────────────────────────────────────────────────────

def test_unit_sessions_of_a_drained_close_are_the_claims_alone(monkeypatch, tmp_path):
    g = _gate(monkeypatch, tmp_path)
    monkeypatch.setenv("MIND_SID", SID)
    rec = {"claimed_by_sid": SID_CLAIM, "executed_by_sid": SID_PEER}
    assert g.unit_sessions(rec, drained=True) == {SID_CLAIM, SID_PEER}
    assert g.unit_sessions(None, drained=True) == set()
    # Control: the same record, not drained, counts the closing process too.
    assert g.unit_sessions(rec) == {SID, SID_CLAIM, SID_PEER}
    assert g.unit_sessions(rec, drained=False) == {SID, SID_CLAIM, SID_PEER}


# ─── 2. evaluate ──────────────────────────────────────────────────────────

def test_the_closers_own_write_does_not_run_the_suite_for_a_drained_close(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MIND_SID", SID)
    marker = tmp_path / "ran.marker"
    g = _gate(monkeypatch, tmp_path)
    root, world, _scripts = _tree(tmp_path, marker)
    _claimed(g)
    ledger = _ledger(tmp_path / "ledger.jsonl", [_row("pkg/config.py", SID, time.time() - 600)])
    rc, err = _run(g, world, root, ledger, capsys, drained=True)
    assert rc == 0 and not marker.exists(), "the suite ran for the drainer's own write"
    assert "[domain-suite-gate] not running the domain suite for g-999-01" in err
    assert DRAINED_NOTE in err


def test_control_the_same_write_runs_the_suite_when_the_close_is_not_drained(monkeypatch, tmp_path, capsys):
    # Identical inputs to the test above, without --drain: the closer's write is its own unit's.
    monkeypatch.setenv("MIND_SID", SID)
    marker = tmp_path / "ran.marker"
    g = _gate(monkeypatch, tmp_path)
    root, world, _scripts = _tree(tmp_path, marker)
    _claimed(g)
    ledger = _ledger(tmp_path / "ledger.jsonl", [_row("pkg/config.py", SID, time.time() - 600)])
    rc, err = _run(g, world, root, ledger, capsys, drained=False)
    assert rc == 0 and marker.exists(), "a loop close's own write must run the suite"
    assert DRAINED_NOTE not in err


def test_the_units_own_write_still_runs_the_suite_for_a_drained_close(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MIND_SID", SID)
    marker = tmp_path / "ran.marker"
    g = _gate(monkeypatch, tmp_path)
    root, world, _scripts = _tree(tmp_path, marker)
    _claimed(g)
    ledger = _ledger(tmp_path / "ledger.jsonl", [_row("pkg/config.py", SID_CLAIM, time.time() - 600)])
    rc, err = _run(g, world, root, ledger, capsys, drained=True)
    assert rc == 0 and marker.exists(), "a write by the claim's session must run the suite"
    (line,) = [ln for ln in err.splitlines() if ln.startswith("[domain-suite-gate] running")]
    assert "all written by this unit's sessions" in line and DRAINED_NOTE in line


def test_a_drained_record_naming_no_session_keeps_the_wide_trigger(monkeypatch, tmp_path, capsys):
    # With the closer dropped and no claim ids, no row can be attributed: the gate cannot
    # show the change came from elsewhere, so it runs the suite and says why.
    monkeypatch.setenv("MIND_SID", SID)
    marker = tmp_path / "ran.marker"
    g = _gate(monkeypatch, tmp_path)
    root, world, _scripts = _tree(tmp_path, marker)
    _claimed(g, sids=False)
    ledger = _ledger(tmp_path / "ledger.jsonl", [_row("pkg/config.py", SID, time.time() - 600)])
    rc, err = _run(g, world, root, ledger, capsys, drained=True)
    assert rc == 0 and marker.exists()
    assert "not narrowed to this unit's own writes: no session id for this close" in err


# ─── 3. the firing's clear-text telemetry ─────────────────────────────────

def test_drained_and_seconds_ride_every_firing_and_reset_per_evaluate(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MIND_SID", SID)
    marker = tmp_path / "ran.marker"
    g = _gate(monkeypatch, tmp_path)
    root, world, _scripts = _tree(tmp_path, marker)
    _claimed(g)
    own = _ledger(tmp_path / "own.jsonl", [_row("pkg/config.py", SID_CLAIM, time.time() - 600)])
    closer = _ledger(tmp_path / "closer.jsonl", [_row("pkg/config.py", SID, time.time() - 600)])

    _run(g, world, root, own, capsys, drained=True)        # drained, suite ran
    _run(g, world, root, closer, capsys, drained=True)     # drained, skipped
    _run(g, world, root, closer, capsys, drained=False)    # loop close, suite ran
    rc, _doc, _err = _evaluate(g, world, root, closer, capsys)  # loop close, the existing helper
    assert rc == 0
    extras = [c["kwargs"].get("extra") for c in g._telemetry]
    assert len(extras) == 4
    ran_drained, skipped_drained, ran_loop, ran_loop_again = extras
    assert ran_drained["drained"] is True and isinstance(ran_drained["seconds"], int)
    assert skipped_drained == {"drained": True}
    assert set(ran_loop) == {"seconds"} and set(ran_loop_again) == {"seconds"}


def test_a_loop_close_that_runs_nothing_carries_no_extra(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MIND_SID", SID)
    g = _gate(monkeypatch, tmp_path)
    root, world, _scripts = _tree(tmp_path, tmp_path / "ran.marker")
    _claimed(g)
    peer = _ledger(tmp_path / "peer.jsonl", [_row("pkg/config.py", SID_PEER, time.time() - 600)])
    _run(g, world, root, peer, capsys, drained=False)
    (call,) = g._telemetry
    assert call["kwargs"].get("extra") is None


# ─── 4. main ──────────────────────────────────────────────────────────────

def test_main_forwards_drain_and_defaults_it_off(monkeypatch, tmp_path):
    g = _gate(monkeypatch, tmp_path)
    seen = []
    monkeypatch.setattr(g, "evaluate", lambda *a, **k: seen.append(k.get("drained")) or 0)
    assert g.main(["--goal", "g-999-01", "--drain"]) == 0
    assert g.main(["--goal", "g-999-01"]) == 0
    assert seen == [True, False]
