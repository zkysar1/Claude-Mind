"""Fail-open-ladder provenance tests for liveness_check + _team_state ().

WHY THIS FILE EXISTS SEPARATELY FROM test_liveness_check.py: that suite injects
into the PURE ``decide_liveness``, so it can never observe the defect this file
guards. The bug lived in the IO half — ``_team_state.read_shard_authoritative``
fails open to the LOCAL MIRROR at three layers and returned a bare dict, so
``fetch_authoritative_last_active`` handed a mirror value to ``decide_liveness``
as though it came from the store of record. ``decide_liveness`` then returned
verdict=alive / signal=authoritative_last_active with a reason asserting "the
local mirror lagged" — about a value read FROM that mirror. A test that starts
at the pure function has already skipped the layer that lies.

So every test below drives the LADDER: it forces a specific fail-open layer and
asserts on what comes out the far end. guard-1753 is the general rule (a
fail-open reader must be able to express its own failure); guard-1937 is why the
local-backend case is pinned too (a branch nobody exercises carries no evidence).
"""
import os
import sys
import types
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _team_state  # noqa: E402
import liveness_check as lc  # noqa: E402

NOW = datetime(2026, 8, 3, 12, 0, 0)


def _ago(**kw):
    return (NOW - timedelta(**kw)).isoformat(timespec="seconds")


def _world(tmp_path, agent="peer", last_active=None):
    """A world dir with one agent shard on disk. Returns (world_dir, shard_path)."""
    d = tmp_path / "world" / "team-state" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    shard = d / f"{agent}.yaml"
    shard.write_text(f"agent: {agent}\nlast_active: '{last_active or _ago(minutes=5)}'\n",
                     encoding="utf-8")
    return str(tmp_path / "world"), shard


def _fake_owncloud(monkeypatch, *, from_env_raises=None, read_text_raises=None,
                   read_text_returns=None):
    """Install a fake ``owncloud_backend`` module driving one ladder layer."""
    class _Backend:
        @staticmethod
        def from_env():
            if from_env_raises is not None:
                raise from_env_raises
            return _Backend()

        def read_text(self, path, force_fresh=False):
            if read_text_raises is not None:
                raise read_text_raises
            return read_text_returns

    mod = types.ModuleType("owncloud_backend")
    mod.OwnCloudBackend = _Backend
    monkeypatch.setitem(sys.modules, "owncloud_backend", mod)
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")


# ---------------------------------------------------------------- ladder layers

def test_local_backend_is_authoritative_not_mirror(tmp_path, monkeypatch):
    """On a non-own-cloud deployment the local file IS the store of record.

    Pinned because the tempting over-correction — 'the read came off local disk,
    call it a mirror' — would degrade EVERY verdict on every local deployment to
    unknown, turning a false-alive fix into a fleet-wide false-unknown.
    """
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    world, _ = _world(tmp_path)
    row, prov = _team_state.read_shard_authoritative_with_provenance(world, "peer")
    assert isinstance(row, dict)
    assert prov == _team_state.PROV_AUTHORITATIVE


def test_owncloud_backend_init_error_degrades_to_local_mirror(tmp_path, monkeypatch):
    world, _ = _world(tmp_path)
    _fake_owncloud(monkeypatch, from_env_raises=RuntimeError("no creds"))
    row, prov = _team_state.read_shard_authoritative_with_provenance(world, "peer")
    assert isinstance(row, dict), "fail-open must still yield the mirror row"
    assert prov == _team_state.PROV_LOCAL_MIRROR


def test_owncloud_read_error_degrades_to_local_mirror(tmp_path, monkeypatch):
    world, _ = _world(tmp_path)
    _fake_owncloud(monkeypatch, read_text_raises=OSError("transient S3 error"))
    row, prov = _team_state.read_shard_authoritative_with_provenance(world, "peer")
    assert isinstance(row, dict)
    assert prov == _team_state.PROV_LOCAL_MIRROR


