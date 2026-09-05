#!/usr/bin/env python3
"""Fixtures for the across-state check replay classifier (gap-089 forge).

WHAT THIS SEAM EXCLUDES, said plainly because a fixture injection point is a
silent scope declaration (guard-1462): every test below drives `classify()`,
which is PURE. So worktree creation, the local-paths.conf copy, the
STORAGE_BACKEND pin, the MIND_WORLD/META pops and the teardown call are all
UPSTREAM of this seam and are structurally unfalsifiable here. They were
validated by a LIVE two-state run against this repo (recorded in the goal's
outcome_note), which is the only thing that can reach them.

The anti-vacuity guard is `test_the_six_shapes_do_not_collapse`. Per guard-1793
it is mutated against ON ITS OWN, not via the suite: an aggregate that stays
green through a defect it was written to catch is not a health check.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from check_replay_across_states import (  # noqa: E402
    classify,
    diff_failing_ids,
    parse_failing_ids,
)


def S(short, passed):
    return {"short": short, "passed": passed}


# ── the four documented outcomes, each a DISTINCT verdict ────────────────────

def test_green_then_red_is_a_regression():
    v, why = classify([S("aaa", True), S("bbb", False)])
    assert v == "REGRESSION"
    assert "introduced" in why


def test_red_then_green_is_fixed_not_a_regression():
    """The  case: the reading that INVERTED under replay.

    Reporting this span as a regression would revert work that fixed nine
    pre-existing reds — the most expensive possible direction to be wrong in.
    """
    v, why = classify([S("aaa", False), S("bbb", True)])
    assert v == "FIXED"
    assert "do NOT report as a regression" in why


def test_red_at_both_ends_is_pre_existing():
    v, why = classify([S("aaa", False), S("bbb", False)])
    assert v == "PRE_EXISTING"
    assert "owning goal" in why


def test_green_at_both_ends_does_not_claim_all_clear():
    """STILL_GREEN must not be readable as 'nothing is wrong'.

    The solo-vs-suite (environmental) axis is invisible to this module, so the
    reason string has to say so or the verdict over-claims.
    """
    v, why = classify([S("aaa", True), S("bbb", True)])
    assert v == "STILL_GREEN"
    assert "solo-vs-suite axis was not examined" in why


# ── edge shapes ──────────────────────────────────────────────────────────────

def test_multiple_transitions_refuse_a_single_endpoint_verdict():
    """green,red,green endpoints are green->green, but calling that STILL_GREEN
    would hide a real break in the middle. More than one flip must escalate."""
    v, why = classify([S("a", True), S("b", False), S("c", True)])
    assert v == "MIXED"
    assert "flips more than once" in why


def test_monotone_span_across_many_states_keeps_the_endpoint_verdict():
    # exactly one transition over 4 states is still a clean REGRESSION
    v, _ = classify([S("a", True), S("b", True), S("c", False), S("d", False)])
    assert v == "REGRESSION"


def test_invalid_states_are_skipped_not_counted_as_pass():
    """An unevaluable state (no conf, worktree failure, timeout) must never be
    silently folded in as a pass — that is how an INVALID run reads as green."""
    v, _ = classify([S("a", None), S("b", False), S("c", False)])
    assert v == "PRE_EXISTING"


def test_fewer_than_two_evaluated_states_is_indeterminate():
    v, why = classify([S("a", True), S("b", None)])
    assert v == "INDETERMINATE"
    assert "need >= 2" in why


def test_all_states_invalid_is_indeterminate_not_still_green():
    v, _ = classify([S("a", None), S("b", None)])
    assert v == "INDETERMINATE"


# ── anti-vacuity (guard-1220 two-way proof; mutate THIS one per guard-1793) ──

def test_the_six_shapes_do_not_collapse():
    """Six inputs, six DISTINCT verdicts.

    A classifier that answered the same way for every shape would pass each
    test above only if that test's own string happened to match — this is the
    assertion that fails if the mapping is ever flattened.
    """
    shapes = {
        "REGRESSION":    [S("a", True),  S("b", False)],
        "FIXED":         [S("a", False), S("b", True)],
        "PRE_EXISTING":  [S("a", False), S("b", False)],
        "STILL_GREEN":   [S("a", True),  S("b", True)],
        "MIXED":         [S("a", True),  S("b", False), S("c", True)],
        "INDETERMINATE": [S("a", True),  S("b", None)],
    }
    got = {name: classify(v)[0] for name, v in shapes.items()}
    assert got == {k: k for k in shapes}, got
    assert len(set(got.values())) == 6


# ═══ set-level readout (gap-183 extension) ═══════════════════════════════════
#
# WHAT THIS SECOND SEAM EXCLUDES (guard-1462, same discipline as the header):
# every test below drives `parse_failing_ids` and `diff_failing_ids`, which are
# PURE. The wiring that feeds them -- `want_ids` threading through `replay()`,
# the decision to parse `r.stdout` rather than the 15-line `output_tail`, the
# `--test-ids` / `--confirm-with` flags, the rc-2-on-unknown branch in `main()`,
# and the whole `confirm_new_at_baseline` worktree run -- is UPSTREAM of this
# seam and structurally unfalsifiable here. `test_real_pytest_output_matches_the
# _fixture_shape` reaches one layer further out by parsing the output of an
# ACTUAL pytest process; nothing here reaches the worktree layer.

import subprocess
import tempfile

import pytest

# A realistic capture: pytest's own progress/verbose body, THEN the summary
# block. Both halves matter -- the body is what an unanchored token grep would
# wrongly harvest from (guard-3918).
_PYTEST_OUT = """\
============================= test session starts ==============================
collected 4 items

