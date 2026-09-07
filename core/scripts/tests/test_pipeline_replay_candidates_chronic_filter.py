#!/usr/bin/env python3
"""test_pipeline_replay_candidates_chronic_filter.py — pins the  fix to
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
from datetime import date, timedelta
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
         outcome="CORRECTED", replay_count=3, last_replayed=None):
    """Build a pipeline record. encoded=None omits encoded_via_chronic entirely
    (the common case); encoded=True/False sets it explicitly. last_replayed=None
    omits the field — the partially-stamped defect class (g-115-9221) is exactly
    last_replayed SET with next_review omitted, so both must be independently
    controllable."""
    rm = {"replay_count": replay_count}
    if encoded is not None:
        rm["encoded_via_chronic"] = encoded
    if next_review is not None:
        rm["next_review_date"] = next_review
    if last_replayed is not None:
        rm["last_replayed"] = last_replayed
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
        _rec("arch-encoded", "archived", encoded=True),         # OUT (canonical  case)
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


def test_replay_count_cap_excluded_from_both_stages(tmp_path):
    # : rc>=5 records are source-excluded — the LLM-side archive
    # remedy is a no-op for already-archived records, which cycled forever.
    ids = _read_candidates(
        tmp_path,
        live=[
            _rec("live-rc4", "resolved", replay_count=4),        # IN  (below cap)
            _rec("live-rc5", "resolved", replay_count=5),        # OUT (at cap)
        ],
        archive=[
            _rec("arch-rc5", "archived", replay_count=5),        # OUT (canonical case)
            _rec("arch-rc6", "archived", replay_count=6),        # OUT (above cap)
        ],
    )
    assert "live-rc4" in ids
    assert "live-rc5" not in ids
    assert "arch-rc5" not in ids
    assert "arch-rc6" not in ids


def test_replay_count_string_coerced(tmp_path):
    # replay_count is a string on some records — "5" must still exclude.
    ids = _read_candidates(
        tmp_path,
        live=[
            _rec("string-rc5", "resolved", replay_count="5"),    # OUT (coerced)
            _rec("string-rc2", "resolved", replay_count="2"),    # IN
        ],
        archive=[],
    )
    assert "string-rc5" not in ids
    assert "string-rc2" in ids


def test_replay_count_unparseable_falls_through(tmp_path):
    # Fail-open: an unparseable replay_count must not exclude the record.
    ids = _read_candidates(
        tmp_path,
        live=[_rec("bad-rc", "resolved", replay_count="not-a-number")],
        archive=[],
    )
    assert "bad-rc" in ids


def test_next_review_datetime_form_tolerated(tmp_path):
    #  bundled hardening: a datetime-form next_review_date must not
    # silently defeat the 7-day exclusion (bare date.fromisoformat raises on
    # "YYYY-MM-DDTHH:MM:SS" and the swallowed ValueError meant INCLUDE).
    ids = _read_candidates(
        tmp_path,
        live=[
            _rec("dt-not-due", "resolved", next_review="2999-01-01T09:30:00"),  # OUT (future)
            _rec("dt-due", "resolved", next_review="2000-01-01T09:30:00"),      # IN  (past)
        ],
        archive=[],
    )
    assert "dt-not-due" not in ids
    assert "dt-due" in ids


def test_dual_present_prefers_live_copy_next_review(tmp_path):
    # : read/write copy-preference inversion. A record present in BOTH
    # live (full stage=archived tombstone) AND archive (frozen at first-archival)
    # must be judged by the LIVE copy's fresh replay_metadata — where update_field
    # (live-first) actually stamps replay progress. Here a replay pushed
    # next_review far future on the LIVE copy (→ should defer/exclude), while the
    # frozen ARCHIVE copy still shows a PAST next_review (→ would leak back in if
    # the archive copy won the dedup, as it did before the fix).
    rid = "dual-present"
    live = [_rec(rid, "archived", encoded=None, next_review="2999-01-01", replay_count=3)]
    archive = [_rec(rid, "archived", encoded=None, next_review="2000-01-01", replay_count=1)]
    ids = _read_candidates(tmp_path, live, archive)
    assert rid not in ids, (
        "live copy's future next_review must win the dedup → excluded (no leak). "
        "If this fails, the archive-wins inversion has regressed (g-115-2773)."
    )


def test_dual_present_live_rc_cap_wins(tmp_path):
    # Symmetric proof on the rc>=5 cap: a record replayed to rc=5 on the LIVE
    # copy (via update_field) must be excluded even though the frozen ARCHIVE
    # copy still shows rc=1. Before the fix the archive rc=1 won → the exhausted
    # record leaked back every cycle.
    rid = "dual-rc"
    live = [_rec(rid, "archived", replay_count=5)]      # exhausted on live → OUT
    archive = [_rec(rid, "archived", replay_count=1)]   # frozen → would leak in if it won
    ids = _read_candidates(tmp_path, live, archive)
    assert rid not in ids, "live copy's exhausted replay_count must win → excluded (g-115-2773)"


def test_dual_present_live_freshness_can_include(tmp_path):
    # The inversion cuts both ways: prefer-live must also let a record that is
    # DUE on the live copy surface even if the frozen archive copy had deferred
    # it. Live next_review is past (due) while archive's was future — live wins,
    # so it IS a candidate. Guards against a naive "always exclude dual-present"
    # over-correction.
    rid = "dual-due"
    live = [_rec(rid, "archived", next_review="2000-01-01", replay_count=2)]   # due on live → IN
    archive = [_rec(rid, "archived", next_review="2999-01-01", replay_count=2)]  # deferred (stale)
    ids = _read_candidates(tmp_path, live, archive)
    assert rid in ids, "live copy's past next_review must win → included when genuinely due (g-115-2773)"


# --------------------------------------------------------------------------
#  / guard-6125 — the exclusion must read the FIELD PAIR.
#
# THE BUG: the exclusion tested next_review_date ALONE, under `if next_review:`.
# A record stamped with last_replayed but a NULL next_review_date therefore
# satisfied no comparison at all and re-entered the pool every cycle, however
# recently it had been replayed. replay/SKILL.md claims two layers of
# defense-in-depth; for this population there was ONE, and it was the
# honor-system LLM-side skip in Step 1.
#
# MEASURED 2026-09-06 against the live endpoint: 784 candidates before, 774
# after, 10 removed, 0 newly added — all 10 partially stamped and inside the
# window. Five carried a bare date and five an ISO timestamp, which is why
# test_last_replayed_datetime_form_tolerated below is not hypothetical.
# --------------------------------------------------------------------------

def _days_ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


def test_last_replayed_within_window_excluded_when_next_review_null(tmp_path):
    # THE FIX. next_review_date absent, last_replayed 1 day old → must be held
    # back. Before the fix this record was returned on every single cycle.
    ids = _read_candidates(
        tmp_path,
        live=[_rec("partial-fresh", "resolved", next_review=None,
                   last_replayed=_days_ago(1), replay_count=1)],
        archive=[],
    )
    assert "partial-fresh" not in ids, (
        "a record replayed 1 day ago must be excluded even with next_review_date "
        "NULL — this is the g-115-9221 leak"
    )


def test_last_replayed_outside_window_included_when_next_review_null(tmp_path):
    # The other side of the boundary, and the over-correction guard: once the
    # 7-day window has passed the record is genuinely due again and MUST return.
    # A fix that excluded on last_replayed presence alone would strand it forever.
    ids = _read_candidates(
        tmp_path,
        live=[_rec("partial-stale", "resolved", next_review=None,
                   last_replayed=_days_ago(30), replay_count=1)],
        archive=[],
    )
    assert "partial-stale" in ids, (
        "a record last replayed 30 days ago is due again — excluding it would "
        "convert a leak into a permanent strand"
    )


def test_last_replayed_window_boundary(tmp_path):
    # Pin the boundary explicitly so a future edit to REPLAY_WINDOW_DAYS or to
    # the comparison operator fails loudly rather than shifting silently by a day.
    ids = _read_candidates(
        tmp_path,
        live=[
            _rec("inside-6d", "resolved", next_review=None, last_replayed=_days_ago(6)),
            _rec("outside-8d", "resolved", next_review=None, last_replayed=_days_ago(8)),
        ],
        archive=[],
    )
    assert "inside-6d" not in ids
    assert "outside-8d" in ids


def test_last_replayed_datetime_form_tolerated(tmp_path):
    # guard-6125: last_replayed is written in BOTH bare-date and ISO-timestamp
    # form, and format screens IN a bad writer without ever screening one OUT —
    # 5 of the 10 live leaks carried the timestamp form. A bare
    # date.fromisoformat() raises on it, and the swallowed ValueError would mean
    # INCLUDE, silently restoring the exact bug for half the population.
    ids = _read_candidates(
        tmp_path,
        live=[
            _rec("dt-fresh", "resolved", next_review=None,
                 last_replayed=_days_ago(1) + "T17:58:15"),
            _rec("dt-stale", "resolved", next_review=None,
                 last_replayed=_days_ago(30) + "T17:58:15"),
        ],
        archive=[],
    )
    assert "dt-fresh" not in ids
    assert "dt-stale" in ids


def test_last_replayed_unparseable_falls_through(tmp_path):
    # Fail-open, matching the replay_count and next_review_date branches: an
    # unparseable stamp must not exclude the record.
    ids = _read_candidates(
        tmp_path,
        live=[_rec("bad-lr", "resolved", next_review=None,
                   last_replayed="not-a-date")],
        archive=[],
    )
    assert "bad-lr" in ids


def test_next_review_clause_still_independent(tmp_path):
    # Regression guard on the OTHER clause: a future next_review_date must still
    # exclude on its own, even when last_replayed is old enough to pass. The two
    # branches are independent by design (guard-2024 — neither field's absence is
    # fused into the other's comparison), so each must be able to exclude alone.
    ids = _read_candidates(
        tmp_path,
        live=[_rec("future-nr-old-lr", "resolved", next_review="2999-01-01",
                   last_replayed=_days_ago(30))],
        archive=[],
    )
    assert "future-nr-old-lr" not in ids


def test_neither_field_set_still_included(tmp_path):
    # A never-replayed record carries neither field and must remain a candidate.
    # Reading either field at the wrong nesting level would match every record
    # and silently empty the pool (guard-5676), so pin the both-absent case.
    ids = _read_candidates(
        tmp_path,
        live=[_rec("never-replayed", "resolved", next_review=None,
                   last_replayed=None, replay_count=0)],
        archive=[],
    )
    assert "never-replayed" in ids


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
