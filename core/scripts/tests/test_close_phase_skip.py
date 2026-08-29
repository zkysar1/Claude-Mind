#!/usr/bin/env python3
"""Tests for the close-phase skip check ().

DELIBERATELY pytest-style `assert` ONLY — no main()-that-returns-bools.
The sibling suite test_loop_state_bump_idempotency.py is that hybrid shape, and
measured 2026-08-29 it reports rc=0 under `pytest` while 6 of its 9 tests fail,
because pytest COLLECTS the functions and IGNORES their `return False`. A suite
whose verdict depends on which runner you used is not a verdict. Plain asserts
fail the same way under every runner.

WHAT THE FIXTURES PIN, and why each one exists:

  * THE ANTI-VACUITY PROOF (guard-5163). A discriminator earns its keep only by
    separating two cases that were previously identical. `test_the_two_closes_
    are_indistinguishable_on_every_old_signal` asserts the OLD signals really do
    collapse — same status, same completed_at, same completed_by_sid — and
    `test_...but_the_new_check_separates_them` asserts the new verdict differs.
    Without the first assertion the second proves nothing: it would pass just as
    well against two goals that any existing field already told apart.

  * THE TWO-CAUSE DISCRIMINATION. "Uncounted" means either the phase was skipped
    or the phase ran and its fail-open bump silently no-op'd. guard-1641 forbids
    reporting those as one thing. The ledger loop-state-bump-failures.jsonl
    already records the second cause and had ONE writer and ZERO readers; these
    tests pin that reading it actually changes the verdict, so a future refactor
    that drops the ledger argument fails loudly instead of silently re-fusing the
    two causes.

  * INDETERMINATE IS NOT HEALTH. An oracle that cannot answer must not render as
    a clean sweep — that substitution is the exact defect this check exists to
    catch, one level up. Pinned in both halves: decide() must not put an
    indeterminate goal in `skipped`, AND must not leave `completeness` complete.

  * THE WORKER DECLINE. A worker never runs state-update, so every worker close
    is uncounted and an unscoped sweep fires on all of them. `applicable: False`
    must be distinguishable from a clean verdict (guard-1922).

WHAT THESE FIXTURES CANNOT REACH (guard-1462 — naming the excluded layers):
the seam is `decide()` and `_verify_counted_many(wm_path, ids)`, so everything
UPSTREAM of it is structurally unfalsifiable here: the population query against
the real aspirations store, the MIND_AGENT/MIND_SID binding, the BODY_ROLE
env, and `wm.wm_path()`'s per-Body routing. Those were exercised by live runs
against the real store on cc-08 2026-08-29 (worker path -> not-applicable in
0.03s; reducer path -> 25/25 population assembled, 118 total, bound reported)
and that live leg is not reproducible in this file.

ALSO MEASURED, NOT TESTED HERE: recurring goals ARE in scope. It looks like they
should be excluded (they close through recurring-close.sh and get their signal
mutation from recurring-loop-state-mutate.py), but recurring-close.sh:389 runs
`iteration-close.sh --phase state-update`, which reaches the bump, and the
counted-list append at loop-state-bump-counters.py is unconditional on
--recurring. Excluding them would have blinded the check to every recurring
close for a reason that reads plausible and is false.
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))

import close_phase_skip as cps  # noqa: E402


def _load_bump_module():
    """Import the hyphenated component so the batch mode can be called with an
    EXPLICIT wm_path — sidestepping wm.wm_path()'s per-Body routing, which is
    precisely what makes the sibling suite unable to isolate."""
    spec = importlib.util.spec_from_file_location(
        "lsbc_under_test", _SCRIPTS / "loop-state-bump-counters.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── the population fixture: two closes, identical on every pre-existing signal ──

HEALTHY = {"id": "g-500-01", "status": "completed",
           "completed_at": "2026-08-29T04:00:00", "completed_by_sid": "SID-A"}
SKIPPED_CLOSE = {"id": "g-500-02", "status": "completed",
                 "completed_at": "2026-08-29T04:00:00", "completed_by_sid": "SID-A"}

_COUNTED = {"g-500-01"}


def _oracle(counted=_COUNTED):
    return lambda g: cps.COUNTED if g in counted else "absent"


# ── anti-vacuity: the pair must be identical BEFORE and different AFTER ────────

def test_the_two_closes_are_indistinguishable_on_every_old_signal():
    """The half that makes the next test mean something.

    If these two goals differed on any pre-existing field, a new check that told
    them apart would prove nothing — an existing signal already did.
    """
    for field in ("status", "completed_at", "completed_by_sid"):
        assert HEALTHY[field] == SKIPPED_CLOSE[field], field
    assert HEALTHY["id"] != SKIPPED_CLOSE["id"]  # the only difference is identity


def test_but_the_new_check_separates_them():
    r = cps.decide([HEALTHY, SKIPPED_CLOSE], _oracle())
    assert r["skipped"] == ["g-500-02"]
    assert "g-500-01" not in r["skipped"]
    assert r["status"] == "findings"


def test_an_all_healthy_population_is_clean_not_merely_empty():
    """Positive control on the other side: the check must be capable of
    returning clean, or 'findings' above would be the only reachable answer."""
    r = cps.decide([HEALTHY], _oracle())
    assert r["status"] == "clean"
    assert r["skipped"] == []
    assert r["population"] == 1  # it LOOKED at one goal — not a vacuous zero


# ── the two-cause discrimination ──────────────────────────────────────────────

def test_uncounted_with_a_ledger_row_is_attributed_to_the_bump_not_a_skip():
    r = cps.decide([SKIPPED_CLOSE], _oracle(), bump_failures={"g-500-02"})
    assert r["bump_noop"] == ["g-500-02"]
    assert r["skipped"] == []
    assert r["status"] == "clean"  # a known, self-healing condition is not an alarm


def test_uncounted_without_a_ledger_row_is_the_finding():
    r = cps.decide([SKIPPED_CLOSE], _oracle(), bump_failures=set())
    assert r["skipped"] == ["g-500-02"]
    assert r["bump_noop"] == []
    assert r["status"] == "findings"


def test_the_ledger_is_what_flips_it_nothing_else_changed():
    """The mutation this pair exists to kill: dropping the bump_failures argument.

    Same population, same oracle, same membership answer — only the ledger
    differs, and the verdict must differ with it. A refactor that ignores the
    ledger re-fuses two causes guard-1641 requires kept apart, and would do so
    silently.
    """
    without = cps.decide([SKIPPED_CLOSE], _oracle(), bump_failures=set())
    with_row = cps.decide([SKIPPED_CLOSE], _oracle(), bump_failures={"g-500-02"})
    assert without["status"] != with_row["status"]
    assert without["skipped"] != with_row["skipped"]


# ── indeterminate is not health ───────────────────────────────────────────────

def test_indeterminate_is_never_reported_as_a_skip():
    r = cps.decide([SKIPPED_CLOSE], lambda _g: cps.INDETERMINATE)
    assert r["skipped"] == []
    assert r["indeterminate"] == ["g-500-02"]


def test_indeterminate_makes_the_sweep_partial_not_complete():
    """status and completeness are ORTHOGONAL. A sweep that saw nothing must not
    render as a sweep that found nothing."""
    r = cps.decide([SKIPPED_CLOSE], lambda _g: cps.INDETERMINATE)
    assert r["status"] == "clean"
    assert r["completeness"] == "partial"


def test_a_clean_readable_sweep_is_complete():
    r = cps.decide([HEALTHY], _oracle())
    assert r["completeness"] == "complete"


def test_clean_and_blind_do_not_render_identically():
    """The rendering half of the same invariant — a reader must be able to tell
    'nothing wrong' from 'could not look'."""
    clean = cps.render(cps.decide([HEALTHY], _oracle()))
    blind = cps.render(cps.decide([SKIPPED_CLOSE], lambda _g: cps.INDETERMINATE))
    assert clean != blind
    assert "INCOMPLETE" in blind
    assert "INCOMPLETE" not in clean


# ── worker scoping ────────────────────────────────────────────────────────────

def test_worker_declines_rather_than_reporting_findings():
    r = cps.decide([SKIPPED_CLOSE], _oracle(counted=set()), role="worker")
    assert r["applicable"] is False
    assert r["skipped"] == []
    assert r["status"] == "clean"


def test_worker_decline_is_distinguishable_from_a_clean_reducer_sweep():
    """guard-1922: 'not my question' and 'nothing wrong' must not render the
    same, or the lane retires itself silently on every worker box."""
    worker = cps.decide([SKIPPED_CLOSE], _oracle(counted=set()), role="worker")
    reducer = cps.decide([HEALTHY], _oracle())
    assert worker["applicable"] != reducer["applicable"]
    assert cps.render(worker) != cps.render(reducer)
    assert "n/a" in cps.render(worker)


def test_an_unrecognised_role_looks_rather_than_declines():
    """Failing toward LOOKING is the safe direction for a detector: a garbled
    role must not silence the check."""
    r = cps.decide([SKIPPED_CLOSE], _oracle(), role="something-else")
    assert r["applicable"] is True
    assert r["skipped"] == ["g-500-02"]


# ── population hygiene ────────────────────────────────────────────────────────

def test_goals_without_an_id_are_not_counted_into_the_population():
    r = cps.decide([SKIPPED_CLOSE, {}, {"id": None}], _oracle())
    assert r["population"] == 1


def test_classify_is_directly_testable_per_goal():
    assert cps.classify("g-500-01", _oracle(), set()) == cps.COUNTED
    assert cps.classify("g-500-02", _oracle(), set()) == cps.SKIPPED
    assert cps.classify("g-500-02", _oracle(), {"g-500-02"}) == cps.BUMP_NOOP
    assert cps.classify("g-500-02", lambda _g: cps.INDETERMINATE, set()) == cps.INDETERMINATE


# ── the batch mode in the component ───────────────────────────────────────────

def _seed_wm(tmpdir, counted):
    p = Path(tmpdir) / "working-memory.yaml"
    p.write_text(yaml.safe_dump(
        {"slots": {"loop_state": {"counted_goals_this_session": counted}}}),
        encoding="utf-8")
    return p


def test_batch_mode_answers_every_id_from_one_read(capsys):
    mod = _load_bump_module()
    with tempfile.TemporaryDirectory() as td:
        wm = _seed_wm(td, ["g-a", "g-b"])
        rc = mod._verify_counted_many(wm, ["g-a", "g-b", "g-c"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["indeterminate"] is False
    assert out["counted"] == ["g-a", "g-b"]
    assert out["absent"] == ["g-c"]


def test_batch_mode_agrees_with_the_shipped_single_goal_predicate(capsys):
    """The mode was ADDED for speed; it must not become a second opinion.

    Both are driven off the same seeded WM through the same module, so a
    divergence here is a real behavioural split, not an environment difference.
    """
    mod = _load_bump_module()
    with tempfile.TemporaryDirectory() as td:
        wm = _seed_wm(td, ["g-a"])
        mod._verify_counted_many(wm, ["g-a", "g-c"])
        batch = json.loads(capsys.readouterr().out)
        single = {g: mod._verify_counted(wm, g) for g in ("g-a", "g-c")}
    # single: rc 0 == counted-or-indeterminate, rc 1 == confidently absent
    assert single["g-a"] == 0 and "g-a" in batch["counted"]
    assert single["g-c"] == 1 and "g-c" in batch["absent"]


def test_batch_mode_reports_an_unreadable_wm_as_indeterminate_not_absent(capsys):
    """The blind spot the batch mode exists to close, beyond speed.

    The single-goal mode collapses counted and indeterminate into rc 0, so a
    caller using it cannot see a torn read at all. If this ever returned the ids
    under `absent` instead, an unreadable WM would render as N findings; if it
    returned them under `counted`, it would render as health. Both are wrong and
    they are wrong in opposite directions, which is why the third state exists.
    """
    mod = _load_bump_module()
    with tempfile.TemporaryDirectory() as td:
        missing = Path(td) / "nope.yaml"
        rc = mod._verify_counted_many(missing, ["g-a", "g-b"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["indeterminate"] is True
    assert out["counted"] == [] and out["absent"] == []


def test_batch_mode_treats_a_missing_counted_list_as_a_real_absence(capsys):
    """Distinct from the unreadable case above: the WM parsed fine and simply
    holds nothing counted. That is an ANSWER, not a blind spot."""
    mod = _load_bump_module()
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "working-memory.yaml"
        p.write_text(yaml.safe_dump({"slots": {"loop_state": {}}}), encoding="utf-8")
        mod._verify_counted_many(p, ["g-a"])
    out = json.loads(capsys.readouterr().out)
    assert out["indeterminate"] is False
    assert out["absent"] == ["g-a"]


# ── cross-agent scoping in the CLI half ───────────────────────────────────────

def _load_cli_module():
    spec = importlib.util.spec_from_file_location(
        "cps_cli_under_test", _SCRIPTS / "close-phase-skip-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _store(tmpdir, goals):
    p = Path(tmpdir) / "aspirations.jsonl"
    p.write_text(json.dumps({"id": "asp-1", "goals": goals}) + "\n", encoding="utf-8")
    return p


def test_population_excludes_closes_made_by_another_agent():
    """The membership oracle resolves the WM as THIS agent, so a population
    closed by a DIFFERENT agent would be compared against the wrong counted list
    and every row would read uncounted.

    Measured while validating this check: pointed at another agent's SID it
    reported 118 of 118 skipped against a true value of 11 — a confident, wholly
    wrong answer, which is precisely the defect class this lane exists to detect.
    """
    cli = _load_cli_module()
    goals = [
        {"id": "g-1", "completed_by_sid": "S", "completed_by": "me",
         "completed_at": "2026-08-29T01:00:00"},
        {"id": "g-2", "completed_by_sid": "S", "completed_by": "someone-else",
         "completed_at": "2026-08-29T02:00:00"},
    ]
    with tempfile.TemporaryDirectory() as td:
        pop, total, foreign = cli._closed_this_session(_store(td, goals), "S", 25, "me")
    assert [r["id"] for r in pop] == ["g-1"]
    assert total == 1
    assert foreign == 1


def test_a_close_with_no_completed_by_is_kept_not_discarded():
    """Absence of `completed_by` is not evidence of foreignness. Discarding those
    rows would silently shrink the population — the same silent-absence failure
    the lane detects, reintroduced in its own population query."""
    cli = _load_cli_module()
    goals = [{"id": "g-1", "completed_by_sid": "S", "completed_at": "2026-08-29T01:00:00"}]
    with tempfile.TemporaryDirectory() as td:
        pop, total, foreign = cli._closed_this_session(_store(td, goals), "S", 25, "me")
    assert [r["id"] for r in pop] == ["g-1"] and total == 1 and foreign == 0


def test_the_window_reports_what_it_hid_rather_than_truncating_silently():
    cli = _load_cli_module()
    goals = [{"id": f"g-{i}", "completed_by_sid": "S", "completed_by": "me",
              "completed_at": f"2026-08-29T0{i}:00:00"} for i in range(1, 6)]
    with tempfile.TemporaryDirectory() as td:
        pop, total, _ = cli._closed_this_session(_store(td, goals), "S", 2, "me")
    assert len(pop) == 2 and total == 5           # the caller can see 3 were hidden
    assert [r["id"] for r in pop] == ["g-5", "g-4"]  # most recent first


def test_a_torn_store_line_does_not_blind_the_whole_sweep():
    cli = _load_cli_module()
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "aspirations.jsonl"
        p.write_text(
            "{not json\n"
            + json.dumps({"goals": [{"id": "g-1", "completed_by_sid": "S",
                                     "completed_by": "me",
                                     "completed_at": "2026-08-29T01:00:00"}]}) + "\n",
            encoding="utf-8")
        pop, total, _ = cli._closed_this_session(p, "S", 25, "me")
    assert [r["id"] for r in pop] == ["g-1"]
