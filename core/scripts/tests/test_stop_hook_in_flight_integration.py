"""INTEGRATION path for the worker-close in_flight rail ().

WHAT THIS COVERS THAT THE PRE-EXISTING TESTS DO NOT
---------------------------------------------------
`test_worker_close_in_flight_clear.py` (25 tests) proves the CALLER's decision
table. `test_clear_in_flight_cas.py` (12) proves the store-level CAS and pins the
stop-hook invocation's *source text*. Across the rail there are 105 pre-existing
test functions in 9 files -- and three of those files collect ZERO under pytest
because they are `main()`-style, so their coverage is invisible to a pytest-only
run (guard-1760).

The number that matters is smaller and was measured, not counted: only THREE
files in `core/scripts/tests/` mention `stop-hook.sh` at all, and the two
pre-existing ones make **zero** `subprocess` calls between them -- every
reference is a `.read_text()` assertion or a prose comment. Before this file,
nothing in the repo executed `stop-hook.sh`.

(An earlier revision of this docstring said "26 tests", "(20 tests)", "(9)", and
"eight files". All four were inherited from the goal title or estimated; all
four were wrong, while the load-bearing zero around them was right. Re-measured
2026-08-03 on hostname cc-04 -- guard-1476: a statistic in a durable artifact
must be measured, and a correct headline is exactly what stops anyone checking
the numbers supporting it.)

So nothing established that the genuine-close branch RUNS. rb-5146: a test that
reads SOURCE TEXT can prove wiring exists, never that it executes. guard-1451:
structural assertions are never sufficient alone. Both apply to the two
source-level stop-hook pins added by g-306-137 -- they stay green if the whole
branch is unreachable.

This file drives the real chain end to end:

    stop-hook.sh (sid-mismatch -> genuine-close branch)
      -> body-manifest.py close-body-on-genuine   => "marked"
      -> worker_close_in_flight_clear.py --agent --sid
      -> POST /v1/team-state/clear-in-flight (real daemon, real CAS)
      -> the in_flight row is GONE from the world's team-state shard

and asserts on the SIDE EFFECT, not on any string.

WHY THE HOOK ROOT IS A PHYSICAL COPY AND NOT A SYMLINK
------------------------------------------------------
`stop-hook.sh` resolves everything from `$PROJECT_ROOT`, which `_paths.sh`
derives from its own location -- there is no env override. So a hermetic run
needs a tmp project root carrying `core/`.

Symlinking `core/scripts` into tmp DOES work for bash: `_paths.sh` uses
`cd "$SCRIPT_DIR/.." && pwd`, which is LOGICAL, so `PROJECT_ROOT` comes back as
the tmp dir. It is a trap. `_paths.py` and `body-manifest.py` use
`Path(__file__).resolve()`, which FOLLOWS the symlink, so the PYTHON half of the
same chain resolves `PROJECT_ROOT=/opt/ayoai-mind` -- the REAL repo. Measured
2026-08-03 on this box: the bash pre-guard passed on tmp files, then
`close-body-on-genuine` returned `no-forked-wm` because it looked for the Body
session dir under the production agents tree. Two things make that dangerous
rather than merely wrong: the verdict is INDISTINGUISHABLE from a clean
nothing-to-close run, and had the tmp SID collided with a live session dir the
helper would have marked a REAL Body `closed-pending-merge` and consumed its
sentinel.

The copy costs 24ms for 1132 files (`__pycache__` and `tests` excluded), so the
hazard buys nothing. Any future hermetic harness for a bash+python chain in this
repo should copy for the same reason.

guard-1165: no module-level os.environ mutation, no sys.modules stubs.
guard-955: DaemonFixture hard-pins STORAGE_BACKEND=local.
No `daemon_integration` marker: DaemonFixture is IN-PROCESS with its own
runtime_dir, so this never claims or recycles the live fleet daemon.

Run: STORAGE_BACKEND=local python -m pytest \
       core/scripts/tests/test_stop_hook_in_flight_integration.py -q
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml


def _symlink_available() -> bool:
    """Can THIS process create a directory symlink? ()

    PROBED, never inferred from sys.platform. Windows creates symlinks fine when
    the process holds SeCreateSymbolicLinkPrivilege (developer mode, or
    elevated), so `os.name == "nt"` would skip boxes that are perfectly capable
    — and no platform check catches a Linux container running with that
    capability dropped. Trying it is the only answer that is true on the box
    doing the asking (guard-326).

    Directory-flavoured because that is exactly what the test below creates.
    """
    try:
        with tempfile.TemporaryDirectory() as probe:
            target = Path(probe) / "target"
            target.mkdir()
            (Path(probe) / "link").symlink_to(target, target_is_directory=True)
        return True
    except (OSError, NotImplementedError, AttributeError):
        return False


_SYMLINKS = _symlink_available()

# One .parent per level, each NAMED for the level it lands on. guard-1037 warns
# that a test here needs FOUR .parent's and that an off-by-one lands on
# `<repo>/core`, after which every `ROOT/"core"/...` join silently doubles
# (`core/core/config`) and fails a downstream exists(). Neither half of that can
# happen here: naming each level makes a miscount visible on the page, and the
# only consumer of CORE is `_copy_core`'s copytree(CORE/"scripts") -- so a wrong
# root raises FileNotFoundError on the FIRST test rather than failing quietly
# later. guard-1037's literal prescription is the `_find_repo_root()` marker
# search; measured 2026-08-03, that is not a shared helper -- it is defined in 6
# test files and imported by none, each copy anchored on its own test's
# dependencies. Copying a nine-anchor BATCH-3 search into this file would add a
# second unverified thing to keep in sync, not remove one.
TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent
CORE = SCRIPTS.parent
REAL_ROOT = CORE.parent
sys.path.insert(0, str(TESTS))

from _bash_helpers import BASH  # noqa: E402
from _daemon_fixture import DaemonFixture  # noqa: E402

BODY_SID = "body-sid-0000-1111-2222"
REDUCER_SID = "reducer-sid-3333-4444-5555"
AGENT = "alpha"
GOAL = "g-306-162-fixture"

# The one line under test. Kept as a module constant so the mutation proof and
# the positive test cannot drift apart.
INVOCATION = "worker_close_in_flight_clear.py"

# Same discipline for the worker-net branch (): the `elif` IS the
# mechanism, because it is what makes a genuine close fall through to the
# pre-existing ALLOW instead of being trapped. Mutating this string is therefore
# a faithful "the net was removed" proof, not a cosmetic edit.
WORKER_NET = 'elif [ -f "$_BODY_WM" ]; then'


def _copy_core(dest_root: Path) -> None:
    """Physical copy of core/scripts + core/config (see module docstring).

    ~1132 files / 16MB / ~25ms with `__pycache__` and `tests` excluded. Uses
    copytree rather than a tar round-trip: `TarFile.extractall` without an
    explicit `filter=` is deprecated in 3.12 and changes default in 3.14, and
    the deprecation warning fired on every one of these tests.
    """
    (dest_root / "core" / "logs").mkdir(parents=True, exist_ok=True)
    ignore = shutil.ignore_patterns("__pycache__", "tests")
    for name in ("scripts", "config"):
        shutil.copytree(CORE / name, dest_root / "core" / name,
                        ignore=ignore, symlinks=False)


def _build_hook_root(tmp: Path, world: Path, meta: Path,
                     closing: bool = True, runner_file: bool = True,
                     body_wm: bool = True,
                     body_state: str = "active") -> Path:
    """A tmp PROJECT_ROOT the hook can resolve, with a worker Body mid-close.

    `closing=False` omits ONLY the body-closing sentinel, which is the exact
    difference between the two branches under the sid-mismatch gate: with it the
    Body is closing (genuine close, ALLOW); without it the Body is between work
    units and the g-306-168 worker-net must BLOCK. Keeping every other artifact
    identical is what makes the pair a positive control (guard-2250) rather than
    two unrelated fixtures.

    `runner_file=False` omits running-session-id ENTIRELY. That is the
    PRODUCTION shape of a cross-box worker (g-306-214): a worker box has no
    local runner, so the file does not exist -- it is not merely a different
    SID. Every fixture here wrote the file unconditionally until then, which
    pinned the whole suite to the sid-MISMATCH shape and left the shape the
    fleet actually runs untested (guard-920). Both shapes are kept: the
    mismatch fixtures still cover a same-box observer/second-terminal, which is
    a real case and a different one.

    `body_wm=False` additionally omits the per-Body working-memory.yaml, i.e. a
    box that never forked a Body. It is the negative control for the hoist: the
    per-Body branch must NOT be entered, so Gate 0's `no-runner` ALLOW still
    fires and no py-3 subprocess is spawned.

    `body_state` is written in the QUOTED form _render_manifest actually emits
    (`body_state: 'active'`) — guard-920: the fixture replicates the literal
    production shape, and the worker-net's closed-manifest stand-down greps
    that line.
    """
    root = tmp / "hookroot"
    root.mkdir()
    _copy_core(root)

    adir = root / "agents" / AGENT
    (adir / "session").mkdir(parents=True)
    sess = adir / "sessions" / BODY_SID
    sess.mkdir(parents=True)

    (adir / "local-paths.conf").write_text(
        f"WORLD_PATH={world.as_posix()}\nMETA_PATH={meta.as_posix()}\n",
        encoding="utf-8")
    # running-session-id names the REDUCER, so this Body's SID mismatches and
    # the hook takes the sid-mismatch branch where the close logic lives.
    # Omitted entirely when runner_file=False -- see the docstring: absence is a
    # different state from "present but naming someone else", and the per-Body
    # branch has to reach both.
    if runner_file:
        (adir / "session" / "running-session-id").write_text(REDUCER_SID,
                                                             encoding="utf-8")
    (adir / "session" / "agent-state").write_text("RUNNING", encoding="utf-8")

    (sess / "binding.yaml").write_text(
        f"agent: {AGENT}\nmode: autonomous\n", encoding="utf-8")
    # The two bash pre-guards: a forked Body WM and the genuine-close sentinel.
    if body_wm:
        (sess / "working-memory.yaml").write_text("active_context: null\n",
                                                  encoding="utf-8")
    if closing:
        (sess / "body-closing").write_text("", encoding="utf-8")
    (sess / "body-manifest.yaml").write_text(
        f"sid: {BODY_SID}\nagent: {AGENT}\nenv_id: local\n"
        f"role: worker\nbody_state: '{body_state}'\n", encoding="utf-8")
    return root


def _seed_world(world: Path, claimed_by_sid: str) -> Path:
    """A world whose alpha row is in_flight on GOAL, claimed by claimed_by_sid."""
    (world / "team-state" / "agents").mkdir(parents=True)
    shard = world / "team-state" / "agents" / f"{AGENT}.yaml"
    shard.write_text(yaml.safe_dump({
        "last_active": "2026-08-03T19:00:00",
        "in_flight": {"goal_id": GOAL, "title": "worker work", "phase": "4"},
    }), encoding="utf-8")
    (world / "team-state.yaml").write_text(
        yaml.safe_dump({"agent_status": {}}), encoding="utf-8")

    asp = {
        "id": "asp-306", "title": "fixture", "status": "active",
        "priority": "HIGH",
        "goals": [{
            "id": GOAL, "title": "fixture goal", "status": "in-progress",
            "priority": "MEDIUM", "participants": ["agent"],
            "claimed_by": AGENT, "claimed_by_sid": claimed_by_sid,
        }],
    }
    (world / "aspirations.jsonl").write_text(
        json.dumps(asp) + "\n", encoding="utf-8")
    return shard


def _run_hook(root: Path, runtime_dir: Path,
              scrub_env: bool = False) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["RT_DIR"] = str(runtime_dir)
    env["MIND_SID"] = BODY_SID
    env["MIND_AGENT"] = AGENT
    env["STORAGE_BACKEND"] = "local"
    if scrub_env:
        # PRODUCTION SHAPE. A real Stop event provides NEITHER var -- Gate 0
        # runs before stop-hook.sh exports MIND_AGENT -- so a branch that
        # accidentally depends on either passes here and is inert in production.
        # That is guard-1742 exactly, and it is the failure mode that left
        # pre-edit-context-gate silently dead for 59 days while hand-testing
        # green. The worker-net reads $HOOK_AGENT_DIR (resolved by the hook from
        # the binding), never the env, so it must survive this scrub.
        env.pop("MIND_SID", None)
        env.pop("MIND_AGENT", None)
    return subprocess.run(
        # BASH, not a literal "/bin/bash": that path is not executable by native
        # Windows Python, so this file would hard-FAIL rather than skip on a
        # Windows box. It also must not be a bare "bash" -- on Windows PATH that
        # reaches the System32 WSL launcher (guard-580). _bash_helpers resolves
        # both cases and is the house helper for exactly this ().
        [BASH, str(root / "core" / "scripts" / "stop-hook.sh")],
        input=json.dumps({"session_id": BODY_SID}),
        capture_output=True, text=True, timeout=180, env=env,
    )


def _hook_log(root: Path) -> str:
    p = root / "core" / "logs" / "stop-hook.log"
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def _in_flight(shard: Path):
    return (yaml.safe_load(shard.read_text(encoding="utf-8")) or {}).get("in_flight")


def _drive(tmp_path, claimed_by_sid=BODY_SID, mutate=None, closing=True,
           scrub_env=False, stop_requested=False, runner_file=True,
           body_wm=True, body_state="active",
           session_stop_requested=False, foreign_session_stop=False):
    """Run the whole chain once. `mutate` edits stop-hook.sh before the run.

    `session_stop_requested` writes the SESSION-SCOPED sessions/<BODY_SID>/
    stop-requested that /stop Step 0.6 arms (g-115-7309). Deliberately a
    separate knob from `stop_requested`, which writes the AGENT-WIDE file: they
    are different objects with opposite blast radii, and the worker short-circuit
    is forbidden to write the agent-wide one.

    `foreign_session_stop` writes that same file under a DIFFERENT sid. It is the
    negative control for the scoping — a stop typed on box A must leave box B's
    worker fully netted — and without it a valve that ignored $HOOK_SID entirely
    would pass every other test here.
    """
    world = tmp_path / "world"
    world.mkdir()
    meta = tmp_path / "meta_hook"
    meta.mkdir()
    shard = _seed_world(world, claimed_by_sid)
    root = _build_hook_root(tmp_path, world, meta, closing=closing,
                            runner_file=runner_file, body_wm=body_wm,
                            body_state=body_state)
    if stop_requested:
        # Agent-level (not per-session) -- the same file /stop writes, and the
        # same path the worker-net tests directly.
        (root / "agents" / AGENT / "session" / "stop-requested").write_text(
            "", encoding="utf-8")
    if session_stop_requested:
        (root / "agents" / AGENT / "sessions" / BODY_SID
         / "stop-requested").write_text("", encoding="utf-8")
    if foreign_session_stop:
        other = root / "agents" / AGENT / "sessions" / "some-other-body-sid"
        other.mkdir(parents=True, exist_ok=True)
        (other / "stop-requested").write_text("", encoding="utf-8")
    if mutate is not None:
        hook = root / "core" / "scripts" / "stop-hook.sh"
        hook.write_text(mutate(hook.read_text(encoding="utf-8")),
                        encoding="utf-8")
    with DaemonFixture(world) as df:
        proc = _run_hook(root, df.runtime_dir, scrub_env=scrub_env)
    return proc, shard, root


def _blocked(proc) -> bool:
    """The hook's BLOCK verdict, read from the decision payload it prints."""
    return '"decision": "block"' in (proc.stdout or "")