def test_owncloud_empty_doc_degrades_to_local_mirror(tmp_path, monkeypatch):
    """The silent layer: no exception, just an empty/non-dict document."""
    world, _ = _world(tmp_path)
    _fake_owncloud(monkeypatch, read_text_returns="")
    row, prov = _team_state.read_shard_authoritative_with_provenance(world, "peer")
    assert isinstance(row, dict)
    assert prov == _team_state.PROV_LOCAL_MIRROR


def test_owncloud_success_is_authoritative(tmp_path, monkeypatch):
    world, _ = _world(tmp_path)
    _fake_owncloud(monkeypatch,
                   read_text_returns="agent: peer\nlast_active: '2026-08-03T11:59:00'\n")
    row, prov = _team_state.read_shard_authoritative_with_provenance(world, "peer")
    assert row["last_active"] == "2026-08-03T11:59:00"
    assert prov == _team_state.PROV_AUTHORITATIVE


def test_no_shard_anywhere_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    world, shard = _world(tmp_path)
    shard.unlink()
    row, prov = _team_state.read_shard_authoritative_with_provenance(world, "peer")
    assert row is None
    assert prov == _team_state.PROV_NONE


def test_bare_wrapper_returns_pair_first_element(tmp_path, monkeypatch):
    """The pre- contract stays byte-identical for provenance-blind callers
    (mind_api aspirations_write's claim-holder probe is one)."""
    world, _ = _world(tmp_path)
    _fake_owncloud(monkeypatch, from_env_raises=RuntimeError("boom"))
    pair = _team_state.read_shard_authoritative_with_provenance(world, "peer")
    assert _team_state.read_shard_authoritative(world, "peer") == pair[0]


# ------------------------------------------------- ladder -> verdict (end to end)

def test_ladder_to_verdict_degrades_to_unknown(tmp_path, monkeypatch):
    """THE REGRESSION TEST. Real ladder, forced to fail open, with a FRESH mirror
    value and a STALE local last_active — the exact conjunction that produced the
    false ALIVE. Must be unknown, never alive."""
    world, _ = _world(tmp_path, last_active=_ago(minutes=5))
    _fake_owncloud(monkeypatch, read_text_raises=OSError("transient S3 error"))

    iso, prov = lc.fetch_authoritative_last_active_with_provenance("peer", world)
    assert prov == _team_state.PROV_LOCAL_MIRROR
    assert iso is not None, "the mirror value is still read; only its trust changes"

    r = lc.decide_liveness(_ago(days=7), None, threshold_hours=6, now=NOW,
                           authoritative_last_active_iso=iso,
                           authoritative_provenance=prov)
    assert r["verdict"] == "unknown", (
        "a fresh LOCAL-MIRROR last_active must not promote to alive — that asserts "
        "the mirror lagged using a value read from the mirror")
    assert r["signal"] is None


def test_ladder_authoritative_value_still_alive(tmp_path, monkeypatch):
    """Positive control: the fix must not blanket-degrade. Same shape, but the
    authoritative read SUCCEEDS -> alive on the authoritative_last_active signal."""
    world, _ = _world(tmp_path, last_active=_ago(days=9))
    _fake_owncloud(monkeypatch,
                   read_text_returns=f"agent: peer\nlast_active: '{_ago(minutes=5)}'\n")

    iso, prov = lc.fetch_authoritative_last_active_with_provenance("peer", world)
    assert prov == _team_state.PROV_AUTHORITATIVE

    r = lc.decide_liveness(_ago(days=7), None, threshold_hours=6, now=NOW,
                           authoritative_last_active_iso=iso,
                           authoritative_provenance=prov)
    assert r["verdict"] == "alive"
    assert r["signal"] == "authoritative_last_active"


