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
mutation from recurring-loop-state-mutate.py), but recurring-close.sh's
`run_phase state-update` line (cited :389 until 2026-09-07, when re-derivation
under g-115-4366 found that number stale -- grep the symbol) runs
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


# ── The close-PATH classifier () ────────────────────────────────────
# `classify()` answers WHY a close is uncounted. `classify_close_path()` answers
# whether the close was SANCTIONED — taken by a path that legitimately runs no
# iteration — and if so whether the missing state-update is owed (CREDIT) or not
# (EXCLUDE). Two sanctioned paths produce a BYTE-IDENTICAL changelog fingerprint
# (a CNC-drain and a hypothesis-resolution close), so the split cannot key on
# drain-ness; it keys on the claim history.
#
# ANTI-VACUITY (guard-5163): the first test asserts the four measured-dead
# record signals really do collapse the two cases, so the second test — that the
# classifier separates them — proves something.

def _row(ts, op, agent="bravo", field=None):
    return {"ts": ts, "agent": agent, "op": op, "field": field}


# PROVENANCE. 's verification names  (drain) and 
# (hypothesis-resolution) as the two instances to pin. Neither has a record in the
# live stores or a row in the readable changelog window (it starts 2026-09-15), so
# the rows below are OTHER measured members of those two classes, not those goals'
# own sequences.
_DRAIN_ROWS = [                                    # cause 4: CNC-drain
    _row("2026-08-25T14:02:36", "claim", "foxtrot"),
    _row("2026-09-06T06:41:58", "complete-by", "echo"),
    _row("2026-09-06T06:42:02", "update-goal", "echo", "outcome_class"),
]
_HYPRES_ROWS = [                                   # cause 6: hypothesis-resolution
    _row("2026-09-07T04:30:14", "add-goal", "bravo"),
    _row("2026-09-07T14:36:00", "complete-by", "bravo"),
    _row("2026-09-07T14:36:04", "update-goal", "bravo", "outcome_class"),
]


def test_the_two_sanctioned_closes_collapse_on_every_dead_record_signal():
    """Both are: uncounted, complete-by present, outcome_class present seconds
    later, and claimed_by None at close. Every record-derived discriminator
    measured across three boxes reads the SAME for both."""
    for rows in (_DRAIN_ROWS, _HYPRES_ROWS):
        assert any(r["op"] == "complete-by" for r in rows)
        assert any(r["field"] == "outcome_class" for r in rows)
        # no `release` and no live claim at close time in either
        assert not any(r["op"] == "release" for r in rows)


def test_but_the_classifier_separates_exclude_from_credit():
    assert cps.classify_close_path(_DRAIN_ROWS, "echo") == cps.PATH_SANCTIONED_DISPOSAL
    assert cps.classify_close_path(_HYPRES_ROWS, "bravo") == cps.PATH_SANCTIONED_SELF


def test_the_resume_shape_is_never_suppressed():
    """: complete-by then a LONE outcome_class 15m28s later. This is
    the incident shape the whole check exists for — suppressing it would be the
    worst possible regression."""
    rows = [_row("2026-09-03T14:26:22", "complete-by"),
            _row("2026-09-03T14:41:50", "update-goal", field="outcome_class")]
    assert cps.classify_close_path(rows, "alpha") == cps.PATH_UNCLASSIFIED


def test_a_bare_status_close_has_no_complete_by_and_stays_a_finding():
    rows = [_row("2026-09-03T10:00:00", "update-goal", field="status")]
    assert cps.classify_close_path(rows, "alpha") == cps.PATH_UNCLASSIFIED


def test_a_same_agent_claim_under_matches_rather_than_suppressing():
    """A same-agent claim cannot separate 'disposed my own stale row' from
    'executed and lost the close'. Under-matching is the safe direction."""
    rows = [_row("2026-09-07T10:00:00", "claim", "bravo")] + _HYPRES_ROWS[1:]
    assert cps.classify_close_path(rows, "bravo") == cps.PATH_UNCLASSIFIED


def test_a_row_interposed_between_the_pair_breaks_the_fingerprint():
    rows = [_row("2026-09-07T14:36:00", "complete-by"),
            _row("2026-09-07T14:36:02", "update-goal", field="outcome_note"),
            _row("2026-09-07T14:36:04", "update-goal", field="outcome_class")]
    assert cps.classify_close_path(rows, "bravo") == cps.PATH_UNCLASSIFIED


def test_an_unparseable_stamp_never_reads_as_a_small_gap():
    rows = [_row("not-a-timestamp", "complete-by"),
            _row("2026-09-07T14:36:04", "update-goal", field="outcome_class")]
    assert cps.classify_close_path(rows, "bravo") == cps.PATH_UNCLASSIFIED


def test_no_rows_at_all_is_a_finding_not_a_suppression():
    assert cps.classify_close_path([], "bravo") == cps.PATH_UNCLASSIFIED
    assert cps.classify_close_path(None, "bravo") == cps.PATH_UNCLASSIFIED


