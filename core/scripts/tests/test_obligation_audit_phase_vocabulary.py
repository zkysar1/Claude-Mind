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


def test_a_following_SENTENCE_does_not_invalidate_a_true_claim(oa, aoa, schema):
    """The sentence form is the LIVE shape; only the parenthesised one was stripped.

    Measured 2026-09-21 (g-115-10407) over the whole fleet corpus — 14 records, 5
    agents, read out of the remote store because this log is push-only telemetry:
    2 of the 13 false verdicts were caused by nothing but this punctuation. Both were
    `context_budget.zone == tight` at zone tight, corroborated by the record's own
    `claim_banner_zone`. Verbatim head of one of them.
    """
    live = ("context_budget.zone == tight. Steps 8 (tree encoding), 8.5 and 8.55 ran "
            "in full. Step 8.75 ran inline without loading /reflect-on-outcome.")
    assert oa._normalize_condition(live) == "context_budget.zone == tight"
    assert aoa._normalize_condition(live) == "context_budget.zone == tight"
    assert oa._validate("state", live, "tight", "deep", schema) is True
    assert aoa._validate_claim("state", live, schema, "deep", "tight")[0] is True


def test_the_period_cut_needs_the_SPACE_or_it_eats_a_canonical_token(oa):
    """`context_budget.zone` carries a period — cutting on a bare '.' would truncate it.

    This is the whole reason the separator is ". " and not ".". A future simplification
    to `partition(".")` fails here rather than silently scoring every zone claim invalid.
    """
    assert oa._normalize_condition("context_budget.zone == tight") == "context_budget.zone == tight"
    # And the cut takes the EARLIEST separator, whichever punctuation arrives first.
    assert oa._normalize_condition("context_budget.zone == tight. x (y)") == "context_budget.zone == tight"
    assert oa._normalize_condition("context_budget.zone == tight (y). x") == "context_budget.zone == tight"


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
        # ...or wearing a following sentence. Widening the cut must not widen the
        # ALLOW-list: the head before the separator still has to BE a canonical token.
        ("state-update", "I was in a hurry. Zone was tight.", "tight", "deep"),
        # A spark claim carrying a context-budget condition — the live residual shape
        # (4 of 14 fleet records, 2026-09-21). spark allows only `outcome_class ==
        # routine`, so this must keep failing until the SCHEMA changes, not the parser.
        ("spark", "context zone TIGHT. A full spark load would risk truncation.", "tight", "deep"),
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


def test_both_auditors_give_the_SAME_REASON_not_just_the_same_verdict(oa, aoa, schema):
    """A shared boolean is not a shared diagnosis — and only one of them files the goal.

    `obligation-audit.py` is the module that turns these verdicts into an Investigate
    goal, and until 2026-09-21 it recorded no reason at all, so every distinct cause
    reached the reader as "conditions were not true at iteration time". Measured over
    the fleet corpus that day: 5 of 13 false verdicts were `unknown obligation phase`
    (no condition was ever evaluated) and 2 were TRUE conditions rejected on
    punctuation — so that sentence was wrong about 7 of 13. Pin the vocabulary, not
    only the boolean (g-115-10407).

    THE FIRST THREE CASES BELOW ARE NOT ENOUGH, and that is the point of the three
    after them. Fresh-eyes 2026-09-21 (zeta/cc-02) measured this test green while the
    two vocabularies disagreed on EVERY zone case it did not list — the test asserted
    the property it could reach, not the property it names (guard-1806). Verified
    pre-fix, with the two agreeing cases above as the positive control:

        zone="normal"  OA "runtime zone=normal but claim says tight"
                      AOA "banner says zone=normal but claim says tight"
        zone=None      OA "runtime zone=None but claim says tight"   <- leaked the
                                                                       Python literal
                                                                       into a filed goal
                      AOA "banner line missing — citation absent"
        empty spec     OA "unknown obligation phase"
                      AOA "schema disallows this condition"          <- `is None` vs
                                                                       `not spec`

    The reason is a CATEGORY KEY (obligation-audit tallies it into the `BY REASON:`
    breakdown of the goal it files), so two spellings of one cause fragment the tally.
    Keep these cases; they are the only thing standing between the shared vocabulary
    and a silent re-divergence.
    """
    cases = [
        ("bogus-phase", "context_budget.zone == tight", "tight", "deep", "unknown obligation phase"),
        ("spark", "context_budget.zone == tight", "tight", "deep", "schema disallows this condition"),
        ("state-update", "context_budget.zone == tight", "tight", "deep", None),
        # --- the cases the original list avoided ( fresh-eyes) ---
        ("state-update", "context_budget.zone == tight", "normal", "deep",
         "observed zone=normal but claim says tight"),
        ("learning-gate", "context_budget.zone == tight", "fresh", "deep",
         "observed zone=fresh but claim says tight"),
        # An ABSENT zone is a missing citation, never a mismatch — and the reason
        # must not render `None` into the goal this module files.
        ("state-update", "context_budget.zone == tight", None, "deep",
         "zone citation absent"),
    ]
    for phase, condition, zone, outcome, expected_reason in cases:
        a_valid, a_reason = oa._validate_claim(phase, condition, zone, outcome, schema)
        b_valid, b_reason = aoa._validate_claim(phase, condition, schema, outcome, zone)
        assert a_valid is b_valid, f"verdicts diverge on {phase}/{condition}"
        assert a_reason == expected_reason, f"{phase}: got {a_reason!r}"
        assert b_reason == expected_reason, f"{phase} (sibling): got {b_reason!r}"


