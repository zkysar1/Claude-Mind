"""Aggregate-deadline + explicit-transport-timeout pins ().

WHY THIS FILE EXISTS, stated first because it decides what the tests assert.
Every layer of the own-cloud write path was ALREADY bounded -- shell client 90s,
python client 30s, file lock 10s -- and the path still wedged for minutes
(observed 6:14 / 2:37 / 2:06). The defect was never a missing timeout; it was a
missing AGGREGATE DEADLINE over the COMPOSITION of individually-bounded layers.

So these tests pin the PRODUCT, not the presence of a field. A test asserting
only `connect_timeout is not None` would have passed on the broken code on the
day it wedged, which is precisely the trap the goal warned about: "DO NOT close
on 'the config now has a timeout'."

The retry half uses the shared `_conflict_fixture` seam rather than a hand-rolled
stub -- LocalBackend.conflict_error is the EMPTY TUPLE, so `except conflict_cls`
silently matches nothing and a naive test exercises the single-pass branch while
reporting green. `assert_reinvoked` is what makes that failure loud.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _fileops  # noqa: E402
from _conflict_fixture import patch_conflict_backend, assert_reinvoked  # noqa: E402


class _Conflict(Exception):
    """A REAL exception class -- the empty tuple is what the fixture defeats."""


class _FakeBackend:
    conflict_error = _Conflict


# ---------------------------------------------------------------------------
# Budget parsing. Fail-back direction is the point: an unusable value must
# resolve to BOUNDED, never to "no deadline".
# ---------------------------------------------------------------------------

def test_default_when_env_absent(monkeypatch):
    monkeypatch.delenv("MIND_RMW_DEADLINE_SECONDS", raising=False)
    assert _fileops._rmw_deadline_seconds() == _fileops._RMW_DEADLINE_DEFAULT


def test_reads_env(monkeypatch):
    monkeypatch.setenv("MIND_RMW_DEADLINE_SECONDS", "7.5")
    assert _fileops._rmw_deadline_seconds() == 7.5


@pytest.mark.parametrize("raw", ["", "   ", "abc", "0", "0.0", "-1", "nan"])
def test_unusable_values_fall_back_to_bounded(monkeypatch, raw):
    """Non-numeric, zero and negative all fall back to the bounded default.

    `nan` is in the list deliberately: it PARSES as a float, so only the
    `val > 0` test rejects it (nan comparisons are always False). A refactor to
    `val >= 0` or a bare try/except would let nan through as the deadline and
    silently restore the unbounded composition.
    """
    monkeypatch.setenv("MIND_RMW_DEADLINE_SECONDS", raw)
    assert _fileops._rmw_deadline_seconds() == _fileops._RMW_DEADLINE_DEFAULT


def test_deadline_is_resolved_per_call_not_cached_at_import(monkeypatch):
    """The env must be honored without reloading the module."""
    monkeypatch.setenv("MIND_RMW_DEADLINE_SECONDS", "11")
    assert _fileops._rmw_deadline_seconds() == 11
    monkeypatch.setenv("MIND_RMW_DEADLINE_SECONDS", "22")
    assert _fileops._rmw_deadline_seconds() == 22


# ---------------------------------------------------------------------------
# The composition bound.
# ---------------------------------------------------------------------------

def test_ample_budget_still_exhausts_the_cap(monkeypatch):
    """CONTROL -- and it is load-bearing, not ceremony.

    With budget to spare the loop must still use every _CONFLICT_RETRY_CAP
    attempt. Without this, the early-abort test below would pass just as well if
    the retry path were broken outright, and a green pair would prove nothing.
    """
    monkeypatch.setenv("MIND_RMW_DEADLINE_SECONDS", "3600")
    patch_conflict_backend(monkeypatch, _FakeBackend())

    seen = []

    def cycle():
        seen.append(time.monotonic())
        raise _Conflict("simulated optimistic-concurrency conflict")

    with pytest.raises(_Conflict):
        _fileops._rmw_with_conflict_retry(Path("x.yaml"), cycle)
    assert_reinvoked(seen, expected=_fileops._CONFLICT_RETRY_CAP)


def test_spent_budget_aborts_before_the_cap(monkeypatch):
    """Same conflict, same cap, only the budget differs -> 1 cycle, not 3."""
    monkeypatch.setenv("MIND_RMW_DEADLINE_SECONDS", "0.001")
    patch_conflict_backend(monkeypatch, _FakeBackend())

    seen = []

    def cycle():
        seen.append(time.monotonic())
        time.sleep(0.02)  # spend the budget INSIDE the cycle
        raise _Conflict("simulated optimistic-concurrency conflict")

    with pytest.raises(_Conflict):
        _fileops._rmw_with_conflict_retry(Path("x.yaml"), cycle)
    assert_reinvoked(seen, expected=1)


def test_deadline_exhaustion_raises_the_same_conflict_as_cap_exhaustion(monkeypatch):
    """Reusing the cap-exhaustion exception is deliberate: every caller already
    handles it and releases its lock through the same `finally`, so the deadline
    introduces no new failure mode and cannot strand a lock (guard-2227)."""
    monkeypatch.setenv("MIND_RMW_DEADLINE_SECONDS", "0.001")
    patch_conflict_backend(monkeypatch, _FakeBackend())

    def cycle():
        time.sleep(0.02)
        raise _Conflict("simulated")

    with pytest.raises(_Conflict):
        _fileops._rmw_with_conflict_retry(Path("x.yaml"), cycle)


def test_wall_clock_ceiling_holds(monkeypatch):
    """Outcome 3: the call FAILS OPEN inside the stated ceiling.

    The ceiling is asserted as `budget + ONE in-flight cycle`, not as `budget`
    flat -- the check runs BETWEEN cycles and cannot interrupt a cycle already
    running. Overstating it as a hard `budget` ceiling would be the same
    over-claim this goal exists to correct.
    """
    CYCLE, BUDGET = 0.05, 0.01
    monkeypatch.setenv("MIND_RMW_DEADLINE_SECONDS", str(BUDGET))
    patch_conflict_backend(monkeypatch, _FakeBackend())

    def cycle():
        time.sleep(CYCLE)
        raise _Conflict("simulated")

    t0 = time.monotonic()
    with pytest.raises(_Conflict):
        _fileops._rmw_with_conflict_retry(Path("x.yaml"), cycle)
    elapsed = time.monotonic() - t0

    unbounded = CYCLE * _fileops._CONFLICT_RETRY_CAP
    assert elapsed < BUDGET + CYCLE * 1.8, (
        f"elapsed {elapsed:.3f}s exceeded budget+one-cycle "
        f"({BUDGET + CYCLE * 1.8:.3f}s)"
    )
    assert elapsed < unbounded, (
        f"elapsed {elapsed:.3f}s did not beat the unbounded composition "
        f"({unbounded:.3f}s) -- the deadline did not fire"
    )


# ---------------------------------------------------------------------------
# Transport bounds (part 1). Asserted on the config the client is ACTUALLY
# built with, via the real constructor path -- not by re-deriving the values.
# ---------------------------------------------------------------------------

def _capture_client_configs(monkeypatch):
    import owncloud_backend as ob

    captured = {}

    class _RecordingBoto3:
        @staticmethod
        def client(svc, **kw):
            captured[svc] = kw.get("config")
            return object()

    monkeypatch.setattr(ob, "boto3", _RecordingBoto3)
    ob.OwnCloudBackend(env_id="t", bucket="b", lock_table="l",
                       sessions_table="s", cache_root="/tmp/g1155853")
    return captured


def test_transport_timeouts_are_explicit_by_default(monkeypatch):
    monkeypatch.delenv("MIND_S3_CONNECT_TIMEOUT", raising=False)
    monkeypatch.delenv("MIND_S3_READ_TIMEOUT", raising=False)
    captured = _capture_client_configs(monkeypatch)

    cfg = captured["s3"]
    assert cfg.connect_timeout == 10, "connect_timeout must be explicit, not botocore's 60s default"
    assert cfg.read_timeout == 30, "read_timeout must be explicit, not botocore's 60s default"


def test_dynamodb_client_inherits_the_same_bounds(monkeypatch):
    """The lock table rides the SAME _cfg. A DDB hang wedges the RMW just as a
    an S3 hang does, so leaving DDB on the 60s defaults would bound half the path
    and read as bounded."""
    monkeypatch.delenv("MIND_S3_CONNECT_TIMEOUT", raising=False)
    monkeypatch.delenv("MIND_S3_READ_TIMEOUT", raising=False)
    captured = _capture_client_configs(monkeypatch)

    assert captured["dynamodb"].connect_timeout == 10
    assert captured["dynamodb"].read_timeout == 30


def test_transport_timeouts_are_env_overridable(monkeypatch):
    """A box on a slow link must be able to raise them without a code change."""
    monkeypatch.setenv("MIND_S3_CONNECT_TIMEOUT", "5")
    monkeypatch.setenv("MIND_S3_READ_TIMEOUT", "15")
    captured = _capture_client_configs(monkeypatch)

    assert captured["s3"].connect_timeout == 5
    assert captured["s3"].read_timeout == 15


def test_retry_attempts_are_unchanged(monkeypatch):
    """Guard against a future edit 'simplifying' the config and dropping the
    retry policy while adding the timeouts -- the arithmetic in the source
    comment (3 x (10+30)) depends on max_attempts staying 3."""
    captured = _capture_client_configs(monkeypatch)
    assert captured["s3"].retries["max_attempts"] == 3