def test_the_gap_threshold_separates_the_two_measured_distributions():
    """Sanctioned gaps measured 2-22s (n=8, four boxes); the resume shape 928s."""
    def gap(seconds):
        base = "2026-09-07T14:36:"
        return [_row(base + "00", "complete-by"),
                _row(base + f"{seconds:02d}", "update-goal", field="outcome_class")]
    assert cps.classify_close_path(gap(22), "bravo") == cps.PATH_SANCTIONED_SELF
    assert cps.classify_close_path(gap(59), "bravo") == cps.PATH_SANCTIONED_SELF
    # 61s is past the bound and must fall back to the finding side
    rows = [_row("2026-09-07T14:36:00", "complete-by"),
            _row("2026-09-07T14:37:01", "update-goal", field="outcome_class")]
    assert cps.classify_close_path(rows, "bravo") == cps.PATH_UNCLASSIFIED


def test_decide_without_a_close_path_oracle_is_unchanged():
    """No changelog read (or an unreadable one) must behave exactly as the check
    did before the classifier existed: every uncounted close stays a finding."""
    r = cps.decide([{"id": "g-1"}, {"id": "g-2"}], lambda _g: "absent")
    assert r["skipped"] == ["g-1", "g-2"]
    assert r["sanctioned_excluded"] == [] and r["sanctioned_credited"] == []
    assert r["status"] == "findings"


def test_an_excluded_close_leaves_the_sweep_clean():
    r = cps.decide([{"id": "g-1"}], lambda _g: "absent",
                   close_path=lambda _g: cps.PATH_SANCTIONED_DISPOSAL)
    assert r["skipped"] == [] and r["sanctioned_excluded"] == ["g-1"]
    assert r["status"] == "clean"


def test_a_credited_close_is_still_a_finding():
    """CREDIT means work HAPPENED and the bump is genuinely owed — it must never
    render as health just because the close path was sanctioned."""
    r = cps.decide([{"id": "g-1"}], lambda _g: "absent",
                   close_path=lambda _g: cps.PATH_SANCTIONED_SELF)
    assert r["skipped"] == [] and r["sanctioned_credited"] == ["g-1"]
    assert r["status"] == "findings"


def test_the_classifier_only_reclassifies_skips_never_bump_noops():
    """A ledger-attributed bump no-op must stay attributed to the bump; the
    close-path oracle is consulted only for SKIPPED."""
    seen = []

    def oracle(gid):
        seen.append(gid)
        return cps.PATH_SANCTIONED_DISPOSAL

    r = cps.decide([{"id": "g-1"}], lambda _g: "absent", ("g-1",), close_path=oracle)
    assert r["bump_noop"] == ["g-1"] and seen == []


def test_render_names_both_sanctioned_buckets_distinctly():
    r = cps.decide([{"id": "g-1"}, {"id": "g-2"}, {"id": "g-3"}], lambda _g: "absent",
                   close_path=lambda g: {"g-1": cps.PATH_UNCLASSIFIED,
                                         "g-2": cps.PATH_SANCTIONED_DISPOSAL,
                                         "g-3": cps.PATH_SANCTIONED_SELF}[g])
    out = cps.render(r)
    assert "EXCLUDED" in out and "g-2" in out
    assert "ACCOUNTING GAP" in out and "g-3" in out


def test_the_emitted_prose_did_not_grow_into_a_sixth_caveat(): 
    """guard-4649 / this goal's outcome 2: the discrimination is a classifier in
    the code, not another paragraph in the output. The five-cause catalog moved
    to core/config/rationale/close-phase-skip-causes.md, so the findings line
    must stay short and point there."""
    r = cps.decide([{"id": "g-1"}], lambda _g: "absent")
    out = cps.render(r)
    assert len(out) < 1200, f"findings line regrew to {len(out)} chars"
    assert "close-phase-skip-causes.md" in out


# ── The no-`complete-by` DRAIN close (, unit zc04-nocb-drain) ───────
# The 2026-08-29 cause catalog named the 0.5g.7 completed-not-closed drain as
# cause 4, fingerprinted `complete-by` + `outcome_class` seconds apart. But the
# drain was re-shown to no longer emit `complete-by` at all (rechecks
# 2026-10-08..11): the iteration-close DRAIN branch writes `status`,
# `outcome_class`, then its OWN close-time stamp `key_finding`, seconds apart,
# with no `complete-by` row. So the pre-branch classifier hit its
# `complete_by is None` early return and read EVERY one as a lost state-update,
# re-firing the false alarm daily.
#
# THE DISCRIMINATOR IS THE DRAIN'S OWN STAMP. `key_finding` as an
# `update-goal` row has EXACTLY ONE writer (measured, ):
# iteration-close.sh's DRAIN branch. `aspirations-complete-by.sh` folds
# key_finding into the record under its own `complete-by` row, never as
# `update-goal key_finding`. So a `key_finding` row with NO `complete-by` row
# in the goal's entire changelog can only be a drained close — and a drained
# close is a sanctioned non-executing close (the drainer ran no unit), so the
# missing state-update is EXCLUDEd, CREDITed nothing. The claim history is
# irrelevant for THIS shape (unlike the with-`complete-by` fingerprint, where
# it is the only separator): `--drain` is the closer's own declaration it did
# not run the unit.
#
# ANTI-VACUITY (guard-5163): the drained close and the cause-3 bare-status
# close are IDENTICAL on every pre-existing signal — both have no `complete-by`,
# both are uncounted. The only thing that separates them is the drain's own
# `key_finding` stamp beside the close triple. The first test below asserts the
# old signals really do collapse, so the second — that the classifier now
# separates them — proves something.

