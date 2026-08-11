"""EXECUTION coverage for stop-hook Gate 1 (not-RUNNING) and Gate 2.6 (background jobs).

WHAT WAS UNCOVERED, MEASURED NOT ESTIMATED
------------------------------------------
Measured 2026-08-03 (zeta, hostname cc-02, uname -r 6.8.0-136-generic): exactly
THREE files under ``core/scripts/tests`` contain the literal ``stop-hook.sh``.
Two of them -- ``test_clear_in_flight_cas.py`` (3 mentions) and
``test_turn_end_gate_body_filter.py`` (1) -- make ZERO ``subprocess`` calls
between them; every reference is a ``.read_text()`` pin or a prose comment. The
third, ``test_stop_hook_in_flight_integration.py`` (g-306-162), is the only file
in the repo that has ever EXECUTED the hook, and it drives exactly one path:
Gate 0's sid-mismatch branch.

The hook logs nine distinct gate outcomes (``gate=`` no-sid / no-agent /
no-agent-RESOLUTION_GAP / no-runner / sid-mismatch / not-running / stop-loop /
pending-agents / background-jobs, plus BLOCK). One of the nine was executed.
This file adds the two the filing goal (g-306-173) named as highest-stakes:
Gate 1 and Gate 2.6, each with a positive path, a negative control, and a
mutation proof.

WHY THESE TWO ARE NOT ALREADY COVERED BY THE SIBLING UNIT TESTS
---------------------------------------------------------------
``test_turn_end_gate_body_filter.py`` already pins the g-306-135 body filter
thoroughly -- but at the PYTHON level (``cmd_has_pending`` called in-process)
plus one SOURCE-TEXT assertion that the hook's call carries
``--body-sid "$HOOK_SID"``. rb-5146: source text proves wiring exists, never
that it executes. Nothing established that stop-hook.sh REACHES Gate 2.6 at
all -- every gate ahead of it (0, 1, 2, 2.5) has to pass first, and none of
those transitions was executed by any test. That composed seam is what this
file covers; the filter's own decision table is NOT re-tested here.

THE HOOK ENVIRONMENT IS THE PRODUCTION ONE (guard-1742) -- LOAD-BEARING HERE
----------------------------------------------------------------------------
``_run_hook_as_runner`` SCRUBS ``MIND_SID`` and ``MIND_AGENT`` from the child
env. Both are injected only into PreToolUse[Bash] by ``bash-agent-inject.py``;
a Stop event provides NEITHER, and stop-hook.sh:334-338 says so explicitly --
which is precisely why Gates 2.5/2.6 pass ``--body-sid "$HOOK_SID"`` (parsed
from the event payload) rather than reading ``$MIND_SID``.

Setting ``MIND_SID`` here would silently destroy the value of
``test_gate_2_6_sibling_body_job_does_not_allow``: a future edit swapping
``$HOOK_SID`` for ``$MIND_SID`` would keep passing, because the test env would
be supplying the variable production never supplies. With it scrubbed, that edit
resolves ``--body-sid ""``, which ``cmd_has_pending`` treats as "nothing is
mine" (exit 1) -- so the positive test goes red. Verified by probe, not
assumed: ``_paths.sh`` sourced with ``MIND_AGENT`` unset returns rc=0 with
``PROJECT_ROOT``/``AGENTS_PARENT_DIR`` intact and ``AGENT_NAME`` simply unset,
and the hook resolves the agent from the ``sessions/<SID>/binding.yaml`` glob
this fixture creates.

(The sibling harness DOES set both vars. That is a separate, pre-existing
divergence in a file whose assertions do not turn on it; changing it is out of
this goal's scope and is recorded as a finding rather than edited here.)

``STORAGE_BACKEND=local`` is pinned anyway and is NOT an exception to the above.
guard-955/rb-2983: under own-cloud, ``OwnCloudBackend._s3_key`` derives the key
from customer_prefix+env_id+filename and ignores the tmp root entirely, so an
unpinned test write lands on a PRODUCTION key. That var changes which STORE is
addressed; guard-1742 is about vars that change which BRANCH is taken.

CONTAINMENT (guard-2484, both seams named)
------------------------------------------
Seam 1 -- agent dir: isolation is a throwaway ``PROJECT_ROOT`` (physical copy of
``core/``), which guard-2446 names as the only form that actually works. This
file never injects ``MIND_AGENT_DIR`` or ``_AGENT_DIR_OVERRIDE``.
Seam 2 -- name-keyed shared-store writes, which the dir seam does NOT cover:
neither gate path reaches one. Gate 1's only write is
``pending-deploys.py roll-handoff``, which writes ``agent_dir/session/handoff.yaml``
(``_handoff_path``); Gate 2.5/2.6's only write is ``cmd_has_pending``'s
staleness-pruned write-back to the same tmp agent dir. No ``team-state-update``
is on either path, so no shard is created under any name.

guard-1165: no module-level ``os.environ`` mutation, no ``sys.modules`` stubs.
No ``daemon_integration`` marker: neither gate touches a daemon -- Gate 1 and
Gate 2.6 both exit before anything reaches one, which is also why this file
needs no ``DaemonFixture`` and runs an order of magnitude cheaper than its
sibling.

WHY THIS IMPORTS FROM THE SIBLING TEST MODULE
---------------------------------------------
``_copy_core`` and ``_hook_log`` are imported from
``test_stop_hook_in_flight_integration`` rather than copied or extracted.
Copying would fork them; extracting would require deleting the origin copy in
the same change (guard-2015) and editing a green file this goal has no reason
to touch. Importing keeps exactly ONE copy in the repo. The coupling is
deliberate and fails loudly (collection error) if the sibling is renamed.
``_run_hook`` is deliberately NOT imported -- it hardcodes the worker-body SID
and sets the two env vars this file must scrub, so the runner below is a
different thing, not a fork of that one.

Run: STORAGE_BACKEND=local python3 -m pytest \\
       core/scripts/tests/test_stop_hook_gate_integration.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent
CORE = SCRIPTS.parent
REAL_ROOT = CORE.parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from _bash_helpers import BASH  # noqa: E402
from test_stop_hook_in_flight_integration import _copy_core, _hook_log  # noqa: E402

AGENT = "alpha"
# HOOK_SID == running-session-id, so Gate 0 passes through to Gate 1 instead of
# taking the sid-mismatch branch the sibling file covers.
RUNNER_SID = "runner-sid-9999-8888-7777"
SIBLING_SID = "sibling-body-sid-4444-3333"

# The two production lines under test, kept as module constants so each
# mutation proof and its positive test cannot drift apart.
GATE1_ROLL = "pending-deploys.py"
GATE26_FLAG = '--body-sid "$HOOK_SID"'


def _build_runner_root(tmp: Path, world: Path, meta: Path, state: str) -> Path:
    """A tmp PROJECT_ROOT whose RUNNER session is the one the hook is fired for.

    Deliberately does NOT create the worker-Body artifacts the sibling fixture
    needs (forked working-memory.yaml, body-closing sentinel, body-manifest):
    this root models the reducer, and Gate 0 exits before that branch anyway
    once the SIDs match. Adding them would suggest a dependency that is not
    there.
    """
    root = tmp / "gateroot"
    root.mkdir()
    _copy_core(root)

    adir = root / "agents" / AGENT
    (adir / "session").mkdir(parents=True)
    (adir / "sessions" / RUNNER_SID).mkdir(parents=True)

    (adir / "local-paths.conf").write_text(
        f"WORLD_PATH={world.as_posix()}\nMETA_PATH={meta.as_posix()}\n",
        encoding="utf-8")
    (adir / "session" / "running-session-id").write_text(RUNNER_SID,
                                                         encoding="utf-8")
    (adir / "session" / "agent-state").write_text(state, encoding="utf-8")
    (adir / "sessions" / RUNNER_SID / "binding.yaml").write_text(
        f"agent: {AGENT}\nmode: autonomous\n", encoding="utf-8")
    return root


def _run_hook_as_runner(root: Path) -> subprocess.CompletedProcess:
    """Fire the hook in the environment a Stop event actually provides.

    See the module docstring: MIND_SID / MIND_AGENT are scrubbed because
    production does not set them (guard-1742), and STORAGE_BACKEND is pinned
    because an own-cloud write would address a production S3 key regardless of
    the tmp root (guard-955).
    """
    env = os.environ.copy()
    env.pop("MIND_SID", None)
    env.pop("MIND_AGENT", None)
    env["STORAGE_BACKEND"] = "local"
    return subprocess.run(
        # BASH, never a literal "/bin/bash" and never a bare "bash" -- the
        # latter reaches the System32 WSL launcher on a Windows PATH
        # (guard-580). Same house helper the sibling harness uses.
        [BASH, str(root / "core" / "scripts" / "stop-hook.sh")],
        input=json.dumps({"session_id": RUNNER_SID}),
        capture_output=True, text=True, timeout=180, env=env,
    )


def _agent_dir(root: Path) -> Path:
    return root / "agents" / AGENT


def _drive(tmp_path, state="RUNNING", jobs=None, deploys=None, mutate=None):
    """Run the whole hook once. `mutate` edits stop-hook.sh before the run."""
    world = tmp_path / "world"
    world.mkdir()
    meta = tmp_path / "meta_gate"
    meta.mkdir()
    root = _build_runner_root(tmp_path, world, meta, state)

    sess = _agent_dir(root) / "session"
    if jobs is not None:
        (sess / "background-jobs.yaml").write_text(
            yaml.safe_dump({"jobs": jobs}), encoding="utf-8")
    if deploys is not None:
        (sess / "pending-deploys.yaml").write_text(
            yaml.safe_dump(deploys), encoding="utf-8")

    if mutate is not None:
        hook = root / "core" / "scripts" / "stop-hook.sh"
        hook.write_text(mutate(hook.read_text(encoding="utf-8")),
                        encoding="utf-8")

    proc = _run_hook_as_runner(root)
    return proc, root


def _live_job(owner_sid: str) -> dict:
    """A job that cmd_has_pending will actually count.

    All three of its conditions must hold or the gate is silently a no-op:
    owner_sid matches, the PID is ALIVE, and a completion mechanism is
    registered (guard-1619 -- a dead pid registers fine, lists fine, and
    has-pending still returns rc=1). os.getpid() is this pytest process, which
    is unambiguously alive for the duration of the subprocess call.
    """
    return {"job_id": "fixture-job", "pid": os.getpid(),
            "owner_sid": owner_sid, "monitor_goal_id": "g-306-173-fixture",
            "started_at": "2026-08-03T22:00:00"}


def _blocked(proc) -> bool:
    """The hook's BLOCK verdict, read from the decision payload it prints."""
    return '"decision": "block"' in (proc.stdout or "")


