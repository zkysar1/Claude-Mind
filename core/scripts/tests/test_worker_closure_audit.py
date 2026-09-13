"""Reducer-side sampling audit of worker closures ( item 2).

The convergence split moves verification onto the worker Body that did the work,
which removes the only outside reader. This module is the compensating control, so
these cases pin the two things that decide whether it is worth having:

  * the checks FIRE on the defect shapes their guardrails describe, and
  * they stay SILENT on the healthy shapes that resemble them.

The second half is where the value is, and every case below marked REGRESSION was
a real false positive measured against 110 live worker closures on 2026-09-03 —
not a hypothetical. Two of them would have made the tool useless in opposite ways:
recurring goals rest at `status: pending` by design and produced 12 of 12 false
DISAGREE verdicts on the largest aspiration, and closure notes in this corpus cite
sibling goal ids constantly ("g-115-4846 ... still pending"), so generic queue
vocabulary described OTHER goals far more often than the closer's own leftovers.

A checker that cries wolf on ordinary prose gets ignored wholesale, so the
remainder check is deliberately UNDER-matching; `test_narrow_by_design` states that
as an intended property rather than leaving it to look like a gap.

STORAGE_BACKEND=local on every subprocess (guard-955): these cases run on boxes
whose default backend derives its object key from the environment id rather than
the tmp path, where a tmp write would collide on a production key.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "worker-closure-audit.py"


def _mod():
    spec = importlib.util.spec_from_file_location("worker_closure_audit", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


wca = _mod()


def goal(**kw):
    """A worker closure with enough note text to clear the empty_note floor."""
    base = {
        "id": "g-1-1",
        "title": "t",
        "priority": "MEDIUM",
        "status": "completed",
        "completed_by_role": "worker",
        "outcome_note": "Applied the change and ran the targeted suite: 12/12 passed, exit 0.",
    }
    base.update(kw)
    return base


def checks(g):
    return {c["check"] for c in wca.run_checks(g)}


# ─── the checks fire on their own defect shape ─────────────────────────────

def test_empty_note_fires_high():
    fired = wca.run_checks(goal(outcome_note="done"))
    assert "empty_note" in {c["check"] for c in fired}
    assert any(c["confidence"] == "high" for c in fired if c["check"] == "empty_note")
    assert wca.verdict_for(fired) == "DISAGREE"


def test_note_done_status_disagrees_fires_high():
    g = goal(status="pending",
             outcome_note="DONE - all 4 outcomes met, the install ran on the peer box "
                          "and the settled policy was re-probed with evidence recorded.")
    fired = wca.run_checks(g)
    assert "note_done_status_disagrees" in {c["check"] for c in fired}
    assert wca.verdict_for(fired) == "DISAGREE"


def test_remainder_language_fires_medium():
    g = goal(outcome_note="Drained 19 of 184. 165 entries remain and the slot is "
                          "deliberately NOT cleared for the next pass.")
    fired = wca.run_checks(g)
    assert "remainder_language" in {c["check"] for c in fired}
    assert wca.verdict_for(fired) == "REVIEW", "medium alone must never read DISAGREE"


def test_criterion_unrun_is_low_and_cannot_force_disagree():
    g = goal(outcome_note="I reworded the section so it reads more clearly for the "
                          "next maintainer of this area of the document.",
             verification={"outcomes": ["a", "b"]})
    fired = wca.run_checks(g)
    assert "criterion_unrun" in {c["check"] for c in fired}
    assert wca.verdict_for(fired) == "REVIEW"


# ─── and stay silent on the healthy shapes that resemble them ──────────────

def test_recurring_at_rest_is_not_a_disagreement():
    """REGRESSION. A recurring goal runs, records DONE, and returns to `pending`
    for its next interval. That is the designed resting state, not the
    stranded-claim class. Measured 2026-09-03: all 12 DISAGREE verdicts on
    asp-115 were recurring goals (achievedCount up to 426) — the entire
    high-confidence bucket on the largest aspiration was noise."""
    g = goal(status="pending", recurring=True, achievedCount=124,
             outcome_note="DONE - swept the inbox, 0 new alerts, 3/3 lanes checked.")
    assert "note_done_status_disagrees" not in checks(g)
    assert wca.verdict_for(wca.run_checks(g)) == "AGREE"


def test_recurring_does_not_exempt_the_other_checks():
    """The recurring carve-out is scoped to ONE check. An empty note is a defect
    whatever the goal's cadence — a blanket exemption would silently drop every
    recurring closure out of the audit."""
    g = goal(status="pending", recurring=True, outcome_note="ok")
    assert "empty_note" in checks(g)


@pytest.mark.parametrize("note", [
    "The dedup search surfaced g-115-4846 (bravo, cc-05, 2026-08-03, still pending): "
    "the closer only matches a quote at line start.",
    "6 of the 9 ids the census names return empty: g-115-4240, g-115-5207 LIVE and "
    "still pending - 4 siblings, none a plain duplicate.",
    "Net effect: note present, status still pending, zero trace - a goal wearing a "
    "false supersession note that no audit can find.",
])
def test_generic_pending_prose_about_other_goals_does_not_fire(note):
    """REGRESSION. These are verbatim live closure notes. Each `still pending`
    describes a DIFFERENT goal; none is the closer's own leftover."""
    assert "remainder_language" not in checks(goal(outcome_note=note))


