"""Tests for the `position` TYPE gate on hypothesis records ().

Both write paths validate `position` content behind an `isinstance(position, str)`
guard, so a NON-string position bypassed every check — including the stage-name and
type-name rejects the block was written for (the 2026-04-13 malformed-cohort
incident, g-001-158). Measured 2026-08-02 across pipeline.jsonl +
pipeline-archive.jsonl: 77 live records carried bare numbers as position — `84` and
`10000` against boolean claims, `1`/`0` standing in for YES/NO — and the rate was
accelerating (7 in May, 16 in Jun, 52 in Jul).

Both directions are covered per guard-385 / guard-1660:
  - REFUSE: non-string positions, including the exact shapes found in the corpus.
  - ACCEPT: legitimate YES/NO verdicts AND narrative stances.

The narrative-accept cases are the load-bearing regression guard. `position` is
documented as a "narrative claim (string); not the stage enum value"
(pipeline.py help text) and the existing validator's own comment calls a >=20-char
value a "genuine multi-word claim". 90.7% of scoreable records carry a position
that is genuinely distinct from their `claim`. A fix that required YES/NO would
reject the overwhelming majority of correct-by-contract records — so these tests
pin that it does not.
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

import pipeline  # noqa: E402
from mind_api.src.world import pipeline_write  # noqa: E402


def _rec(position) -> dict:
    """Minimal record that passes every check EXCEPT whatever `position` does."""
    return {
        "id": "2026-08-02_position-type-gate-fixture",
        "title": "position type gate fixture",
        "stage": "resolved",
        "horizon": "short",
        "type": "calibration",
        "confidence": 0.5,
        "category": "framework-patterns",
        "formed_date": "2026-08-02",
        "position": position,
    }


# Both validators, run through the same cases. The CLI and daemon paths are
# separate implementations that must stay in lockstep — parametrizing over both
# is what catches a fix applied to only one of them.
VALIDATORS = [
    pytest.param(pipeline.validate_record, id="cli"),
    pytest.param(pipeline_write._validate_record, id="daemon"),
]


# ── REFUSE: non-string positions ────────────────────────────────────────────

@pytest.mark.parametrize("validate", VALIDATORS)
@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(84, id="int-84-real-corpus"),
        pytest.param(10000, id="int-10000-real-corpus"),
        pytest.param(1, id="int-1-yes-surrogate"),
        pytest.param(0, id="int-0-no-surrogate"),
        pytest.param(0.7, id="float-0.7-real-corpus"),
        pytest.param(3, id="int-3-real-corpus"),
        pytest.param(True, id="bool-true"),
        pytest.param(["YES"], id="list"),
        pytest.param({"verdict": "YES"}, id="dict"),
    ],
)
def test_non_string_position_is_refused(validate, bad):
    """A bare number is not a prediction in claim-space or prediction-space.
    Every one of these previously passed validation untouched."""
    with pytest.raises(ValueError, match="[Ii]nvalid position"):
        validate(_rec(bad))


@pytest.mark.parametrize("validate", VALIDATORS)
def test_refusal_names_the_offending_type(validate):
    """The message must name the type, so the writer can see WHY it was refused
    rather than re-reading the validator."""
    with pytest.raises(ValueError, match="int"):
        validate(_rec(84))


# ── ACCEPT: legitimate string positions (the regression guard) ──────────────

@pytest.mark.parametrize("validate", VALIDATORS)
@pytest.mark.parametrize(
    "good",
    [
        pytest.param("YES", id="bare-yes"),
        pytest.param("NO", id="bare-no"),
        pytest.param("yes", id="lowercase-yes"),
        pytest.param("NO -- the sweep will not fire", id="no-with-rationale"),
        # The 90.7% shape: a narrative stance distinct from the claim. Taken
        # verbatim from 2026-05-16_predictive-elimination-shape-corrects-7-of-10.
        pytest.param(
            "rb-960 predictive-elimination CORRECTED pattern continues to hold "
            "forward, not just retrospectively.",
            id="narrative-stance-real-corpus",
        ),
        pytest.param("partial recovery expected", id="multi-word-short"),
    ],
)
def test_legitimate_string_position_still_accepted(validate, good):
    """Must not raise. If this fails, the type gate over-reached into content."""
    validate(_rec(good))


@pytest.mark.parametrize("validate", VALIDATORS)
def test_null_position_behaviour_unchanged(validate):
    """`position` is in REQUIRED_FIELDS, so presence is enforced elsewhere. The
    type gate deliberately does NOT change how an explicit null is treated —
    tightening that is a separate decision, not this fix's scope."""
    validate(_rec(None))


# ── No regression to the existing content checks ────────────────────────────

@pytest.mark.parametrize("validate", VALIDATORS)
@pytest.mark.parametrize(
    "artifact", ["resolved", "discovered", "calibration", "contrarian"]
)
def test_stage_and_type_names_still_refused(validate, artifact):
    """The  rejects the block exists for must keep working — the type
    gate is added ahead of them, not in place of them."""
    with pytest.raises(ValueError, match="[Ii]nvalid position"):
        validate(_rec(artifact))


