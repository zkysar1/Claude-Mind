"""The WM writers hold the lock across their READ and their WRITE ().

Remedy-side regression pins for the two no-stall lost-update paths reproduced
in g-115-8536. That file reproduces the SHAPE by driving body-merge's
primitives (`_read_yaml` / `_write_yaml_atomic`) by hand; those primitives are
deliberately still unlocked, because the lock belongs at the CALL SITE that
spans both. So a green reproduction there says nothing about whether the real
callers are safe -- which is exactly why these tests exist and why they call
the real entry points instead.

guard-5818: "enumerate every writer of that store and check, per writer,
whether it holds the SAME lock across its read and its write." One test per
writer, in the order that guardrail names them.

TWO INSTRUMENT SHAPES, and the difference is deliberate rather than cosmetic:

  * body-merge (both passes) is driven END-TO-END against a live daemon. Its
    store I/O sits OUTSIDE the lock by design, which leaves a deterministic
    seam (`_read_staged_yaml`) to fire a real POST /v1/wm/set from -- the
    write lands in precisely the window that used to swallow it, with no
    threading and no sleep. The assertion is the strong one: the daemon's
    value SURVIVES.
  * compact-restore-slots and wm-contamination-check have no such seam --
    after the fix their entire read->write span is inside the lock, so a
    synchronous set fired from within would simply block on it. These are
    pinned by LOCK-HELD OBSERVATION instead: at the moment of the read and of
    the write, the sibling .lock must exist. That proves the invariant ("the
    lock is held here") without racing it -- see _lock_is_held for why the
    obvious acquire_lock()-probe version is actively wrong.

Naming the weaker instrument as weaker is the point -- an exclusion pin is
evidence the lock is held, not evidence a concurrent writer survived, and
reporting the two as one thing is how a test suite starts overstating itself.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

TESTS_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = TESTS_DIR.parent
for _p in (str(TESTS_DIR), str(CORE_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _daemon_fixture import DaemonFixture  # noqa: E402

AGENT = "alpha"
SID_WORKER = "22222222-2222-4222-8222-222222222222"
_BAD_HASH = "0" * 64  # never matches a real WM -> forces the merge path
_WM_FILE = "working-memory.yaml"


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(modname, CORE_SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed(path: Path, data: dict) -> None:
    """Write a TMP fixture WM. Never a live store — every caller below builds
    its path under tmp_path or DaemonFixture's throwaway project root."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _get(port, slot):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/wm/read?slot={slot}&json=1")
    req.add_header("X-Mind-Agent", AGENT)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _set(port, slot, value):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/wm/set?slot={slot}",
        data=json.dumps(value).encode("utf-8"), method="POST")
    req.add_header("X-Mind-Agent", AGENT)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


