""": the class-(b) write sites in mind_api/src/meta re-fence and retry.

THE BUG. Five read-modify-write sites across meta_backpressure.py,
meta_experiment.py, meta_transfer.py and strategy_apply.py did their read
either outside the lock entirely or inside a bare `file_locks.locked()` with no
`refresh()` and no conflict retry. Every store they write is class (b)
FENCE-ONLY (`coordination_merge.merge_handler_for` returns None), so nothing
reconciles below the write and a stale If-Match token is a PERMANENT per-object
per-box wedge, not transient contention (rb-2639,
core/config/conventions/governed-store-write-classes.md).

WHAT THESE TESTS PIN — the three separable invariants of the required pattern:

  1. the cycle calls refresh() — re-taking the If-Match fence per attempt is
     what breaks the deadlock; retrying on a stale token conflicts identically
     forever.
  2. the cycle is wrapped in locked_rmw — so a conflict retries at all.
  3. the READ is INSIDE the cycle — otherwise the retry re-applies a mutation
     computed from a pre-conflict snapshot and the lost-update window stays
     open. Asserted at READ time: a hoisted read still WRITES under the lock, so
     a write-time assertion passes the very revert it exists to catch.

None of it is observable under STORAGE_BACKEND=local, where `conflict_error` is
the empty tuple and locked_rmw degrades to a transparent single pass — which is
exactly why these five survived a green suite (rb-5250, guard-955). A stub
backend supplies the conflict type without S3.

PLUS TWO THINGS THIS SUITE PINS THAT ITS SIBLINGS DO NOT:

  A. RETRY IDEMPOTENCE OF NON-STORE EFFECTS. evolution_check's cycle calls
     _evolution_rollback, which restores a file, appends a world stream, posts
     to the board and EMAILS A HUMAN. locked_rmw re-runs the cycle, so the
     mechanical cure introduces a duplicate-email bug unless the effect is
     cached across attempts. `check` and `evolution_check` likewise accumulate
     response lists that duplicate per retry if hoisted. A conflict-retry cure
     is not complete when the store is correct; it is complete when everything
     the cycle touches is once-only.

  B. THE MIXED-CLASS SCOPE DECISION, which is the part most likely to be
     "fixed" wrongly later. strategy_apply.STRATEGY_FILES and
     meta_transfer.import_bundle each drive BOTH a class-(a) and a class-(b)
     file through the same loop, and meta_generations/pipeline_write are
     class (a) throughout. Those are deliberately left on the bare lock. The
     test_scope_* tests at the bottom pin the DISCRIMINATOR, not just the
     outcome, so a future audit reading only "these look identical" cannot
     complete this goal by converting them.

Stub-backend pattern mirrors test_meta_yaml_conflict_retry.py /
test_experience_conflict_retry.py (the reference suites named by the
convention).
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

_SCRIPTS = Path(__file__).resolve().parents[1]        # core/scripts
_ROOT = _SCRIPTS.parents[1]                           # PROJECT_ROOT
for _p in (str(_SCRIPTS), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from mind_api.src.meta import meta_backpressure as BP   # noqa: E402
from mind_api.src.meta import meta_experiment as EX     # noqa: E402
from mind_api.src.meta import meta_transfer as TR       # noqa: E402
from mind_api.src.meta import strategy_apply as SA      # noqa: E402


def _fileops_mod():
    """The CURRENT _fileops module object (sibling suites reload it)."""
    return importlib.import_module("_fileops")


class _Conflict(Exception):
    """Stand-in for OwnCloudBackend's optimistic-concurrency exception."""


class _StubBackend:
    """Identity ensure_local/refresh with call counters, plus a conflict type.

    `refresh_calls` is the load-bearing counter: it separates "re-read the local
    mirror" (ensure_local — the wedged shape) from "force-pull the remote and
    re-take the If-Match fence" (refresh — the fix). `_held` tracks lock state
    so the read-time scope assertion can observe it; _fileops.acquire_lock
    routes through the backend, so this is the real lock signal, not a proxy.
    """

    def __init__(self):
        self.conflict_error = _Conflict
        self.refresh_calls = 0
        self.ensure_local_calls = 0
        self._held = set()

    def ensure_local(self, p):
        self.ensure_local_calls += 1
        return p

    def refresh(self, p):
        self.refresh_calls += 1
        return p

    def acquire_lock(self, lock_path, timeout=10, stale_seconds=30):
        self._held.add(str(lock_path))

    def release_lock(self, lock_path):
        self._held.discard(str(lock_path))

    def atomic_write(self, target, write_to_handle, *, max_retries=10):
        class _Result:
            fallback_used = False
            retry_count = 0
            error_msg = ""
        with open(target, "w", encoding="utf-8") as h:
            write_to_handle(h)
        return _Result()


