"""Pin the two phase vocabularies `obligation-audit.py` has to reconcile ().

The journal/loop label the four obligations `verify` / `state-update` / `learning-gate` /
`spark`; `core/config/obligation-schema.yaml` keys them `verify` / `state` / `learn` /
`spark`. Nothing mapped between them, so `obligations.get("state-update")` was always None
and `_validate` returned False for EVERY abbreviation claim on two of the four phases —
independent of the claim text and of the runtime state. Those false verdicts are what filed
the `Investigate: false-abbreviation-claims` goals.

Per rb-1915 this pins BOTH sides against the REAL schema (not a fixture copy of it), so a
rename on either side fails here instead of silently re-opening the gap.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent.parent
SCHEMA_PATH = REPO / "core" / "config" / "obligation-schema.yaml"

# The labels the producer actually writes, paired with the schema key each must reach.
# `_parse_abbreviations` passes the journal label through verbatim, so these ARE the
# strings `_validate` receives in production.
PRODUCER_TO_SCHEMA = {
    "verify": "verify",
    "state-update": "state",
    "learning-gate": "learn",
    "spark": "spark",
}


def _load_module():
    spec = importlib.util.spec_from_file_location("obligation_audit", SCRIPTS / "obligation-audit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def oa():
    return _load_module()


@pytest.fixture(scope="module")
def schema():
    return yaml.safe_load(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_every_producer_label_resolves_to_a_real_schema_key(oa, schema):
    """The normalizer is total onto the schema vocabulary for every label we emit."""
    obligations = schema.get("obligations") or {}
    for producer_label, schema_key in PRODUCER_TO_SCHEMA.items():
        assert oa._normalize_phase(producer_label) == schema_key
        assert schema_key in obligations, (
            f"schema lost key {schema_key!r}; the producer still writes {producer_label!r}"
        )


def test_claims_on_the_renamed_phases_validate_against_runtime(oa, schema):
    """The regression itself: state-update and learning-gate were unvalidatable.

    Both live cc-05 rows claimed `context_budget.zone == tight` with runtime_zone
    `tight` — a condition BOTH phases explicitly allow — and were recorded valid=false.
    """
    for label in ("state-update", "learning-gate"):
        assert oa._validate(label, "context_budget.zone == tight", "tight", "deep", schema) is True


def test_a_trailing_parenthetical_does_not_invalidate_a_true_claim(oa, schema):
    """Recording WHAT was done inline must not cost the claim its validity.

    `condition not in allowed` is exact membership, so the more informative claim failed
    while a bare token passed — the opposite of the incentive the audit wants.
    """
    verbose = "context_budget.zone == tight (skill protocol run inline: tree node updated)"
    assert oa._validate("state-update", verbose, "tight", "deep", schema) is True
    assert oa._normalize_condition(verbose) == "context_budget.zone == tight"


@pytest.mark.parametrize(
    "phase,condition,zone,outcome",
    [
        # The condition is allowed for this phase but did NOT hold at runtime.
        ("state-update", "context_budget.zone == tight", "normal", "deep"),
        # The condition is real but this phase does not allow it.
        ("spark", "context_budget.zone == tight", "tight", "deep"),
        # An unrecognised phase must surface, never be rubber-stamped.
        ("bogus-phase", "context_budget.zone == tight", "tight", "deep"),
        # A genuinely different condition merely wearing a parenthetical.
        ("state-update", "I was in a hurry (tight)", "tight", "deep"),
    ],
)
def test_the_audit_still_refuses_what_it_should(oa, schema, phase, condition, zone, outcome):
    """Negative controls — the fix must not turn the auditor into a rubber stamp."""
    assert oa._validate(phase, condition, zone, outcome, schema) is False


# --- The sibling auditor ( fresh-eyes pass) ---------------------------------
# `abbreviated-obligation-audit.py` validates the SAME claims from the learning-gate skill
# and carried the identical defect independently. The alias map therefore lives in
# obligation-schema.yaml, not in either script, and these tests pin both readers to it.


@pytest.fixture(scope="module")
def aoa():
    spec = importlib.util.spec_from_file_location(
        "abbreviated_obligation_audit", SCRIPTS / "abbreviated-obligation-audit.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_alias_map_is_declared_in_the_schema_not_only_in_the_scripts(schema):
    """Single source of truth — a copy per script is the drift that caused the defect."""
    assert schema.get("phase_aliases") == {"state-update": "state", "learning-gate": "learn"}


def test_both_auditors_agree_on_every_case(oa, aoa, schema):
    """The two validators must not diverge: same claim, same verdict."""
    cases = [
        ("state-update", "context_budget.zone == tight (tree node updated)", "tight", "deep", True),
        ("learning-gate", "context_budget.zone == tight", "tight", "deep", True),
        ("verify", "outcome_class == routine", None, "routine", True),
        ("spark", "outcome_class == routine", None, "routine", True),
        ("state-update", "context_budget.zone == tight", "normal", "deep", False),
        ("spark", "context_budget.zone == tight", "tight", "deep", False),
        ("bogus-phase", "context_budget.zone == tight", "tight", "deep", False),
        ("state-update", "I was in a hurry (tight)", "tight", "deep", False),
    ]
    for phase, condition, zone, outcome, expected in cases:
        a = oa._validate(phase, condition, zone or "normal", outcome, schema)
        b, _reason = aoa._validate_claim(phase, condition, schema, outcome, zone)
        assert a is expected, f"obligation-audit disagreed on {phase}/{condition}"
        assert b is expected, f"abbreviated-obligation-audit disagreed on {phase}/{condition}"


def test_normalizers_fall_back_when_the_schema_is_unreadable(oa, aoa):
    """An unreadable schema must not silently disable the normalizer."""
    for mod, fn in ((oa, oa._normalize_phase), (aoa, aoa._normalize_phase)):
        assert fn("state-update", {}) == "state"
        assert fn("learning-gate", None) == "learn"
        assert fn("verify", {}) == "verify"