def _compact_pending(root: Path) -> Path:
    """Written ONLY on the BLOCK path (stop-hook.sh:369) -- a file side effect,
    so a BLOCK is provable without parsing stdout."""
    return _agent_dir(root) / "session" / "compact-pending"


# ── Gate 1: not RUNNING → allow ──────────────────────────────────────────────

def test_gate_1_not_running_allows_the_turn_end(tmp_path):
    """State IDLE must reach Gate 1 and ALLOW -- executed, not source-read."""
    proc, root = _drive(tmp_path, state="IDLE")
    log = _hook_log(root)

    assert proc.returncode == 0, f"hook must fail-open: {proc.stderr[-2000:]}"
    assert "gate=not-running" in log, (
        f"Gate 1 was never reached; log:\n{log}\nstderr:\n{proc.stderr[-2000:]}")
    assert "state=IDLE" in log, (
        f"Gate 1 fired but did not report the state it read; log:\n{log}")
    assert not _blocked(proc), (
        f"an IDLE agent must not be BLOCKED; stdout:\n{proc.stdout[-2000:]}")
    assert not _compact_pending(root).exists(), (
        "compact-pending is written only on the BLOCK path -- its presence "
        "means the hook fell through Gate 1")


def test_gate_1_rolls_pending_deploys_into_handoff_before_allowing(tmp_path):
    """SG-c: the roll is a SIDE EFFECT on disk, not a log string.

    stop-hook.sh:317-320 rolls unresolved deploy obligations into handoff.yaml
    so they are surfaced in the next session's boot summary instead of silently
    crossing the stop boundary. Nothing executed that backstop before this test,
    so a break in it would have been invisible: the roll is fail-open and its
    log line is emitted only when the helper prints something.
    """
    deploys = [{"repo": "acme/widget-service", "sha": "deadbeef1234",
                "goal_id": "g-306-173-fixture", "dir": "",
                "ts": "2026-08-03T21:00:00"}]
    proc, root = _drive(tmp_path, state="IDLE", deploys=deploys)
    log = _hook_log(root)

    assert "gate=not-running" in log, f"Gate 1 not reached; log:\n{log}"
    assert "pending-deploys-roll" in log, (
        f"the SG-c roll produced no output; log:\n{log}\n"
        f"stderr:\n{proc.stderr[-2000:]}")

    handoff = _agent_dir(root) / "session" / "handoff.yaml"
    assert handoff.is_file(), (
        f"handoff.yaml was never written; log:\n{log}")
    doc = yaml.safe_load(handoff.read_text(encoding="utf-8")) or {}
    rolled = doc.get("pending_deploys") or []
    assert [(e.get("repo"), e.get("sha")) for e in rolled] == [
        ("acme/widget-service", "deadbeef1234")], (
        f"the obligation did not reach handoff.yaml: {rolled!r}")


