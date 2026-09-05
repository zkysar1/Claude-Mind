"""Live-Body stop-list derivation tests (, guard-6027).

Covers `fleet-live-bodies.py`, which replaced the hand-maintained per-AGENT
stop table in the fleet-quiesce ceremony. The defect it fixes is a NOUN
mismatch: the table's rows counted AGENTS while the ceremony acts on BODIES,
so one agent live on six boxes contributed a single row and five terminals
kept running through a window that was declared quiet.

Each test below pins one half of the union that makes the derivation safe.
Losing any of them silently re-creates a missing live Body in the stop list,
which is the unrecoverable direction: a needless extra terminal check costs
seconds, a missed Body invalidates the whole window.

  - Rows COUNT BODIES: two carriers for ONE agent on two hosts => TWO rows.
  - SELF always appears (the carrier scan excludes the Body running it) and
    is never duplicated when the scan already reported it.
  - A rostered agent with no live Body is FLAGGED, never omitted, and never
    counted as live.
  - A carrier-scan failure degrades to self + flagged rows and still returns
    a payload (exit 0) rather than raising.

Daemon-safe (no daemon_integration marker — pure dict arithmetic; both fleet
readers are monkeypatched).

Run:
  python -m pytest core/scripts/tests/test_fleet_live_bodies.py -q
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent  # core/scripts/


def _load_fleet_live_bodies():
    """Load the hyphen-named module via importlib (not importable by name)."""
    spec = importlib.util.spec_from_file_location(
        "fleet_live_bodies", CORE_SCRIPTS / "fleet-live-bodies.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def flb():
    return _load_fleet_live_bodies()


def _stub(mod, monkeypatch, carriers, roster, scanned=7, err=None):
    """Pin both fleet readers so the derivation is tested, not the fleet."""
    monkeypatch.setattr(mod, "_fresh_carriers",
                        lambda agent, sid: (carriers, scanned, err))
    monkeypatch.setattr(mod, "_roster", lambda: roster)


def test_rows_count_bodies_not_agents(flb, monkeypatch):
    """THE defect. One agent, two boxes => two rows, because a quiesce stops
    terminals, not agents. An agent-keyed table returns 1 here and that is
    exactly how five terminals survived a declared-quiet window."""
    _stub(flb, monkeypatch,
          carriers=[
              {"agent": "alpha", "host": "cc-04", "sid": "s1", "age_s": 5},
              {"agent": "alpha", "host": "cc-08", "sid": "s2", "age_s": 9},
          ],
          roster=["alpha"])

    out = flb.collect(agent="alpha", sid="s1", host="cc-04")

    assert out["bodies_live"] == 2
    assert {r["host"] for r in out["rows"]} == {"cc-04", "cc-08"}
    # One agent, two rows: the count must not collapse on the agent name.
    assert len({r["agent"] for r in out["rows"]}) == 1


def test_self_is_included_when_scan_omits_it(flb, monkeypatch):
    """The carrier scan excludes the Body it runs from, by construction. A
    stop list that omits the terminal you are sitting at is wrong."""
    _stub(flb, monkeypatch,
          carriers=[{"agent": "bravo", "host": "cc-05", "sid": "s9", "age_s": 3}],
          roster=["alpha", "bravo"])

    out = flb.collect(agent="alpha", sid="mine", host="cc-04")

    self_rows = [r for r in out["rows"] if r["source"] == "self"]
    assert len(self_rows) == 1
    assert self_rows[0]["agent"] == "alpha"
    assert self_rows[0]["host"] == "cc-04"
    assert self_rows[0]["status"] == "live"
    assert out["bodies_live"] == 2


def test_self_not_duplicated_when_scan_reports_it(flb, monkeypatch):
    """If the scan already returned this Body, adding a `self` row again would
    double-count it and inflate the live total."""
    _stub(flb, monkeypatch,
          carriers=[{"agent": "alpha", "host": "cc-04", "sid": "mine", "age_s": 1}],
          roster=["alpha"])

    out = flb.collect(agent="alpha", sid="mine", host="cc-04")

    assert out["bodies_live"] == 1
    assert [r["source"] for r in out["rows"]] == ["fresh-carrier"]


def test_unaccounted_agent_is_flagged_never_omitted(flb, monkeypatch):
    """Fresh carriers are a FLOOR, not a census: a live Body whose carrier went
    stale vanishes from the scan. A rostered agent with no live Body must be
    reported as unverified, and must NOT be counted as live."""
    _stub(flb, monkeypatch,
          carriers=[{"agent": "alpha", "host": "cc-04", "sid": "s1", "age_s": 2}],
          roster=["alpha", "zeta"])

    out = flb.collect(agent="alpha", sid="s1", host="cc-04")

    flagged = [r for r in out["rows"] if r["source"] == "roster"]
    assert [r["agent"] for r in flagged] == ["zeta"]
    assert flagged[0]["status"] == "carrier-stale-verify-at-terminal"
    assert out["unverified_agents"] == 1
    # Reported, but never counted as a live Body.
    assert out["bodies_live"] == 1
    assert out["rows_total"] == 2


def test_scan_failure_degrades_and_still_returns(flb, monkeypatch):
    """An advisory that refuses to run is worse than one that reports what it
    saw: a failed carrier scan must still yield self + flagged rows."""
    _stub(flb, monkeypatch, carriers=[], roster=["alpha", "bravo"],
          scanned=0, err="RuntimeError: boom")

    out = flb.collect(agent="alpha", sid="s1", host="cc-04")

    assert out["scan_error"] == "RuntimeError: boom"
    assert out["bodies_live"] == 1          # self only
    assert out["unverified_agents"] == 1    # bravo unaccounted
    assert any(r["source"] == "self" for r in out["rows"])


def test_fresh_carrier_reader_never_raises(flb, monkeypatch):
    """The real reader wraps the fleet scan: a broken import or a bad agents
    root must degrade to an error string, never propagate."""
    agent_out, scanned, err = flb._fresh_carriers("no-such-agent", None)
    assert isinstance(agent_out, list)
    assert isinstance(scanned, int)
    assert err is None or isinstance(err, str)
