"""Branch proof for reducer self-fencing ().

Drives `decide()` directly — it is pure, so every branch is reachable without a
daemon, a second box, or a live lease. Two invariants are under test and they
pull in OPPOSITE directions, which is the whole point of the module:

  * a reducer that is UNAMBIGUOUSLY superseded (the live claim is held by a
    different machine) MUST stand down;
  * a reducer facing any AMBIGUOUS signal — a failed renewal, a daemon blip, an
    unreadable holder — MUST hold, because stopping a healthy loop on a plumbing
    fault is worse than the disease this fixes (guard-1562).

Plus the config invariant T_stepdown < T_takeover, with a non-vacuity proof that
the invariant assertion actually fires when violated.

guard-1165: no module-level os.environ mutation and no sys.modules stubs.
"""

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from reducer_self_fence import (  # noqa: E402
    DEFAULT_STEPDOWN_SECONDS,
    FAILURE_RCS,
    VERDICT_HOLD,
    VERDICT_STAND_DOWN,
    decide,
    load_stepdown_seconds,
    parse_machine,
    read_failure_elapsed,
)

T = 1950  # a T_stepdown to drive the duration branches against


# ── OUTCOME 2: the UNAMBIGUOUS different-holder read stands the reducer down ──

def test_live_claim_on_a_different_machine_stands_down():
    r = decide(0, "cc-05", "cc-02", 0, T)
    assert r["verdict"] == VERDICT_STAND_DOWN
    assert r["trigger"] == "different-holder"
    # The reason must NAME both boxes — "superseded" with no machine ids is
    # unactionable for whoever reads the loop's exit.
    assert "cc-05" in r["reason"] and "cc-02" in r["reason"]


def test_different_holder_stands_down_even_with_zero_failure_elapsed():
    """The unambiguous read is decisive ON ITS OWN.

    It must not require a renewal failure to have been observed first: the
    canonical incident is precisely a reducer whose renewals were failing
    SILENTLY while it believed itself healthy.
    """
    assert decide(0, "cc-05", "cc-02", 0, T)["verdict"] == VERDICT_STAND_DOWN


def test_live_claim_on_my_own_machine_holds():
    r = decide(0, "cc-02", "cc-02", 0, T)
    assert r["verdict"] == VERDICT_HOLD
    assert r["trigger"] == "holding"


# ── OUTCOME 3: ambiguity NEVER stops the loop ────────────────────────────────

@pytest.mark.parametrize("rc", list(FAILURE_RCS) + [4])
def test_every_ambiguous_rc_holds_below_the_stepdown_window(rc):
    """A transient renewal FAILURE does not stop the loop — the outcome-3 pin.

    rc=4 is included deliberately: it is DECISIVE for a worker (wind down) and
    must be INERT for a reducer. Copying the worker's treatment across is the
    specific bug this parametrization exists to catch.
    """
    r = decide(rc, None, "cc-02", T - 1, T)
    assert r["verdict"] == VERDICT_HOLD
    assert r["trigger"] == "ambiguous-not-yet-decisive"


@pytest.mark.parametrize("rc", list(FAILURE_RCS) + [4])
def test_every_ambiguous_rc_stands_down_once_the_gap_is_sustained(rc):
    r = decide(rc, None, "cc-02", T, T)
    assert r["verdict"] == VERDICT_STAND_DOWN
    assert r["trigger"] == "sustained-renewal-gap"


def test_a_single_blip_is_not_a_sustained_gap():
    assert decide(1, None, "cc-02", 0, T)["verdict"] == VERDICT_HOLD


def test_stepdown_boundary_is_inclusive():
    """>= not >, so a gap that exactly reaches T_stepdown yields."""
    assert decide(1, None, "cc-02", T - 1, T)["verdict"] == VERDICT_HOLD
    assert decide(1, None, "cc-02", T, T)["verdict"] == VERDICT_STAND_DOWN


def test_unreadable_holder_is_non_discriminating_not_a_takeover():
    """A parse miss must not read as supersession."""
    for observed, me in ((None, "cc-02"), ("cc-02", None), (None, None)):
        r = decide(0, observed, me, 0, T)
        assert r["verdict"] == VERDICT_HOLD
        assert r["trigger"] == "holder-unreadable"