def _retry_globals():
    """The namespace `_rmw_with_conflict_retry` actually reads at call time.

    mind_api/src/file_locks.py binds the FUNCTION OBJECT once via `from
    _fileops import _rmw_with_conflict_retry`. That function resolves
    `get_backend` and `_conflict_backoff` from its own __globals__ — the
    _fileops module dict it was DEFINED in — which is not reliably the dict
    importlib hands back after a sibling suite reloads _fileops. Patching the
    wrong one lets `conflict_cls` fall back to the real backend's empty-tuple
    type, `except conflict_cls` matches nothing, and the injected _Conflict
    escapes: the conflict tests then pass in isolation and fail in the full
    suite. Going through __globals__ is exact and reload-proof (same note in
    test_meta_yaml_conflict_retry.py / test_experience_conflict_retry.py).
    """
    return BP.file_locks._rmw_with_conflict_retry.__globals__


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """Retry sleeps would make these tests slow for no signal."""
    monkeypatch.setitem(_retry_globals(), "_conflict_backoff", lambda *_: 0)


@pytest.fixture()
def meta_dir(tmp_path):
    d = tmp_path / "meta"
    (d / "experiments").mkdir(parents=True)
    (d / "transfer").mkdir(parents=True)
    return d


@pytest.fixture()
def backend(monkeypatch):
    be = _StubBackend()
    # Three namespaces resolve get_backend on these paths and are NOT
    # guaranteed to be the same dict (see _retry_globals):
    #   1. storage_backend — every _read_yaml here imports it at CALL time
    #   2. the retry primitive's own __globals__ — where conflict_cls comes from
    #   3. the current _fileops module object — the lock path + atomic write
    import storage_backend
    monkeypatch.setattr(storage_backend, "get_backend", lambda: be)
    monkeypatch.setitem(_retry_globals(), "get_backend", lambda: be)
    monkeypatch.setattr(_fileops_mod(), "get_backend", lambda: be, raising=False)
    return be


@pytest.fixture(autouse=True)
def _quiet_side_effects(monkeypatch):
    """history/changelog/cruft-guard write outside the tmp tree; not under test."""
    for mod in (BP, EX, TR, SA):
        monkeypatch.setattr(mod.history, "snapshot", lambda *a, **k: None)
        monkeypatch.setattr(mod.changelog, "append", lambda *a, **k: None)
        monkeypatch.setattr(mod, "assert_not_cruft", lambda *a, **k: None)


def _ctx(meta_dir: Path, body=None, query=None):
    import json as _json
    return SimpleNamespace(
        headers={"x-mind-agent": "alpha"},
        body=_json.dumps(body).encode("utf-8") if body is not None else b"",
        query=query or {},
        paths=SimpleNamespace(meta=meta_dir, world=meta_dir.parent / "world",
                              agent=meta_dir.parent / "agent",
                              project_root=meta_dir.parent),
    )


def _yaml_of(path: Path):
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _bp(meta_dir):
    return _yaml_of(meta_dir / "backpressure.yaml")


def _seed_bp(meta_dir, monitors):
    (meta_dir / "backpressure.yaml").write_text(
        yaml.safe_dump({"version": 1, "active_monitors": monitors,
                        "rollback_history": []}, sort_keys=False),
        encoding="utf-8")


def _flaky(module, fail_on, side_effect=None):
    """Make `module._atomic_write_yaml`'s underlying write raise _Conflict on the
    Nth call. Injects at the same seam the sibling suites use: the module-level
    _atomic_write_with_fallback each _atomic_write_yaml delegates to."""
    real = module._atomic_write_with_fallback
    calls = {"n": 0}

    def flaky_write(path, write_fn, **kw):
        calls["n"] += 1
        if calls["n"] in fail_on:
            if side_effect is not None:
                side_effect(path)
            raise _Conflict("412 stale If-Match")
        return real(path, write_fn, **kw)

    return flaky_write, calls