@contextlib.contextmanager
def _daemon():
    """Live daemon over a tmp project root, with a seeded agent-wide WM."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd) / "world"
        world.mkdir()
        with DaemonFixture(world, agent=AGENT) as df:
            wm_path = df.project_root / "agents" / AGENT / "session" / _WM_FILE
            _seed(wm_path, {
                "session_start": "2026-09-02T00:00:00",
                "slots": {"reducer_own_slot": "untouched"},
                "slot_meta": {},
            })
            yield df, wm_path


def _lock_is_held(path: Path) -> bool:
    """True iff `path`'s sibling .lock exists right now.

    LocalBackend.acquire_lock creates the .lock with O_CREAT|O_EXCL and
    release_lock unlinks it, so its existence IS the held/not-held state.

    Do NOT probe this by calling acquire_lock() with a short timeout, which is
    the obvious-looking version and is wrong twice over: acquire_lock first
    queues on _write_queue's per-path in-process FIFO, so a probe issued from
    the holder's own process WAITS for a turn it can never get; and having
    waited stale_seconds it then STALE-BREAKS the very lock it was measuring,
    reporting "not held" while destroying the mutual exclusion under test.
    Measured while writing this file -- 4/4 pins failed with
    `[lock-stale-break] ... age=10.0s ... breaker_pid == holder_pid`.
    """
    return path.with_suffix(".lock").exists()


# ───────────────── writer 1: body-merge _consume_staged ─────────────────

def test_consume_staged_preserves_a_daemon_set_that_lands_mid_drain():
    """END-TO-END: a real POST /v1/wm/set inside the staged drain survives.

    Fired from `_read_staged_yaml`, which runs per-unit INSIDE the drain loop
    -- the window that, before this fix, sat between a pre-loop reducer read
    and the whole-file rewrite. Pre-fix that rewrite reverted the set with
    rc=0 at every step (the g-115-7322 signature); post-fix the read is taken
    fresh under the lock AFTER the loop, so the set is merged forward.
    """
    merge = _load("body_merge_consume", "body-merge.py")

    with _daemon() as (df, wm_path):
        state_dir = wm_path.parent
        staged = state_dir / merge._STAGED_DIRNAME
        staged.mkdir(parents=True, exist_ok=True)
        _seed(staged / "unit-1-wm.yaml", {
            "slots": {"worker_merge_marker": "merged-from-a-staged-body"},
            "slot_meta": {},
        })

        merge._get_backend = lambda: None  # hermetic: local-only, no store I/O

        fired = []

        def _fire_then_read(backend, path):
            # One real daemon write, in the pre-fix danger window.
            if not fired:
                fired.append(_set(df.port, "last_strategic_scan", "2026-09-02T13:31:58"))
                assert _get(df.port, "last_strategic_scan") == "2026-09-02T13:31:58"
            return None  # no baseline staged -> the retained 2-way fallback

        merge._read_staged_yaml = _fire_then_read

        summary = {"scanned": 0, "noop": [], "skipped": [], "staged_merged": [],
                   "staged_dedup": [], "staged_deferred": []}
        merge._consume_staged(state_dir, wm_path, summary, set())

        assert summary["staged_merged"] == ["unit-1"], summary
        assert fired and fired[0][0] == 200, fired
        # The merge landed ...
        assert _get(df.port, "worker_merge_marker") == "merged-from-a-staged-body"
        # ... and did NOT revert the concurrent daemon write.
        assert _get(df.port, "last_strategic_scan") == "2026-09-02T13:31:58", (
            "the staged drain reverted a verified daemon set — _consume_staged "
            "is reading the reducer WM outside the lock that spans its write")


def test_consume_staged_holds_the_lock_across_its_read_and_write():
    """The write happens with the lock HELD (the invariant, independent of race)."""
    merge = _load("body_merge_consume_lock", "body-merge.py")

    with _daemon() as (df, wm_path):
        state_dir = wm_path.parent
        staged = state_dir / merge._STAGED_DIRNAME
        staged.mkdir(parents=True, exist_ok=True)
        _seed(staged / "unit-1-wm.yaml", {"slots": {"m": "x"}, "slot_meta": {}})
        merge._get_backend = lambda: None
        merge._read_staged_yaml = lambda backend, path: None

        seen = {}
        real_read, real_write = merge._read_yaml, merge._write_yaml_atomic
        merge._read_yaml = lambda p: (seen.__setitem__("read", _lock_is_held(p)),
                                      real_read(p))[1]
        merge._write_yaml_atomic = lambda p, d: (
            seen.__setitem__("write", _lock_is_held(p)), real_write(p, d))[1]

        summary = {"scanned": 0, "noop": [], "skipped": [], "staged_merged": [],
                   "staged_dedup": [], "staged_deferred": []}
        merge._consume_staged(state_dir, wm_path, summary, set())

        assert seen.get("read") is True, "reducer WM read was taken WITHOUT the lock"
        assert seen.get("write") is True, "reducer WM write was made WITHOUT the lock"


# ───────────────── writer 2: body-merge sessions-pass ─────────────────

def test_sessions_pass_preserves_a_daemon_set_that_lands_mid_merge():
    """END-TO-END: the same guarantee for generalize_down's sessions pass."""
    merge = _load("body_merge_sessions", "body-merge.py")

    with _daemon() as (df, wm_path):
        pr = df.project_root
        sess = pr / "agents" / AGENT / "sessions" / SID_WORKER
        _seed(sess / _WM_FILE, {
            "slots": {"worker_merge_marker": "merged-from-a-sessions-body"},
            "slot_meta": {},
        })
        _seed(sess / "body-manifest.yaml", {
            "unitKey": SID_WORKER, "mindKey": AGENT, "env_id": "local",
            "role": "worker", "body_state": "closed-pending-merge",
            "started_at": "2026-06-24T00:00:00", "forked_wm_hash": _BAD_HASH,
        })

        merge._get_backend = lambda: None
        fired = []

        def _fire_then_read(backend, path):
            if not fired:
                fired.append(_set(df.port, "last_strategic_scan", "2026-09-02T13:31:58"))
            return None

        merge._read_staged_yaml = _fire_then_read

        summary = merge.generalize_down(AGENT, project_root=pr)

        assert summary["merged"] == [SID_WORKER], summary
        assert fired and fired[0][0] == 200, fired
        assert _get(df.port, "worker_merge_marker") == "merged-from-a-sessions-body"
        assert _get(df.port, "last_strategic_scan") == "2026-09-02T13:31:58", (
            "the sessions pass reverted a verified daemon set — its reducer read "
            "is outside the lock that spans its write")


# ───────────────── writer 3: compact-restore-slots ─────────────────

