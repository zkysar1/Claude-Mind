"""Fail-open-ladder provenance tests for ``_team_state.load_rows_authoritative``
(g-306-158) — the multi-shard sibling of the g-306-138 single-shard fix.

WHY THIS FILE EXISTS SEPARATELY FROM test_team_state_authoritative.py: that suite
pins the ROWS (does the authoritative read return the right content?) and is
deliberately provenance-blind, because it predates provenance. It therefore
cannot observe the defect guarded here — that a caller could not tell an
authoritative multi-shard read from a fallback to the local mirror. Keeping it
untouched is also the byte-identical-contract proof (rb-2148): the wrapper it
exercises must behave exactly as before.

WHAT MAKES THE MULTI-SHARD CASE DIFFERENT from the single-shard one, and why a
scalar provenance would be wrong: ONE call can mix layers — agent A reads clean
from S3 while agent B's read raises and falls back to B's local mirror row.
``test_mixed_per_shard_provenance`` is that case, and it is the reason the return
is per-agent. A scalar would have to collapse it, and both collapses are
defects: "authoritative" hides B (guard-1753 false positive), "local-mirror"
degrades A (the blanket-degrade trap, pinned by
``test_local_backend_is_authoritative_not_mirror``).

AND WHY ``roster`` IS A SEPARATE FIELD: the consumer forms a NEGATIVE over the
peer set ("no partners in_flight"). A peer never ENUMERATED has no row and so no
per-agent label, yet that is the measured failure — the local mirror drops peer
shards entirely (echo/zeta absent locally, fresh on S3, cc-04 2026-07-14).
Row-level provenance is structurally silent about an agent it never saw.

STUBBING NOTE (guard-2244 — know what your fixture replaced): ``_backend_rows``
is stubbed to {} throughout. It is the ADDITIVE local-overlay path inside
``load_rows``, reached via ``storage_backend.get_backend`` — a different module
from the ``owncloud_backend`` seam the ladder uses, and orthogonal to it. Left
live under STORAGE_BACKEND=own-cloud it would issue a real S3 list against a
tmp path, making these results network- and box-dependent. The LADDER itself is
NOT stubbed: it runs the real ``load_rows_authoritative_with_provenance`` against
the same fake-backend seam the sibling suite uses.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _team_state as ts  # noqa: E402

# Reuse the sibling suite's fixture seam rather than hand-rolling an 8th
# _FakeBackend copy —  filed  to extract exactly this, and
# adding another copy would make that goal worse.
from test_team_state_authoritative import _FakeBackend, _seed_local_rows  # noqa: E402


class _ListBoom(_FakeBackend):
    """_FakeBackend whose S3 roster listing fails (roster-degradation layer)."""

    def list_dir(self, path):  # noqa: ARG002
        raise RuntimeError("simulated S3 list error")


def _no_overlay(monkeypatch):
    monkeypatch.setattr(ts, "_backend_rows", lambda world_dir: {})
    ts._backend_rows_cache.clear()


def _own_cloud(monkeypatch, backend):
    """Point STORAGE_BACKEND at own-cloud and from_env at `backend`."""
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    import owncloud_backend
    monkeypatch.setattr(owncloud_backend.OwnCloudBackend, "from_env",
                        classmethod(lambda cls: backend))


def _own_cloud_init_raises(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    import owncloud_backend

    def _boom(cls):
        raise RuntimeError("simulated backend init failure")

    monkeypatch.setattr(owncloud_backend.OwnCloudBackend, "from_env",
                        classmethod(_boom))


LOCAL_ROWS = {
    "alpha": {"last_active": "2026-08-01T10:00:00", "in_flight": None},
    "bravo": {"last_active": "2026-07-27T10:00:00",
              "in_flight": {"goal_id": "g-stale", "title": "stale local"}},
}


# ---------------------------------------------------------------- ladder layers

def test_local_backend_is_authoritative_not_mirror(tmp_path, monkeypatch):
    """Non-own-cloud: the local files ARE the store of record.

    The tempting over-correction — "it came off local disk, call it a mirror" —
    would degrade every correct verdict on every local deployment, trading a
    narrow false-negative for a fleet-wide one. A mutation aimed at the
    own-cloud defect path does NOT redden this, which is why it is pinned
    separately (guard-1937).
    """
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    _no_overlay(monkeypatch)
    _seed_local_rows(tmp_path, LOCAL_ROWS)
    rows, prov = ts.load_rows_authoritative_with_provenance(tmp_path)
    assert set(rows) == {"alpha", "bravo"}
    assert prov["roster"] == ts.PROV_AUTHORITATIVE
    assert prov["by_agent"] == {"alpha": ts.PROV_AUTHORITATIVE,
                                "bravo": ts.PROV_AUTHORITATIVE}


def test_backend_init_error_is_all_local_mirror(tmp_path, monkeypatch):
    """Backend never constructed: every row is the mirror's, roster included."""
    _no_overlay(monkeypatch)
    _seed_local_rows(tmp_path, LOCAL_ROWS)
    _own_cloud_init_raises(monkeypatch)
    rows, prov = ts.load_rows_authoritative_with_provenance(tmp_path)
    assert set(rows) == {"alpha", "bravo"}
    assert prov["roster"] == ts.PROV_LOCAL_MIRROR
    assert prov["by_agent"] == {"alpha": ts.PROV_LOCAL_MIRROR,
                                "bravo": ts.PROV_LOCAL_MIRROR}