def test_gate_1_negative_control_running_state_blocks(tmp_path):
    """The discriminator for the two tests above.

    Same fixture, same hook, only agent-state differs. Without this, a hook
    that ALLOWED unconditionally would pass both Gate 1 tests.
    """
    proc, root = _drive(tmp_path, state="RUNNING")
    log = _hook_log(root)

    assert proc.returncode == 0
    assert "gate=not-running" not in log, (
        f"Gate 1 fired on a RUNNING agent; log:\n{log}")
    assert "BLOCK sid=" in log, (
        f"a RUNNING agent with no stop signal must BLOCK; log:\n{log}\n"
        f"stderr:\n{proc.stderr[-2000:]}")
    assert _blocked(proc), f"no block decision emitted; stdout:\n{proc.stdout}"
    assert _compact_pending(root).is_file(), (
        "the BLOCK path must write compact-pending -- session-save-id.sh needs "
        "it to re-point running-session-id after autocompact")


def test_mutation_neutralizing_the_roll_leaves_handoff_empty(tmp_path):
    """Proves the handoff side effect comes from THIS invocation.

    Neutralize the CALL and keep the assignment: deleting the line outright
    leaves the following `[ -n "$_PDROLL" ]` unbound under `set -u`. The
    ALLOW must still fire -- a mutation that also breaks Gate 1 would satisfy
    the empty-handoff assertion for the wrong reason.
    """
    def _strip(src: str) -> str:
        out, hit = [], 0
        for ln in src.splitlines(keepends=True):
            if GATE1_ROLL in ln and "_PDROLL=$(" in ln:
                out.append("        _PDROLL=$(true)\n")
                hit += 1
            else:
                out.append(ln)
        assert hit == 1, f"expected exactly 1 roll-handoff line, found {hit}"
        return "".join(out)

    deploys = [{"repo": "acme/widget-service", "sha": "deadbeef1234",
                "goal_id": "g-306-173-fixture", "dir": "",
                "ts": "2026-08-03T21:00:00"}]
    proc, root = _drive(tmp_path, state="IDLE", deploys=deploys, mutate=_strip)
    log = _hook_log(root)

    assert proc.returncode == 0
    assert "gate=not-running" in log, (
        "the mutation must remove ONLY the roll, not Gate 1 itself -- "
        f"otherwise this proves nothing about SG-c; log:\n{log}")
    handoff = _agent_dir(root) / "session" / "handoff.yaml"
    assert not handoff.exists() or not (
        (yaml.safe_load(handoff.read_text(encoding="utf-8")) or {}
         ).get("pending_deploys")), (
        "the obligation reached handoff.yaml with the roll neutralized -- "
        "something ELSE is writing it, so the positive test is not testing "
        "this backstop")


