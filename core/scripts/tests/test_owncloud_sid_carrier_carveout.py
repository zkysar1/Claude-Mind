"""test_owncloud_sid_carrier_carveout.py —  regression.

Pins the SID-keyed body-heartbeat carve-out in owncloud_sync's H4a ownership
gate.

THE DEFECT (diagnosed in g-306-234, corrected 2026-08-06 by zeta): the gate
lets a box publish `agents/<agent>/` only while it holds that agent's live DDB
runner claim. A worker Body runs, by definition, on a box that does NOT hold
the claim — so it can never publish `body-heartbeat-<SID>.json`, the one file
whose entire purpose is to let it vouch for itself cross-box. The carrier was
structurally unable to serve its stated purpose in exactly and only the case it
was built for. Measured harm: stranded-claim-sweep released live worker claims
(g-115-105, g-335-745) past the 120m foreign-SID grace.

WHAT THESE TESTS DEFEND, and why the second half matters more than the first.
The fix LOOSENS a fail-closed gate (guard-1562, dangerous direction), so the
tests that matter are not the ones proving the carrier now flows — they are the
ones proving nothing ELSE does. Under own-cloud the local tree is a read-through
cache, so a PEER's carrier can legitimately sit on this disk as pulled bytes; a
carve-out written as `body-heartbeat-*.json` would have admitted it and pushed a
stale cache over the peer's newer S3 write. `test_peer_carrier_still_skipped`
and `test_same_sid_other_agent_still_skipped` are that regression guard.

Both publication paths are covered or the fix is half-applied: the periodic
`sweep()` (post-walk targeted push) and `sync_file()` (PostToolUse single-file).

Hermetic: FakeBackend models S3 as a dict, `_owned_agents` is monkeypatched, and
all roots are tmp_path. No S3, no DDB, no daemon.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import owncloud_sync as ocs  # noqa: E402

MY_SID = "8433a74a-fb28-400a-a67e-5acbebacd4ce"
PEER_SID = "03fda40a-1111-2222-3333-444455556666"


def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


class FakeBackend:
    """Minimal S3 model — same shape as test_owncloud_sync.FakeBackend."""

    def __init__(self, roots):
        self._roots = roots
        self.s3 = {}
        self.puts = []

    def stat(self, path):
        b = self.s3.get(str(path))
        if b is None:
            return None

        class _S:
            version = None
            size = 0
            mtime_ns = 0

        s = _S()
        s.version = '"' + _md5(b) + '"'
        s.size = len(b)
        return s

    def mirror_put(self, path, content, *, expected_version=None):
        self.s3[str(path)] = content
        self.puts.append(str(path))

    def refresh(self, path):
        return None

    def list_dir(self, path):
        return []

    def delete_object(self, path):
        self.s3.pop(str(path), None)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """agents root + a backend, with this process presenting as alpha/MY_SID."""
    agents = tmp_path / "agents"
    (agents / "alpha" / "session").mkdir(parents=True)
    (agents / "bravo" / "session").mkdir(parents=True)
    be = FakeBackend([(str(agents), "agents")])
    monkeypatch.setenv("MIND_AGENT", "alpha")
    monkeypatch.setenv("MIND_SID", MY_SID)
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    return {"agents": agents, "be": be, "tmp": tmp_path}


def _carrier(agents: Path, agent: str, sid: str, body: bytes = b'{"ok":1}'):
    p = agents / agent / "session" / f"body-heartbeat-{sid}.json"
    p.write_bytes(body)
    return p


# ── _own_sid_carrier_path: the computed-not-matched predicate ────────────────

def test_returns_none_when_sid_unset(env, monkeypatch):
    _carrier(env["agents"], "alpha", MY_SID)
    monkeypatch.delenv("MIND_SID", raising=False)
    assert ocs._own_sid_carrier_path(env["be"]) is None


def test_returns_none_when_agent_unset(env, monkeypatch):
    _carrier(env["agents"], "alpha", MY_SID)
    monkeypatch.delenv("MIND_AGENT", raising=False)
    assert ocs._own_sid_carrier_path(env["be"]) is None


def test_returns_none_when_carrier_absent(env):
    # Env is set but nothing on disk — must not fabricate a path.
    assert ocs._own_sid_carrier_path(env["be"]) is None


def test_returns_triple_when_present(env):
    p = _carrier(env["agents"], "alpha", MY_SID)
    got = ocs._own_sid_carrier_path(env["be"])
    assert got is not None
    path, prefix, root = got
    assert path == p
    assert prefix == "agents"
    assert root == env["agents"]


@pytest.mark.parametrize("bad", ["../../etc", "a/b", "a\\b", ".", ".."])
def test_traversal_sids_rejected(env, monkeypatch, bad):
    """A sid is interpolated into a filename — refuse anything path-shaped."""
    monkeypatch.setenv("MIND_SID", bad)
    assert ocs._own_sid_carrier_path(env["be"]) is None


# ── sync_file: the PostToolUse single-file path ──────────────────────────────

def test_own_carrier_pushed_when_agent_not_owned(env, monkeypatch):
    """THE FIX. Worker case: this box does not own alpha, yet its own carrier
    must still publish."""
    p = _carrier(env["agents"], "alpha", MY_SID)
    monkeypatch.setattr(ocs, "_owned_agents", lambda be=None: {"bravo"})
    stats = {}
    ocs.sync_file(env["be"], p, dry_run=False, stats_out=stats)
    assert stats.get("reason") != "peer_agent"
    assert str(p.resolve()) in env["be"].puts


def test_peer_carrier_still_skipped(env, monkeypatch):
    """THE REGRESSION GUARD. A peer's carrier sitting here as a pulled
    read-through cache carries a FOREIGN sid — pushing it would clobber the
    peer's newer S3 write. This is the hole a `body-heartbeat-*.json` glob
    would have re-opened."""
    p = _carrier(env["agents"], "bravo", PEER_SID)
    monkeypatch.setattr(ocs, "_owned_agents", lambda be=None: {"alpha"})
    stats = {}
    ocs.sync_file(env["be"], p, dry_run=False, stats_out=stats)
    assert stats.get("reason") == "peer_agent"
    assert env["be"].puts == []


def test_same_sid_other_agent_still_skipped(env, monkeypatch):
    """Belt-and-braces: even OUR sid under a peer's agent dir is not ours to
    push. The exemption is exact-path, not sid-substring."""
    p = _carrier(env["agents"], "bravo", MY_SID)
    monkeypatch.setattr(ocs, "_owned_agents", lambda be=None: {"alpha"})
    stats = {}
    ocs.sync_file(env["be"], p, dry_run=False, stats_out=stats)
    assert stats.get("reason") == "peer_agent"
    assert env["be"].puts == []


def test_non_carrier_file_in_unowned_dir_still_skipped(env, monkeypatch):
    """The exemption must not widen to the agent's other session state —
    handoff.yaml IS the stale-cache class the gate exists to protect."""
    p = env["agents"] / "alpha" / "session" / "handoff.yaml"
    p.write_text("x: 1\n", encoding="utf-8")
    monkeypatch.setattr(ocs, "_owned_agents", lambda be=None: {"bravo"})
    stats = {}
    ocs.sync_file(env["be"], p, dry_run=False, stats_out=stats)
    assert stats.get("reason") == "peer_agent"
    assert env["be"].puts == []


# ── sweep: the periodic path ─────────────────────────────────────────────────

def _sweep(be, monkeypatch, owned, tmp_path, *,
           only_root="agents", only_agent=None):
    """Defaults reproduce the pre- call shape EXACTLY, so the four
    tests below that omit the keywords are byte-for-byte unaffected."""
    monkeypatch.setattr(ocs, "_owned_agents", lambda be=None: owned)
    monkeypatch.setattr(ocs, "_load_manifest", lambda: {})
    monkeypatch.setattr(ocs, "_save_manifest", lambda m: None)
    monkeypatch.setattr(ocs, "_update_conflict_streaks", lambda s: None)
    monkeypatch.setattr(ocs, "propagate_temp_moves",
                        lambda *a, **k: {"agents_checked": 0})
    return ocs.sweep(be, only_root=only_root, dry_run=False,
                     use_manifest=False, full=False, only_agent=only_agent)


def test_sweep_pushes_own_carrier_when_agent_pruned(env, monkeypatch):
    """THE OTHER HALF. The walk prunes the unowned agent dir at the DIRNAME
    level, so it never reaches the file — the targeted post-walk push is what
    covers the periodic path."""
    p = _carrier(env["agents"], "alpha", MY_SID)
    stats = _sweep(env["be"], monkeypatch, {"bravo"}, env["tmp"])
    assert stats.get("own_carrier_pushed") == 1
    assert str(p.resolve()) in env["be"].puts


def test_sweep_no_double_push_when_agent_owned(env, monkeypatch):
    """When the agent IS owned the walk already pushed it; the targeted push
    must not fire a second _sync_one on the same key."""
    _carrier(env["agents"], "alpha", MY_SID)
    stats = _sweep(env["be"], monkeypatch, {"alpha"}, env["tmp"])
    assert stats.get("own_carrier_pushed", 0) == 0


def test_sweep_does_not_push_peer_carrier(env, monkeypatch):
    """A peer's carrier in a pruned dir stays unpublished."""
    p = _carrier(env["agents"], "bravo", PEER_SID)
    stats = _sweep(env["be"], monkeypatch, {"alpha"}, env["tmp"])
    assert stats.get("own_carrier_pushed", 0) == 0
    assert str(p.resolve()) not in env["be"].puts