def test_roster_list_error_degrades_roster_but_not_rows(tmp_path, monkeypatch):
    """S3 list fails, per-shard reads still succeed.

    The rows ARE authoritative; what is unknown is whether the peer SET is
    complete. Exactly the case per-agent provenance alone cannot express.
    """
    _no_overlay(monkeypatch)
    _seed_local_rows(tmp_path, LOCAL_ROWS)
    fresh = {"alpha": {"last_active": "2026-08-03T22:00:00", "in_flight": None},
             "bravo": {"last_active": "2026-08-03T22:00:00", "in_flight": None}}
    _own_cloud(monkeypatch, _ListBoom(fresh))
    rows, prov = ts.load_rows_authoritative_with_provenance(tmp_path)
    assert prov["roster"] == ts.PROV_LOCAL_MIRROR
    assert prov["by_agent"] == {"alpha": ts.PROV_AUTHORITATIVE,
                                "bravo": ts.PROV_AUTHORITATIVE}
    assert rows["bravo"]["last_active"] == "2026-08-03T22:00:00"


def test_mixed_per_shard_provenance(tmp_path, monkeypatch):
    """THE headline case: one call, two different provenances.

    This is what makes a scalar return wrong rather than merely coarse.
    """
    _no_overlay(monkeypatch)
    _seed_local_rows(tmp_path, LOCAL_ROWS)
    fresh = {"alpha": {"last_active": "2026-08-03T22:00:00", "in_flight": None},
             "bravo": {"last_active": "2026-08-03T22:00:00", "in_flight": None}}
    _own_cloud(monkeypatch, _FakeBackend(fresh, errors=["bravo"]))
    rows, prov = ts.load_rows_authoritative_with_provenance(tmp_path)
    assert prov["by_agent"]["alpha"] == ts.PROV_AUTHORITATIVE
    assert prov["by_agent"]["bravo"] == ts.PROV_LOCAL_MIRROR
    assert prov["roster"] == ts.PROV_AUTHORITATIVE
    # bravo's row is the STALE local one — the mirror value, not evidence.
    assert rows["bravo"]["in_flight"]["goal_id"] == "g-stale"


def test_discovered_on_s3_but_unreadable_is_none(tmp_path, monkeypatch):
    """A peer listed on the store of record whose shard will not read.

    Known blindness, not an absent peer: it is labelled PROV_NONE and is absent
    from rows, so a caller cannot mistake "I could not read echo" for
    "echo has nothing in flight".
    """
    _no_overlay(monkeypatch)
    _seed_local_rows(tmp_path, {"alpha": LOCAL_ROWS["alpha"]})
    fresh = {"alpha": {"last_active": "2026-08-03T22:00:00", "in_flight": None},
             "echo": {"last_active": "2026-08-03T22:00:00", "in_flight": None}}
    _own_cloud(monkeypatch, _FakeBackend(fresh, errors=["echo"]))
    rows, prov = ts.load_rows_authoritative_with_provenance(tmp_path)
    assert prov["by_agent"]["alpha"] == ts.PROV_AUTHORITATIVE
    assert prov["by_agent"]["echo"] == ts.PROV_NONE
    assert "echo" not in rows