def _drain_rows(status_field="status", kf_offset=6, oc_offset=3, claim=True):
    """The measured drain shape: [claim], then the close triple seconds apart.
    `status_field` is the logged field of the status row (`status`, or
    `status+outcome_note` when the closure note was folded into the SAME locked
    RMW as the status flip — g-358-36 companion; measured on drain close
    g-377-108, recheck 2026-10-10). `kf_offset`/`oc_offset` are seconds after
    the status row for key_finding / outcome_class."""
    base = "2026-10-08T07:49:"
    rows = []
    if claim:
        rows.append(_row("2026-10-08T07:49:00", "claim", "bravo"))
    rows.append(_row(base + "20", "update-goal", "bravo", status_field))
    rows.append(_row(base + f"{20 + oc_offset:02d}", "update-goal", "bravo",
                     "outcome_class"))
    rows.append(_row(base + f"{20 + kf_offset:02d}", "update-goal", "bravo",
                     "key_finding"))
    return rows


_DRAIN_ROWS_NOCL = _drain_rows()          # bare status + outcome_class + key_finding
_DRAIN_ROWS_COMPANION = _drain_rows(status_field="status+outcome_note")
_BARE_STATUS_NOCL = [                      # cause 3: bare-status close, NO drain stamp
    _row("2026-10-10T09:00:00", "claim", "bravo"),
    _row("2026-10-10T09:05:00", "update-goal", "bravo", "status"),
]


def test_the_drain_and_bare_status_collapse_on_every_dead_signal():
    """The half that makes the next test mean something.

    Both are uncounted and both have NO `complete-by` row. Every record-derived
    discriminator reads the same for both; the only thing that separates them is
    the drain's own key_finding stamp, which the old classifier never read.
    """
    for rows in (_DRAIN_ROWS_NOCL, _BARE_STATUS_NOCL):
        assert not any(r["op"] == "complete-by" for r in rows), rows
        assert any(r["op"] == "claim" for r in rows), rows
        assert any(r["field"] == "status" for r in rows), rows


def test_the_classifier_separates_the_drain_close_from_a_bare_status():
    """The new branch: a drained close (key_finding stamp, no complete-by) is a
    sanctioned non-executing close -> EXCLUDE. A bare-status close (cause 3, no
    drain stamp) stays a finding -> UNCLASSIFIED."""
    assert cps.classify_close_path(_DRAIN_ROWS_NOCL, "bravo") == \
        cps.PATH_SANCTIONED_DISPOSAL
    assert cps.classify_close_path(_BARE_STATUS_NOCL, "bravo") == \
        cps.PATH_UNCLASSIFIED


def test_the_drain_close_is_excluded_even_for_a_same_agent_claim():
    """`--drain` is the closer's own declaration it ran no unit, so a same-agent
    claim does NOT make the missing state-update owed (unlike the with-
    `complete-by` fingerprint, where a same-agent claim under-matches to
    UNCLASSIFIED). The 10-08..11 rechecks measured exactly this same-agent drain
    shape being misread as a loss; the repair (a state-update) would corrupt the
    counters it is meant to fix."""
    rows = _drain_rows(claim=True)
    assert any(r["op"] == "claim" and r["agent"] == "bravo" for r in rows)
    assert cps.classify_close_path(rows, "bravo") == cps.PATH_SANCTIONED_DISPOSAL


def test_the_companion_status_field_variant_is_also_a_drain_close():
    """ folds the closure note into the SAME locked RMW as the status
    flip; the daemon logs that row's field as `status+outcome_note`, not
    `status`. Measured on drain close g-377-108 (recheck 2026-10-10). It must
    classify as the same EXCLUDE, not under-match to a finding."""
    assert cps.classify_close_path(_DRAIN_ROWS_COMPANION, "bravo") == \
        cps.PATH_SANCTIONED_DISPOSAL


def test_the_drain_branch_never_fires_when_a_complete_by_is_present():
    """The branch is gated on `complete_by is None`. A goal that DID close
    through complete-by (and also carries a key_finding, as the recurring /
    hypothesis-resolution paths can) must take the with-`complete-by` path, not
    be re-read as a drain. No claim row -> CREDIT, never the drain's EXCLUDE."""
    rows = [
        _row("2026-09-07T14:36:00", "complete-by", "bravo"),
        _row("2026-09-07T14:36:04", "update-goal", "bravo", "outcome_class"),
        _row("2026-09-07T14:36:06", "update-goal", "bravo", "key_finding"),
    ]
    assert cps.classify_close_path(rows, "bravo") == cps.PATH_SANCTIONED_SELF


