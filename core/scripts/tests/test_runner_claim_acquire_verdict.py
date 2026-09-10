"""Branch proof for the /start runner-claim acquire verdict (2026-09-10).

THE INCIDENT THIS PINS. A paying member's resident agent hit ACQUIRE_RC=1
(daemon unreachable) at /start, found no branch for it in
`.claude/skills/start/SKILL.md` — which documented ONLY `ACQUIRE_RC=4 +
reducer_only` and `ACQUIRE_RC=4 otherwise` — and halted. It sat IDLE through a
56-minute billed hour, mailed the owner the same three options twice, tripped a
burn-rate alarm at 21x baseline, and cost $1.96 for a stopped agent.

The fail-open rule for every non-4 rc DID exist, in two other places
(`runner-claim.sh`'s rc contract and `core/config/start-phase-c.md`), neither of
which is the file describing the flow the resident was executing.

TWO INVARIANTS, and the asymmetry between them is the whole lesson.

  * VERDICT — every exit code maps to exactly one action, and rc=4 is the ONLY
    refusal. The rare dangerous branch was already emphasised in prose AND
    pinned by a test (test_owncloud_backend.py); the common benign branch was
    neither. That asymmetry is what made an hour of paid idleness the
    path of least resistance.

  * WIRING — the acquire step in `start/SKILL.md` must actually account for
    non-4 exit codes. A verdict function alone would not have prevented this:
    the original defect was a correct rule that the executing file never
    mentioned, so a resolution-only suite passes perfectly over the live bug.

guard-1165: no module-level os.environ mutation, no sys.modules stubs.
"""

import re
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SCRIPTS.parents[1]
sys.path.insert(0, str(SCRIPTS))

import runner_claim_acquire as rca  # noqa: E402
from runner_claim_acquire import (  # noqa: E402
    JOIN_AS_WORKER,
    PROCEED,
    REFUSE,
    REFUSE_RC,
    decide,
    main,
)

START_SKILL = PROJECT_ROOT / ".claude" / "skills" / "start" / "SKILL.md"
VERDICT_SCRIPT = "runner-claim-acquire-verdict.sh"


# --------------------------------------------------------------------------
# Invariant 1 — the verdict
# --------------------------------------------------------------------------


def test_rc1_daemon_unreachable_proceeds():
    """THE REGRESSION THIS FILE EXISTS FOR.

    rc=1 is the measured incident code. `runner-claim.sh`'s own header says
    "1 — daemon returned an error (caller decides; the three mutating call
    sites fail open)". A refusal here is an hour of paid idleness.
    """
    verdict, reason = decide(1)
    assert verdict == PROCEED
    assert "1" in reason


def test_rc0_proceeds():
    assert decide(0)[0] == PROCEED


def test_rc4_without_reducer_only_joins_as_a_second_body():
    assert decide(REFUSE_RC)[0] == JOIN_AS_WORKER


def test_rc4_with_reducer_only_refuses():
    assert decide(REFUSE_RC, reducer_only=True)[0] == REFUSE


@pytest.mark.parametrize("rc", [2, 3, 5, 6, 127, -1, 99])
def test_every_other_nonzero_rc_fails_open(rc):
    """start-phase-c.md: "Any OTHER non-zero rc is FAIL-OPEN: log and PROCEED."

    Policy is INHERITED here, not invented — including rc=2 (bad usage), which
    fails open because that is what the existing contract says. It is surfaced
    in the reason rather than silently promoted to a halt; changing that is a
    separate decision from closing this hole.
    """
    assert decide(rc)[0] == PROCEED


@pytest.mark.parametrize("rc", [None, "", "abc", object()])
def test_unreadable_rc_fails_open_rather_than_refusing(rc):
    """An rc that will not parse is not evidence a peer holds the claim.

    Refusing on it would strand the agent for exactly the reason this module
    exists, so the unreadable case leans the same way as the transient one.
    """
    verdict, reason = decide(rc)
    assert verdict == PROCEED
    assert reason


def test_string_rc_from_shell_is_accepted():
    """Shell hands `$?` through as a string; both spellings must agree."""
    assert decide("4") == decide(4)
    assert decide("1") == decide(1)


def test_reducer_only_changes_nothing_except_the_rc4_case():
    """The flag is consumed at exactly ONE branch.

    If it ever leaked into the fail-open path it would resurrect the incident
    under a flag nobody associates with starting an agent.
    """
    for rc in (0, 1, 2, 3, 5, 99):
        assert decide(rc, reducer_only=True)[0] == decide(rc, reducer_only=False)[0]
    assert decide(4, reducer_only=True)[0] != decide(4, reducer_only=False)[0]


