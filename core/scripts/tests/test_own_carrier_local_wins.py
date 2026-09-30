"""test_own_carrier_local_wins.py —  regression.

A worker Body publishes its own body-heartbeat carrier through sync_file
(heartbeat-tick.sh --body-only, then POST /v1/admin/owncloud-sync-file, which
names the session with set_carrier_identity). After one lost baseline stamp,
local and store both differ from the manifest baseline. Before this fix the
both-diverged branch could not clear that for a carrier: sync_file passes no
own-cloud authority, so LOCAL-WINS never classified the file, and the periodic
sweep reaches only the carrier of the session its daemon was spawned in. Every
later publish was a CONFLICT skip. The store copy froze while the Body kept
ticking, and the stranded-claim sweep read the frozen copy and released a live
claim (measured 2026-09-30 on a worker box).

The fix admits LOCAL-WINS for THIS session's own carrier only (the exact path
computed from the session identity, g-306-235), guarded by the carrier's `ts`:
push only a local copy strictly newer than the store copy.

  A  sync_file heals a wedged own carrier, named the way the endpoint names it
     (request identity, while the process env names another session).
  B  the sweep's own-carrier pass heals it too and saves local's md5 as the
     new baseline, and refuses a same-ts store-side repair it used to push over.
  C  a store copy with the SAME ts and different bytes (a store-side repair,
     which keeps ts by design) is refused.
  D  a store copy with a NEWER ts is refused.
  E  an unreadable local carrier is refused (fails closed); an unreadable store
     copy is admitted (fails open); a ts with an offset compares with one
     without.
  F  positive controls (guard-4166): a PEER sid's carrier in the same agent dir
     and a non-carrier session file, both reached both-diverged in the
     sync_file call shape, still take the CONFLICT skip.

Hermetic: the fake store from test_owncloud_sync (fenced mirror_put, and the
read_authoritative_bytes the store-side guard needs, guard-5501), RUNTIME_DIR in
tmp_path, ownership and the history snapshot monkeypatched.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))
sys.path.insert(0, str(SCRIPT_DIR))

import owncloud_sync as ocs  # noqa: E402
from test_owncloud_sync import FakeBackend, _new_stats  # noqa: E402

MY_SID = "8433a74a-fb28-400a-a67e-5acbebacd4ce"
PEER_SID = "03fda40a-1111-2222-3333-444455556666"
DAEMON_SID = "5c3ed4a2-aaaa-bbbb-cccc-ddddeeeeffff"

BASE_TS = "2026-09-29T22:59:17"     # the version the lost stamp left as baseline
STORE_TS = "2026-09-29T23:17:38"    # the push whose stamp was lost
LOCAL_TS = "2026-09-30T01:27:26"    # the Body kept ticking locally


def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def _doc(ts, *, sid=MY_SID, state="working") -> bytes:
    return (json.dumps({"sid": sid, "agent": "alpha", "host": "box-a",
                        "ts": ts, "body_state": state, "machine_id": ""})
            + "\n").encode()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A worker box: agents root, alpha NOT owned, own-cloud, a tmp RUNTIME_DIR
    for the manifest, and the history snapshot recorded instead of written."""
    agents = tmp_path / "agents"
    (agents / "alpha" / "session").mkdir(parents=True)
    (agents / "bravo" / "session").mkdir(parents=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("MIND_AGENT", "alpha")
    monkeypatch.setenv("MIND_SID", MY_SID)
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    monkeypatch.setenv("RUNTIME_DIR", str(runtime))
    monkeypatch.delenv("MACHINE_MULTI", raising=False)
    monkeypatch.setattr(ocs, "_owned_agents", lambda be=None: {"bravo"})
    snapshots = []
    monkeypatch.setattr(ocs, "_snapshot_before_pull",
                        lambda full, summary=None: snapshots.append(str(full)))
    return {"agents": agents, "be": FakeBackend([(agents, "agents")]),
            "runtime": runtime, "snapshots": snapshots}


def _wedge(env, *, sid=MY_SID, local=None, store=None, name=None):
    """Write the three-way split a lost stamp leaves: local, store and the
    manifest baseline all different. Returns the local path."""
    p = env["agents"] / "alpha" / "session" / (
        name or f"body-heartbeat-{sid}.json")
    p.write_bytes(_doc(LOCAL_TS, sid=sid) if local is None else local)
    env["be"].s3[str(p)] = _doc(STORE_TS, sid=sid) if store is None else store
    rel = f"agents/{p.relative_to(env['agents']).as_posix()}"
    manifest = ocs._load_manifest()
    manifest[rel] = {"mtime": 0, "md5": _md5(_doc(BASE_TS, sid=sid))}
    (env["runtime"] / "owncloud-sync-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    return p


def _publish(env, p):
    """sync_file named as the endpoint names it: request identity set, process
    env naming the session that spawned the daemon (g-375-40)."""
    stats = {}
    token = ocs.set_carrier_identity("alpha", MY_SID)
    try:
        ocs.sync_file(env["be"], p, dry_run=False, stats_out=stats)
    finally:
        ocs.reset_carrier_identity(token)
    return stats


# ── A: the fix, on the path the heartbeat tick uses ──────────────────────────

def test_sync_file_heals_a_wedged_own_carrier(env, monkeypatch):
    monkeypatch.setenv("MIND_SID", DAEMON_SID)
    p = _wedge(env)
    local = p.read_bytes()

    stats = _publish(env, p)

    assert stats.get("local_wins_resolved") == 1
    assert env["be"].s3[str(p)] == local           # the store has the live beat
    assert p.read_bytes() == local                 # local untouched
    assert env["be"].puts == [str(p)]              # one fenced PUT
    assert stats.get("diverged_skipped") in (None, 0)
    assert "conflict_paths" not in stats
    assert env["snapshots"] == [str(p)]            # the pre-local-wins receipt


# ── B: the periodic sweep's own-carrier pass ─────────────────────────────────

def test_sweep_heals_a_wedged_own_carrier_and_saves_the_new_baseline(
        env, monkeypatch):
    p = _wedge(env)
    local = p.read_bytes()
    monkeypatch.setattr(ocs, "_holder_since_map", lambda be=None: {})
    monkeypatch.setattr(ocs, "_update_conflict_streaks", lambda s: None)
    monkeypatch.setattr(ocs, "propagate_temp_moves",
                        lambda *a, **k: {"agents_checked": 0})

    stats = ocs.sweep(env["be"], only_root="agents", dry_run=False,
                      use_manifest=True, full=False)

    assert stats.get("local_wins_resolved") == 1
    assert stats.get("own_carrier_pushed") == 1
    assert env["be"].s3[str(p)] == local
    saved = json.loads((env["runtime"] / "owncloud-sync-manifest.json")
                       .read_text(encoding="utf-8"))
    rel = f"agents/{p.relative_to(env['agents']).as_posix()}"
    assert saved[rel]["md5"] == _md5(local)        # the next publish is a plain push


def test_sweep_refuses_a_store_side_repair_with_the_same_ts(env, monkeypatch):
    """Before  the sweep reached this carrier through the per-agent
    lane, whose content guard reads capture slots a carrier does not have, so
    it pushed over a repair. The ts guard now applies on this path too."""
    repaired = _doc(LOCAL_TS, state="closed")
    p = _wedge(env, store=repaired)
    monkeypatch.setattr(ocs, "_holder_since_map", lambda be=None: {})
    monkeypatch.setattr(ocs, "_update_conflict_streaks", lambda s: None)
    monkeypatch.setattr(ocs, "propagate_temp_moves",
                        lambda *a, **k: {"agents_checked": 0})

    stats = ocs.sweep(env["be"], only_root="agents", dry_run=False,
                      use_manifest=True, full=False)

    assert env["be"].s3[str(p)] == repaired
    assert env["be"].puts == []
    assert stats.get("local_wins_blocked_stale_local") == 1


# ── C, D: a store copy that is not older is never overwritten ────────────────

def test_a_store_side_repair_with_the_same_ts_is_refused(env):
    repaired = _doc(LOCAL_TS, state="closed")      # body_state changed, ts kept
    p = _wedge(env, store=repaired)

    stats = _publish(env, p)

    assert env["be"].s3[str(p)] == repaired
    assert env["be"].puts == []
    assert stats.get("local_wins_blocked_stale_local") == 1
    assert stats.get("conflict_paths") == [str(p)]


def test_a_newer_store_copy_is_refused(env):
    newer = _doc("2026-09-30T02:00:00")
    p = _wedge(env, store=newer)

    stats = _publish(env, p)

    assert env["be"].s3[str(p)] == newer
    assert env["be"].puts == []
    assert stats.get("local_wins_blocked_stale_local") == 1


# ── E: which side fails open ─────────────────────────────────────────────────

def test_an_unreadable_local_carrier_is_refused(env):
    p = _wedge(env, local=b"{not json\n")

    stats = _publish(env, p)

    assert env["be"].s3[str(p)] == _doc(STORE_TS)
    assert env["be"].puts == []
    assert stats.get("local_wins_blocked_stale_local") == 1


def test_an_unreadable_store_copy_is_overwritten(env):
    p = _wedge(env, store=b"\x00garbage")
    local = p.read_bytes()

    stats = _publish(env, p)

    assert stats.get("local_wins_resolved") == 1
    assert env["be"].s3[str(p)] == local


def test_a_ts_with_an_offset_compares_with_one_without(env):
    p = _wedge(env)
    env["be"].s3[str(p)] = _doc(STORE_TS + "Z")
    assert ocs._own_carrier_stale_probe(env["be"], p, p.read_bytes()) is None
    env["be"].s3[str(p)] = _doc("2026-09-30T01:27:26+00:00")
    assert (ocs._own_carrier_stale_probe(env["be"], p, p.read_bytes())
            == "carrier_not_newer")


# ── F: positive controls, nothing but this session's carrier is admitted ─────

@pytest.mark.parametrize("name,sid", [
    (f"body-heartbeat-{PEER_SID}.json", PEER_SID),   # another session's carrier
    ("working-memory.yaml", MY_SID),                 # not a carrier at all
])
def test_other_files_in_the_same_dir_still_take_the_conflict_skip(env, name, sid):
    own = env["agents"] / "alpha" / "session" / f"body-heartbeat-{MY_SID}.json"
    own.write_bytes(_doc(LOCAL_TS))                  # identity resolves to a real path
    p = _wedge(env, sid=sid, name=name)
    store = env["be"].s3[str(p)]
    baseline = _md5(_doc(BASE_TS, sid=sid))
    stats = _new_stats()

    token = ocs.set_carrier_identity("alpha", MY_SID)
    try:
        out = ocs._sync_one(env["be"], p, dry_run=False, stats=stats,
                            baseline_md5=baseline, multi_machine=False)
    finally:
        ocs.reset_carrier_identity(token)

    assert out is None
    assert stats.get("diverged_skipped") == 1
    assert stats.get("local_wins_resolved") in (None, 0)
    assert env["be"].s3[str(p)] == store
    assert env["be"].puts == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
