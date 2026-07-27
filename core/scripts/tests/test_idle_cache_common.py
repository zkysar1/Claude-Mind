"""test_idle_cache_common.py -- the shared idle-path elapsed-check helper
(_idle_cache_common.wake_timer_elapsed), facet-1 of the g-115-3059 consolidation
(story-a / g-115-3060).

Both cycle-caches (dry-idle-cycle-cache.py + quiescence-cycle-cache.py) route their
stored-baseline AND fresh-scan timer checks through this ONE helper. The cache test
files exercise it indirectly via evaluate_cache; these tests pin the helper's own
contract directly so a future helper refactor fails loud at the helper level:
  - None / empty / "null" / unparseable input  -> False (no timer -> no MISS)
  - ISO string OR datetime input accepted
  - elapsed (wake <= now)                       -> True
  - within the `within_s` imminent margin       -> True
  - beyond the margin                           -> False
  - boundary (>= is inclusive)                  -> True

Timestamps are computed DYNAMICALLY (now +/- delta) per guard-566 so the suite
never rots against a frozen date.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import json  # noqa: E402

from _idle_cache_common import (  # noqa: E402
    wake_timer_elapsed,
    authoritative_earliest_wake_at,
)

import pytest  # noqa: E402


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


# --- None / unparseable -> False (no timer forces no MISS) --------------------

def test_none_is_false():
    assert wake_timer_elapsed(None, datetime.now()) is False


def test_empty_string_is_false():
    assert wake_timer_elapsed("", datetime.now()) is False


def test_null_literal_is_false():
    assert wake_timer_elapsed("null", datetime.now()) is False


def test_unparseable_is_false():
    assert wake_timer_elapsed("not-a-timestamp", datetime.now()) is False


# --- elapsed (wake <= now) -> True -------------------------------------------

def test_elapsed_string_is_true():
    now = datetime.now()
    assert wake_timer_elapsed(_iso(now - timedelta(minutes=1)), now) is True


def test_elapsed_datetime_is_true():
    now = datetime.now()
    assert wake_timer_elapsed(now - timedelta(minutes=1), now) is True


# --- future beyond the (default 0) margin -> False ---------------------------

def test_future_no_margin_is_false():
    now = datetime.now()
    assert wake_timer_elapsed(_iso(now + timedelta(minutes=1)), now) is False


def test_future_datetime_no_margin_is_false():
    now = datetime.now()
    assert wake_timer_elapsed(now + timedelta(minutes=1), now) is False


# --- within_s imminent margin -------------------------------------------------

def test_future_within_margin_is_true():
    # wake 60s out, margin 120s -> imminent -> True
    now = datetime.now()
    assert wake_timer_elapsed(_iso(now + timedelta(seconds=60)), now, within_s=120) is True


def test_future_beyond_margin_is_false():
    # wake 300s out, margin 120s -> not yet imminent -> False
    now = datetime.now()
    assert wake_timer_elapsed(_iso(now + timedelta(seconds=300)), now, within_s=120) is False


# --- boundary: >= is inclusive ------------------------------------------------

def test_wake_equals_now_is_true():
    now = datetime.now().replace(microsecond=0)
    assert wake_timer_elapsed(_iso(now), now) is True


def test_wake_equals_now_plus_margin_is_true():
    # wake exactly `within_s` in the future -> (now + within_s) == wake -> True
    now = datetime.now().replace(microsecond=0)
    assert wake_timer_elapsed(_iso(now + timedelta(seconds=600)), now, within_s=600) is True


# =============================================================================
# authoritative_earliest_wake_at -- facet-2 of the  consolidation
# (story-b / ). Reads the AUTHORITATIVE store (never the local mirror)
# so an own-cloud cache-invalidation decision cannot sleep through now-due work
# (guard-1139 / ). Tests inject a fake backend + concrete world/agent
# dirs (test-only kwargs), so they are fully hermetic -- no env, no S3, no
# _paths resolution -- yet exercise the REAL FileNotFoundError-skip /
# propagate-on-error / JSONL-parse / min-across-both-queues logic.
# =============================================================================

_WORLD = Path("/tmp/_zeta_fac2_world/aspirations.jsonl")
_AGENT = Path("/tmp/_zeta_fac2_agent/aspirations.jsonl")


class _FakeBackend:
    """read_authoritative_bytes returns pre-seeded bytes per path, or raises the
    seeded exception. A path absent from the map raises FileNotFoundError (the
    'absent in the store' case the helper must SKIP, not fail on)."""

    def __init__(self, by_path):
        # by_path: {Path: bytes | Exception instance | Exception subclass}
        self._by_path = {str(k): v for k, v in by_path.items()}

    def read_authoritative_bytes(self, path):
        val = self._by_path.get(str(path))
        if val is None:
            raise FileNotFoundError(str(path))
        if isinstance(val, type) and issubclass(val, BaseException):
            raise val(str(path))
        if isinstance(val, BaseException):
            raise val
        return val


def _asps_bytes(*goals):
    """One active aspiration wrapping the given goal dicts, as JSONL bytes."""
    return (json.dumps({"id": "asp-x", "status": "active",
                        "goals": list(goals)}) + "\n").encode("utf-8")


def _recurring_due_at(due_dt):
    """A pending recurring goal whose next-due == due_dt (interval 1h,
    lastAchievedAt = due_dt - 1h). Mirrors AC2's 'recurring goal due T+N'."""
    return {"id": "g-rec", "status": "pending", "recurring": True,
            "interval_hours": 1,
            "lastAchievedAt": _iso(due_dt - timedelta(hours=1))}


