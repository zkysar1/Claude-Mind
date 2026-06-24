"""test_inner_refinement_validation.py —  / BRD Layer-1 Gap 4.

Pins the validation contract for the optional `inner_refinement` Self-Refine
block (Madaan et al., arXiv 2303.17651) added to validate_goal in aspirations.py.

The block is OPTIONAL and default-OFF: a goal without it (or with it null) must
validate exactly as before. When present and non-null it must be a dict
{max_iters: int in [1, INNER_REFINEMENT_MAX_ITERS_CAP], satisficed_when:
non-empty str}.

The max_iters upper bound (INNER_REFINEMENT_MAX_ITERS_CAP) is the TERMINATION
guarantee: a stored max_iters can never exceed the cap, so the Phase 4
generate -> critique -> regenerate loop (aspirations-execute SKILL.md), which
clamps to min(max_iters, CAP), is provably bounded. This test pins both the
constant and the rejection of out-of-range values.

CLI-only by design (guard-547): the daemon _validate_goal validates a minimal
id/status/recurring/interval subset; optional-field validation lives in the CLI
validate_goal, matching reallocatable/abstained_by/intended_agent. So this is a
direct-import unit test of aspirations.validate_goal, not a daemon test.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from aspirations import INNER_REFINEMENT_MAX_ITERS_CAP, validate_goal  # noqa: E402


def _base_goal(**overrides):
    """Minimal goal that passes every OTHER validate_goal check, so a raise can
    only come from the inner_refinement block under test. The description has no
    'Verification outcomes:'/'Verification checks:' markers, so the prose-drift
    gate noops regardless of the empty checks list."""
    g = {
        "id": "g-100-01",
        "status": "pending",
        "description": "A benign goal for inner_refinement validation testing.",
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
    }
    g.update(overrides)
    return g


# ── The cap constant IS the termination bound ──────────────────────────────

def test_cap_constant_is_five():
    """The structural termination bound is a small, fixed integer. If this
    changes, the SKILL.md execution clamp + goal-schemas.md doc must follow."""
    assert INNER_REFINEMENT_MAX_ITERS_CAP == 5
    assert isinstance(INNER_REFINEMENT_MAX_ITERS_CAP, int)


# ── Default OFF: absent / null behave exactly as before (outcome 3) ─────────

def test_absent_inner_refinement_ok():
    validate_goal(_base_goal())  # no inner_refinement key -> no raise


def test_null_inner_refinement_ok():
    validate_goal(_base_goal(inner_refinement=None))  # explicit null -> OFF


# ── Valid blocks accepted, including both boundaries ───────────────────────

def test_valid_block_accepted():
    validate_goal(_base_goal(inner_refinement={
        "max_iters": 3,
        "satisficed_when": "all verification outcomes met",
    }))


def test_max_iters_lower_boundary_accepted():
    validate_goal(_base_goal(inner_refinement={
        "max_iters": 1,
        "satisficed_when": "good enough",
    }))


def test_max_iters_at_cap_accepted():
    validate_goal(_base_goal(inner_refinement={
        "max_iters": INNER_REFINEMENT_MAX_ITERS_CAP,
        "satisficed_when": "good enough",
    }))


# ── max_iters out of range rejected (the termination bound) ────────────────

def test_max_iters_above_cap_rejected():
    with pytest.raises(ValueError, match="max_iters"):
        validate_goal(_base_goal(inner_refinement={
            "max_iters": INNER_REFINEMENT_MAX_ITERS_CAP + 1,
            "satisficed_when": "x",
        }))


def test_max_iters_zero_rejected():
    with pytest.raises(ValueError, match="max_iters"):
        validate_goal(_base_goal(inner_refinement={
            "max_iters": 0,
            "satisficed_when": "x",
        }))


def test_max_iters_negative_rejected():
    with pytest.raises(ValueError, match="max_iters"):
        validate_goal(_base_goal(inner_refinement={
            "max_iters": -1,
            "satisficed_when": "x",
        }))


# ── max_iters wrong type rejected (bool / float / str / missing) ───────────

def test_max_iters_bool_rejected():
    # bool is an int subclass in Python — must be explicitly rejected so
    # True (==1) / False (==0) can't sneak past the range check.
    with pytest.raises(ValueError, match="max_iters"):
        validate_goal(_base_goal(inner_refinement={
            "max_iters": True,
            "satisficed_when": "x",
        }))


def test_max_iters_float_rejected():
    with pytest.raises(ValueError, match="max_iters"):
        validate_goal(_base_goal(inner_refinement={
            "max_iters": 2.5,
            "satisficed_when": "x",
        }))


def test_max_iters_str_rejected():
    with pytest.raises(ValueError, match="max_iters"):
        validate_goal(_base_goal(inner_refinement={
            "max_iters": "3",
            "satisficed_when": "x",
        }))


def test_max_iters_missing_rejected():
    with pytest.raises(ValueError, match="max_iters"):
        validate_goal(_base_goal(inner_refinement={
            "satisficed_when": "x",
        }))


# ── satisficed_when must be a non-empty string ─────────────────────────────

def test_satisficed_when_missing_rejected():
    with pytest.raises(ValueError, match="satisficed_when"):
        validate_goal(_base_goal(inner_refinement={"max_iters": 2}))


def test_satisficed_when_empty_rejected():
    with pytest.raises(ValueError, match="satisficed_when"):
        validate_goal(_base_goal(inner_refinement={
            "max_iters": 2,
            "satisficed_when": "",
        }))


def test_satisficed_when_whitespace_rejected():
    with pytest.raises(ValueError, match="satisficed_when"):
        validate_goal(_base_goal(inner_refinement={
            "max_iters": 2,
            "satisficed_when": "   ",
        }))


def test_satisficed_when_non_str_rejected():
    with pytest.raises(ValueError, match="satisficed_when"):
        validate_goal(_base_goal(inner_refinement={
            "max_iters": 2,
            "satisficed_when": 123,
        }))


# ── inner_refinement wrong outer type rejected ─────────────────────────────

def test_inner_refinement_non_dict_rejected():
    with pytest.raises(ValueError, match="inner_refinement must be a dict"):
        validate_goal(_base_goal(inner_refinement="yes"))


def test_inner_refinement_list_rejected():
    with pytest.raises(ValueError, match="inner_refinement must be a dict"):
        validate_goal(_base_goal(inner_refinement=[1, 2, 3]))
