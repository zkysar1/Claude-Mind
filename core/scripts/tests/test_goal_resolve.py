""" — a goal id must never resolve to bare "not found" when the
framework still knows what happened to it.

THE DEFECT THIS PINS. `aspirations-query.sh --goal-field id` returns EMPTY for an
EVICTED goal, and empty reads as never-existed. Three investigations were opened
on that ambiguity and 33 live goals sit deferred against ids whose dispositions
were recorded the whole time in `archived_census.evicted_ids`.

READ THE MUTATION CONTRACT BEFORE ADDING ANYTHING HERE.

A do-nothing implementation — one that only ever scans the two JSONL stores —
returns `unknown` for every evicted id, which is byte-for-byte the behaviour the
script exists to remove. So `test_evicted_resolves_as_evicted_not_unknown` is the
load-bearing assertion: it is the only one that a census-blind resolver fails.
`test_live_*` and `test_archived_*` are POSITIVE CONTROLS in the opposite
direction — a census-blind resolver passes both, so neither discriminates, and
they are here to prove the census lookup did not break ordinary resolution.

`test_unknown_stays_unknown` is the NEGATIVE control and it matters as much as
the positive one: over-claiming `evicted` for an id nobody ever minted would turn
a benign gap into fabricated provenance. And `test_prose_reference_is_not_a_record`
pins the prefilter trap that `retrieve.py` documents in its own census lookup — an
id appearing in ANOTHER goal's description must never resolve as that goal.

Ask of anything you add: would a resolver that ignored archived_census entirely
still satisfy your assertion? If yes, it is not a control.

Run: STORAGE_BACKEND=local python -m pytest core/scripts/tests/test_goal_resolve.py -q
"""

import importlib.util
import json
import os
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "goal-resolve.py"


def _load():
    spec = importlib.util.spec_from_file_location("goal_resolve_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(world, fname, records):
    p = Path(world) / fname
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


@pytest.fixture
def world(tmp_path):
    """A minimal world holding one of each disposition."""
    w = tmp_path / "world"
    w.mkdir()
    _write(w, "aspirations.jsonl", [{
        "id": "asp-115",
        "goals": [
            {"id": "g-115-1", "status": "pending", "title": "a live goal",
             "description": "mentions g-115-777 only as prose"},
        ],
        # post-cutover census: an ID SET, the tombstone shape
        "archived_census": {"evicted_ids": {"completed": ["g-115-500"],
                                            "skipped": ["g-115-501"]}},
    }])
    _write(w, "aspirations-archive.jsonl", [{
        "id": "asp-004",
        "goals": [{"id": "g-004-07", "status": "completed", "title": "an archived goal"}],
    }])
    return str(w)


# --- LOAD-BEARING: the only assertion a census-blind resolver fails ----------

def test_evicted_resolves_as_evicted_not_unknown(world):
    mod = _load()
    r = mod.resolve("g-115-500", world)
    assert r["disposition"] == "evicted", (
        "an evicted goal resolved %r — a census-blind resolver returns 'unknown' "
        "here, which is exactly the ambiguity g-115-6818 was filed about"
        % r["disposition"]
    )
    assert r["status"] == "completed", "the census carries the disposition; report it"
    assert r["aspiration_id"] == "asp-115"


def test_evicted_reports_its_own_status_not_a_fixed_one(world):
    """Two evicted ids under DIFFERENT statuses must not collapse to one answer."""
    mod = _load()
    assert mod.resolve("g-115-500", world)["status"] == "completed"
    assert mod.resolve("g-115-501", world)["status"] == "skipped"


# --- POSITIVE CONTROLS: must stay green (a census-blind resolver passes these) ---

def test_live_record_resolves_live(world):
    mod = _load()
    r = mod.resolve("g-115-1", world)
    assert r["disposition"] == "live" and r["status"] == "pending"
    assert r["title"] == "a live goal"


def test_archived_record_resolves_archived(world):
    mod = _load()
    r = mod.resolve("g-004-07", world)
    assert r["disposition"] == "archived" and r["status"] == "completed"


def test_record_wins_over_census(world):
    """A real record must outrank a census entry — a resurrected goal is LIVE."""
    mod = _load()
    _write(world, "aspirations.jsonl", [{
        "id": "asp-115",
        "goals": [{"id": "g-115-500", "status": "in-progress", "title": "resurrected"}],
        "archived_census": {"evicted_ids": {"completed": ["g-115-500"]}},
    }])
    r = mod.resolve("g-115-500", world)
    assert r["disposition"] == "live", (
        "the census is a TOMBSTONE, not an override — a goal that came back must "
        "resolve to its live record"
    )
    assert r["status"] == "in-progress"


# --- NEGATIVE CONTROLS: over-claiming is the opposite failure ----------------

def test_unknown_stays_unknown(world):
    """Never invent provenance for an id nobody minted."""
    mod = _load()
    r = mod.resolve("g-999-9999", world)
    assert r["disposition"] == "unknown"
    assert r["status"] is None


def test_prose_reference_is_not_a_record(world):
    """ appears only inside another goal's description text.

    retrieve.py documents this exact trap in its own census lookup: the cheap
    substring prefilter matches the LINE, so a resolver that returns on the
    prefilter reports a reference as a record.
    """
    mod = _load()
    assert mod.resolve("g-115-777", world)["disposition"] == "unknown"


def test_legacy_counts_only_census_does_not_fabricate(world):
    """A legacy `by_status` census carries COUNTS and no ids.

    It is structurally unenumerable, so those goals must resolve `unknown` — an
    honest blind lane. Guessing an id into it would be fabrication.
    """
    mod = _load()
    _write(world, "aspirations.jsonl", [{
        "id": "asp-248",
        "goals": [{"id": "g-248-1", "status": "pending", "title": "x"}],
        "archived_census": {"by_status": {"completed": 59}},
    }])
    r = mod.resolve("g-248-40", world)
    assert r["disposition"] == "unknown", (
        "a counts-only census must not be reported as a resolved eviction"
    )


def test_missing_stores_do_not_raise(tmp_path):
    """A world with no aspiration files at all resolves cleanly."""
    mod = _load()
    empty = tmp_path / "empty"
    empty.mkdir()
    assert mod.resolve("g-115-1", str(empty))["disposition"] == "unknown"
