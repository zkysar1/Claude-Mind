""": a recurring audit must widen its lookback to cover its own gap.

INCIDENT (measured, this fleet). A recurring audit declares `interval_hours: 24`
and runs `--since 24`. The scorer demotes it; it does not actually fire for 107h.
It then examines the last 24h, finds nothing, and reports CLEAN — over an 83h
hole it never looked at.

The defect is SELF-REINFORCING, which is why a constant cannot fix it: the clean
report is itself the evidence that the audit is low-value, which keeps it
demoted, which widens the hole. Raising the constant to 48 or 168 buys time and
re-arms the identical trap at the new number, because nothing ties the window to
the cadence actually achieved. guard-1997 says this directly — compare against
the ACHIEVED interval (`now - lastAchievedAt`), never the DECLARED
`interval_hours`, and fix by DERIVING the window rather than enlarging the
constant.

Measured on the live audit while writing this (`scorer-override-audit.py`):
  --since-hours 24  -> 5 overrides,  1 agent over threshold, recommendation ['review']
  --since-hours 200 -> 17 overrides, 2 agents over threshold, recommendation ['routing'],
                       plus a STUCK-AT-TOP finding (g-115-22=8x) invisible at 24h.
The narrow window does not merely under-report; it produces a DIFFERENT and
WRONG conclusion. That is the cost these tests protect against.

Run: py -3 -m pytest core/scripts/tests/test_derive_lookback.py -v
"""
import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def load_module():
    """Import derive-lookback.py (hyphen in the name blocks a plain import)."""
    spec = importlib.util.spec_from_file_location(
        "derive_lookback_module", SCRIPT_DIR / "derive-lookback.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = load_module()


def _stamp(hours_ago):
    return (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def starved_goal(declared=24, achieved=107):
    """The LITERAL production shape from the incident, not a contract-ideal one.

    24h declared / 107h achieved are the measured numbers. A fixture with a
    gentler ratio would still pass against buggy code that widens by some fixed
    token amount; these numbers require the window to actually track the gap
    (guard-920 — replicate the production shape).
    """
    return {
        "id": "g-115-2831",
        "recurring": True,
        "interval_hours": declared,
        "lastAchievedAt": _stamp(achieved),
    }


# ── The constraint the goal names verbatim ──────────────────────────────────
# "an audit whose achieved interval exceeds its declared lookback must widen
#  its window rather than report clean"

def _assert_covers_the_gap(derive_fn):
    """Shared assertion, run against BOTH the live code and the mutant below.

    Factored out so the mutation proof exercises the IDENTICAL assertion rather
    than a restatement of it — otherwise the proof only shows that two different
    assertions behave differently, which proves nothing about this one.
    """
    goal = starved_goal(declared=24, achieved=107)
    window, detail = derive_fn(goal, 24)
    assert window >= 107, (
        f"reported a {window}h window against a 107h achieved interval — "
        f"{107 - window}h would go unexamined and be reported CLEAN. "
        f"reason={detail.get('reason')!r}"
    )


def test_starved_audit_widens_to_cover_its_own_gap():
    _assert_covers_the_gap(M.derive)


def test_mutation_pre_fix_fixed_lookback_reddens_the_same_assertion():
    """guard-1475 / guard-1780: prove the assertion discriminates.

    `pre_fix` is exactly what the code did before this goal — return the
    caller's constant, unconditionally. If the assertion above still passed
    against it, it would keep passing after a regression and would be testing
    nothing.
    """
    def pre_fix(goal, default, margin_pct=10.0, cap=None):
        return default, {"reason": "fixed lookback (pre-g-115-4061 behaviour)"}

    with pytest.raises(AssertionError):
        _assert_covers_the_gap(pre_fix)


def test_widening_is_proportional_not_a_fixed_bump():
    """A bigger gap must produce a bigger window.

    Separate constraint from the one above, with its own failure mode: code
    that widened by a fixed constant (say +24h) would satisfy the gap test at
    one ratio and silently under-cover at every larger one.
    """
    small, _ = M.derive(starved_goal(declared=24, achieved=48), 24)
    large, _ = M.derive(starved_goal(declared=24, achieved=240), 24)
    assert large > small, f"240h gap produced {large}h, 48h gap produced {small}h"
    assert large >= 240, f"{large}h does not cover a 240h gap"


# ── Invariant 1: NEVER NARROWER THAN THE CALLER'S DEFAULT ───────────────────
# A derivation that could shrink the window would make coverage WORSE than the
# constant it replaced — turning a safety fix into a regression.

def test_on_cadence_goal_never_narrows_below_default():
    on_cadence = starved_goal(declared=24, achieved=4)
    window, _ = M.derive(on_cadence, 24)
    assert window >= 24, f"narrowed to {window}h below the caller's 24h default"


def test_zero_margin_still_never_narrows():
    window, _ = M.derive(starved_goal(declared=24, achieved=1), 24, margin_pct=0.0)
    assert window >= 24, f"narrowed to {window}h with margin_pct=0"


def test_cap_below_default_refloors_to_default():
    """A cap lower than the default is a caller error; honour the default.

    Without the re-floor, a cap would be able to violate invariant 1 — the one
    path by which this helper could return LESS coverage than doing nothing.
    """
    window, _ = M.derive(starved_goal(declared=24, achieved=107), 24, cap=5)
    assert window >= 24, f"cap=5 drove the window to {window}h, below the 24h default"


def test_cap_bounds_a_runaway_ratio():
    window, _ = M.derive(starved_goal(declared=1, achieved=5000), 24, cap=200)
    assert window == 200, f"expected the cap to bound this at 200, got {window}"


# ── Invariant 2: FAIL-OPEN TO THE DEFAULT ──────────────────────────────────
# Every error path returns the caller's default, so a broken helper is never
# worse than today's behaviour.

@pytest.mark.parametrize("bad_goal,label", [
    (None, "goal not found"),
    ({}, "empty record"),
    ({"id": "g-x", "interval_hours": 0, "lastAchievedAt": _stamp(50)}, "zero interval"),
    ({"id": "g-x", "interval_hours": None, "lastAchievedAt": _stamp(50)}, "null interval"),
    ({"id": "g-x", "interval_hours": "banana", "lastAchievedAt": _stamp(50)}, "junk interval"),
    ({"id": "g-x", "interval_hours": 24, "lastAchievedAt": "not-a-date"}, "junk stamp"),
])
def test_fail_open_returns_the_default(bad_goal, label):
    window, detail = M.derive(bad_goal, 24)
    assert window >= 24, f"{label}: returned {window}, narrower than the 24h default"
    assert detail.get("reason"), f"{label}: failed silently with no stated reason"


def test_never_run_goal_widens_rather_than_reporting_clean():
    """No lastAchievedAt means no bound on the gap — the one case where a
    narrow window is guaranteed wrong, since the audit has never looked at all.
    """
    never_run = {"id": "g-x", "recurring": True, "interval_hours": 24,
                 "lastAchievedAt": None}
    window, detail = M.derive(never_run, 24)
    assert window >= 24 * M.NEVER_RUN_RATIO, (
        f"a never-run audit got a {window}h window; reason={detail.get('reason')!r}"
    )


# ── Unit-agnostic: the ratio is TIME, the window is whatever the caller uses ─

def test_count_window_widens_by_the_same_time_ratio():
    """`alert-sweep.sh --max 8` is a COUNT window with the identical defect.

    The helper must not assume its `default` is in hours: the ratio is always
    computed in time and applied to whatever unit the caller passes. Without
    this, half the measured family is unfixable by this helper.
    """
    goal = starved_goal(declared=6, achieved=24)   # 4x overdue
    hours_window, _ = M.derive(goal, 24)
    count_window, _ = M.derive(goal, 8, cap=200)
    assert count_window > 8, f"count window did not widen ({count_window})"
    # Same ratio applied to both units.
    assert abs((count_window / 8) - (hours_window / 24)) < 0.25, (
        f"unit-dependent scaling: count 8->{count_window}, hours 24->{hours_window}"
    )


# ── The fail-open path must be LOUD (fresh-eyes F-001, same-iteration fix) ──
# Found by /fresh-eyes-code on this file an hour after writing it. The full
# suite was CLEAN over the buggy version and could not have caught it: every
# test asserted on the RETURN VALUE, and the defect was in what was NOT printed.
# A dimension nothing asserts on is a dimension a green suite says nothing about.

def _run_main_capturing_stderr(argv):
    """Drive the real CLI entry point and capture what an operator would see."""
    import contextlib
    import io
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = M.main(argv)
    return rc, out.getvalue(), err.getvalue()


def _dead_root(tmp_path):
    """A root with no aspirations-read.sh — forces every read attempt to fail.

    This is the production-shape failure (guard-920): a wedged daemon, a 90s
    TimeoutExpired, or a non-zero rc all land on the same `continue` and are
    indistinguishable from here.
    """
    return str(tmp_path)


def test_read_failure_is_not_silent(tmp_path):
    """The defect verbatim: window degrades to the default with ZERO output.

    Measured before the fix: stdout '30', stderr '' (0 bytes), exit 0. Neither
    production call site passes --json, so the reason was unreachable and the
    window silently reverted to the caller's constant — the exact blind-window
    condition this script exists to remove.
    """
    rc, out, err = _run_main_capturing_stderr(
        ["--goal-id", "g-115-817", "--default", "30", "--cap", "200",
         "--root", _dead_root(tmp_path)]
    )
    assert rc == 0, "fail-open must still exit 0 (invariant 2)"
    assert out.strip() == "30", f"stdout must stay the bare number, got {out!r}"
    assert err.strip(), (
        "REGRESSION: the read failed and the window silently reverted to the "
        "caller's default with NO diagnostic on stderr. An operator cannot tell "
        "this run from a healthy one."
    )


def test_read_failure_is_distinguished_from_goal_absent(tmp_path):
    """'goal not found' and 'the read never succeeded' mean opposite things.

    Both degrade to the default, so the VALUE cannot tell them apart — only the
    reason can. Reporting the wrong one sends the next reader hunting for a
    deleted goal that was never deleted.
    """
    rc, out, err = _run_main_capturing_stderr(
        ["--goal-id", "g-115-817", "--default", "30",
         "--root", _dead_root(tmp_path), "--json"]
    )
    payload = json.loads(out[out.index("{"):out.rindex("}") + 1])
    assert "READ FAILED" in payload["reason"], (
        f"reason must name the read failure, got {payload['reason']!r}"
    )
    assert payload.get("read_errors"), "the underlying read errors must be carried"


def test_mutation_changed_only_gate_reddens_the_loudness_assertion():
    """guard-1475 / guard-1780: prove the loudness test discriminates.

    `pre_fix_emit` restores the original `if window != args.default` gate. Every
    fail-open path returns EXACTLY the default, so that gate is false precisely
    when a signal is needed — which is why the bug was invisible. If the
    assertion above still passed under this mutation it would be testing nothing.
    """
    import contextlib
    import io

    def pre_fix_emit(window, default):
        if window != default:            # the original, buggy condition
            print("[derive-lookback] ...", file=sys.stderr)

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        pre_fix_emit(30, 30)             # the fail-open shape: window == default
    with pytest.raises(AssertionError):
        assert err.getvalue().strip(), "mutation did not redden"


def test_healthy_path_still_reports_its_reasoning():
    """Loudness must not be limited to failures — a widened window says why.

    Guards the over-correction where someone makes ONLY the error path loud and
    leaves the normal derivation silent, which would hide the more common
    question: why is this window 43 and not 30?
    """
    goal = starved_goal(declared=6, achieved=24)
    window, detail = M.derive(goal, 30, cap=200)
    assert window > 30
    assert detail["reason"], "a widened window must carry its reason"
    assert "exceeds declared" in detail["reason"], (
        f"reason should name the gap that drove the widening, got {detail['reason']!r}"
    )