def test_unrecognised_rc_holds():
    r = decide(99, None, "cc-02", 0, T)
    assert r["verdict"] == VERDICT_HOLD
    assert r["trigger"] == "unrecognised-rc"


def test_the_fail_safe_direction_is_the_mirror_of_the_worker_poll():
    """Same rc, same inputs, opposite verdict — asserted against the real
    worker module rather than restated in prose, so a future edit that fuses
    the two modules (or flips one default) fails here.

    rc=4 is the discriminating input: NOT-LIVE is decisive for a worker (it must
    never run without a reducer) and inert for a reducer (nobody was observed
    holding the claim, so it keeps its own).
    """
    from worker_reducer_liveness import VERDICT_WIND_DOWN, decide as worker_decide
    assert worker_decide(4, None, "cc-02", 0)["verdict"] == VERDICT_WIND_DOWN
    assert decide(4, None, "cc-02", 0, T)["verdict"] == VERDICT_HOLD


# ── OUTCOME 1: the config invariant, and proof it is not vacuous ─────────────

def test_config_invariant_stepdown_precedes_takeover():
    """T_stepdown MUST be < T_takeover — classic lease correctness.

    The holder yields before a peer may seize, so the two windows can never
    overlap. This is the OPPOSITE direction from the sibling invariants in the
    same config block (which require the cross-machine value to EXCEED the local
    one), because this one governs when the OWNER stops, not when a PEER may
    start. Before g-306-225 T_stepdown was effectively infinity against a finite
    T_takeover — exactly inverted from safe.
    """
    from owncloud_backend import DEFAULT_RUNNER_STALE_SECONDS
    cfg = SCRIPTS.parent / "config" / "aspirations.yaml"
    stepdown = load_stepdown_seconds(cfg)
    assert isinstance(stepdown, int) and stepdown > 0, (
        "runner_heartbeat.stepdown_seconds missing or invalid in aspirations.yaml")
    assert stepdown < DEFAULT_RUNNER_STALE_SECONDS, (
        f"T_stepdown ({stepdown}s) must be strictly below T_takeover "
        f"({DEFAULT_RUNNER_STALE_SECONDS}s) — a holder that yields no earlier "
        f"than a peer may seize produces the zombie-leader window this goal fixes")


def test_the_config_invariant_is_not_vacuous(tmp_path):
    """The loader must actually REJECT the shapes the invariant depends on.

    Without this, a typo'd key or a renamed block would make
    load_stepdown_seconds return None forever and the invariant above would
    fail loudly rather than silently pass — but a future edit that added a
    fallback default would flip it to a silent pass. Pin the None-returns.
    """
    bad = tmp_path / "bad.yaml"
    bad.write_text("runner_heartbeat:\n  stale_minutes: 60\n", encoding="utf-8")
    assert load_stepdown_seconds(bad) is None, "missing key must not default"
    bad.write_text("runner_heartbeat:\n  stepdown_seconds: 0\n", encoding="utf-8")
    assert load_stepdown_seconds(bad) is None, "non-positive must not pass"
    bad.write_text("runner_heartbeat:\n  stepdown_seconds: nineteen-fifty\n",
                   encoding="utf-8")
    assert load_stepdown_seconds(bad) is None, "non-int must not pass"
    assert load_stepdown_seconds(tmp_path / "does-not-exist.yaml") is None
    # And the real file must NOT be one of those shapes — otherwise the
    # invariant test above is passing on a value nobody set.
    assert load_stepdown_seconds(SCRIPTS.parent / "config" / "aspirations.yaml")


def test_stepdown_matches_the_heartbeat_tick_escalation_threshold():
    """The loud warning and the stand-down must fire at ONE threshold.

    heartbeat-tick.sh escalates at `_HB_STALE / 2`. If the config drifts away
    from that, a reader has two different numbers to reconcile and the stderr
    banner stops meaning "you are about to stand down".
    """
    from owncloud_backend import DEFAULT_RUNNER_STALE_SECONDS
    stepdown = load_stepdown_seconds(SCRIPTS.parent / "config" / "aspirations.yaml")
    assert stepdown == DEFAULT_RUNNER_STALE_SECONDS // 2, (
        f"stepdown_seconds ({stepdown}) has drifted from heartbeat-tick.sh's "
        f"escalation threshold (_HB_STALE / 2 = "
        f"{DEFAULT_RUNNER_STALE_SECONDS // 2})")


