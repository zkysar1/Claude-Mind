"""Tests for the gate-firings store-composition seam (_gate_log.firings_paths).

g-328-38. The seam exists so `meta/gate-firings.jsonl` can be split into date
segments without touching its three consumers (gate-stats.py,
gate-retirement-eval.py, override-ledger-consume.py), each of which previously
hardcoded the filename.

Two properties carry real risk and are tested as such rather than structurally:

1. SPOOL EXCLUSION. The machine-local spool files share the `gate-firings`
   stem (`gate-firings.spool.jsonl`) but are NOT part of the shared store --
   they are drained into it by gate-firings-flush.py and are listed in
   owncloud_sync._EXCLUDE_NAMES. If the seam picked them up, every consumer
   would double-count the un-flushed tail.

2. SEGMENTS ARE ACTUALLY ADDED. test_segments_change_the_result is the mutation
   proof: it fails if firings_paths is reverted to returning only the legacy
   file, which is exactly the regression that would silently starve consumers
   to a few hours of data while they report a 30-day window.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _gate_log import firings_paths  # noqa: E402


def _write(p: Path, text: str = '{"ts": "2026-07-31T00:00:00"}\n') -> Path:
    p.write_text(text, encoding="utf-8")
    return p


def test_empty_dir_returns_nothing(tmp_path):
    assert firings_paths(tmp_path) == []


def test_legacy_only(tmp_path):
    legacy = _write(tmp_path / "gate-firings.jsonl")
    assert firings_paths(tmp_path) == [legacy]


def test_segments_follow_legacy_in_chronological_order(tmp_path):
    legacy = _write(tmp_path / "gate-firings.jsonl")
    # Deliberately created out of order -- ISO dates sort lexically, so the
    # seam must return them chronologically regardless of creation order.
    seg_c = _write(tmp_path / "gate-firings-2026-08-03.jsonl")
    seg_a = _write(tmp_path / "gate-firings-2026-08-01.jsonl")
    seg_b = _write(tmp_path / "gate-firings-2026-08-02.jsonl")

    assert firings_paths(tmp_path) == [legacy, seg_a, seg_b, seg_c]


def test_segments_without_legacy(tmp_path):
    """After the legacy file ages out, segments alone are the store."""
    seg = _write(tmp_path / "gate-firings-2026-08-01.jsonl")
    assert firings_paths(tmp_path) == [seg]


@pytest.mark.parametrize("spool_name", [
    "gate-firings.spool.jsonl",
    "gate-firings.spool.flushing.jsonl",
])
def test_spool_files_are_excluded(tmp_path, spool_name):
    """The spool shares the stem but is machine-local and NOT part of the store.

    Including it would double-count every un-flushed record in all three
    consumers.
    """
    legacy = _write(tmp_path / "gate-firings.jsonl")
    _write(tmp_path / spool_name)

    assert firings_paths(tmp_path) == [legacy]


def test_spool_excluded_even_when_hyphenated(tmp_path):
    """A hyphenated spool name must still be excluded.

    This test FAILED on first run and the implementation was wrong, not the
    expectation. The original seam globbed `gate-firings-*.jsonl` and excluded
    spools with `name.startswith("gate-firings.spool")` -- keyed on the DOTTED
    production form, which that glob can never produce. So the exclusion was
    structurally dead: it could only ever fire for names that never reached it,
    while `gate-firings-spool.jsonl` sailed through. Replaced by a strict
    date-shaped `_SEGMENT_RE`, which excludes non-segments by construction
    rather than by a denylist needing sync with every future sibling file.
    """
    legacy = _write(tmp_path / "gate-firings.jsonl")
    _write(tmp_path / "gate-firings-spool.jsonl")

    assert firings_paths(tmp_path) == [legacy]


def test_directories_are_not_returned(tmp_path):
    _write(tmp_path / "gate-firings.jsonl")
    (tmp_path / "gate-firings-2026-08-01.jsonl").mkdir()
    assert all(p.is_file() for p in firings_paths(tmp_path))


def test_segments_change_the_result(tmp_path):
    """MUTATION PROOF -- fails if firings_paths reverts to the legacy file only.

    Reading the union must yield strictly more records than the legacy file
    alone. A seam that quietly returns just the legacy path passes every
    structural test above but starves consumers in production, so this asserts
    on CONTENT rather than on path membership.
    """
    _write(tmp_path / "gate-firings.jsonl", '{"ts": "2026-07-01T00:00:00"}\n')
    _write(tmp_path / "gate-firings-2026-08-01.jsonl",
           '{"ts": "2026-08-01T00:00:00"}\n{"ts": "2026-08-01T01:00:00"}\n')

    lines = [ln for p in firings_paths(tmp_path)
             for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]

    assert len(lines) == 3, "segments were not included in the store composition"
    assert any("2026-08-01T01:00:00" in ln for ln in lines)


# ---------------------------------------------------------------------------
# Stage-2 writer contract (). The reader half is tested above; these
# pin the WRITER against it. Splitting a filename convention across two modules
# fails silently -- the writer keeps emitting files the reader ignores, so
# consumers read a few hours and report it as a 30-day window -- so the
# round-trip is asserted directly rather than each half in isolation.
# ---------------------------------------------------------------------------

import datetime as _dt  # noqa: E402
import importlib.util  # noqa: E402

from _gate_log import _SEGMENT_RE, segment_name  # noqa: E402


def _load_flush_module():
    """Import gate-firings-flush.py, whose hyphenated name blocks a plain import."""
    path = Path(__file__).resolve().parents[1] / "gate-firings-flush.py"
    spec = importlib.util.spec_from_file_location("gate_firings_flush", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_segment_name_matches_the_readers_pattern():
    """The writer's filename must satisfy the reader's matcher -- the contract."""
    assert _SEGMENT_RE.match(segment_name(_dt.date(2026, 8, 1)))
    assert segment_name(_dt.date(2026, 8, 1)) == "gate-firings-2026-08-01.jsonl"


def test_segment_name_defaults_to_today():
    assert segment_name() == f"gate-firings-{_dt.datetime.now().date().isoformat()}.jsonl"


def test_writer_target_defaults_to_legacy_store(tmp_path, monkeypatch):
    """Flag OFF must be byte-identical to pre-flag behaviour."""
    mod = _load_flush_module()
    monkeypatch.delenv(mod.SEGMENTED_ENV, raising=False)
    assert mod._store_path(tmp_path) == tmp_path / "gate-firings.jsonl"


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes"])
def test_writer_target_is_a_segment_when_enabled(tmp_path, monkeypatch, value):
    mod = _load_flush_module()
    monkeypatch.setenv(mod.SEGMENTED_ENV, value)
    target = mod._store_path(tmp_path)
    assert target.name == segment_name()
    assert _SEGMENT_RE.match(target.name)


@pytest.mark.parametrize("value", ["", "0", "no", "off", "false"])
def test_flag_is_off_unless_explicitly_truthy(tmp_path, monkeypatch, value):
    mod = _load_flush_module()
    monkeypatch.setenv(mod.SEGMENTED_ENV, value)
    assert mod._store_path(tmp_path) == tmp_path / "gate-firings.jsonl"


def test_writer_output_is_discoverable_by_the_reader(tmp_path, monkeypatch):
    """ROUND-TRIP -- what the writer emits, the reader must find.

    This is the test that would have caught a writer/reader filename drift.
    Asserting each half separately passes even when they disagree; only the
    round-trip binds them.
    """
    mod = _load_flush_module()
    monkeypatch.setenv(mod.SEGMENTED_ENV, "1")

    target = mod._store_path(tmp_path)
    target.write_text('{"ts": "2026-08-01T00:00:00"}\n', encoding="utf-8")

    assert target in firings_paths(tmp_path), (
        "writer emitted a path the reader does not recognise as part of the store"
    )
