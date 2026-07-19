"""test_goal_selector_rne_time_gate.py -- 8 regression (2026-07-17).

Pins the hypothesis time gate (`resolves_no_earlier_than`) against the
datetime-form inert-gate bug fixed in g-115-2507 (commit 0893a074e).

The field arrives in TWO shapes: date-only ("2026-07-20") from older records
and full datetime ("2026-07-20T00:00:00") from the sq-009 hypothesis template.
The pre-fix gates parsed with `date.fromisoformat(str(rne))`, which RAISES on
the datetime shape (pinned below), and every site swallowed the raise to
no-gate -- so a 3-days-future hypothesis goal top-ranked at 8.57 instead of
being excluded. The 202-test selector population passed before the fix too,
proving no test exercised the integration path; this file closes that gap:

  1. `_parse_rne_dt` unit shapes (datetime / date-only / garbage / None).
  2. collect_candidates: future datetime-form AND future date-only rne are
     excluded; past rne ranks.
  3. collect_blocked: the two future forms classify as block_reason=
     hypothesis_gate; past rne is not hypothesis-blocked.
  4. The root-cause canary: `date.fromisoformat` raising on datetime-form is
     WHY `_parse_rne_dt` exists -- if a future Python accepts it, the comment
     trail survives but the canary flags the assumption change.

Fails-on-pre-fix proof (run 2026-07-17 against 0893a074e~1 via
spec_from_file_location): pre-fix collect_candidates INCLUDED the future
datetime-form goal (test 2 asserts absent -> fails) and collect_blocked did
NOT classify it (test 3 fails). Both pass on the fixed module.

Harness mirrors test_goal_selector_idle_reallocation.py: pin MIND_AGENT
around import, per-test AGENT_NAME pin (g-115-2313 -- bash-agent-inject
pre-sets the env on agent boxes), neutralize the orthogonal capability
filter. No subprocess, no tmp world.
"""

from __future__ import annotations

import importlib
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "alpha")

gs = importlib.import_module("goal-selector")

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT


def _iso_dt(days_from_now):
    """Full datetime form -- the sq-009 hypothesis-template shape."""
    return (datetime.now() + timedelta(days=days_from_now)).strftime(
        "%Y-%m-%dT%H:%M:%S")


def _iso_d(days_from_now):
    """Date-only form -- the legacy record shape."""
    return (datetime.now() + timedelta(days=days_from_now)).strftime("%Y-%m-%d")


def _goal(gid, rne=None):
    """Minimal pending, agent-eligible, unclaimed, non-recurring goal."""
    g = {
        "id": gid, "title": "goal %s" % gid, "status": "pending",
        "participants": ["agent"], "category": "test", "priority": "MEDIUM",
    }
    if rne is not None:
        g["resolves_no_earlier_than"] = rne
    return g


def _asps(goals):
    return [{"id": "asp-test", "status": "active", "goals": goals}]


def _pin(monkeypatch):
    monkeypatch.setattr(gs, "AGENT_NAME", "alpha")
    monkeypatch.setattr(gs, "_get_runner_capabilities", lambda: set())


THREE_GOALS = [
    _goal("g-rne-future-dt", rne=_iso_dt(3)),   # sq-009 template shape
    _goal("g-rne-future-d", rne=_iso_d(3)),     # legacy date-only shape
    _goal("g-rne-past", rne=_iso_dt(-2)),       # window open -> must rank
]


# -- 1. _parse_rne_dt unit shapes ------------------------------------------

def test_parse_rne_dt_datetime_form():
    assert gs._parse_rne_dt("2099-07-20T00:00:00") == datetime(2099, 7, 20)


def test_parse_rne_dt_datetime_form_with_time():
    assert gs._parse_rne_dt("2099-07-20T09:30:15") == datetime(2099, 7, 20, 9, 30, 15)


def test_parse_rne_dt_date_only_promotes_to_midnight():
    assert gs._parse_rne_dt("2099-07-20") == datetime(2099, 7, 20)


def test_parse_rne_dt_garbage_is_none():
    assert gs._parse_rne_dt("not-a-date") is None


def test_parse_rne_dt_empty_and_none():
    assert gs._parse_rne_dt(None) is None
    assert gs._parse_rne_dt("") is None


# -- 2. collect_candidates: future forms excluded, past ranks ---------------

def test_candidates_exclude_future_rne_both_forms(monkeypatch):
    _pin(monkeypatch)
    ids = {c["goal"]["id"]
           for c in gs.collect_candidates(_asps(THREE_GOALS), source="world")}
    assert "g-rne-future-dt" not in ids   # pre-fix: INCLUDED (inert gate)
    assert "g-rne-future-d" not in ids
    assert "g-rne-past" in ids


# -- 3. collect_blocked: future forms classified as hypothesis_gate ---------

def test_blocked_classifies_future_rne_both_forms(monkeypatch):
    _pin(monkeypatch)
    blocked = {e["goal_id"] if "goal_id" in e else e.get("goal", {}).get("id"):
               e for e in gs.collect_blocked(_asps(THREE_GOALS))}
    # Normalize: collect_blocked entries carry goal_id per its docstring shape;
    # fall back handled above keeps the assertion resilient to either key.
    assert "g-rne-future-dt" in blocked
    assert blocked["g-rne-future-dt"]["block_reason"] == "hypothesis_gate"
    assert "g-rne-future-d" in blocked
    assert blocked["g-rne-future-d"]["block_reason"] == "hypothesis_gate"
    hypothesis_blocked = {gid for gid, e in blocked.items()
                          if e.get("block_reason") == "hypothesis_gate"}
    assert "g-rne-past" not in hypothesis_blocked


# -- 4. Root-cause canary ---------------------------------------------------

def test_date_fromisoformat_raises_on_datetime_form():
    """The pre-fix parse `date.fromisoformat("<datetime>")` raises on this
    interpreter -- the swallowed raise is what made the gate inert (g-115-2507).
    If a future Python version starts ACCEPTING datetime strings here, this
    canary fires so the _parse_rne_dt rationale gets re-checked (the fix stays
    correct either way; only the incident precondition changes)."""
    with pytest.raises(ValueError):
        date.fromisoformat("2099-07-20T00:00:00")
