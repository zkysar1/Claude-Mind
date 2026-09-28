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


# ── _put: the THIRD ownership gate () ───────────────────────────────
#
# The two sections above pin the carve-out in owncloud_sync's two publication
# paths. Both were correct and both were INERT, because a later change
# () added an INDEPENDENT ownership refusal at the write itself —
# OwnCloudBackend._put — and did not carry the exemption down. Measured on
# cc-09 2026-09-04: sync_file admitted the carrier (`would_push: 1`, the exempt
# path resolving to exactly this file) and the PUT one call below refused it
# `no_claim`, so the carrier never reached S3 from ANY worker box. Every
# peer-side reader of it — stranded-claim-sweep's foreign-SID grace,
# worker_stall's S3 prefix listing, reducer_promotion — saw `absent` for a
# demonstrably live Body, and stranded-claim-sweep released a live worker's
# claim at 126.5 minutes (msg-20260904-045322-alpha-6092).
#
# THE LESSON THESE TESTS EXIST TO PIN is not "the carrier publishes" — the
# section above already asserted that and stayed green through the whole
# defect. It is that a gate's exemption must be pinned at EVERY layer that can
# refuse (guard-2783), because passing one gate proves nothing about the next.

class _ReachedPut(Exception):
    """Sentinel: _put got PAST the ownership consult to the real write."""


class _FakeSelf:
    """Minimal stand-in for an OwnCloudBackend instance. `_put` is called
    unbound so no S3/DDB config is needed; `_s3_key` is the first call after
    the consult, so raising there is exactly 'the consult admitted this'."""

    def __init__(self, roots):
        self._roots = roots

    def _machine_local(self, path):
        return False

    def _assert_not_tempdir_put(self, path):
        # The guard-955 tripwire fires on tmp_path by design and sits BEFORE
        # the consult (ordering pinned by test_no_claim_error). Neutralised
        # here so this test can reach the layer it is about.
        return None

    def _s3_key(self, path):
        raise _ReachedPut(str(path))


def _put_env(env, monkeypatch, owned):
    """Present this box as holding a live claim on `owned` only."""
    import _paths
    import owncloud_sync as _ocs
    monkeypatch.setattr(_paths, "agents_root", lambda: env["agents"])
    monkeypatch.setattr(_ocs, "_owned_agents_with_provenance",
                        lambda *a, **k: (set(owned), "live-claims"))
    return _FakeSelf([(str(env["agents"]), "agents")])


def _call_put(slf, path):
    import owncloud_backend
    return owncloud_backend.OwnCloudBackend._put(slf, path, b'{"ok":1}')


def test_put_admits_own_sid_carrier_when_agent_not_owned(env, monkeypatch):
    """THE regression. Before  this raised NoClaimError, which is why
    a worker Body's carrier never reached S3 and `fresh-correct` was
    unreachable on every box."""
    import owncloud_backend
    p = _carrier(env["agents"], "alpha", MY_SID)
    slf = _put_env(env, monkeypatch, {"bravo"})
    with pytest.raises(_ReachedPut):
        _call_put(slf, p)
    # and specifically NOT the refusal
    try:
        _call_put(slf, p)
    except owncloud_backend.NoClaimError:  # pragma: no cover - fails the test
        pytest.fail("_put refused this session's own SID-keyed carrier")
    except _ReachedPut:
        pass


def test_put_still_refuses_peer_carrier_in_unowned_dir(env, monkeypatch):
    """LOAD-BEARING (guard-2860: pin the negatives). Under own-cloud the local
    tree is a read-through cache, so a PEER's carrier legitimately sits on this
    disk as pulled bytes. A `body-heartbeat-*.json` predicate would admit it
    and push stale bytes over the peer's newer write."""
    import owncloud_backend
    _carrier(env["agents"], "alpha", MY_SID)          # ours exists
    peer = _carrier(env["agents"], "alpha", PEER_SID)  # theirs, pulled here
    slf = _put_env(env, monkeypatch, {"bravo"})
    with pytest.raises(owncloud_backend.NoClaimError):
        _call_put(slf, peer)


def test_put_still_refuses_same_sid_under_other_agent(env, monkeypatch):
    """LOAD-BEARING. The sid alone must not authorise a write — the agent dir
    is half of the identity."""
    import owncloud_backend
    _carrier(env["agents"], "alpha", MY_SID)
    other = _carrier(env["agents"], "bravo", MY_SID)
    slf = _put_env(env, monkeypatch, set())
    with pytest.raises(owncloud_backend.NoClaimError):
        _call_put(slf, other)


