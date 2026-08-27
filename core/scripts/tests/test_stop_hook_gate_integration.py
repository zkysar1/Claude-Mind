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

g-115-7303 adds a THIRD, Gate 0b (same SID, different owning process), in the
same shape. It belongs in this file and not the sibling for one structural
reason: Gate 0b fires only when HOOK_SID == RUNNER_SID, and this fixture is the
only one in the repo where that holds -- the sibling drives the MISMATCH branch,
where Gate 0 exits two lines earlier and Gate 0b is unreachable by construction.

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
# Gate 0b (), kept whole so a mutation proof can never drift from the
# line it claims to mutate. Pinned against the REAL hook below: a rename that
# leaves these stale would otherwise turn every mutation test into a silent no-op
# that still passes (rb-5146 -- source text proves wiring exists, not that it runs).
GATE0B_SID_GUARD = '[ -n "$RUNNER_SID" ] && [ "$HOOK_SID" = "$RUNNER_SID" ] && '
GATE0B_AGENT_ARG = 'runner_proc_foreign_live "$HOOK_AGENT"'
GATE0B_LINE = "if " + GATE0B_SID_GUARD + GATE0B_AGENT_ARG + "; then"


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


def _run_hook_as_runner(root: Path, extra_env: dict | None = None) -> subprocess.CompletedProcess:
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
    # RUNNER_PROC_ID is the SAME seam the shell suite uses at three sites
    # (test-runner-identity-check.sh:248/274/329), not a new one invented here
    # (guard-1885). It is REQUIRED, not a convenience: _resolve_owner_proc walks
    # for a `claude` ancestor, a pytest->bash tree has none, so without the
    # override the predicate fails closed and every Gate 0b test would BLOCK for
    # the wrong reason -- passing while proving nothing.
    env.update(extra_env or {})
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


def _drive(tmp_path, state="RUNNING", jobs=None, deploys=None, mutate=None,
           runner_proc=None, my_proc=None, runner_sid=None):
    """Run the whole hook once. `mutate` edits stop-hook.sh before the run.

    ``runner_proc`` stamps ``session/runner-proc`` (who OWNS the runner role),
    ``my_proc`` says who THIS process is via RUNNER_PROC_ID, and ``runner_sid``
    overwrites ``running-session-id`` -- the three inputs Gate 0b reads.
    """
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

    if runner_proc is not None:
        (sess / "runner-proc").write_text(runner_proc + "\n", encoding="utf-8")
    if runner_sid is not None:
        (sess / "running-session-id").write_text(runner_sid, encoding="utf-8")

    if mutate is not None:
        hook = root / "core" / "scripts" / "stop-hook.sh"
        hook.write_text(mutate(hook.read_text(encoding="utf-8")),
                        encoding="utf-8")

    proc = _run_hook_as_runner(
        root, extra_env={"RUNNER_PROC_ID": my_proc} if my_proc else None)
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


# ── Gate 0b: same SID, different owning process () ─────────────────
#
# THE WEDGE. runner-identity-check.sh ejects a duplicate instance that SHARES the
# runner's SID, keyed on the owning-process identity. stop-hook Gate 0 allowed a
# turn-end only on a SID MISMATCH -- so for that ejected process HOOK_SID ==
# RUNNER_SID and the hook BLOCKED every turn-end while the gate ejected every
# re-entry. Neither iterate nor stop. Measured zeta/cc-02 2026-08-22, 3 turns.
#
# Every test below drives state=RUNNING, so the DEFAULT verdict on this path is
# BLOCK. That is what makes the negative controls mean something: three of the
# four inputs Gate 0b can see must leave that BLOCK untouched, and only the
# fourth may turn it into an ALLOW.


def _starttime(pid: int) -> str:
    """/proc/<pid>/stat field 22, read the comm-safe way the shell helper reads it.

    Split on the LAST ')' because comm is parenthesized and may itself contain
    spaces and parens; positional parsing of the raw line is wrong. Index 19
    of the remainder was verified byte-equal against `_proc_stat_field <pid> 20`
    rather than derived from the field table.
    """
    rest = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1]
    return rest.split()[19]


def _proc_id(pid: int) -> str:
    return f"{pid}:{_starttime(pid)}"


def _live_other():
    """A real, live, DIFFERENT process -- never a synthesized id.

    _owner_alive re-reads starttime from /proc, so a made-up pair would read as
    DEAD and every "foreign live owner" test would pass for the wrong reason.
    """
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])


MINE = "424242:777777"  # this process's identity, injected via RUNNER_PROC_ID