def test_the_drain_fingerprint_anchors_on_the_close_cluster_not_the_first_flip():
    """A goal's window can carry an in-progress `status` flip days before the
    close. Anchoring on that earlier row would read the days-long gap as a
    resume and under-match a real drain; each cluster member must be the row
    NEAREST the drain's own stamp."""
    rows = [
        _row("2026-09-01T10:00:00", "claim", "bravo"),
        _row("2026-09-01T10:05:00", "update-goal", "bravo", "status"),  # days earlier
        _row("2026-09-20T21:18:54", "update-goal", "bravo", "status"),  # the close
        _row("2026-09-20T21:18:57", "update-goal", "bravo", "outcome_class"),
        _row("2026-09-20T21:19:00", "update-goal", "bravo", "key_finding"),
    ]
    assert cps.classify_close_path(rows, "bravo") == cps.PATH_SANCTIONED_DISPOSAL


def test_a_drain_close_with_a_gap_past_the_bound_stays_a_finding():
    """A close triple where outcome_class lands >60s from the drain stamp is not
    the sanctioned seconds-apart shape — under-match to a finding."""
    rows = [
        _row("2026-10-08T07:49:00", "claim", "bravo"),
        _row("2026-10-08T07:49:20", "update-goal", "bravo", "status"),
        _row("2026-10-08T07:49:26", "update-goal", "bravo", "key_finding"),
        _row("2026-10-08T07:52:00", "update-goal", "bravo", "outcome_class"),  # 154s after kf
    ]
    assert cps.classify_close_path(rows, "bravo") == cps.PATH_UNCLASSIFIED


def test_a_row_interposed_inside_the_drain_cluster_breaks_the_fingerprint():
    """The same intra-cluster hygiene the with-`complete-by` fingerprint enforces:
    a stray mid-close write breaks the match and the close stays a finding."""
    rows = [
        _row("2026-10-08T07:49:00", "claim", "bravo"),
        _row("2026-10-08T07:49:20", "update-goal", "bravo", "status"),
        _row("2026-10-08T07:49:22", "update-goal", "bravo", "outcome_note"),  # stray
        _row("2026-10-08T07:49:23", "update-goal", "bravo", "outcome_class"),
        _row("2026-10-08T07:49:26", "update-goal", "bravo", "key_finding"),
    ]
    assert cps.classify_close_path(rows, "bravo") == cps.PATH_UNCLASSIFIED


def test_a_drain_stamp_without_the_full_triple_is_not_a_drain_close():
    """A key_finding row with no outcome_class, or with no status, beside it is
    not the close triple — a lone stamp does not prove the close ran. Both
    under-match to a finding."""
    no_oc = [
        _row("2026-10-08T07:49:00", "claim", "bravo"),
        _row("2026-10-08T07:49:20", "update-goal", "bravo", "status"),
        _row("2026-10-08T07:49:26", "update-goal", "bravo", "key_finding"),
    ]
    no_st = [
        _row("2026-10-08T07:49:00", "claim", "bravo"),
        _row("2026-10-08T07:49:23", "update-goal", "bravo", "outcome_class"),
        _row("2026-10-08T07:49:26", "update-goal", "bravo", "key_finding"),
    ]
    assert cps.classify_close_path(no_oc, "bravo") == cps.PATH_UNCLASSIFIED
    assert cps.classify_close_path(no_st, "bravo") == cps.PATH_UNCLASSIFIED


def test_an_unparseable_drain_stamp_never_reads_as_a_small_gap():
    rows = [
        _row("2026-10-08T07:49:00", "claim", "bravo"),
        _row("2026-10-08T07:49:20", "update-goal", "bravo", "status"),
        _row("2026-10-08T07:49:23", "update-goal", "bravo", "outcome_class"),
        _row("not-a-timestamp", "update-goal", "bravo", "key_finding"),
    ]
    assert cps.classify_close_path(rows, "bravo") == cps.PATH_UNCLASSIFIED


def test_decide_excludes_an_uncounted_drain_close_and_keeps_the_sweep_clean():
    """The integration half: an uncounted close the classifier attributes to a
    drained path is EXCLUDED (not a finding), and a sweep whose only uncounted
    close is such a drain renders CLEAN, not findings."""
    r = cps.decide([{"id": "g-1"}], lambda _g: "absent",
                   close_path=lambda _g: cps.classify_close_path(_DRAIN_ROWS_NOCL,
                                                                 "bravo"))
    assert r["skipped"] == [] and r["sanctioned_excluded"] == ["g-1"]
    assert r["status"] == "clean"
    assert "EXCLUDED" in cps.render(r)