# ── the integration path ─────────────────────────────────────────────────────

def test_genuine_close_clears_in_flight_through_the_real_chain(tmp_path):
    """The side effect, not the source text.

    Nothing here is monkeypatched: a real bash `stop-hook.sh` runs, its real
    body-manifest delegate marks the close, the real helper POSTs to a real
    daemon, and the assertion reads the world's team-state shard off disk.
    """
    proc, shard, root = _drive(tmp_path)

    assert proc.returncode == 0, f"hook must fail-open: {proc.stderr[-2000:]}"
    log = _hook_log(root)
    assert "genuine-close result=marked" in log, (
        f"the close branch did not mark the Body; log:\n{log}\n"
        f"stderr:\n{proc.stderr[-2000:]}")
    assert "BODY-CLOSE-INFLIGHT" in log, (
        f"the in_flight invocation was never reached; log:\n{log}")

    assert _in_flight(shard) is None, (
        "in_flight survived a genuine worker close -- the rail is inert.\n"
        f"log:\n{log}")


def test_the_helper_reports_cleared_not_absent(tmp_path):
    """`absent` is what a BROKEN invocation looks like -- pin the real verdict.

    A helper that cannot reach the daemon fail-opens to
    {"verdict": "absent"} and the hook logs it exactly like a clean run
    (verify-before-assuming Rule 4). Asserting the row is gone is necessary but
    not sufficient: a world seeded with no row would also pass. This pins that
    the clear was AFFIRMATIVE.
    """
    proc, _shard, root = _drive(tmp_path)
    log = _hook_log(root)
    assert '"verdict": "cleared"' in log, (
        f"expected an affirmative clear, got:\n{log}\n"
        f"stderr:\n{proc.stderr[-2000:]}")