def _plain_goal():
    """A pending goal with NO timer lane -> _goal_wake_time returns None."""
    return {"id": "g-plain", "status": "pending"}


def test_authoritative_reads_wake_from_store():
    # World store shows a recurring goal due now+10s; agent absent.
    now = datetime.now().replace(microsecond=0)
    be = _FakeBackend({_WORLD: _asps_bytes(_recurring_due_at(now + timedelta(seconds=10)))})
    got = authoritative_earliest_wake_at(now, backend=be,
                                         world_dir=_WORLD.parent, agent_dir=_AGENT.parent)
    assert got is not None
    # ~now+10s: within a 60s margin (imminent) but not yet elapsed.
    assert wake_timer_elapsed(got, now, 60) is True
    assert wake_timer_elapsed(got, now, 0) is False


def test_authoritative_ac2_store_sooner_drives_miss():
    # AC2: the AUTHORITATIVE store shows the goal due T+10s. Whatever a stale
    # LOCAL mirror would have shown (T+300s, outside dry-idle's 60s margin -> HIT)
    # is irrelevant -- the helper reads the STORE, so the caller's
    # wake_timer_elapsed(..., 60) MISSes on the imminent store wake.
    now = datetime.now().replace(microsecond=0)
    be = _FakeBackend({_AGENT: _asps_bytes(_recurring_due_at(now + timedelta(seconds=10)))})
    got = authoritative_earliest_wake_at(now, backend=be,
                                         world_dir=_WORLD.parent, agent_dir=_AGENT.parent)
    assert wake_timer_elapsed(got, now, 60) is True  # dry-idle MIN_SHORTCIRCUIT_S margin -> MISS


def test_authoritative_no_timer_is_none():
    now = datetime.now().replace(microsecond=0)
    be = _FakeBackend({_WORLD: _asps_bytes(_plain_goal(), _plain_goal())})
    assert authoritative_earliest_wake_at(now, backend=be,
                                          world_dir=_WORLD.parent, agent_dir=_AGENT.parent) is None


def test_authoritative_absent_source_skipped_not_fatal():
    # Agent absent (FileNotFoundError); world present with a timer -> world's wake.
    now = datetime.now().replace(microsecond=0)
    be = _FakeBackend({_WORLD: _asps_bytes(_recurring_due_at(now + timedelta(seconds=30)))})
    got = authoritative_earliest_wake_at(now, backend=be,
                                         world_dir=_WORLD.parent, agent_dir=_AGENT.parent)
    assert got is not None and wake_timer_elapsed(got, now, 60) is True


def test_authoritative_both_absent_is_none():
    now = datetime.now().replace(microsecond=0)
    be = _FakeBackend({})  # every path -> FileNotFoundError
    assert authoritative_earliest_wake_at(now, backend=be,
                                          world_dir=_WORLD.parent, agent_dir=_AGENT.parent) is None


def test_authoritative_unexpected_error_propagates():
    # A NON-FileNotFoundError store failure must PROPAGATE so the caller fails
    # open to a MISS (never sleeps on a local-only decision -- guard-1139).
    now = datetime.now().replace(microsecond=0)
    be = _FakeBackend({_AGENT: RuntimeError("s3 unreachable")})
    with pytest.raises(RuntimeError):
        authoritative_earliest_wake_at(now, backend=be,
                                       world_dir=_WORLD.parent, agent_dir=_AGENT.parent)


def test_authoritative_corrupt_line_skipped():
    now = datetime.now().replace(microsecond=0)
    good = json.dumps({"id": "asp-x", "status": "active",
                       "goals": [_recurring_due_at(now + timedelta(seconds=20))]})
    raw = ("{ this is not json\n" + good + "\n").encode("utf-8")
    be = _FakeBackend({_WORLD: raw})
    got = authoritative_earliest_wake_at(now, backend=be,
                                         world_dir=_WORLD.parent, agent_dir=_AGENT.parent)
    assert got is not None and wake_timer_elapsed(got, now, 60) is True


def test_authoritative_min_across_both_queues():
    # Agent due now+300s, world due now+30s -> min is world's now+30s.
    now = datetime.now().replace(microsecond=0)
    be = _FakeBackend({
        _AGENT: _asps_bytes(_recurring_due_at(now + timedelta(seconds=300))),
        _WORLD: _asps_bytes(_recurring_due_at(now + timedelta(seconds=30))),
    })
    got = authoritative_earliest_wake_at(now, backend=be,
                                         world_dir=_WORLD.parent, agent_dir=_AGENT.parent)
    # ~now+30s: imminent within 60s (world's, the min), NOT the agent's now+300s.
    assert wake_timer_elapsed(got, now, 60) is True
    assert wake_timer_elapsed(got, now, 45) is True
    # And it is the WORLD wake (30s), not the agent wake (300s): 300s is beyond 60s.
    parsed_secs = (datetime.fromisoformat(got) - now).total_seconds()
    assert 25 <= parsed_secs <= 35