# ── evidence readers ─────────────────────────────────────────────────────────

def test_parse_machine_reads_the_real_status_line():
    line = ("[runner-claim] status: LIVE (backend=own-cloud) — 'zeta' is RUNNING "
            "on 'cc-02', heartbeat 272s old (threshold 3900s)")
    assert parse_machine(line) == "cc-02"


@pytest.mark.parametrize("line", [
    "",
    "[runner-claim] status: ABSENT (backend=own-cloud) — no runner claim row",
    "is RUNNING on cc-02",           # unquoted -> refuse rather than guess
    "is RUNNING on 'cc-02",          # unterminated quote
])
def test_parse_machine_returns_none_rather_than_guessing(line):
    assert parse_machine(line) is None


def test_absent_marker_means_the_last_renewal_succeeded(tmp_path):
    assert read_failure_elapsed(tmp_path / "nope", 1_000_000) == 0


def test_marker_elapsed_is_measured_from_first_failure(tmp_path):
    m = tmp_path / "claim-heartbeat-failure"
    m.write_text("first_failed_at=1000\ncount=3\nlast_rc=1\n", encoding="utf-8")
    assert read_failure_elapsed(m, 2950) == 1950


@pytest.mark.parametrize("body", [
    "count=3\n",                      # no first_failed_at at all
    "first_failed_at=\n",             # empty
    "first_failed_at=not-a-number\n",
    "",
])
def test_corrupt_marker_yields_zero_not_a_long_gap(tmp_path, body):
    """An unreadable duration must never be treated as a sustained one —
    that would stand a healthy reducer down on a half-written file."""
    m = tmp_path / "claim-heartbeat-failure"
    m.write_text(body, encoding="utf-8")
    assert read_failure_elapsed(m, 10_000_000) == 0


def test_clock_skew_cannot_manufacture_a_negative_gap(tmp_path):
    m = tmp_path / "claim-heartbeat-failure"
    m.write_text("first_failed_at=5000\n", encoding="utf-8")
    assert read_failure_elapsed(m, 1000) == 0


# ── OUTCOME 4: the ex-reducer derives worker on next /start ──────────────────

def test_start_derives_worker_on_a_held_claim():
    """Outcome 4 is satisfied by the EXISTING role-derivation branch (-a,
    2026-08-03) — this pins the prose so a future edit cannot silently remove it
    and leave a demoted reducer needing a human.

    The behaviour lives in skill pseudocode, not in a callable, so a grep pin is
    the honest instrument: it asserts the branch is still documented, and makes
    no claim to have executed /start.
    """
    md = (SCRIPTS.parents[1] / ".claude" / "skills" / "start" / "SKILL.md").read_text(
        encoding="utf-8")
    assert "--reducer-only" in md, "the refusal flag is gone"
    assert "reducer_only" in md, "the rc=4 branch predicate is gone"
    # The auto-join must remain the DEFAULT (no flag), which is what makes it
    # "without human intervention".
    assert "auto-join" in md or "auto-joins" in md, (
        "start/SKILL.md no longer documents the bare-/start auto-join as a "
        "worker on rc=4 — outcome 4 of g-306-225 has regressed")


# ── MACHINE_ID resolution (the wiring half — a green decide() proves nothing
#    about whether the fence can identify THIS box; guard-1943/guard-2323) ────

def test_machine_id_read_from_env_file(tmp_path):
    from reducer_self_fence import _machine_id_from_env_file as rd
    f = tmp_path / ".env.local"
    f.write_text("STORAGE_BACKEND=own-cloud\nMACHINE_ID=cc-02\nOTHER=x\n", encoding="utf-8")
    assert rd(f) == "cc-02"


def test_machine_id_last_assignment_wins_and_comments_are_stripped(tmp_path):
    from reducer_self_fence import _machine_id_from_env_file as rd
    f = tmp_path / ".env.local"
    f.write_text('MACHINE_ID=old\nMACHINE_ID="cc-05"  # the real one\n', encoding="utf-8")
    assert rd(f) == "cc-05"