# ── Gate 2.6: pending long-running background jobs → allow ───────────────────

def test_gate_2_6_own_body_job_allows_the_turn_end(tmp_path):
    """A live job owned by THIS body must ALLOW -- through the whole chain.

    Reaching this line means Gates 0, 1, 2 and 2.5 all passed in a real run.
    That composed transition is what no test covered: the body filter's own
    decision table is already unit-tested, its call shape already source-pinned.
    """
    proc, root = _drive(tmp_path, state="RUNNING", jobs=[_live_job(RUNNER_SID)])
    log = _hook_log(root)

    assert proc.returncode == 0, f"hook must fail-open: {proc.stderr[-2000:]}"
    assert "gate=background-jobs" in log, (
        f"Gate 2.6 did not ALLOW for an own-body job; log:\n{log}\n"
        f"stderr:\n{proc.stderr[-2000:]}")
    assert not _blocked(proc), (
        f"a live own-body job must not BLOCK; stdout:\n{proc.stdout[-2000:]}")
    assert not _compact_pending(root).exists()


def test_gate_2_6_sibling_body_job_does_not_allow(tmp_path):
    """The  rail, executed rather than source-read.

    background-jobs.yaml is AGENT-WIDE -- one file per mind, shared by every
    body. A worker body's job must not ALLOW the reducer's turn-end, because
    an ALLOW is exactly what removes the text-death net.
    """
    proc, root = _drive(tmp_path, state="RUNNING", jobs=[_live_job(SIBLING_SID)])
    log = _hook_log(root)

    assert proc.returncode == 0
    assert "gate=background-jobs" not in log, (
        f"a SIBLING body's job ALLOWed this body's turn-end; log:\n{log}")
    assert _blocked(proc), (
        f"expected BLOCK for a foreign-owned job; stdout:\n{proc.stdout}")
    assert _compact_pending(root).is_file()