@pytest.mark.parametrize("validate", VALIDATORS)
def test_single_word_artifact_still_refused(validate):
    """The original prompt-template artifact case ("for") stays refused."""
    with pytest.raises(ValueError, match="[Ii]nvalid position"):
        validate(_rec("for"))


# ── changed_fields scoping () ─────────────────────────────────────
#
# The gate above is whole-record, so once it landed, every record whose
# position predated it was refused by EVERY later mutation — including
# archive_sweep, the mechanism that would have retired it. Measured
# 2026-08-04: 70 records (25 live / 45 archive) immutable for fields the
# write never touched, and position was the ONLY field with such a
# population (a full-corpus validator run over all 1,543 records returned
# position failures and nothing else).
#
# The argument shapes below are the LITERAL ones the production call sites
# pass (guard-920) — pipeline_write.py update_field `{field}`, move
# `set(merge_data) | {"stage"}`, and the two archive_sweep candidate sets.

# One legacy shape per real corpus value class: confidence float, YES/NO
# surrogate, and the large-int sentinels.
LEGACY_POSITIONS = [
    pytest.param(1084, id="int-1084-real-corpus"),
    pytest.param(2, id="int-2-real-corpus"),
    pytest.param(0.75, id="float-0.75-real-corpus"),
]


@pytest.mark.parametrize("validate", VALIDATORS)
@pytest.mark.parametrize("legacy", LEGACY_POSITIONS)
def test_unrelated_single_field_write_accepts_legacy_position(validate, legacy):
    """THE DEFECT. `pipeline-update-field.sh <id> replay_metadata '{}'` does not
    touch position, so a pre-existing one must not refuse it. This is the exact
    write that silently failed on 3 of 10 /replay candidates."""
    validate(_rec(legacy), changed_fields={"replay_metadata"})


@pytest.mark.parametrize("validate", VALIDATORS)
@pytest.mark.parametrize("legacy", LEGACY_POSITIONS)
@pytest.mark.parametrize(
    "sweep_fields",
    [
        pytest.param({"stage", "archived_date"}, id="resolved-archive-branch"),
        pytest.param({"outcome", "outcome_date", "stage", "archived_date"},
                     id="expiry-branch"),
    ],
)
def test_archive_sweep_can_retire_legacy_position(validate, legacy, sweep_fields):
    """SECOND-ORDER. Both archive_sweep branches build a candidate that changes
    only these fields. Validating position there is what made the retirement
    path refuse the very records it exists to retire — the safety net and the
    defect were the same write."""
    validate(_rec(legacy), changed_fields=sweep_fields)


@pytest.mark.parametrize("validate", VALIDATORS)
@pytest.mark.parametrize("legacy", LEGACY_POSITIONS)
def test_write_that_sets_a_bad_position_is_still_refused(validate, legacy):
    """guard-1800 is preserved where it matters: scoping must not become a
    licence to WRITE a bare number."""
    with pytest.raises(ValueError, match="[Ii]nvalid position"):
        validate(_rec(legacy), changed_fields={"position"})


@pytest.mark.parametrize("validate", VALIDATORS)
def test_dotted_position_path_still_triggers_the_check(validate):
    """guard-354: a bare `f == "position"` test would leave a nested
    `position.sub` write as a back door around the gate."""
    with pytest.raises(ValueError, match="[Ii]nvalid position"):
        validate(_rec(84), changed_fields={"position.sub"})


@pytest.mark.parametrize("validate", VALIDATORS)
def test_none_changed_fields_validates_everything(validate):
    """The create path and whole-record-replace callers pass no changed_fields,
    and must keep the pre-g-115-4821 behaviour."""
    with pytest.raises(ValueError, match="[Ii]nvalid position"):
        validate(_rec(84), changed_fields=None)


@pytest.mark.parametrize("validate", VALIDATORS)
def test_move_merge_supplying_position_is_validated(validate):
    """The move path passes `set(merge_data) | {"stage"}`. A merge that
    SUPPLIES position is checked (this is why the pre-fix hand-repair escape
    hatch worked); one that does not is not."""
    with pytest.raises(ValueError, match="[Ii]nvalid position"):
        validate(_rec(84), changed_fields={"position", "outcome", "stage"})
    validate(_rec(84), changed_fields={"outcome", "stage"})


@pytest.mark.parametrize("validate", VALIDATORS)
def test_structural_checks_still_run_under_scoping(validate):
    """guard-330: every write path must still run the FULL-record validator —
    scoping applies to the position VALUE checks alone, never to the
    structural ones. Add-path-only validation is explicitly insufficient."""
    bad_stage = _rec("YES")
    bad_stage["stage"] = "not-a-stage"
    with pytest.raises(ValueError, match="[Ii]nvalid stage"):
        validate(bad_stage, changed_fields={"replay_metadata"})

    missing = _rec("YES")
    del missing["confidence"]
    with pytest.raises(ValueError, match="Missing required fields"):
        validate(missing, changed_fields={"replay_metadata"})