def test_a_foreign_bodys_row_is_left_alone(tmp_path):
    """Negative control -- the ownership test must still refuse.

    Same chain, same close, but the goal was claimed by a DIFFERENT Body. The
    row belongs to a live sibling and must survive: in_flight is agent-keyed
    with no sid, so an unconditional clear here would blank a working reducer's
    row and make a live agent read idle fleet-wide (rb-6498).
    """
    proc, shard, root = _drive(tmp_path, claimed_by_sid="some-other-body-sid")
    log = _hook_log(root)

    assert "BODY-CLOSE-INFLIGHT" in log, f"invocation not reached; log:\n{log}"
    assert '"verdict": "not-ours"' in log, (
        f"expected not-ours for a foreign claim; log:\n{log}")
    assert _in_flight(shard) == {"goal_id": GOAL, "title": "worker work",
                                 "phase": "4"}, (
        "a live foreign in_flight row was destroyed by a worker close")
    assert proc.returncode == 0


# ── mutation proofs (guard-1451: the test must be able to FAIL) ──────────────

def test_mutation_removing_the_invocation_turns_this_red(tmp_path):
    """Neutralize the invocation: the row must survive.

    This is the discriminator the source-text pins cannot supply. If the row is
    still cleared with the call gone, the positive test above has stopped
    depending on the invocation and proves nothing.
    """
    def _strip(src: str) -> str:
        # Neutralize the CALL, keep the assignment. Deleting the whole line
        # leaves the following `result=$_IF_RESULT` / `unset _IF_RESULT` under
        # `set -u` with an unbound variable, so the hook dies rc=1 -- which
        # would "pass" this test for the wrong reason (a dead hook clears
        # nothing either). Measured on the first run of this file: the mutation
        # must change ONLY the thing under test.
        out, hit = [], 0
        for ln in src.splitlines(keepends=True):
            if INVOCATION in ln and "_IF_RESULT=$(" in ln:
                out.append('                _IF_RESULT=$(true)\n')
                hit += 1
            else:
                out.append(ln)
        assert hit == 1, f"expected exactly 1 invocation line, found {hit}"
        return "".join(out)

    proc, shard, root = _drive(tmp_path, mutate=_strip)

    assert proc.returncode == 0
    assert "genuine-close result=marked" in _hook_log(root), (
        "the mutation must remove ONLY the in_flight invocation, not the "
        "close branch itself -- otherwise this proves nothing about the rail")
    assert _in_flight(shard) is not None, (
        "in_flight was cleared with the invocation neutralized -- something "
        "ELSE is clearing it, so the positive test is not testing this rail")