def test_verdict_is_always_one_of_three():
    """Callers switch on the verdict with no fallback, so a fourth value —
    or an unmapped integer falling through — must be unreachable."""
    allowed = {PROCEED, JOIN_AS_WORKER, REFUSE}
    for rc in (0, 1, 2, 3, 4, 5, 99, None, "x"):
        for flag in (True, False):
            assert decide(rc, reducer_only=flag)[0] in allowed


def test_rc4_is_the_only_refusing_code():
    """Non-vacuity control (rb-245).

    Every assertion above still passes if `decide` always returned PROCEED,
    except the two rc=4 cases. This states the discrimination directly: exactly
    one exit code refuses, and it is 4.
    """
    refusing = [
        rc for rc in range(0, 130) if decide(rc, reducer_only=True)[0] == REFUSE
    ]
    assert refusing == [REFUSE_RC]


# --------------------------------------------------------------------------
# Invariant 2 — the CLI
# --------------------------------------------------------------------------


def test_cli_prints_the_verdict_and_reason(capsys):
    assert main(["1"]) == 0
    out = capsys.readouterr()
    assert out.out.strip() == PROCEED
    assert "runner-claim-acquire" in out.err


def test_cli_honours_the_reducer_only_flag(capsys):
    assert main(["4", "--reducer-only"]) == 0
    assert capsys.readouterr().out.strip() == REFUSE
    assert main(["4"]) == 0
    assert capsys.readouterr().out.strip() == JOIN_AS_WORKER


def test_cli_with_no_rc_at_all_still_prints_a_usable_verdict(capsys):
    """A caller that forgets to pass $? must not get a crash or an empty line —
    it must get the fail-open verdict, for the same reason as above."""
    assert main([]) == 0
    assert capsys.readouterr().out.strip() == PROCEED


def test_cli_exit_status_carries_no_signal(capsys):
    """Deliberate: the verdict is the product. An exit code would be a second
    thing to interpret, which is the failure mode being removed."""
    for argv in (["0"], ["1"], ["4"], ["4", "--reducer-only"], ["garbage"]):
        assert main(argv) == 0
        capsys.readouterr()


# --------------------------------------------------------------------------
# Invariant 3 — the wiring (the load-bearing half)
# --------------------------------------------------------------------------


def _acquire_region(body):
    """The slice of start/SKILL.md that handles the acquire exit code."""
    start = body.find("runner-claim.sh acquire")
    assert start != -1, "start/SKILL.md no longer runs a runner-claim acquire"
    return body[start : start + 1600]


def test_start_skill_accounts_for_non_refusing_exit_codes():
    """THE REGRESSION GUARD.

    The original defect was NOT a wrong rule — it was a correct rule that the
    executing file never mentioned. `start/SKILL.md` documented both rc=4 cases
    and was silent on every other exit code, so an agent meeting rc=1 had no
    sanctioned action and halted.

    This asserts the acquire region says SOMETHING about the non-refusing
    codes: either by delegating to the verdict script, or by carrying the
    fail-open rule inline. Deliberately not an exact-string match — the point
    is that the branch is reachable by a reader, not that it is phrased one way.
    """
    body = START_SKILL.read_text(encoding="utf-8")
    region = _acquire_region(body)
    delegates = VERDICT_SCRIPT in region
    states_rule = re.search(r"fail[- ]open", region, re.IGNORECASE) is not None
    assert delegates or states_rule, (
        "start/SKILL.md's acquire step documents only the rc=4 branches. An "
        "agent meeting ACQUIRE_RC=1 (daemon unreachable) has no sanctioned "
        "action and will halt, idling a paying member's agent for the whole "
        f"run. Delegate to {VERDICT_SCRIPT} or state the fail-open rule inline."
    )


def test_verdict_script_exists_and_reaches_the_resolver():
    script = SCRIPTS / VERDICT_SCRIPT
    assert script.is_file()
    assert "runner_claim_acquire.py" in script.read_text(encoding="utf-8")


def test_refuse_rc_matches_the_wrapper_contract():
    """Pins the two ends together (guard-359: script/doc name drift).

    `runner-claim.sh`'s documented contract is that acquire exits 4 when
    another machine holds a live claim. If that ever changes without changing
    REFUSE_RC, this module silently starts refusing on the wrong code — or
    worse, stops refusing at all.
    """
    wrapper = (SCRIPTS / "runner-claim.sh").read_text(encoding="utf-8")
    assert re.search(
        r"4\s+—.*REFUSE.*acquire.*another machine holds a live claim",
        wrapper,
        re.IGNORECASE | re.DOTALL,
    ), "runner-claim.sh's rc=4 contract line changed; re-verify REFUSE_RC"
    assert rca.REFUSE_RC == 4
