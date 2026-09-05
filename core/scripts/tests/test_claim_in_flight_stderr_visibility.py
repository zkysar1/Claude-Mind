"""The claim's in_flight stamp must not discard the helper's stderr — .

WHAT BROKE: aspirations-claim.sh invoked team-state-in-flight.sh with
`>/dev/null 2>&1 || echo "WARN: in_flight stamp failed"`. Two independent
reasons that WARN could not fire on the case that matters:

  (a) EVERY non-reducer path of team-state-in-flight.sh ends `exit 0` — the
      reducer row is reducer-owned and a skip is not a failure — so on a worker
      Body the `||` branch is unreachable BY DESIGN; and
  (b) `2>&1` discarded the helper's own account of what it did.

Measured live 2026-09-03 (alpha worker Body, cc-13) at the exact production arg
shape: rc=0, stdout 0 bytes, stderr 254 bytes. So the claim held ZERO bits of
information about this leg on every worker claim — guard-114's rc=0-refusal
case, where checking the exit code cannot help and only stderr carries signal.

Three messages were silenced, and only one is routine:
    "SKIP stamp: non-reducer body ... in_flight is reducer-owned"   (routine)
    "body row written: <agent>.in_flight_bodies.<sid> -> <goal>"    (the
        worker's only cross-Body visibility signal landing)
    "WARN: body row write failed for <agent>/<sid>"                 (that write
        FAILING — fail-open inside fail-open, so a worker's whole cross-Body
        visibility could vanish with no signal on any channel)

WHAT THIS PINS is the redirect SHAPE at that one call site. It is a source-level
assertion, in the style this script's own suite already uses
(test_claim_multiunit_advisory.py asserts advisory lines end `>&2`), and it is
deliberately paired with a self-control: an extractor that silently found
nothing would let every assertion below pass vacuously (guard-3474).

NOT PINNED, stated rather than blurred: that a human or a backgrounded caller
actually READS the stderr. guard-772 is explicit that a stderr-only warning is
invisible inside a backgrounded Bash call; making this leg durable is a
different change with a different blast radius, and is not in this goal's scope.
"""

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
CLAIM = REPO / "core" / "scripts" / "aspirations-claim.sh"
ANCHOR = 'scripts/team-state-in-flight.sh"'


def _stamp_invocation() -> str:
    """The in_flight stamp command, from its anchor line through the `||` arm.

    Returns the joined block. Raises if the anchor is absent, so a renamed or
    relocated call site fails LOUDLY here instead of silently emptying the
    block and passing every assertion below.
    """
    lines = CLAIM.read_text(encoding="utf-8").splitlines()
    starts = [i for i, ln in enumerate(lines) if ANCHOR in ln and not ln.lstrip().startswith("#")]
    assert len(starts) == 1, (
        f"expected exactly 1 non-comment invocation of {ANCHOR} in "
        f"{CLAIM.name}, found {len(starts)}: {starts}"
    )
    i = starts[0]
    block = [lines[i]]
    # A backslash-continued command: consume while the PREVIOUS line continues.
    while block[-1].rstrip().endswith("\\") and i + 1 < len(lines):
        i += 1
        block.append(lines[i])
    return "\n".join(block)


def test_the_extractor_actually_finds_the_block():
    """Self-control: every assertion below is vacuous if this one is wrong."""
    block = _stamp_invocation()
    assert ANCHOR in block
    assert "--goal-id" in block, f"block does not look like the stamp call:\n{block}"
    assert len(block.splitlines()) >= 3, f"suspiciously short block:\n{block}"


def discards_stderr(block: str) -> bool:
    """THE predicate. Shared by the real assertion and its reject-control below,
    so the control cannot drift into testing a different rule than the one that
    actually guards the file (guard-3474)."""
    return "2>&1" in block or "2>/dev/null" in block


def test_stamp_does_not_discard_the_helpers_stderr():
    """The regression pin: re-adding 2>&1 reinstates the  defect."""
    block = _stamp_invocation()
    assert not discards_stderr(block), (
        "aspirations-claim.sh discards team-state-in-flight.sh's stderr again.\n"
        "Every non-reducer path of that helper exits 0, so the `||` WARN arm is\n"
        "unreachable on a worker Body and stderr is the ONLY channel carrying\n"
        "the outcome (measured: rc=0, stdout 0 bytes, stderr 254 bytes).\n"
        "Silencing it hides a FAILED body-row write, which the three readers\n"
        "named in the block comment cannot distinguish from an idle partner.\n"
        f"block:\n{block}"
    )


def test_stamp_still_drops_stdout_and_stays_fail_open():
    """The two properties the fix must NOT change."""
    block = _stamp_invocation()
    assert ">/dev/null" in block, (
        "stdout should still be dropped — the helper's success line is noise on "
        f"a successful claim.\nblock:\n{block}"
    )
    assert "|| echo" in block and "in_flight stamp failed" in block, (
        "the block must retain its `||` fail-open arm; a stamp failure must "
        f"never fail a claim that already committed in the daemon.\nblock:\n{block}"
    )


@pytest.mark.parametrize("bad", [">/dev/null 2>&1", "2>&1 >/dev/null", "2>/dev/null"])
def test_the_predicate_can_still_reject(bad):
    """guard-3474: a predicate that can no longer reject anything passes
    forever. Feed the REAL predicate the regression it exists to catch —
    reusing the live block so this control tracks the real call site."""
    assert discards_stderr(_stamp_invocation() + " " + bad)


def test_the_predicate_accepts_the_current_shape():
    """Both directions: the control above must not pass by rejecting everything."""
    assert not discards_stderr(_stamp_invocation())