# ---------------------------------------------------------------------------
# meta_backpressure.monitor — the simplest of the five; pins all three halves.
# ---------------------------------------------------------------------------

def _monitor_body(change_id="mc-1"):
    return {"change_id": change_id, "file": "goal-selection-strategy.yaml",
            "field": "weights.novelty", "old": 1.0, "new": 1.2, "baseline": 0.5}


def test_monitor_refreshes_the_fence(meta_dir, backend):
    """Invariant 1: the cycle force-pulls instead of trusting the mirror."""
    resp = BP.monitor(_ctx(meta_dir, _monitor_body()))

    assert resp.status == 200, "monitor failed: {}".format(resp.body)
    assert backend.refresh_calls >= 1, (
        "write cycle never called refresh() — it is fencing against the local "
        "mirror, which is the rb-2639 stale-IfMatch wedge")
    assert [m["meta_change_id"] for m in _bp(meta_dir)["active_monitors"]] == ["mc-1"]


def test_monitor_retries_a_conflict_and_lands_exactly_once(meta_dir, backend, monkeypatch):
    """Invariants 2+3: a conflict is absorbed, re-fenced, and applied ONCE."""
    flaky, calls = _flaky(BP, fail_on={1})
    monkeypatch.setattr(BP, "_atomic_write_with_fallback", flaky)

    resp = BP.monitor(_ctx(meta_dir, _monitor_body()))

    assert resp.status == 200, "conflict was not absorbed: {}".format(resp.body)
    assert calls["n"] == 2, "conflict was not retried"
    assert backend.refresh_calls >= 2, (
        "the retry did not RE-fence — retrying against the same stale token "
        "conflicts identically forever (the deadlock, not a transient)")
    ids = [m["meta_change_id"] for m in _bp(meta_dir)["active_monitors"]]
    assert ids == ["mc-1"], "the retry must apply the append exactly once: {}".format(ids)


def test_monitor_read_happens_inside_the_lock(meta_dir, backend, monkeypatch):
    """Invariant 3, at the assertion point that actually discriminates.

    Hoisting the read out of the cycle while leaving refresh() and locked_rmw
    intact leaves every other test in this file GREEN — the revert is only
    visible at READ time, because a hoisted read still WRITES under the lock.
    """
    events = []
    real_refresh = backend.refresh
    real_write = BP._atomic_write_with_fallback

    def spy_refresh(p):
        events.append(("read", bool(backend._held)))
        return real_refresh(p)

    def spy_write(path, write_fn, **kw):
        events.append(("write", bool(backend._held)))
        return real_write(path, write_fn, **kw)

    monkeypatch.setattr(backend, "refresh", spy_refresh)
    monkeypatch.setattr(BP, "_atomic_write_with_fallback", spy_write)

    BP.monitor(_ctx(meta_dir, _monitor_body()))

    kinds = [k for k, _ in events]
    assert "write" in kinds, "nothing was written"
    first_write = kinds.index("write")
    rmw_reads = [held for k, held in events[:first_write] if k == "read"]

    assert len(rmw_reads) >= 1, (
        "no force_fresh read preceded the write — the cycle is not re-fencing")
    assert all(rmw_reads), (
        "a force_fresh READ happened with NO lock held ({}) — the read has been "
        "hoisted out of the locked_rmw cycle, re-opening the unlocked-RMW "
        "lost-update window a peer write slips through".format(rmw_reads))
    assert events[first_write][1], "the WRITE happened with no lock held"


def test_monitor_retry_preserves_a_peer_write(meta_dir, backend, monkeypatch):
    """The lost-update pin: a peer's monitor must survive our retry.

    With the read hoisted, the retry rewrites the file from the pre-conflict
    snapshot and the peer's monitor vanishes — silently, with a 200.
    """
    _seed_bp(meta_dir, [])

    def peer_lands(path):
        data = _yaml_of(path)
        data.setdefault("active_monitors", []).append(
            {"meta_change_id": "mc-peer", "status": "monitoring",
             "strategy_file": "x.yaml", "field": "f", "baseline_imp_k": 0.1,
             "consecutive_below_baseline": 0, "consecutive_above_baseline": 0,
             "goals_since_change": 0, "imp_k_samples": []})
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    flaky, _calls = _flaky(BP, fail_on={1}, side_effect=peer_lands)
    monkeypatch.setattr(BP, "_atomic_write_with_fallback", flaky)

    resp = BP.monitor(_ctx(meta_dir, _monitor_body()))

    assert resp.status == 200
    ids = [m["meta_change_id"] for m in _bp(meta_dir)["active_monitors"]]
    assert "mc-peer" in ids, (
        "THE PEER'S MONITOR WAS LOST ({}) — the retry rewrote the file from a "
        "pre-conflict snapshot, which means the read is outside the cycle. This "
        "is the lost-update window invariant 3 exists to close.".format(ids))
    assert "mc-1" in ids, "our own monitor did not land"


