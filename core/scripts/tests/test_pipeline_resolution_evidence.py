"""Tests for the resolution-evidence requirement ().

Every CONFIRMED/CORRECTED hypothesis resolution must carry >=1 verifiable
external-evidence pointer so the calibration number is independently auditable
(from the g-303-15 calibration-honesty audit: ~53% of CONFIRMED/CORRECTED
records had no outcome_detail at all).

The detector + validator live in two mirrored modules:
  - core/scripts/pipeline.py            (CLI library; vestigial write path)
  - mind_api/src/world/pipeline_write.py (the LIVE daemon single-writer)

These tests exercise BOTH and assert parity, since the daemon docstring
documents the mirror invariant ("Mirror upstream when these change; a parity
test could enforce") -- this file IS that parity test.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
for _p in (str(CORE_SCRIPTS), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pipeline as cli_pipeline  # noqa: E402  (core/scripts/pipeline.py)
from mind_api.src.world import pipeline_write  # noqa: E402

# (detector, validator, compute_meta, empty_meta) for each module.
CLI = (
    cli_pipeline.has_resolution_evidence,
    cli_pipeline.validate_resolution_evidence,
    cli_pipeline.compute_meta,
    cli_pipeline.empty_meta,
)
DAEMON = (
    pipeline_write._has_resolution_evidence,
    pipeline_write._validate_resolution_evidence,
    pipeline_write._compute_meta,
    pipeline_write._empty_meta,
)
BOTH = [pytest.param(CLI, id="cli"), pytest.param(DAEMON, id="daemon")]

# ---------------------------------------------------------------------------
# Detector: positive cases (each carries exactly one evidence shape)
# ---------------------------------------------------------------------------

POSITIVE_CASES = [
    ("experience_ref", {"experience_ref": "exp-g-303-27"}),
    ("evidence_for_str", {"evidence_for": "log output attached"}),
    ("evidence_for_list", {"evidence_for": ["pointer-a", "pointer-b"]}),
    ("goal_id", {"outcome_detail": "confirmed by g-115-1604 closure"}),
    ("commit_sha", {"outcome_detail": "shipped in commit 7d17a21a6df8"}),
    ("file_line", {"outcome_detail": "see core/scripts/pipeline_write.py:352"}),
    ("script_name", {"outcome_detail": "ran pipeline-read.sh, confirmed empty"}),
    ("rb_id", {"outcome_detail": "encoded rb-2185 from this resolution"}),
    ("guard_id", {"outcome_detail": "added guard-672 to enforce it"}),
    ("msg_id", {"outcome_detail": "per board post msg-20260622-033625-bravo"}),
    ("pct", {"outcome_detail": "accuracy rose to 53.1% over 194 records"}),
    ("session_named", {"outcome_detail": "observed in session 1781782182988"}),
    ("rationale_field", {"rationale": "validated against g-303-15 audit data"}),
    ("links_field", {"links": "core/scripts/foo.py:10"}),
]


@pytest.mark.parametrize("funcs", BOTH)
@pytest.mark.parametrize("label,rec", POSITIVE_CASES,
                         ids=[c[0] for c in POSITIVE_CASES])
def test_detector_positive(funcs, label, rec):
    has_evidence = funcs[0]
    assert has_evidence(rec) is True, f"{label} should be detected as evidence"


# ---------------------------------------------------------------------------
# Detector: negative cases (no verifiable pointer)
# ---------------------------------------------------------------------------

NEGATIVE_CASES = [
    ("empty", {}),
    ("empty_detail", {"outcome_detail": ""}),
    ("prose_only", {"outcome_detail": "It turned out to be true as expected."}),
    ("prose_rationale", {"rationale": "confirmed by careful reasoning, no data"}),
    # A bare 7-digit count must NOT match the commit-SHA pattern (it has no
    # hex letter) and has no other pointer -> no evidence.
    ("pure_number", {"outcome_detail": "we processed 1234567 items total"}),
    ("whitespace_detail", {"outcome_detail": "   \n  "}),
]


@pytest.mark.parametrize("funcs", BOTH)
@pytest.mark.parametrize("label,rec", NEGATIVE_CASES,
                         ids=[c[0] for c in NEGATIVE_CASES])
def test_detector_negative(funcs, label, rec):
    has_evidence = funcs[0]
    assert has_evidence(rec) is False, f"{label} should NOT count as evidence"


# ---------------------------------------------------------------------------
# Validator: raise / pass / exempt / override / no-op
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("funcs", BOTH)
@pytest.mark.parametrize("outcome", ["CONFIRMED", "CORRECTED"])
def test_validator_raises_without_evidence(funcs, outcome):
    validate = funcs[1]
    with pytest.raises(ValueError, match="resolution evidence"):
        validate({"outcome": outcome, "outcome_detail": "it worked, no pointer"})


@pytest.mark.parametrize("funcs", BOTH)
@pytest.mark.parametrize("outcome", ["CONFIRMED", "CORRECTED"])
def test_validator_passes_with_evidence(funcs, outcome):
    validate = funcs[1]
    # Should not raise.
    validate({"outcome": outcome, "outcome_detail": "confirmed via g-303-27"})


@pytest.mark.parametrize("funcs", BOTH)
def test_validator_passes_with_override(funcs):
    validate = funcs[1]
    validate({"outcome": "CONFIRMED",
              "evidence_override": "math proof: the derivation IS the evidence"})


@pytest.mark.parametrize("funcs", BOTH)
def test_validator_empty_override_still_raises(funcs):
    validate = funcs[1]
    with pytest.raises(ValueError):
        validate({"outcome": "CORRECTED", "evidence_override": "   "})


@pytest.mark.parametrize("funcs", BOTH)
@pytest.mark.parametrize("outcome", ["EXPIRED", "UNRESOLVABLE", None])
def test_validator_exempt_non_accuracy_outcomes(funcs, outcome):
    validate = funcs[1]
    # EXPIRED/UNRESOLVABLE validated no prediction; None means not resolved.
    validate({"outcome": outcome})  # must not raise


# ---------------------------------------------------------------------------
# evidence_pct in computed + empty meta
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("funcs", BOTH)
def test_empty_meta_has_evidence_fields(funcs):
    empty_meta = funcs[3]
    acc = empty_meta()["accuracy"]
    assert acc["with_evidence"] == 0
    assert acc["evidence_pct"] == 0.0


@pytest.mark.parametrize("funcs", BOTH)
def test_compute_meta_evidence_pct(funcs):
    compute_meta = funcs[2]
    live = [
        {"stage": "resolved", "outcome": "CONFIRMED",
         "outcome_detail": "confirmed via g-115-1604"},          # has evidence
        {"stage": "resolved", "outcome": "CORRECTED",
         "outcome_detail": "wrong, but no pointer here"},        # no evidence
        {"stage": "resolved", "outcome": "EXPIRED",
         "outcome_detail": "timed out"},                          # not counted
        {"stage": "active", "outcome": None},                     # not resolved
    ]
    meta = compute_meta(live, [])
    acc = meta["accuracy"]
    # resolved_records = CONFIRMED + CORRECTED only (EXPIRED excluded).
    assert acc["total_resolved"] == 2
    assert acc["with_evidence"] == 1
    assert acc["evidence_pct"] == 50.0


@pytest.mark.parametrize("funcs", BOTH)
def test_compute_meta_evidence_pct_zero_when_no_resolved(funcs):
    compute_meta = funcs[2]
    meta = compute_meta([{"stage": "active", "outcome": None}], [])
    assert meta["accuracy"]["evidence_pct"] == 0.0
    assert meta["accuracy"]["with_evidence"] == 0


# ---------------------------------------------------------------------------
# Parity: the two mirrored detectors must agree on every case
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label,rec",
                         [(c[0], c[1]) for c in POSITIVE_CASES + NEGATIVE_CASES],
                         ids=[c[0] for c in POSITIVE_CASES + NEGATIVE_CASES])
def test_detector_parity(label, rec):
    assert (cli_pipeline.has_resolution_evidence(rec)
            == pipeline_write._has_resolution_evidence(rec)), (
        f"CLI/daemon detector disagree on {label}")


# ---------------------------------------------------------------------------
# normalize_record rename value-drop ()
#
# DEFAULT_FIELDS pre-seeds surprise=None on every record. When a record reaches
# normalize carrying BOTH surprise_level (a real value) and surprise=None, the
# both-exist rename branch must copy the value forward before dropping the old
# key — otherwise the value is lost and surprise stays None. Verified root cause
# of 19/48 resolved records having surprise=None. The two mirrored modules
# (vestigial CLI + live daemon writer) must behave identically (parity invariant).
# ---------------------------------------------------------------------------

NORMALIZE_FNS = [
    pytest.param(cli_pipeline.normalize_record, id="cli"),
    pytest.param(pipeline_write._normalize_record, id="daemon"),
]


@pytest.mark.parametrize("normalize", NORMALIZE_FNS)
def test_normalize_preserves_renamed_value_over_none_default(normalize):
    # Canonical incident: surprise_level carries the value, surprise is the
    # pre-seeded None default → the value must survive the rename.
    out = normalize({"surprise_level": 6, "surprise": None})
    assert out["surprise"] == 6
    assert "surprise_level" not in out


@pytest.mark.parametrize("normalize", NORMALIZE_FNS)
def test_normalize_real_new_value_wins_over_old(normalize):
    # When BOTH names carry real values, the new name still wins (unchanged
    # behavior — we only copy when the new name is at its None default).
    out = normalize({"surprise_level": 6, "surprise": 9})
    assert out["surprise"] == 9
    assert "surprise_level" not in out


@pytest.mark.parametrize("normalize", NORMALIZE_FNS)
def test_normalize_old_name_only_carries_value(normalize):
    # Only the old name present → first rename branch, value carried over.
    out = normalize({"surprise_level": 6})
    assert out["surprise"] == 6
    assert "surprise_level" not in out


@pytest.mark.parametrize("normalize", NORMALIZE_FNS)
def test_normalize_legitimate_none_surprise_unchanged(normalize):
    # No surprise_level at all → surprise stays None. guard-562: the fix does
    # NOT change the default for records that legitimately have no value.
    out = normalize({"surprise": None})
    assert out["surprise"] is None


@pytest.mark.parametrize("normalize", NORMALIZE_FNS)
def test_normalize_value_preservation_covers_all_renames(normalize):
    # The fix lives in the generic rename loop, so every rename pair is covered
    # (the goal called this out: outcome_notes/resolved_date/created share the risk).
    out = normalize({"outcome_notes": "ran probe X", "outcome_detail": None})
    assert out["outcome_detail"] == "ran probe X"
    assert "outcome_notes" not in out


# ---------------------------------------------------------------------------
# by_strategy: no `verification` fallback ()
#
# compute_meta used to read r.get("strategy", r.get("verification", "unknown")).
# `verification` holds free-text resolution criteria, so whenever `strategy` was
# absent — measured at ~95% of the corpus — the BIN KEY became prose: 35 bins
# over 42 of 944 resolved records, 34 of them n=1, keys up to 814 chars. The
# breakdown read as populated while the field it stands for was dead. A record
# with no usable strategy now bins under one explicit "unlabeled" key.
# ---------------------------------------------------------------------------

PROSE = ("Run: efs-session-classify.sh --limit 30 --since 2026-08-12T06:00 "
         "--output json. Read excluded_by_floor BEFORE concluding anything.")


@pytest.mark.parametrize("funcs", BOTH)
def test_compute_meta_no_verification_fallback(funcs):
    compute_meta = funcs[2]
    rec = {"stage": "resolved", "outcome": "CONFIRMED", "verification": PROSE}
    bs = compute_meta([rec], [])["accuracy"]["by_strategy"]
    assert PROSE not in bs, "verification prose leaked back into a bin key"
    assert bs == {"unlabeled": {"confirmed": 1, "total": 1, "pct": 100.0}}


@pytest.mark.parametrize("funcs", BOTH)
def test_compute_meta_named_strategy_survives(funcs):
    # The fix must not stop binning records that DO carry a real strategy --
    # otherwise "no prose bins" would be satisfied by an empty breakdown.
    compute_meta = funcs[2]
    rec = {"stage": "resolved", "outcome": "CONFIRMED",
           "strategy": "self_check", "verification": PROSE}
    bs = compute_meta([rec], [])["accuracy"]["by_strategy"]
    assert bs == {"self_check": {"confirmed": 1, "total": 1, "pct": 100.0}}


@pytest.mark.parametrize("funcs", BOTH)
@pytest.mark.parametrize("value", [None, "", "   ", "unknown", {"legacy": "d"}, 7])
def test_compute_meta_unusable_strategy_bins_unlabeled(funcs, value):
    # Every unusable shape lands in the SAME bin, including the legacy dict and
    # the literal "unknown" -- both of which the old code dropped on the floor,
    # so those records appeared in no bin at all.
    compute_meta = funcs[2]
    rec = {"stage": "resolved", "outcome": "CORRECTED", "strategy": value}
    bs = compute_meta([rec], [])["accuracy"]["by_strategy"]
    assert list(bs) == ["unlabeled"]
    assert bs["unlabeled"]["total"] == 1


@pytest.mark.parametrize("funcs", BOTH)
def test_compute_meta_bins_carry_their_denominator(funcs):
    #  outcome 4: an unlabeled count is only readable against a
    # denominator, so every resolved record must land in exactly one bin and
    # the bin totals must sum to total_resolved.
    compute_meta = funcs[2]
    live = [
        {"stage": "resolved", "outcome": "CONFIRMED", "strategy": "self_check"},
        {"stage": "resolved", "outcome": "CORRECTED", "verification": PROSE},
        {"stage": "resolved", "outcome": "CONFIRMED"},
        {"stage": "resolved", "outcome": "EXPIRED"},   # not a resolved record
        {"stage": "active", "outcome": None},          # not a resolved record
    ]
    acc = compute_meta(live, [])["accuracy"]
    assert acc["total_resolved"] == 3
    assert sum(b["total"] for b in acc["by_strategy"].values()) == 3
    assert acc["by_strategy"]["unlabeled"]["total"] == 2


# ---------------------------------------------------------------------------
# Call-site parity: the two compute_meta implementations must agree ENTIRELY
#
# The constraining insight_trigger (bravo, msg-20260729-114459) measured a
# helper call DELETED from the CLI copy with the suite still green: nothing
# asserted parity BETWEEN the tiers, only that each was internally consistent.
# Comparing the WHOLE meta dict rather than one block is what makes any future
# divergence -- in any block, not just the one being edited today -- fail here
# instead of silently in production. guard-547 / guard-2323 / guard-1189.
# ---------------------------------------------------------------------------

PARITY_LIVE = [
    {"id": "h1", "stage": "resolved", "outcome": "CONFIRMED",
     "type": "high-conviction", "horizon": "short", "confidence": 0.9,
     "strategy": "self_check", "outcome_detail": "confirmed by g-115-1604"},
    {"id": "h2", "stage": "resolved", "outcome": "CORRECTED",
     "type": "exploration", "horizon": "session", "confidence": 0.3,
     "verification": PROSE},                       # prose, no strategy
    {"id": "h3", "stage": "resolved", "outcome": "CONFIRMED",
     "type": "calibration", "horizon": "long", "confidence": 0.55,
     "strategy": {"legacy": "dict"}},              # unusable strategy shape
    {"id": "h4", "stage": "active", "outcome": None, "type": "contrarian"},
    {"id": "h5", "stage": "resolved", "outcome": "EXPIRED", "type": "calibration"},
    {"stage": "resolved", "outcome": "CONFIRMED", "type": "calibration"},  # no id
]
PARITY_ARCHIVE = [
    {"id": "h6", "stage": "archived", "outcome": "CORRECTED",
     "type": "contrarian", "horizon": "micro", "confidence": 0.7,
     "strategy": "unknown"},                       # literal "unknown"
    # Same id as a live record: dedup must count it once, archive copy winning.
    {"id": "h1", "stage": "archived", "outcome": "CONFIRMED",
     "type": "high-conviction", "horizon": "short", "confidence": 0.9,
     "strategy": "self_check", "outcome_detail": "confirmed by g-115-1604"},
]


def test_compute_meta_parity():
    assert (cli_pipeline.compute_meta(PARITY_LIVE, PARITY_ARCHIVE)
            == pipeline_write._compute_meta(PARITY_LIVE, PARITY_ARCHIVE)), (
        "CLI and daemon compute_meta disagree -- the tiers have diverged")


def test_empty_meta_parity():
    assert cli_pipeline.empty_meta() == pipeline_write._empty_meta(), (
        "CLI and daemon empty_meta disagree -- a block exists in one tier only")