@pytest.mark.parametrize("body", [
    "",
    "STORAGE_BACKEND=own-cloud\n",
    "MACHINE_ID=\n",
    "MACHINE_ID_SUFFIX=cc-02\n",   # prefix match must NOT count
    "# MACHINE_ID=cc-02\n",        # commented out
])
def test_machine_id_returns_none_rather_than_guessing(tmp_path, body):
    from reducer_self_fence import _machine_id_from_env_file as rd
    f = tmp_path / ".env.local"
    f.write_text(body, encoding="utf-8")
    assert rd(f) is None


def test_machine_id_missing_file_is_none(tmp_path):
    from reducer_self_fence import _machine_id_from_env_file as rd
    assert rd(tmp_path / "nope") is None


def test_an_unknown_self_id_leaves_the_fence_inert_not_dangerous():
    """The coverage gap must fail SAFE. A box that cannot say who it is holds."""
    r = decide(0, "cc-05", None, 0, T)
    assert r["verdict"] == VERDICT_HOLD
    assert r["trigger"] == "holder-unreadable"


# ── THE FENCE GOVERNS THE REDUCER ONLY ───────────────────────────────────────
# The unit branches above all pass with or without this guard, because decide()
# is correct in isolation; the defect it prevents is applying decide() to a Body
# it does not govern. A cross-box WORKER runs precisely BECAUSE a reducer holds
# the claim elsewhere, so the different-holder branch would stand every worker
# down on its first heartbeat tick. (guard-1943: a green suite certifies the
# FUNCTION, never the WIRING.)

def _run_module(env_overrides):
    import json as _json
    import os as _os
    import subprocess as _sp
    env = dict(_os.environ)
    env.setdefault("STORAGE_BACKEND", "local")
    for k, v in env_overrides.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    p = _sp.run([sys.executable, str(SCRIPTS / "reducer_self_fence.py")],
                capture_output=True, text=True, env=env)
    try:
        return _json.loads(p.stdout)
    except Exception:
        return {"_unparseable": p.stdout, "_stderr": p.stderr, "_rc": p.returncode}


def test_no_bound_agent_holds():
    out = _run_module({"MIND_AGENT": None})
    assert out.get("verdict") == VERDICT_HOLD
    assert out.get("trigger") == "no-agent"


def test_an_unreadable_body_role_holds_rather_than_guessing():
    """No MIND_SID -> cannot tell reducer from worker -> hold.

    Reachable without fabricating any session state, and it exercises the real
    main() path (agent resolution, MACHINE_ID resolution, config load) up to the
    role check — so a regression that moved the role guard after the poll would
    change this verdict.
    """
    out = _run_module({"MIND_AGENT": "zeta", "MIND_SID": None})
    assert out.get("verdict") == VERDICT_HOLD
    assert out.get("trigger") == "body-role-unreadable"


def test_the_worker_guard_precedes_the_poll_in_source():
    """Source-contract pin for the branch that CANNOT be exercised safely here.

    Proving `not-the-reducer` behaviourally would mean creating a forked
    per-session working-memory.yaml under the live agent dir — which is exactly
    the artifact that makes the REAL system treat this Body as a worker. The
    honest instrument is therefore an ordering pin on the source: the role guard
    must appear before the poll, and it must key on the forked per-session WM.
    """
    src = (SCRIPTS / "reducer_self_fence.py").read_text(encoding="utf-8")
    guard = src.find('"not-the-reducer"')
    poll = src.find("out = check(")
    assert guard > 0, "the worker guard is gone — every cross-box worker would self-fence"
    assert poll > 0, "check() call not found; this pin needs updating"
    assert guard < poll, "the worker guard must run BEFORE the poll, not after"
    assert 'sessions" / sid / "working-memory.yaml"' in src, (
        "the reducer predicate must key on the forked per-session working memory "
        "(the same predicate bash-agent-inject and worker_reducer_liveness use)")


def test_the_two_fences_never_both_fire():
    """The reducer fence and the worker poll are complements, and the shared
    input that would make them collide is a cross-box worker: the worker poll
    must WIND DOWN only when no reducer is live, while this fence must never
    reach a verdict for a worker at all. Pin the module-level divergence so a
    future merge of the two cannot silently make a worker self-fence."""
    src = (SCRIPTS / "reducer_self_fence.py").read_text(encoding="utf-8")
    wsrc = (SCRIPTS / "worker_reducer_liveness.py").read_text(encoding="utf-8")
    assert "VERDICT_STAND_DOWN" in src and "VERDICT_WIND_DOWN" not in src, (
        "the reducer fence must not import or emit the worker's verdict vocabulary")
    assert "VERDICT_WIND_DOWN" in wsrc and "VERDICT_STAND_DOWN" not in wsrc


