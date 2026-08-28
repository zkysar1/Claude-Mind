#!/usr/bin/env python3
"""test_goal_selector_residence.py — hermetic pins for goal-selector.py _agent_is_resident ().

g-115-5850 added _agent_is_resident() to suppress three cross-agent writes
(drain-lane-state, scorer-verdict, applications_log) when the bound MIND_AGENT
has no local-paths.conf on this box, so a fleet-vantage `MIND_AGENT=<other>
goal-selector.sh` probe cannot fabricate a partner's selector state. That guard
shipped verified only by hand; this file pins it hermetically.

Contract pinned (maps to g-115-6111 verification outcomes 1 + 2):
  1. INVERSION-SENSITIVE: resident (conf present) -> True, non-resident (conf
     absent) -> False. Inverting the `.exists()` return flips BOTH, so the pair
     goes red on exactly the mutation the outcome names.
  2. BOTH FAIL-OPEN branches: AGENT_DIR is None -> True, and a stat raising
     OSError -> True. Neither is reachable by a hand-probe (the writes already
     no-op when AGENT_DIR is None; OSError needs a broken stat), which is why
     they need a test.
  3. CALL-SITE DROP: _record_strategy_application short-circuits (no write) when
     the predicate is False, so dropping its `if not _agent_is_resident()` guard
     goes red (the strategy file is left byte-unchanged under suppression).

NOT covered here (integration/environment, tracked as a follow-up): the
read-side ordering invariant across a full selector run, and the live
one-conf-per-box measurement.
"""
import importlib
import os
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# goal-selector.py derives AGENT_DIR from MIND_AGENT at import; capture-restore
# so collection-time env mutation cannot leak (rb-1096, guard-588).
_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "bravo")
gs = importlib.import_module("goal-selector")
if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT


class _RaisingPath:
    """Path-like whose .exists() raises OSError, to reach the fail-open except."""

    def __truediv__(self, _other):
        return self

    def exists(self):
        raise OSError("simulated stat failure")


def _resident_with_agent_dir(value):
    saved = gs.AGENT_DIR
    try:
        gs.AGENT_DIR = value
        return gs._agent_is_resident()
    finally:
        gs.AGENT_DIR = saved


def test_resident_when_conf_present():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "local-paths.conf").write_text("WORLD_PATH=/x\n")
        assert _resident_with_agent_dir(Path(d)) is True


def test_not_resident_when_conf_absent():
    with tempfile.TemporaryDirectory() as d:
        assert _resident_with_agent_dir(Path(d)) is False


def test_fail_open_agent_dir_none():
    assert _resident_with_agent_dir(None) is True


def test_fail_open_oserror():
    assert _resident_with_agent_dir(_RaisingPath()) is True


def test_call_site_suppresses_when_not_resident():
    saved = gs.AGENT_DIR
    try:
        with tempfile.TemporaryDirectory() as d:
            gs.AGENT_DIR = Path(d)  # no conf -> not resident
            strat = Path(d) / "strategy.yaml"
            strat.write_text("params: {}\n")
            before = strat.read_text()
            gs._record_strategy_application(str(strat), "unit-test probe")
            # Suppressed path returns before any write: file byte-unchanged.
            assert strat.read_text() == before
    finally:
        gs.AGENT_DIR = saved


if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"ok: {_name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL: {_name}: {e}")
    print("ALL PASS" if failures == 0 else f"{failures} FAILURE(S)")
    sys.exit(1 if failures else 0)
