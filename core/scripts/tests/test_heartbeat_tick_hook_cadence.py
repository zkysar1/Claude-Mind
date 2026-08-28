"""heartbeat-tick.sh at tool-call cadence: `--body-only` and the ungated lease — .

WHAT BROKE (measured 2026-08-28, coach on zc-03, STORAGE_BACKEND unset = local, a
served 27B): the reducer acquired its runner lease at /start and never renewed
it. Two independent defects, one symptom ("Reducer STALE ... this Body PARKED"):

  1. heartbeat-tick.sh renewed the claim ONLY under STORAGE_BACKEND=own-cloud.
     g-306-331 gave the local backend a real claim store (a git ref) and re-keyed
     the READ side (`runner-claim.sh status`) on capability, but this WRITE side
     kept keying on the backend NAME. A scoped fix present and inert (rb-9476).
  2. every tick caller was keyed to loop structure (one per iteration, one per
     diary write, one per 60s of sleep). A runner that issues tool calls for
     hours without completing an iteration or writing a breadcrumb never ticked;
     precheck alone ran 1h53m and the claim aged to 6544s.

The hook caller (bash-agent-inject.py, pinned in test_bash_inject_heartbeat_tick.py)
passes `--body-only` for any Body that is not the reducer, because a SAME-BOX
worker shares agent-state=RUNNING and the tick's state gate cannot separate the
roles for it.

WHAT THESE TESTS PIN:
  1. `--body-only` writes this SID's carrier and the same-box heartbeat file, and
     touches NOTHING agent-wide: no runner-heartbeat, no team-state, no claim
     renewal, no fence. Exit 0.
  2. a full tick on the LOCAL backend renews the claim and runs the fence
     (positive control: the pre-fix name gate skipped both).
  3. the same holds with NO backend named anywhere (coach's exact shape).
  4. own-cloud is unchanged: it renews too.

HERMETIC the way the sibling files are: a RELOCATED PROJECT_ROOT, scripts COPIED
(never symlinked — guard-2534), every sibling the tick shells out to replaced by
a recorder stub, tmp world/meta pinned (guard-2337). STORAGE_BACKEND=local on
every run that does not deliberately set own-cloud (guard-955).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SID = "77777777-1111-2222-3333-777777777777"
AGENT = "alpha"


def _bash_cmd():
    sys.path.insert(0, str(REPO / "core" / "scripts"))
    from _runtime_bash import bash_cmd  # noqa: E402

    return bash_cmd


def _stub(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _stage(tmp_path: Path, *, env_line: str | None) -> tuple[Path, Path, Path]:
    """Relocated root. Returns (root, session_dir, calls_log)."""
    root = tmp_path / "repo"
    scripts = root / "core" / "scripts"
    scripts.mkdir(parents=True)
    (root / "core" / "config").mkdir()
    for name in ("heartbeat-tick.sh", "_paths.sh", "_platform.sh"):
        shutil.copy2(REPO / "core" / "scripts" / name, scripts / name)
    calls = root / "calls.log"
    _stub(scripts / "session-state-get.sh", "echo RUNNING")
    for name in ("team-state-update.sh", "live-phase-emit.sh", "runner-claim.sh",
                 "reducer-self-fence.sh", "session-signal-exists.sh"):
        _stub(scripts / name, f'echo "{name} $*" >> "{calls.as_posix()}"; exit 0')
    sess = root / "agents" / AGENT / "session"
    sess.mkdir(parents=True)
    (sess / "agent-state").write_text("RUNNING", encoding="utf-8")
    (root / "agents" / AGENT / "sessions" / SID).mkdir(parents=True)
    if env_line is not None:
        (root / ".env.local").write_text(env_line, encoding="utf-8")
    return root, sess, calls


def _tick(root: Path, *args: str, backend: str | None = "local") -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({
        "MIND_AGENT": AGENT,
        "MIND_AGENT_DIR": str(root / "agents" / AGENT),
        "PROJECT_ROOT": str(root),
        "MIND_SID": SID,
    })
    if backend is None:
        env.pop("STORAGE_BACKEND", None)
    else:
        env["STORAGE_BACKEND"] = backend
    for var, sub in (("MIND_WORLD", "world"), ("MIND_META", "meta")):
        (root / sub).mkdir(parents=True, exist_ok=True)
        env[var] = str(root / sub)
    return subprocess.run(
        _bash_cmd()(str(root / "core" / "scripts" / "heartbeat-tick.sh"), *args),
        capture_output=True, text=True, timeout=120, env=env, cwd=str(root))


def _calls(calls: Path) -> str:
    return calls.read_text(encoding="utf-8") if calls.exists() else ""


# --- 1. --body-only: this SID's carrier, nothing agent-wide -------------------
def test_body_only_refreshes_the_carrier_and_nothing_agent_wide(tmp_path):
    root, sess, calls = _stage(tmp_path, env_line="STORAGE_BACKEND=local\n")
    r = _tick(root, "--body-only")
    assert r.returncode == 0, f"rc={r.returncode} stderr={r.stderr[-400:]}"
    carrier = sess / f"body-heartbeat-{SID}.json"
    assert carrier.exists(), f"no carrier written. stderr={r.stderr[-400:]}"
    assert json.loads(carrier.read_text(encoding="utf-8"))["sid"] == SID
    assert (root / "agents" / AGENT / "sessions" / SID / "body-heartbeat").exists()
    assert not (sess / "runner-heartbeat").exists(), (
        "--body-only touched the agent-wide runner-heartbeat — a same-box worker "
        "would keep a dead reducer looking alive")
    logged = _calls(calls)
    for forbidden in ("team-state-update.sh", "runner-claim.sh", "reducer-self-fence.sh"):
        assert forbidden not in logged, (
            f"--body-only reached {forbidden}; the agent-wide legs are the "
            f"reducer's alone. calls={logged!r}")


# --- 2. LOCAL backend: the lease IS renewed and the fence runs ----------------
def test_full_tick_on_local_backend_renews_the_claim_and_runs_the_fence(tmp_path):
    root, sess, calls = _stage(tmp_path, env_line="STORAGE_BACKEND=local\n")
    r = _tick(root, backend="local")
    assert r.returncode == 0, f"rc={r.returncode} stderr={r.stderr[-400:]}"
    logged = _calls(calls)
    assert f"runner-claim.sh heartbeat --agent {AGENT}" in logged, (
        "the local backend's lease was NOT renewed — the pre-g-115-8200 name gate "
        f"skipped it, so a local reducer's claim aged out 65min after /start. "
        f"calls={logged!r}")
    assert "reducer-self-fence.sh" in logged, (
        f"the self-fence did not run on the local backend. calls={logged!r}")
    assert (sess / "runner-heartbeat").exists()
    assert (sess / f"body-heartbeat-{SID}.json").exists()


# --- 3. no backend named ANYWHERE (coach's shape) -----------------------------
def test_full_tick_with_no_backend_named_anywhere_still_renews(tmp_path):
    root, sess, calls = _stage(tmp_path, env_line=None)
    r = _tick(root, backend=None)
    assert r.returncode == 0, f"rc={r.returncode} stderr={r.stderr[-400:]}"
    assert f"runner-claim.sh heartbeat --agent {AGENT}" in _calls(calls), (
        "with STORAGE_BACKEND unset and no .env.local the lease was not renewed; "
        "this is exactly the coach/zc-03 shape that parked the worker")


# --- 4. own-cloud unchanged -----------------------------------------------------
def test_full_tick_on_own_cloud_still_renews(tmp_path):
    root, sess, calls = _stage(tmp_path, env_line="STORAGE_BACKEND=own-cloud\n")
    r = _tick(root, backend="own-cloud")
    assert r.returncode == 0, f"rc={r.returncode} stderr={r.stderr[-400:]}"
    assert f"runner-claim.sh heartbeat --agent {AGENT}" in _calls(calls)