# ── The RESOLVE-PASS close (, cause 11, zeta/cc-02 2026-10-11) ──────
# A hypothesis RESOLVER goal closed by the resolve pass itself. The pass is an
# iteration of an enclosing recurring goal (, occurrence 81) whose own
# state-update ran — its journal block is stamped 03:25 and the goal reads
# counted — while the two resolver goals it closed read uncounted and were
# reported as "NO state-update". Neither had an iteration of its own: no claim
# row, no outcome_class row, just a note and a bare `complete-by` 8 seconds
# later. That changelog shape is byte-identical to a lost DIRECT close (causes 2
# and 5), so the changelog alone cannot separate them (the first test pins that).
# What separates them is a DURABLE RECORD OF THE WORK: the pipeline stamps the
# hypothesis `resolved_at` / `resolved_by` when the pass resolves it.
#
# THE DISCRIMINATOR AND ITS NEIGHBOURS (guard-2898). The property that carries
# the carve-out is "no iteration of this goal's own existed AND its work is
# recorded by the closer's own resolution" — a CONJUNCTION. Each neighbour that a
# narrower reading of it would wrongly admit is pinned below to keep its old
# verdict: a claimed resolver goal that lost its close, a close that ran the
# verify machinery, another agent's resolution, a close that PRECEDES the
# resolution, another agent's complete-by, and every unreadable input.

_RESOLVE_ROWS_11976 = [                      # measured, cc-02, 2026-10-11
    _row("2026-10-04T04:35:38", "add-goal", "zeta"),
    _row("2026-10-04T04:35:55", "update-goal", "zeta", "status+outcome_note"),
    _row("2026-10-11T02:56:42", "update-goal", "zeta", "outcome_note"),
    _row("2026-10-11T02:56:50", "complete-by", "zeta"),
]
_RESOLUTION_11976 = {"resolved_at": "2026-10-11T02:55:08", "resolved_by": "zeta"}
_RESOLVE_ROWS_12073 = [                      # measured, cc-02, 2026-10-11
    _row("2026-10-11T03:15:44", "update-goal", "zeta", "status+outcome_note"),
    _row("2026-10-11T03:16:15", "update-goal", "zeta", "outcome_note"),
    _row("2026-10-11T03:16:23", "complete-by", "zeta"),
]
_RESOLUTION_12073 = {"resolved_at": "2026-10-11T03:10:57", "resolved_by": "zeta"}
_RESOLVE_CASES = [(_RESOLVE_ROWS_11976, _RESOLUTION_11976),
                  (_RESOLVE_ROWS_12073, _RESOLUTION_12073)]


def test_the_resolve_pass_close_is_a_finding_on_the_changelog_alone():
    """Anti-vacuity (guard-5163). Both measured closes have no claim, no
    outcome_class and a lone complete-by — the exact fingerprint of a lost direct
    close — so without the pipeline stamp they MUST stay findings. If this
    passed through to EXCLUDE, the stamp would be doing no work."""
    for rows, _res in _RESOLVE_CASES:
        assert not any(r["op"] == "claim" for r in rows)
        assert not any(r["field"] == "outcome_class" for r in rows)
        assert cps.classify_close_path(rows, "zeta") == cps.PATH_UNCLASSIFIED


def test_the_pipeline_stamp_separates_the_measured_resolve_pass_closes():
    for rows, res in _RESOLVE_CASES:
        assert cps.classify_close_path(rows, "zeta", resolution=res) \
            == cps.PATH_SANCTIONED_DISPOSAL
        assert cps.classify_close_path(rows, "zeta", resolution=lambda r=res: r) \
            == cps.PATH_SANCTIONED_DISPOSAL


def test_a_claimed_resolver_goal_whose_close_was_lost_stays_a_finding():
    """The neighbour that matters most: a resolver goal executed as an iteration
    of its own (claimed) and closed without state-update is the loss this check
    exists to catch, and it has the same pipeline stamp as the resolve pass."""
    for claimant in ("zeta", "echo"):
        rows = [_row("2026-10-11T02:30:00", "claim", claimant)] + _RESOLVE_ROWS_11976
        assert cps.classify_close_path(rows, "zeta", resolution=_RESOLUTION_11976) \
            == cps.PATH_UNCLASSIFIED


def test_a_close_that_ran_the_verify_machinery_is_not_this_branch():
    """Any outcome_class row means close machinery ran, so the cause-6 pair, the
    cause-1 resume and the cause-8 reversal own the verdict."""
    rows = _RESOLVE_ROWS_11976
    late = rows + [_row("2026-10-11T03:11:50", "update-goal", "zeta", "outcome_class")]
    early = [_row("2026-10-11T02:56:30", "update-goal", "zeta", "outcome_class")] + rows
    for shape in (late, early):
        assert cps.classify_close_path(shape, "zeta", resolution=_RESOLUTION_11976) \
            == cps.PATH_UNCLASSIFIED
    # the cause-6 pair keeps its CREDIT verdict even when a resolution exists
    res = {"resolved_at": "2026-09-07T14:00:00", "resolved_by": "bravo"}
    assert cps.classify_close_path(_HYPRES_ROWS, "bravo", resolution=res) \
        == cps.PATH_SANCTIONED_SELF