# ── : THE INTEGRATION PATH — wiring, write ORDER, revert, containment ─
#
# Everything above certifies decide() and its readers. None of it touches
# reducer-self-fence.sh, which owns the WRITE. So the fence could be unwired from
# its only caller, or write its two files in the wrong order, or resolve a session
# dir at the filesystem root, with this whole suite green.
# guard-1943 / guard-2323 / guard-984: a green suite certifies the FUNCTION, never
# the WIRING.

import os          # noqa: E402
import shutil      # noqa: E402
import subprocess  # noqa: E402

from _runtime_bash import bash_cmd  # noqa: E402

FENCE_SH = SCRIPTS / "reducer-self-fence.sh"


def _tick_invokes_fence(src: str) -> bool:
    """THE predicate — shared by the pin and its mutation proof.

    Factored deliberately: a mutation test carrying its own inline predicate
    proves only that *that* predicate is mutable, not that the one guarding
    production is (guard-1475).
    """
    return any(
        "reducer-self-fence.sh" in line and line.lstrip().startswith("bash ")
        for line in src.splitlines()
        if not line.lstrip().startswith("#")
    )


def test_heartbeat_tick_still_invokes_the_fence():
    """The fence has exactly ONE caller. Deleting that line makes it 100% inert
    on every box while every unit test above keeps passing — the failure shape
    read-before-edit.md records for pre-edit-context-gate.sh, which was declared
    fixed twice while inert."""
    assert _tick_invokes_fence((SCRIPTS / "heartbeat-tick.sh").read_text(encoding="utf-8")), (
        "heartbeat-tick.sh no longer invokes reducer-self-fence.sh — the fence is "
        "unwired and cannot fire anywhere")


def test_the_wiring_pin_is_not_vacuous():
    """Mutation proof: strip the call from a COPY, and the pin must go RED."""
    src = (SCRIPTS / "heartbeat-tick.sh").read_text(encoding="utf-8")
    mutated = "\n".join(
        line for line in src.splitlines()
        if not ("reducer-self-fence.sh" in line and line.lstrip().startswith("bash "))
    )
    assert mutated != src, "mutation removed nothing; this proof is not exercising anything"
    assert not _tick_invokes_fence(mutated), (
        "the pin survives deletion of the invocation — it matches something other "
        "than the call itself")


def test_the_pin_distinguishes_a_live_call_from_a_commented_out_one():
    """Positive and negative control on the predicate itself. Commenting a call
    out is the likeliest way it dies, and it leaves the filename in place — so a
    substring pin would not notice."""
    live = '    bash "$(dirname "$0")/reducer-self-fence.sh" || true'
    assert _tick_invokes_fence(live)
    assert not _tick_invokes_fence("    # " + live.lstrip())


# ── the stand-down WRITE path, driven hermetically ───────────────────────────
# Running the real wrapper against the live session dir would set stop-requested
# on THIS box, so every behavioural test below runs a copy inside a tmp tree with
# a stub for each sibling it shells out to. The layout mirrors the real one
# because the wrapper derives PROJECT_ROOT as SCRIPT_DIR/../.. for containment.