def test_put_still_refuses_non_carrier_in_unowned_dir(env, monkeypatch):
    """LOAD-BEARING. The exemption is ONE computed path, not a directory."""
    import owncloud_backend
    _carrier(env["agents"], "alpha", MY_SID)
    other = env["agents"] / "alpha" / "session" / "handoff.yaml"
    other.write_bytes(b"x: 1\n")
    slf = _put_env(env, monkeypatch, {"bravo"})
    with pytest.raises(owncloud_backend.NoClaimError):
        _call_put(slf, other)


def test_put_unaffected_when_agent_is_owned(env, monkeypatch):
    """CONTROL — green before AND after. An owned dir never consulted the
    exemption; the change must not alter that path."""
    p = _carrier(env["agents"], "alpha", MY_SID)
    slf = _put_env(env, monkeypatch, {"alpha"})
    with pytest.raises(_ReachedPut):
        _call_put(slf, p)


# ── The REQUEST's identity, not the daemon's env () ──────────────────
#
# Every test above sets MIND_SID to the carrier's own sid, which is the shape
# of a CLI sweep run inside the session. The long-lived daemon is the other
# shape: its env is the one it was SPAWNED with, so it names an earlier session
# or none. Measured 2026-09-27 on 10 worker boxes: the 3 whose Body session
# started after their daemon never published their live carrier, the
# stranded-claim sweep read it `absent` and released 3 live claims, and a
# second Body duplicated each goal. Per guard-7059, every test here uses a
# subject the identity is load-bearing for: an env that does NOT name the
# carrier's session.

OLD_SID = "5e55e55e-0000-1111-2222-333344445555"  # an EARLIER session's sid


def _named(agent, sid):
    """Context manager: the request names its caller for the block."""
    import contextlib

    @contextlib.contextmanager
    def _cm():
        token = ocs.set_carrier_identity(agent, sid)
        try:
            yield
        finally:
            ocs.reset_carrier_identity(token)
    return _cm()


def test_identity_publishes_the_carrier_a_sessionless_daemon_refuses(env, monkeypatch):
    """THE FIX. The first half is the defect exactly as measured on a worker
    box's daemon (reason=peer_agent for its own live carrier)."""
    p = _carrier(env["agents"], "alpha", MY_SID)
    monkeypatch.delenv("MIND_SID", raising=False)
    monkeypatch.setattr(ocs, "_owned_agents", lambda be=None: {"bravo"})
    before = {}
    ocs.sync_file(env["be"], p, dry_run=False, stats_out=before)
    assert before.get("reason") == "peer_agent"
    assert env["be"].puts == []
    with _named("alpha", MY_SID):
        after = {}
        ocs.sync_file(env["be"], p, dry_run=False, stats_out=after)
    assert after.get("reason") != "peer_agent"
    assert env["be"].puts == [str(p.resolve())]


def test_identity_admits_one_carrier_not_also_the_envs(env, monkeypatch):
    """Cardinality ONE (guard-2860). A daemon spawned by an EARLIER session
    carries that session's sid; naming the caller must replace it, not add to
    it."""
    new = _carrier(env["agents"], "alpha", MY_SID)
    old = _carrier(env["agents"], "alpha", OLD_SID)
    monkeypatch.setenv("MIND_SID", OLD_SID)
    monkeypatch.setattr(ocs, "_owned_agents", lambda be=None: {"bravo"})
    s_new, s_old = {}, {}
    with _named("alpha", MY_SID):
        ocs.sync_file(env["be"], new, dry_run=False, stats_out=s_new)
        ocs.sync_file(env["be"], old, dry_run=False, stats_out=s_old)
    assert s_old.get("reason") == "peer_agent"
    assert env["be"].puts == [str(new.resolve())]


def test_identity_still_refuses_a_pulled_peer_carrier(env, monkeypatch):
    """LOAD-BEARING negative, with its positive control under the SAME
    identity: a peer's carrier sitting here as a read-through cache must never
    be pushed over the peer's newer write."""
    mine = _carrier(env["agents"], "alpha", MY_SID)
    peer = _carrier(env["agents"], "alpha", PEER_SID)
    monkeypatch.delenv("MIND_SID", raising=False)
    monkeypatch.setattr(ocs, "_owned_agents", lambda be=None: {"bravo"})
    s_peer, s_mine = {}, {}
    with _named("alpha", MY_SID):
        ocs.sync_file(env["be"], peer, dry_run=False, stats_out=s_peer)
        ocs.sync_file(env["be"], mine, dry_run=False, stats_out=s_mine)
    assert s_peer.get("reason") == "peer_agent"
    assert env["be"].puts == [str(mine.resolve())]