def test_mirror_reason_never_claims_authoritative_provenance(tmp_path, monkeypatch):
    """Outcome 3: no reason string may assert authoritative provenance for a value
    that came from the mirror. The old text said 'the local mirror lagged'."""
    world, _ = _world(tmp_path, last_active=_ago(minutes=5))
    _fake_owncloud(monkeypatch, from_env_raises=RuntimeError("boom"))
    iso, prov = lc.fetch_authoritative_last_active_with_provenance("peer", world)
    reason = lc.decide_liveness(_ago(days=7), None, threshold_hours=6, now=NOW,
                                authoritative_last_active_iso=iso,
                                authoritative_provenance=prov)["reason"]
    assert "the local mirror lagged" not in reason
    assert "authoritative-store shard's" not in reason
    assert "LOCAL MIRROR" in reason, "the reason must NAME the provenance it distrusts"


def test_absent_provenance_stays_byte_identical(tmp_path):
    """Callers predating  pass no provenance; behavior must not move."""
    kw = dict(threshold_hours=6, now=NOW,
              authoritative_last_active_iso=_ago(minutes=5))
    assert lc.decide_liveness(_ago(days=7), None, **kw)["verdict"] == "alive"
    assert lc.decide_liveness(_ago(days=7), None, authoritative_provenance=None,
                              **kw)["verdict"] == "alive"
    assert lc.decide_liveness(_ago(days=7), None,
                              authoritative_provenance=_team_state.PROV_AUTHORITATIVE,
                              **kw)["verdict"] == "alive"


# ------------------------------------------------------------------- contracts

def test_prov_constants_match_liveness_check_literals():
    """Drift pin (guard-426). liveness_check carries TWO literal copies of these
    constants and cannot import them where they are used: decide_liveness is a PURE
    function that deliberately imports nothing (it compares against "local-mirror"),
    and fetch_..._with_provenance's final fallback returns "none" from OUTSIDE the
    try block that imports _team_state — reached precisely when that import failed,
    so the constant is unavailable by construction. Both copies are pinned here.
    Pinning only the first is how the second silently drifts."""
    assert _team_state.PROV_LOCAL_MIRROR == "local-mirror"
    assert _team_state.PROV_NONE == "none"
    assert _team_state.PROV_AUTHORITATIVE == "authoritative"


def test_verdict_carries_provenance_for_every_verdict():
    """The provenance travels with the age it qualifies, in EVERY verdict — not
    bolted on by main() afterwards. Found by /fresh-eyes-code on this goal's own
    diff: a consumer reading authoritative_last_active_age_min otherwise faces the
    same 'number with no provenance' ambiguity g-306-138 exists to remove."""
    for prov in ("authoritative", "local-mirror", "none", None):
        r = lc.decide_liveness(_ago(days=7), _ago(days=7), threshold_hours=6, now=NOW,
                               authoritative_last_active_iso=_ago(days=7),
                               authoritative_provenance=prov)
        assert r["authoritative_last_active_provenance"] == prov
    # ...including the retired verdict, which returns before every freshness branch.
    r = lc.decide_liveness(_ago(minutes=1), None, threshold_hours=6, now=NOW,
                           retired_entry={"retired_at": "x", "retired_by": "y"},
                           authoritative_provenance="local-mirror")
    assert r["verdict"] == "retired"
    assert r["authoritative_last_active_provenance"] == "local-mirror"


def test_fetch_authoritative_last_active_takes_no_backend_arg():
    """Outcome 4: the ignored ``backend`` parameter is REMOVED, not documented away.
    It invited `--backend own-cloud` under an unset STORAGE_BACKEND to send one
    probe to S3 and the other to the local file."""
    import inspect
    for fn in (lc.fetch_authoritative_last_active,
               lc.fetch_authoritative_last_active_with_provenance):
        assert "backend" not in inspect.signature(fn).parameters, (
            f"{fn.__name__} must not accept a backend argument — the shared "
            "primitive dispatches on STORAGE_BACKEND (single source of truth)")