# ---------------------------------------------------------------------------
# meta_backpressure.check — accumulator idempotence. The store can be perfectly
# correct while the RESPONSE double-counts, and only this shape catches it.
# ---------------------------------------------------------------------------

def _below_monitor(change_id="mc-reg", below=4):
    return {"meta_change_id": change_id, "strategy_file": "goal-selection-strategy.yaml",
            "field": "weights.novelty", "old_value": 1.0, "new_value": 1.2,
            "baseline_imp_k": 0.9, "goals_since_change": 10, "imp_k_samples": [],
            "consecutive_below_baseline": below, "consecutive_above_baseline": 0,
            "status": "monitoring", "created": "2026-01-01T00:00:00"}


def test_check_retry_does_not_duplicate_rollback_actions(meta_dir, backend, monkeypatch):
    """`rollback_actions` must be rebuilt per attempt, not accumulated outside.

    Hoisting the three accumulator lists to the enclosing scope — their shape
    before this fix — leaves the STORE correct (each attempt starts from a fresh
    read) while the RESPONSE reports the rollback twice. A duplicated rollback
    is indistinguishable from a real one to every downstream consumer.
    """
    _seed_bp(meta_dir, [_below_monitor()])
    flaky, calls = _flaky(BP, fail_on={1})
    monkeypatch.setattr(BP, "_atomic_write_with_fallback", flaky)

    resp = BP.check(_ctx(meta_dir, {"learning_value": 0.1}))

    assert resp.status == 200, "conflict not absorbed: {}".format(resp.body)
    assert calls["n"] == 2, "conflict was not retried"

    import json as _json
    payload = _json.loads(resp.body if isinstance(resp.body, str)
                          else resp.body.decode("utf-8"))
    assert len(payload["rollback_actions"]) == 1, (
        "the retry duplicated rollback_actions ({}) — the accumulator is built "
        "OUTSIDE the cycle, so every attempt appends again".format(
            len(payload["rollback_actions"])))
    history = _bp(meta_dir).get("rollback_history", [])
    assert len(history) == 1, (
        "rollback_history got {} entries for one rollback".format(len(history)))


# ---------------------------------------------------------------------------
# meta_backpressure.evolution_check — the invariant NO sibling suite covers:
# a cycle whose body has externally-visible, NON-IDEMPOTENT side effects.
# ---------------------------------------------------------------------------

def _evo_monitor(revision="rev-1", below=5):
    return {"monitor_kind": "self_evolution", "revision_id": revision,
            "file_path": "agents/alpha/self.md", "agent": "alpha",
            "history_snapshot": "/tmp/snap.md", "baseline": {"sig": 1.0},
            "metric_samples": [], "consecutive_below_baseline": below,
            "consecutive_above_baseline": 0, "status": "monitoring",
            "created": "2026-01-01T00:00:00"}


