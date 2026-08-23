""" -- pin the worker-only pending-deploys ALL-sweep in do_verify.

WHAT THIS FILE IS AND IS NOT. This is a STRUCTURAL pin on the wiring, not a
behavioural test: `do_verify` writes goal status, touches the board and the
stores, and cannot be exec'd hermetically for the price this assertion is
worth. The BEHAVIOUR the wiring exists to produce is tested for EFFECT against
the real gate in `test_pending_deploys_gate.py` (the three g-115-6932 tests at
the end: the empty-goal_id entry is invisible to the goal-scoped call, visible
to the un-scoped one, and a failed deploy on that path files a HIGH Unblock).
Read the two files as a pair -- this one says "a worker reaches the sweep",
that one says "the sweep does the thing".

A structural test can go vacuous silently (a renamed function, a moved call,
and every regex matches nothing while the file stays green), so every
assertion here is paired with a positive control that fails if the derivation
stopped seeing what it thinks it sees -- the discipline
`test_iteration_close_override_forwarding.py` established.

The defect: `do_verify`'s existing gate call is goal-scoped (`--goal
$GOAL_ID`), and `pending-deploys.py list --goal-id` filters by EXACT equality,
so it never matches an accumulated entry (goal_id ""). The un-scoped sweep
that does reach those lives in `do_productivity_check`, which a worker SKIPS.
Both call sites were unreachable from a worker box.
"""

import re
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
ICLOSE = CORE_SCRIPTS / "iteration-close.sh"


def _do_verify_body() -> str:
    """The text of do_verify(), start line to its closing brace at column 0.

    Backslash-continuations are joined HERE rather than in `_gate_calls`, so
    every offset the tests below compute is an offset into this one string.
    Joining later produced call strings that were not substrings of the body
    being searched, and `str.index` raised instead of asserting -- a test that
    fails for a reason unrelated to what it claims to check.

    The join matters on its own terms too: the goal-scoped call wraps with a
    trailing backslash, so a naive per-line scan reads its `--goal` as absent
    and the "un-scoped" assertions below would pass for the wrong reason.
    """
    lines = ICLOSE.read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("do_verify()"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return re.sub(r"\\\n\s*", " ", "\n".join(lines[start:end]))


def _gate_calls(body: str):
    """Every pending-deploys-gate.sh invocation in `body`, as full commands."""
    return [l.strip() for l in body.splitlines()
            if "pending-deploys-gate.sh" in l and not l.strip().startswith("#")]


def _only_unscoped_call(body: str) -> str:
    """The single gate call with no --goal, asserted rather than indexed.

    A bare `[...][0]` raises IndexError when the worker branch is absent --
    which IS the regression these tests exist to catch, reported as a stack
    trace about list indexing instead of as the finding. Verified by mutation
    (worker branch deleted from a scratch copy): this reports the cause.
    """
    unscoped = [c for c in _gate_calls(body) if "--goal " not in c]
    assert len(unscoped) == 1, (
        "expected exactly one un-scoped pending-deploys-gate.sh call in "
        f"do_verify (the g-115-6932 worker sweep); found {len(unscoped)}. "
        f"All gate calls: {_gate_calls(body)}")
    return unscoped[0]


def test_do_verify_body_extraction_is_not_vacuous():
    """Control: the parse really isolated do_verify, not the whole file."""
    body = _do_verify_body()
    assert body.startswith("do_verify()"), body[:80]
    assert len(body.splitlines()) > 100, "do_verify body implausibly short"
    # It must NOT have swallowed the next function -- that would make every
    # 'is in do_verify' assertion below trivially true.
    assert "do_state_update()" not in body
    assert "do_learning_gate()" not in body


def test_both_call_shapes_are_present_and_distinguishable():
    """Control: exactly two gate calls, one WITH --goal and one WITHOUT.

    If the file ever carries only one shape, the two tests below stop meaning
    what they claim -- an all-match or a no-match regex would satisfy them.
    """
    calls = _gate_calls(_do_verify_body())
    assert len(calls) == 2, f"expected exactly 2 gate calls in do_verify: {calls}"
    scoped = [c for c in calls if "--goal " in c]
    unscoped = [c for c in calls if "--goal " not in c]
    assert len(scoped) == 1, f"the goal-scoped call is missing or duplicated: {calls}"
    assert len(unscoped) == 1, f"the un-scoped sweep is missing or duplicated: {calls}"
    # The scoped one must genuinely pass the goal id through, or "--goal " is
    # matching a comment or a flag name rather than a real argument.
    assert "$GOAL_ID" in scoped[0], scoped[0]


def test_worker_sweep_is_unscoped_and_guarded_by_body_role():
    """The sweep a worker reaches must be BOTH un-scoped and worker-only.

    Un-scoped, or it cannot see an empty goal_id (the whole defect). Worker
    only, or the reducer double-probes every entry twice per iteration --
    it already reaches the identical sweep via do_productivity_check
    (guard-2783: state the complement, do not leave it implicit).
    """
    body = _do_verify_body()
    unscoped = _only_unscoped_call(body)
    idx = body.index(unscoped)
    # Walk back to the nearest enclosing `if` and require it to test BODY_ROLE.
    preceding = body[:idx]
    guard = preceding[preceding.rindex("if "):]
    assert "BODY_ROLE" in guard and "worker" in guard, (
        f"the un-scoped sweep is not guarded by a worker BODY_ROLE test: {guard[:200]!r}")


def test_worker_sweep_is_outside_the_completed_only_gate():
    """Obligations accumulate however the unit closed, so the sweep must not
    sit inside `if [[ "$GOAL_STATUS" == "completed" ]]` -- a worker whose units
    close blocked or skipped would otherwise still never sweep."""
    body = _do_verify_body()
    calls = _gate_calls(body)
    scoped = [c for c in calls if "--goal " in c][0]
    unscoped = _only_unscoped_call(body)
    completed_gate = body.index('if [[ "$GOAL_STATUS" == "completed" ]]')

    # Control: the SCOPED call is inside that gate. If this fails, the marker
    # moved and 'after the gate' below no longer means 'outside' it.
    assert completed_gate < body.index(scoped), (
        "the goal-scoped call is expected inside the completed-only gate")
    gate_block_end = body.index("\n    fi\n", completed_gate)
    assert body.index(scoped) < gate_block_end, "scoped call escaped its gate"

    assert body.index(unscoped) > gate_block_end, (
        "the worker sweep sits inside the completed-only gate; obligations "
        "accumulate regardless of how the unit closed")
