"""test_claim_wrapper_live_checkpoint_invariance.py —  regression.

DEFECT (occ258, recorded on the goal). A claim-wrapper test runs the REAL
aspirations-claim.sh with a SUCCESSFUL world claim, so its _post_claim_effects
anchor fires `loop-state-save.sh init` on the real single-writer. That writer
resolves its project root from its SCRIPT LOCATION (the real repo — there is no
env override seam; MIND_AGENT_DIR does not reach body_state_path, guard-2985),
so the anchor lands in the LIVE repo's iteration checkpoint, not in the test's
tmp tree:

  * BODY-KEYED channel — the test process inherits the worker Body's MIND_SID
    (mind_api's conftest captured it into _BOOTSTRAP_ENV and re-injected it
    before every test — the g-115-6942 restore channel), so
    _checkpoint_path() resolved agents/alpha/sessions/<LIVE SID>/
    iteration-checkpoint.json and anchored it to fixture goal g-001-01.
  * AGENT-WIDE channel (occ258(4)) — even with MIND_SID stripped, the claim
    runs as the REAL agent name, so body_state_path's fallback writes
    agents/alpha/session/iteration-checkpoint.json in the live repo.

The FIX this file pins, three independent parts (any one missing re-opens a
leak):

  1. The per-tree conftests SCRUB MIND_SID / BODY_WM_PATH / BODY_ROLE /
     MIND_CHECKPOINT_PATH (one-way; the mind_api restore channel no longer
     captures MIND_SID at all), and run-invisible-suites.sh unsets the same
     vars before dispatching main()-style files (the delegation path that loads
     no conftest).
  2. loop-state-save.py::_checkpoint_path honours the MIND_CHECKPOINT_PATH
     seam (read per call) so a harness can redirect all four commands to its
     tmp tree.
  3. The leak-site harness (mind_api/tests/
     test_wrapper_aspirations_retire_release_claim.py::_run) PINs the seam to
     its tmp project_root — the RT_DIR precedent, "the seam that actually
     holds".

test_claim_wrapper_tests_leave_live_checkpoint_byte_identical is the load-
bearing test: it re-injects the ambient session sid into THIS process (simulating
a worker-Body launch context the conftest scrub is meant to defeat) and runs
the real claim-wrapper suite in a subprocess. The subprocess's conftest scrubs
again, its harness pins the seam, and the live checkpoint must come out
byte-identical — sha for both the agent-wide path and every body-keyed path
that has a forked working-memory.yaml. Without the fix, the run mutates the
live checkpoint and this test goes RED (mutation-proven).

The seam tests below run the REAL loop-state-save.sh (the canonical call shape,
guard-580: never a bare "bash" argv[0]) with the pin set, and assert every
command lands in the tmp file and never in the live tree.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
REPO_ROOT = CORE_SCRIPTS.parent.parent
LEAK_SITE = REPO_ROOT / "mind_api" / "tests" / "test_wrapper_aspirations_retire_release_claim.py"
RUNNER = SCRIPT_DIR / "run-invisible-suites.sh"

sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH as GIT_BASH  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _sha(path: Path):
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _leak_targets() -> list[Path]:
    """Every live checkpoint path a claim-wrapper run could land on.

    The wrapper's _run hard-sets MIND_AGENT=alpha, so the agent-wide target is
    fixed regardless of this process's env. The body-keyed targets are the
    sids the inner run could inherit: this process's ambient MIND_SID (a
    worker-Body launch context) and the harness's setdefault fallback (which
    has no forked working-memory.yaml, so its only channel is agent-wide —
    listed anyway so the assertion is explicit).
    """
    targets = [REPO_ROOT / "agents" / "alpha" / "session" / "iteration-checkpoint.json"]
    sids = {s for s in (os.environ.get("MIND_SID"), "pytest-wrapper-harness-sid") if s}
    for sid in sids:
        targets.append(
            REPO_ROOT / "agents" / "alpha" / "sessions" / sid / "iteration-checkpoint.json"
        )
    return targets


def _snapshot(paths: list[Path]) -> dict:
    return {str(p): _sha(p) for p in paths}


def _assert_identical(before: dict, after: dict, paths: list[Path]) -> None:
    for p in paths:
        key = str(p)
        if before[key] != after[key]:
            rel = p.relative_to(REPO_ROOT)
            if before[key] is None:
                pytest.fail(
                    f"LIVE CHECKPOINT MUTATED by claim-wrapper run: {rel} was "
                    f"ABSENT before and now exists ({after[key][:16]}…) — the "
                    f"g-115-11509 checkpoint isolation is not holding"
                )
            pytest.fail(
                f"LIVE CHECKPOINT MUTATED by claim-wrapper run: {rel} sha "
                f"{before[key][:16]}… -> {after[key][:16]}… — the g-115-11509 "
                f"checkpoint isolation is not holding"
            )


# ---------------------------------------------------------------------------
# the load-bearing regression (goal outcome 2)
# ---------------------------------------------------------------------------

def test_claim_wrapper_tests_leave_live_checkpoint_byte_identical():
    """Run the real claim-wrapper suite from a SIMULATED worker-Body context.

    The simulated context re-injects MIND_SID into this process's os.environ
    exactly as the bash-agent-inject hook would on a worker box. The subprocess
    inherits it — the pre-fix leak condition. The subprocess's conftest must
    scrub it, the harness must pin the seam, and the live checkpoint must come
    out byte-identical for every target in _leak_targets().
    """
    paths = _leak_targets()
    before = _snapshot(paths)

    # Simulate the worker-Body launch context: the ambient session sid IS the
    # leak. Use this process's real sid when present (the strongest simulation:
    # it has a forked working-memory.yaml, so an unscrubbed inner run takes the
    # body-keyed channel into the LIVE session dir); otherwise a synthetic sid
    # (the inner run's setdefault fallback, agent-wide channel).
    ambient_sid = os.environ.get("MIND_SID")
    sim_sid = ambient_sid or "sim-g11511509-no-fork"

    env = os.environ.copy()
    env["MIND_SID"] = sim_sid

    # Scope to the single test that does a SUCCESSFUL world claim — the one
    # whose _post_claim_effects fires the anchor. -k keeps the run to one test
    # and one in-process daemon (fast, no cross-test flakiness) while still
    # driving the REAL aspirations-claim.sh end to end.
    proc = subprocess.run(
        [sys.executable, "-u", "-m", "pytest", "-q",
         str(LEAK_SITE), "--no-header", "-p", "no:cacheprovider",
         "-k", "test_wrapper_claim_happy_path"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
        timeout=300,
    )
    after = _snapshot(paths)

    tail = "\n".join((proc.stdout or "").splitlines()[-20:])
    assert proc.returncode == 0, (
        f"claim-wrapper test failed under simulated live sid (rc={proc.returncode});\n"
        f"tail:\n{tail}\nstderr tail:\n"
        + "\n".join((proc.stderr or "").splitlines()[-20:])
    )
    _assert_identical(before, after, paths)


def test_invisible_suite_runner_strips_body_identity_vars():
    """Pin the scrub on the delegation path (goal outcome 3, runner layer).

    run-invisible-suites.sh dispatches main()-style files with a bare
    `python3 "$f"` that inherits its launch env, and it is the path
    run-scoped-suite.py delegates zero-test files through — the conftest scrubs
    never load there. Source-text pin (house precedent: test_claim_liveness.py
    pins mind-api-start.sh the same way): the unset line must name every body
    identity var, and it must sit BEFORE the dispatch loop.
    """
    text = RUNNER.read_text(encoding="utf-8")
    unset_at = text.find("unset MIND_SID BODY_WM_PATH BODY_ROLE MIND_CHECKPOINT_PATH")
    assert unset_at != -1, (
        "run-invisible-suites.sh no longer unsets the forked-Body identity vars "
        "before dispatch — a worker-Body shell would leak its live MIND_SID "
        "into every main()-style suite (g-115-11509)"
    )
    dispatch_at = text.find("for f in \"${INVISIBLE[@]}\"")
    assert dispatch_at != -1 and unset_at < dispatch_at, (
        "the identity-var unset must come BEFORE the file dispatch loop"
    )


# ---------------------------------------------------------------------------
# the seam, driven through the REAL loop-state-save.sh (canonical call shape)
# ---------------------------------------------------------------------------

_ANCHOR = json.dumps({
    "goal_id": "g-001-01",
    "aspiration_id": "asp-001",
    "source": "world",
    "phase": "selected",
    "selected_at": "2026-10-01T13:25:18",
})


def _lss(env_extra: dict, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["MIND_AGENT"] = "alpha"
    env.update(env_extra)
    return subprocess.run(
        [GIT_BASH, str(CORE_SCRIPTS / "loop-state-save.sh"), *args],
        input=stdin, env=env, capture_output=True, text=True, timeout=60,
    )


def test_checkpoint_seam_redirects_all_commands_away_from_live():
    """With MIND_CHECKPOINT_PATH pinned, init/update/read/clear follow the tmp
    file and the live tree stays byte-identical — even under a simulated live
    MIND_SID (the body-keyed channel's input)."""
    live = _leak_targets()
    before = _snapshot(live)

    tmp = Path(tempfile.mkdtemp(prefix="ckpt-seam-g11511509-"))
    pin = str(tmp / "iteration-checkpoint.json")
    env_extra = {"MIND_SID": "sim-g11511509-no-fork", "MIND_CHECKPOINT_PATH": pin}

    p = _lss(env_extra, "init", stdin=_ANCHOR)
    assert p.returncode == 0, f"init rc={p.returncode}: {p.stderr}"
    assert Path(pin).exists(), "init did not write the pinned tmp path — seam inert"
    assert json.loads(Path(pin).read_text(encoding="utf-8"))["goal_id"] == "g-001-01"

    p = _lss(env_extra, "update", "--set", "phase=executed")
    assert p.returncode == 0, f"update rc={p.returncode}: {p.stderr}"
    assert json.loads(Path(pin).read_text(encoding="utf-8"))["phase"] == "executed"

    p = _lss(env_extra, "read")
    assert p.returncode == 0
    assert json.loads(p.stdout)["goal_id"] == "g-001-01"

    p = _lss(env_extra, "clear", "--if-goal", "g-001-01")
    assert p.returncode == 0, f"clear rc={p.returncode}: {p.stderr}"
    assert not Path(pin).exists(), "clear did not remove the pinned tmp path"

    _assert_identical(before, _snapshot(live), live)


def test_checkpoint_seam_missing_pin_keeps_production_resolution():
    """Without the pin the path resolves exactly as before (body_state_path
    fallback) — the seam is an override, not a behaviour change. Assert via
    read: with no forked working-memory.yaml for the sim sid the fallback is
    the agent-wide path, and a read there must NOT see the tmp file's content.
    (read is the only command exercised: it is the one with zero write
    side effects on the live tree.)"""
    tmp = Path(tempfile.mkdtemp(prefix="ckpt-seam-g11511509-"))
    pin = tmp / "iteration-checkpoint.json"
    pin.write_text(_ANCHOR, encoding="utf-8")

    # Pin set: read follows the tmp file.
    p = _lss({"MIND_SID": "sim-g11511509-no-fork",
              "MIND_CHECKPOINT_PATH": str(pin)}, "read")
    assert p.returncode == 0 and json.loads(p.stdout)["goal_id"] == "g-001-01"

    # Pin absent, same sim sid (no forked WM on the real tree for it): read
    # resolves the production fallback. Whatever that resolves to, it must be
    # a DIFFERENT file than the pin — i.e. the tmp content is not visible
    # through production resolution.
    p = _lss({"MIND_SID": "sim-g11511509-no-fork"}, "read")
    if p.returncode == 0:
        data = json.loads(p.stdout)
        assert data.get("goal_id") != "g-001-01" or Path(
            REPO_ROOT / "agents" / "alpha" / "session" / "iteration-checkpoint.json"
        ).exists(), (
            "read without the pin returned the tmp file's content — the seam "
            "leaks into production resolution"
        )
