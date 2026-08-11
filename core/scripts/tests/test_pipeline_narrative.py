#!/usr/bin/env python3
"""test_pipeline_narrative.py — pins `GET /v1/pipeline/read?narrative=1`
(mind_api/src/world/pipeline.py), the gap-062 outcome-narrative normalizer.

THE GAP (gap-062, forge_threshold reached 2026-08-02): the outcome narrative is not
always in `outcome_detail`, so every reader hand-rolled the same ten-key fallback
chain. Two independent agents did so within three days, and the first hand-rolled
variant truncated the field before scanning it and reported a false 0-of-10 result.
Satisfied by EXTENSION rather than a new skill: the procedure is deterministic
(id in, first-non-empty-key out), so it belongs on the script surface every caller
already uses, not behind a new LLM-facing entry point.

MEASURED on this deployment 2026-08-04 (echo, cc-03) over the 351-record
replay-candidate population, and each figure below is pinned by a test here:
  - outcome_detail wins on 260 (74.1%) — so 26% of narratives are NOT there;
  - 79 (22.5%) win on a fallback key;
  - 12 (3.4%) are bare under all ten keys — these must be distinguishable from
    the fallback group, which is what narrative_key=None gives the caller;
  - 7 winning values were NOT strings (6 list, 1 dict). A normalizer that assumes
    str raises AttributeError on exactly those, so the coercion is load-bearing.

Tests call read(ctx) directly with a fake ctx (SimpleNamespace query + paths.world)
against tmp pipeline.jsonl / pipeline-archive.jsonl fixtures — same shape as
test_pipeline_replay_candidates_chronic_filter.py.
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


def _write_jsonl(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _ctx(world_dir, **query):
    q = {"narrative": "1"}
    q.update(query)
    return types.SimpleNamespace(
        query=q, paths=types.SimpleNamespace(world=world_dir),
    )


def _read(world_dir, live, archive=(), **query):
    _write_jsonl(world_dir / "pipeline.jsonl", live)
    _write_jsonl(world_dir / "pipeline-archive.jsonl", list(archive))
    resp = pipeline.read(_ctx(world_dir, **query))
    return resp


def _rows(world_dir, live, archive=(), **query):
    return {r["id"]: r for r in json.loads(_read(world_dir, live, archive, **query).body)}


# ── the chain itself ────────────────────────────────────────────────────────

def test_chain_order_is_the_documented_ten_keys():
    # The chain is the contract every caller was re-deriving; pin it literally so a
    # reorder or a dropped key is a test failure rather than a silent behavior change.
    assert pipeline.NARRATIVE_CHAIN == (
        "outcome_detail", "resolution_note", "resolution", "resolution_summary",
        "resolution_evidence", "outcome_note", "reflection_note", "actual_outcome",
        "evidence_for", "rationale",
    )
    # `result` holds the bare verdict ("CONFIRMED"), never prose — it must stay out.
    assert "result" not in pipeline.NARRATIVE_CHAIN


def test_first_non_empty_key_wins_over_later_ones():
    rec = {"id": "x", "outcome_detail": "the real lesson", "rationale": "later key"}
    assert pipeline.narrative_of(rec) == ("outcome_detail", "the real lesson")


def test_falls_through_empty_and_whitespace_only_values():
    # Empty string, whitespace, None and empty containers are all "absent" — a
    # whitespace-only outcome_detail must not shadow a real narrative further down.
    rec = {
        "id": "x", "outcome_detail": "   ", "resolution_note": "", "resolution": None,
        "resolution_summary": [], "resolution_evidence": {},
        "outcome_note": "found here",
    }
    assert pipeline.narrative_of(rec) == ("outcome_note", "found here")


@pytest.mark.parametrize("key", list(pipeline.NARRATIVE_CHAIN))
def test_every_key_in_the_chain_is_reachable(key):
    # Guards against a key that is listed but unreachable (e.g. shadowed by a typo).
    assert pipeline.narrative_of({"id": "x", key: "prose"}) == (key, "prose")


# ── non-string coercion (the 7 measured records) ────────────────────────────

def test_list_valued_narrative_is_joined_not_crashed():
    # 6 of 351 winning values were lists (evidence_for). `.strip()` would raise.
    rec = {"id": "x", "evidence_for": ["first point", "  ", "second point"]}
    key, text = pipeline.narrative_of(rec)
    assert key == "evidence_for"
    assert text == "first point; second point"   # empty element dropped, not rendered


def test_dict_valued_narrative_is_serialized_not_crashed():
    # 1 of 351 winning values was a dict (resolution).
    rec = {"id": "x", "resolution": {"verdict": "CONFIRMED", "by": "echo"}}
    key, text = pipeline.narrative_of(rec)
    assert key == "resolution"
    assert json.loads(text) == {"verdict": "CONFIRMED", "by": "echo"}


def test_empty_container_is_absence_not_a_narrative():
    # An empty list/dict serializes to something truthy under a naive impl ("[]"/"{}"),
    # which would report a narrative where there is none.
    assert pipeline.narrative_of({"id": "x", "evidence_for": [], "resolution": {}}) == (None, "")


def test_non_string_scalar_is_coerced():
    assert pipeline.narrative_of({"id": "x", "outcome_detail": 42}) == ("outcome_detail", "42")


def test_narrative_is_never_truncated():
    # The originating measurement error was a hand-rolled variant that truncated at
    # 500 chars before scanning, producing a false 0-of-10 indicator scan.
    long = "z" * 5000
    key, text = pipeline.narrative_of({"id": "x", "outcome_detail": long})
    assert (key, len(text)) == ("outcome_detail", 5000)


# ── endpoint behavior ───────────────────────────────────────────────────────

def test_bare_record_reports_null_key_not_a_blank_string(tmp_path):
    # THE POINT of the flag: "recorded under another key" and "never recorded" must
    # be distinguishable. narrative_key=None is the discriminator.
    rows = _rows(tmp_path, live=[
        {"id": "fallback", "stage": "resolved", "resolution_note": "lesson"},
        {"id": "bare", "stage": "resolved", "result": "CONFIRMED"},
    ])
    assert rows["fallback"]["narrative_key"] == "resolution_note"
    assert rows["bare"]["narrative_key"] is None
    assert rows["bare"]["narrative"] == ""
    assert rows["bare"]["chars"] == 0


def test_covers_live_and_archive_union(tmp_path):
    rows = _rows(
        tmp_path,
        live=[{"id": "live-1", "stage": "resolved", "outcome_detail": "a"}],
        archive=[{"id": "arch-1", "stage": "archived", "outcome_detail": "b"}],
    )
    assert set(rows) == {"live-1", "arch-1"}


def test_live_copy_wins_the_dedup(tmp_path):
    # A tombstoned id lives in BOTH files; update_field writes to the LIVE copy, so
    # the live narrative is the fresh one (same ordering rationale as replay_candidates).
    rows = _rows(
        tmp_path,
        live=[{"id": "dup", "stage": "archived", "outcome_detail": "fresh"}],
        archive=[{"id": "dup", "stage": "archived", "outcome_detail": "stale"}],
    )
    assert len(rows) == 1
    assert rows["dup"]["narrative"] == "fresh"


def test_id_composes_and_returns_one_record(tmp_path):
    # The id branch returns the raw record and sits earlier in read(); narrative must
    # take precedence or `--narrative --id X` silently answers the wrong question.
    rows = _rows(
        tmp_path,
        live=[{"id": "a", "stage": "resolved", "outcome_detail": "aa"},
              {"id": "b", "stage": "resolved", "outcome_detail": "bb"}],
        id="b",
    )
    assert set(rows) == {"b"}
    assert rows["b"]["narrative"] == "bb"


def test_id_finds_archive_only_records(tmp_path):
    rows = _rows(
        tmp_path,
        live=[],
        archive=[{"id": "gone", "stage": "archived", "rationale": "r"}],
        id="gone",
    )
    assert rows["gone"]["narrative_key"] == "rationale"


def test_unknown_id_is_404(tmp_path):
    resp = _read(tmp_path, live=[{"id": "a", "stage": "resolved"}], id="nope")
    assert resp.status == 404


def test_stage_composes_as_a_filter(tmp_path):
    rows = _rows(
        tmp_path,
        live=[{"id": "r1", "stage": "resolved", "outcome_detail": "x"},
              {"id": "a1", "stage": "active", "rationale": "y"}],
        stage="resolved",
    )
    assert set(rows) == {"r1"}


def test_archived_stage_asymmetry_is_pinned(tmp_path):
    # Surfaced by the fresh-eyes probe on this same goal, measured on the live world:
    # `--stage archived` returned 829 and `--narrative --stage archived` returned 827.
    # Cause: the bare stage branch reads the ARCHIVE FILE ONLY and skips the stage
    # filter, while narrative unions live+archive (live wins) and filters on the
    # record's own stage. An id in BOTH files with different stages resolves to the
    # LIVE copy and is then excluded. That is the fresher answer, so this pins the
    # behavior rather than "fixing" it toward the stale archived label.
    rows = _rows(
        tmp_path,
        live=[{"id": "reopened", "stage": "discovered", "outcome_detail": "live copy"}],
        archive=[{"id": "reopened", "stage": "archived", "outcome_detail": "stale copy"}],
        stage="archived",
    )
    assert rows == {}, "live copy (stage=discovered) wins dedup, so it fails the archived filter"

    # ...and the same id IS reachable under its live stage, with the live narrative.
    rows = _rows(
        tmp_path,
        live=[{"id": "reopened", "stage": "discovered", "outcome_detail": "live copy"}],
        archive=[{"id": "reopened", "stage": "archived", "outcome_detail": "stale copy"}],
        stage="discovered",
    )
    assert rows["reopened"]["narrative"] == "live copy"


def test_invalid_stage_is_400(tmp_path):
    resp = _read(tmp_path, live=[{"id": "a", "stage": "resolved"}], stage="bogus")
    assert resp.status == 400


def test_row_shape_is_stable(tmp_path):
    rows = _rows(tmp_path, live=[
        {"id": "a", "stage": "resolved", "outcome": "CONFIRMED", "outcome_detail": "hi"},
    ])
    assert rows["a"] == {
        "id": "a", "stage": "resolved", "outcome": "CONFIRMED",
        "narrative_key": "outcome_detail", "narrative": "hi", "chars": 2,
    }


# ── no-regression on the flags that already existed ─────────────────────────

def test_narrative_absent_leaves_id_branch_untouched(tmp_path):
    # Precedence was justified by "nothing existing sets narrative" — prove the
    # existing id branch still returns the RAW record when narrative is not asked for.
    _write_jsonl(tmp_path / "pipeline.jsonl",
                 [{"id": "a", "stage": "resolved", "outcome_detail": "hi", "extra": 1}])
    _write_jsonl(tmp_path / "pipeline-archive.jsonl", [])
    ctx = types.SimpleNamespace(query={"id": "a"},
                                paths=types.SimpleNamespace(world=tmp_path))
    body = json.loads(pipeline.read(ctx).body)
    assert body["extra"] == 1          # raw record, not a narrative row
    assert "narrative_key" not in body


def test_narrative_is_offered_in_the_missing_flag_error(tmp_path):
    ctx = types.SimpleNamespace(query={}, paths=types.SimpleNamespace(world=tmp_path))
    resp = pipeline.read(ctx)
    assert resp.status == 400
    body = resp.body
    assert "narrative" in (body.decode() if isinstance(body, bytes) else body)