def test_identity_reaches_the_put_layer(env, monkeypatch):
    """guard-2783: an exemption holds only if EVERY refusing layer honours it.
    _put's ownership consult is the third, and it reads the same SSOT helper."""
    import owncloud_backend
    mine = _carrier(env["agents"], "alpha", MY_SID)
    peer = _carrier(env["agents"], "alpha", PEER_SID)
    monkeypatch.delenv("MIND_SID", raising=False)
    slf = _put_env(env, monkeypatch, {"bravo"})
    with pytest.raises(owncloud_backend.NoClaimError):
        _call_put(slf, mine)  # the env alone cannot name it
    with _named("alpha", MY_SID):
        with pytest.raises(_ReachedPut):
            _call_put(slf, mine)
        with pytest.raises(owncloud_backend.NoClaimError):
            _call_put(slf, peer)


def test_reset_restores_the_env_behaviour(env, monkeypatch):
    _carrier(env["agents"], "alpha", MY_SID)
    monkeypatch.delenv("MIND_SID", raising=False)
    with _named("alpha", MY_SID):
        assert ocs._own_sid_carrier_path(env["be"]) is not None
    assert ocs._own_sid_carrier_path(env["be"]) is None


def test_identity_never_reaches_a_thread_already_running(env, monkeypatch):
    """The periodic sweep runs on a thread started at daemon boot. A request's
    identity must stay on the request's own thread."""
    import threading
    _carrier(env["agents"], "alpha", MY_SID)
    monkeypatch.delenv("MIND_SID", raising=False)
    go, seen = threading.Event(), []

    def _sweep_thread():
        go.wait(10)
        seen.append(ocs._own_sid_carrier_path(env["be"]))

    t = threading.Thread(target=_sweep_thread)
    t.start()
    with _named("alpha", MY_SID):
        go.set()
        t.join(10)
    assert seen == [None]


@pytest.mark.parametrize("bad", ["../../etc", "a/b", "a\\b", ".", ".."])
def test_identity_traversal_sids_rejected(env, bad):
    """The identity replaces the env, so it gets the env's defences. The file
    the bad sid would address is CREATED first, so only the defence, not a
    missing file, can make this return None."""
    target = env["agents"] / "alpha" / "session" / f"body-heartbeat-{bad}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b'{"ok":1}')
    with _named("alpha", bad):
        assert ocs._own_sid_carrier_path(env["be"]) is None


# ── The endpoint names its caller () ─────────────────────────────────

class _Ctx:
    class _P:
        project_root = Path(__file__).resolve().parents[3]

    def __init__(self, query, headers):
        self.query = query
        self.headers = headers
        self.paths = self._P()


def _endpoint(monkeypatch, seen, *, raise_exc=None):
    sys.path.insert(0, str(CORE_SCRIPTS.parent.parent))
    from mind_api.src.endpoints import admin
    import storage_backend

    def _fake_sync_file(be, target, *, dry_run, stats_out=None):
        seen.append(ocs._carrier_identity.get())
        if raise_exc is not None:
            raise raise_exc
        return 0

    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    monkeypatch.setattr(ocs, "sync_file", _fake_sync_file)
    monkeypatch.setattr(storage_backend, "get_backend", lambda: object())
    return admin


def test_endpoint_names_the_caller_for_the_push_only(monkeypatch):
    seen = []
    admin = _endpoint(monkeypatch, seen)
    admin.owncloud_sync_file(_Ctx(
        {"path": "/x/agents/alpha/session/c.json"},
        {"x-mind-agent": "alpha", "x-mind-sid": MY_SID}))
    assert seen == [("alpha", MY_SID)]
    # Reset: never carried into the next request served on this thread.
    assert ocs._carrier_identity.get() is None


def test_endpoint_without_a_sid_keeps_the_env_behaviour(monkeypatch):
    """The PostToolUse shim's bare curl sends no identity headers."""
    seen = []
    admin = _endpoint(monkeypatch, seen)
    admin.owncloud_sync_file(_Ctx({"path": "/x/world/n.md"}, {}))
    assert seen == [None]


def test_endpoint_resets_the_identity_when_the_push_raises(monkeypatch):
    seen = []
    admin = _endpoint(monkeypatch, seen, raise_exc=RuntimeError("boom"))
    admin.owncloud_sync_file(_Ctx(
        {"path": "/x/agents/alpha/session/c.json"},
        {"x-mind-agent": "alpha", "x-mind-sid": MY_SID}))
    assert seen == [("alpha", MY_SID)]
    assert ocs._carrier_identity.get() is None
