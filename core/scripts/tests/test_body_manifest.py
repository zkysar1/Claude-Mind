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


# ------------------------------------------------ park / resume ()
#
# `parked` is the first body_state that is NOT active and NOT terminal. The
# whole hazard class it introduces is that "not active" and "closed" were
# interchangeable predicates before it existed, so these tests pin the
# SEPARATION as hard as they pin the mechanics.

def test_park_marks_and_stamps(tmp_path):
    _mk_body_session(tmp_path, "alpha", SID_A, manifest_state="active")
    assert bm.park_body(SID_A, "alpha", project_root=tmp_path) == "parked"
    m = _read(tmp_path, "alpha", SID_A)
    assert m["body_state"] == "parked"
    assert m.get("parked_at"), "the cap cannot be measured without a stamp"


def test_repark_is_idempotent_and_preserves_the_ORIGINAL_stamp(tmp_path):
    """The cap must measure the WHOLE park, not the last poll.

    Re-stamping on every hourly re-park would make PARK_MAX_HOURS unreachable:
    the elapsed time would reset to ~0 every hour and the Body would never
    expire, so a genuinely abandoned Body would poll forever instead of closing
    and telling a human.
    """
    import datetime as _dt
    _mk_body_session(tmp_path, "alpha", SID_A, manifest_state="active")
    bm.park_body(SID_A, "alpha", project_root=tmp_path)

    # BACKDATE BEFORE RE-PARKING. Comparing the stamp to itself across two
    # immediate calls proves nothing: `_now_iso_local()` has SECOND resolution,
    # so a re-stamping implementation produces a byte-identical value inside the
    # same second and an equality assertion passes forever while the property is
    # broken. Verified by mutation — the naive form killed zero mutants. An
    # explicit backdate makes the two outcomes differ by hours, independent of
    # clock resolution and of how fast the test runs.
    p = tmp_path / "agents" / "alpha" / "sessions" / SID_A / "body-manifest.yaml"
    old = (_dt.datetime.now() - _dt.timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S")
    p.write_text(p.read_text(encoding="utf-8").replace(
        _read(tmp_path, "alpha", SID_A)["parked_at"], old), encoding="utf-8")

    assert bm.park_body(SID_A, "alpha", project_root=tmp_path) == "already-parked"
    assert _read(tmp_path, "alpha", SID_A)["parked_at"] == old, (
        "a re-park must not restart the clock — the cap would be unreachable")


def test_resume_returns_to_active_and_clears_the_stamp(tmp_path):
    """A Body that parked, resumed, and parks again has NOT been unattended for
    the sum of both windows — a carried-over stamp would expire it early, which
    is the wrong (unrecoverable) direction."""
    _mk_body_session(tmp_path, "alpha", SID_A, manifest_state="active")
    bm.park_body(SID_A, "alpha", project_root=tmp_path)
    assert bm.resume_body(SID_A, "alpha", project_root=tmp_path) == "resumed"
    m = _read(tmp_path, "alpha", SID_A)
    assert m["body_state"] == "active"
    assert "parked_at" not in m


def test_resume_on_an_unparked_body_is_a_noop(tmp_path):
    _mk_body_session(tmp_path, "alpha", SID_A, manifest_state="active")
    assert bm.resume_body(SID_A, "alpha", project_root=tmp_path) == "not-parked"
    assert _read(tmp_path, "alpha", SID_A)["body_state"] == "active"


@pytest.mark.parametrize("closed",
                         ["closed-pending-merge", "merged", "closed-stale"])
def test_park_refuses_a_CLOSED_body(tmp_path, closed):
    """Parking a closed Body would resurrect it into the poll loop after its WM
    was already staged — the divergence-after-merge hazard close_body_on_genuine
    exists to prevent."""
    _mk_body_session(tmp_path, "alpha", SID_A, manifest_state=closed)
    assert bm.park_body(SID_A, "alpha", project_root=tmp_path) == "not-active"
    assert _read(tmp_path, "alpha", SID_A)["body_state"] == closed


def test_park_refuses_a_reducer_or_observer(tmp_path):
    """No forked WM => this is not a worker Body and has no park semantics."""
    _mk_body_session(tmp_path, "alpha", SID_A, wm=False, manifest_state="active")
    assert bm.park_body(SID_A, "alpha", project_root=tmp_path) == "no-forked-wm"


def test_park_expiry_is_false_before_the_cap_and_true_after(tmp_path):
    import datetime as _dt
    _mk_body_session(tmp_path, "alpha", SID_A, manifest_state="active")
    bm.park_body(SID_A, "alpha", project_root=tmp_path)
    assert bm.park_expired(SID_A, "alpha", project_root=tmp_path) is False

    sd = tmp_path / "agents" / "alpha" / "sessions" / SID_A / "body-manifest.yaml"
    old = (_dt.datetime.now() - _dt.timedelta(hours=bm.PARK_MAX_HOURS + 1)
           ).strftime("%Y-%m-%dT%H:%M:%S")
    sd.write_text(sd.read_text(encoding="utf-8").replace(
        _read(tmp_path, "alpha", SID_A)["parked_at"], old), encoding="utf-8")
    assert bm.park_expired(SID_A, "alpha", project_root=tmp_path) is True


@pytest.mark.parametrize("stamp", ["", "not-a-timestamp", "2026-13-45T99:99:99"])
def test_park_expiry_FAILS_TOWARD_STAYING_PARKED(tmp_path, stamp):
    """A stamp problem must never close a Body.

    The tempting alternative — unreadable stamp means "assume expired" — makes a
    FIELD-FORMAT bug durably close a live worker, and Phase -0 then refuses every
    further unit until a human runs the user-only /start. A park that runs long
    costs one poll per hour and is visible on the board.
    """
    _mk_body_session(tmp_path, "alpha", SID_A, manifest_state="active")
    bm.park_body(SID_A, "alpha", project_root=tmp_path)
    p = tmp_path / "agents" / "alpha" / "sessions" / SID_A / "body-manifest.yaml"
    cur = _read(tmp_path, "alpha", SID_A)["parked_at"]
    p.write_text(p.read_text(encoding="utf-8").replace(cur, stamp), encoding="utf-8")
    assert bm.park_expired(SID_A, "alpha", project_root=tmp_path) is False


def test_park_expiry_is_false_for_a_body_that_is_not_parked(tmp_path):
    _mk_body_session(tmp_path, "alpha", SID_A, manifest_state="active")
    assert bm.park_expired(SID_A, "alpha", project_root=tmp_path) is False


def test_parked_is_a_valid_state_but_NOT_in_the_closed_set(tmp_path):
    """The predicate rule, pinned at the module level.

    Three live consumers keyed on `!= active` before this landed (worker-loop's
    Phase -0 gate, the deadman resurrection prompt, the stop-hook worker-net) and
    each would have refused, wedged, or closed the exact Body parking keeps
    alive. Any NEW consumer must name the closed states explicitly.
    """
    assert "parked" in bm.VALID_STATES
    assert bm.PARK_MAX_HOURS > 0
    assert "parked" in bm.CLOSEABLE_STATES, "a park is a LIVE Body"
    assert "parked" not in bm.CLOSED_STATES
    assert set(bm.CLOSEABLE_STATES) | set(bm.CLOSED_STATES) == set(bm.VALID_STATES), (
        "every state must land in exactly one half or a consumer inherits a gap")
    assert not set(bm.CLOSEABLE_STATES) & set(bm.CLOSED_STATES)


def test_a_parked_body_CAN_still_be_genuinely_closed(tmp_path):
    """The expiry path depends entirely on this, and it was broken.

    A park ends for real two ways — the 60h cap, or a user stop — and both touch
    the body-closing sentinel and route through close_body_on_genuine. Under the
    original `!= "active"` predicate that returned 'not-active': sentinel
    consumed, NOTHING staged, manifest left at `parked` forever with the Body's
    divergent WM stranded — and the close reported as a benign no-op, so nothing
    anywhere would have said so.

    Found by writing the test, not by reading the code: the first version of this
    file asserted 'not-active' was correct here, because that is what the code
    did.
    """
    _mk_body_session(tmp_path, "alpha", SID_A, sentinel=True, manifest_state="active")
    bm.park_body(SID_A, "alpha", project_root=tmp_path)
    sd = tmp_path / "agents" / "alpha" / "sessions" / SID_A
    assert bm.close_body_on_genuine(SID_A, "alpha", project_root=tmp_path) == "marked"
    assert _read(tmp_path, "alpha", SID_A)["body_state"] == "closed-pending-merge"
    assert not (sd / bm._CLOSE_SENTINEL_FILENAME).exists()


@pytest.mark.parametrize("closed",
                         ["closed-pending-merge", "merged", "closed-stale"])
def test_an_already_closed_body_is_still_never_requeued(tmp_path, closed):
    """The other half of the same predicate: widening it to admit `parked` must
    not weaken the idempotence guarantee it was written for."""
    sd = _mk_body_session(tmp_path, "alpha", SID_A, sentinel=True,
                          manifest_state=closed)
    assert bm.close_body_on_genuine(SID_A, "alpha", project_root=tmp_path) == "not-active"
    assert _read(tmp_path, "alpha", SID_A)["body_state"] == closed
    assert not (sd / bm._CLOSE_SENTINEL_FILENAME).exists()


# ------------------------------------------------- carrier state mirror ()
#
# THE WRITE HALF. worker_stall.classify_body is a pure classifier and its own
# tests prove only that it reads `body_state` correctly -- they say nothing
# about whether the field ever reaches the carrier. A direction that is
# unit-tested but never exercised on a real qualifying write carries no evidence
# it can fire in production (guard-1937), and here the failure would be silent
# in the WORST direction: without the mirror, every cleanly-closed Body leaves a
# carrier still reading `active`, goes stale, and is reported as a stall. The
# split would flood on exactly the population it exists to keep quiet.

def _carrier(project_root: Path, agent: str, sid: str) -> Path:
    return project_root / "agents" / agent / "session" / f"body-heartbeat-{sid}.json"


def _write_carrier(project_root: Path, agent: str, sid: str, **extra) -> Path:
    """The shape heartbeat-tick.sh writes."""
    import json
    p = _carrier(project_root, agent, sid)
    doc = {"sid": sid, "agent": agent, "host": "cc-test",
           "ts": "2026-08-18T10:00:00"}
    doc.update(extra)
    p.write_text(json.dumps(doc) + "\n", encoding="utf-8")
    return p


def _carrier_doc(project_root: Path, agent: str, sid: str) -> dict:
    import json
    return json.loads(_carrier(project_root, agent, sid).read_text(encoding="utf-8"))


def test_set_state_mirrors_into_the_syncable_carrier(tmp_path):
    """The close path's state must reach the ONE file a peer can actually read.

    body-manifest.yaml lives under `sessions/`, which is in
    owncloud_sync._EXCLUDE_DIRS (walk-pruned, never pushed), so a peer cannot
    read it at any price. The carrier is published, so the mirror is what makes
    a close visible off-box at all.
    """
    _mk_agent(tmp_path, "alpha", running_sid=SID_B)
    bm.write_manifest(SID_A, "alpha", role="worker", project_root=tmp_path)
    _write_carrier(tmp_path, "alpha", SID_A, body_state="active")

    bm.set_state(SID_A, "alpha", "closed-pending-merge", project_root=tmp_path)

    doc = _carrier_doc(tmp_path, "alpha", SID_A)
    assert doc["body_state"] == "closed-pending-merge"
    # EXACT SHAPE (guard-3948): the mirror must not drop or rename the fields
    # the reader already depends on -- `ts` in particular decides staleness, and
    # a mirror that clobbered it would make every closed Body look fresh.
    assert set(doc) == {"sid", "agent", "host", "ts", "body_state"}
    assert doc["ts"] == "2026-08-18T10:00:00"
    assert doc["sid"] == SID_A


def test_genuine_close_reaches_the_carrier_end_to_end(tmp_path):
    """Through the REAL close entry point, not set_state directly -- that is the
    call the stop-hook actually makes."""
    _mk_agent(tmp_path, "alpha", running_sid=SID_B, wm_text="slots: {}\n")
    bm.write_manifest(SID_A, "alpha", role="worker", project_root=tmp_path)
    sd = tmp_path / "agents" / "alpha" / "sessions" / SID_A
    (sd / bm._CLOSE_SENTINEL_FILENAME).write_text("", encoding="utf-8")
    _write_carrier(tmp_path, "alpha", SID_A, body_state="active")

    assert bm.close_body_on_genuine(SID_A, "alpha", project_root=tmp_path) == "marked"
    assert _carrier_doc(tmp_path, "alpha", SID_A)["body_state"] == "closed-pending-merge"


def test_park_and_resume_each_mirror(tmp_path):
    """park_body and resume_body write the manifest DIRECTLY (they must set
    `parked_at` in the same atomic write), so each needs its own mirror. A park
    is the case that would otherwise false-alert: a parked Body sits dormant
    between hourly re-polls, so a carrier left reading `active` goes stale and
    reads as a stall."""
    _mk_agent(tmp_path, "alpha", running_sid=SID_B, wm_text="slots: {}\n")
    bm.write_manifest(SID_A, "alpha", role="worker", project_root=tmp_path)
    _write_carrier(tmp_path, "alpha", SID_A, body_state="active")

    assert bm.park_body(SID_A, "alpha", project_root=tmp_path) == "parked"
    assert _carrier_doc(tmp_path, "alpha", SID_A)["body_state"] == "parked"

    assert bm.resume_body(SID_A, "alpha", project_root=tmp_path) == "resumed"
    assert _carrier_doc(tmp_path, "alpha", SID_A)["body_state"] == "active"


def test_mirror_is_fail_open_and_never_breaks_a_close(tmp_path):
    """A close must succeed when the carrier is absent, unparseable, or not an
    object. The reader renders a missing state as `stale_state_unknown`, which
    never alerts -- so a mirror failure degrades to the PRE-CHANGE behaviour
    rather than to a false alarm."""
    for setup in ("absent", "garbage", "not-an-object"):
        pr = tmp_path / setup
        pr.mkdir()
        _mk_agent(pr, "alpha", running_sid=SID_B)
        bm.write_manifest(SID_A, "alpha", role="worker", project_root=pr)
        if setup == "garbage":
            _carrier(pr, "alpha", SID_A).write_text("{not json", encoding="utf-8")
        elif setup == "not-an-object":
            _carrier(pr, "alpha", SID_A).write_text("[1,2,3]", encoding="utf-8")

        # The close still lands in the record of truth. Asserting the manifest
        # (not merely "no exception") is what proves the close path was REACHED
        # and completed, rather than the mirror having been skipped upstream
        # (guard-2536: a negative needs a positive marker in the same test).
        bm.set_state(SID_A, "alpha", "merged", project_root=pr)
        assert _read(pr, "alpha", SID_A)["body_state"] == "merged", setup


def test_manifest_is_written_before_the_mirror(tmp_path):
    """Ordering is load-bearing: a crash between the two writes must leave a
    correct manifest with a stale mirror (benign), never a carrier claiming a
    close the manifest never recorded."""
    _mk_agent(tmp_path, "alpha", running_sid=SID_B)
    bm.write_manifest(SID_A, "alpha", role="worker", project_root=tmp_path)
    _write_carrier(tmp_path, "alpha", SID_A, body_state="active")

    seen = {}
    real = bm._write_atomic

    def spy(target, body):
        if target.name.startswith("body-heartbeat-"):
            # Read the manifest as it stands AT THE MOMENT the mirror is written.
            seen["manifest_state"] = _read(tmp_path, "alpha", SID_A)["body_state"]
        return real(target, body)

    bm._write_atomic = spy
    try:
        bm.set_state(SID_A, "alpha", "merged", project_root=tmp_path)
    finally:
        bm._write_atomic = real

    assert seen.get("manifest_state") == "merged", (
        "the mirror ran before the manifest write landed")
