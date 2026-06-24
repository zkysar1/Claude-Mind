#!/usr/bin/env python3
"""test_pipeline_replay_candidates_chronic_filter.py — pins the 1 fix to
the `GET /v1/pipeline/read?replay_candidates=1` endpoint (mind_api/src/world/pipeline.py).

THE BUG (g-115-1421): replay_candidates merges pipeline.jsonl + pipeline-archive.jsonl
and filtered only on `reflected` + `next_review_date`. A chronic-CORRECTED hypothesis
that Replay Step 3.6 had ALREADY encoded as a calibration guardrail (marked
replay_metadata.encoded_via_chronic == true) kept re-surfacing as a candidate every
cycle — zero further replay value, ~3-5 wasted cycles each until the rc>=5 archive cap.
Empirically, 7 such archived items were re-surfacing among 176 candidates at fix time.

THE FIX: replay_candidates now excludes records whose replay_metadata.encoded_via_chronic
is True, at the source (bash-gated) — complementing Replay Step 1's LLM-side skip.

These tests call read(ctx) directly with a fake ctx (SimpleNamespace query + paths.world)
against tmp pipeline.jsonl / pipeline-archive.jsonl fixtures. They prove:
  - encoded_via_chronic records are EXCLUDED whether resolved OR archived (the fix);
  - the archive merge still includes non-encoded archived records (no regression);
  - the pre-existing reflected + future-next_review filters still hold (no regression).
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
for p in (str(CORE_SCRIPTS), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from mind_api.src.world import pipeline  # noqa: E402


def _rec(rid, stage, reflected=True, encoded=None, next_review=None,
         outcome="CORRECTED", replay_count=3):
    """Build a pipeline record. encoded=None omits encoded_via_chronic entirely
    (the common case); encoded=True/False sets it explicitly."""
    rm = {"replay_count": replay_count}
    if encoded is not None:
        rm["encoded_via_chronic"] = encoded
    if next_review is not None:
        rm["next_review_date"] = next_review
    return {
        "id": rid, "stage": stage, "reflected": reflected,
        "outcome": outcome, "category": "npc-cognition", "replay_metadata": rm,
    }


def _write_jsonl(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _ctx(world_dir):
    return types.SimpleNamespace(
        query={"replay_candidates": "1"},
        paths=types.SimpleNamespace(world=world_dir),
    )


def _read_candidates(world_dir, live, archive):
    _write_jsonl(world_dir / "pipeline.jsonl", live)
    _write_jsonl(world_dir / "pipeline-archive.jsonl", archive)
    resp = pipeline.read(_ctx(world_dir))
    return {r["id"] for r in json.loads(resp.body)}


def test_encoded_via_chronic_excluded_from_both_stages(tmp_path):
    live = [
        _rec("live-unencoded", "resolved", encoded=None),       # IN  (baseline)
        _rec("live-encoded", "resolved", encoded=True),         # OUT (the fix)
    ]
    archive = [
        _rec("arch-unencoded", "archived", encoded=None),       # IN  (merge preserved)
        _rec("arch-encoded", "archived", encoded=True),         # OUT (canonical 1 case)
    ]
    ids = _read_candidates(tmp_path, live, archive)
    assert "live-unencoded" in ids
    assert "arch-unencoded" in ids
    assert "live-encoded" not in ids
    assert "arch-encoded" not in ids


def test_encoded_false_is_not_excluded(tmp_path):
    # Only `is True` excludes — an explicit False (or absent) must remain a candidate.
    ids = _read_candidates(
        tmp_path,
        live=[_rec("explicit-false", "resolved", encoded=False)],
        archive=[],
    )
    assert "explicit-false" in ids


def test_archive_merge_still_works(tmp_path):
    # Regression guard: the archive is still merged into the candidate pool.
    ids = _read_candidates(
        tmp_path,
        live=[_rec("live-1", "resolved", encoded=None)],
        archive=[_rec("arch-1", "archived", encoded=None)],
    )
    assert ids == {"live-1", "arch-1"}


def test_reflected_filter_intact(tmp_path):
    # Regression guard: unreflected records are still excluded.
    ids = _read_candidates(
        tmp_path,
        live=[
            _rec("reflected-yes", "resolved", reflected=True, encoded=None),
            _rec("reflected-no", "resolved", reflected=False, encoded=None),
        ],
        archive=[],
    )
    assert ids == {"reflected-yes"}


def test_future_next_review_filter_intact(tmp_path):
    # Regression guard: a future next_review_date still defers the candidate.
    ids = _read_candidates(
        tmp_path,
        live=[
            _rec("due", "resolved", encoded=None, next_review="2000-01-01"),
            _rec("not-due", "resolved", encoded=None, next_review="2999-01-01"),
        ],
        archive=[],
    )
    assert "due" in ids
    assert "not-due" not in ids


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
