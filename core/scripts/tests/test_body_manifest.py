"""Body-manifest helper tests (Phase 1B, ).

Covers the SOLE writer of `sessions/<unitKey>/body-manifest.yaml` and the
reducer-aware fork decision that is the Mind/Body backward-compatibility
keystone:

  - The REDUCER Body (no running-session-id, or running-session-id == its own
    unitKey) does NOT fork: forked_wm_hash is null, NO body-WM-file is created,
    so Phase 1A routing (keyed on the body-WM-file's existence) stays agent-wide
    — today's behavior, unchanged.
  - A NON-reducer worker (running-session-id names a DIFFERENT live Body) forks:
    the Mind WM is copied as the Body baseline and its sha256 recorded.
  - Observers never fork (read-only).
  - Lifecycle: write(active) -> set-state(closed-pending-merge) preserves every
    other field.

Daemon-safe (no daemon_integration marker — pure path + file arithmetic).

Run:
  python -m pytest core/scripts/tests/test_body_manifest.py -q
"""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent  # core/scripts/


def _load_body_manifest():
    """Load the hyphen-named module via importlib (not importable by name)."""
    spec = importlib.util.spec_from_file_location(
        "body_manifest", CORE_SCRIPTS / "body-manifest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bm = _load_body_manifest()


# A SID-shaped string (the validator requires the Claude Code UUID shape).
SID_A = "11111111-1111-4111-8111-111111111111"
SID_B = "22222222-2222-4222-8222-222222222222"


def _mk_agent(tmp_path: Path, name: str = "alpha",
              running_sid: str | None = None, wm_text: str | None = None) -> Path:
    """Build a minimal tmp agent dir: agents/<name>/session/ with optional
    running-session-id + working-memory.yaml. Returns the project_root."""
    adir = tmp_path / "agents" / name
    state = adir / "session"
    state.mkdir(parents=True, exist_ok=True)
    if running_sid is not None:
        (state / "running-session-id").write_text(running_sid, encoding="utf-8")
    if wm_text is not None:
        # write_bytes (not write_text) so the on-disk WM is byte-exact across
        # platforms — Windows write_text would translate \n->\r\n and make the
        # forked_wm_hash assertion platform-dependent.
        (state / "working-memory.yaml").write_bytes(wm_text.encode("utf-8"))
    return tmp_path


def _read(pr: Path, agent: str, sid: str) -> dict:
    return bm.read_manifest(sid, agent, project_root=pr)


# ─────────────────────────── reducer (no fork) ───────────────────────────

def test_reducer_no_running_sid_does_not_fork(tmp_path):
    pr = _mk_agent(tmp_path, wm_text="slot: x\n")
    path = bm.write_manifest(SID_A, "alpha", project_root=pr)
    data = _read(pr, "alpha", SID_A)
    assert data["forked_wm_hash"] is None
    assert data["body_state"] == "active"
    assert data["unitKey"] == SID_A and data["mindKey"] == "alpha"
    # NO body-WM-file => Phase 1A routing stays agent-wide (the keystone).
    assert not (path.parent / "working-memory.yaml").exists()


def test_reducer_running_sid_equals_self_does_not_fork(tmp_path):
    pr = _mk_agent(tmp_path, running_sid=SID_A, wm_text="slot: x\n")
    bm.write_manifest(SID_A, "alpha", project_root=pr)
    data = _read(pr, "alpha", SID_A)
    assert data["forked_wm_hash"] is None
    assert bm.is_reducer(SID_A, "alpha", project_root=pr) is True


# ─────────────────────────── non-reducer (fork) ───────────────────────────

def test_nonreducer_worker_forks_and_hashes(tmp_path):
    wm = "slot: original\ncounter: 3\n"
    # A DIFFERENT Body (SID_A) already holds the reducer slot.
    pr = _mk_agent(tmp_path, running_sid=SID_A, wm_text=wm)
    path = bm.write_manifest(SID_B, "alpha", role="worker", project_root=pr)
    data = _read(pr, "alpha", SID_B)
    expected_hash = hashlib.sha256(wm.encode("utf-8")).hexdigest()
    assert data["forked_wm_hash"] == expected_hash
    # body-WM-file created as a BYTE-FAITHFUL copy of the Mind WM => routing
    # flips per-Body, and the bytes match the hashed baseline.
    body_wm = path.parent / "working-memory.yaml"
    assert body_wm.exists()
    assert body_wm.read_bytes() == wm.encode("utf-8")
    assert bm.is_reducer(SID_B, "alpha", project_root=pr) is False


def test_observer_never_forks(tmp_path):
    # Even with a different reducer present, an observer must not fork.
    pr = _mk_agent(tmp_path, running_sid=SID_A, wm_text="slot: x\n")
    path = bm.write_manifest(SID_B, "alpha", role="observer", project_root=pr)
    data = _read(pr, "alpha", SID_B)
    assert data["role"] == "observer"
    assert data["forked_wm_hash"] is None
    assert not (path.parent / "working-memory.yaml").exists()


# ─────────────────────────── lifecycle + validation ───────────────────────────

def test_set_state_preserves_other_fields(tmp_path):
    pr = _mk_agent(tmp_path, wm_text="slot: x\n")
    bm.write_manifest(SID_A, "alpha", env_id="env-7", project_root=pr)
    bm.set_state(SID_A, "alpha", "closed-pending-merge", project_root=pr)
    data = _read(pr, "alpha", SID_A)
    assert data["body_state"] == "closed-pending-merge"
    assert data["env_id"] == "env-7"          # preserved
    assert data["unitKey"] == SID_A           # preserved


def test_set_state_rejects_invalid(tmp_path):
    pr = _mk_agent(tmp_path, wm_text="slot: x\n")
    bm.write_manifest(SID_A, "alpha", project_root=pr)
    with pytest.raises(ValueError):
        bm.set_state(SID_A, "alpha", "bogus-state", project_root=pr)


def test_write_rejects_invalid_role(tmp_path):
    pr = _mk_agent(tmp_path, wm_text="slot: x\n")
    with pytest.raises(ValueError):
        bm.write_manifest(SID_A, "alpha", role="captain", project_root=pr)


# ─────────────── : fork-time immutable baseline snapshot ───────────────

def test_nonreducer_fork_writes_immutable_baseline(tmp_path):
    # The non-reducer worker fork must ALSO snapshot forked-wm-baseline.yaml — the
    # frozen common ancestor for generalize-down's 3-way delta — byte-faithfully
    # beside the live (mutating) body WM.
    wm = "slot: original\ncounter: 3\n"
    pr = _mk_agent(tmp_path, running_sid=SID_A, wm_text=wm)
    path = bm.write_manifest(SID_B, "alpha", role="worker", project_root=pr)
    session_dir = path.parent
    baseline = session_dir / bm._BASELINE_FILENAME
    assert baseline.exists(), "non-reducer fork must snapshot an immutable baseline"
    # Byte-faithful copy of the Mind WM == the exact bytes the hash was taken over.
    assert baseline.read_bytes() == wm.encode("utf-8")
    data = _read(pr, "alpha", SID_B)
    assert hashlib.sha256(baseline.read_bytes()).hexdigest() == data["forked_wm_hash"]
    # The live body WM and the baseline start identical; the baseline then stays
    # frozen while the live WM diverges as the Body works.
    assert (session_dir / "working-memory.yaml").read_bytes() == baseline.read_bytes()


def test_reducer_no_fork_writes_no_baseline(tmp_path):
    # No running-session-id -> reducer -> no fork -> no baseline snapshot.
    pr = _mk_agent(tmp_path, wm_text="slot: x\n")
    path = bm.write_manifest(SID_A, "alpha", project_root=pr)
    assert not (path.parent / bm._BASELINE_FILENAME).exists(), \
        "reducer never forks -> no baseline snapshot"


def test_observer_no_baseline(tmp_path):
    pr = _mk_agent(tmp_path, running_sid=SID_A, wm_text="slot: x\n")
    path = bm.write_manifest(SID_B, "alpha", role="observer", project_root=pr)
    assert not (path.parent / bm._BASELINE_FILENAME).exists()


# ─────────────── : close_body_on_genuine (genuine-close gating) ───────────────

def _mk_body_session(pr: Path, agent: str, sid: str, *,
                     wm: bool = True, sentinel: bool = False,
                     manifest_state: str | None = "active") -> Path:
    """Create agents/<agent>/sessions/<sid>/ with optional forked body WM, the
    body-closing sentinel, and a manifest at the given body_state (None = absent).
    Creating the session dir makes agents/<agent>/ exist for _agent_paths."""
    session_dir = pr / "agents" / agent / "sessions" / sid
    session_dir.mkdir(parents=True, exist_ok=True)
    if wm:
        (session_dir / "working-memory.yaml").write_text(
            "slots:\n  forked: true\n", encoding="utf-8")
    if sentinel:
        (session_dir / bm._CLOSE_SENTINEL_FILENAME).write_text("", encoding="utf-8")
    if manifest_state is not None:
        (session_dir / "body-manifest.yaml").write_text(
            f"unitKey: {sid}\nmindKey: {agent}\nbody_state: {manifest_state}\n",
            encoding="utf-8")
    return session_dir


def test_close_genuine_no_forked_wm(tmp_path):
    # Reducer/observer: no body WM file -> noop, returns BEFORE sentinel logic.
    sd = _mk_body_session(tmp_path, "alpha", SID_A, wm=False, sentinel=True)
    assert bm.close_body_on_genuine(SID_A, "alpha", project_root=tmp_path) == "no-forked-wm"
    assert (sd / bm._CLOSE_SENTINEL_FILENAME).exists()  # not consumed (early return)


def test_close_genuine_no_sentinel(tmp_path):
    # Body WM present but no body-closing sentinel -> a between-turns turn-end.
    _mk_body_session(tmp_path, "alpha", SID_A, wm=True, sentinel=False)
    assert bm.close_body_on_genuine(SID_A, "alpha", project_root=tmp_path) == "no-sentinel"
    assert _read(tmp_path, "alpha", SID_A)["body_state"] == "active"  # NOT queued


def test_close_genuine_no_manifest(tmp_path):
    # Sentinel + body WM present, manifest missing -> noop, sentinel consumed.
    sd = _mk_body_session(tmp_path, "alpha", SID_A, wm=True, sentinel=True,
                          manifest_state=None)
    assert bm.close_body_on_genuine(SID_A, "alpha", project_root=tmp_path) == "no-manifest"
    assert not (sd / bm._CLOSE_SENTINEL_FILENAME).exists()  # consumed


def test_close_genuine_not_active(tmp_path):
    # Genuine close but already merged (consumed re-fire) -> noop, no re-mark.
    sd = _mk_body_session(tmp_path, "alpha", SID_A, wm=True, sentinel=True,
                          manifest_state="merged")
    assert bm.close_body_on_genuine(SID_A, "alpha", project_root=tmp_path) == "not-active"
    assert _read(tmp_path, "alpha", SID_A)["body_state"] == "merged"  # unchanged
    assert not (sd / bm._CLOSE_SENTINEL_FILENAME).exists()  # consumed


def test_close_genuine_marks_and_consumes(tmp_path):
    # Happy path: genuine close + active -> closed-pending-merge, sentinel gone.
    sd = _mk_body_session(tmp_path, "alpha", SID_A, wm=True, sentinel=True,
                          manifest_state="active")
    assert bm.close_body_on_genuine(SID_A, "alpha", project_root=tmp_path) == "marked"
    assert _read(tmp_path, "alpha", SID_A)["body_state"] == "closed-pending-merge"
    assert not (sd / bm._CLOSE_SENTINEL_FILENAME).exists()  # consumed
    # Idempotent: re-fire (sentinel already gone) returns no-sentinel, no re-mark.
    assert bm.close_body_on_genuine(SID_A, "alpha", project_root=tmp_path) == "no-sentinel"
    assert _read(tmp_path, "alpha", SID_A)["body_state"] == "closed-pending-merge"


# ─────────────── : explicit reducer role + reducer_sid field ───────────────

def test_explicit_reducer_role_no_fork(tmp_path):
    # Passing role="reducer" explicitly: manifest records role=reducer,
    # reducer_sid=null (reducer IS the Reducer), no WM fork (Phase 1B inert).
    pr = _mk_agent(tmp_path, wm_text="slot: x\n")
    path = bm.write_manifest(SID_A, "alpha", role="reducer", project_root=pr)
    data = _read(pr, "alpha", SID_A)
    assert data["role"] == "reducer"
    assert data["reducer_sid"] is None, "reducer_sid must be null for the reducer itself"
    assert data["forked_wm_hash"] is None, "reducer never forks its WM"
    assert data["body_state"] == "active"
    # NO body-WM-file -> Phase 1A routing stays agent-wide (backward-compatible).
    body_wm = path.parent / "working-memory.yaml"
    assert not body_wm.exists(), "reducer must not create a per-Body WM file"


def test_worker_has_correct_reducer_sid(tmp_path):
    # With SID_A holding running-session-id, a new worker body (SID_B) must record
    # reducer_sid = SID_A so the framework-ES can locate the Reducer's es-snapshot.yaml.
    pr = _mk_agent(tmp_path, running_sid=SID_A, wm_text="slot: x\n")
    path = bm.write_manifest(SID_B, "alpha", role="worker", project_root=pr)
    data = _read(pr, "alpha", SID_B)
    assert data["role"] == "worker"
    assert data["reducer_sid"] == SID_A, "worker must carry the reducer's SID"
    # Non-reducer worker forks: hash and body-WM-file must both be present.
    assert data["forked_wm_hash"] is not None
    assert (path.parent / "working-memory.yaml").exists()


def test_observer_has_correct_reducer_sid(tmp_path):
    # Observer also records reducer_sid but never forks (read-only).
    pr = _mk_agent(tmp_path, running_sid=SID_A, wm_text="slot: x\n")
    path = bm.write_manifest(SID_B, "alpha", role="observer", project_root=pr)
    data = _read(pr, "alpha", SID_B)
    assert data["role"] == "observer"
    assert data["reducer_sid"] == SID_A, "observer must carry the reducer's SID"
    assert data["forked_wm_hash"] is None, "observer never forks"
    assert not (path.parent / "working-memory.yaml").exists(), \
        "observer must not create a per-Body WM file"


# ─────────────── 2026-08-10: (re-)write consumes a stale close sentinel ───────────────

def test_write_consumes_stale_close_sentinel(tmp_path):
    # The stale-sentinel edge (fresh-eyes review of b8ac6a4cf): a worker touched
    # body-closing at genuine close, the turn ended in TEXT, and Claude Code
    # skipped the Stop event (rb-629 gap) — so nothing consumed the sentinel.
    # If /start later re-forks the SAME SID (--continue), the fresh active
    # manifest would pair with the stale sentinel and the re-forked Body's
    # FIRST turn-end would take the stop-hook's WM+sentinel close branch:
    # closed-pending-merge after one work unit. write_manifest resets to
    # active, so it must consume the stale close signal with the same stroke.
    pr = _mk_agent(tmp_path, running_sid=SID_A, wm_text="slot: x\n")
    # First life: forked worker whose close began (sentinel written) but whose
    # Stop event never fired (manifest still active, sentinel still present).
    bm.write_manifest(SID_B, "alpha", role="worker", project_root=pr)
    session_dir = pr / "agents" / "alpha" / "sessions" / SID_B
    (session_dir / bm._CLOSE_SENTINEL_FILENAME).write_text("", encoding="utf-8")
    # Second life: /start re-forks the same SID.
    bm.write_manifest(SID_B, "alpha", role="worker", project_root=pr)
    assert not (session_dir / bm._CLOSE_SENTINEL_FILENAME).exists(), \
        "reset-to-active must consume a stale body-closing sentinel"
    assert _read(pr, "alpha", SID_B)["body_state"] == "active"
    # The behavioral point: the re-forked Body's first turn-end is a clean
    # between-turns 'no-sentinel', NOT a premature genuine close. On the
    # pre-fix write_manifest this scenario returns 'marked' (discrimination
    # probed against b8ac6a4cf's version when this test was authored).
    assert bm.close_body_on_genuine(SID_B, "alpha", project_root=pr) == "no-sentinel"
    assert _read(pr, "alpha", SID_B)["body_state"] == "active"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
