"""Filing-time validator for structured verification checks ().

The three verdicts are tested as three separate populations because collapsing any
pair defeats the gate:

  invalid -> REFUSE   the check names a type or omits a field the evaluator needs
  vacuous -> WARN     well-formed but already satisfied, so it gates nothing
  ok      -> ALLOW    correctly formed and the work is simply not done yet

`ok` is the LOAD-BEARING NEGATIVE. A gate that refuses every failing check would
refuse every correct one too, since at filing time correct checks fail by design.
If that case ever goes red the gate has become a filing blocker, not a validator.
"""

import re
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from gates.check_schema import (  # noqa: E402
    SCHEMA_REASONS, SELF_PASSING_REASONS, WORK_STATE_REASONS, classify, evaluate,
    is_structured, required_fields, valid_types,
)


def goal(checks, gid="g-999-01"):
    return {"id": gid, "verification": {"checks": checks}}


# -- invalid: the population that must be REFUSED -----------------------------

@pytest.mark.parametrize("check,why", [
    ({"type": "file_exists_since", "path": "x"}, "unknown predicate type"),
    ({"type": "file_exists_after", "path": "x"}, "missing after_ref"),
    ({"type": "file_exists_after", "after_ref": "iso:2020-01-01T00:00:00"}, "missing path"),
    ({"type": "command_succeeds"}, "missing command"),
    ({"type": "command_succeeds", "command": "rm -rf /"}, "command not in allowlist"),
    ({"type": "pr_merged", "repo": "a/b"}, "missing pr"),
    ({"type": "pr_merged", "pr": 1}, "missing repo"),
    ({"type": "metric_threshold", "path": "x"}, "must specify min or max"),
    ({"type": "goal_completed_after", "goal_id": "g-1-1"}, "missing after_ref"),
    ({"type": "after_time", "timestamp": "2020-01-01T00:00:00"}, "wrong field name"),
    ({"type": "after_time", "anchor": "2020-01-01T00:00:00"}, "missing delay_seconds"),
    ({"type": "after_time", "anchor": "not-a-date", "delay_seconds": 0}, "invalid anchor"),
    ({"type": "vcs_commits_since", "repo": "."}, "must specify since_goal or after_ref"),
    ({"type": "file_check", "path": "x", "condition": "maybe"}, "unsupported condition"),
])
def test_malformed_checks_are_refused(check, why):
    assert classify(check)["verdict"] == "invalid", f"should be refused: {why}"


# -- scope: the gate must not be stricter than the evaluator it protects -------

@pytest.mark.parametrize("check", [
    "Run the suite and confirm it is green.",           # the 96% case
    "EXECUTABLE COMPLETENESS CHECK - run this, do not eyeball it: bash x.sh",
    {"target": "core/scripts/tests", "condition": "regression test added"},  # typeless
    {"type": "", "path": "x"},                          # falsy type
])
def test_non_structured_checks_are_out_of_scope(check):
    """The regression that would have stopped goal filing fleet-wide.

    verification.checks carries TWO sanctioned formats. Natural-language strings
    route to LLM Q1/Q2/Q3 verification; only dicts with a truthy `type` reach the
    predicate evaluator. Measured on the live world queue: 729 of 758 checks on
    open goals (96%) are strings. The first draft of this gate judged every
    element and called each string "not a dict: str" -- which would have refused
    the shape of 364 open goals at filing time.
    """
    assert classify(check)["verdict"] == "skipped"


def test_string_checks_never_block_a_filing():
    r = evaluate(goal(["Run the suite and confirm it is green.",
                       "Confirm the doc reads correctly."]))
    assert r["would_block"] is False
    assert r["message"] is None and r["warning"] is None
    assert len(r["skipped"]) == 2 and not r["invalid"]