def test_another_agents_resolution_is_not_the_closers_work():
    res = dict(_RESOLUTION_11976, resolved_by="echo")
    assert cps.classify_close_path(_RESOLVE_ROWS_11976, "zeta", resolution=res) \
        == cps.PATH_UNCLASSIFIED


def test_a_close_that_precedes_the_resolution_is_not_its_bookkeeping():
    late = dict(_RESOLUTION_11976, resolved_at="2026-10-11T02:56:51")
    assert cps.classify_close_path(_RESOLVE_ROWS_11976, "zeta", resolution=late) \
        == cps.PATH_UNCLASSIFIED
    same = dict(_RESOLUTION_11976, resolved_at="2026-10-11T02:56:50")
    assert cps.classify_close_path(_RESOLVE_ROWS_11976, "zeta", resolution=same) \
        == cps.PATH_SANCTIONED_DISPOSAL


def test_another_agents_complete_by_is_not_the_closers_close():
    rows = _RESOLVE_ROWS_11976[:-1] + [_row("2026-10-11T02:56:50", "complete-by", "echo")]
    assert cps.classify_close_path(rows, "zeta", resolution=_RESOLUTION_11976) \
        == cps.PATH_UNCLASSIFIED


@pytest.mark.parametrize("resolution", [
    None, {}, {"resolved_by": "zeta"}, {"resolved_at": "2026-10-11T02:55:08"},
    {"resolved_at": "not-a-timestamp", "resolved_by": "zeta"}, "resolved", [],
])
def test_an_unreadable_resolution_never_suppresses(resolution):
    assert cps.classify_close_path(_RESOLVE_ROWS_11976, "zeta", resolution=resolution) \
        == cps.PATH_UNCLASSIFIED


def test_a_raising_or_unknown_closer_never_suppresses():
    def boom():
        raise RuntimeError("pipeline unreadable")
    assert cps.classify_close_path(_RESOLVE_ROWS_11976, "zeta", resolution=boom) \
        == cps.PATH_UNCLASSIFIED
    assert cps.classify_close_path(_RESOLVE_ROWS_11976, None,
                                   resolution=_RESOLUTION_11976) == cps.PATH_UNCLASSIFIED
    bad_close = _RESOLVE_ROWS_11976[:-1] + [_row("garbled", "complete-by", "zeta")]
    assert cps.classify_close_path(bad_close, "zeta", resolution=_RESOLUTION_11976) \
        == cps.PATH_UNCLASSIFIED


def test_the_resolution_is_read_only_when_the_changelog_shape_already_matches():
    """The pipeline read is a subprocess. It must not happen for every uncounted
    close — only for one whose changelog already says no claim, no outcome_class
    and the closer's own complete-by."""
    calls = []

    def lookup():
        calls.append(1)
        return _RESOLUTION_11976

    claimed = [_row("2026-10-11T02:30:00", "claim", "zeta")] + _RESOLVE_ROWS_11976
    verified = _RESOLVE_ROWS_11976 + [_row("2026-10-11T02:56:53", "update-goal",
                                           "zeta", "outcome_class")]
    foreign = _RESOLVE_ROWS_11976[:-1] + [_row("2026-10-11T02:56:50", "complete-by", "echo")]
    foreign_status = [_row("2026-10-11T02:56:42", "update-goal", "echo", "status")]
    note_only = [_row("2026-10-11T02:56:42", "update-goal", "zeta", "outcome_note")]
    for rows in (claimed, verified, foreign, foreign_status, note_only):
        cps.classify_close_path(rows, "zeta", resolution=lookup)
    assert calls == []
    assert cps.classify_close_path(_RESOLVE_ROWS_11976, "zeta", resolution=lookup) \
        == cps.PATH_SANCTIONED_DISPOSAL
    assert calls == [1]
    # the status-flip close matches the same structure, so it reads exactly once too
    bare = [_row("2026-10-11T02:56:42", "update-goal", "zeta", "status")]
    assert cps.classify_close_path(bare, "zeta", resolution=lookup) \
        == cps.PATH_SANCTIONED_DISPOSAL
    assert calls == [1, 1]


def test_there_is_no_time_bound_on_the_close():
    """Deliberate (see _resolve_pass_close): conditions 1-4 already say no
    iteration existed and the work is recorded, so how long after the resolution
    the close came does not change the verdict. A future bound must break this
    test on purpose, not by accident."""
    early = dict(_RESOLUTION_11976, resolved_at="2026-09-20T09:00:00")
    assert cps.classify_close_path(_RESOLVE_ROWS_11976, "zeta", resolution=early) \
        == cps.PATH_SANCTIONED_DISPOSAL