def test_mutation_dropping_body_sid_lets_a_sibling_job_allow(tmp_path):
    """Proves the test above depends on the flag, not on luck.

    Drop `--body-sid "$HOOK_SID"` from Gate 2.6 only -- Gate 2.5's
    pending-agents call carries the identical text, and a whole-file replace
    would mutate a line this test says nothing about. With the flag gone the
    check goes agent-wide and the sibling's job ALLOWs, which is precisely the
    pre-g-306-135 defect.
    """
    def _drop_flag(src: str) -> str:
        out, hit = [], 0
        for ln in src.splitlines(keepends=True):
            if "background-jobs.sh" in ln and "has-pending" in ln:
                assert GATE26_FLAG in ln, (
                    "production arg shape changed on the Gate 2.6 line -- "
                    "re-derive this pin")
                out.append(ln.replace(GATE26_FLAG + " ", ""))
                hit += 1
            else:
                out.append(ln)
        assert hit == 1, f"expected exactly 1 Gate 2.6 line, found {hit}"
        return "".join(out)

    proc, root = _drive(tmp_path, state="RUNNING",
                        jobs=[_live_job(SIBLING_SID)], mutate=_drop_flag)
    log = _hook_log(root)

    assert proc.returncode == 0
    assert "gate=background-jobs" in log, (
        "with --body-sid removed the sibling's job should ALLOW agent-wide; it "
        "did not, so the negative control above is passing for some other "
        f"reason and the body filter is not what it depends on; log:\n{log}\n"
        f"stderr:\n{proc.stderr[-2000:]}")


def test_mutation_reading_ayoai_sid_instead_of_hook_sid_kills_the_gate(tmp_path):
    """The reason this file scrubs MIND_SID, pinned instead of merely stated.

    A Stop event supplies no MIND_SID (it is injected only into
    PreToolUse[Bash]), which is why Gate 2.6 passes the payload-parsed
    ``$HOOK_SID``. Swap in ``${MIND_SID:-}`` -- the SILENT form; a bare
    ``$MIND_SID`` would die under ``set -u`` and be noticed -- and the flag
    resolves empty, which cmd_has_pending reads as "nothing is mine" (exit 1).
    A live own-body job then fails to ALLOW and the turn-end BLOCKs.

    Measured 2026-08-03 (zeta, hostname cc-02, uname -r 6.8.0-136-generic) with
    a same-run control: unmutated ALLOW=True/BLOCK=False, swapped
    ALLOW=False/BLOCK=True.

    Without this test the guarantee lives only in the module docstring, and a
    test env that leaked MIND_SID would quietly restore the green
    (guard-1742: the only environment where such a hook fails is the only
    environment where it runs).
    """
    def _read_env_instead(src: str) -> str:
        out, hit = [], 0
        for ln in src.splitlines(keepends=True):
            if "background-jobs.sh" in ln and "has-pending" in ln:
                assert GATE26_FLAG in ln, (
                    "production arg shape changed on the Gate 2.6 line -- "
                    "re-derive this pin")
                out.append(ln.replace(GATE26_FLAG, '--body-sid "${MIND_SID:-}"'))
                hit += 1
            else:
                out.append(ln)
        assert hit == 1, f"expected exactly 1 Gate 2.6 line, found {hit}"
        return "".join(out)

    proc, root = _drive(tmp_path, state="RUNNING",
                        jobs=[_live_job(RUNNER_SID)], mutate=_read_env_instead)
    log = _hook_log(root)

    assert proc.returncode == 0
    assert "gate=background-jobs" not in log, (
        "Gate 2.6 still ALLOWed after being pointed at MIND_SID -- either the "
        "test environment is leaking that variable (see the module docstring) "
        f"or the gate is no longer reading the flag at all; log:\n{log}")
    assert _blocked(proc), (
        f"expected BLOCK once the body sid resolves empty; stdout:\n{proc.stdout}")