def test_the_boolean_face_still_exists_for_its_callers(oa, schema):
    """`_validate` is the shape four call sites and the parity test above use."""
    assert oa._validate("state-update", "context_budget.zone == tight", "tight", "deep", schema) is True
    assert oa._validate("bogus-phase", "context_budget.zone == tight", "tight", "deep", schema) is False


def test_a_phase_key_with_an_EMPTY_spec_is_unknown_in_BOTH_auditors(oa, aoa):
    """`obligations.get(phase)` truthiness — `not spec` vs `is None` is a real split.

    Needs a synthetic schema: the live one has no empty-bodied phase, which is
    exactly why the divergence survived. Measured 2026-09-21 (zeta/cc-02) before the
    fix — obligation-audit said `unknown obligation phase` while the twin fell
    through to `schema disallows this condition`, because one tested `not spec` and
    the other `spec is None`. A phase key carrying an empty body governs nothing, so
    "unknown" is the honest category in both.
    """
    synthetic = {"obligations": {"state": {}}, "phase_aliases": {"state-update": "state"}}
    a = oa._validate_claim("state-update", "context_budget.zone == tight", "tight", "deep", synthetic)
    b = aoa._validate_claim("state-update", "context_budget.zone == tight", synthetic, "deep", "tight")
    assert a == b, f"empty-spec verdict/reason diverge: OA={a!r} AOA={b!r}"
    assert a == (False, "unknown obligation phase")
    # POSITIVE CONTROL: the same synthetic schema with a REAL body must still
    # validate, or the assertion above would pass on a schema neither can read.
    populated = {"obligations": {"state": {"abbreviated_allowed_when": ["context_budget.zone == tight"]}},
                 "phase_aliases": {"state-update": "state"}}
    assert oa._validate_claim("state-update", "context_budget.zone == tight", "tight", "deep", populated) == (True, None)
    assert aoa._validate_claim("state-update", "context_budget.zone == tight", populated, "deep", "tight") == (True, None)


# --- The claim's own WORDING () ----------------------------------------------
# `Investigate: false-abbreviation-claims` was filed over two claims whose condition was
# TRUE: state-update and learning-gate at runtime zone tight, each written as
# `zone tight (...)`. `condition not in allowed` was exact membership and only the
# canonical token is listed, so both scored "schema disallows this condition" and the
# runtime check never ran. The rename lives in the schema (`condition_aliases:`, read by
# both auditors); these tests pin it against the REAL schema and keep the runtime in charge.

# Verbatim from agents/zeta/journal/2026/09/2026-09-30.md, the claims behind the goal.
FILED_OVER = [
    ("state-update", "zone tight (Skill body not loaded; mechanical close via recurring-close.sh; "
                     "tree Verified Values + summary STATUS already written in-turn)"),
    ("learning-gate", "zone tight (mechanical phase via recurring-close.sh; no meta-signal; "
                      "goals_completed not a multiple of 5)"),
]


def _allowed_tokens(schema):
    return {t for spec in (schema.get("obligations") or {}).values()
            for t in (spec.get("abbreviated_allowed_when") or [])}


def test_condition_aliases_are_declared_and_every_target_is_a_token_the_schema_allows(schema):
    """An alias that names a token no phase allows would map a claim onto nothing."""
    aliases = schema.get("condition_aliases")
    assert isinstance(aliases, dict) and aliases, "condition_aliases missing from the schema"
    allowed = _allowed_tokens(schema)
    for wording, token in aliases.items():
        assert token in allowed, f"alias {wording!r} -> {token!r}, which no phase allows"
        assert wording == " ".join(str(wording).lower().split()), (
            f"alias key {wording!r} must be lower-cased with single spaces: lookups compare in that form"
        )


@pytest.mark.parametrize("phase,condition", FILED_OVER)
def test_the_claims_the_goal_was_filed_over_validate_when_the_zone_was_tight(oa, aoa, schema, phase, condition):
    """The positive control, and the reason the goal existed: both conditions were TRUE."""
    assert oa._validate_claim(phase, condition, "tight", "deep", schema) == (True, None)
    assert aoa._validate_claim(phase, condition, schema, "deep", "tight") == (True, None)