def test_remainder_naming_its_successor_is_suppressed():
    """guard-4007's prescribed remedy is to file the successor and NAME ITS ID in
    the note. Flagging a note that does so punishes the exact behaviour the
    guardrail asks for — measured on g-115-8667, which wrote 'Filed as g-115-8683
    (Case A - the unfinished remainder of sanctioned scope)'. The defect is a
    remainder with NO tracker, so the id is what makes it compliance."""
    tracked = goal(outcome_note="Drained the first batch; 165 entries remain. Filed as "
                                "g-115-8683 to carry the rest, deduped first.")
    assert "remainder_language" not in checks(tracked)

    untracked = goal(outcome_note="Drained the first batch; 165 entries remain and the "
                                  "slot is deliberately NOT cleared.")
    assert "remainder_language" in checks(untracked)


def test_narrow_by_design():
    """Under-matching is the INTENDED direction: a missed remainder costs one
    unaudited goal; a checker that fires on ordinary cross-references gets ignored
    wholesale. Bare 'remainder' and 'still pending' are deliberately not patterns."""
    assert "remainder_language" not in checks(goal(outcome_note=
        "This is the remainder of the sanctioned scope, handled separately elsewhere."))


def test_clean_close_agrees():
    g = goal(verification={"outcomes": ["a"]},
             outcome_note="Applied the fix and ran the targeted suite: 46/46 passed, "
                          "exit 0. Verified the inserted literal with a grep returning 3 hits.")
    assert wca.run_checks(g) == []
    assert wca.verdict_for([]) == "AGREE"


# ─── population and sampling ───────────────────────────────────────────────

def test_only_worker_closures_are_audited():
    assert wca.is_worker_closure(goal()) is True
    assert wca.is_worker_closure(goal(completed_by_role="reducer")) is False
    assert wca.is_worker_closure(goal(completed_by_role=None)) is False


def test_every_high_is_sampled():
    for n in range(40):
        g = goal(id=f"g-9-{n}", priority="HIGH")
        take, why = wca.sampled(g, 0.0)
        assert take is True and why == "every-high", "a HIGH must be sampled at any fraction"


def test_sampling_is_deterministic():
    """A randomly-resampling auditor produces a different denominator every pass,
    so its trend line means nothing. Same id must always land the same way."""
    g = goal(id="g-9-77", priority="MEDIUM")
    first = wca.sampled(g, 0.25)
    for _ in range(20):
        assert wca.sampled(g, 0.25) == first


def test_fraction_bounds_are_honoured():
    ids = [goal(id=f"g-9-{n}", priority="MEDIUM") for n in range(400)]
    assert sum(wca.sampled(g, 0.0)[0] for g in ids) == 0
    assert sum(wca.sampled(g, 1.0)[0] for g in ids) == len(ids)
    quarter = sum(wca.sampled(g, 0.25)[0] for g in ids)
    assert 0.15 * len(ids) < quarter < 0.35 * len(ids)


def test_audit_counts_and_excludes_non_workers():
    goals = [
        goal(id="g-2-1", priority="HIGH"),
        goal(id="g-2-2", priority="HIGH", outcome_note="x"),          # empty_note
        goal(id="g-2-3", priority="HIGH", completed_by_role="reducer"),
    ]
    res = wca.audit(goals, 0.25, "asp-2", "tester")
    assert res["worker_closures"] == 2
    assert res["counts"]["DISAGREE"] == 1
    assert res["counts"]["AGREE"] == 1


# ─── CLI ───────────────────────────────────────────────────────────────────