@pytest.fixture
def crs_env(tmp_path, monkeypatch):
    """compact-restore-slots bound to a tmp WM via BODY_WM_PATH.

    BODY_WM_PATH is resolved PER CALL by wm.wm_path(), so it redirects both
    read_wm/write_wm AND wm_lock() together — the property under test. The
    CHECKPOINT_PATH module redirect is the guard-1415-sanctioned form used by
    the sibling test_compact_restore_* files.
    """
    wm_file = tmp_path / _WM_FILE
    _seed(wm_file, {
        "session_start": "2026-09-02T00:00:00",
        "slots": {"loop_state": None, "keeper": "v"}, "slot_meta": {},
    })
    monkeypatch.setenv("BODY_WM_PATH", str(wm_file))
    monkeypatch.setenv("MIND_AGENT", AGENT)
    crs = _load("compact_restore_lock", "compact-restore-slots.py")
    crs.CHECKPOINT_PATH = tmp_path / "compact-checkpoint.yaml"
    return crs, wm_file


def test_loop_state_recovery_holds_the_lock_across_read_and_write(crs_env):
    """_recover_lost_loop_state: null-guard DECISION and write under one lock.

    guard-746 -- loop_state must never be persisted from a snapshot taken
    before the mutation. The null guard here reads on-disk loop_state and acts
    on that reading, so an unlocked straddle can reinstate a checkpoint
    snapshot over a value a daemon set had already replaced.
    """
    crs, wm_file = crs_env
    crs.CHECKPOINT_PATH.write_text(yaml.safe_dump({
        "all_slots": {"loop_state": {"goals_completed": 7, "productive_goals": 5}},
    }), encoding="utf-8")

    seen = {}
    real_read, real_write = crs.read_wm, crs.write_wm
    crs.read_wm = lambda: (seen.__setitem__("read", _lock_is_held(wm_file)), real_read())[1]
    crs.write_wm = lambda d: (seen.__setitem__("write", _lock_is_held(wm_file)), real_write(d))[1]

    crs._recover_lost_loop_state()

    assert seen.get("read") is True, "read_wm() ran WITHOUT the WM lock"
    assert seen.get("write") is True, "write_wm() ran WITHOUT the WM lock"
    on_disk = yaml.safe_load(wm_file.read_text(encoding="utf-8"))
    assert on_disk["slots"]["loop_state"]["goals_completed"] == 7, on_disk


def test_main_restore_holds_the_lock_across_read_and_write(crs_env):
    """main()'s restore: read_wm() ... write_wm() is one lock hold.

    This region re-persists loop_state verbatim as COLLATERAL (it is in
    SKIP_SLOTS, never merged), which is the guard-746 shape: an unlocked
    straddle reverts any daemon counter bump landing mid-restore.
    """
    crs, wm_file = crs_env
    crs.CHECKPOINT_PATH.write_text(yaml.safe_dump({
        "saved_at": crs.now_iso(),
        "all_slots": {"restored_slot": "from-checkpoint"},
        "slot_meta": {},
    }), encoding="utf-8")

    seen = {}
    real_read, real_write = crs.read_wm, crs.write_wm
    crs.read_wm = lambda: (seen.__setitem__("read", _lock_is_held(wm_file)), real_read())[1]
    crs.write_wm = lambda d: (seen.__setitem__("write", _lock_is_held(wm_file)), real_write(d))[1]

    with contextlib.suppress(SystemExit):
        crs.main()

    assert seen.get("read") is True, "main()'s read_wm() ran WITHOUT the WM lock"
    assert seen.get("write") is True, "main()'s write_wm() ran WITHOUT the WM lock"


# ───────────────── writer 4: wm-contamination-check quarantine ─────────────────

def test_quarantine_holds_the_lock_across_the_swap(tmp_path):
    """Both os.replace calls happen under one lock hold.

    Between them wm_path DOES NOT EXIST. A daemon set landing in that window
    re-creates the file and the second replace clobbers it -- and unlike the
    wholesale set-aside, that loss is NOT preserved in the quarantine copy,
    because the write never reached the quarantined file.
    """
    wcc = _load("wm_contam_lock", "wm-contamination-check.py")
    wm_file = tmp_path / "session" / _WM_FILE
    _seed(wm_file, {"slots": {"foreign": "contaminated"}})

    held = []
    real_replace = os.replace
    wcc.os.replace = lambda a, b: (held.append(_lock_is_held(wm_file)),
                                   real_replace(a, b))[1]
    try:
        ok, info = wcc.quarantine(wm_file, tmp_path)
    finally:
        wcc.os.replace = real_replace

    assert ok, info
    assert len(held) == 2, f"expected both replaces instrumented, saw {len(held)}"
    assert all(held), f"a replace ran WITHOUT the WM lock held: {held}"
    assert wm_file.exists(), "fresh WM not written back"
    assert Path(info).exists(), "quarantine copy missing"