def test_sweep_local_backend_unaffected(env, monkeypatch):
    """owned is None on a local backend (own-all) — the targeted push is skipped
    entirely because the walk already covers every agent dir."""
    _carrier(env["agents"], "alpha", MY_SID)
    stats = _sweep(env["be"], monkeypatch, None, env["tmp"])
    assert stats.get("own_carrier_pushed", 0) == 0


# ── sweep: SCOPING () ───────────────────────────────────────────────
#
# The four tests above all call sweep(only_root="agents", only_agent=None). The
# pre- predicate (`if owned is not None`) and the scoped one agree on
# every one of those rows, so that suite had ZERO power over this change no
# matter how many cases it held (guard-2353 — a green suite announces nothing).
# The two FAIL-BEFORE tests here are the discriminating rows; the two controls
# after them are the guard-1080 assertion that the one legitimate writer — the
# unscoped periodic sweep a worker Body depends on — still succeeds.
#
# Both shapes are reachable from main(): --root is choices=(world|meta|agents)
# and --agent NAME sets only_agent non-None.

def test_sweep_world_root_does_not_push_agents_carrier(env, monkeypatch):
    """FAIL-BEFORE. A world-scoped sweep must issue no agents-root push at all.
    The post-walk block sits at function level, so before the fix it fired even
    when the walk itself was scoped away from the agents root entirely."""
    p = _carrier(env["agents"], "alpha", MY_SID)
    stats = _sweep(env["be"], monkeypatch, {"bravo"}, env["tmp"],
                   only_root="world")
    assert stats.get("own_carrier_pushed", 0) == 0
    assert str(p.resolve()) not in env["be"].puts
    # Stronger than the line above, and deliberately kept: the path-absence
    # check passes if the sweep pushed some OTHER agents-root key, whereas a
    # world-scoped sweep must issue no agents PUT at all. Carried on the worker
    # ref (2505c0707) and dropped by the independent re-implementation that
    # reached main first (db41bbd02) — re-added when the two were reconciled.
    # Measured 2026-08-08: it is defense-in-depth, NOT independently
    # discriminating. Removing the only_root guard is caught by the
    # own_carrier_pushed assertion above, which short-circuits before this line
    # runs, so no mutation proof attributes a catch to it. Do not read that as
    # dead code and delete it — it pins a DIFFERENT property (no agents PUT at
    # all) that no other assertion here covers.
    assert env["be"].puts == [], "a world-scoped sweep must issue no agents PUT"