def _run(*args, **kw):
    env = dict(os.environ, STORAGE_BACKEND="local")
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, timeout=180, env=env, **kw)


def test_cli_dry_run_writes_nothing(tmp_path):
    gj = tmp_path / "goals.json"
    gj.write_text(json.dumps([goal(id="g-3-1", priority="HIGH", outcome_note="x")]),
                  encoding="utf-8")
    res = _run("--goals-json", str(gj), "--asp", "asp-3", "--dry-run", "--json")
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["emit"] == "dry-run: not written"
    assert out["counts"]["DISAGREE"] == 1


def test_cli_is_report_only_never_refuses(tmp_path):
    """rc must stay 0 even when every sampled closure disagrees: an audit that can
    wedge the completion review it audits would be traded away the first time it
    misfired."""
    gj = tmp_path / "goals.json"
    gj.write_text(json.dumps([goal(id=f"g-4-{n}", priority="HIGH", outcome_note="x")
                              for n in range(5)]), encoding="utf-8")
    res = _run("--goals-json", str(gj), "--asp", "asp-4", "--dry-run", "--json")
    assert res.returncode == 0
    assert json.loads(res.stdout)["counts"]["DISAGREE"] == 5


def test_cli_requires_a_source():
    assert _run("--dry-run").returncode == 2


# ──  part4: worker-vs-auditor agreement ───────────────────────────
#
# Until verify_verdict existed this module had NOTHING to compare against, so
# its AGREE/DISAGREE measured whether the RECORD was self-consistent — not
# whether the auditor agreed with the WORKER. These cases pin the difference,
# and the one that matters most is the LAST: `not_comparable` must never be
# counted or read as agreement (guard-963), because every closure written
# before the field landed carries no verdict at all.


# ── : the fixtures below are REAL, and that is the repair ────────
#
# Both objects are VERBATIM `verify_verdict` values lifted from live
# world/aspirations records on 2026-09-11 (alpha, cc-08) — not shapes invented
# in this file. The module used to ask for a `verdict` key that no writer has
# ever emitted — `jsonl-field-probe.py --field goals.verify_verdict.verdict`
# reads every record of the store and answers field_present:false — so the
# baseline run read {agree: 0, disagree: 0, not_comparable: 52} over 52 sampled
# closures: the only independent check on worker self-grading, returning
# "cannot compare" for every closure. The suite stayed green because every
# fixture here was hand-written to the shape the READER wanted. A test corpus
# that never holds a producer's actual output cannot tell a working consumer
# from a blind one (guard-1943 class: pinning a writer says nothing about the
# wiring).

REAL_Q4_TRUE = {            #  — the full 8-key shape, bare q4 boolean
    "q1_passed": True,
    "q1_artifact": "world/audit-reports/agent-inbox-dispositions-2026-09-11-run251.txt",
    "q1_5_passed": True,
    "q1_5_checklist": "10/10",
    "q2_passed": True,
    "q2_failure_mode_checked": "a machine-alert classification could have destroyed "
                               "fleet-addressed mail whose only copy was the manifest",
    "q3_scope": "integration",
    "q4_passed": True,
}

REAL_Q4_FALSE = {           #  — same shape, Q4 recorded a FAILURE
    "q1_passed": True,
    "q1_artifact": "cnc.log(97916B,SWEEP_EXIT=0)+g-115-2571.outcome_note(7548)",
    "q1_5_passed": True,
    "q1_5_checklist": "abbreviated-routine-3/3",
    "q2_passed": True,
    "q2_failure_mode_checked": "sweep-ran-but-measured-nothing: refuted by in-chain rc=0",
    "q3_scope": "integration",
    "q4_passed": False,
}


def test_the_pre_fix_reader_returns_nothing_on_both_real_records():
    """Positive control (guard-1475). `self_verdict_of` WAS `v.get("verdict")`.

    Run that exact predicate against the same fixtures every test below asserts
    on: it yields None for both. So these fixtures discriminate the repaired
    reader from the blind one — without this control, a green suite would prove
    only that the tests and the code agree with each other.
    """
    for real in (REAL_Q4_TRUE, REAL_Q4_FALSE):
        assert real.get("verdict") is None, "the pre-fix predicate found nothing"
        assert wca.self_verdict_of(goal(verify_verdict=real)) == "completed"