def test_evolution_check_rollback_side_effect_fires_once_across_retries(
        meta_dir, backend, monkeypatch):
    """A retry must NOT re-restore the file, re-post the board, or RE-EMAIL.

    _evolution_rollback restores a file through history.py, appends a world
    stream, posts to the coordination board and sends the user an email. None of
    that is idempotent, and locked_rmw re-runs the cycle. Wrapping this handler
    without caching the executed rollback converts a correctness fix into a
    duplicate-email bug — a regression a store-only assertion cannot see, since
    the persisted rollback_history is deduped by the fresh re-read either way.
    """
    # below=3 with regression_window=3 below: +1 on this pass crosses it. The
    # window is pinned here rather than inherited from core/config/meta.yaml so
    # the test cannot go vacuously green (fire 0 times, assert nothing) if a
    # config default drifts — the failure mode this very test caught on its
    # first run, where a seed of 5 never reached the real default of 8.
    _seed_bp(meta_dir, [_evo_monitor(below=3)])
    monkeypatch.setattr(BP, "_load_evolution_config", lambda ctx: {
        "per_kind": {"self_evolution": {"regression_window": 3,
                                        "graduation_window": 99}}})
    fired = {"n": 0}

    def counting_rollback(ctx, mon, ev_cfg, current, vote):
        fired["n"] += 1
        return {"revision_id": mon.get("revision_id"), "rolled_back": True,
                "monitor_kind": mon.get("monitor_kind"), "attempt": fired["n"]}

    monkeypatch.setattr(BP, "_evolution_rollback", counting_rollback)
    monkeypatch.setattr(BP, "_sample_vector", lambda *a, **k: {"sig": 0.1})
    monkeypatch.setattr(BP, "_aggregate_vote",
                        lambda *a, **k: {"vote": "below", "worst_signal": "sig",
                                         "worst_drop": 0.9, "majority_below": 1})

    flaky, calls = _flaky(BP, fail_on={1})
    monkeypatch.setattr(BP, "_atomic_write_with_fallback", flaky)

    resp = BP.evolution_check(_ctx(meta_dir))

    assert resp.status == 200, "conflict not absorbed: {}".format(resp.body)
    assert calls["n"] == 2, "conflict was not retried"
    assert fired["n"] == 1, (
        "_evolution_rollback fired {} times for ONE rollback — the retry "
        "re-executed a file restore, a world-stream append, a board post and an "
        "EMAIL TO A HUMAN. The executed record must be cached across attempts."
        .format(fired["n"]))

    history = _bp(meta_dir).get("rollback_history", [])
    assert len(history) == 1, "rollback_history got {} entries".format(len(history))
    assert history[0]["attempt"] == 1, (
        "the persisted record came from a re-execution, not the cache")


def test_evolution_check_rollback_cache_does_not_collide_across_agents(
        meta_dir, backend, monkeypatch):
    """The side-effect cache must key on the AGENT too (fresh-eyes 2026-07-30).

    evolution_monitor stamps each monitor with the registering agent, so in an
    N-agent fleet two agents legitimately hold monitors with the SAME
    (kind, revision_id, file_path) — the normal case for a shared file. A cache
    keyed only on that triple collides, and unlike every other hazard in this
    file the collision bites on the FIRST pass rather than only on a retry: the
    second monitor finds the first's record cached, replays it, and its own
    rollback NEVER EXECUTES. Note there is no conflict injected here at all —
    that is the point.
    """
    _seed_bp(meta_dir, [
        dict(_evo_monitor(below=3), agent="alpha"),
        dict(_evo_monitor(below=3), agent="bravo"),   # same kind/revision/path
    ])
    monkeypatch.setattr(BP, "_load_evolution_config", lambda ctx: {
        "per_kind": {"self_evolution": {"regression_window": 3,
                                        "graduation_window": 99}}})
    fired = []

    def counting_rollback(ctx, mon, ev_cfg, current, vote):
        fired.append(mon.get("agent"))
        return {"revision_id": mon.get("revision_id"), "agent": mon.get("agent"),
                "monitor_kind": mon.get("monitor_kind"), "rolled_back": True}

    monkeypatch.setattr(BP, "_evolution_rollback", counting_rollback)
    monkeypatch.setattr(BP, "_sample_vector", lambda *a, **k: {"sig": 0.1})
    monkeypatch.setattr(BP, "_aggregate_vote",
                        lambda *a, **k: {"vote": "below", "worst_signal": "sig",
                                         "worst_drop": 0.9, "majority_below": 1})

    resp = BP.evolution_check(_ctx(meta_dir))

    assert resp.status == 200, "evolution_check failed: {}".format(resp.body)
    assert sorted(fired) == ["alpha", "bravo"], (
        "expected BOTH agents' rollbacks to execute, got {} — the side-effect "
        "cache key omits `agent`, so the second agent's monitor replayed the "
        "first's cached record and its rollback never ran".format(fired))
    history = _bp(meta_dir).get("rollback_history", [])
    assert sorted(r.get("agent") for r in history) == ["alpha", "bravo"], (
        "rollback_history must carry one record PER AGENT, got {} — a collided "
        "cache persists a duplicate of the first agent's record".format(
            [r.get("agent") for r in history]))


