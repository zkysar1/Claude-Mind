"""Pin : state-update / learning-gate must read the goal record forward.

THE DEFECT, measured twice in one day (echo, hostname cc-03, uname -r
6.8.0-136-generic, 2026-08-05) from two OPPOSITE causes:
  - g-315-532: verify NEVER RAN — the execution diary jumps from the claim
    straight to state-update.
  - g-115-4718, 56 minutes later: verify RAN and was REFUSED (the call omitted
    --status, so do_verify exits 2 at its entry check before doing anything).
Both times state-update, learning-gate and productivity-check all reported
SUCCESS over a goal record still at status=pending with a live claim, which
left it a live goal-selector candidate one step from re-executing work that had
already been committed.

The only pre-existing read of the record, `_probe_goal_status`, is called ONLY
inside `_print_recovery_instructions`, which by construction runs on rc!=0. In
both incidents every phase returned 0, so nothing read anything.

WHY THE PREDICATE IS `pending|in-progress` AND NOT "not terminal".
g-115-5001 proposed refusing when the live status "is not terminal". That is
the goal's REASONED half, not its measured half (guard-1719), and reading the
code falsifies it: do_verify legitimately accepts
`--status <completed|blocked|skipped>`, and `blocked` is NOT in
`_goal_census.TERMINAL_STATUSES` (completed + skipped/expired/decomposed/
superseded). A not-terminal predicate would therefore false-fire on EVERY
legitimate blocked close. `test_blocked_close_does_not_warn` is the pin for
that correction and is the load-bearing test in this file — it is the one that
would have caught the goal's own proposal.

WHY IT WARNED, AND WHY IT NOW REFUSES (g-115-5573, 2026-08-09).
The original decision was to WARN, on guard-2760: adding a consumer of a failure
signal whose remedy is destructive (halting a close mid-sequence) requires
evidence that a reversible remedy is insufficient, and no loud warning had ever
been tried. That was correct at the time. Both of its premises have since been
measured and both have fallen:

  1. THE REVERSIBLE REMEDY WAS TRIED AND IS INSUFFICIENT. The warning landed in
     8b4cb6f67 (2026-08-06 19:59 UTC). g-115-5104 was miscounted on 2026-08-09
     ~21:12 — three days later, with the warning live. Its fix had shipped as
     bd0e6c913, yet state-update, learning-gate and productivity all ran and
     counted it closed while the record stayed at pending with no outcome_note,
     returning shipped work to the selectable pool. A warning that is printed
     and then walked past is not a remedy; that is exactly the evidence
     guard-2760 asked for.
  2. THE STALE-READ FALSE POSITIVE DOES NOT APPLY TO THIS PROBE.
     _probe_goal_status reads via aspirations-read.sh, which sources
     _runtime.sh and issues `rt_call GET /v1/aspirations/read` — daemon-routed,
     with no Python CLI fallback. It is not the local read-through cache. And
     verify and state-update run seconds apart on one box through one daemon,
     so a verify that DID close the goal reads completed.

The residual risk is handled by the FAIL LADDER, not by hope: the refusal fires
only on TWO independent CLEAN positive reads (an open status AND a confirmed
non-recurring goal). Every unknown proceeds. A daemon that is unreachable makes
aspirations-read.sh fail loud, _probe_goal_status return "", and the phase
PROCEED — so a store blip can never wedge a close.

THE RECURRING CARVE-OUT IS WHAT MAKES THE REFUSAL SHIPPABLE, and its absence is
why the predecessor could only ever warn. do_verify routes recurring goals to
aspirations-complete-by.sh, which sets status back to "pending" on every cycle,
outcome-independently (pinned by test_complete_by_recurring_status_reset.py). So
pending is the NORMAL post-close state for a recurring goal and carries no
information about whether verify landed. Refusing without the carve-out would
have fired on 100% of recurring closes — measured 2026-08-09 on asp-115, 40 of
45 recurring goals with achievedCount>0 sat at pending and 3 more at
in-progress. That is a fleet-wide outage, not an edge case.

HOW THESE TESTS RUN THE REAL CODE. The precondition sits AFTER the phase entry
validation, so the cheap "validation-reject" invocation the sibling
test_iteration_close_recovery_probe.py uses cannot reach it, and invoking the
phase for real would run the entire ~1250-line state-update with live side
effects. So the behavioural tests EXTRACT the function's bytes from the real
script at test time (never a hardcoded copy — a copy would keep passing after
the real one drifted) and source it with `_probe_goal_status` stubbed. The
structural tests then pin the wiring the extraction cannot see.
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _runtime_bash import BASH  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "iteration-close.sh"
FUNC = "_assert_verify_landed"


def _extract(func: str) -> str:
    """Return the real function's source text, from the real file, at test time.

    Anchors on a column-0 `}` as the terminator, which is this file's style for
    every top-level function. Raises rather than returning a partial body — a
    silently truncated extraction would make every behavioural test below pass
    vacuously, which is the failure mode these tests exist to prevent.
    """
    src = SCRIPT.read_text(encoding="utf-8").splitlines()
    start = next((i for i, ln in enumerate(src) if ln.startswith(f"{func}() {{")), None)
    assert start is not None, f"{func} not found in {SCRIPT.name}"
    end = next((i for i in range(start + 1, len(src)) if src[i] == "}"), None)
    assert end is not None, f"no column-0 close brace for {func}"
    return "\n".join(src[start:end + 1])


def _run_predicate(status: str, phase: str = "state-update",
                   recurring: str = "false", claim_held: str = "false",
                   raw: str | None = None):
    """Source the real function with _probe_goal_record stubbed.

    The stub emits the wide "<status>\\t<recurring>\\t<claim_held>" line the real
    probe now returns (g-115-5216). `raw` overrides the whole line so the
    genuinely-empty (unreadable-record) case can be exercised directly rather
    than simulated with an empty status field. (The g-115-5573 tri-state
    recurring probe this harness used to stub was retired at the 2026-08-13
    reconcile — the record probe carries `recurring` in the same read.)
    """
    line = raw if raw is not None else f"{status}\t{recurring}\t{claim_held}"
    harness = f"""