def test_self_verdict_is_derived_from_the_keys_the_writer_actually_writes():
    assert wca.self_verdict_of(goal(verify_verdict=REAL_Q4_TRUE)) == "completed"
    assert wca.self_verdict_of(goal(verify_verdict=REAL_Q4_FALSE)) == "completed", (
        "q4_passed is NOT a gating key — the verdict comes from q1/q1_5/q2")
    partial = {k: v for k, v in REAL_Q4_TRUE.items() if k != "q1_5_passed"}
    assert wca.self_verdict_of(goal(verify_verdict=partial)) == "completed", (
        "live records do omit Q-keys; read the keys present, do not discard")
    failed = {**REAL_Q4_TRUE, "q2_passed": False}
    assert wca.self_verdict_of(goal(verify_verdict=failed)) == "incomplete"


def test_an_explicit_verdict_string_still_wins():
    """Kept so a future writer that emits one needs no change to this module."""
    assert wca.self_verdict_of(goal(verify_verdict={"verdict": "completed"})) == "completed"
    assert wca.self_verdict_of(goal(verify_verdict={"verdict": "  SKIPPED "})) == "skipped"
    assert wca.self_verdict_of(
        goal(verify_verdict={**REAL_Q4_TRUE, "verdict": "incomplete"})) == "incomplete"


@pytest.mark.parametrize("bad", [
    None,                          # field absent entirely (every legacy closure)
    "completed",                   # a string where a dict belongs
    {},                            # dict with no keys at all
    {"verdict": None},             # explicit null
    {"verdict": "   "},            # whitespace only
    {"q3_scope": "unit"},          # a LABEL key only — nothing was gated
    {"q4_passed": True},           # a q4-only record: carries no gating key
])
def test_absent_or_malformed_verdict_is_none_never_a_pass(bad):
    """Fail-open: a shape this module cannot read is UNKNOWN, not agreement."""
    g = goal() if bad is None else goal(verify_verdict=bad)
    assert wca.self_verdict_of(g) is None
    assert wca.agreement_for(g, []) == "not_comparable"


@pytest.mark.parametrize("vv,expected", [
    (None, "absent"),                                      # no verify_verdict
    ("completed", "absent"),                               # not a dict
    ({}, "absent"),
    ({"q1_passed": True}, "absent"),                       # Q4 never ran
    (REAL_Q4_TRUE, "unknown"),                             # THE legacy population
    (REAL_Q4_FALSE, "fail"),
    ({"q4_verdict": "pass"}, "pass"),
    ({"q4_verdict": "fail"}, "fail"),
    ({"q4_verdict": " SKIPPED "}, "skipped"),
    ({"q4_verdict": "skipped", "q4_passed": True}, "skipped"),   # tri-state wins
    ({"q4_verdict": "nonsense", "q4_passed": True}, "unknown"),  # unreadable -> bool path
])
def test_q4_state_is_tri_state_over_a_key_that_could_only_hold_a_bool(vv, expected):
    g = goal() if vv is None else goal(verify_verdict=vv)
    assert wca.q4_state_of(g) == expected


def test_a_bare_q4_passed_true_is_never_read_as_a_pass():
    """The goal's explicit constraint. Q4 exits 0 for BOTH `pass` and `skipped`,
    so a Body that correctly read a SKIPPED verdict as non-blocking wrote
    `q4_passed: true`. Reading that as a pass would convert this module's silent
    blindness into CONFIDENT WRONG AGREEMENT — worse than the defect repaired
    here (guard-1753: "could not resolve" is not "resolved and found nothing").
    """
    g = goal(verify_verdict=REAL_Q4_TRUE)
    assert wca.q4_state_of(g) == "unknown"
    assert wca.q4_state_of(g) != "pass"


def test_an_explicitly_skipped_q4_is_not_counted_as_agreement():
    """The goal's second check. A closure whose provenance leg verified NOTHING
    offers no self-assessment on that axis, so it is not_comparable — and the
    SAME record carrying an explicit `pass` does compare, which is what makes
    this a distinction rather than a blanket downgrade."""
    skipped = goal(verify_verdict={**REAL_Q4_TRUE, "q4_verdict": "skipped"})
    assert wca.self_verdict_of(skipped) == "completed"
    assert wca.agreement_for(skipped, wca.run_checks(skipped)) == "not_comparable"
    passed = goal(verify_verdict={**REAL_Q4_TRUE, "q4_verdict": "pass"})
    assert wca.agreement_for(passed, wca.run_checks(passed)) == "agree"