def test_decide_excludes_both_measured_resolve_pass_closes_and_keeps_the_sweep_clean():
    by_goal = {"g-115-11976": (_RESOLVE_ROWS_11976, _RESOLUTION_11976),
               "g-115-12073": (_RESOLVE_ROWS_12073, _RESOLUTION_12073)}
    r = cps.decide([{"id": g} for g in by_goal], lambda _g: "absent",
                   close_path=lambda g: cps.classify_close_path(
                       by_goal[g][0], "zeta", resolution=by_goal[g][1]))
    assert r["skipped"] == [] and r["sanctioned_credited"] == []
    assert sorted(r["sanctioned_excluded"]) == ["g-115-11976", "g-115-12073"]
    assert r["status"] == "clean"


# The SAME close typed as a bare status flip (no `complete-by`). SHAPE AS REPORTED
# by foxtrot ( recheck-foxtrot-20261011: three hypothesis-review
# sub-goals closed by `update-goal outcome_note` then `update-goal status` inside
# one occurrence of ) — NOT measured on this box, so these rows are
# modeled on the measured complete-by rows of  with the close typed as
# a status flip. The earlier `status+outcome_note` row is that goal's real GROOM
# promote, days before the resolution: it is why the branch must anchor on the
# LAST status write — anchored on the first, the ordering test would fail.
_RESOLVE_STATUS_ROWS = [
    _row("2026-10-04T04:35:38", "add-goal", "foxtrot"),
    _row("2026-10-04T04:35:55", "update-goal", "foxtrot", "status+outcome_note"),
    _row("2026-10-11T02:56:42", "update-goal", "foxtrot", "outcome_note"),
    _row("2026-10-11T02:56:50", "update-goal", "foxtrot", "status"),
]
_RESOLUTION_STATUS = {"resolved_at": "2026-10-11T02:55:08", "resolved_by": "foxtrot"}


def test_a_resolve_pass_close_typed_as_a_status_flip_needs_the_stamp_too():
    """Anti-vacuity: without the stamp this is cause 3's bare-status fingerprint
    and stays a finding; the pipeline stamp is the only thing that moves it."""
    assert cps.classify_close_path(_RESOLVE_STATUS_ROWS, "foxtrot") == cps.PATH_UNCLASSIFIED
    assert cps.classify_close_path(_RESOLVE_STATUS_ROWS, "foxtrot",
                                   resolution=_RESOLUTION_STATUS) \
        == cps.PATH_SANCTIONED_DISPOSAL


def test_the_status_flip_variant_accepts_the_companion_field_and_keeps_its_neighbours():
    close = _row("2026-10-11T02:56:50", "update-goal", "foxtrot", "status+outcome_note")
    folded = _RESOLVE_STATUS_ROWS[:2] + [close]
    assert cps.classify_close_path(folded, "foxtrot", resolution=_RESOLUTION_STATUS) \
        == cps.PATH_SANCTIONED_DISPOSAL
    claimed = [_row("2026-10-11T02:30:00", "claim", "foxtrot")] + _RESOLVE_STATUS_ROWS
    verified = _RESOLVE_STATUS_ROWS + [_row("2026-10-11T02:56:53", "update-goal",
                                            "foxtrot", "outcome_class")]
    foreign_close = _RESOLVE_STATUS_ROWS[:-1] + [_row("2026-10-11T02:56:50", "update-goal",
                                                      "echo", "status")]
    no_status = [_row("2026-10-11T02:56:42", "update-goal", "foxtrot", "outcome_note")]
    for name, rows in (("claimed", claimed), ("verified", verified),
                       ("foreign close", foreign_close), ("no status write", no_status)):
        assert cps.classify_close_path(rows, "foxtrot", resolution=_RESOLUTION_STATUS) \
            == cps.PATH_UNCLASSIFIED, name
    other = dict(_RESOLUTION_STATUS, resolved_by="echo")
    assert cps.classify_close_path(_RESOLVE_STATUS_ROWS, "foxtrot", resolution=other) \
        == cps.PATH_UNCLASSIFIED
    late = dict(_RESOLUTION_STATUS, resolved_at="2026-10-11T02:56:51")
    assert cps.classify_close_path(_RESOLVE_STATUS_ROWS, "foxtrot", resolution=late) \
        == cps.PATH_UNCLASSIFIED


def test_a_drain_close_still_takes_the_drain_branch_first():
    """The two no-complete-by branches must not shadow each other: a drain triple
    is EXCLUDEd by the drain fingerprint whether or not a resolution is passed."""
    assert cps.classify_close_path(_DRAIN_ROWS_NOCL, "bravo") == cps.PATH_SANCTIONED_DISPOSAL
    assert cps.classify_close_path(_DRAIN_ROWS_NOCL, "bravo",
                                   resolution={"resolved_at": "2026-01-01T00:00:00",
                                               "resolved_by": "bravo"}) \
        == cps.PATH_SANCTIONED_DISPOSAL


# ── The CLI half carries the resolution to the classifier (guard-2241, rb-6234) ─
# Widening what the classifier READS is inert if its production call site never
# hands it the data. Every test above passes `resolution=` by hand, so none of
# them can see a call site that forgets to. These do.

def _cl_line(ts, agent, op, gid, field=""):
    return (f"[{ts}] {agent} edit aspirations.jsonl (27 lines) — {op} {gid}"
            f"{(' ' + field) if field else ''}")