set -uo pipefail
GOAL_ID="g-999-1"; GOAL_STATUS="completed"; SOURCE="world"; OUTCOME="deep"
_probe_goal_record() {{ printf '%s' "{line}"; }}
{_extract(FUNC)}
{FUNC} "{phase}"
echo "RC=$?"
"""
    return subprocess.run(
        [BASH, "-c", harness], capture_output=True, text=True, timeout=60
    )


WARN_MARKER = "FORWARD-PRECONDITION WARNING"
REFUSE_MARKER = "REFUSED"


@pytest.mark.parametrize("status", ["pending", "in-progress"])
@pytest.mark.parametrize("phase", ["state-update", "learning-gate"])
def test_open_non_recurring_statuses_refuse(status, phase):
    """The two statuses both incidents actually exhibited (: REFUSE).

    Was test_open_statuses_warn, asserting RC=0. The rc flipped deliberately —
    see the module docstring for the evidence that retired guard-2760's
    warn-first posture. The phase is parametrized because a closer may run
    learning-gate alone.
    """
    r = _run_predicate(status, phase=phase)
    assert REFUSE_MARKER in r.stderr, f"no refusal for status={status}"
    assert f"status={status}" in r.stderr, "the refusal must name the status it saw"
    assert phase in r.stderr, "the refusal must name the phase it blocked"
    assert "--phase verify" in r.stderr, "must print the verify-first retry command"
    assert "RC=1" in r.stdout, "an open non-recurring record must HALT the phase"


# (test_recurring_goal_proceeds was retired at the 2026-08-13 reconcile: it
# asserted the healthy-recurring branch SAYS why it proceeds, and the merged
# design adopts 's measurement that any output there is a false
# positive — the branch is now silent. Its invariant is carried, sharpened, by
# test_recurring_successful_close_does_not_warn below.)


@pytest.mark.parametrize("status", ["pending", "in-progress"])
def test_unknown_recurring_fails_open(status):
    """Refuse on clean reads only. "" means the recurring answer is UNKNOWN.

    Unreachable via the real record probe (a found record always carries a
    definite recurring boolean — the unknown case is an EMPTY rec, pinned by
    test_unreadable_record_fails_open). Kept because the refusal's authorizing
    read must stay the clean "false" even if a stub or a future probe emits an
    ambiguous line: a refusal must never rest on an ambiguous read.
    """
    r = _run_predicate(status, recurring="")
    assert "RC=0" in r.stdout, f"unknown-recurring at {status} must fail OPEN"
    assert REFUSE_MARKER not in r.stderr
    assert "asserting neither" in r.stderr


def test_carve_out_is_load_bearing_not_defensive():
    """guard-3126: a mutation kill says "my case fires", not "the case was needed".

    Same open status, ONLY the recurring flag differs -> opposite verdicts. That
    is what makes the carve-out load-bearing rather than belt-and-braces.
    """
    rec = _run_predicate("pending", recurring="true")
    plain = _run_predicate("pending", recurring="false")
    assert "RC=0" in rec.stdout and "RC=1" in plain.stdout, (
        "the recurring flag must decide the verdict at identical status")


def test_blocked_close_does_not_warn():
    """THE LOAD-BEARING PIN — `blocked` is a legitimate verify close.

    do_verify accepts --status <completed|blocked|skipped>. `blocked` is NOT in
    _goal_census.TERMINAL_STATUSES, so the "refuse when not terminal" predicate
    g-115-5001 proposed would fire on every legitimate blocked close. If this
    test ever reddens, someone has re-widened the predicate back to the goal's
    original proposal and reintroduced that false positive.
    """
    r = _run_predicate("blocked")
    # Assert on BOTH markers and the rc, not just the warn marker. Measured
    # 2026-08-09 (): after the warn->refuse change, widening the
    # predicate to include `blocked` made this path emit REFUSED rather than the
    # warning — so a warn-only assertion passed vacuously through the exact
    # regression this test is named for. A test that pins one branch's output
    # string stops pinning anything the moment the branch's output changes.
    assert WARN_MARKER not in r.stderr, (
        "warned on a legitimate blocked close — the predicate has been widened "
        "back to 'not terminal'; see this module's docstring")
    assert REFUSE_MARKER not in r.stderr, (
        "REFUSED a legitimate blocked close — do_verify accepts "
        "--status blocked, so this halts a valid close sequence")
    assert "RC=0" in r.stdout, "a legitimate blocked close must not be halted"


@pytest.mark.parametrize("status", ["completed", "skipped"])
def test_closed_statuses_do_not_warn(status):
    r = _run_predicate(status)
    assert WARN_MARKER not in r.stderr, f"false positive on a closed goal ({status})"
    assert REFUSE_MARKER not in r.stderr, f"REFUSED a closed goal ({status})"
    assert "RC=0" in r.stdout, f"halted a closed goal ({status})"


def test_unreadable_record_fails_open():
    """_probe_goal_record prints "" for an unset/unparseable id, an unreadable
    queue, and g-xw-* ids whose aspiration is not derivable. All must be silent:
    the recovery block already models this as "asserting neither direction"."""
    r = _run_predicate("", raw="")
    assert WARN_MARKER not in r.stderr, "asserted a direction on an unreadable record"
    assert REFUSE_MARKER not in r.stderr, (
        "REFUSED on an unreadable record — a store-read blip now wedges every "
        "close, which is the one failure mode the fail ladder exists to prevent")
    assert "RC=0" in r.stdout


# ── : the recurring carve-out (kept under the  refusal) ─
#
# The warning was wrong on 100% of recurring closes, because a recurring goal
# RETURNS to status=pending on a SUCCESSFUL close by design (guard-1483). Both
# tests below are load-bearing and they pull in OPPOSITE directions: the first
# is the fix, the second is its guard-1562 blast-radius pin. Deleting either
# one alone leaves a suite that looks green while the other failure mode is
# wide open.

@pytest.mark.parametrize("status", ["pending", "in-progress"])
def test_recurring_successful_close_does_not_warn(status):
    """THE FIX. A healthy recurring close pops claimed_by
    (aspirations_write.py L3592-3594) and cycles status back to pending. That
    is not a half-closed goal and must not warn — and under the merged refusal
    design it must not refuse either."""
    r = _run_predicate(status, recurring="true", claim_held="false")
    assert WARN_MARKER not in r.stderr, (
        f"warned on a healthy recurring close (status={status}) — this is the "
        f"100%-false-positive class g-115-5216 removed")
    assert REFUSE_MARKER not in r.stderr, (
        f"REFUSED a healthy recurring close (status={status}) — the carve-out "
        f"is what makes the refusal shippable; without it this is a fleet-wide "
        f"outage (see module docstring)")
    assert "RC=0" in r.stdout


@pytest.mark.parametrize("status", ["pending", "in-progress"])
def test_recurring_with_live_claim_still_warns(status):
    """THE BLAST-RADIUS PIN (guard-1562). Suppressing on `recurring` ALONE
    would silence a recurring goal whose verify genuinely never ran — the exact
    defect the warning exists to catch. A close that did not happen leaves the
    claim live (both g-115-5001 incidents sat pending with one, ~19 and ~11
    min), so claim_held keeps the warning honest for that case.

    WARN, not refuse: the g-115-5104 field evidence behind the refusal is
    non-recurring, and extending a destructive remedy to a population with no
    evidence is what guard-2760 forbids (see the block comment above the
    function)."""
    r = _run_predicate(status, recurring="true", claim_held="true")
    assert WARN_MARKER in r.stderr, (
        "silenced a recurring goal that still holds its claim — the carve-out "
        "has been widened to bare `recurring`, reopening the case the warning "
        "was built for")
    assert REFUSE_MARKER not in r.stderr, (
        "REFUSED an abandoned recurring close — no field evidence supports a "
        "destructive remedy for this population (guard-2760)")
    assert "RC=1" not in r.stdout, "the abandoned-recurring branch must proceed"


def test_carve_out_does_not_key_on_lastachievedat():
    """Guard the CORRECTION, at the source level — the sibling of
    test_predicate_is_not_the_terminal_set.

    Five independent confirmations on g-115-5216 all proposed keying the
    carve-out on "lastAchievedAt advanced during this close". guard-2197
    falsifies that: recurring-precondition-sweep.py advances lastAchievedAt on a
    precondition-SHELVED goal while never writing achievedCount, so a shelved
    goal and a closed one are indistinguishable at that field — and the shelved
    one is precisely the case that must keep warning. Reaching for it here would
    read as principled (it is what the goal record prescribes) while silently
    re-suppressing that population.
    """
    body = _extract(FUNC)
    assert "lastAchievedAt" not in body, (
        f"{FUNC} keys on lastAchievedAt; guard-2197 measured it advancing on "
        f"precondition-shelved goals, so it cannot separate 'verify closed it' "
        f"from 'the sweep shelved it' — see this test's docstring")


def test_gate_probe_is_daemon_routed_not_the_local_cache_one():
    """The gate must not consult _probe_is_recurring — guard-980.

    That helper emits "false" from THREE distinct sites — the genuine answer,
    goal-absent-from-file, and `|| echo "false"` on invocation failure — and it
    reads the LOCAL aspirations.jsonl, a read-through cache under own-cloud.
    A goal absent from this box therefore reads "false", which is the value
    that AUTHORIZES a refusal here. The g-115-5573 tri-state probe that first
    carried this pin was retired at the 2026-08-13 reconcile (the record probe
    answers `recurring` on the same read); the invariant transfers to the
    probe the gate consults now.
    """
    gate = _extract(FUNC)
    assert "_probe_is_recurring" not in gate, (
        "the gate consults the local-cache probe, which cannot express 'unknown'")
    assert "_probe_goal_record" in gate, (
        "the gate no longer consults the wide record probe — where does its "
        "recurring answer come from?")
    probe = _extract("_probe_goal_record")
    assert "aspirations-read.sh" in probe, "record probe must be daemon-routed"


def test_predicate_is_not_the_terminal_set():
    """Guard the CORRECTION itself, at the source level.

    The behavioural tests above prove today's predicate behaves correctly. This
    one pins that nobody reintroduces the falsified formulation by reaching for
    the terminal-status vocabulary here — which would read as principled (it is
    the canonical set) while silently re-adding the blocked false positive.
    """
    body = _extract(FUNC)
    for token in ("TERMINAL_STATUSES", "ABANDONED_STATUSES"):
        assert token not in body, (
            f"{FUNC} references {token}; the terminal set INCLUDES neither "
            f"'blocked' nor a not-terminal test that is safe here — see the "
            f"module docstring")
    assert "pending" in body and "in-progress" in body


# ── structural pins: the wiring the extraction cannot see ──────────────────

def _code_lines():
    """Non-comment lines only — this file documents the call in comments right
    above it, so a raw substring count conflates prose with code (guard-1099)."""
    return [ln for ln in SCRIPT.read_text(encoding="utf-8").splitlines()
            if not ln.lstrip().startswith("#")]


def test_probe_emits_the_three_fields_from_one_read():
    """ widened the probe rather than adding a second read.

    The whole carve-out rests on `recurring` and `claimed_by` being available
    where `status` already was. If someone re-narrows the emitter, the carve-out
    silently stops firing (both fields read empty -> every recurring close warns
    again) with no test failing anywhere else, because the behavioural tests
    above stub the probe out entirely.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    start = src.index("_probe_goal_record() {")
    body = src[start:src.index("\n}\n", start)]
    for field in ("status", "recurring", "claimed_by"):
        assert field in body, f"_probe_goal_record no longer reads {field}"
    assert 'aspirations-read.sh' in body
    assert body.count("aspirations-read.sh") == 1, (
        "_probe_goal_record must stay ONE read — the three fields ride along on "
        "the round-trip that already fetched status (see its header comment)")


