"""test_pending_deploys_gate_budget.py — .

THE DEFECT THIS PINS. pending-deploys-gate.sh runs inside iteration-close.sh,
which runs inside a foreground Bash tool call bounded at 120s. The gate derived
its hard subprocess kill as TIMEOUT_MINS*60+30 with TIMEOUT_MINS=3, i.e. 210s.
210 > 120, so ANY close with a pending deploy was deterministically killed at the
bound (exit 143) — and killed HALF-APPLIED: lastAchievedAt advanced, the claim was
released, loop_state counters bumped and the commit landed, while outcome_note
still held the previous run's text and no health row was written. Every cheap
signal reads "closed", so a retry double-counts. Measured twice in one iteration
(echo, cc-03, 2026-08-11).

TWO INDEPENDENT BOUNDS, AND A PIN ON ONE PROVES NOTHING ABOUT THE OTHER:
  (1) PER-ENTRY  — the sp_timeout arithmetic must fit inside the foreground bound.
  (2) LOOP TOTAL — the gate calls resolve ONCE PER ENTRY, so the worst case is
      N * sp_timeout, unbounded in N. Fixing (1) alone still lets two pending
      deploys blow the same bound.

WHAT MAKES THESE PINS NON-VACUOUS. test_pre_fix_value_would_breach_the_bound
asserts the PRE-FIX constant FAILS the same assertion the post-fix one passes; and
test_budget_is_what_defers is paired with a positive control on a large budget, so
a deferral that happened for any other reason would fail the pair. A pin that
cannot fail against the defect it names is not a pin.

The ordering invariant (guard-1737) is pinned separately: TIMEOUT_MINS drives BOTH
deploy-verify's own deadline AND the hard kill, and the +30 gap exists so
deploy-verify always times out FIRST and emits its considered verdict. Invert it
and slow-CI probes report "invocation error" instead — a changed CAUSE with
nothing going red.
"""
from __future__ import annotations

import contextlib
import re
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from test_pending_deploys_gate import (  # noqa: E402  (reuse the hermetic harness)
    GATE,
    PROJECT_TMP,
    _entry,
    _load_store,
    _run_gate,
    _seed,
    _setup_repo,
    _summary,
)


@contextlib.contextmanager
def _repo():
    """The sibling suite's temp-mind-repo idiom, factored so each test reads as
    one statement. Same PROJECT_TMP root, so cleanup behaviour is unchanged."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        yield _setup_repo(Path(td))

# The Bash tool's foreground bound. External and fixed — deliberately NOT derived
# from anything in the script, so a regression that moves the script's own
# constants cannot move the threshold with them (the drifting-threshold trap).
FOREGROUND_BOUND_SECS = 120

GATE_TEXT = GATE.read_text(encoding="utf-8")


def _default_timeout_mins() -> int:
    """The TIMEOUT_MINS default as the script actually sets it."""
    m = re.search(r"^TIMEOUT_MINS=(\d+)\s*$", GATE_TEXT, re.M)
    assert m, "could not find the TIMEOUT_MINS default assignment in the gate"
    return int(m.group(1))


def _sp_timeout(timeout_mins: int) -> int:
    """Mirror the script's derivation, read from the script rather than retyped."""
    m = re.search(r"sp_timeout=\$\(\(\s*TIMEOUT_MINS\s*\*\s*60\s*\+\s*(\d+)\s*\)\)",
                  GATE_TEXT)
    assert m, "sp_timeout derivation not found or changed shape — re-read the gate"
    return timeout_mins * 60 + int(m.group(1))


# --- (1) the per-entry bound ---------------------------------------------

def test_default_subprocess_timeout_fits_inside_the_foreground_bound():
    sp = _sp_timeout(_default_timeout_mins())
    assert sp < FOREGROUND_BOUND_SECS, (
        "sp_timeout %ds >= the %ds foreground Bash bound: every close with a "
        "pending deploy is killed half-applied" % (sp, FOREGROUND_BOUND_SECS))


def test_pre_fix_value_would_breach_the_bound():
    """THE DISCRIMINATOR. TIMEOUT_MINS=3 was the shipped value; it must FAIL the
    assertion above, or that assertion is not testing anything."""
    assert _sp_timeout(3) > FOREGROUND_BOUND_SECS, (
        "the pre-fix constant must breach the bound, else the pin proves nothing")


def test_ordering_invariant_holds_guard_1737():
    """deploy-verify's own deadline must fire BEFORE the hard kill, so the
    reported cause stays deploy-verify's considered verdict."""
    tm = _default_timeout_mins()
    assert _sp_timeout(tm) > tm * 60, (
        "hard kill must exceed deploy-verify's own deadline (guard-1737)")


def test_timeout_mins_stays_a_whole_minute():
    """deploy-verify computes `deadline=$(( now + TIMEOUT_MINS * 60 ))` in bash
    INTEGER arithmetic, so a fractional value is not merely imprecise — it is a
    syntax error at the far end. This is why the two limits cannot be scaled
    together, and why the budget defers whole entries instead of clamping."""
    assert _default_timeout_mins() >= 1


# --- (2) the loop total ---------------------------------------------------

def test_budget_defers_the_second_entry_and_keeps_it():
    """Two entries, a budget too small to admit a second probe. The first entry
    always runs; the second must be DEFERRED, not dropped."""
    with _repo() as repo:
        _seed(repo, [_entry(sha="a" * 40), _entry(sha="b" * 40)])
        r = _run_gate(repo, MIND_PD_GATE_BUDGET_SECS="1",
                      FAKE_GH_CONCLUSION="success")
        s = _summary(r)
        assert s["budget_skipped"] == 1, s
        assert s["checked"] == 1, "deferred entry counted as checked: %s" % s
        assert s["not_clean"] is True, "deferral must keep closure not-clean: %s" % s
        # The deferred obligation still exists — deferral reuses the unverified
        # fail-safe, it does not loosen the obligation.
        assert len(_load_store(repo)) >= 1, "deferred entry dropped from the store"


def test_budget_is_what_defers():
    """POSITIVE CONTROL for the test above. Same two entries, same stubs, only the
    budget changes — so a deferral caused by anything else fails this pair."""
    with _repo() as repo:
        _seed(repo, [_entry(sha="a" * 40), _entry(sha="b" * 40)])
        r = _run_gate(repo, MIND_PD_GATE_BUDGET_SECS="100000",
                      FAKE_GH_CONCLUSION="success")
        s = _summary(r)
        assert s["budget_skipped"] == 0, s
        assert s["checked"] == 2, "both entries must be probed: %s" % s


def test_first_entry_always_runs_regardless_of_budget():
    """A budget smaller than a single probe must NOT verify nothing forever — that
    failure looks exactly like a clean gate, which is worse than overrunning once."""
    with _repo() as repo:
        _seed(repo, [_entry(sha="c" * 40)])
        r = _run_gate(repo, MIND_PD_GATE_BUDGET_SECS="0",
                      FAKE_GH_CONCLUSION="success")
        s = _summary(r)
        assert s["checked"] == 1, "the first entry must always be probed: %s" % s
        assert s["budget_skipped"] == 0, s


def test_summary_carries_budget_key_on_the_clean_path():
    """The key must be present even when nothing is pending, so consumers can read
    it unconditionally."""
    with _repo() as repo:
        _seed(repo, [])
        s = _summary(_run_gate(repo))
        assert "budget_skipped" in s, s
        assert s["budget_skipped"] == 0, s