@pytest.mark.parametrize("phase,condition", FILED_OVER)
@pytest.mark.parametrize("zone", ["normal", "fresh"])
def test_a_wording_alias_does_not_launder_a_claim_the_runtime_contradicts(oa, aoa, schema, phase, condition, zone):
    """The map renames; the runtime check still decides (no rubber stamp)."""
    expected = (False, f"observed zone={zone} but claim says tight")
    assert oa._validate_claim(phase, condition, zone, "deep", schema) == expected
    assert aoa._validate_claim(phase, condition, schema, "deep", zone) == expected


@pytest.mark.parametrize("phase", ["verify", "spark"])
def test_a_routine_wording_alias_does_not_launder_a_deep_checkpoint(oa, aoa, schema, phase):
    """The routine half of the no-laundering control; the zone half is the test above.

    7 of the 12 claims the map turned valid in the corpus snapshot were the routine
    wording in `spark`, the largest alias population, and the zone test could not reach it.
    """
    wordings = [w for w, token in schema["condition_aliases"].items() if token == "outcome_class == routine"]
    # The list is built from the schema, so retargeting or deleting an alias would silently shrink it
    # and the loop would pass over what remained (measured: a sabotage that retargeted one key left
    # this test green). Pin the two wordings the corpus measured; later additions are covered too.
    assert {"outcome_class routine", "outcome_class=routine"} <= set(wordings), wordings
    contradicted = (False, "claim says routine but checkpoint says deep")
    for wording in wordings:
        assert oa._validate_claim(phase, wording, "tight", "deep", schema) == contradicted, wording
        assert aoa._validate_claim(phase, wording, schema, "deep", "tight") == contradicted, wording
        # POSITIVE CONTROL: the same wording is honoured when the checkpoint IS routine.
        assert oa._validate_claim(phase, wording, "tight", "routine", schema) == (True, None), wording
        assert aoa._validate_claim(phase, wording, schema, "routine", "tight") == (True, None), wording


def test_every_alias_reaches_the_canonical_token_in_both_auditors_through_any_punctuation(oa, aoa, schema):
    """Case, spacing, a parenthetical and a following sentence all leave the head intact."""
    for wording, token in schema["condition_aliases"].items():
        variants = [
            wording,
            wording.upper(),
            "  " + wording.replace(" ", "   ") + "  ",
            wording + " (the explanation goes here)",
            wording + ". A following sentence explains.",
        ]
        for variant in variants:
            assert oa._normalize_condition(variant, schema) == token, f"OA {variant!r}"
            assert aoa._normalize_condition(variant, schema) == token, f"AOA {variant!r}"


@pytest.mark.parametrize(
    "condition",
    [
        "zone tightness",              # a longer word, not the alias
        "the zone was tight",          # a different sentence
        "tight zone",                  # a wording the corpus never used, so it is NOT listed
        "outcome_class routine-ish",
        "I was in a hurry (zone tight)",
    ],
)
def test_an_unlisted_wording_passes_through_unchanged_and_still_fails(oa, aoa, schema, condition):
    """Normalizers stay total onto the vocabulary (rb-1915): unknown text surfaces, never matches."""
    head = oa._normalize_condition(condition)  # the schema-free behaviour: the cut only
    assert oa._normalize_condition(condition, schema) == head
    assert aoa._normalize_condition(condition, schema) == head
    assert oa._validate("state-update", condition, "tight", "routine", schema) is False
    assert aoa._validate_claim("state-update", condition, schema, "routine", "tight")[0] is False


def test_an_alias_does_not_widen_what_a_phase_allows(oa, aoa, schema):
    """The alias lands on a canonical token; each phase's own allow-list still gates it."""
    # state and learn allow only the zone token, spark only the routine token.
    cases = [
        ("state-update", "outcome_class routine", "tight", "routine"),
        ("learning-gate", "outcome_class=routine", "tight", "routine"),
        ("spark", "zone tight", "tight", "deep"),
    ]
    for phase, condition, zone, outcome in cases:
        refused = (False, "schema disallows this condition")
        assert oa._validate_claim(phase, condition, zone, outcome, schema) == refused
        assert aoa._validate_claim(phase, condition, schema, outcome, zone) == refused
    # POSITIVE CONTROL: the same wordings are honoured where the phase DOES allow its token.
    assert oa._validate_claim("spark", "outcome_class routine", "tight", "routine", schema) == (True, None)
    assert oa._validate_claim("verify", "zone tight", "tight", "deep", schema) == (True, None)


def test_the_normalizer_without_a_readable_schema_is_the_old_normalizer(oa, aoa):
    """An unreadable schema disables the rename, never the cut, and never raises."""
    for mod in (oa, aoa):
        assert mod._normalize_condition("zone tight (x)") == "zone tight"
        assert mod._normalize_condition("zone tight (x)", {}) == "zone tight"
        assert mod._normalize_condition("zone tight (x)", None) == "zone tight"