def _sandbox(tmp_path, *, paths="good", signal_rc=0, verdict="stand-down"):
    root = tmp_path / "root"
    scripts = root / "core" / "scripts"
    scripts.mkdir(parents=True)
    session = root / "agents" / "ag" / "session"

    shutil.copy(FENCE_SH, scripts / "reducer-self-fence.sh")

    # no stop in progress, so the wrapper proceeds past its idempotence check
    (scripts / "session-signal-exists.sh").write_text(
        "#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")

    if paths == "good":
        (scripts / "_paths.sh").write_text(
            'agent_dir() { echo "%s/agents/$1"; }\n' % root.as_posix(), encoding="utf-8")
    elif paths == "undefined":
        pass  # no _paths.sh at all -> source fails -> agent_dir undefined (F-001)
    elif paths == "escapes":
        # resolves to "" so SESSION_DIR becomes the filesystem-root "/session"
        (scripts / "_paths.sh").write_text('agent_dir() { echo ""; }\n', encoding="utf-8")
    elif paths == "outside":
        # A well-formed path that is simply NOT under PROJECT_ROOT. Same defect
        # class as "escapes", but it lands in tmp instead of at "/" — so this is
        # the variant the mutation proof can safely disable the guard against.
        (scripts / "_paths.sh").write_text(
            'agent_dir() { echo "%s/outside/agents/$1"; }\n' % tmp_path.as_posix(),
            encoding="utf-8")

    (scripts / "_runtime.sh").write_text(
        'rt_python_launcher() { echo "%s"; }\n' % sys.executable, encoding="utf-8")
    (scripts / "reducer_self_fence.py").write_text(
        "import json\n"
        "print(json.dumps({'verdict': %r, 'trigger': 'different-holder',"
        " 'reason': 'sandbox'}))\n" % verdict,
        encoding="utf-8")

    # The ORDER WITNESS. It records whether stop-target-mode existed AT THE MOMENT
    # the signal write was attempted — the only way to observe ordering from
    # outside. Inspecting the files afterwards cannot distinguish the two orders,
    # because both leave the same end state.
    (scripts / "session-signal-set.sh").write_text(
        "#!/usr/bin/env bash\n"
        'if [ -f "%s/stop-target-mode" ]; then echo present > "%s/order-witness"; '
        'else echo ABSENT > "%s/order-witness"; fi\n'
        "exit %d\n" % (session.as_posix(), root.as_posix(), root.as_posix(), signal_rc),
        encoding="utf-8")

    for p in scripts.glob("*.sh"):
        p.chmod(0o755)
    return root, scripts, session


def _run_fence(scripts, agent="ag"):
    env = dict(os.environ)
    env["MIND_AGENT"] = agent
    env["STORAGE_BACKEND"] = "local"
    return subprocess.run(
        bash_cmd(str(scripts / "reducer-self-fence.sh")),
        capture_output=True, text=True, env=env)


def test_stand_down_writes_the_target_mode_BEFORE_the_stop_signal(tmp_path):
    """ORDER CRITICAL. /stop Phase -1.4 reads stop-target-mode with NO fallback,
    so a signal set before the target mode exists is a stop nobody can complete.
    Reviewed-only until now."""
    root, scripts, session = _sandbox(tmp_path)
    p = _run_fence(scripts)
    assert p.returncode == 0, p.stderr
    witness = root / "order-witness"
    assert witness.exists(), (
        "session-signal-set.sh was never called — the stand-down path did not run, "
        "so this test proved nothing about ordering:\n" + p.stderr)
    assert witness.read_text(encoding="utf-8").strip() == "present", (
        "stop-target-mode did NOT exist when the stop signal was written — /stop "
        "Phase -1.4 would read a missing target mode with no fallback")
    assert (session / "stop-target-mode").read_text(encoding="utf-8") == "assistant"
    assert (session / "reducer-self-fenced").exists(), "the durable marker is missing"


def test_target_mode_is_reverted_when_the_signal_write_fails(tmp_path):
    """No dangling target mode may survive a stop that is not happening — the
    next reader would see a stop in progress that nobody requested."""
    root, scripts, session = _sandbox(tmp_path, signal_rc=1)
    p = _run_fence(scripts)
    assert p.returncode == 0, "the fence must stay fail-open on a failed signal write"
    assert (root / "order-witness").exists(), "the signal write was never attempted"
    assert not (session / "stop-target-mode").exists(), (
        "stop-target-mode survived a FAILED stop-signal write — a stop that is not "
        "happening now looks like one that is")
    assert "SPLIT-BRAIN RISK" in p.stderr, "the revert must be announced, not silent"


def test_a_hold_verdict_writes_nothing(tmp_path):
    """Non-vacuity for the two tests above: they only mean something if a HOLD
    leaves the same files absent."""
    root, scripts, session = _sandbox(tmp_path, verdict="hold")
    p = _run_fence(scripts)
    assert p.returncode == 0
    assert not (root / "order-witness").exists(), "a HOLD must not touch the stop signal"
    assert not (session / "stop-target-mode").exists()
    assert not (session / "reducer-self-fenced").exists()


# ── F-001: the containment guard (fresh-eyes review, zeta 2026-08-05) ────────