def test_mutation_wrong_sid_arg_shape_turns_this_red(tmp_path):
    """guard-920: pin the production ARG SHAPE, not just the call's presence.

    Passing a wrong `--sid` leaves the helper unable to match `claimed_by_sid`,
    so the ownership test correctly declines. The row surviving here is what
    proves the positive test depends on the REAL sid reaching the helper.
    """
    def _break_sid(src: str) -> str:
        # LINE-SCOPED. `--sid "$HOOK_SID"` appears on the body-manifest
        # close-body-on-genuine call too, and a whole-file str.replace broke
        # THAT one instead: the close returned `no-forked-wm`, the chain never
        # reached the invocation, and the row survived for a reason that had
        # nothing to do with the arg shape. Measured on the first run of this
        # file -- a shared arg-shape string is exactly what a naive mutation
        # gets wrong, and the wrong mutation still produces a green test.
        old = '--sid "$HOOK_SID"'
        out, hit = [], 0
        for ln in src.splitlines(keepends=True):
            if INVOCATION in ln:
                assert old in ln, ("production arg shape changed on the "
                                   "invocation line -- re-derive this pin")
                out.append(ln.replace(old, '--sid "not-the-body-sid"'))
                hit += 1
            else:
                out.append(ln)
        assert hit == 1, f"expected exactly 1 invocation line, found {hit}"
        return "".join(out)

    proc, shard, root = _drive(tmp_path, mutate=_break_sid)

    assert proc.returncode == 0
    assert '"verdict": "not-ours"' in _hook_log(root)
    assert _in_flight(shard) is not None, (
        "the row was cleared despite a wrong --sid -- the ownership test is "
        "not actually gating on the value the hook passes")


# ── worker-net: the resurrection BLOCK for a Body between work units ─────────
#
# . Gate 0 is runner-keyed, so a worker Body is sid-mismatched BY
# DEFINITION and every worker turn-end took the ALLOW -- a text-death between
# work units ended an unattended worker silently. These three tests pin the
# three-way branch: BLOCK between work units, ALLOW on a genuine close, ALLOW on
# stop-requested. Each varies exactly ONE artifact from the same fixture.


def test_worker_net_blocks_a_turn_end_between_work_units(tmp_path):
    """The net itself, in the env a real Stop event provides.

    `scrub_env` is the load-bearing half: the branch runs BEFORE stop-hook.sh
    exports MIND_AGENT, so reading the env here would hand-test green and be
    inert in production (guard-1742). Passing with both vars removed is what
    proves it reads $HOOK_AGENT_DIR instead.
    """
    proc, _shard, root = _drive(tmp_path, closing=False, scrub_env=True)

    assert proc.returncode == 0, f"hook must fail-open: {proc.stderr[-2000:]}"
    assert _blocked(proc), (
        "a worker Body ended its turn between work units and the hook ALLOWED "
        f"it -- the resurrection net is inert.\nstdout:\n{proc.stdout}\n"
        f"log:\n{_hook_log(root)}")
    assert "worker-loop" in proc.stdout, (
        "the BLOCK fired but does not name Skill('worker-loop') -- a worker "
        "told to re-enter the REDUCER loop is worse than no net at all "
        f"(guard-517/guard-463).\nstdout:\n{proc.stdout}")
    assert "gate=worker-net" in _hook_log(root), (
        f"the decision was not logged as the worker net; log:\n{_hook_log(root)}")


