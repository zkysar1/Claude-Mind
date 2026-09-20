#!/usr/bin/env python3
""": the cross-call goal-count census, and the proof it beats the
in-call control the goal was filed with.

WHY THIS EXISTS. A single-field `update-goal` write dropped 29 goal records at
once (ZDS g-022-78, relayed 2026-09-17). The filing proposed asserting the
record count is conserved ACROSS ONE CALL. That control cannot catch this
defect, and `test_the_filed_in_call_control_would_have_passed` is the proof —
it reconstructs the incident shape and shows the in-call assertion holding
while goals vanish, then shows the cross-call census catching the same read.
guard-2260 is the governing rule: a remedy is a separate claim from its
diagnosis and needs its own measurement, including a case expected NOT to
match.

WHAT THIS SEAM EXCLUDES (guard-1462). Every test drives the pure module. The
daemon wiring — the read inside `file_locks.locked`, the persisted-count write
after `_atomic_write_jsonl`, and the fail-open wrapper around the call — is
upstream and unfalsifiable here. `count_goals` is fed hand-built store images
shaped like real `aspirations.jsonl` records, not a real store.

Anti-vacuity guard: `test_the_two_verdicts_do_not_collapse`. Mutate against
THAT ALONE (guard-1793).
"""
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "gates", "goal_count_census.py")
_spec = importlib.util.spec_from_file_location("goal_count_census_mod", _SRC)
gcc = importlib.util.module_from_spec(_spec)
sys.modules["goal_count_census_mod"] = gcc
_spec.loader.exec_module(gcc)


def _store(*goal_counts):
    """A store image: one aspiration record per arg, carrying that many goals."""
    return [
        {"id": f"asp-{i:03d}", "goals": [{"id": f"g-{i:03d}-{j:02d}"}
                                         for j in range(n)]}
        for i, n in enumerate(goal_counts)
    ]


# ── count_goals: the quantity `lines_changed` does not measure ───────────────

def test_counts_goals_not_records():
    """The whole point: 27 records can hold ~2300 goals, and the incident moved
    the record count by zero."""
    items = _store(10, 20, 30)
    assert len(items) == 3
    assert gcc.count_goals(items) == 60


def test_losing_goals_from_inside_one_record_leaves_the_record_count_flat():
    """The measured blind spot, reconstructed: same number of records, 29 fewer
    goals. `lines_changed=len(items)` is identical across the pair."""
    before = _store(40, 40, 40)
    after = _store(40, 11, 40)
    assert len(before) == len(after)                 # lines_changed: unchanged
    assert gcc.count_goals(before) - gcc.count_goals(after) == 29


def test_empty_and_malformed_images_are_countable_not_fatal():
    """A census must be computable over whatever the read returned."""
    assert gcc.count_goals([]) == 0
    assert gcc.count_goals(None) == 0
    assert gcc.count_goals("not-a-list") == 0
    assert gcc.count_goals([{"id": "asp-000"}]) == 0            # no goals key
    assert gcc.count_goals([{"id": "asp-000", "goals": None}]) == 0
    assert gcc.count_goals([{"id": "asp-000", "goals": {}}]) == 0
    assert gcc.count_goals(["a string record", 7, None]) == 0


# ── evaluate: one assertion per decision_path ────────────────────────────────

def test_no_expectation_is_not_anomalous():
    """Bootstrap. Treating an absent expectation as a finding would make every
    fresh store report on its first touch."""
    r = gcc.evaluate(2300, None)
    assert r["anomalous"] is False
    assert r["decision_path"] == "no-expectation"
    assert r["observed"] == 2300


def test_equal_counts_are_clean():
    r = gcc.evaluate(2300, 2300)
    assert r["anomalous"] is False
    assert r["decision_path"] == "no-decrease"
    assert r["delta"] == 0


def test_an_increase_is_clean_because_another_endpoint_may_have_added():
    r = gcc.evaluate(2305, 2300)
    assert r["anomalous"] is False
    assert r["decision_path"] == "no-decrease"
    assert r["delta"] == 5


def test_the_incident_magnitude_is_flagged():
    r = gcc.evaluate(2271, 2300)
    assert r["anomalous"] is True
    assert r["decision_path"] == "decrease-anomalous"
    assert r["delta"] == -29
    assert "29 goal(s) missing" in r["message"]