@pytest.mark.parametrize("paths", ["undefined", "escapes"])
def test_an_unresolvable_paths_helper_writes_nothing_anywhere(tmp_path, paths):
    """`source _paths.sh 2>/dev/null || true` lets the helper fail SILENTLY,
    leaving agent_dir undefined; SESSION_DIR then becomes the filesystem-root
    path "/session". Measured on cc-02 and independently on cc-08: as root with a
    writable /, the mkdir SUCCEEDS, so the stand-down wrote stop-target-mode to
    /session while the signal was set in the REAL session dir — the signal SET
    with no target mode where /stop reads it, which is exactly the split the
    ORDER-CRITICAL block exists to prevent, defeated two lines above it.
    """
    root, scripts, session = _sandbox(tmp_path, paths=paths)
    p = _run_fence(scripts)
    assert p.returncode == 0, "containment must stay fail-open"
    assert not (root / "order-witness").exists(), (
        "the stop signal was written despite an unresolvable session dir")
    assert not (session / "stop-target-mode").exists()
    assert "UNDECIDABLE" in p.stderr, (
        "an unresolvable session dir must announce itself, not exit silently")
    assert not Path("/session").exists(), (
        "the fence created /session at the filesystem root — F-001 has regressed")


def test_containment_refuses_a_session_dir_outside_project_root(tmp_path):
    """The general form of F-001: a well-formed path that is simply not under
    PROJECT_ROOT. Kept separate from the two cases above because the escaping
    path here lands in tmp rather than at "/", which makes it the variant a
    mutation proof can safely disable the guard against."""
    root, scripts, _ = _sandbox(tmp_path, paths="outside")
    p = _run_fence(scripts)
    assert p.returncode == 0
    assert not (root / "order-witness").exists(), (
        "the stop signal was written for a session dir outside PROJECT_ROOT")
    stray = tmp_path / "outside" / "agents" / "ag" / "session" / "stop-target-mode"
    assert not stray.exists(), f"the fence wrote outside PROJECT_ROOT: {stray}"
    assert "outside PROJECT_ROOT" in p.stderr


def test_the_containment_guard_is_not_vacuous(tmp_path):
    """The same sandbox with a WORKING paths helper must reach the write path.

    Without this, a guard that refused everything would pass the two cases above
    while making the fence permanently inert — the failure this whole section is
    about, reintroduced by the fix for it.
    """
    root, scripts, session = _sandbox(tmp_path, paths="good")
    _run_fence(scripts)
    assert (root / "order-witness").exists() and (session / "stop-target-mode").exists()


def _code_only(src: str) -> str:
    """Strip comment lines before any source-position pin.

    Not defensive boilerplate — earned. The ordering pin below first matched
    `mkdir -p "$SESSION_DIR"` inside the *comment* that quotes it while
    explaining F-001, reporting the guard as mis-ordered when the code was
    correct. A pin that reads prose as code is the same defect this whole
    section exists to catch, so it is fixed at the predicate rather than by
    rewording the comment.
    """
    return "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#"))


def test_the_containment_guard_rejects_a_prefix_lookalike():
    """`<root>-evil/...` must not pass a containment check on `<root>`.

    A naive prefix compare accepts it; the guard strips PROJECT_ROOT *with a
    trailing separator*, so a sibling directory whose name merely starts with
    the root is refused.
    """
    code = _code_only(FENCE_SH.read_text(encoding="utf-8"))
    assert '${SESSION_DIR#"$_PR"/}' in code, (
        "the containment check no longer strips a trailing separator — a sibling "
        "path like <root>-evil/... would be accepted as inside <root>")


def test_the_guard_runs_before_the_session_dir_is_used():
    """Ordering pin: the agent_dir type-check must precede the SESSION_DIR
    assignment, or the very expansion it guards has already happened."""
    code = _code_only(FENCE_SH.read_text(encoding="utf-8"))
    type_check = code.find('type -t agent_dir')
    assign = code.find('SESSION_DIR="$(agent_dir')
    mkdir = code.find('mkdir -p "$SESSION_DIR"')
    assert 0 < type_check < assign < mkdir, (
        f"the agent_dir guard must run before SESSION_DIR is assigned and long "
        f"before it is created (type_check={type_check} assign={assign} "
        f"mkdir={mkdir})")
