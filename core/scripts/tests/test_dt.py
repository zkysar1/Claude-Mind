"""Tests for core/scripts/_dt.parse_naive_iso ().

The shared parser exists to kill the aware->naive TypeError class: the
open-coded ``fromisoformat(str(s).replace("Z", ""))`` idiom returns an AWARE
datetime for offset-bearing input, so the next ``datetime.now() - dt`` (naive)
raises. parse_naive_iso must ALWAYS return a naive datetime (or None) so that
compare never happens.
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _dt import parse_naive_iso  # noqa: E402


def test_naive_input_roundtrips():
    r = parse_naive_iso("2026-07-24T13:00:00")
    assert r == datetime(2026, 7, 24, 13, 0, 0)
    assert r.tzinfo is None


def test_trailing_z_yields_naive():
    r = parse_naive_iso("2026-07-24T13:00:00Z")
    assert r.tzinfo is None
    assert r == datetime(2026, 7, 24, 13, 0, 0)


def test_zero_offset_is_stripped_the_bug_case():
    # THE bug: replace("Z","") left this AWARE -> now-dt TypeError.
    r = parse_naive_iso("2026-07-24T13:00:00+00:00")
    assert r is not None
    assert r.tzinfo is None
    # The whole point: a naive `now` can subtract this without raising.
    _ = datetime.now() - r


def test_nonzero_offset_is_stripped():
    r = parse_naive_iso("2026-07-24T13:00:00-05:00")
    assert r is not None
    assert r.tzinfo is None
    _ = datetime.now() - r  # must not raise


def test_json_quoted_input():
    r = parse_naive_iso('"2026-07-24T13:00:00Z"')
    assert r == datetime(2026, 7, 24, 13, 0, 0)
    assert r.tzinfo is None


def test_none_and_nullish_return_none():
    assert parse_naive_iso(None) is None
    assert parse_naive_iso("") is None
    assert parse_naive_iso("null") is None
    assert parse_naive_iso("NONE") is None


def test_junk_returns_none_not_raises():
    assert parse_naive_iso("not-a-date") is None
    assert parse_naive_iso("2026-13-99") is None


def test_every_result_is_naive_across_a_mixed_batch():
    now = datetime.now()  # naive
    for val in [
        "2026-07-24T13:00:00",
        "2026-07-24T13:00:00Z",
        "2026-07-24T13:00:00+00:00",
        "2026-07-24T13:00:00-05:00",
        "2026-01-01T00:00:00+09:30",
    ]:
        r = parse_naive_iso(val)
        assert r is not None and r.tzinfo is None
        # The invariant the whole module protects:
        _ = now - r  # must never raise TypeError
