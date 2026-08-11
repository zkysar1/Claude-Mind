""" — the verdict closure in `cells-archive.upsert_cell` must describe
ONLY the invocation that actually wrote.

`locked_modify_yaml` routes through `_fileops._rmw_with_conflict_retry`, which
RE-INVOKES the same modifier object on an own-cloud If-Match conflict. Each
attempt re-reads fresh, so the DATA is always current and the mutation was never
wrong. The CLOSURE is what carries state across attempts: `verdict` is assigned
on all five branches and so self-healed, but `evicted` was assigned on exactly
one (evicted-and-added), so an attempt that evicted and then lost the fence
reported an eviction that never happened in the winning attempt.

WHY THESE TESTS INJECT THE CONFLICT (inherited from test_clear_in_flight_cas.py,
g-306-163 F-002, and load-bearing here). `LocalBackend.conflict_error` is the
EMPTY TUPLE, so `except ()` catches nothing, `_rmw_with_conflict_retry`
degenerates to a single transparent pass, and the modifier is invoked exactly
ONCE. guard-955 mandates STORAGE_BACKEND=local for every test run on an
own-cloud box, so WITHOUT this injection no test in this tree can reach the
second invocation and the whole re-invocation defect class is invisible to the
suite. Patch the seam; never reach for own-cloud to make a test see a retry.

WHY THEY DRIVE THE REAL WRAPPER (guard-1829). Two hand-written calls to the
modifier would prove the remedy under strictly easier conditions than production
supplies — whatever sits between the two competing events is a serialization
point, and a remedy demonstrated across one has not been demonstrated at all.
These call `_rmw_with_conflict_retry` itself and let IT decide when to re-enter,
against the REAL `_modifier` closure that `upsert_cell` builds.

Run: STORAGE_BACKEND=local python -m pytest core/scripts/tests/test_cells_archive_conflict_retry.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import _conflict_fixture as CF  # noqa: E402  (shared conflict seam, )
import _fileops  # noqa: E402

# Hyphen-named script -> importlib load (the test_cells_archive.py pattern).
_CELLS_PATH = CORE_SCRIPTS / "cells-archive.py"
_spec = importlib.util.spec_from_file_location("cells_archive_cr", _CELLS_PATH)
cells = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cells)


class _FakeConflict(Exception):
    """Stands in for owncloud_backend.ConflictError (a bare Exception subclass)."""


class _FakeBackend:
    conflict_error = _FakeConflict


def _rec(cell_id, score):
    return {"cell_id": cell_id, "category": "c", "state_signature": f"sig-{cell_id}",
            "trajectory": [cell_id], "score": score, "visits": 1,
            "created_at": "2026-08-03T00:00:00", "updated_at": "2026-08-03T00:00:00"}


def _full_cap(weakest_id="weak-1", weakest_score=0.1):
    """A category at exactly CELL_CAP, with one identifiable weakest incumbent."""
    data = {weakest_id: _rec(weakest_id, weakest_score)}
    for i in range(cells.CELL_CAP - 1):
        data[f"strong-{i}"] = _rec(f"strong-{i}", 0.9)
    assert len(data) == cells.CELL_CAP
    return data


def _drive_one_conflict(monkeypatch, first_data, second_data, *, score=0.95,
                        cell_id="newcell"):
    """Run the REAL retry wrapper against the REAL upsert_cell modifier.

    Attempt 1 sees first_data then loses the CAS fence; attempt 2 sees
    second_data and commits. Returns upsert_cell's verdict dict.
    """
    # Routed through the shared seam (): the module-level `import
    # _fileops` above binds the COLLECTION-time object, so patching it
    # directly is invisible after a sibling suite reloads _fileops.
    CF.patch_conflict_backend(monkeypatch, _FakeBackend())

    payloads = [first_data, second_data]
    attempts = []

    def _fake_locked_modify_yaml(path, modifier, initial=None):
        # Mirrors the real helper's contract: a full re-read->modify->write cycle
        # handed to the REAL retry wrapper, which owns the re-entry decision.
        def _cycle():
            data = payloads[len(attempts)]
            attempts.append(data)
            written = modifier(data)
            if len(attempts) == 1:
                raise _FakeConflict("If-Match fence lost to a concurrent writer")
            return written
        return _fileops._rmw_with_conflict_retry(path, _cycle)

    monkeypatch.setattr(cells, "locked_modify_yaml", _fake_locked_modify_yaml)

    verdict = cells.upsert_cell(
        cell_id, "c", state_signature=f"sig-{cell_id}", trajectory=[cell_id],
        score=score, cells_dir="/unused", now="2026-08-03T12:00:00",
    )
    CF.assert_reinvoked(attempts)
    return verdict


# --- the defect -------------------------------------------------------------

def test_an_evicting_attempt_does_not_leak_its_eviction_into_a_kept(monkeypatch):
    """Attempt 1 is at cap and evicts the weakest to admit the new cell, then
    loses the fence. Attempt 2 re-reads and finds a peer already added the cell,
    so it takes the EXISTING path and evicts nothing.

    The caller must be told nothing was evicted. A leaked id here is a false
    eviction record for a cell that is still present — anything reconciling
    downstream state or accounting for archive churn believes a cell vanished.
    """
    second = _full_cap()
    second["newcell"] = _rec("newcell", 0.99)   # peer got there first, and better

    verdict = _drive_one_conflict(monkeypatch, _full_cap(), second)

    assert verdict["verdict"] == "kept", verdict
    assert verdict["evicted"] is None, (
        f"leaked a losing attempt's eviction: {verdict['evicted']!r}")


def test_an_evicting_attempt_does_not_leak_into_a_rejected_full(monkeypatch):
    """Same shape, other terminal branch: attempt 2 finds the cap full of cells
    that all beat the newcomer, so it rejects. `rejected-full` evicts nothing.

    Worth pinning separately from `kept` because the two reach the return by
    different routes — `kept` exits through the EXISTING block, `rejected-full`
    through the NEW-cell cap block, which is the same block that sets `evicted`.
    """
    verdict = _drive_one_conflict(
        monkeypatch, _full_cap(), _full_cap(weakest_score=0.99), score=0.5)

    assert verdict["verdict"] == "rejected-full", verdict
    assert verdict["evicted"] is None, (
        f"leaked a losing attempt's eviction: {verdict['evicted']!r}")


def test_a_genuine_eviction_is_still_reported(monkeypatch):
    """The reset must not suppress a REAL eviction: when the winning attempt is
    the one that evicts, its id must survive to the caller.

    Without this, a fix that simply hardcoded `evicted=None` would pass both
    tests above while destroying the field's only purpose.
    """
    verdict = _drive_one_conflict(
        monkeypatch, _full_cap(weakest_id="loser-a"), _full_cap(weakest_id="loser-b"))

    assert verdict["verdict"] == "evicted-and-added", verdict
    assert verdict["evicted"] == "loser-b", (
        f"the WINNING attempt evicted loser-b; reported {verdict['evicted']!r}")


# --- permanent negative control (guard-1829) --------------------------------

def test_the_seed_once_variant_is_the_permanent_negative_control(monkeypatch):
    """Reproduce the PRE-FIX shape — seed the closure once outside the modifier,
    never reset it — and prove this harness SEES the leak.

    Without a variant that fails, the three tests above assert something about
    their own shape rather than about the fix: a harness that cannot reach the
    second invocation would report green for any implementation at all. This is
    the instrument's calibration, and it must stay red-capable forever.
    """
    # Routed through the shared seam (): the module-level `import
    # _fileops` above binds the COLLECTION-time object, so patching it
    # directly is invisible after a sibling suite reloads _fileops.
    CF.patch_conflict_backend(monkeypatch, _FakeBackend())

    outcome = {"verdict": None, "evicted": None}   # seeded ONCE — the defect

    def _seed_once_modifier(data):
        existing = data.get("newcell")
        if existing is not None:
            outcome["verdict"] = "kept"
            return data
        if len(data) >= cells.CELL_CAP:
            min_id, min_score = cells._min_score_cell(data)
            if 0.95 > min_score:
                del data[min_id]
                data["newcell"] = _rec("newcell", 0.95)
                outcome["verdict"] = "evicted-and-added"
                outcome["evicted"] = min_id          # branch-conditional, never reset
        return data

    second = _full_cap()
    second["newcell"] = _rec("newcell", 0.99)
    payloads = [_full_cap(), second]
    attempts = []

    def _cycle():
        data = payloads[len(attempts)]
        attempts.append(data)
        written = _seed_once_modifier(data)
        if len(attempts) == 1:
            raise _FakeConflict("If-Match fence lost to a concurrent writer")
        return written

    _fileops._rmw_with_conflict_retry(Path("unused.yaml"), _cycle)

    CF.assert_reinvoked(attempts)
    assert outcome["verdict"] == "kept"
    assert outcome["evicted"] == "weak-1", (
        "the seed-once variant did NOT leak — this harness can no longer detect "
        "the defect, so the passing tests above prove nothing")


# --- scope pin --------------------------------------------------------------

def test_reset_is_keyed_to_the_whole_dict_not_just_the_known_leak():
    """The reset must clear EVERY key from one source of truth.

    Resetting only `evicted` would fix today's leak and silently re-open the
    class the moment another branch-conditional key is added — which is exactly
    how this defect reached production in a file whose own comment already
    described the closure-capture pattern.
    """
    src = _CELLS_PATH.read_text(encoding="utf-8")
    assert "_OUTCOME_DEFAULTS" in src, "the shared default set is gone"
    assert "outcome.clear()" in src, (
        "the reset no longer clears the whole dict — a per-key reset re-opens "
        "the class for any future branch-conditional key")
    # Seed and reset must both read the SAME constant, or they can drift.
    assert src.count("_OUTCOME_DEFAULTS") >= 3, (
        "expected the constant to be defined once and read at both the seed and "
        "the reset")