def _fake_run(changelog_lines, pipeline, seen):
    """Stand-in for subprocess.run: answers the changelog read and the pipeline
    read, and refuses anything else so a stray call cannot pass silently."""
    class _Done:
        def __init__(self, out):
            self.stdout, self.stderr, self.returncode = out, "", 0

    def run(cmd, **_kw):
        target = str(cmd[1])
        if target.endswith("changelog-read.sh"):
            return _Done("\n".join(changelog_lines) + "\n")
        if target.endswith("pipeline-read.sh") and cmd[2] == "--id":
            seen.append(cmd[3])
            rec = pipeline.get(cmd[3])
            return _Done(json.dumps(rec) if rec is not None else "")
        raise AssertionError(f"unexpected subprocess call: {cmd}")
    return run


def test_the_population_carries_the_hypothesis_id_to_the_oracle():
    cli = _load_cli_module()
    goals = [{"id": "g-1", "completed_by_sid": "S", "completed_by": "me",
              "completed_at": "2026-10-11T02:56:47", "hypothesis_id": "2026-10-04_h"},
             {"id": "g-2", "completed_by_sid": "S", "completed_by": "me",
              "completed_at": "2026-10-11T02:57:00"}]
    with tempfile.TemporaryDirectory() as td:
        pop, _, _ = cli._closed_this_session(_store(td, goals), "S", 25, "me")
    assert {r["id"]: r["hypothesis_id"] for r in pop} == {"g-1": "2026-10-04_h", "g-2": None}


def test_the_oracle_hands_the_classifier_a_lazy_pipeline_lookup(monkeypatch):
    cli = _load_cli_module()
    lines = [
        _cl_line("2026-10-11T02:56:42", "zeta", "update-goal", "g-900-01", "outcome_note"),
        _cl_line("2026-10-11T02:56:50", "zeta", "complete-by", "g-900-01"),
        # the same shape, but CLAIMED: an iteration of its own, so a real loss
        _cl_line("2026-10-11T02:30:00", "zeta", "claim", "g-900-02"),
        _cl_line("2026-10-11T02:56:42", "zeta", "update-goal", "g-900-02", "outcome_note"),
        _cl_line("2026-10-11T02:56:50", "zeta", "complete-by", "g-900-02"),
        # the same shape with NO hypothesis behind it
        _cl_line("2026-10-11T02:56:42", "zeta", "update-goal", "g-900-03", "outcome_note"),
        _cl_line("2026-10-11T02:56:50", "zeta", "complete-by", "g-900-03"),
    ]
    pipeline = {"2026-10-04_h": {"id": "2026-10-04_h", "stage": "resolved",
                                 "resolved_at": "2026-10-11T02:55:08", "resolved_by": "zeta"}}
    seen = []
    monkeypatch.setattr(cli.subprocess, "run", _fake_run(lines, pipeline, seen))
    verdict = cli._close_path_oracle(
        ["g-900-01", "g-900-02", "g-900-03"], "zeta",
        {"g-900-01": "2026-10-04_h", "g-900-02": "2026-10-04_h"})
    assert verdict("g-900-01") == cps.PATH_SANCTIONED_DISPOSAL
    assert verdict("g-900-02") == cps.PATH_UNCLASSIFIED
    assert verdict("g-900-03") == cps.PATH_UNCLASSIFIED
    assert seen == ["2026-10-04_h"], "pipeline read only for the matching shape"


@pytest.mark.parametrize("record", [
    {"stage": "active", "resolved_at": None},
    {"stage": "resolved"},
    {"stage": "discovered", "resolved_at": "2026-10-11T02:55:08", "resolved_by": "zeta"},
    None,
])
def test_an_unresolved_or_unreadable_pipeline_record_never_suppresses(monkeypatch, record):
    cli = _load_cli_module()
    monkeypatch.setattr(cli.subprocess, "run", _fake_run([], {"h": record}, []))
    assert cli._hypothesis_resolution("h") is None


def test_a_resolved_pipeline_record_yields_its_stamp_even_list_wrapped(monkeypatch):
    cli = _load_cli_module()
    rec = {"stage": "resolved", "resolved_at": "2026-10-11T02:55:08", "resolved_by": "zeta"}
    for payload in (rec, [rec]):
        monkeypatch.setattr(cli.subprocess, "run", _fake_run([], {"h": payload}, []))
        assert cli._hypothesis_resolution("h") == {"resolved_at": "2026-10-11T02:55:08",
                                                   "resolved_by": "zeta"}


def test_main_passes_the_hypothesis_map_into_the_oracle():
    """Pin the production call site (rb-6234): the unit tests above hand the map
    to `_close_path_oracle` themselves, so a `main()` that stopped building it
    would leave every one of them green and the lane unchanged."""
    src = (_SCRIPTS / "close-phase-skip-check.py").read_text(encoding="utf-8")
    assert "_close_path_oracle(gids, agent, hypotheses)" in src
    assert 'if r.get("id") and r.get("hypothesis_id")' in src