def test_probe_goal_status_stays_narrow():
    """guard-695: the wrapper's output shape is the back-compat contract.

    Two other call sites and any external caller still expect a bare status
    string. Pin that _probe_goal_status did not itself become the wide emitter.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    start = src.index("_probe_goal_status() {")
    body = src[start:src.index("\n}\n", start)]
    assert "_probe_goal_record" in body, (
        "_probe_goal_status must delegate to the wide reader, not re-query")
    assert "aspirations-read.sh" not in body, (
        "_probe_goal_status re-queries the store — that is a second read per "
        "call site, which the g-115-5216 split exists to avoid")


def test_both_phases_call_it():
    """Both phases, not just the first:  showed learning-gate
    reporting success over an open record independently, and a closer may run
    learning-gate on its own."""
    calls = [ln for ln in _code_lines() if re.search(rf"^\s*{FUNC}\s+\"", ln)]
    assert len(calls) == 2, f"expected exactly 2 call sites, got {calls}"
    joined = "\n".join(calls)
    assert '"state-update"' in joined and '"learning-gate"' in joined


def test_calls_are_on_the_success_path_not_in_a_recovery_block():
    """The entire defect is that every pre-existing read sits behind rc!=0.

    A call placed inside _print_recovery_instructions would satisfy
    test_both_phases_call_it while restoring the exact blind spot, so pin that
    both calls land inside the phase functions and after
    _print_recovery_instructions has ended.
    """
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()

    def line_of(pat):
        return next(i for i, ln in enumerate(lines)
                    if re.search(pat, ln) and not ln.lstrip().startswith("#"))

    recovery = line_of(r"^_print_recovery_instructions\(\) \{")
    su = line_of(r"^do_state_update\(\) \{")
    lg = line_of(r"^do_learning_gate\(\) \{")
    calls = [i for i, ln in enumerate(lines)
             if re.search(rf"^\s*{FUNC}\s+\"", ln) and not ln.lstrip().startswith("#")]

    assert len(calls) == 2
    for c in calls:
        assert c > recovery, f"call at line {c+1} precedes the recovery block"
    assert su < calls[0] < lg, "first call must be inside do_state_update"
    assert calls[1] > lg, "second call must be inside do_learning_gate"


def test_precondition_runs_before_the_phase_body():
    """A read placed at the END of state-update would report the record open
    only after ~1250 lines of work had already run and written. Pin that each
    call sits near its phase's entry echo."""
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    for fn, phase in (("do_state_update", "state-update"),
                      ("do_learning_gate", "learning-gate")):
        start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{fn}() {{"))
        call = next(i for i in range(start, len(lines))
                    if re.search(rf"^\s*{FUNC}\s+\"{phase}\"", lines[i]))
        assert call - start < 30, (
            f"{fn}: precondition is {call - start} lines past the entry — it "
            f"must run before the phase body does its work")