def test_sweep_other_agent_scope_does_not_push_own_carrier(env, monkeypatch):
    """FAIL-BEFORE. `--agent bravo` scopes the sweep to bravo; this box's own
    alpha carrier must stay unpublished."""
    p = _carrier(env["agents"], "alpha", MY_SID)
    stats = _sweep(env["be"], monkeypatch, {"bravo"}, env["tmp"],
                   only_agent="bravo")
    assert stats.get("own_carrier_pushed", 0) == 0
    assert str(p.resolve()) not in env["be"].puts


def test_sweep_unscoped_still_pushes_own_carrier(env, monkeypatch):
    """CONTROL — green before AND after. This is the path  exists to
    serve: the unscoped periodic sweep publishing a worker Body's heartbeat.
    Losing it would re-hide the Body from stranded-claim-sweep, which is the
    exact harm g-306-235 was filed to fix — so this asserts the change SCOPES
    the push rather than disabling it."""
    p = _carrier(env["agents"], "alpha", MY_SID)
    stats = _sweep(env["be"], monkeypatch, {"bravo"}, env["tmp"],
                   only_root=None)
    assert stats.get("own_carrier_pushed") == 1
    assert str(p.resolve()) in env["be"].puts


def test_sweep_own_agent_scope_still_pushes_own_carrier(env, monkeypatch):
    """CONTROL — green before AND after. `--agent alpha` names THIS agent, so
    the carrier is in scope and must still publish."""
    p = _carrier(env["agents"], "alpha", MY_SID)
    stats = _sweep(env["be"], monkeypatch, {"bravo"}, env["tmp"],
                   only_agent="alpha")
    assert stats.get("own_carrier_pushed") == 1
    assert str(p.resolve()) in env["be"].puts