def test_worker_net_does_not_trap_a_genuine_close(tmp_path):
    """The `elif` is the safety property, so it gets its own assertion.

    The sibling test above proves a genuine close CLEARS in_flight; this proves
    the same close is not BLOCKED on its way there. Those are different failure
    modes: a trapped close would still clear the row and then refuse to end.
    """
    proc, _shard, root = _drive(tmp_path, closing=True)

    assert proc.returncode == 0
    assert not _blocked(proc), (
        "a genuine close (body-closing present) was trapped by the worker net "
        "-- the branch must be an `elif`, so the close reaches the ALLOW.\n"
        f"stdout:\n{proc.stdout}")


def test_worker_net_stands_down_on_stop_requested(tmp_path):
    """The user asked the agent to stop; a net that overrides that is a trap."""
    proc, _shard, root = _drive(tmp_path, closing=False, stop_requested=True)

    assert proc.returncode == 0
    assert not _blocked(proc), (
        "stop-requested was set and the worker net blocked anyway -- the user "
        f"cannot stop a worker.\nstdout:\n{proc.stdout}")
    # TRAILING SPACE IS LOAD-BEARING (): the session-scoped valve's
    # gate name `worker-net-stop-requested-session` CONTAINS this one as a
    # prefix, so a bare substring test would pass on either valve and this test
    # would stop discriminating the moment the sibling valve landed.
    assert "gate=worker-net-stop-requested " in _hook_log(root), (
        f"the stand-down was not logged; log:\n{_hook_log(root)}")


def test_worker_net_stands_down_on_session_scoped_stop_requested(tmp_path):
    """A /stop typed on a WORKER box must be able to end its own turn.

    THE DEFECT THIS PINS (guard-4900, fixed by g-115-7309): valve #2 read only
    the AGENT-WIDE session/stop-requested, and /stop Step 0.6 is forbidden to
    write it -- writing it would stop the REDUCER on another machine. So the one
    actor that needed the valve was structurally barred from firing it, and every
    worker /stop BLOCKed at turn-end. The only escape was hand-writing
    body-closing, which DURABLY retires the Body.

    Note the fixture sets NO agent-wide stop-requested, so a pass here cannot be
    coming from the sibling valve.
    """
    proc, _shard, root = _drive(tmp_path, closing=False,
                                session_stop_requested=True)
    log = _hook_log(root)

    assert proc.returncode == 0, f"hook must fail-open: {proc.stderr[-2000:]}"
    assert not _blocked(proc), (
        "a session-scoped stop was armed and the worker net blocked anyway -- "
        f"the user still cannot stop this box.\nstdout:\n{proc.stdout}\nlog:\n{log}")
    assert "gate=worker-net-stop-requested-session" in log, (
        f"the stand-down was not logged as the session-scoped valve; log:\n{log}")


def test_session_scoped_stop_does_not_retire_the_body(tmp_path):
    """The whole point of the fix: stopping one box is not retiring the Body.

    body-closing would ALSO clear the net, which is why it was the observed
    workaround -- but it flips body_state to closed-pending-merge, after which
    worker-loop Phase -0 refuses every further unit on that SID and only a
    user-only /start reopens it. This asserts the cheaper signal does NOT do that.
    """
    proc, _shard, root = _drive(tmp_path, closing=False,
                                session_stop_requested=True)
    manifest = (root / "agents" / AGENT / "sessions" / BODY_SID
                / "body-manifest.yaml").read_text(encoding="utf-8")

    assert not _blocked(proc)
    assert "body_state: 'active'" in manifest, (
        "the session-scoped stop retired the Body -- it must only stand the net "
        f"down, never close.\nmanifest:\n{manifest}")
    assert not (root / "agents" / AGENT / "sessions" / BODY_SID
                / "body-closing").exists(), (
        "a body-closing sentinel appeared; the session-scoped path must not "
        "invent one")


def test_session_scoped_stop_does_not_leak_to_a_sibling_body(tmp_path):
    """A stop typed on box A must leave box B's worker fully netted.

    The negative control for `[ -n "$HOOK_SID" ] && [ -f .../$HOOK_SID/... ]`.
    A valve that globbed sessions/*/stop-requested, or that ignored $HOOK_SID,
    would pass every other test in this file and silently un-net every sibling
    Body of the same agent -- which is the reducer-stopping blast radius the
    short-circuit exists to prevent, reintroduced one directory down.
    """
    proc, _shard, root = _drive(tmp_path, closing=False,
                                foreign_session_stop=True)
    log = _hook_log(root)

    assert proc.returncode == 0, f"hook must fail-open: {proc.stderr[-2000:]}"
    assert _blocked(proc), (
        "another Body's stop-requested stood this Body's net down -- the valve "
        f"is not scoped to $HOOK_SID.\nstdout:\n{proc.stdout}\nlog:\n{log}")
    assert "gate=worker-net-stop-requested-session" not in log, (
        f"the session valve fired for a foreign sid; log:\n{log}")


def test_mutation_removing_the_session_valve_turns_this_red(tmp_path):
    """Mutation proof: the new elif IS the mechanism, not decoration.

    Same discipline as test_mutation_neutralizing_the_worker_net_turns_this_red
    -- without this, a rename or an accidental deletion would leave the three
    tests above passing against whatever ELSE happens to ALLOW (rb-5146: source
    text proves wiring exists, not that it runs).
    """
    def _kill(text):
        needle = ('elif [ -n "$HOOK_SID" ] && '
                  '[ -f "$HOOK_AGENT_DIR/sessions/$HOOK_SID/stop-requested" ]; then')
        assert needle in text, "the session valve line moved; update this proof"
        return text.replace(needle, 'elif false; then')

    proc, _shard, root = _drive(tmp_path, closing=False,
                                session_stop_requested=True, mutate=_kill)

    assert _blocked(proc), (
        "neutralizing the session valve did NOT turn this red -- the tests above "
        f"are passing for some other reason.\nstdout:\n{proc.stdout}")


