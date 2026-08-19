"""Pin : recovery instructions must not assert unread goal state.

The state-update / learning-gate cases of _print_recovery_instructions in
iteration-close.sh previously printed "Goal X has status=completed (verify
succeeded)" / "Goal X is closed" UNCONDITIONALLY on any rc!=0 from those
phases. The common way to get rc=2 is entry validation rejecting the call
(missing --goal/--source) AFTER _CURRENT_PHASE is set — nothing ran, verify
never fired, and the goal may still be pending with a live claim (measured:
bravo 2026-07-30, g-115-4084 sat pending with a live claim ~40min on the
strength of "No goal-status revert needed").

The fix probes the live record (_probe_goal_status) and branches three ways:
completed → original text (true there); readable-but-not-completed → honest
"verify has NOT marked it completed"; unreadable → UNKNOWN, asserting neither.

These tests exercise the UNREADABLE branch with the literal production arg
shape (guard-920): a validation-rejecting invocation, where the goal id is
either nonexistent or unparseable. Under pytest the daemon wrapper refuses to
spawn (PYTEST_CURRENT_TEST set, RUNTIME_DIR unset — g-115-3329), so the probe
fails closed to "" exactly as it does in production when the read fails. The
completed/pending branches require live queue state and were verified by hand
probes at fix time; the load-bearing pin here is that the OLD unconditional
text can no longer print without a completed read backing it.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _runtime_bash import BASH  # noqa: E402
from _paths import WORLD_DIR  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "iteration-close.sh"
FAKE_AGENT_DIR = Path(__file__).resolve().parents[3] / "agents" / "ic-recovery-test-agent"
# SECOND residue surface, in a DIFFERENT tree with a DIFFERENT consumer
# (). See the fixture docstring.
FAKE_AGENT_SHARD = Path(WORLD_DIR) / "team-state" / "agents" / "ic-recovery-test-agent.yaml"


@pytest.fixture(autouse=True, scope="module")
def _cleanup_fake_agent_dir():
    """Remove the fake agent's residue — BOTH surfaces (dir 2026-07-30, shard 2026-08-07).

    On a box with a LIVE daemon, the script's EXIT-trap writes (execution
    diary, heartbeat) reach the daemon — the g-115-3329 refusal blocks
    SPAWNS, not calls to an already-running daemon — and the daemon
    materializes agents/ic-recovery-test-agent/, a phantom agent every
    cross-agent glob consumer then enumerates. Pointing RT_DIR at a tmp dir
    is NOT the fix: rt_spawn would then spawn a real orphan daemon per call
    (the refusal guards only the shared dir). Transient existence during
    this module's few seconds is acceptable; permanent residue is not.

    THE SAME TRAP WRITES A SECOND RESIDUE THIS FIXTURE DID NOT SEE (g-115-5220).
    The heartbeat write also materializes the TEAM-STATE SHARD at
    world/team-state/agents/ic-recovery-test-agent.yaml — a different tree,
    read by a different consumer (_agents.py::_from_team_state globs that dir
    to build ACTIVE_AGENTS). The shard alone is sufficient to keep the phantom
    in the roster with the agent dir long gone, which is exactly what happened:
    test_capability_route_gate::test_active_agents_tripwire went red on a box
    where this fixture had faithfully removed the dir every run.

    The lesson worth carrying: this fixture was written against the residue its
    author could SEE. One cleanup is not evidence of complete cleanup — after
    removing a residue, ask what OTHER tree the same write touched.

    A ONE-OFF TOMBSTONE IS NOT ENOUGH, MEASURED. The shard had already been retired
    (retired_at 2026-08-06T11:23:34, by hand) and came BACK, because a heartbeat
    newer than retired_at auto-un-retires the row. So cleaning the shard here is
    load-bearing, not belt-and-braces: without it every retirement is undone by
    the next run of this module. What DOES hold is a tombstone written after this
    module's last write — which is what the teardown below does.

    BUT THE CLEANUP MUST NOT BE A BARE unlink(), MEASURED AGAIN (g-115-5220,
    2026-08-08). That is what this fixture shipped with, and it cleaned nothing:
    fleet boxes do not hold s3:DeleteObject, so the local mirror goes, the backing
    object survives, and the next read re-materializes the shard UN-tombstoned.
    g-115-4327 had already measured exactly this inside _team_state.retire_agent a
    week before this fixture was written.

    NOR MAY IT BE AN IN-PROCESS WRITE — INCLUDING THE GOVERNED ONE. This docstring
    prescribed exactly that (locked_modify_yaml, then a best-effort unlink) until
    2026-08-10, and the prescription was already refuted by measurement when it was
    written: the pollution and the cleanup sit on OPPOSITE SIDES of the guard-955
    pin. The phantom row is written by the DAEMON, which runs own-cloud and lands
    in S3; this pytest process is pinned STORAGE_BACKEND=local, so ANY in-process
    write — governed or not — reaches the LOCAL MIRROR ONLY and the next read
    discards it. Measured 2026-08-08 (cc-04): the in-process helper left the S3
    object byte-identical (same LastModified, retired_by unchanged), while the
    same retirement issued through team-state-retire.sh moved it and flipped
    _is_retired to True.

    So the retirement MUST go out through team-state-retire.sh, which puts the
    write back on the daemon's own lane. That is what the shared helper below
    does — do not re-implement it here. See the conftest helper for the full trace.


    MIND_WORLD TMP ISOLATION DOES NOT WORK HERE, ALSO MEASURED (do not "improve"
    this into that). Running the production arg shape with MIND_WORLD pointed at
    a tmp dir still advanced the LIVE shard and left the tmp world empty: the
    write is performed by the DAEMON, which resolves its own world path, so no
    env var set in this process can redirect it.
    """
    # Shared with the conftest session-teardown net so there is ONE retirement
    # mechanism, not two that can drift apart. If the import is unavailable,
    # skip the shard half rather than falling back to the bare unlink this fix
    # exists to remove — a silent fallback would reintroduce the defect. The
    # conftest fixture still catches it at session end; only the window widens.
    try:
        from conftest import _retire_phantom_shard as _retire
    except ImportError:
        _retire = None

    def _shard_present():
        """AUTHORITATIVE first, local mirror as the union — never local alone.

        guard-980: a bare Path.exists() is the wrong question on this backend.
        The mirror is a read-through cache, so it reads False for a row the
        DAEMON wrote to the backing store that this box has simply not read yet
        — which is exactly the case this cleanup exists for. Measured 2026-08-08
        (cc-04): the daemon advanced the phantom's row in S3 with NO local mirror
        present at teardown, so a local-only gate skipped the retire and reported
        success while the row sat latent and re-materialized on the next read.
        The sibling conftest teardown enumerates the same way (backend list_dir
        UNION local glob) after measuring the two lanes disagree in practice —
        12 shards vs 11. Keep the union: OR, not either alone, so a local-only
        file with no backing object is still cleaned.
        """
        try:
            import storage_backend as _sb
            if _sb.get_backend().exists(FAKE_AGENT_SHARD):
                return True
        except Exception:
            pass
        return FAKE_AGENT_SHARD.exists()

    def _clean():
        shutil.rmtree(FAKE_AGENT_DIR, ignore_errors=True)
        if _retire is not None and _shard_present():
            _retire(FAKE_AGENT_SHARD)

    _clean()
    yield
    _clean()

OLD_STATE_UPDATE_ASSERTION = "has status=completed (verify succeeded)"
OLD_LEARNING_GATE_ASSERTION = "is closed (verify + state-update done)"


def _run(phase: str, goal_id: str):
    env = os.environ.copy()
    env["STORAGE_BACKEND"] = "local"  # guard-955: mandatory pin for any test runner
    # Fake agent binding: the script exits before dispatch (no recovery
    # block) when MIND_AGENT is unset, and a REAL agent name would append
    # phase-marker rows to that agent's live execution diary via the EXIT
    # trap. A nonexistent agent reaches the recovery block (verified at fix
    # time). NOT fully hermetic: with a live daemon the trap's writes land
    # and materialize the fake agent dir — the module fixture above removes
    # it (fresh-eyes finding, 2026-07-30).
    env["MIND_AGENT"] = "ic-recovery-test-agent"
    # Validation-reject shape: --goal given, --source omitted → exit 2 from the
    # phase entry check, AFTER _CURRENT_PHASE is set → recovery block prints.
    proc = subprocess.run(
        [BASH, SCRIPT.as_posix(), "--phase", phase, "--goal", goal_id],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    return proc


def test_state_update_unreadable_goal_does_not_assert_completed():
    proc = _run("state-update", "g-999999-99")
    assert proc.returncode == 2, proc.stderr
    assert "verify state UNKNOWN" in proc.stderr
    assert "asserting neither direction" in proc.stderr
    assert OLD_STATE_UPDATE_ASSERTION not in proc.stderr
    assert "No goal-status revert needed" not in proc.stderr


def test_state_update_unparseable_goal_id_takes_unreadable_branch():
    # g-xw-* ids carry no derivable aspiration id — the probe's regex guard
    # must fall through to the unreadable branch, not crash the trap.
    proc = _run("state-update", "g-xw-20260101T000000-01")
    assert proc.returncode == 2, proc.stderr
    assert "verify state UNKNOWN" in proc.stderr
    assert OLD_STATE_UPDATE_ASSERTION not in proc.stderr


def test_learning_gate_unreadable_goal_does_not_assert_closed():
    proc = _run("learning-gate", "g-999999-99")
    assert proc.returncode == 2, proc.stderr
    assert "closure state UNKNOWN" in proc.stderr
    assert OLD_LEARNING_GATE_ASSERTION not in proc.stderr


def test_verify_case_unchanged_hedge_survives():
    # The verify case was already honest ("may be in indeterminate state") —
    # the goal's sibling audit keeps it untouched; pin the hedge so a future
    # edit does not import the unconditional-assertion shape there.
    proc = _run("verify", "g-999999-99")
    assert proc.returncode == 2, proc.stderr
    assert "may be in indeterminate state" in proc.stderr