# ---------------------------------------------------------------------------
# meta_experiment.create — the fresh read feeds ID MINTING, so a stale read is
# not merely lossy, it mints a colliding id.
# ---------------------------------------------------------------------------

def _seed_active(meta_dir, experiments):
    (meta_dir / "experiments" / "active-experiments.yaml").write_text(
        yaml.safe_dump({"experiments": experiments}, sort_keys=False),
        encoding="utf-8")


def _create_body():
    return {"strategy": "goal-selection-strategy.yaml", "field": "weights.novelty",
            "baseline": 1.0, "variant": 1.3}


def test_experiment_create_retry_re_mints_id_against_peer_write(
        meta_dir, backend, monkeypatch):
    """A peer's experiment lands in the same 412 that rejects ours.

    _next_id is derived from the list the cycle read. With the read inside the
    cycle the retry re-derives it and both experiments survive with distinct
    ids; with the read hoisted the retry re-uses the pre-conflict id AND writes
    the pre-conflict list, so the peer's experiment vanishes.
    """
    _seed_active(meta_dir, [])
    monkeypatch.setattr(EX, "_config",
                        lambda ctx: {"experiments": {"max_concurrent": 10}})

    def peer_lands(path):
        data = _yaml_of(path)
        data.setdefault("experiments", []).append(
            {"id": "exp-001", "status": "active", "strategy_file": "peer.yaml",
             "field": "peer", "created": "2026-02-02T00:00:00"})
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    flaky, calls = _flaky(EX, fail_on={1}, side_effect=peer_lands)
    monkeypatch.setattr(EX, "_atomic_write_with_fallback", flaky)

    resp = EX.create(_ctx(meta_dir, _create_body()))

    assert resp.status == 200, "conflict not absorbed: {}".format(resp.body)
    assert calls["n"] == 2, "conflict was not retried"
    # The force_fresh HALF, pinned separately and deliberately. The stub's
    # ensure_local and refresh both read the same on-disk file, so no
    # data-level assertion below can tell them apart — swapping
    # `_read_yaml(p, force_fresh=True)` for `_read_yaml(p)` leaves every other
    # assertion in this test GREEN (measured). Counting refresh is the only
    # instrument that separates "re-read the local mirror" (the wedged shape)
    # from "force-pull the remote and re-take the If-Match fence" (the fix).
    assert backend.refresh_calls >= 2, (
        "the cycle is not force-refreshing per attempt — it fences against the "
        "local mirror, which is the rb-2639 stale-IfMatch wedge")

    got = _yaml_of(meta_dir / "experiments" / "active-experiments.yaml")["experiments"]
    ids = [e["id"] for e in got]
    assert "exp-001" in ids, (
        "THE PEER'S EXPERIMENT WAS LOST ({}) — the retry wrote a pre-conflict "
        "snapshot".format(ids))
    assert len(ids) == len(set(ids)), (
        "the retry re-used a stale id and collided with the peer: {} — _next_id "
        "must be re-derived from the fresh in-cycle read".format(ids))
    assert len(ids) == 2, "expected the peer's row plus ours, got {}".format(ids)


def test_experiment_create_max_concurrent_still_maps_to_409(meta_dir, backend, monkeypatch):
    """The early return moved inside the cycle — it must still reach the caller
    as a 409, and must not write."""
    _seed_active(meta_dir, [{"id": "exp-001", "status": "active"}])
    monkeypatch.setattr(EX, "_config",
                        lambda ctx: {"experiments": {"max_concurrent": 1}})

    resp = EX.create(_ctx(meta_dir, _create_body()))

    assert resp.status == 409, "expected 409, got {}".format(resp.status)
    got = _yaml_of(meta_dir / "experiments" / "active-experiments.yaml")["experiments"]
    assert len(got) == 1, "a refused create must not write"


def test_experiment_resolve_not_found_still_maps_to_404(meta_dir, backend):
    """resolve's 404 exit now lives inside cycle A and returns through
    locked_rmw — a no-write cycle must not be retried into something else."""
    _seed_active(meta_dir, [])
    resp = EX.resolve(_ctx(meta_dir, {"id": "exp-nope"}))
    assert resp.status == 404, "expected 404, got {}".format(resp.status)


