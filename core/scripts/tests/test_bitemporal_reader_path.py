"""test_bitemporal_reader_path.py — bi-temporal READER path (, rb-335).

The writer path (g-306-35, test_bitemporal_writer_path.py) stamps valid_from /
valid_to on RB / guardrails / beliefs / tree records. This is its reader twin:
given an instant T, retrieve.py returns the record VERSIONS that were valid at T
(valid_from <= T < valid_to; valid_to null = still current), status-agnostic and
without bumping retrieval counters. Without the reader the writer fields are dead
weight — rb-335.

Covers:
  * pure helpers (_parse_iso, _valid_at half-open interval + created floor,
    _as_of_dt_or_raise)
  * the canonical falsification scenario (close-old/insert-new): an as-of query
    at T1 returns the OLD closed version, at T3 returns the NEW open version,
    and the default (as_of=None) returns only the current active record
  * status-agnostic surfacing on the as_of path (a since-retired version valid
    at T still surfaces)
  * NO counter bump on a point-in-time read (and a bump DOES fire on the
    default current-version path)
  * byte-compat: as_of=None is identical to the pre-g-306-36 current-active view
  * malformed as_of raises (loaders + _as_of_dt_or_raise)
  * load_guardrails / load_pattern_signatures / load_beliefs as_of parity

Pure stdlib + PyYAML. Self-contained — never touches the live world directory.
Bootstraps retrieve.py via importlib (same env-stash pattern as
test_retrieve_supplementary_filter.py / test_bitemporal_writer_path.py).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# retrieve.py binds RB_PATH/GUARD_PATH/... from WORLD_DIR at module load; point
# it at a scratch dir BEFORE import. Capture-restore env so sibling tests in the
# same pytest session don't inherit a popped MIND_AGENT ( pattern).
_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")

_TMPDIR = tempfile.mkdtemp(prefix="retrieve-bitemporal-reader-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)

import importlib.util  # noqa: E402

_RETRIEVE_PATH = CORE_SCRIPTS / "retrieve.py"
_spec = importlib.util.spec_from_file_location("retrieve_mod_bitemporal", _RETRIEVE_PATH)
_retrieve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_retrieve)

if _ORIG_MIND_WORLD is not None:
    os.environ["MIND_WORLD"] = _ORIG_MIND_WORLD
elif "MIND_WORLD" in os.environ:
    del os.environ["MIND_WORLD"]
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT


# --- fixtures ---------------------------------------------------------------

def _rb(rb_id, category, *, status="active", valid_from=None, valid_to=None,
        created="2026-05-01", score=0.5):
    rec = {
        "id": rb_id,
        "title": f"entry {rb_id}",
        "content": "...",
        "category": category,
        "status": status,
        "created": created,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "utilization": {
            "retrieval_count": 10,
            "last_retrieved": "2026-05-09",
            "times_helpful": 0,
            "times_noise": 0,
            "utilization_score": score,
        },
    }
    return rec


def _guard(gid, category, *, status="active", valid_from=None, valid_to=None,
           created="2026-05-01", score=0.5):
    return {
        "id": gid,
        "rule": f"rule {gid}",
        "trigger_condition": "always",
        "category": category,
        "status": status,
        "created": created,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "utilization": {
            "retrieval_count": 10,
            "last_retrieved": "2026-05-09",
            "times_helpful": 0,
            "utilization_score": score,
        },
    }


def _seed_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


_T1 = "2026-06-10T00:00:00"   # inside the OLD interval [06-01, 06-15)
_TCUT = "2026-06-15T00:00:00"  # the falsification instant
_T3 = "2026-06-20T00:00:00"   # inside the NEW interval [06-15, +inf)


# --- _parse_iso -------------------------------------------------------------

def test_parse_iso_valid():
    assert _retrieve._parse_iso("2026-06-19T01:00:00") == datetime(2026, 6, 19, 1, 0, 0)


@pytest.mark.parametrize("bad", ["not-a-date", "", "2026-13-01T00:00:00", None, 42, []])
def test_parse_iso_rejects_nonparseable(bad):
    assert _retrieve._parse_iso(bad) is None


# --- _valid_at (half-open interval + lower-bound fallback) -------------------

def test_valid_at_open_interval_current_version():
    """valid_to None => +inf: valid at any T at/after the lower bound."""
    rec = {"valid_from": "2026-06-01T00:00:00", "valid_to": None}
    assert _retrieve._valid_at(rec, datetime.fromisoformat(_T3))
    # before valid_from => not yet valid
    assert not _retrieve._valid_at(rec, datetime.fromisoformat("2026-05-01T00:00:00"))


def test_valid_at_closed_interval_half_open():
    """[valid_from, valid_to): valid_from inclusive, valid_to EXCLUSIVE."""
    rec = {"valid_from": "2026-06-01T00:00:00", "valid_to": _TCUT}
    assert _retrieve._valid_at(rec, datetime.fromisoformat(_T1))            # inside
    assert _retrieve._valid_at(rec, datetime.fromisoformat("2026-06-01T00:00:00"))  # lower inclusive
    assert not _retrieve._valid_at(rec, datetime.fromisoformat(_TCUT))     # upper EXCLUSIVE
    assert not _retrieve._valid_at(rec, datetime.fromisoformat(_T3))       # after


def test_valid_at_falsification_pair_non_overlapping():
    """The close-old/insert-new pair is valid for exactly one version per instant."""
    old = {"valid_from": "2026-06-01T00:00:00", "valid_to": _TCUT}
    new = {"valid_from": _TCUT, "valid_to": None}
    t1, tcut, t3 = (datetime.fromisoformat(x) for x in (_T1, _TCUT, _T3))
    assert _retrieve._valid_at(old, t1) and not _retrieve._valid_at(new, t1)
    # at the cut instant exactly one is valid (new owns the boundary, half-open)
    assert _retrieve._valid_at(new, tcut) and not _retrieve._valid_at(old, tcut)
    assert _retrieve._valid_at(new, t3) and not _retrieve._valid_at(old, t3)


def test_valid_at_created_floor_for_legacy_records():
    """No valid_from => falls back to `created` as the lower bound (a record
    created after T was NOT valid at T)."""
    rec = {"created": "2026-06-12T00:00:00"}  # no valid_from/valid_to
    assert not _retrieve._valid_at(rec, datetime.fromisoformat(_T1))   # 06-10 < created
    assert _retrieve._valid_at(rec, datetime.fromisoformat(_T3))        # 06-20 >= created


def test_valid_at_last_observed_floor_for_beliefs():
    """Beliefs carry last_observed, not created — it is in the fallback chain."""
    rec = {"last_observed": "2026-06-12T00:00:00"}
    assert not _retrieve._valid_at(rec, datetime.fromisoformat(_T1))
    assert _retrieve._valid_at(rec, datetime.fromisoformat(_T3))


def test_valid_at_no_temporal_fields_always_valid():
    """A record with no parseable temporal field has -inf lower / +inf upper."""
    assert _retrieve._valid_at({}, datetime.fromisoformat(_T1))
    # unparseable bounds also degrade to unbounded (fail-open)
    assert _retrieve._valid_at({"valid_from": "junk", "valid_to": "junk"},
                               datetime.fromisoformat(_T1))


# --- _as_of_dt_or_raise -----------------------------------------------------

def test_as_of_dt_or_raise_none_passthrough():
    assert _retrieve._as_of_dt_or_raise(None) is None


def test_as_of_dt_or_raise_parses_valid():
    assert _retrieve._as_of_dt_or_raise(_T1) == datetime.fromisoformat(_T1)


def test_as_of_dt_or_raise_rejects_malformed():
    with pytest.raises(ValueError):
        _retrieve._as_of_dt_or_raise("not-a-date")


# --- load_reasoning_bank: the canonical falsification scenario ---------------

def _seed_rb_falsification(tmp_path):
    """OLD (closed, retired) + NEW (open, active) version of a logical lesson,
    plus an unrelated current record. category match: 'npc-intelligence'."""
    p = tmp_path / "reasoning-bank.jsonl"
    _seed_jsonl(p, [
        _rb("rb-old", "npc-intelligence", status="retired",
            valid_from="2026-06-01T00:00:00", valid_to=_TCUT, created="2026-06-01T00:00:00"),
        _rb("rb-new", "npc-intelligence", status="active",
            valid_from=_TCUT, valid_to=None, created=_TCUT),
        _rb("rb-other", "npc-intelligence", status="active",
            valid_from=None, valid_to=None, created="2026-05-01T00:00:00"),
    ])
    _retrieve.RB_PATH = p
    return p


def test_rb_as_of_t1_returns_old_closed_version(tmp_path):
    _seed_rb_falsification(tmp_path)
    domain, _ = _retrieve.load_reasoning_bank(["npc-intelligence"], read_only=True, as_of=_T1)
    ids = {r["id"] for r in domain}
    # OLD version (retired but valid at T1) surfaces; NEW (not yet valid) does not.
    assert "rb-old" in ids, ids
    assert "rb-new" not in ids, ids
    # rb-other (created 05-01, open) is also valid at T1
    assert "rb-other" in ids, ids


def test_rb_as_of_t3_returns_new_open_version(tmp_path):
    _seed_rb_falsification(tmp_path)
    domain, _ = _retrieve.load_reasoning_bank(["npc-intelligence"], read_only=True, as_of=_T3)
    ids = {r["id"] for r in domain}
    assert "rb-new" in ids, ids
    assert "rb-old" not in ids, ids   # closed at 06-15, excluded at 06-20


def test_rb_as_of_status_agnostic(tmp_path):
    """A since-retired version that was valid at T MUST surface (the status
    filter is dropped on the as_of path — that is the whole point)."""
    _seed_rb_falsification(tmp_path)
    domain, _ = _retrieve.load_reasoning_bank(["npc-intelligence"], read_only=True, as_of=_T1)
    old = next(r for r in domain if r["id"] == "rb-old")
    assert old["status"] == "retired"   # retired, yet returned for as-of T1


def test_rb_as_of_none_is_current_active_only(tmp_path):
    """Byte-compat: as_of=None returns only status==active (rb-old retired)."""
    _seed_rb_falsification(tmp_path)
    domain, _ = _retrieve.load_reasoning_bank(["npc-intelligence"], read_only=True)
    ids = {r["id"] for r in domain}
    assert ids == {"rb-new", "rb-other"}, ids   # rb-old excluded (retired)


# --- no counter bump on a point-in-time read --------------------------------

def test_rb_as_of_never_bumps_counters(tmp_path, monkeypatch):
    _seed_rb_falsification(tmp_path)
    calls = []
    monkeypatch.setattr(_retrieve, "_locked_bump_jsonl",
                        lambda *a, **k: calls.append(a))
    # as_of read with read_only=False: must NOT bump (historical/observational).
    _retrieve.load_reasoning_bank(["npc-intelligence"], read_only=False, as_of=_T1)
    assert calls == [], "as_of read bumped retrieval counters"


def test_rb_current_read_does_bump(tmp_path, monkeypatch):
    """Control: the default (as_of=None, read_only=False) path DOES bump, so the
    no-bump assertion above is meaningful, not vacuous."""
    _seed_rb_falsification(tmp_path)
    calls = []
    monkeypatch.setattr(_retrieve, "_locked_bump_jsonl",
                        lambda *a, **k: calls.append(a))
    _retrieve.load_reasoning_bank(["npc-intelligence"], read_only=False)
    assert len(calls) == 1, "current-version read should bump once"


def test_rb_as_of_malformed_raises(tmp_path):
    _seed_rb_falsification(tmp_path)
    with pytest.raises(ValueError):
        _retrieve.load_reasoning_bank(["npc-intelligence"], read_only=True, as_of="not-a-date")


# --- load_guardrails as_of parity -------------------------------------------

def test_guardrails_as_of_point_in_time(tmp_path):
    p = tmp_path / "guardrails.jsonl"
    _seed_jsonl(p, [
        _guard("guard-old", "framework-architecture", status="retired",
               valid_from="2026-06-01T00:00:00", valid_to=_TCUT, created="2026-06-01T00:00:00"),
        _guard("guard-new", "framework-architecture", status="active",
               valid_from=_TCUT, valid_to=None, created=_TCUT),
    ])
    _retrieve.GUARD_PATH = p
    at_t1 = {g["id"] for g in _retrieve.load_guardrails(["framework-architecture"],
                                                        read_only=True, as_of=_T1)}
    at_t3 = {g["id"] for g in _retrieve.load_guardrails(["framework-architecture"],
                                                        read_only=True, as_of=_T3)}
    current = {g["id"] for g in _retrieve.load_guardrails(["framework-architecture"],
                                                          read_only=True)}
    assert at_t1 == {"guard-old"}, at_t1
    assert at_t3 == {"guard-new"}, at_t3
    assert current == {"guard-new"}, current   # retired old excluded by default


# --- load_pattern_signatures as_of (created-floor fallback) ------------------

def test_pattern_signatures_as_of_created_floor(tmp_path):
    """Pattern sigs carry no valid_from/valid_to — created is the floor, so an
    as-of view is still coherent (a pattern created after T is excluded)."""
    p = tmp_path / "pattern-signatures.jsonl"
    _seed_jsonl(p, [
        {"id": "sig-early", "category": "framework-architecture", "status": "active",
         "created": "2026-06-05T00:00:00", "utilization": {"utilization_score": 0.5}},
        {"id": "sig-late", "category": "framework-architecture", "status": "active",
         "created": _T3, "utilization": {"utilization_score": 0.5}},
    ])
    _retrieve.SIGS_PATH = p
    at_t1 = {s["id"] for s in _retrieve.load_pattern_signatures(
        ["framework-architecture"], read_only=True, as_of=_T1)}
    # sig-early (created 06-05) valid at 06-10; sig-late (created 06-20) is not.
    assert at_t1 == {"sig-early"}, at_t1


# --- load_beliefs as_of -----------------------------------------------------

def test_beliefs_as_of_point_in_time(tmp_path):
    import yaml
    p = tmp_path / "beliefs.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"beliefs": [
        {"id": "bel-old", "status": "weakened",
         "valid_from": "2026-06-01T00:00:00", "valid_to": _TCUT, "last_observed": "2026-06-01T00:00:00"},
        {"id": "bel-new", "status": "active",
         "valid_from": _TCUT, "valid_to": None, "last_observed": _TCUT},
    ]}
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f)
    _retrieve.BELIEFS_PATH = p
    at_t1 = {b["id"] for b in _retrieve.load_beliefs([], as_of=_T1)}
    at_t3 = {b["id"] for b in _retrieve.load_beliefs([], as_of=_T3)}
    current = {b["id"] for b in _retrieve.load_beliefs([])}
    assert at_t1 == {"bel-old"}, at_t1
    assert at_t3 == {"bel-new"}, at_t3
    assert current == {"bel-old", "bel-new"}, current   # both active/weakened now