def test_worker_net_stands_down_on_closed_manifest(tmp_path):
    """A GENUINELY-CLOSED Body's later turn-end must ALLOW, not BLOCK.

    The post-close shape (2026-08-09, cc-08 04:39→04:49): close-body-on-genuine
    CONSUMED the body-closing sentinel and the fork WM survives the close, so
    to the net's own discriminators this is indistinguishable from a
    between-units text-death — while body-manifest.yaml body_state says
    closed. The deadman wakeup armed by the last work unit fires ~600s after
    EVERY genuine close, so without the manifest stand-down every close was
    followed by a BLOCK that coerced a pointless second sentinel ceremony
    ('not-active' noop).

    `runner_file=False` + `scrub_env=True` is the literal cross-box production
    shape the incident ran in (guard-920). The discriminator pair is
    test_worker_net_blocks_when_there_is_no_runner_file: identical fixture,
    body_state 'active' → BLOCK; here 'closed-pending-merge' → ALLOW. The
    fixture writes the quoted form _render_manifest emits.
    """
    proc, _shard, root = _drive(tmp_path, closing=False, runner_file=False,
                                scrub_env=True,
                                body_state="closed-pending-merge")
    log = _hook_log(root)

    assert proc.returncode == 0, f"hook must fail-open: {proc.stderr[-2000:]}"
    assert not _blocked(proc), (
        "a closed Body's turn-end was BLOCKED — every post-close deadman "
        "firing gets trapped into a second sentinel ceremony.\n"
        f"stdout:\n{proc.stdout}\nlog:\n{log}")
    assert "gate=worker-net-body-closed" in log, (
        f"the stand-down was not logged as the closed-manifest valve; "
        f"log:\n{log}")
    assert "BLOCK gate=worker-net" not in log, (
        f"the net still BLOCKed despite the closed manifest; log:\n{log}")


def test_mutation_neutralizing_the_worker_net_turns_this_red(tmp_path):
    """Remove the net; the between-work-units turn-end must go back to ALLOW.

    LINE-SCOPED and `hit == 1`, for the reason the sibling mutations document:
    `$_BODY_WM` appears on the genuine-close `if` too, and a whole-file replace
    would disable BOTH branches -- which still produces a green test, for the
    wrong reason. `elif false` keeps the block syntactically intact under
    `set -u`, so only the predicate changes.
    """
    def _kill_net(src: str) -> str:
        out, hit = [], 0
        for ln in src.splitlines(keepends=True):
            if WORKER_NET in ln:
                out.append(ln.replace(WORKER_NET, "elif false; then"))
                hit += 1
            else:
                out.append(ln)
        assert hit == 1, f"expected exactly 1 worker-net line, found {hit}"
        return "".join(out)

    proc, _shard, root = _drive(tmp_path, closing=False, mutate=_kill_net)

    assert proc.returncode == 0
    assert not _blocked(proc), (
        "the turn-end was still BLOCKED with the worker net neutralized -- "
        "something ELSE is blocking, so the positive test above is not testing "
        f"this branch.\nstdout:\n{proc.stdout}")


# ── no-runner-file: the shape a cross-box worker actually runs in ────────────
#
# . Everything above this line writes running-session-id, so the whole
# suite ran on the sid-MISMATCH shape. A worker box has no local runner at all,
# so `[ -f "$RUNNER_FILE" ]` is FALSE there -- and until the hoist, Gate 0's
# `exit 0` fired before the per-Body branch was ever evaluated. Both producers
# were unreachable in production while every test here was green, which is
# guard-920 in one sentence: the fixtures replicated a contract-ideal arg shape
# instead of the literal production one.
#
# These three vary ONE artifact from the fixtures above -- the presence of the
# runner file -- so a failure localises to the hoist and not to the branch
# bodies, which the sid-mismatch tests already pin.


def test_worker_net_blocks_when_there_is_no_runner_file(tmp_path):
    """Outcome 1: the resurrection net on the shape the fleet actually runs.

    `scrub_env=True` for the guard-1742 reason its sid-mismatch twin documents:
    the branch runs before stop-hook.sh exports MIND_AGENT, so it must resolve
    the agent dir from the binding, not the env. Both halves matter here --
    a branch that is reachable but env-dependent is still inert in production.
    """
    proc, _shard, root = _drive(tmp_path, closing=False, runner_file=False,
                                scrub_env=True)

    assert proc.returncode == 0, f"hook must fail-open: {proc.stderr[-2000:]}"
    assert _blocked(proc), (
        "a worker Body on a box with NO running-session-id ended its turn "
        "between work units and the hook ALLOWED it -- the per-Body branch is "
        "still below Gate 0's no-runner exit, so the net is inert in exactly "
        f"the shape production runs.\nstdout:\n{proc.stdout}\n"
        f"log:\n{_hook_log(root)}")
    assert "gate=worker-net" in _hook_log(root), (
        f"the decision was not logged as the worker net; log:\n{_hook_log(root)}")
    assert "gate=no-runner" not in _hook_log(root), (
        "Gate 0's no-runner ALLOW was logged as well -- the branch cannot have "
        f"run before it.\nlog:\n{_hook_log(root)}")


def test_close_producer_runs_when_there_is_no_runner_file(tmp_path):
    """Outcome 2: a GENUINE worker close is not stranded on a no-runner box.

    The louder half of the same defect. A net that fails to BLOCK loses a turn;
    a close producer that never runs strands the Body's whole learning payload
    -- manifest left active, nothing staged for the reducer to merge. Measured
    once in soak #2 before the hoist.
    """
    proc, shard, root = _drive(tmp_path, closing=True, runner_file=False)

    assert proc.returncode == 0, f"hook must fail-open: {proc.stderr[-2000:]}"
    log = _hook_log(root)
    assert "genuine-close result=marked" in log, (
        f"the close branch did not mark the Body; log:\n{log}\n"
        f"stderr:\n{proc.stderr[-2000:]}")
    assert "BODY-CLOSE-INFLIGHT" in log, (
        f"the in_flight invocation was never reached; log:\n{log}")
    assert _in_flight(shard) is None, (
        "in_flight survived a genuine worker close on a no-runner box.\n"
        f"log:\n{log}")
    assert not _blocked(proc), (
        f"the genuine close was trapped by the worker net.\nstdout:\n{proc.stdout}")


