"""Tests for evolution-cadence-check.py decide() ().

The evolution-cadence precheck safety-net fires /aspirations-evolve when
last_evolution_at_time is stale beyond maintenance_cadence.evolution.hours_cadence
— because recurring-close.sh bypasses the Phase 8.8 evolution tick on
recurring-heavy sessions (observed 2026-07-15: ~99h stale vs the 12h cadence).
These tests pin the pure decision: fire/noop, the per-session cap (checked
BEFORE cadence), unset vs unparseable stamp, and the cadence boundary.

Pattern: same importlib + sys.path shape as test_defer_drift_check.py (the
script name has hyphens, so it cannot be a plain `import`).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "evolution-cadence-check.py"

# Fixed reference "now" so age math is deterministic across machines.
NOW = dt.datetime(2026, 7, 15, 13, 0, 0)
NOW_EPOCH = NOW.timestamp()
CADENCE = 12.0   # maintenance_cadence.evolution.hours_cadence
MAX_EVO = 2      # global.max_evolutions_per_session


def _import():
    spec = importlib.util.spec_from_file_location("evolution_cadence_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["evolution_cadence_check"] = mod
    spec.loader.exec_module(mod)
    return mod


def _epoch(iso: str) -> float:
    return dt.datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").timestamp()


def test_fire_when_stale():
    """last stamp 13h old, cadence 12h -> fire (evolution starved)."""
    m = _import()
    last = "2026-07-15T00:00:00"  # 13h before NOW
    code, msg, warn = m.decide(last, _epoch(last), CADENCE, 0, MAX_EVO, NOW_EPOCH)
    assert code == 0
    assert msg.startswith("fire: evolution stale")
    assert warn is None


def test_noop_when_fresh():
    """last stamp 0.5h old -> noop (well within cadence)."""
    m = _import()
    last = "2026-07-15T12:30:00"  # 0.5h before NOW
    code, msg, warn = m.decide(last, _epoch(last), CADENCE, 1, MAX_EVO, NOW_EPOCH)
    assert code == 1
    assert msg.startswith("noop: evolution fresh")
    assert warn is None


def test_fire_when_unset():
    """No stamp at all -> fire (evolution never ran in this world/session)."""
    m = _import()
    code, msg, warn = m.decide(None, None, CADENCE, 0, MAX_EVO, NOW_EPOCH)
    assert code == 0
    assert "unset" in msg
    assert warn is None


def test_cap_beats_staleness():
    """Cap is checked BEFORE cadence: a capped session never fires even when stale."""
    m = _import()
    last = "2026-07-15T00:00:00"  # 13h stale
    code, msg, warn = m.decide(last, _epoch(last), CADENCE, MAX_EVO, MAX_EVO, NOW_EPOCH)
    assert code == 1
    assert "session cap reached" in msg
    assert warn is None


def test_unparseable_stamp_fails_open_to_noop_with_warn():
    """A non-empty but unparseable stamp -> NOOP + loud warn (guard-424), never a
    spurious fire every iteration."""
    m = _import()
    code, msg, warn = m.decide("garbage-not-a-date", None, CADENCE, 0, MAX_EVO, NOW_EPOCH)
    assert code == 1
    assert msg == ""
    assert warn is not None and "unparseable" in warn


def test_cadence_boundary_is_inclusive():
    """age exactly == cadence fires (>= boundary) — an on-the-dot-stale evolution
    is caught this iteration, not deferred one more."""
    m = _import()
    last_epoch = NOW_EPOCH - CADENCE * 3600  # exactly 12h old
    code, msg, warn = m.decide(
        "2026-07-15T01:00:00", last_epoch, CADENCE, 0, MAX_EVO, NOW_EPOCH
    )
    assert code == 0
    assert msg.startswith("fire: evolution stale")
    assert warn is None