def test_a_single_missing_goal_is_flagged_at_tolerance_zero():
    """TOLERANCE is 0 on purpose — stage 1 reports every decrease so the
    threshold can be chosen from telemetry instead of invented."""
    assert gcc.TOLERANCE == 0
    r = gcc.evaluate(2299, 2300)
    assert r["anomalous"] is True
    assert r["delta"] == -1


def test_non_integer_operands_are_inert():
    for bad in ("2300", 2300.0, None, [], {}):
        assert gcc.evaluate(bad, 2300)["decision_path"] == "non-integer-observed"
    for bad in ("2300", 2300.0, [], {}):
        assert gcc.evaluate(2300, bad)["decision_path"] == "non-integer-expected"


def test_booleans_are_not_counts():
    """bool subclasses int; a stray True must not compare as 1."""
    assert gcc.evaluate(True, 2300)["decision_path"] == "non-integer-observed"
    assert gcc.evaluate(2300, False)["decision_path"] == "non-integer-expected"


def test_every_branch_sets_a_unique_decision_path():
    """guard-502: telemetry must be able to tell the branches apart."""
    paths = {
        gcc.evaluate("x", 1)["decision_path"],
        gcc.evaluate(1, None)["decision_path"],
        gcc.evaluate(1, "x")["decision_path"],
        gcc.evaluate(2, 1)["decision_path"],
        gcc.evaluate(1, 2)["decision_path"],
    }
    assert paths == {"non-integer-observed", "no-expectation",
                     "non-integer-expected", "no-decrease",
                     "decrease-anomalous"}


def test_the_verdict_key_is_anomalous_not_blocked():
    """Stage 1 does not refuse. The key name is the guard against a caller
    wiring a refusal by autocomplete."""
    r = gcc.evaluate(2271, 2300)
    assert "anomalous" in r
    assert "blocked" not in r
    assert "REPORT ONLY" in r["message"]


# ── guard-2260: does the signal actually produce the symptom? ────────────────

def test_the_filed_in_call_control_would_have_passed():
    """THE LOAD-BEARING TEST. Reconstruct the incident: a short-but-well-formed
    read drops 29 goals, and the write rewrites the store from it.

    The control the goal was FILED with — conserve the count across one call —
    is computed from the same short read the write uses, so it holds perfectly
    while the goals vanish. The cross-call census, given the count persisted by
    the previous write, catches the same read. Without this pair the remedy is
    an unmeasured claim."""
    true_store = _store(40, 40, 40)                  # 120 goals on disk
    persisted_expectation = gcc.count_goals(true_store)
    assert persisted_expectation == 120

    short_read = _store(40, 11, 40)                  # 91 goals came back
    written_back = short_read                        # update_goal rewrites from `items`

    # (a) the FILED control: before vs after, both from the short read.
    before = gcc.count_goals(short_read)
    after = gcc.count_goals(written_back)
    assert before == after                           # passes — and 29 are gone
    assert gcc.evaluate(after, before)["anomalous"] is False

    # (b) the CROSS-CALL census, same read, expectation from the prior write.
    verdict = gcc.evaluate(before, persisted_expectation)
    assert verdict["anomalous"] is True
    assert verdict["delta"] == -29


def test_positive_control_a_healthy_read_is_not_flagged():
    """The case expected NOT to match (guard-2260). An ordinary update reads the
    whole store, mutates one goal in place, and conserves the count — the census
    must stay silent, or it reports on every write and nobody can adopt it."""
    true_store = _store(40, 40, 40)
    persisted_expectation = gcc.count_goals(true_store)
    full_read = _store(40, 40, 40)
    full_read[1]["goals"][3]["status"] = "completed"     # the actual mutation
    verdict = gcc.evaluate(gcc.count_goals(full_read), persisted_expectation)
    assert verdict["anomalous"] is False
    assert verdict["decision_path"] == "no-decrease"


def test_the_two_verdicts_do_not_collapse():
    """Anti-vacuity: a module stubbed to always return one verdict fails here."""
    clean = gcc.evaluate(120, 120)
    dirty = gcc.evaluate(91, 120)
    assert clean["anomalous"] != dirty["anomalous"]
    assert clean["decision_path"] != dirty["decision_path"]
    assert clean["message"] is None and dirty["message"] is not None
    assert clean["delta"] == 0 and dirty["delta"] == -29
