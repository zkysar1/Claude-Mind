""" — the worker-path recurring tally.

Two jobs. The behavioural tests pin the ARITHMETIC; the parity test pins the
fact that recurring-close.sh's copy of that arithmetic has not drifted from it.
The second is the one criterion (c) asks for: without it, a future edit to
either side changes one path's counters and nothing anywhere goes red.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from recurring_tally import TALLY_FIELDS, compute, read_goal  # noqa: E402

NOW = "2026-08-29T03:00:00"


def _goal(**over):
    base = {
        "id": "g-test-01", "recurring": True,
        "substantive_runs": 10, "substantive_hits": 4,
        "consecutive_routine": 3, "consecutive_deep": 0,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------- arithmetic

def test_routine_close_advances_runs_and_routine_streak_only():
    out = compute(_goal(), "routine", "genuine", NOW)
    assert out["substantive_runs"] == 11
    assert out["consecutive_routine"] == 4
    # A routine close must not touch the numerator or its stamp — that is what
    # keeps lifetime_hit_rate meaningful.
    assert "substantive_hits" not in out
    assert "last_substantive_at" not in out


def test_genuine_deep_advances_numerator_and_stamps_the_catch():
    out = compute(_goal(consecutive_deep=2), "deep", "genuine", NOW)
    assert out["substantive_runs"] == 11
    assert out["substantive_hits"] == 5
    assert out["last_substantive_at"] == NOW
    assert out["consecutive_deep"] == 3
    assert out["consecutive_routine"] == 0


def test_forced_flip_deep_advances_denominator_but_never_the_numerator():
    """The anti-drift flip is a defense, not substantive output.

    If a forced flip advanced substantive_hits it would inflate exactly the
    rate the chronic-low detector reads, which is the failure the genuine/
    forced split exists to prevent.
    """
    out = compute(_goal(consecutive_deep=2), "deep", "forced-flip", NOW)
    assert out["substantive_runs"] == 11
    assert "substantive_hits" not in out
    assert "last_substantive_at" not in out
    assert "consecutive_deep" not in out          # PINNED, not advanced
    assert out["consecutive_routine"] == 0


def test_legacy_goal_with_no_counters_starts_at_one():
    out = compute({"id": "g", "recurring": True}, "routine", "genuine", NOW)
    assert out["substantive_runs"] == 1
    assert out["consecutive_routine"] == 1


def test_corrupt_counter_is_treated_as_zero_not_fatal():
    """A close must never be blocked by an unparseable bookkeeping field."""
    out = compute(_goal(substantive_runs="oops"), "routine", "genuine", NOW)
    assert out["substantive_runs"] == 1


def test_no_op_fields_are_omitted_so_callers_issue_no_pointless_writes():
    out = compute(_goal(consecutive_routine=0, consecutive_deep=0),
                  "deep", "forced-flip", NOW)
    assert set(out) == {"substantive_runs"}


def test_every_returned_key_is_a_declared_tally_field():
    for outcome, origin in (("routine", "genuine"), ("deep", "genuine"),
                            ("deep", "forced-flip")):
        assert set(compute(_goal(), outcome, origin, NOW)) <= set(TALLY_FIELDS)


# ------------------------------------------------------------------- parity

def test_arithmetic_matches_recurring_close_source():
    """THE DIVERGENCE PIN (criterion c).

    recurring-close.sh carries its own copy of this arithmetic in a post-phase
    heredoc. These four lines are that copy. If anyone edits them, this test
    goes red and forces the same edit here — which is the whole point: before
    this test existed, the two paths could disagree with nothing failing.

    Deliberately a SOURCE pin, not a behavioural one: the heredoc also clears
    pull_signal, reads aspirations.yaml and emits notifications, so executing
    it inside a unit test would be a different and much heavier thing than
    checking that its arithmetic still reads the way this module implements it.
    """
    src = (SCRIPTS / "recurring-close.sh").read_text(encoding="utf-8")
    for expr in (
        "new_sub_runs = current_sub_runs + 1",
        "new_sub_hits = current_sub_hits + 1",
        'new_val = current + 1 if outcome == "routine" else 0',
        "new_deep = current_deep + 1",
    ):
        assert expr in src, (
            f"recurring-close.sh no longer contains {expr!r}. Its arithmetic "
            f"changed; update recurring_tally.compute() to match, then update "
            f"this list."
        )


def test_worker_branch_is_role_guarded_so_the_reducer_cannot_double_count():
    """NEGATIVE CONTROL (guard-1665).

    The reducer reaches the same arithmetic through recurring-close.sh, and it
    invokes do_verify as its own `verify` phase — so an UNGUARDED call site
    here would add a second increment to every reducer close. The guard is the
    load-bearing half of this change; assert the call site is inside it.
    """
    src = (SCRIPTS / "iteration-close.sh").read_text(encoding="utf-8")
    # Anchor on the INVOCATION, not the module name: the name also appears in
    # the block comment above, and anchoring there passed over ~700 characters
    # of prose instead of the code. (This assertion caught exactly that on its
    # first run — the test was wrong, the shell was right.)
    call = 'python3 "$SCRIPT_DIR/recurring_tally.py"'
    assert src.count(call) == 1, f"expected exactly one call site, found {src.count(call)}"
    idx = src.index(call)
    guard = ('if [[ "${BODY_ROLE:-}" == "worker" '
             '&& -z "${RECURRING_TALLY_OWNER:-}" ]]; then')
    # Scope to THIS block. iteration-close.sh carries an earlier, unrelated
    # BODY_ROLE=worker guard (the  pending-deploys sweep), and
    # searching the whole file found THAT one — so this assertion passed with
    # my guard deleted. The mutation proof caught it (role-guard-removed
    # SURVIVED); rb-9476, a scoped check that is present, correct-looking and
    # inert. Anchor on this block's own marker.
    marker = "g-115-6768: WORKER-ONLY recurring-tally advance"
    assert src.count(marker) == 1
    before = src[src.index(marker):idx]
    assert guard in before, "tally call site is not inside a BODY_ROLE=worker guard"
    # It must be the NEAREST preceding guard and still OPEN at the call site —
    # a guard that closed before the call would read as protection and be none.
    between = before[before.rindex(guard) + len(guard):]
    assert "\n        fi" not in between, (
        "the BODY_ROLE guard closes before the tally call — the call is unguarded")


# ---------------------------------------------------------------- end-to-end

def _fixture(tmp_path, goal):
    p = tmp_path / "asp.jsonl"
    p.write_text(json.dumps({"id": "asp-t", "goals": [goal]}) + "\n", encoding="utf-8")
    return p


def _run(src_file, gid, outcome, origin=None):
    env = dict(os.environ, GID=gid, SF=str(src_file), OUTCOME=outcome, NOW=NOW,
               STORAGE_BACKEND="local")
    if origin:
        env["OUTCOME_ORIGIN"] = origin
    return subprocess.run([sys.executable, str(SCRIPTS / "recurring_tally.py")],
                          capture_output=True, text=True, env=env)


def test_cli_emits_tab_separated_pairs_for_a_real_goal(tmp_path):
    r = _run(_fixture(tmp_path, _goal()), "g-test-01", "deep")
    assert r.returncode == 0, r.stderr
    got = dict(l.split("\t") for l in r.stdout.strip().splitlines())
    assert got["substantive_runs"] == "11"
    assert got["substantive_hits"] == "5"          # default origin is genuine
    assert got["last_substantive_at"] == NOW


def test_cli_refuses_a_non_recurring_goal_and_emits_nothing(tmp_path):
    r = _run(_fixture(tmp_path, _goal(recurring=False)), "g-test-01", "deep")
    assert r.returncode == 1
    assert r.stdout.strip() == ""


def test_cli_refuses_a_missing_goal_and_emits_nothing(tmp_path):
    r = _run(_fixture(tmp_path, _goal()), "g-nope-99", "deep")
    assert r.returncode == 1
    assert r.stdout.strip() == ""


def test_cli_survives_a_malformed_line_without_losing_later_records(tmp_path):
    p = tmp_path / "asp.jsonl"
    p.write_text("{not json\n" + json.dumps({"id": "asp-t", "goals": [_goal()]}) + "\n",
                 encoding="utf-8")
    r = _run(p, "g-test-01", "routine")
    assert r.returncode == 0, r.stderr
    assert "substantive_runs\t11" in r.stdout


def test_read_goal_returns_none_for_an_unreadable_store(tmp_path):
    assert read_goal(str(tmp_path / "absent.jsonl"), "g-test-01") is None


def test_recurring_close_declares_tally_ownership_around_its_verify_call():
    """The OTHER half of the double-count defense.

    BODY_ROLE alone does not separate the two writers: guard-1591 instructs
    agents to close recurring goals via recurring-close.sh, so a worker Body
    following that instruction arrives inside do_verify with BODY_ROLE=worker
    (the bash hook exports it on every call). recurring-close.sh writes the
    same five counters after its phases, so without this declaration that
    worker would advance every counter TWICE.
    """
    src = (SCRIPTS / "recurring-close.sh").read_text(encoding="utf-8")
    call = 'bash "$SCRIPT_DIR/iteration-close.sh" "$@"'
    assert src.count(call) == 1, "run_phase's iteration-close call moved or multiplied"
    idx = src.index(call)
    line_start = src.rindex("\n", 0, idx) + 1
    assert 'RECURRING_TALLY_OWNER="recurring-close"' in src[line_start:idx], (
        "recurring-close.sh no longer declares tally ownership on the call that "
        "reaches do_verify — a worker closing through it would double-count")


def test_do_verify_defers_to_a_declared_tally_owner():
    """The condition must test BOTH halves, not just the role."""
    src = (SCRIPTS / "iteration-close.sh").read_text(encoding="utf-8")
    marker = "g-115-6768: WORKER-ONLY recurring-tally advance"
    call = 'python3 "$SCRIPT_DIR/recurring_tally.py"'
    window = src[src.index(marker):src.index(call)]
    assert '-z "${RECURRING_TALLY_OWNER:-}"' in window, (
        "the tally branch no longer defers to a declared owner")