def test_by_agent_is_total_over_rows(tmp_path, monkeypatch):
    """Every row carries a label — no row can be provenance-less."""
    _no_overlay(monkeypatch)
    _seed_local_rows(tmp_path, LOCAL_ROWS)
    fresh = {"alpha": {"last_active": "2026-08-03T22:00:00", "in_flight": None}}
    _own_cloud(monkeypatch, _FakeBackend(fresh, errors=[]))
    rows, prov = ts.load_rows_authoritative_with_provenance(tmp_path)
    assert set(rows) <= set(prov["by_agent"])


# ------------------------------------------------- consumer: the negative claim

def _run_partner_check(monkeypatch, tmp_path, prov, rows=None, core=None):
    """Drive the duplication gate's partner_in_flight check with a stubbed
    provenance read. Stubbing is right HERE and not in the ladder tests above:
    this pins the CONSUMER's interpretation of provenance, and the producer has
    its own seven-layer ladder (guard-2244 — know which half your fixture
    replaced).

    `core` seeds the MONOLITHIC team-state.yaml agent_status (default empty, as
    before). It is a distinct seam from `rows`: the gate reads this file with a
    plain open() and no force_fresh, so a peer reachable only here is composed
    in off the local mirror — the g-306-179 Direction-2 case.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gates"))
    import goal_duplication as gd

    if rows is None:
        rows = {"alpha": {"in_flight": None}, "bravo": {"in_flight": None}}
    monkeypatch.setattr(ts, "load_rows_authoritative_with_provenance",
                        lambda world_dir: (rows, prov))
    (tmp_path / "team-state.yaml").write_text(
        json.dumps({"agent_status": core or {}}), encoding="utf-8")
    return gd._check_partner_in_flight(
        {"id": "g-prov-01", "title": "probe", "status": "pending"},
        [], set(), "bravo", "world", tmp_path)


DEGRADED = {"by_agent": {"alpha": "local-mirror", "bravo": "authoritative"},
            "roster": "local-mirror"}
CLEAN = {"by_agent": {"alpha": "authoritative", "bravo": "authoritative"},
         "roster": "authoritative"}


def test_consumer_qualifies_the_NON_empty_exit_too(tmp_path, monkeypatch):
    """The second negative exit — 'no blocking overlap' — is the COMMON one.

    Whenever any partner is working, partner_inflights is non-empty and control
    never reaches the empty-set branch, so qualifying only that branch would
    leave provenance unconsulted almost all the time. Caught by probing the LIVE
    daemon after the recycle: two partners were genuinely in flight, and the
    reason came back unqualified from an exit the first fix never touched.
    """
    rows = {"alpha": {"in_flight": {"goal_id": "g-zzz-99",
                                    "title": "entirely unrelated work"}},
            "bravo": {"in_flight": None}}
    res = _run_partner_check(monkeypatch, tmp_path, DEGRADED, rows=rows)
    assert res["passed"] is True                      # fail-open preserved
    assert "no blocking overlap" in res["reason"]     # the non-empty exit
    assert "NOT a verified clear" in res["reason"]    # ...and it is qualified
    assert "alpha" in res["reason"]


def test_consumer_non_empty_exit_stays_clean_when_authoritative(tmp_path, monkeypatch):
    """Negative control for the test above: a fully authoritative read must NOT
    acquire a caveat, or the qualifier becomes noise on every clean pass."""
    rows = {"alpha": {"in_flight": {"goal_id": "g-zzz-99",
                                    "title": "entirely unrelated work"}},
            "bravo": {"in_flight": None}}
    res = _run_partner_check(monkeypatch, tmp_path, CLEAN, rows=rows)
    assert res["passed"] is True
    assert "no blocking overlap" in res["reason"]
    assert "NOT a verified clear" not in res["reason"]


def test_consumer_clean_read_claims_store_of_record(tmp_path, monkeypatch):
    """A genuinely authoritative empty read may say so."""
    res = _run_partner_check(monkeypatch, tmp_path, {
        "by_agent": {"alpha": ts.PROV_AUTHORITATIVE, "bravo": ts.PROV_AUTHORITATIVE},
        "roster": ts.PROV_AUTHORITATIVE})
    assert res["passed"] is True
    assert "store of record" in res["reason"]
    assert "NOT a verified clear" not in res["reason"]


def test_consumer_degraded_read_is_not_a_verified_clear(tmp_path, monkeypatch):
    """The defect this goal exists to fix: an empty result off a degraded read
    must not report as a clean one. Stays fail-open (a transient store error
    must never block filing) — only the CLAIM changes."""
    res = _run_partner_check(monkeypatch, tmp_path, {
        "by_agent": {"alpha": ts.PROV_LOCAL_MIRROR, "bravo": ts.PROV_AUTHORITATIVE},
        "roster": ts.PROV_LOCAL_MIRROR})
    assert res["passed"] is True          # fail-open preserved
    assert "NOT a verified clear" in res["reason"]
    assert "alpha" in res["reason"]       # names the untrusted peer
    assert "roster" in res["reason"]      # and the incomplete enumeration
    # self must never be reported as an untrusted peer
    assert "bravo" not in res["reason"]


# ------------------------------------- consumer: caveat keyed over the COMPOSED
# set, not over rows (). The two pins below are the two directions the
# row-keyed reading got wrong, and they fail in OPPOSITE ways — one invents a
# caveat, one withholds one — so neither alone would have caught the other.

RETIRED_ROW = {"in_flight": None, "retired": True,
               "retired_at": "2026-07-01T00:00:00"}


def test_retired_peer_degraded_shard_does_not_qualify_the_clear(tmp_path,
                                                                monkeypatch):
    """DIRECTION 1 — false alarm, reachable today.

    compose_agent_status drops retired rows BEFORE partner_inflights is built,
    so a degraded read of a retired peer's shard cannot hide an in-flight peer:
    that peer is never examined, and a caveat naming it qualifies a clear over
    something outside the negative's scope. Live shard dir held 8 rows vs 6
    composed, both extras retired.
    """
    rows = {"alpha": {"in_flight": None},
            "bravo": {"in_flight": None},
            "meta-tiebreaker": dict(RETIRED_ROW)}
    prov = {"by_agent": {"alpha": ts.PROV_AUTHORITATIVE,
                         "bravo": ts.PROV_AUTHORITATIVE,
                         "meta-tiebreaker": ts.PROV_LOCAL_MIRROR},
            "roster": ts.PROV_AUTHORITATIVE}
    res = _run_partner_check(monkeypatch, tmp_path, prov, rows=rows)
    assert res["passed"] is True
    assert "NOT a verified clear" not in res["reason"]
    assert "meta-tiebreaker" not in res["reason"]
    assert "store of record" in res["reason"]


def test_core_only_peer_is_not_a_verified_clear(tmp_path, monkeypatch):
    """DIRECTION 2 — silent over-claim, the failure direction that matters.

    A peer reachable only through the monolithic team-state.yaml is composed
    into the peer set, but has no shard and therefore no by_agent key. An
    absent key read as clean, so the reason claimed the store of record over a
    row obtained with a plain open() and no force_fresh — precisely the
    guard-1753 false positive g-306-158 removed, surviving in the half that fix
    did not cover. Fail-open is unchanged; only the CLAIM moves.
    """
    rows = {"alpha": {"in_flight": None}, "bravo": {"in_flight": None}}
    prov = {"by_agent": {"alpha": ts.PROV_AUTHORITATIVE,
                         "bravo": ts.PROV_AUTHORITATIVE},
            "roster": ts.PROV_AUTHORITATIVE}
    core = {"echo": {"last_active": "2026-08-04T03:00:00", "in_flight": None}}
    res = _run_partner_check(monkeypatch, tmp_path, prov, rows=rows, core=core)
    assert res["passed"] is True                    # fail-open preserved
    assert "NOT a verified clear" in res["reason"]
    assert "echo" in res["reason"]                  # names the core-sourced peer
    assert "store of record" not in res["reason"]


def test_wrapper_contract_is_byte_identical(tmp_path, monkeypatch):
    """The provenance-blind wrapper returns exactly element 0 (rb-2148)."""
    _no_overlay(monkeypatch)
    _seed_local_rows(tmp_path, LOCAL_ROWS)
    fresh = {"alpha": {"last_active": "2026-08-03T22:00:00", "in_flight": None},
             "bravo": {"last_active": "2026-08-03T22:00:00", "in_flight": None}}
    _own_cloud(monkeypatch, _FakeBackend(fresh, errors=["bravo"]))
    bare = ts.load_rows_authoritative(tmp_path)
    rows, _ = ts.load_rows_authoritative_with_provenance(tmp_path)
    assert bare == rows