def test_the_legacy_bare_boolean_does_NOT_block_agreement():
    """The other half of the design, and the one that keeps the repair honest.

    35 of the 44 asp-115 worker closures that carry a q4 key at all carry the
    bare boolean (measured 2026-09-11). Treating that ambiguity as a
    blocker would re-zero `agreement_counts` — reinstating the exact defect this
    goal exists to remove, while LOOKING more rigorous. The ambiguity is
    reported in `q4_states` instead, so it is countable rather than invisible."""
    g = goal(verify_verdict=REAL_Q4_TRUE)
    assert wca.q4_state_of(g) == "unknown"
    assert wca.agreement_for(g, wca.run_checks(g)) == "agree"


def test_agreement_disagrees_when_worker_self_graded_over_a_high_defect():
    g = goal(outcome_note="done", verify_verdict={"verdict": "completed"},
             verification={"outcomes": ["x"], "checks": []})
    fired = wca.run_checks(g)
    assert any(f["confidence"] == "high" for f in fired), "fixture must trip a high check"
    assert wca.agreement_for(g, fired) == "disagree"


def test_agreement_agrees_on_a_clean_close_carrying_a_verdict():
    g = goal(verify_verdict={"verdict": "completed"})
    assert wca.agreement_for(g, wca.run_checks(g)) == "agree"


def test_not_comparable_is_tallied_separately_from_agree():
    """The guard-963 property: zero compared items must not read as clean."""
    goals = [goal(id="g-1-01"), goal(id="g-1-02")]          # neither has a verdict
    res = wca.audit(goals, 1.0, "asp-t", "tester")
    a = res["agreement_counts"]
    assert a == {"agree": 0, "disagree": 0, "not_comparable": 2}
    assert res["counts"]["AGREE"] == 2, (
        "record-consistency still reads AGREE — which is exactly why the "
        "agreement tally must be reported separately, not folded into it")


def test_cli_says_no_agreement_was_measured_when_none_was(tmp_path):
    p = tmp_path / "goals.json"
    p.write_text(json.dumps([goal(id="g-1-01"), goal(id="g-1-02")]), encoding="utf-8")
    env = {**os.environ, "STORAGE_BACKEND": "local"}
    r = subprocess.run([sys.executable, str(SCRIPT), "--goals-json", str(p),
                        "--asp", "t", "--dry-run", "--fraction", "1.0"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0
    assert "not_comparable=2" in r.stdout
    assert "not evidence of agreement" in r.stdout


def _mixed_corpus():
    """One goal per q4 state, each carrying a real writer shape where one exists."""
    return [
        goal(id="g-1-01", verify_verdict=REAL_Q4_TRUE),
        goal(id="g-1-02", verify_verdict=REAL_Q4_FALSE),
        goal(id="g-1-03", verify_verdict={**REAL_Q4_TRUE, "q4_verdict": "skipped"}),
        goal(id="g-1-04"),                                   # legacy, no field at all
    ]


def test_audit_measures_agreement_on_a_corpus_that_carries_verdicts():
    """Outcome 1, stated as the goal states it: agree + disagree > 0.

    The pre-fix module returned {0, 0, N} here — an unconsumed DETECTOR, which
    learning-philosophy.md ranks strictly worse than an unconsumed attributor.
    """
    res = wca.audit(_mixed_corpus(), 1.0, "asp-t", "tester")
    a = res["agreement_counts"]
    assert a["agree"] + a["disagree"] > 0
    assert a == {"agree": 2, "disagree": 0, "not_comparable": 2}
    assert res["q4_states"] == {"pass": 0, "fail": 1, "skipped": 1,
                                "unknown": 1, "absent": 1}
    assert [r["q4_state"] for r in res["rows"]] == ["unknown", "fail", "skipped", "absent"]


def test_cli_prints_the_q4_tally_and_names_the_ambiguous_population(tmp_path):
    p = tmp_path / "goals.json"
    p.write_text(json.dumps(_mixed_corpus()), encoding="utf-8")
    env = {**os.environ, "STORAGE_BACKEND": "local"}
    r = subprocess.run([sys.executable, str(SCRIPT), "--goals-json", str(p),
                        "--asp", "t", "--dry-run", "--fraction", "1.0"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0
    assert "agree=2" in r.stdout and "not_comparable=2" in r.stdout
    assert "q4-provenance: pass=0  fail=1  skipped=1  unknown=1  absent=1" in r.stdout
    assert "cannot distinguish a PASS from a SKIPPED run" in r.stdout
    assert "not evidence of agreement" not in r.stdout, (
        "agreement WAS measured here — the guard-963 note must not fire")