def test_experiment_resolve_retry_preserves_a_peer_write(meta_dir, backend, monkeypatch):
    """resolve read BOTH files fully unlocked before this fix — the widest RMW
    window of the five sites. The peer's row must survive the retry."""
    _seed_active(meta_dir, [
        {"id": "exp-001", "status": "active", "strategy_file": "s.yaml",
         "field": "f", "metrics": {"baseline": [1.0], "variant": [2.0]}}])
    (meta_dir / "experiments" / "completed-experiments.yaml").write_text(
        yaml.safe_dump({"experiments": []}, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(EX, "_config",
                        lambda ctx: {"experiments": {"significance_threshold": 0.05}})

    def peer_lands(path):
        data = _yaml_of(path)
        data.setdefault("experiments", []).append(
            {"id": "exp-peer", "status": "active", "strategy_file": "peer.yaml"})
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    flaky, calls = _flaky(EX, fail_on={1}, side_effect=peer_lands)
    monkeypatch.setattr(EX, "_atomic_write_with_fallback", flaky)

    resp = EX.resolve(_ctx(meta_dir, {"id": "exp-001"}))

    assert resp.status == 200, "conflict not absorbed: {}".format(resp.body)
    assert backend.refresh_calls >= 2, "the retry did not re-fence"
    active = _yaml_of(meta_dir / "experiments" / "active-experiments.yaml")["experiments"]
    completed = _yaml_of(
        meta_dir / "experiments" / "completed-experiments.yaml")["experiments"]

    assert [e["id"] for e in active] == ["exp-peer"], (
        "THE PEER'S EXPERIMENT WAS LOST ({}) — the retry wrote a pre-conflict "
        "snapshot".format([e["id"] for e in active]))
    assert [e["id"] for e in completed] == ["exp-001"], (
        "the resolved experiment must land in completed exactly once: {}".format(
            [e["id"] for e in completed]))


# ---------------------------------------------------------------------------
# meta_transfer.export — transfer/_index.yaml
# ---------------------------------------------------------------------------

def test_transfer_export_retry_preserves_a_peer_bundle(meta_dir, backend, monkeypatch):
    """Two boxes exporting concurrently must not lose a registration."""
    index_path = meta_dir / "transfer" / "_index.yaml"
    index_path.write_text(yaml.safe_dump({"bundles": []}, sort_keys=False),
                          encoding="utf-8")
    (meta_dir / "self.md").write_text("# self\n", encoding="utf-8")

    def peer_lands(path):
        if path != index_path:
            return
        data = _yaml_of(path)
        data.setdefault("bundles", []).append({"path": "peer.yaml",
                                               "exported": "2026-02-02",
                                               "source": "bravo"})
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    flaky, calls = _flaky(TR, fail_on={1}, side_effect=peer_lands)
    monkeypatch.setattr(TR, "_atomic_write_yaml_csafe",
                        lambda p, d: flaky(p, lambda h: yaml.safe_dump(d, h)))

    resp = TR.export(_ctx(meta_dir, {"output": "mine.yaml"}))

    assert resp.status == 200, "conflict not absorbed: {}".format(resp.body)
    assert calls["n"] == 2, "conflict was not retried"
    assert backend.refresh_calls >= 2, "the retry did not re-fence"
    paths = [b["path"] for b in _yaml_of(index_path)["bundles"]]
    assert "peer.yaml" in paths, (
        "THE PEER'S BUNDLE REGISTRATION WAS LOST ({}) — the index append is "
        "reading outside the cycle".format(paths))
    assert paths.count("mine.yaml") == 1, (
        "our registration landed {} times: {}".format(paths.count("mine.yaml"), paths))


# ---------------------------------------------------------------------------
# SCOPE. These pin the DISCRIMINATOR behind leaving four class-(a) stores on a
# bare lock, so a future audit reading only "these helpers look identical" does
# not "complete" this goal by converting them.
# ---------------------------------------------------------------------------

def test_scope_write_classes_are_what_the_cure_assumed():
    """The one lookup guard-1733 mandates, pinned as an executable assertion.

    Every routing decision in this changeset derives from merge_handler_for.
    If the registry moves a file across the split, THIS test is the signal to
    re-derive the cure — not a silent behaviour change in production.
    """
    from coordination_merge import merge_handler_for

    fence_only = ["backpressure.yaml", "active-experiments.yaml",
                  "completed-experiments.yaml", "_index.yaml",
                  "reflection-strategy.yaml", "encoding-strategy.yaml",
                  "aspiration-generation-strategy.yaml"]
    merge_protected = ["goal-selection-strategy.yaml", "strategy-generations.yaml",
                       "pipeline-meta.json"]

    for name in fence_only:
        assert merge_handler_for(name) is None, (
            "{} became MERGE-PROTECTED — it no longer needs the locked_rmw cure "
            "this changeset gave it".format(name))
    for name in merge_protected:
        assert merge_handler_for(name) is not None, (
            "{} became FENCE-ONLY — it was deliberately LEFT on a bare lock and "
            "now needs the locked_rmw + force_fresh cure its siblings got"
            .format(name))


def test_scope_strategy_apply_routes_each_file_by_its_own_class(meta_dir, backend,
                                                                monkeypatch):
    """The mixed-class pin, asserted in BOTH directions in one run.

    STRATEGY_FILES drives a class-(a) and a class-(b) file through the SAME
    loop. The class-(b) file must retry a conflict; the class-(a) file must NOT
    be wrapped (its merge handler reconciles below the write). Asserting only
    the (b) direction would pass a blanket conversion that erases the split —
    the specific wrong "fix" this test exists to refuse.
    """
    for fname, field in (("goal-selection-strategy.yaml", "selection_heuristics"),
                         ("aspiration-generation-strategy.yaml", "generation_heuristics")):
        (meta_dir / fname).write_text(
            yaml.safe_dump({field: [{"id": "h-1", "description": "widget tuning rule"}]},
                           sort_keys=False), encoding="utf-8")

    # class (b): a conflict on the generation file must be absorbed and retried.
    flaky_b, calls_b = _flaky(SA, fail_on={1})
    monkeypatch.setattr(SA, "_atomic_write_with_fallback", flaky_b)
    before_refresh = backend.refresh_calls
    matched = SA._run_match(_ctx(meta_dir), "generation", ["widget"], True)
    assert backend.refresh_calls - before_refresh >= 2, (
        "the class-(b) cycle is not force-refreshing per attempt — _load must be "
        "called with force_fresh=True inside the cycle")
    assert calls_b["n"] == 2, (
        "aspiration-generation-strategy.yaml is class (b) and did NOT retry a "
        "conflict — it is still on the bare lock")
    assert len(matched) == 1, (
        "the retry duplicated the matched list ({}) — it is accumulated outside "
        "the cycle".format(len(matched)))
    got = _yaml_of(meta_dir / "aspiration-generation-strategy.yaml")
    assert got["generation_heuristics"][0]["times_applied"] == 1, (
        "the increment must land exactly once across the retry")

    # class (a): the same conflict on the selection file must NOT be retried.
    flaky_a, calls_a = _flaky(SA, fail_on={1})
    monkeypatch.setattr(SA, "_atomic_write_with_fallback", flaky_a)
    with pytest.raises(_Conflict):
        SA._run_match(_ctx(meta_dir), "selection", ["widget"], True)
    assert calls_a["n"] == 1, (
        "goal-selection-strategy.yaml is MERGE-PROTECTED (class (a)) and was "
        "wrapped in locked_rmw anyway — the blanket conversion erases the "
        "write-class split that guard-1733 exists to preserve")


def test_scope_meta_generations_left_on_the_bare_lock(meta_dir, backend, monkeypatch):
    """strategy-generations.yaml is class (a): a conflict must surface, not retry.

    This module's _persist is byte-identical in shape to the fence-only ones
    that WERE cured. That similarity is the inference guard-1733 forbids, and
    this test is what keeps the next reader from acting on it.
    """
    from mind_api.src.meta import meta_generations as GEN
    monkeypatch.setattr(GEN.history, "snapshot", lambda *a, **k: None)
    monkeypatch.setattr(GEN.changelog, "append", lambda *a, **k: None)
    monkeypatch.setattr(GEN, "assert_not_cruft", lambda *a, **k: None)

    flaky, calls = _flaky(GEN, fail_on={1})
    monkeypatch.setattr(GEN, "_atomic_write_with_fallback", flaky)

    with pytest.raises(_Conflict):
        GEN._persist(_ctx(meta_dir), meta_dir / "strategy-generations.yaml",
                     {"version": 1})
    assert calls["n"] == 1, (
        "meta_generations was wrapped in locked_rmw — strategy-generations.yaml "
        "is MERGE-PROTECTED and a reconciler already runs below the write")