def test_a_box_that_never_forked_a_body_still_allows(tmp_path):
    """The negative control for the hoist: the dormant path must be untouched.

    Hoisting a branch above an early exit risks widening it. The per-Body WM is
    the signal-level guard that keeps it narrow, so a box with no runner AND no
    forked Body must still take Gate 0's ALLOW -- not the net. Without this,
    both tests above pass just as well if the guard had been dropped entirely,
    which would BLOCK every observer session on every box.
    """
    proc, _shard, root = _drive(tmp_path, closing=False, runner_file=False,
                                body_wm=False, scrub_env=True)

    assert proc.returncode == 0, f"hook must fail-open: {proc.stderr[-2000:]}"
    assert not _blocked(proc), (
        "a box that never forked a Body was BLOCKED -- the hoisted branch lost "
        f"its per-Body guard and now traps every session.\nstdout:\n{proc.stdout}")
    assert "gate=no-runner" in _hook_log(root), (
        "Gate 0's no-runner ALLOW did not fire; the hoist moved the exit "
        f"instead of preceding it.\nlog:\n{_hook_log(root)}")


# ── the harness hazard, pinned so it cannot be reintroduced ──────────────────

@pytest.mark.skipif(
    not _SYMLINKS,
    reason="cannot create a directory symlink here (Windows without "
           "SeCreateSymbolicLinkPrivilege raises WinError 1314). NOT a coverage "
           "hole: this test exists to prove a SYMLINKED core splits the project "
           "root, and where symlinks cannot be created that hazard cannot be "
           "reached — the copy this pins is used unconditionally either way.")
def test_symlinked_core_would_split_the_project_root(tmp_path):
    """Why `_copy_core` copies instead of symlinking (see module docstring).

    bash `cd`+`pwd` is logical; Python `Path(__file__).resolve()` is physical.
    Under a symlinked core they disagree, and the python half of the chain
    silently addresses the REAL repo. Pinned as a test so a future
    "optimization" to a symlink fails loudly here instead of quietly operating
    on production agent dirs.
    """
    root = tmp_path / "symroot"
    (root / "core").mkdir(parents=True)
    # target_is_directory MATTERS, and only on Windows (POSIX ignores it): it
    # selects a directory symlink over a file one. Without it, a privileged
    # Windows box passes _symlink_available (which probes WITH the flag) and
    # then builds a file-flavoured link to a directory here, so the test stops
    # exercising the hazard it pins while still looking like it ran.
    (root / "core" / "scripts").symlink_to(CORE / "scripts",
                                           target_is_directory=True)

    # The `>/dev/null 2>&1` here suppresses _paths.sh's own chatter, and a
    # suppressed command is zero signals (verify-before-assuming Rule 4). It is
    # safe ONLY because the empty-output path below is explicitly caught and
    # turned into a skip -- so a broken source can never render as a PASS.
    bash_root = subprocess.run(
        [BASH, "-c",
         'source "$1/core/scripts/_paths.sh" >/dev/null 2>&1; printf %s "$PROJECT_ROOT"',
         "_", str(root)],
        capture_output=True, text=True, timeout=60).stdout.strip()
    py_root = subprocess.run(
        [sys.executable, "-c",
         "import sys;sys.path.insert(0,sys.argv[1]);"
         "import _paths;print(_paths.PROJECT_ROOT)",
         str(root / "core" / "scripts")],
        capture_output=True, text=True, timeout=60).stdout.strip()

    if not bash_root or not py_root:
        pytest.skip("could not resolve both roots on this platform")
    assert bash_root == str(root), (
        f"bash resolved {bash_root!r}, expected the symlinked tmp root")
    assert Path(py_root) == REAL_ROOT, (
        f"python resolved {py_root!r}, expected the REAL repo root -- if this "
        "now matches bash, the hazard is gone and _copy_core may be simplified")
    assert bash_root != py_root, "the split-brain hazard should still hold"


# ── worker-net valve 5: the PARK stand-down () ─────────────────────
#
# TWO PARK VALVES ONCE EXISTED HERE AND THE FORK IS NOW RESOLVED (
# reducer ruling, 2026-08-17). Both were built for  concurrently, on
# boxes that could not see each other: a SENTINEL valve keyed on a `body-parked`
# file with a 70-minute freshness bound, and this MANIFEST valve keyed on
# `body_state: parked`. git auto-merged stop-hook.sh CLEANLY, which is the
# dangerous case worker-loop/SKILL.md's Phase -0.2 comment warns about — the
# conflicting file announced itself, the harmless-looking one did not. Both
# logged the SAME gate name from different predicates, so the log could not say
# which mechanism parked a Body.
#
# The manifest won on measurement, not preference: nothing ever wrote the
# sentinel file (it was unreachable in production from the day it landed), its
# stated reason to exist was a worker-loop constraint that no longer holds
# (`parked` is non-active AND resumable there now), and the entire park
# lifecycle — worker-loop's park/resume/park-expired calls, deadman-directive's
# resumable branch — is already written against the manifest. The sentinel valve
# and its three tests were REMOVED, not left inert. Full reasoning: the ruling on
# the `decisions` board and the comment block at the valve itself.
#
# These two pin the surviving valve: that it fires, and that it stays DISJOINT
# from the closed valve it sits next to.

