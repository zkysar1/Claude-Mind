"""test_pipeline_confidence_bool_rejection.py — .

Python bools ARE ints, so `isinstance(True, (int, float))` is True and
`0 <= True <= 1` passes. A bool confidence therefore sailed through the numeric
bound-check untouched. Measured end-to-end before the fix:

    POST /v1/pipeline/update-field?field=confidence&value=true -> 200
    stored: confidence=True, surprise=0

WHY THE 0 IS THE DAMAGE, not the bool. float(True) is 1.0, so a CONFIRMED record
derives round((1 - 1.0) * 10) = 0. Since g-115-3801 made surprise a pure function
of confidence, that 0 reads as "unsurprising" and SKIPS the /review-hypotheses
Step 3.5 broad re-retrieve — the exact under-stated class g-115-3801 was filed to
end (47 of 158 drifted records). The bound-check gap pre-dates that change; what
made it corrupting was deriving surprise from confidence.

WHY TYPE AND NOT VALUE (the trap this file exists to keep shut): True == 1 and
False == 0, so a value-based guard — `confidence in (0, 1)` or any equality
test — would reject the genuinely VALID confidences 1 and 0.
``test_valid_numerics_still_accepted`` covers exactly 0 and 1 for that reason;
they are not filler cases.

WHY BOTH VALIDATORS, DRIVEN FROM ONE MATRIX (guard-547): the daemon
(mind_api/src/world/pipeline_write.py::_validate_record) and the CLI twin
(core/scripts/pipeline.py::validate_record) are independent implementations of
the same contract, and the documented failure mode is fixing one and letting the
other drift. Every case below runs through BOTH, and
``test_the_two_validators_agree_on_every_case`` fails if they ever diverge —
including on inputs nobody thought to enumerate separately.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
PROJECT_ROOT = CORE_SCRIPTS.parent
for _p in (str(CORE_SCRIPTS), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from mind_api.src.world import pipeline_write  # noqa: E402

_spec = importlib.util.spec_from_file_location("pipeline_cli", CORE_SCRIPTS / "pipeline.py")
pipeline_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pipeline_cli)

DAEMON = pipeline_write._validate_record
CLI = pipeline_cli.validate_record
BOTH = pytest.mark.parametrize("validate", [DAEMON, CLI],
                               ids=["daemon", "cli"])


def _rec(confidence):
    return {
        "id": "2026-08-11_bool-confidence-probe",
        "title": "probe",
        "stage": "discovered",
        "horizon": "session",
        "type": "exploration",
        "confidence": confidence,
        "position": "a plain string position",
        "formed_date": "2026-08-11",
        "category": "framework-architecture",
    }


def _accepts(validate, confidence):
    try:
        validate(_rec(confidence))
        return True
    except ValueError:
        return False


# --- the defect: bools must be refused, in BOTH directions (guard-385/1660) ---

@BOTH
@pytest.mark.parametrize("bad", [True, False])
def test_bool_confidence_is_rejected(validate, bad):
    with pytest.raises(ValueError) as ei:
        validate(_rec(bad))
    msg = str(ei.value)
    assert "confidence" in msg.lower()
    assert "bool" in msg.lower(), (
        "the message must name the actual problem — a caller told only "
        "'must be 0.0-1.0' will reasonably conclude True IS in range, because "
        "arithmetically it is")


# --- anti-vacuity: a guard that rejected too much would pass the cases above --

@BOTH
@pytest.mark.parametrize("good", [0, 0.0, 0.45, 1, 1.0])
def test_valid_numerics_still_accepted(validate, good):
    """0 and 1 are the load-bearing entries. A value-based guard (`== 1`,
    `in (0, 1)`) would reject exactly these two while still refusing bools, so
    without them the rejection tests alone cannot distinguish a correct
    type-based guard from a broken value-based one."""
    assert _accepts(validate, good), f"{good!r} is a valid confidence"


@BOTH
@pytest.mark.parametrize("bad", [-0.1, 1.1, 2, -1])
def test_out_of_range_still_rejected(validate, bad):
    assert not _accepts(validate, bad)


@BOTH
@pytest.mark.parametrize("bad", ["0.5", None, [], {}, "true"])
def test_non_numeric_still_rejected(validate, bad):
    """Includes the string "true" — the shape an untyped query-string write
    arrives as. It must stay rejected by the numeric check, not accidentally
    routed into the new bool branch."""
    assert not _accepts(validate, bad)


# --- guard-547: the twins must not drift apart -------------------------------

@pytest.mark.parametrize("value", [
    True, False, 0, 0.0, 0.45, 1, 1.0, -0.1, 1.1, 2, -1,
    "0.5", "true", None, [], {},
])
def test_the_two_validators_agree_on_every_case(value):
    """The parity guard. Fixing one validator and letting the other drift is the
    documented failure mode here, and it is invisible to any test that drives
    only one of them — the CLI and the daemon serve different call paths, so a
    divergence surfaces as 'it works from the wrapper but not the daemon'."""
    d, c = _accepts(DAEMON, value), _accepts(CLI, value)
    assert d == c, (
        f"validator drift on confidence={value!r}: daemon "
        f"{'accepts' if d else 'rejects'} but CLI {'accepts' if c else 'rejects'}")