def test_scope_matches_the_real_consumer_exactly():
    """Pin is_structured against verify-check-eval's own filter, not a copy of it.

    THE GATE'S POPULATION MUST NOT BE WIDER THAN ITS CONSUMER'S -- a gate that
    judges checks the evaluator never evaluates enforces a rule nothing else
    holds, and refuses work the system legitimately accepts. Rather than trust
    that the two filters agree, this loads the consumer's `_extract_checks` and
    asserts they select the identical set over a mixed corpus.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_vce", SCRIPTS / "verify-check-eval.py")
    vce = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vce)

    corpus = [
        "a natural-language check",
        {"type": "file_exists_after", "path": "x", "after_ref": "iso:2030-01-01T00:00:00"},
        {"type": "file_exists_since", "path": "x"},
        {"target": "x", "condition": "y"},
        {"type": ""},
        {"type": "command_succeeds", "command": "git status"},
        7,
        None,
    ]
    consumer_structured, _ = vce._extract_checks({"verification": {"checks": corpus}})
    mine = [c for c in corpus if is_structured(c)]
    assert mine == consumer_structured, (
        "gates.check_schema.is_structured has drifted from "
        "verify-check-eval._extract_checks. The gate would judge a population the "
        "evaluator never sees (over-blocking) or miss one it does (under-blocking)."
    )


def test_four_of_these_were_false_negatives_in_the_first_draft():
    """SCHEMA_REASONS was first copied from the goal's prose and let these through.

    Kept as a named case rather than folded into the table above: these are not
    hypothetical shapes, they are the ones that actually escaped, and a false
    NEGATIVE here reports a broken check as healthy -- the very defect the gate
    exists to catch, reproduced inside the gate.
    """
    for check in [{"type": "after_time", "timestamp": "2020-01-01T00:00:00"},
                  {"type": "command_succeeds"},
                  {"type": "pr_merged", "repo": "a/b"},
                  {"type": "vcs_commits_since", "repo": "."}]:
        assert classify(check)["verdict"] == "invalid"


# -- ok: the load-bearing negative --------------------------------------------

def test_correct_check_on_undone_work_is_allowed():
    """The whole gate rests on this NOT being refused."""
    c = classify({"type": "file_exists_after",
                  "path": "core/scripts/gates/check_schema.py",
                  "after_ref": "iso:2030-01-01T00:00:00"})
    assert c["verdict"] == "ok"
    assert "found 0 fresh" in c["reason"]


def test_goal_completed_after_on_an_unfinished_goal_is_allowed():
    c = classify({"type": "goal_completed_after", "goal_id": "g-999-99999",
                  "after_ref": "iso:2020-01-01T00:00:00"})
    assert c["verdict"] == "ok"


def test_a_well_formed_goal_does_not_block():
    r = evaluate(goal([{"type": "file_exists_after", "path": "core/scripts/predicate.py",
                        "after_ref": "iso:2030-01-01T00:00:00"}]))
    assert r["would_block"] is False
    assert r["message"] is None and r["warning"] is None


# -- vacuous: warn, never block ------------------------------------------------

def test_already_satisfied_check_is_vacuous():
    c = classify({"type": "after_time", "anchor": "2020-01-01T00:00:00", "delay_seconds": 0})
    assert c["verdict"] == "vacuous"


def test_vacuous_warns_but_does_not_block():
    """Deliberate asymmetry: a vacuous check is optimistic, not broken. Blocking on
    it would refuse checks that merely need a later anchor."""
    r = evaluate(goal([{"type": "after_time", "anchor": "2020-01-01T00:00:00",
                        "delay_seconds": 0}]))
    assert r["would_block"] is False
    assert r["warning"] is not None and "PASS at filing time" in r["warning"]
    assert r["message"] is None


# -- the refusal message must be actionable ------------------------------------

def test_message_names_the_offending_type_and_its_required_fields():
    r = evaluate(goal([{"type": "file_exists_after", "path": "x"}]))
    assert r["would_block"] is True
    assert "file_exists_after" in r["message"]
    assert "path" in r["message"] and "after_ref" in r["message"]


def test_unknown_type_message_lists_the_valid_types():
    r = evaluate(goal([{"type": "file_exists_since", "path": "x"}]))
    assert r["would_block"] is True
    for t in ("command_succeeds", "file_exists_after"):
        assert t in r["message"], "an unknown type must be answered with the real ones"


def test_required_fields_are_asked_of_the_evaluator_not_hardcoded():
    assert required_fields("file_exists_after") == ["path", "after_ref"]
    assert required_fields("goal_completed_after") == ["goal_id", "after_ref"]
    assert required_fields("nonexistent_type") == []


def test_valid_types_come_from_the_dispatch_table():
    """A hand-copied type list is the defect one layer up: 's originating
    incident was 10 checks whose every type name came from a stale 'valid' list."""
    import predicate
    assert valid_types() == sorted(predicate.PREDICATE_TYPES.keys())


# -- mixed + empty populations -------------------------------------------------

def test_one_bad_check_among_good_ones_still_blocks():
    r = evaluate(goal([
        {"type": "file_exists_after", "path": "core/scripts/predicate.py",
         "after_ref": "iso:2030-01-01T00:00:00"},
        {"type": "file_exists_since", "path": "x"},
    ]))
    assert r["would_block"] is True
    assert len(r["ok"]) == 1 and len(r["invalid"]) == 1
    assert "check[1]" in r["message"], "the message must say WHICH check"


def test_goal_with_no_checks_is_not_blocked():
    for g in ({"id": "g-1-1"}, {"id": "g-1-1", "verification": {}},
              {"id": "g-1-1", "verification": {"checks": []}}):
        assert evaluate(g)["would_block"] is False


def test_evaluate_never_raises_on_garbage():
    """A validator that can crash the filing path is worse than the check it catches."""
    for g in (None, {}, {"verification": None}, {"verification": {"checks": "nope"}},
              {"verification": {"checks": [None, 7, "x"]}}):
        r = evaluate(g)
        assert r["would_block"] is False, "junk is out of scope, not a refusal"


# -- the drift guard: my curated lists vs predicate.py's real literals ---------

def _predicate_reason_literals():
    """Every `reason=` literal predicate.py can emit, with f-string holes removed."""
    src = (SCRIPTS / "predicate.py").read_text(encoding="utf-8")
    out = set()
    for m in re.finditer(r'reason=f?"([^"]+)"', src):
        lit = re.sub(r"\{[^}]*\}", "", m.group(1)).strip()
        if lit:
            out.add(lit)
    return out


def test_check_schema_reason_coverage():
    """EVERY reason predicate.py emits must be judged by one list or the other.

    This is the test that matters most in this file. SCHEMA_REASONS is
    hand-curated -- it has to be, since the evaluator does not tag its reasons as
    schema-vs-work-state -- and a hand-curated list silently rots when the thing it
    mirrors grows. Without this, a NEW reason string added to predicate.py would
    default to `ok` and quietly widen the gate's blind spot, which is exactly how
    the first draft of SCHEMA_REASONS let seven malformed shapes through.

    An unjudged reason and a deliberately-allowed one are indistinguishable from
    outside; this makes them distinguishable.
    """
    unjudged = [lit for lit in _predicate_reason_literals()
                if not any(s in lit for s in SCHEMA_REASONS)
                and not any(w in lit for w in WORK_STATE_REASONS)
                and not any(p in lit for p in SELF_PASSING_REASONS)]
    assert not unjudged, (
        "predicate.py emits reason(s) that gates/check_schema.py has not judged: "
        f"{sorted(unjudged)}. Add each to SCHEMA_REASONS (malformed -- can never "
        "gate anything), WORK_STATE_REASONS (correctly formed, work not done), or "
        "SELF_PASSING_REASONS (emitted alongside passed=True, so classify() "
        "intercepts it at the vacuous branch and never consults either list). "
        "Defaulting is not an option: an unjudged reason silently classifies as ok. "
        "Pick by what the evaluator DOES, not by which bucket is inert today -- an "
        "entry parked in the wrong list is correct only until the branch order moves."
    )


def test_the_two_lists_do_not_overlap_ambiguously():
    """A literal matching BOTH lists would classify by list order, not by judgment.

    This test has already earned its place: the first draft used bare "goal" and
    "json_length", which occur INSIDE two schema reasons, so three malformed shapes
    were classified correctly only because classify() tests schema first.
    """
    both = [lit for lit in _predicate_reason_literals()
            if any(s in lit for s in SCHEMA_REASONS)
            and any(w in lit for w in WORK_STATE_REASONS)]
    assert not both, f"reason(s) matched by both lists: {sorted(both)}"

    # SELF_PASSING_REASONS must not collide with SCHEMA_REASONS either, and this
    # half is the one with teeth: classify() tests schema FIRST, so an overlapping
    # entry would be REFUSED rather than warned -- turning a deliberately-declared
    # not-machine-checkable check into a hard filing failure. The overlap would be
    # invisible from the list itself; only this assertion surfaces it.
    schema_clash = [lit for lit in _predicate_reason_literals()
                    if any(p in lit for p in SELF_PASSING_REASONS)
                    and any(s in lit for s in SCHEMA_REASONS)]
    assert not schema_clash, (
        f"self-passing reason(s) also matched by SCHEMA_REASONS: {sorted(schema_clash)} "
        "-- these would be REFUSED at filing, not warned")


@pytest.mark.parametrize("check", [
    {"type": "file_exists_since", "path": "x"},
    {"type": "file_exists_after", "path": "x"},
    {"type": "command_succeeds"},
    {"type": "pr_merged", "repo": "a/b"},
    {"type": "metric_threshold", "path": "x"},
    {"type": "after_time", "anchor": "not-a-date", "delay_seconds": 0},
    {"type": "vcs_commits_since", "repo": "."},
])
def test_a_malformed_check_never_reports_passed(check):
    """THE INVARIANT THAT MAKES classify()'s BRANCH ORDER SAFE. Pinned, not assumed.

    Mutation-testing this file found that swapping the `passed` and schema branches
    in classify() changes nothing -- an EQUIVALENT mutant, because no malformed
    check ever comes back passed=True, so the vacuous branch cannot intercept one.

    The easy readings are both wrong. It is not a weak test suite, and it is not a
    free pass either: the equivalence rests entirely on a property of predicate.py
    that nothing was checking. Let a future predicate return passed=True alongside a
    schema reason -- a type whose evaluator short-circuits before validating, say --
    and the branch order silently becomes load-bearing, with `vacuous` (a warning)
    swallowing a check that should have been REFUSED. That is the false-negative
    direction, and it would arrive with every existing test still green.

    So the mutant is left alive deliberately and the assumption underneath it is
    pinned here instead. This is the test that goes red in that future.
    """
    import predicate
    r = predicate.evaluate(check)
    reason = r.reason or ""
    assert any(s in reason for s in SCHEMA_REASONS), "fixture is not schema-invalid"
    assert r.passed is False, (
        f"predicate.evaluate({check!r}) returned passed=True with schema reason "
        f"{reason!r}. classify()'s branch order is now load-bearing: move the "
        f"SCHEMA_REASONS test ABOVE the `result.passed` test, or this check is "
        f"reported as a vacuous WARNING instead of an invalid REFUSAL."
    )
