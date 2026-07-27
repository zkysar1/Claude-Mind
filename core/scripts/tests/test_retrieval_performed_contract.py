"""test_retrieval_performed_contract.py — .

THE CONTRACT (established empirically, 4 experiments over live session files):

    `retrieval_performed` is written ONLY by iteration-close.sh's no-retrieval
    STUB, always as an explicit False. The real retrieve.sh path records
    goal_id + counts and leaves the key ABSENT. Therefore ABSENT means retrieval
    WAS performed; only an explicit False means it was not.

WHY THIS FILE EXISTS
One field drove three consumers in different directions:

  1. phase-4-26-gate.py:108 used `if not session.get("retrieval_performed")`.
     Absent is falsy, so it read every REAL retrieval as "no signal" and
     returned a vacuous pass — 100% inert since it shipped (g-115-3113). Worse,
     ARMING it without fixing the predicate would have inverted the harm:
     blocking every close that DID consult. Fixed to `is False` here.
  2. iteration-close.sh's perf computation (L1651) already used the correct
     `is False` discriminator. Its false drift-trips came from the PRODUCER gap
     below, not the field — do not "fix" it.
  3. pre-apply-consult-gate.py:203 `is not False` — correct all along, and the
     reference implementation these tests pin against.

MEASURED PRODUCER BEHAVIOUR (the table these tests encode):
    --goal <id> explicit          -> session written, key ABSENT
    no --goal, in_flight SET      -> session written (goal inferred), key ABSENT
    no --goal, in_flight UNSET    -> NOT written; the previous goal's session
                                     stays on disk (was the residual producer gap)
    iteration-close no-retrieval  -> written, key explicitly False

PRODUCER GAP: CLOSED 2026-07-26 by g-115-3199 (foxtrot). `aspirations-claim.sh`
now invokes `team-state-in-flight.sh` inside the claim itself, so in_flight is
SET at claim time and the post-claim/pre-Phase-4 window that produced row 3 no
longer occurs on the claimed-goal path.

Row 3 stays in the table because the MECHANISM still exists: the claim-time
stamp is deliberately FAIL-OPEN (a stamp failure warns but must never fail a
claim that already committed), so a goal-less consult after a failed stamp
still lands on row 3. `test_producer_gap_closed_by_claim_stamp` now pins the
closure rather than the defect — if the stamp is ever removed from claim.sh the
gap silently reopens, and that test is what catches it.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, CORE_SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(CORE_SCRIPTS))
    spec.loader.exec_module(mod)
    return mod


gate = _load("phase_4_26_gate", "phase-4-26-gate.py")


def _real_session(goal_id="g-test-001", **over):
    """A session as the REAL retrieve.sh path writes it.

    Note what is ABSENT: `retrieval_performed`. Copied from a live artifact
    (g-115-3136's own session: goal_id set, 28 tree nodes, 5 supplementary
    items, no retrieval_performed key).
    """
    d = {
        "schema_version": 2,
        "goal_id": goal_id,
        "tree_nodes_loaded": ["a", "b"],
        "supplementary_items": ["rb-001"],
        "utilization_pending": False,
        "utilization_method": "infer",
        "inference_stats": {"helpful": 2},
    }
    d.update(over)
    return d


def _stub_session(goal_id="g-test-001"):
    """A session as iteration-close.sh's no-retrieval STUB writes it."""
    return {
        "schema_version": 2,
        "goal_id": goal_id,
        "retrieval_performed": False,
        "tree_nodes_loaded": [],
        "supplementary_items": [],
        "utilization_pending": False,
        "utilization_completed_at": None,
    }


# ------------------------------------------------- the gate reads the contract


def test_real_session_is_not_dismissed_as_no_signal():
    """THE  REGRESSION GUARD.

    A real retrieval (key ABSENT) must reach the utilization logic, NOT be
    short-circuited by the retrieval_performed branch. Reverting the predicate
    to `not session.get(...)` makes this fail.
    """
    verdict, reason, _method, _n = gate._evaluate(_real_session(), "g-test-001")
    assert "no signal" not in reason and "no-retrieval stub" not in reason, (
        f"REGRESSION: a REAL retrieval was dismissed by the retrieval_performed "
        f"branch (reason={reason!r}). Absent means performed — only explicit "
        f"False is the stub (g-115-3126 contract)."
    )


def test_stub_session_is_recognised_as_nothing_to_gate():
    verdict, reason, _m, _n = gate._evaluate(_stub_session(), "g-test-001")
    assert verdict == "pass"
    assert "stub" in reason or "false" in reason.lower()


