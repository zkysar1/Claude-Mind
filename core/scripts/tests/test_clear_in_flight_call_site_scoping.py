"""The reducer-side in_flight clear is scoped to its own goal ().

`test_clear_in_flight_cas.py` proves the STORE-level compare-and-swap: given
`if_goal`, the modifier declines a foreign row. That is the layer g-306-137
built. This file proves the layer between that guard and the callers who need
it — and which was missing entirely until g-306-161:

  1. `team-state-clear-in-flight.sh` actually FORWARDS `--if-goal` to the
     endpoint. Its arg loop ends in `*) shift;;`, so before this landed a
     `--if-goal` passed by a caller was SILENTLY DISCARDED and the clear ran
     unconditionally — a fix that reads correct at every call site and changes
     nothing (guard-1776 class). A source-pin cannot see that; only running the
     wrapper and reading the query it built can.
  2. the wrapper reports a DECLINED clear as declined. Two branches would print
     "already absent" for a row that is still standing — the g-306-163 defect
     class, a verdict asserting a proven clear that never happened.
  3. both `iteration-close.sh` call sites pass the flag, each with the goal id
     that is actually in scope there. The recovery site must use `$_gid` (from
     the checkpoint) and NOT `$GOAL_ID`: `do_recover` never reads `$GOAL_ID`,
     and `--phase recover` is invoked without `--goal`, so scoping it to
     `$GOAL_ID` would compare against an empty string and silently degrade to
     an unconditional clear.

The wrapper tests are hermetic by staging a COPY of the real script beside a
stub `_runtime.sh` — the wrapper derives CORE_ROOT from its own location, so
the copy sources the stub and never reaches a daemon. Real bytes, no network.

guard-1165: no module-level os.environ mutation, no sys.modules stubs.
Run: STORAGE_BACKEND=local python -m pytest \
     core/scripts/tests/test_clear_in_flight_call_site_scoping.py -q
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402  (guard-580: explicit bash binary)

SCRIPTS = Path(__file__).resolve().parents[1]
WRAPPER = SCRIPTS / "team-state-clear-in-flight.sh"
ITERATION_CLOSE = SCRIPTS / "iteration-close.sh"

# Stub _runtime.sh: records the query rt_call was handed, then answers with a
# canned response. RESPONSE_JSON/QUERY_SINK travel by env so the stub never
# interpolates test data into shell source (guard-165).
STUB_RUNTIME = """
rt_url_encode() { printf '%s' "$1"; }
rt_python_launcher() { printf '%s' "$RT_PY"; }
rt_call() {
    local q=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --query) q="$2"; shift 2;;
            *) shift;;
        esac
    done
    printf '%s' "$q" > "$QUERY_SINK"
    printf '%s' "$RESPONSE_JSON"
    return 0
}
rt_try_autospawn() { return 1; }
rt_no_daemon_error() { echo "no daemon: $1" >&2; exit 1; }
"""

CLEARED = '{"ok":true,"agent":"alpha","cleared":true,"skipped_goal_id":null}'
DECLINED = '{"ok":true,"agent":"alpha","cleared":false,"skipped_goal_id":"g-9-9"}'
ABSENT = '{"ok":true,"agent":"alpha","cleared":false,"skipped_goal_id":null}'
# A row IS standing but carries no goal_id to name, so skipped_goal_id is null
# on the decline too — byte-identical to ABSENT except for row_survived
# (). That collision is the whole defect: without the third key these
# two responses are the same JSON and no reporter can separate them.
UNVERIFIABLE = ('{"ok":true,"agent":"alpha","cleared":false,'
                '"skipped_goal_id":null,"row_survived":true}')


def _stage(tmp_path, drop_if_goal_case=False):
    """Copy the REAL wrapper next to a stub _runtime.sh.

    drop_if_goal_case reproduces the pre-g-306-161 wrapper by deleting only the
    `--if-goal)` case, so the flag falls through to `*) shift;;` exactly as it
    did. That variant is the permanent negative control (guard-1829): it ships
    with the test so the discrimination outlives this commit.
    """
    core = tmp_path / "core" / "scripts"
    core.mkdir(parents=True)
    src = WRAPPER.read_text(encoding="utf-8")
    if drop_if_goal_case:
        kept = [ln for ln in src.splitlines(True)
                if not ln.lstrip().startswith("--if-goal)")]
        assert len(kept) == len(src.splitlines(True)) - 1, (
            "expected exactly one --if-goal) case line to remove")
        src = "".join(kept)
    dest = core / "team-state-clear-in-flight.sh"
    dest.write_text(src, encoding="utf-8")
    (core / "_runtime.sh").write_text(STUB_RUNTIME, encoding="utf-8")
    return dest


def _run(script, tmp_path, args, response=CLEARED):
    sink = tmp_path / "query.txt"
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "RT_PY": sys.executable,
        "RESPONSE_JSON": response,
        "QUERY_SINK": str(sink),
    }
    # guard-580/581: never a bare "bash" argv[0] (on win32 CreateProcess finds
    # the System32 WSL launcher and blocks forever), and never str(WindowsPath)
    # as the script arg (bash strips the backslashes silently).
    proc = subprocess.run([BASH, Path(script).as_posix()] + args,
                          capture_output=True, text=True, env=env, timeout=60)
    query = sink.read_text(encoding="utf-8") if sink.exists() else ""
    return proc, query


# ── 1. the wrapper forwards --if-goal (the layer that was missing) ───────────

def test_wrapper_forwards_if_goal_to_the_query(tmp_path):
    script = _stage(tmp_path)
    proc, query = _run(script, tmp_path,
                       ["--agent", "alpha", "--if-goal", "g-1-1"])
    assert proc.returncode == 0, proc.stderr
    assert "agent=alpha" in query
    assert "if_goal=g-1-1" in query, (
        f"--if-goal never reached the endpoint; query was {query!r}")


def test_omitting_the_flag_sends_no_if_goal_at_all(tmp_path):
    """Back-compat, and the other half of guard-527: with the flag absent the
    request must be byte-identical to the pre-g-306-161 one, so retire /
    release / stop callers keep the unconditional clear they rely on."""
    script = _stage(tmp_path)
    proc, query = _run(script, tmp_path, ["--agent", "alpha"])
    assert proc.returncode == 0, proc.stderr
    assert query == "agent=alpha", f"query grew an unrequested param: {query!r}"


def test_an_empty_if_goal_value_is_forwarded_so_the_endpoint_can_refuse(tmp_path):
    """INVERTED BY  — this assertion used to be `"if_goal" not in query`.

    The old contract was "`--if-goal ''` must NOT send `if_goal=`", and its stated
    reason was: *the endpoint would coerce it back to None anyway, but sending it
    invites a caller to believe a CAS was requested when none was.*

    BOTH halves of that reason are now false, and the first one was the defect:

      - "the endpoint would coerce it back to None anyway" — that coercion
        (`(q or "").strip() or None`) IS g-306-170. Collapsing blank-but-supplied
        into absent turned a CAS request into an UNCONDITIONAL CLEAR that
        destroyed a live row and reported ok/cleared=True. The endpoint now
        refuses a blank with 400 instead of coercing it.
      - "invites a caller to believe a CAS was requested when none was" — a blank
        CAS request is no longer silently downgraded, so there is nothing left to
        be misled about.

    Note what the OLD shape did and did not buy. Dropping the param made the
    request *honest* about being unconditional, but the clear still happened —
    the row died either way. The test pinned legibility, not safety. Forwarding
    is strictly better now: the caller gets a refusal instead of a wipe.

    `returncode == 0` still holds here because `_stage` stubs the response; in
    production the daemon answers 400 and the wrapper surfaces the failure.
    """
    script = _stage(tmp_path)
    proc, query = _run(script, tmp_path, ["--agent", "alpha", "--if-goal", ""])
    assert proc.returncode == 0, proc.stderr
    assert "if_goal=" in query, (
        "empty --if-goal was DROPPED — the daemon then reads it as absent, and "
        f"absent means clear-unconditionally (g-306-170). query was {query!r}")


def test_the_dropped_case_variant_is_the_permanent_negative_control(tmp_path):
    """guard-1776/guard-1829: prove the silent-discard is real and that these
    tests detect it.

    With the `--if-goal)` case removed the flag hits `*) shift;;`, the wrapper
    exits 0, prints a success line, and sends NO if_goal — indistinguishable
    from a correct run at every level except the query. If this ever stops
    reproducing, the tests above have stopped discriminating.
    """
    script = _stage(tmp_path, drop_if_goal_case=True)
    proc, query = _run(script, tmp_path,
                       ["--agent", "alpha", "--if-goal", "g-1-1"])
    assert proc.returncode == 0, "the pre-fix wrapper did not silently succeed"
    assert "if_goal" not in query, (
        "the control carried if_goal — the case-removal no longer reproduces "
        "the pre-g-306-161 wrapper, so the forwarding tests prove nothing")


# ── 2. a declined clear is reported as declined ──────────────────────────────

def test_wrapper_reports_a_declined_clear_rather_than_absent(tmp_path):
    script = _stage(tmp_path)
    proc, _ = _run(script, tmp_path,
                   ["--agent", "alpha", "--if-goal", "g-1-1"],
                   response=DECLINED)
    assert proc.returncode == 0, proc.stderr
    assert "left alone" in proc.stdout, (
        f"a declined CAS was not reported as declined: {proc.stdout!r}")
    assert "g-9-9" in proc.stdout, "the surviving row's goal id was not named"
    assert "already absent" not in proc.stdout


def test_wrapper_reports_an_unverifiable_decline_rather_than_absent(tmp_path):
    """F-002(b): the decline whose row has no goal_id to name.

    `test_wrapper_reports_a_declined_clear_rather_than_absent` above covers the
    decline the wrapper CAN name, because skipped_goal_id carries a value. This
    is its blind twin: the CAS also declines a row that is present but not a
    dict, or a dict without a comparable goal_id — and there skipped_goal_id is
    null, so that branch cannot fire and the wrapper fell through to "already
    absent", asserting a row was gone while it was still standing.

    The paired assertion below is the discriminator (guard-1829): the SAME
    response minus `row_survived` must still print "already absent". Without
    it this test would also pass if the branch simply stopped printing the
    absent message at all, which would break the back-compat case where an
    older daemon omits the key.
    """
    script = _stage(tmp_path)
    proc, _ = _run(script, tmp_path,
                   ["--agent", "alpha", "--if-goal", "g-1-1"],
                   response=UNVERIFIABLE)
    assert proc.returncode == 0, proc.stderr
    assert "already absent" not in proc.stdout, (
        f"a surviving row was reported as absent: {proc.stdout!r}")
    assert "left alone" in proc.stdout, (
        f"an unverifiable decline was not reported as declined: {proc.stdout!r}")

    # Discriminator: drop only row_survived and the absent message must return.
    proc, _ = _run(script, tmp_path,
                   ["--agent", "alpha", "--if-goal", "g-1-1"],
                   response=ABSENT)
    assert "already absent" in proc.stdout, (
        "the wrapper stopped reporting a genuinely-absent row as absent; the "
        "new branch is suppressing the message instead of keying on "
        f"row_survived: {proc.stdout!r}")


def test_wrapper_still_reports_the_two_original_outcomes(tmp_path):
    """The added branch must not disturb the two the CLI twin already prints
    (guard-742 dual-write: these strings are the parity contract)."""
    script = _stage(tmp_path)
    proc, _ = _run(script, tmp_path, ["--agent", "alpha"], response=CLEARED)
    assert "in_flight cleared for alpha" in proc.stdout

    proc, _ = _run(script, tmp_path, ["--agent", "alpha"], response=ABSENT)
    assert "in_flight already absent for alpha" in proc.stdout


# ── 3. both iteration-close call sites scope, each with the right id ─────────

def _clear_call_lines():
    lines = [ln.strip() for ln in
             ITERATION_CLOSE.read_text(encoding="utf-8").splitlines()
             if "team-state-clear-in-flight.sh" in ln and ln.strip().startswith("bash ")]
    return lines


def test_every_iteration_close_clear_call_is_scoped():
    calls = _clear_call_lines()
    assert len(calls) == 2, (
        f"expected 2 clear invocations (verify + recovery), found {len(calls)}: "
        f"{calls}. A new unscoped call site is the g-306-161 defect returning.")
    for ln in calls:
        assert "--if-goal" in ln, f"unscoped clear call site: {ln}"


def test_the_recovery_site_scopes_to_the_checkpoint_goal_not_goal_id():
    """`do_recover` derives the goal from iteration-checkpoint.json into `_gid`
    and never reads `$GOAL_ID`; `--phase recover` is invoked by the loop and by
    /start --recover without `--goal`. Scoping recovery to `$GOAL_ID` would
    compare against an empty string, which the endpoint coerces back to None —
    an unconditional clear wearing a CAS flag, the worst of both.
    """
    calls = _clear_call_lines()
    recovery = [ln for ln in calls if '"$_gid"' in ln]
    assert len(recovery) == 1, (
        f"the recovery clear no longer scopes to $_gid: {calls}")
    assert 'GOAL_ID' not in recovery[0], (
        "the recovery clear scopes to $GOAL_ID, which is not in scope there")


def test_the_verify_site_scopes_to_goal_id():
    calls = _clear_call_lines()
    verify = [ln for ln in calls if '"$GOAL_ID"' in ln]
    assert len(verify) == 1, f"the verify clear no longer scopes to $GOAL_ID: {calls}"


def test_do_verify_still_refuses_an_empty_goal_id():
    """`$GOAL_ID` cannot be empty at the verify-site clear. This pins the
    required-arg check that makes that legible — and, MEASURED, it is not the
    only thing enforcing it.

    An earlier revision of this docstring said the scoping "is only safe
    because" of this check, and its assertion message said removal would let
    the verify CAS "degrade to an unconditional clear without any test
    noticing". `test_verify_phase_empty_goal_id.py` disarmed the check and ran
    `--phase verify` without `--goal`: the clear did NOT fire. `do_verify`
    reaches an UNGUARDED `"${update_cmd[@]}"` first, `aspirations-update-goal.sh`
    refuses the empty id (rc=1, measured against the real wrapper), and
    `set -euo pipefail` aborts the phase one write short of the clear.

    So this guard is REDUNDANT for CAS safety and LOAD-BEARING for DIAGNOSTICS:
    it turns an opaque mid-write abort into `exit 2` plus a usage line naming
    the missing flag. That is worth pinning, which is why the assertion stays;
    only the claim about what its removal causes was wrong.

    Note the verdict differs from the recovery site's twin below, and the
    difference is the interesting part: there an INPUT can hold the second
    barrier open, so that guard really is load-bearing. Here the second barrier
    is a collaborator's contract that no input reaches.
    """
    src = ITERATION_CLOSE.read_text(encoding="utf-8")
    assert ('[[ -z "$GOAL_ID" || -z "$GOAL_STATUS" || -z "$SOURCE" '
            '|| -z "$OUTCOME" ]]') in src, (
        "do_verify no longer refuses an empty --goal. Measured consequence: the "
        "clear is still unreachable (aspirations-update-goal.sh refuses the "
        "empty id and set -e aborts first), but the caller now gets an opaque "
        "mid-write abort instead of exit 2 naming the missing flag. See "
        "test_verify_phase_empty_goal_id.py for the executed 2x2.")


def _function_body(name):
    """Executable lines of one top-level shell function, comments stripped.

    guard-2368: a source pin that locates something inside a script must anchor
    on the EXECUTABLE FORM within the region it means, never on a bare
    substring of the whole file. That matters concretely here — do_verify and
    do_recover BOTH guard an empty goal id, so a whole-file `in src` check
    cannot say which one it found, and this file's own module docstring
    discusses the recovery guard in prose that such a check would also match.
    """
    lines = ITERATION_CLOSE.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if ln.strip() == "{}() {{".format(name))
    end = len(lines)
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        # Next top-level function definition closes the region. Top-level = not
        # indented, so a nested heredoc or a `case` body cannot end it early.
        if ln and not ln[0].isspace() and ln.rstrip().endswith("() {"):
            end = i
            break
    return [ln.strip() for ln in lines[start + 1:end]
            if ln.strip() and not ln.strip().startswith("#")]


def test_do_recover_still_refuses_an_empty_checkpoint_goal_id():
    """The RECOVERY-site twin of test_do_verify_still_refuses_an_empty_goal_id.

    Both scoped call sites depend on their goal-id variable being non-empty: an
    empty value is dropped by the wrapper's `-n` check and the clear silently
    reverts to unconditional (the g-306-170 class). The verify site's guarantee
    was pinned when it shipped; the recovery site's — the early return on an
    empty `_gid` — was not, so it could be deleted with every test in this file
    still green (measured, g-306-172). The reasoning existed in the module
    docstring above and only half of it had become an assertion.

    MEASURED SCOPE — narrower than an earlier revision of this docstring
    claimed, and named here so the next reader does not have to re-derive it.
    That revision said removal let "the recovery-site CAS degrade to an
    unconditional clear without any test noticing". For the input a reader would
    picture — an ordinary queue plus an empty checkpoint id — that is FALSE.
    `do_recover` has a SECOND barrier: the status probe looks `_gid` up in
    aspirations.jsonl, an empty id matches no goal, `_status` comes back empty,
    the `== completed` branch is not taken, and control reaches a rollback
    branch that issues no clear at all. Deleting this guard against an ordinary
    corpus therefore changes nothing observable.

    The guard is load-bearing for exactly one input: a goal record whose id is
    EMPTY at status `completed`, which holds the status probe open. That is a
    real shape a malformed aspirations.jsonl can carry, so the guard earns its
    place — but for a narrower reason than "any empty checkpoint goal id".
    Executed in `test_recover_phase_execution.py::test_removing_the_guard_lets
    _the_empty_id_clear_fire`, whose first attempt kept a normal corpus and
    could not go red (guard-1856: a control that cannot fail proves nothing).

    Anchored to do_recover's body, and ordered: the guard is worthless if it
    ever moves BELOW the clear it protects, which a presence-only check would
    not notice.
    """
    body = _function_body("do_recover")
    guard = '[[ -z "$_gid" || -z "$_src" ]] && return 0'
    assert guard in body, (
        "do_recover no longer returns early on an empty checkpoint goal id. "
        "Measured consequence: harmless for an ordinary corpus (the status "
        "probe below is a second barrier), but a goal record with an EMPTY id "
        "at status completed now degrades the recovery CAS to an unconditional "
        "clear. See test_recover_phase_execution.py for the executed control. "
        f"do_recover body was: {body}")

    clears = [i for i, ln in enumerate(body)
              if ln.startswith("bash ") and "team-state-clear-in-flight.sh" in ln]
    assert len(clears) == 1, (
        f"expected exactly one clear invocation inside do_recover, found "
        f"{len(clears)}")
    assert body.index(guard) < clears[0], (
        "the empty-_gid guard now runs AFTER the clear it is supposed to "
        "protect, so the clear is reached with an empty goal id")


def test_wrapper_help_and_endpoint_agree_on_the_parameter_name():
    """The wrapper spells the flag `--if-goal` and the endpoint reads the query
    key `if_goal`. A rename on either side is silent: the wrapper would send a
    key nothing reads, and the endpoint would default to unconditional."""
    daemon = (SCRIPTS.parent.parent / "mind_api" / "src" / "world"
              / "team_state_write.py").read_text(encoding="utf-8")
    wrapper = WRAPPER.read_text(encoding="utf-8")
    assert 'ctx.query.get("if_goal")' in daemon
    assert "if_goal=$(rt_url_encode" in wrapper, (
        "the wrapper no longer builds the if_goal query parameter")


# ── THIRD CALL SITE: aspirations-release.sh () ────────────────────
# The claim path SETS the busy signal; release cleared neither surface, so a
# released agent read busy to every partner — and aspirations-select drops a
# partner's in_flight goal from its candidates, so a stale row could suppress
# the released goal from the very partner the release was handing it to.
# Measured twice on cc-03 (echo, 2026-08-06), and on the body surface on cc-08
# (foxtrot, 2026-08-07: alpha held an in_flight_bodies row for 
# claimed ~30h earlier).
ASP_RELEASE = SCRIPTS / "aspirations-release.sh"
CLEAR_BODY_ROW = SCRIPTS / "team-state-clear-body-row.sh"


def _release_lines():
    return ASP_RELEASE.read_text(encoding="utf-8").splitlines()


def test_every_release_success_path_clears_in_flight():
    """EVERY success exit clears, not just the first one.

    This is the pin that matters, because the shape of the script invites the
    regression: the daemon call is made TWICE (once directly, once after
    `rt_try_autospawn`), and each path has its own `exit 0` with its own copy
    of the response-printing block. A fix applied to the obvious first path
    leaves the retry path silently un-cleared — and the retry path is the one
    that runs when the daemon had to be spawned, i.e. exactly the
    cold-start case a human is least likely to exercise by hand.
    """
    lines = _release_lines()
    success = [i for i, ln in enumerate(lines)
               if ln.strip() in ("exit 0", "exit 0;;")]
    assert len(success) >= 2, (
        f"expected at least the two daemon success paths, found {len(success)}: "
        f"{[lines[i] for i in success]}")
    for i in success:
        preceding = [ln for ln in lines[:i] if ln.strip()]
        assert preceding and preceding[-1].strip() == "_clear_in_flight", (
            f"success exit at line {i + 1} ({lines[i].strip()!r}) is not "
            f"preceded by _clear_in_flight — its preceding statement is "
            f"{preceding[-1].strip()!r}. A release on this path leaves the "
            f"busy signal standing.")


def test_release_reducer_clear_is_cas_scoped_to_the_released_goal():
    """The agent-keyed clear passes --if-goal, and that is load-bearing.

    `in_flight` is AGENT-keyed and reducer-owned (g-306-132-d). Release runs
    from reducers AND from worker Bodies, so an unconditional clear here would
    let any Body blank a reducer's live row for an unrelated goal. The CAS is
    what makes calling it unconditionally safe.
    """
    src = ASP_RELEASE.read_text(encoding="utf-8")
    assert "team-state-clear-in-flight.sh" in src
    assert '--if-goal "$GOAL_ID"' in src, (
        "the reducer-side clear is no longer scoped to the released goal; it "
        "can now blank a row belonging to another goal")


def test_release_body_clear_is_goal_conditional_in_the_caller():
    """The body branch tests ownership HERE, because its clearer has no CAS.

    `team-state-clear-body-row.sh` takes only --agent/--sid and its arg loop
    ends in `*) shift;;`, so an invented `--if-goal` would be silently
    discarded and the clear would run unconditionally (guard-1776 — the same
    class this file's point 1 documents for the sibling wrapper). The second
    assertion pins that premise: if clear-body-row ever grows a real --if-goal
    case, this caller-side comparison should move into it and this test should
    be the thing that says so.
    """
    src = ASP_RELEASE.read_text(encoding="utf-8")
    assert 'in_flight_bodies.${MIND_SID}.goal_id' in src, (
        "the body branch no longer reads the row's goal_id, so it can clear a "
        "row belonging to a different goal")
    assert '[ "$_BODY_GOAL" = "$GOAL_ID" ]' in src, (
        "the body-row clear is no longer guarded by a goal-id comparison")

    # ANCHOR TO NON-COMMENT LINES (guard-1099). The unanchored form
    # `"--if-goal)" not in body_clearer` FAILS against a correct script: that
    # clearer's header warns future authors about the swallowed-flag class in
    # prose reading "...where exactly that swallowed --if-goal). Anyone adding
    # a flag here", and the sentence's closing parenthesis makes it match. The
    # test would then order a redundant refactor on the strength of a comment.
    # Same defect CLAUDE.md records for the verify-learning glob check, which
    # counted comments quoting a deleted glob as live code and reported PASS.
    case_lines = [ln for ln in CLEAR_BODY_ROW.read_text(encoding="utf-8").splitlines()
                  if not ln.lstrip().startswith("#")]
    assert not [ln for ln in case_lines if ln.lstrip().startswith("--if-goal)")], (
        "team-state-clear-body-row.sh now handles --if-goal itself — move the "
        "caller-side goal comparison in aspirations-release.sh into the "
        "clearer and update this test")