def test_gate_0b_wiring_is_pinned_to_the_real_hook():
    """If this fails, the mutation proofs below are silently no-ops."""
    hook = (REAL_ROOT / "core" / "scripts" / "stop-hook.sh").read_text(encoding="utf-8")
    assert hook.count(GATE0B_LINE) == 1, "Gate 0b line moved or was reworded"
    assert hook.count('source "$CORE_ROOT/scripts/_runner_proc.sh"') == 1


def test_gate_0b_ejected_duplicate_is_allowed_to_end_its_turn(tmp_path):
    """THE FIX: a live foreign owner means this process is the ejected one."""
    other = _live_other()
    try:
        proc, root = _drive(tmp_path, runner_proc=_proc_id(other.pid), my_proc=MINE)
    finally:
        other.kill(); other.wait()
    assert not _blocked(proc), proc.stdout
    assert "gate=same-sid-not-owner" in _hook_log(root)
    assert not _compact_pending(root).exists(), "an ALLOW must write no BLOCK side effect"


def test_gate_0b_negative_control_no_stamp_still_blocks(tmp_path):
    """The positive control (guard-3366): this fixture BLOCKs when nothing is stamped.

    Without it, every assertion above is unfalsifiable -- an ALLOW could just be
    what this path always does.
    """
    proc, root = _drive(tmp_path, my_proc=MINE)
    assert _blocked(proc), proc.stdout
    assert "gate=same-sid-not-owner" not in _hook_log(root)


def test_gate_0b_the_real_runner_is_not_allowed_to_die(tmp_path):
    """Stamp names THIS process -> it IS the runner. The dangerous direction."""
    proc, root = _drive(tmp_path, runner_proc=MINE, my_proc=MINE)
    assert _blocked(proc), "the stamped owner must never be allowed to stop"
    assert "gate=same-sid-not-owner" not in _hook_log(root)


def test_gate_0b_dead_stamped_owner_still_blocks(tmp_path):
    """A dead owner is a TAKEOVER, not an ejection.

    runner-identity-check rewrites the stamp and keeps running in this case, so
    an ALLOW here would be an allowance with no matching eject.
    """
    other = _live_other()
    dead_id = _proc_id(other.pid)
    other.kill(); other.wait()
    proc, root = _drive(tmp_path, runner_proc=dead_id, my_proc=MINE)
    assert _blocked(proc), proc.stdout
    assert "gate=same-sid-not-owner" not in _hook_log(root)


def test_gate_0b_empty_running_session_id_blocks(tmp_path):
    """An EMPTY pointer must NOT allow -- runner-identity-check fail-opens there.

    `[ -n "$RUNNER_SID" ] || exit 0` ejects NOBODY when the pointer is empty. The
    hook falls THROUGH the sid-mismatch branch in that case (it also tests -n),
    so Gate 0b re-states the SID match rather than inheriting it.
    """
    other = _live_other()
    try:
        proc, root = _drive(tmp_path, runner_proc=_proc_id(other.pid),
                            my_proc=MINE, runner_sid="")
    finally:
        other.kill(); other.wait()
    assert _blocked(proc), "an empty pointer has no matching eject -- must not allow"


def test_mutation_dropping_the_sid_precondition_allows_an_empty_pointer(tmp_path):
    """Proves the guard-4315 scar is load-bearing, not decorative.

    Same inputs as the test above; deleting the precondition flips BLOCK to
    ALLOW -- i.e. hands the real runner a licence to end its turn quietly.
    """
    other = _live_other()
    try:
        proc, root = _drive(
            tmp_path, runner_proc=_proc_id(other.pid), my_proc=MINE, runner_sid="",
            mutate=lambda t: t.replace(GATE0B_SID_GUARD, "", 1))
    finally:
        other.kill(); other.wait()
    assert not _blocked(proc), "mutation did not take -- check GATE0B_SID_GUARD"
    assert "gate=same-sid-not-owner" in _hook_log(root)


def test_mutation_dropping_the_agent_argument_kills_the_gate(tmp_path):
    """guard-2601: <agent> is a REQUIRED positional, and the caller must pass it.

    runner_proc_foreign_live returns 1 on an empty agent rather than probing some
    default, so a caller that forgets it gets the pre-existing BLOCK -- a visible
    regression, never a silent probe of the wrong agent's session dir.
    """
    other = _live_other()
    try:
        proc, root = _drive(
            tmp_path, runner_proc=_proc_id(other.pid), my_proc=MINE,
            mutate=lambda t: t.replace(GATE0B_AGENT_ARG,
                                       'runner_proc_foreign_live ""', 1))
    finally:
        other.kill(); other.wait()
    assert _blocked(proc), "an agent-less call must fail closed"
