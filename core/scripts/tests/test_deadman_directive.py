"""Tests for core/scripts/deadman-directive.sh ().

The defect: a WORKER Body had no deadman net at all, so a text-death was
permanent (measured cc-08, foxtrot, dead 68+ min). These tests pin the two
properties that make the worker net actually work, plus the safety properties
that stop it from resurrecting a Body that closed on purpose.

The load-bearing one is `test_worker_prompt_passes_the_gate`. The worker CANNOT
use the reducer's `<<autonomous-loop-dynamic>>` sentinel (it resolves to the
reducer loop, which guard-517/guard-463 forbid a worker from entering, and which
would refuse anyway on agent-state != RUNNING). It therefore arms a
natural-language prompt — and if `schedule-wakeup-gate.py` ever refused that
shape, the whole net would be silently inert. That is exactly the failure class
this goal exists to fix, so it gets a test with a positive control.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS / "deadman-directive.sh"
sys.path.insert(0, str(SCRIPTS))

from _bash_helpers import BASH  # noqa: E402
from _swakeup_predicate import is_bad_slash_prefix  # noqa: E402


def run(role, agent="alpha"):
    # guard-580/581: never a bare "bash" argv[0] (resolves to System32 WSL on
    # win32 and can hang forever), and never str(WindowsPath) (bash silently
    # strips the backslashes). BASH is resolved explicitly; .as_posix() keeps
    # the script path bash-readable on every platform.
    cmd = [BASH, SCRIPT.as_posix(), "--role", role]
    # Inherit the real environment rather than pinning a POSIX-only
    # PATH=/usr/bin:/bin + HOME=/root. Those made the test pass here and fail on
    # the fleet's Windows boxes, which is the platform half of the production
    # shape this repo keeps relearning (guard-920): a green suite on one OS is
    # not evidence. Only the two vars the script actually reads are overlaid.
    env = os.environ.copy()
    env["MIND_AGENT"] = agent
    return subprocess.run(
        cmd, capture_output=True, text=True, env=env,
        cwd=str(SCRIPTS.parent.parent),
    )


def worker_prompt():
    out = run("worker").stdout
    start = out.index("ScheduleWakeup(prompt='") + len("ScheduleWakeup(prompt='")
    return out[start:out.index("'", start)]


def test_worker_prompt_passes_the_gate():
    """The emitted worker prompt must clear schedule-wakeup-gate's predicate.

    If this reddens, the worker net is INERT — armed but refused at the gate,
    which looks identical to working from the caller's side.
    """
    assert is_bad_slash_prefix(worker_prompt()) is False


def test_gate_predicate_actually_discriminates():
    """Positive control for the test above.

    Without this, `is_bad_slash_prefix(...) is False` would still pass if the
    predicate were stubbed to return False for everything — i.e. the test would
    hold while proving nothing.
    """
    assert is_bad_slash_prefix("/aspirations loop") is True
    assert is_bad_slash_prefix("<<autonomous-loop-dynamic>>") is False


def test_worker_prompt_has_no_leading_slash():
    """The mechanical form of the gate rule, pinned independently.

    The predicate anchors on the FIRST character, matching Claude Code's slash
    resolver. A future edit that opens the prompt with a slash would be refused.
    """
    assert not worker_prompt().startswith("/")


def test_worker_never_routed_to_the_reducer_loop():
    """guard-517/guard-463: a worker must NEVER re-enter Skill(aspirations)."""
    out = run("worker").stdout
    assert "Skill(worker-loop)" in out
    assert "<<autonomous-loop-dynamic>>" not in out
    assert "Skill(aspirations)" not in out.replace(
        "NEVER call Skill(aspirations)", ""
    ).replace("args='loop'", "")


def test_worker_prompt_checks_durable_closure_before_resuming():
    """The safe landing reads the DURABLE record, not the consumed sentinel.

    A net armed by the last work unit outlives a genuine close and fires ~600s
    later. The original prompt branched on the body-closing sentinel's
    EXISTENCE — but stop-hook Phase 2B CONSUMES that sentinel on every
    genuine-close branch (close_body_on_genuine), so a COMPLETED close and a
    close that never happened produce the identical observation, and the
    branch read the first as "resume" (measured cc-08 2026-08-09 04:39→04:49).
    The durable closure record is body-manifest.yaml body_state; the sentinel
    remains only as the close-in-flight secondary. Phase -0 role-gating cannot
    cover this either — the forked body-WM file survives a close.
    """
    p = worker_prompt()
    assert "body-manifest.yaml" in p
    assert "body_state" in p
    assert "CONSUMED" in p, "the prompt must say WHY sentinel-absence proves nothing"
    assert "body-closing" in p, "the sentinel stays as the close-in-flight secondary"
    assert "do NOT resume" in p
    assert "do NOT re-arm" in p, (
        "a closed Body that re-arms re-fires every ~600s forever")


def test_worker_prompt_checks_closure_before_rearming():
    """The 2026-08-09 ordering fix, pinned against regression to arm-first.

    Re-arm-first (rb-4345: firing CONSUMES the net, so a turn that dies before
    re-arming has no third chance) still governs the LIVE branch — but it must
    come AFTER the one manifest read, or a CLOSED Body re-schedules the net on
    every firing: a permanent ~600s zombie cycle, which is a certainty on every
    close, against the rare race of dying during one file read.
    """
    p = worker_prompt()
    # The closure check precedes the first re-arm mention.
    assert p.index("body-manifest.yaml") < p.lower().index("re-arm")
    # The LIVE branch still re-arms BEFORE resuming work (rb-4345 preserved).
    # rindex, not index: the parked branch () also re-arms-then-resumes,
    # so an index-based assertion would silently retarget onto THAT branch and
    # keep passing even if the live branch lost its ordering entirely.
    live = p.index("IF body_state is active")
    assert live < p.index("re-arm this same wakeup", live)
    # The proxy is "resume by calling Skill(worker-loop)", not the older
    # "then resume by calling":  put an in-flight-claim check between
    # the re-arm and the re-entry, so the two clauses are no longer adjacent.
    # The INVARIANT is unchanged and is what this pins — re-arm still precedes
    # every path that resumes work.
    assert (p.index("re-arm this same wakeup", live)
            < p.index("resume by calling Skill(worker-loop)", live))


def test_worker_prompt_resumes_its_own_claim_instead_of_reselecting():
    """: the net fires MID-UNIT on a HEALTHY Body, and the prompt
    must not send it back through SELECT.

    The 600s net is re-armed at every terminal pair, so any single unit longer
    than the delay trips it — witnessed 2026-08-26 on a ~55 min deep-code unit
    whose suite alone ran 40 min. The firing is correct; the instruction was
    not. Skill(worker-loop) reaches Phase 1 SELECT, which offers only UNCLAIMED
    goals, so it hands out a DIFFERENT goal while the half-finished unit keeps
    its claim and its uncommitted edits — a stranded claim of exactly the
    g-115-6337 shape.
    """
    p = worker_prompt()
    live = p.index("IF body_state is active")
    branch = p[live:]
    # The check exists, and keys on the SID rather than the agent (guard-1460:
    # another SESSION of the same agent can hold a claim, and claimed_by_sid is
    # the only field that separates them).
    assert "claimed_by_sid" in branch
    assert "NEVER on claimed_by" in branch, (
        "keying on claimed_by would read a peer session's live claim as my own")
    # It must fire BEFORE the re-entry, or the goal is already gone.
    assert (branch.index("claimed_by_sid")
            < branch.index("resume by calling Skill(worker-loop)"))
    # And it must say what to do instead of re-entering.
    assert "do NOT call Skill(worker-loop)" in branch


def test_worker_prompt_treats_parked_as_resumable_not_closed():
    """: `parked` is a WIND-DOWN, not a close. Pin both halves.

    The branch this prompt used to carry was "anything other than active ->
    do NOT resume and do NOT re-arm". That predicate was written when every
    non-active state was terminal, so adding `parked` to VALID_STATES recruited
    this prompt into wedging the exact Body parking exists to keep alive: a
    permanent stop with no wakeup left in the slot — the durable close g-306-291
    removed, reintroduced through the net. Reachable whenever a park turn dies
    before arming its own 3600s poll, leaving the previous unit's 600s net armed.

    The fix is a CLOSED-SET test, so this also pins the fail-safe DIRECTION: an
    unrecognised state must resolve toward resuming (recoverable) rather than
    stopping dead (not).
    """
    p = worker_prompt()
    parked = p.index("IF body_state is parked")
    closed = p.index("IF body_state is one of")

    # The parked branch resumes and re-arms — the two things the closed branch
    # forbids. Assert on the parked SLICE, not the whole prompt, or the closed
    # branch's own "do NOT resume" satisfies a whole-string search.
    slice_ = p[parked:p.index("IF body_state is active")]
    assert "RESUMABLE" in slice_ and "is NOT a close" in slice_
    assert "re-arm this same wakeup FIRST" in slice_
    assert "3600" in slice_, "a park re-polls hourly, not every 600s"
    assert "do NOT resume" not in slice_ and "do NOT re-arm" not in slice_

    # The closed branch must NOT have widened back into a not-active test.
    closed_slice = p[closed:parked]
    assert "anything other than active" not in closed_slice
    for state in ("closed-pending-merge", "merged", "closed-stale"):
        assert state in closed_slice
    assert "parked" not in closed_slice, "parked must never be listed as closed"

    # Fail-safe direction: an unknown state resumes rather than stopping dead.
    assert "any value not named above" in p


def test_reducer_role_is_retired_and_refuses_loudly():
    """ decided RETIRE over WIRE. The refusal must EXPLAIN itself.

    A caller reaching for `--role reducer` is most likely mid-way through wiring
    one of the three emitters. A bare "usage: --role worker" reads as a typo and
    sends them to re-add the branch, which is precisely the outcome the decision
    rejected — so the message names the goal and points at the header.
    """
    r = run("reducer")
    assert r.returncode == 2, "the retired role must not silently succeed"
    assert "RETIRED" in r.stderr and "g-306-241" in r.stderr, (
        "the refusal must say it was deliberate and where the reasoning lives")
    assert "<<autonomous-loop-dynamic>>" not in r.stdout, (
        "no reducer directive may still be emitted")


def test_wiring_the_reducer_emitters_would_have_broken_the_detector():
    """The third measurement behind the RETIRE decision, pinned so it cannot rot.

    iteration-close-reminder.py keys its deep-recurring branch on the LITERAL
    text recurring-close.sh emits. This script emits a different prefix and none
    of those tokens, so wiring recurring-close to it would have silently
    downgraded that branch to the generic reminder — green while inert, which is
    the exact class the whole deadman effort exists to fix.

    Two halves. (a) The coupling the decision cited still exists, so the header
    is not asserting a fact nobody re-checks. (b) This script's output shares
    none of its tokens — the half that is about THIS file.

    Deliberately token-level rather than extracting the reminder's regex and
    replaying it: the first version of this test did that and could not match,
    because the pattern is written `\\[recurring-close\\]` in source and a
    reconstructed `\\[` matches a bracket, not a backslash. Reconstructing
    another file's regex from its source text is a brittle way to assert a
    coupling; the tokens are what the coupling is actually made of.
    """
    reminder = (SCRIPTS / "iteration-close-reminder.py").read_text(encoding="utf-8")
    for token in ("recurring-close", "OUTCOME=deep", "NEXT ACTION REQUIRED"):
        assert token in reminder, (
            f"iteration-close-reminder.py no longer keys on {token!r} — the "
            f"coupling that decided g-306-241 has changed, so re-read this "
            f"script's header before trusting its third measurement")

    out = run("worker").stdout
    assert "[deadman]" in out, "the prefix divergence is the point"
    for token in ("recurring-close", "OUTCOME=", "NEXT ACTION REQUIRED"):
        assert token not in out, (
            f"this script now emits {token!r}, which the reminder keys on — "
            f"re-open the WIRE-vs-RETIRE decision, its third measurement no "
            f"longer holds")


def test_opt_out_flag_disables_the_worker_net():
    """The opt-out is AGENT-level, not role-level: a box whose operator disabled
    the net disabled it for every Body, and this script reads the same flag path
    the three reducer emitters read, so it keeps exactly one meaning.

    STATIC by necessity, and the earlier behavioural version was a live hazard.
    It created the REAL `deadman-disabled` file in the REAL agent session dir
    and removed it in a `finally`. A `finally` does not run when the process is
    killed — and the chunked full-suite runner was killed three times during
    this goal alone — so a kill in the ~50ms window left the flag SET, silently
    disabling the fleet's silent-loop-death protection with no other signal.
    A test that can switch off the safety mechanism it tests is worse than a
    narrower test.

    There is no hermetic seam to keep the behavioural form: `_paths.sh`
    force-exports PROJECT_ROOT derived from BASH_SOURCE (L19), `agent_dir()`
    joins that (L118-124), and `MIND_AGENTS_ROOT` is not honoured there — so
    the script cannot be pointed at a tmp tree. This matches the direct
    precedent, `test_deadman_default_on.py`, which pins the same flag purely by
    reading script text. The branch is four lines of straight-line shell with no
    logic to exercise; what matters is that both roles read the same flag and
    neither arms a wakeup when it is set.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    assert "deadman-disabled" in src, "must gate on the opt-OUT flag"
    assert "deadman-enabled" not in src, (
        "the retired opt-IN flag would silently revert default-ON protection"
    )
    # One flag READ, shared by both roles — not a per-role gate that could drift.
    # Count the `-f` test, NOT raw occurrences of the name: the script also
    # names the flag in a comment and in both DISABLED messages, so a raw
    # count conflates prose with code. That is the guard-1099 trap, and the
    # first version of this assertion fell into it (it asserted == 1 against
    # four occurrences and reddened immediately).
    gate_lines = [ln for ln in src.splitlines()
                  if "-f " in ln and "deadman-disabled" in ln]
    assert len(gate_lines) == 1, f"expected exactly one flag gate, got {gate_lines}"
    # The disabled branch must return before DELAY/ScheduleWakeup are reachable.
    disabled_at = src.index("DISABLED")
    assert src.index("DELAY=600") > disabled_at, (
        "the opt-out branch must exit BEFORE the arming text is emitted"
    )
    assert '"worker"' in src
    # The retired role must be refused BEFORE the flag is read. Otherwise
    # `--role reducer` on a disabled box would fall through to the worker-shaped
    # DISABLED line and exit 0 — a silent SUCCESS for a role that no longer
    # exists, which is the one outcome the loud refusal is there to prevent.
    # This pair replaces an earlier `for role in ("worker", "reducer")` loop that
    # merely asserted both names appear in the file: still green after the
    # retirement, because "reducer" appears in the refusal branch, while meaning
    # something entirely different from what it was written to check.
    lines = src.splitlines()
    refuse_at = next(i for i, ln in enumerate(lines)
                     if 'ROLE" == "reducer"' in ln and not ln.lstrip().startswith("#"))
    gate_at = next(i for i, ln in enumerate(lines)
                   if "-f " in ln and "deadman-disabled" in ln)
    assert refuse_at < gate_at, (
        "the retired-role refusal must precede the opt-out flag read")