def test_worker_net_stands_down_on_a_PARKED_manifest(tmp_path):
    """A PARKED Body's turn-end must ALLOW () — and this valve is what
    makes parking work at all, not a convenience.

    A park deliberately ends the turn with no `Skill(worker-loop)` and no
    body-closing sentinel, so to every pre-existing discriminator it looks
    exactly like a between-units text-death: fork WM present, no sentinel, no
    stop-requested, manifest not in the closed set. It therefore fell through to
    the BLOCK, whose instruction is "write the body-closing sentinel and end the
    turn" — which would durably CLOSE the Body on the very turn it parked, and
    defeat the entire feature. The Body would then need the user-only /start
    that parking exists to remove.

    Discriminator pair, one field apart: the closed-manifest test above
    ('closed-pending-merge' → ALLOW as CLOSED) and
    test_worker_net_blocks_when_there_is_no_runner_file ('active' → BLOCK). This
    is the third value, and it must ALLOW for a DIFFERENT reason than the closed
    one — hence its own gate name in the log. A shared name would make a parked
    Body indistinguishable from a closed one in the only durable record of why
    the turn was let go.

    The fixture writes the quoted form _render_manifest emits (guard-920).
    """
    proc, _shard, root = _drive(tmp_path, closing=False, runner_file=False,
                                scrub_env=True, body_state="parked")
    log = _hook_log(root)

    assert proc.returncode == 0, f"hook must fail-open: {proc.stderr[-2000:]}"
    assert not _blocked(proc), (
        "a PARKED Body's turn-end was BLOCKED — the BLOCK instructs it to write "
        "the body-closing sentinel, which durably closes the Body on the turn "
        "it parked and defeats auto-resume entirely.\n"
        f"stdout:\n{proc.stdout}\nlog:\n{log}")
    assert "gate=worker-net-body-parked" in log, (
        "the stand-down fired under the wrong gate name — parked and closed "
        "must be distinguishable in the log, since only one of them is coming "
        f"back; log:\n{log}")
    assert "BLOCK gate=worker-net" not in log, (
        f"the net still BLOCKed despite the parked manifest; log:\n{log}")


def test_the_parked_and_closed_valves_are_disjoint(tmp_path):
    """Neither valve may swallow the other's state.

    Both greps run against the same manifest line, so a loosened pattern (a
    `parked` matcher that also matches `closed-pending-merge`, or a closed
    matcher widened back to "not active") would silently collapse two verdicts
    into one — and the collapse is invisible: both still ALLOW, so every test
    that only checks the turn-end outcome keeps passing while the log stops
    telling anyone whether the Body is ever coming back.
    """
    (tmp_path / "p").mkdir()
    (tmp_path / "c").mkdir()
    _proc_p, _s, root_p = _drive(tmp_path / "p", closing=False,
                                 runner_file=False, scrub_env=True,
                                 body_state="parked")
    _proc_c, _s2, root_c = _drive(tmp_path / "c", closing=False,
                                  runner_file=False, scrub_env=True,
                                  body_state="closed-pending-merge")
    log_p, log_c = _hook_log(root_p), _hook_log(root_c)
    assert "gate=worker-net-body-parked" in log_p
    assert "gate=worker-net-body-closed" not in log_p, (
        f"the closed valve claimed a parked Body; log:\n{log_p}")
    assert "gate=worker-net-body-closed" in log_c
    assert "gate=worker-net-body-parked" not in log_c, (
        f"the parked valve claimed a closed Body; log:\n{log_c}")


def test_the_parked_valve_arms_the_re_poll_and_the_closed_valve_cancels_it(tmp_path):
    """ / Zak-Code ADR-0102: the hook ARMS the park re-poll itself.

    The park turn is supposed to end on the Body's own ScheduleWakeup(…, 3600);
    measured across 26 zc-03 sessions (2026-08-29) the model armed a wake-up
    ONCE, so a parked Body sat at its prompt like a closed one. A Zak-Code
    harness honours a `wakeup` key in the Stop hook's JSON (arm, replace-slot,
    clamped to 3600 s) and a `cancel`; Claude Code ignores the key. So the parked
    ALLOW must print exactly one JSON document carrying the 3600 s arm with a
    natural-language prompt (never a slash command — schedule-wakeup-correctness),
    and the closed ALLOW must print the cancel, and neither may also print a
    BLOCK. A second document on stdout would turn the whole payload into a
    fail-open parse and lose the arm silently.
    """
    import json as _json
    (tmp_path / "p").mkdir()
    (tmp_path / "c").mkdir()
    proc_p, _s, _root_p = _drive(tmp_path / "p", closing=False,
                                 runner_file=False, scrub_env=True,
                                 body_state="parked")
    proc_c, _s2, _root_c = _drive(tmp_path / "c", closing=False,
                                  runner_file=False, scrub_env=True,
                                  body_state="closed-pending-merge")
    assert proc_p.returncode == 0 and proc_c.returncode == 0
    docs_p = [ln for ln in (proc_p.stdout or "").splitlines() if ln.strip().startswith("{")]
    docs_c = [ln for ln in (proc_c.stdout or "").splitlines() if ln.strip().startswith("{")]
    assert len(docs_p) == 1, f"parked ALLOW must print exactly one JSON document:\n{proc_p.stdout}"
    assert len(docs_c) == 1, f"closed ALLOW must print exactly one JSON document:\n{proc_c.stdout}"
    parked = _json.loads(docs_p[0])
    closed = _json.loads(docs_c[0])
    assert parked.get("decision") != "block" and closed.get("decision") != "block"
    arm = parked["wakeup"]
    assert arm["delay_seconds"] == 3600
    assert isinstance(arm["prompt"], str) and arm["prompt"].strip()
    assert not arm["prompt"].lstrip().startswith("/"), (
        "a slash-prefixed wake-up prompt is rejected as user input at fire time")
    assert "worker-loop" in arm["prompt"] and "parked" in arm["prompt"]
    assert closed["wakeup"] == {"cancel": True}
