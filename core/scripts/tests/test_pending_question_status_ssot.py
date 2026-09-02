"""test_pending_question_status_ssot.py ()

Pins the pending-question status vocabulary so the copies cannot drift apart
again. That drift is the whole reason this goal exists: two scripts defined
`terminal` differently, nothing named or tested the difference, and six agents
across eleven days each found it, correctly diagnosed it, and filed it — nine
pending goals for one defect. A reader who cannot tell "deliberate asymmetry"
from "bug" will re-file, forever.

WHAT THESE TESTS ENCODE, and it is NOT "the two sets must be equal":

  CLOSED_STATUSES  ⊃  SWEEP_SETTLED,  difference == TRANSITION_PENDING

`answered` is CLOSED (the asker is not owed anything) and NOT SETTLED (it still
owes the --apply-cleanup canonicalisation to `resolved`). Making them equal —
the obvious fix, and the one this goal's own 2026-07 description recommended —
deletes the executor g-115-3753/g-115-5025 built. Measured 2026-08-31: it turns
three tests in test_pending_questions_sweep.py red. Hence
test_answered_is_closed_but_NOT_settled, which fails in BOTH directions.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
MODULE = SCRIPTS / "_pending_question_status.py"


def _mod():
    spec = importlib.util.spec_from_file_location("_pq_status_ssot", MODULE)
    m = importlib.util.module_from_spec(spec)
    sys.modules["_pq_status_ssot"] = m
    spec.loader.exec_module(m)
    return m


# ── the relationship between the two sets ──────────────────────────────────

def test_closed_is_a_strict_superset_of_settled():
    m = _mod()
    assert m.SWEEP_SETTLED < m.CLOSED_STATUSES


def test_the_difference_is_EXACTLY_the_transition_backlog():
    """If this fails, someone changed one set without the other."""
    m = _mod()
    assert m.CLOSED_STATUSES - m.SWEEP_SETTLED == m.TRANSITION_PENDING


def test_answered_is_closed_but_NOT_settled():
    """THE asymmetry. Fails in both directions, deliberately.

    Collapsing it (adding `answered` to SWEEP_SETTLED) makes
    `_h_answered_not_cleaned` unreachable and makes --apply-cleanup skip its own
    population. Removing `answered` from CLOSED_STATUSES re-creates the
    blocked-signal defect where an answered question never discharges.
    """
    m = _mod()
    assert m.is_closed("answered")
    assert not m.is_settled("answered")
    assert m.is_closed("agent_answered")
    assert not m.is_settled("agent_answered")


def test_the_closer_writes_a_status_that_is_closed_but_unsettled():
    """Pins the lifecycle: what close.py emits is exactly the backlog state."""
    m = _mod()
    assert m.CLOSER_WRITES in m.TRANSITION_PENDING
    assert m.CANONICAL_SETTLED in m.SWEEP_SETTLED


def test_retired_is_in_BOTH_sets():
    """: `retired` was terminal in NEITHER copy.

    Confirmed 2026-08-03 on foxtrot's pq-fox-wsl-relay-restart, which carries
    status=retired WITH a resolved_at and was counted non-terminal by both
    scripts — so it would surface as an open question forever. It is not part of
    the transition backlog: a retired question is abandoned, not awaiting
    canonicalisation.
    """
    m = _mod()
    assert m.is_closed("retired")
    assert m.is_settled("retired")
    assert "retired" not in m.TRANSITION_PENDING


def test_pending_is_neither():
    m = _mod()
    assert not m.is_closed("pending")
    assert not m.is_settled("pending")


# ── the third set: what discharges a BLOCKER ───────────────────────────────

def test_retired_is_closed_but_does_NOT_discharge_a_blocker():
    """The one status where is_closed and discharges_a_blocker disagree.

    `retired` means the question was WITHDRAWN. It is closed (stop asking) and
    settled (no transition owed), but it did not ANSWER anything — so a goal
    blocked on it is not resolved; its clearing path died and someone needs to
    notice. human-blocked-defer-join.py:83-87 keeps the same split, calling the
    two "opposite actions".
    """
    m = _mod()
    assert m.is_closed("retired")
    assert m.is_settled("retired")
    assert not m.discharges_a_blocker("retired")


def test_discharges_is_closed_minus_exactly_retired():
    m = _mod()
    assert m.CLOSED_STATUSES - m.DISCHARGES_A_BLOCKER == {"retired"}


def test_answered_discharges_a_blocker():
    """The original defect, at the vocabulary level: the closer writes
    `answered`, so a blocker citing an answered question must discharge."""
    m = _mod()
    assert m.discharges_a_blocker("answered")
    assert m.discharges_a_blocker("agent_answered")
    assert m.discharges_a_blocker("resolved")


def test_the_blocked_signal_reader_uses_the_discharge_predicate():
    """Wiring: neither neighbour is correct here, so pin which one is used."""
    src = (SCRIPTS / "blocked-signal-resolution-check.py").read_text(encoding="utf-8")
    assert "discharges_a_blocker as pq_discharges" in src
    assert "pq_discharges(status)" in src


# ── the predicates are total (rb-1915) ─────────────────────────────────────

@pytest.mark.parametrize("bad", [None, "", "   ", "nonsense"])
def test_predicates_never_raise_and_default_to_open(bad):
    """An unknown status is NOT closed — the fail-safe direction, so an entry
    nobody understands stays visible rather than being silently finished."""
    m = _mod()
    assert m.is_closed(bad) is False
    assert m.is_settled(bad) is False


@pytest.mark.parametrize("messy", ["  ANSWERED ", "Resolved", "ReTiReD"])
def test_predicates_normalise_case_and_whitespace(messy):
    m = _mod()
    assert m.is_closed(messy)


# ── the wiring: each consumer imports the RIGHT set ────────────────────────

def test_the_sweep_imports_the_SETTLED_set_not_the_closed_one():
    """The sweep's pipeline must not treat the transition backlog as done."""
    src = (SCRIPTS / "pending-questions-sweep.py").read_text(encoding="utf-8")
    assert "from _pending_question_status import SWEEP_SETTLED as TERMINAL_STATUSES" in src
    assert "CLOSED_STATUSES" not in src, (
        "the sweep must not import the closer's wider set — that is the "
        "regression this goal measured")


def test_the_closer_imports_the_CLOSED_set():
    src = (SCRIPTS / "pending_questions_close.py").read_text(encoding="utf-8")
    assert "from _pending_question_status import CLOSED_STATUSES as TERMINAL" in src


def test_no_consumer_re_inlines_a_literal_terminal_set():
    """Drift prevention. The defect was two hand-written literals; a third would
    restart the cycle, and the cycle cost nine goals from six agents."""
    for name in ("pending-questions-sweep.py", "pending_questions_close.py"):
        src = (SCRIPTS / name).read_text(encoding="utf-8")
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # comments quote the OLD literals on purpose
            assert not (
                stripped.startswith(("TERMINAL =", "TERMINAL_STATUSES ="))
                and "{" in stripped
            ), f"{name} re-inlines a literal terminal set: {stripped}"


def test_the_goal_vocabulary_is_not_reachable_from_this_module():
    """A pending question is never 'completed' and a goal is never 'answered'
    (guard-1127 — decouple at the consumer, never widen a shared value)."""
    m = _mod()
    for goal_only in ("completed", "archived", "skipped", "expired"):
        assert goal_only not in m.CLOSED_STATUSES, goal_only