def test_bad_role_is_a_usage_error():
    r = run("bogus")
    assert r.returncode == 2


def test_worker_loop_skill_calls_the_shared_component():
    """guard-2676 no-transcription contract: worker-loop must CALL the shared
    component, not spell the directive out itself."""
    skill = (SCRIPTS.parent.parent / ".claude" / "skills" / "worker-loop" / "SKILL.md").read_text(encoding="utf-8")
    assert "deadman-directive.sh --role worker" in skill


def test_worker_loop_never_instructs_arming_the_reducer_sentinel():
    """The sentinel must never be something worker-loop tells you to EMIT.

    Deliberately NOT `"<<autonomous-loop-dynamic>>" not in skill`. That was the
    first version of this test and it reddened on the file's own PROHIBITION
    ("Do NOT reach for the reducer's <<autonomous-loop-dynamic>> sentinel") —
    i.e. it failed on the documentation that exists to prevent the very thing
    it was checking for. Same shape as guard-1099, where an unanchored grep
    counted comments quoting a deleted glob as live code. Test the instruction,
    not the token.
    """
    skill = (SCRIPTS.parent.parent / ".claude" / "skills" / "worker-loop" / "SKILL.md").read_text(encoding="utf-8")
    for idx, line in enumerate(skill.splitlines(), 1):
        if "ScheduleWakeup(prompt=" in line and "<<autonomous-loop-dynamic>>" in line:
            pytest.fail(
                f"worker-loop line {idx} arms the REDUCER sentinel: {line.strip()[:120]}"
            )