core/scripts/tests/test_alpha.py::test_ok PASSED                         [ 25%]
core/scripts/tests/test_alpha.py::test_failed_ordering FAILED            [ 50%]
core/scripts/tests/test_beta.py::test_boom FAILED                        [ 75%]
core/scripts/tests/test_beta.py::test_skipme SKIPPED                     [100%]

=================================== FAILURES ===================================
______________________ test_failed_ordering ____________________________________
E   AssertionError: assert 1 == 2
=========================== short test summary info ============================
FAILED core/scripts/tests/test_alpha.py::test_failed_ordering - AssertionError
FAILED core/scripts/tests/test_beta.py::test_boom - AssertionError: nope
========================= 2 failed, 1 passed in 0.42s ==========================
"""

_NO_SUMMARY = """\
============================= test session starts ==============================
collected 900 items

core/scripts/tests/test_alpha.py .........F...
Fatal Python error: Segmentation fault
"""

_SUMMARY_NAMES_NOTHING = """\
=========================== short test summary info ============================
XFAIL core/scripts/tests/test_alpha.py::test_known - documented
========================= 1 xfailed in 0.10s ===================================
ERROR: usage error, no tests collected
"""


def S2(short, passed, ids, source):
    return {"short": short, "passed": passed, "failing_ids": ids,
            "ids_source": source}


# ── parse_failing_ids: three-valued, and the middle value is the point ───────

def test_a_clean_exit_yields_an_empty_set_not_an_unknown_one():
    assert parse_failing_ids("whatever", 0) == ([], "clean-exit")


def test_a_failed_run_with_no_summary_block_is_unknown_not_empty():
    """THE false-negative guard. An unparsed log must not read as zero failures.

    If this returned `[]` the diff would compute `new_at_last == []` and the
    caller would read "nothing new -- pre-existing, not mine" off a log nobody
    could parse. That is the wrong-surface zero, and it is exactly the class of
    error the whole set-level mode was added to remove.
    """
    ids, src = parse_failing_ids(_NO_SUMMARY, 1)
    assert ids is None, ids
    assert src == "absent"


def test_a_failed_run_whose_summary_names_no_test_is_unknown():
    """Non-zero exit + a summary that names no FAILED/ERROR test = still blind.

    Reporting `[]` here would assert "zero failures" over a run that demonstrably
    failed. The rc and the parsed set contradict each other, so the honest answer
    is None.
    """
    ids, src = parse_failing_ids(_SUMMARY_NAMES_NOTHING, 4)
    assert ids is None, ids
    assert src == "summary-empty"


def test_ids_are_parsed_from_the_short_summary_block():
    ids, src = parse_failing_ids(_PYTEST_OUT, 1)
    assert src == "summary"
    assert ids == [
        "core/scripts/tests/test_alpha.py::test_failed_ordering",
        "core/scripts/tests/test_beta.py::test_boom",
    ]


def test_a_test_whose_name_carries_the_word_failed_is_not_read_as_an_outcome():
    """guard-3918: FAILED / ERROR match TEST NAMES, preferentially on good suites.

    Two distinct traps live in `_PYTEST_OUT` and both must be closed:

      1. the VERBOSE BODY line `...::test_failed_ordering FAILED   [ 50%]`
         sits BEFORE the summary header, so an unanchored scan would harvest
         `core/scripts/tests/test_alpha.py::test_failed_ordering` from a
         position where the outcome is the LAST field, not the first;
      2. the test is genuinely named `test_failed_ordering`, so a substring
         test for "failed" cannot distinguish name from outcome at all.

    The parse must be pinned to line-start INSIDE the summary block, where
    pytest writes the outcome as the FIRST field.
    """
    ids, _ = parse_failing_ids(_PYTEST_OUT, 1)
    # every parsed id is a real node id, never a bare outcome token
    assert all("::" in i for i in ids), ids
    assert "FAILED" not in ids and "ERROR" not in ids
    # the name-collision test IS captured -- correctly, and exactly once
    assert sum(1 for i in ids if i.endswith("::test_failed_ordering")) == 1
    # ...and the PASSED/SKIPPED body lines contributed nothing
    assert not any(i.endswith("::test_ok") or i.endswith("::test_skipme")
                   for i in ids), ids


def test_only_the_last_summary_block_is_read():
    """A concatenated capture (retry, or two chunks in one log) must not union
    the runs -- the last block is the one that describes the final state."""
    first = _PYTEST_OUT.replace("test_boom", "test_stale_from_an_earlier_run")
    ids, _ = parse_failing_ids(first + "\n" + _PYTEST_OUT, 1)
    assert not any("stale" in i for i in ids), ids
    assert len(ids) == 2


# ── diff_failing_ids ─────────────────────────────────────────────────────────

def test_red_at_both_ends_with_different_sets_surfaces_the_new_failures():
    """THE gap-183 CASE, and the contrast is the whole finding.

    Same two states, two readings: `classify()` sees one boolean per state and
    says PRE_EXISTING ("not yours, find the owning goal"), while the SETS show
    the span fixed one failure and introduced another. Asserting both in one
    test is deliberate -- either half alone looks like ordinary behaviour.
    """
    states = [
        S2("aaaaaaaaa", False, ["t.py::old_one", "t.py::shared"], "summary"),
        S2("bbbbbbbbb", False, ["t.py::shared", "t.py::brand_new"], "summary"),
    ]
    assert classify(states)[0] == "PRE_EXISTING"

    d = diff_failing_ids(states)
    assert d["status"] == "ok"
    assert d["set_verdict"] == "NEW_FAILURES"
    assert d["new_at_last"] == ["t.py::brand_new"]
    assert d["gone_at_last"] == ["t.py::old_one"]
    assert d["common"] == ["t.py::shared"]
    # the reason must warn about the contention artifact, or a reader will
    # attribute a phantom NEW-RED to the span (echo, 2026-08-31)
    assert "--confirm-with" in d["reason"]


def test_an_unknown_set_at_either_end_refuses_rather_than_reporting_zero_new():
    """Blind at EITHER end must refuse -- and say so where a reader will look.

    A caller that reads only `new_at_last` sees `[]` on this path, which is
    indistinguishable from a genuine "nothing new". The refusal therefore has to
    live in `status`/`set_verdict` AND be spelled out in `reason`.
    """
    for blind_at in (0, 1):
        states = [
            S2("aaaaaaaaa", False, ["t.py::x"], "summary"),
            S2("bbbbbbbbb", False, ["t.py::x"], "summary"),
        ]
        states[blind_at]["failing_ids"] = None
        states[blind_at]["ids_source"] = "absent"
        d = diff_failing_ids(states)
        assert d["status"] == "unknown", blind_at
        assert d["set_verdict"] == "UNKNOWN", blind_at
        assert d["new_at_last"] == [] and d["gone_at_last"] == []
        assert "UNKNOWN SET IS NOT AN EMPTY ONE" in d["reason"]
        assert states[blind_at]["short"] in d["reason"]


def test_identical_red_sets_are_genuinely_pre_existing():
    states = [
        S2("aaaaaaaaa", False, ["t.py::x", "t.py::y"], "summary"),
        S2("bbbbbbbbb", False, ["t.py::y", "t.py::x"], "summary"),
    ]
    d = diff_failing_ids(states)
    assert d["set_verdict"] == "NO_NEW"
    assert d["new_at_last"] == [] and d["gone_at_last"] == []
    assert d["common"] == ["t.py::x", "t.py::y"]
    assert "genuinely pre-existing at set level" in d["reason"]


def test_a_disappeared_failure_is_not_announced_as_a_fix():
    """`gone_at_last` non-empty is a prompt to LOOK, not a victory: a collection
    error stops a test running at all, which is indistinguishable from a pass."""
    states = [
        S2("aaaaaaaaa", False, ["t.py::x", "t.py::y"], "summary"),
        S2("bbbbbbbbb", False, ["t.py::x"], "summary"),
    ]
    d = diff_failing_ids(states)
    assert d["set_verdict"] == "NO_NEW"
    assert d["gone_at_last"] == ["t.py::y"]
    assert "MASKED" in d["reason"]


def test_green_at_both_ends_reports_an_empty_diff_with_status_ok():
    states = [S2("aaaaaaaaa", True, [], "clean-exit"),
              S2("bbbbbbbbb", True, [], "clean-exit")]
    d = diff_failing_ids(states)
    assert d["status"] == "ok" and d["set_verdict"] == "NO_NEW"
    assert d["common"] == []


def test_fewer_than_two_evaluated_states_is_unknown_here_too():
    d = diff_failing_ids([S2("a", True, [], "clean-exit"), S2("b", None, None, None)])
    assert d["status"] == "unknown" and d["set_verdict"] == "UNKNOWN"
    assert "need >= 2" in d["reason"]


def test_every_path_reports_all_three_buckets_as_lists():
    """guard-4374: when code sorts into buckets and a caller reads a count off
    one of them, EVERY bucket must be pinned on EVERY path. An absent key reads
    as zero; a key present only on the happy path is worse than no key at all."""
    shapes = [
        [S2("a", False, ["t::1"], "summary"), S2("b", False, ["t::2"], "summary")],
        [S2("a", False, ["t::1"], "summary"), S2("b", False, ["t::1"], "summary")],
        [S2("a", False, None, "absent"),      S2("b", False, ["t::1"], "summary")],
        [S2("a", True, [], "clean-exit"),     S2("b", True, [], "clean-exit")],
        [S2("a", None, None, None),           S2("b", None, None, None)],
    ]
    for st in shapes:
        d = diff_failing_ids(st)
        for k in ("new_at_last", "gone_at_last", "common"):
            assert k in d and isinstance(d[k], list), (k, d)
        assert d["status"] in ("ok", "unknown")
        assert d["set_verdict"] in ("NEW_FAILURES", "NO_NEW", "UNKNOWN")
        assert d["reason"].strip()


# ── anti-vacuity ─────────────────────────────────────────────────────────────

def test_the_set_verdicts_do_not_collapse():
    """Three inputs, three DISTINCT set verdicts.

    Mutated ON ITS OWN per guard-1793 -- and it is NOT sufficient by itself, by
    construction: it summarises the VERDICT axis, so a defect that swapped the
    new/gone BUCKETS would leave all three verdicts intact and this line green.
    That axis is pinned per-case above (the gap-183 test asserts each bucket by
    name); this one catches a flattened verdict mapping, nothing more.
    """
    got = {
        "NEW_FAILURES": diff_failing_ids(
            [S2("a", False, ["t::1"], "summary"),
             S2("b", False, ["t::2"], "summary")])["set_verdict"],
        "NO_NEW": diff_failing_ids(
            [S2("a", False, ["t::1"], "summary"),
             S2("b", False, ["t::1"], "summary")])["set_verdict"],
        "UNKNOWN": diff_failing_ids(
            [S2("a", False, None, "absent"),
             S2("b", False, ["t::1"], "summary")])["set_verdict"],
    }
    assert got == {k: k for k in got}, got
    assert len(set(got.values())) == 3


def test_real_pytest_output_matches_the_fixture_shape():
    """POSITIVE CONTROL against a REAL pytest process, not a hand-written string.

    Every other test here proves the parser handles a fixture I wrote; none of
    them can prove the fixture resembles what pytest actually emits. If pytest's
    summary format ever moves, the fixtures stay self-consistently green and the
    parser silently stops finding anything in production. This is the only
    assertion in the file that would notice.

    Runs in a tmp cwd so the repo's pytest.ini/conftest do not apply, and with
    STORAGE_BACKEND=local (guard-955) so nothing can touch a real store.
    """
    with tempfile.TemporaryDirectory() as td:
        f = os.path.join(td, "test_probe_shape.py")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("def test_ok():\n    assert True\n\n"
                     "def test_failed_naming_trap():\n    assert 1 == 2\n")
        env = dict(os.environ)
        env["STORAGE_BACKEND"] = "local"
        env.pop("MIND_WORLD", None)
        env.pop("MIND_META", None)
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pytest", "test_probe_shape.py",
                 "-p", "no:cacheprovider"],
                cwd=td, env=env, timeout=180,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except (OSError, subprocess.TimeoutExpired) as exc:  # pragma: no cover
            pytest.skip(f"cannot run a probe pytest here: {exc}")
    ids, source = parse_failing_ids(r.stdout, r.returncode)
    assert source == "summary", (source, r.stdout[-1500:])
    assert ids == ["test_probe_shape.py::test_failed_naming_trap"], (ids, r.stdout[-1500:])