def test_explicit_true_is_treated_as_performed():
    """Legacy sessions carrying an explicit True must not regress to 'stub'."""
    _v, reason, _m, _n = gate._evaluate(
        _real_session(retrieval_performed=True), "g-test-001")
    assert "no-retrieval stub" not in reason


def test_stale_session_for_another_goal_still_fails_open():
    """Orthogonal guard — the goal_id mismatch branch must keep precedence."""
    verdict, reason, _m, _n = gate._evaluate(_real_session("g-other-999"),
                                             "g-test-001")
    assert verdict == "pass"
    assert "stale session" in reason


# ------------------------------------------------------- cross-consumer parity


def test_all_consumers_use_the_same_discriminator():
    """The three consumers must agree on what the field means.

    They are in different files and drifted apart once already; this pins the
    shared predicate so a future edit to any one of them fails loudly here.
    """
    checks = {
        "phase-4-26-gate.py": r'session\.get\("retrieval_performed"\) is False',
        "pre-apply-consult-gate.py": r'\.get\("retrieval_performed"\) is not False',
    }
    for fname, pattern in checks.items():
        src = (CORE_SCRIPTS / fname).read_text(encoding="utf-8")
        assert re.search(pattern, src), (
            f"{fname} no longer uses the `is (not) False` discriminator — the "
            f"g-115-3126 contract requires ABSENT to mean performed. A bare "
            f"truthiness check silently reverses this file's meaning."
        )
    # iteration-close.sh computes the same distinction shell-side.
    ic = (CORE_SCRIPTS / "iteration-close.sh").read_text(encoding="utf-8")
    assert "d.get('retrieval_performed') is False" in ic, (
        "iteration-close.sh's stub-detect probe lost the `is False` "
        "discriminator — perf would go false on every real retrieval."
    )


def test_no_consumer_uses_a_bare_truthiness_check():
    """The specific defect shape, banned by name across all three consumers."""
    banned = re.compile(
        r'if\s+not\s+\w+(?:\.get\(["\']retrieval_performed["\']\)|\[["\']retrieval_performed["\']\])')
    for fname in ("phase-4-26-gate.py", "pre-apply-consult-gate.py"):
        src = (CORE_SCRIPTS / fname).read_text(encoding="utf-8")
        hit = banned.search(src)
        assert not hit, (
            f"{fname}:{src[:hit.start()].count(chr(10)) + 1} reintroduced the bare "
            f"truthiness check on retrieval_performed. Absent is falsy, so this "
            f"reads every REAL retrieval as 'not performed' (g-115-3113)."
        )


# ------------------------------------------------------------ the open defect


def test_producer_gap_closed_by_claim_stamp():
    """Pins the contract table's 4th row AND its 2026-07-26 closure.

    A goal-less `retrieve.sh` resolves its goal via _infer_in_flight_goal_id(),
    which reads agent_status.<agent>.in_flight. That inference is unchanged.

    What changed: `aspirations-claim.sh` used NOT to set in_flight — only the
    honor-system `team-state-in-flight.sh` did, at Phase 4 — so a consult in the
    post-claim/pre-Phase-4 window wrote NO session and the PREVIOUS goal's file
    stayed on disk. Downstream that read as goal_id-mismatch -> stub ->
    perf=false, which is what falsely tripped the drift gate on g-115-3112 and
    g-115-3119 even though the consult really ran. g-115-3199 folded the stamp
    INTO the claim, closing that window.

    Asserted structurally in BOTH directions: if the inference stops keying on
    in_flight, OR if the claim-time stamp is removed (silently reopening the
    gap), this test fails and the contract table above gets re-measured rather
    than going stale. This file previously asserted the gap was OPEN; that
    assertion fired correctly when g-115-3199 landed, which is exactly the
    tripwire working — the table was updated instead of drifting.
    """
    src = (CORE_SCRIPTS / "retrieve.py").read_text(encoding="utf-8")
    assert "_infer_in_flight_goal_id" in src
    assert 'status.get("in_flight")' in src, (
        "retrieve.py's goal inference no longer keys on in_flight — the "
        "g-115-3126 producer-gap analysis is stale; re-measure the 4-row "
        "contract table in this file's docstring."
    )
    claim = (CORE_SCRIPTS / "aspirations-claim.sh").read_text(encoding="utf-8")
    assert "team-state-in-flight.sh" in claim, (
        "aspirations-claim.sh no longer stamps in_flight at claim time — the "
        "g-115-3199 closure has been reverted and contract-table row 3 is "
        "REOPEN: a goal-less consult in the post-claim/pre-Phase-4 window will "
        "again write no session and read downstream as perf=false."
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
