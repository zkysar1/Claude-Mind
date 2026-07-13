"""test_experience_goal_id_backfill.py -- regression for 7.

experience.py builds ids as exp-{goal_id}-{skill_slug} (cmd_archive_goal), so an
id EMBEDS the owning goal-id. But a caller-formed cmd_add record can leave the
goal_id FIELD null while the id still names the goal. The daemon --goal read
filter (mind_api/src/endpoints/experience.py: `rec.get("goal_id") == goal`)
matches on the stored field, so those records are invisible to
`experience-read --goal <id>` -- ~35% (606/1728) of fleet experience records
were blinded, 365 of them carrying a derivable goal-id in their id.

Fix (two parts, both pinned here):
  1. experience.normalize_record derives goal_id from the id when the field is
     null AND the id embeds a canonical g-NNN-NN. NEVER overwrites a present
     value; slug-only ids stay null. Fires on every core read/write path, so
     new records self-heal at write time (forward fix, fleet-wide via shared code).
  2. experience-backfill-goal-id.py repairs existing records at rest (additive,
     idempotent, local-file-only, does not drop records).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

import experience  # conftest puts core/scripts on sys.path


# --------------------------------------------------------------------------
# Part 1: derive helper + normalize_record (pure, backend-free)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("rec_id,expected", [
    ("exp-g-307-01-2026-05-16", "g-307-01"),
    ("exp-g-001-02-delta-20260524", "g-001-02"),
    ("exp-g-250-107-20260526", "g-250-107"),
    ("exp-7-fix", "7"),            # 4-digit tail supported
    ("exp-encode-session-2026-05-25-x", None),        # slug-only, genuinely goal-less
    ("exp-577-behavioral-analysis-20260524", None),   # leading number is not a goal-id
    ("exp-2026-05-16_ohs-perception", None),
    ("", None),
    (None, None),
])
def test_derive_goal_id_from_id(rec_id, expected):
    assert experience.derive_goal_id_from_id(rec_id) == expected


def test_normalize_backfills_null_goal_id():
    rec = {"id": "exp-g-307-01-2026-05-16", "type": "goal_execution", "goal_id": None}
    experience.normalize_record(rec)
    assert rec["goal_id"] == "g-307-01"


def test_normalize_backfills_absent_goal_id_field():
    # goal_id absent entirely -> DEFAULT_FIELDS sets None, then derive fills it.
    rec = {"id": "exp-g-001-02-x", "type": "goal_execution"}
    experience.normalize_record(rec)
    assert rec["goal_id"] == "g-001-02"


def test_normalize_never_overwrites_present_goal_id():
    rec = {"id": "exp-g-307-01-x", "goal_id": "g-999-99"}
    experience.normalize_record(rec)
    assert rec["goal_id"] == "g-999-99"


def test_normalize_slug_only_id_stays_null():
    rec = {"id": "exp-encode-session-x", "goal_id": None}
    experience.normalize_record(rec)
    assert rec["goal_id"] is None


# --------------------------------------------------------------------------
# Part 2: backfill script _apply_file (additive, idempotent, no record drop)
# --------------------------------------------------------------------------

def _load_backfill_module():
    path = Path(__file__).resolve().parents[1] / "experience-backfill-goal-id.py"
    spec = importlib.util.spec_from_file_location("experience_backfill_goal_id", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # module forces STORAGE_BACKEND=local at import
    return mod


def test_backfill_apply_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    bf = _load_backfill_module()
    jsonl = tmp_path / "experience.jsonl"
    records = [
        {"id": "exp--a", "goal_id": None},        # -> 
        {"id": "exp--b", "goal_id": ""},          # empty -> 
        {"id": "exp--c", "goal_id": ""},   # present -> untouched
        {"id": "exp-encode-session-d", "goal_id": None},  # slug-only -> stays null
    ]
    jsonl.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    n = bf._apply_file(str(jsonl))
    assert n == 2  # only the two null + derivable records

    out = [json.loads(l) for l in jsonl.read_text(encoding="utf-8").splitlines() if l.strip()]
    by_id = {r["id"]: r.get("goal_id") for r in out}
    assert by_id["exp-g-307-01-a"] == "g-307-01"
    assert by_id["exp-g-001-02-b"] == "g-001-02"
    assert by_id["exp--c"] == ""    # present value never overwritten
    assert by_id["exp-encode-session-d"] is None    # slug-only stays null
    assert len(out) == 4                            # no records dropped

    # Idempotent: a second apply changes nothing.
    assert bf._apply_file(str(jsonl)) == 0


def test_backfill_missing_file_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    bf = _load_backfill_module()
    assert bf._apply_file(str(tmp_path / "does-not-exist.jsonl")) == 0
