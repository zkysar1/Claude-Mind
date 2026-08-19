"""Measuring G6 (): D3 is a store+local UNION, and it depends on D2.

`decide()` TAKES the three discriminators and never measures them; for a while
nothing else measured D3 either, so the gate the whole promotion safety argument
rests on was unmeasurable. This file pins the measurement.

Weighted the way guard-2860 requires for a loosened fail-closed role gate: the
test proving D3 can be True cannot fail in the dangerous direction, so it is the
LEAST valuable test here and it comes last. The load-bearing ones are the
refusals, and specifically the two OPPOSITE unsound reads the union exists to
avoid:

  a LOCAL-only read  -> True while a live sibling holds a carrier   (DANGEROUS)
  a STORE-only read  -> False by absence of this Body's own carrier (unreliable)

Structure:
  1. the dangerous direction — a fresh sibling must always refuse
  2. the D2 dependency — a non-authoritative read is None, never True
  3. self is read LOCALLY — the store-only false-by-absence must not recur
  4. the population is reported — a bare boolean is unfalsifiable
  5. the reuse contract — no private copy of the carrier read path
  6. the happy path, last, as a positive control
"""

import datetime as dt
import importlib.util
import json
import pathlib

import pytest

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load("reducer_promotion_disc_under_test", "reducer_promotion.py")

NOW = dt.datetime(2026, 8, 16, 12, 0, 0)
SELF_AGENT = "alpha"
SELF_SID = "sid-self"


def _ts(minutes_ago):
    return (NOW - dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def _carrier(agent, sid, minutes_ago, host="box-x", ts=None):
    return {
        "agent": agent, "sid": sid, "read_via": "authoritative",
        "doc": {"agent": agent, "sid": sid, "host": host,
                "ts": _ts(minutes_ago) if ts is None else ts},
    }


def _write_self(tmp_path, minutes_ago, agent=SELF_AGENT, sid=SELF_SID):
    d = tmp_path / agent / "session"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"body-heartbeat-{sid}.json").write_text(
        json.dumps({"sid": sid, "agent": agent, "host": "box-self",
                    "ts": _ts(minutes_ago)}),
        encoding="utf-8")


@pytest.fixture
def patched(monkeypatch):
    """Drive the sibling half without touching a real store.

    The enumerator is stubbed at `_load_worker_stall`, NOT reimplemented: the
    stub returns an object exposing the same three names the production path
    uses (`DEFAULT_STALE_MINUTES`, `_parse_iso`, `enumerate_carriers`), and
    `_parse_iso` is the REAL one from worker_stall so a parsing change cannot
    pass here while failing in production.
    """
    real_ws = _load("_worker_stall_for_test", "worker_stall.py")

    def _install(rows, meta):
        class _Stub:
            DEFAULT_STALE_MINUTES = real_ws.DEFAULT_STALE_MINUTES
            _parse_iso = staticmethod(real_ws._parse_iso)

            @staticmethod
            def enumerate_carriers(_root):
                return list(rows), dict(meta)

        monkeypatch.setattr(mod, "_load_worker_stall", lambda: _Stub)
        return _Stub

    return _install


AUTH_OK = {"read_via": "authoritative", "complete": True,
           "agents_enumerated": 5, "reason": None, "carrier_read_errors": 0}
MIRROR = {"read_via": "local-mirror", "complete": False,
          "agents_enumerated": 5, "carrier_read_errors": 0,
          "reason": "authoritative unavailable (Boom); read-through mirror "
                    "cannot see carriers this box never pulled"}


def _measure(tmp_path, **kw):
    kw.setdefault("claim_read_authoritative", True)
    kw.setdefault("now", NOW)
    return mod.measure_only_fresh_carrier_is_mine(
        tmp_path, SELF_AGENT, SELF_SID, **kw)


# --------------------------------------------------------------------------
# 1. The dangerous direction. A fresh sibling must refuse, every time.
# --------------------------------------------------------------------------

def test_a_fresh_sibling_refuses(tmp_path, patched):
    patched([_carrier("bravo", "sid-b", 2)], AUTH_OK)
    _write_self(tmp_path, 1)
    val, ev = _measure(tmp_path)
    assert val is False
    assert len(ev["fresh"]) == 1 and ev["fresh"][0]["sid"] == "sid-b"


def test_a_sibling_at_the_window_edge_still_counts_as_fresh(tmp_path, patched):
    # Exactly AT the window is fresh, not stale. The fail-safe direction of the
    # boundary: counting an edge sibling as fresh refuses a legal promotion,
    # counting it as stale promotes into a possibly-live one.
    edge = mod._load_worker_stall  # noqa: F841  (documents the source of truth)
    real_ws = _load("_ws_edge", "worker_stall.py")
    patched([_carrier("bravo", "sid-b", real_ws.DEFAULT_STALE_MINUTES)], AUTH_OK)
    _write_self(tmp_path, 1)
    val, _ = _measure(tmp_path)
    assert val is False


def test_a_sibling_carrier_with_an_unparseable_ts_is_None_not_stale(tmp_path, patched):
    # Unknown freshness is not absence. Treating it as stale would let a
    # corrupt-but-live sibling be promoted over.
    patched([_carrier("bravo", "sid-b", 0, ts="not-a-timestamp")], AUTH_OK)
    _write_self(tmp_path, 1)
    val, ev = _measure(tmp_path)
    assert val is None
    assert ev["unreadable"] == 1
    assert "not absence" in ev["reason"]


def test_many_stale_siblings_do_not_hide_one_fresh_one(tmp_path, patched):
    # The measured population shape: ~71% dead-session residue. The fresh one
    # must still be found among them.
    rows = [_carrier("agent%d" % i, "sid-%d" % i, 60 * 24 * 10) for i in range(17)]
    rows.append(_carrier("zeta", "sid-live", 3))
    patched(rows, AUTH_OK)
    _write_self(tmp_path, 1)
    val, ev = _measure(tmp_path)
    assert val is False
    assert ev["stale"] == 17 and len(ev["fresh"]) == 1


# --------------------------------------------------------------------------
# 2. The D2 dependency. This is the half a flat reading of DISCRIMINATORS misses.
# --------------------------------------------------------------------------

def test_D2_false_makes_D3_None_never_True(tmp_path, patched):
    patched([], AUTH_OK)               # store says: no siblings at all
    _write_self(tmp_path, 1)           # and our own carrier is fresh
    val, ev = _measure(tmp_path, claim_read_authoritative=False)
    assert val is None, "D3 must not be satisfiable while D2 is False"
    assert "D2" in ev["reason"]


def test_a_mirror_read_makes_D3_None_even_when_D2_is_True(tmp_path, patched):
    # The two conditions are checked INDEPENDENTLY: the caller's D2 and this
    # measurement's own read path. A mirror enumeration cannot see a sibling
    # carrier this box never pulled (guard-980).
    patched([], MIRROR)
    _write_self(tmp_path, 1)
    val, ev = _measure(tmp_path, claim_read_authoritative=True)
    assert val is None
    assert "local-mirror" in ev["reason"]


def test_an_incomplete_authoritative_read_is_also_None(tmp_path, patched):
    patched([], {**AUTH_OK, "complete": False, "reason": "partial pagination"})
    _write_self(tmp_path, 1)
    val, _ = _measure(tmp_path)
    assert val is None


def test_D2_unmeasured_does_not_by_itself_block_an_authoritative_read(tmp_path, patched):
    # None (unmeasured) is not False. The enumeration's OWN read path is the
    # independent evidence, and decide() still refuses at G6 because D2 is None
    # -- so this is not a loophole, it is the two conditions staying separate.
    patched([], AUTH_OK)
    _write_self(tmp_path, 1)
    val, _ = _measure(tmp_path, claim_read_authoritative=None)
    assert val is True
    d, _ev = mod.measure_discriminators(
        tmp_path, SELF_AGENT, SELF_SID,
        peers_alive_from_this_box=True, claim_read_authoritative=None, now=NOW)
    r = mod.decide({"enabled": True, "fence_verified_at": "x",
                    "eligible_machines": ["m"]},
                   "m", "wind-down", 4, 99999.0, d, t_takeover_s=3900.0)
    assert r["verdict"] == mod.VERDICT_HOLD
    assert r["gate_failed"] == "discriminators"


# --------------------------------------------------------------------------
# 3. Self is read LOCALLY. The store-only false-by-absence must not recur.
# --------------------------------------------------------------------------

def test_self_fresh_locally_but_absent_from_the_store_still_promotes(tmp_path, patched):
    # THE STORE-ONLY DEFECT. Between the local write and the push, an
    # authoritative read cannot see our own carrier. A store-only D3 would find
    # zero fresh carriers -- including ours -- and report False by absence.
    patched([_carrier("bravo", "sid-b", 60 * 24)], AUTH_OK)   # sibling, stale
    _write_self(tmp_path, 1)                                   # us, fresh, local only
    val, ev = _measure(tmp_path)
    assert val is True
    assert ev["self_seen_in_store"] is False
    assert ev["self_carrier_fresh"] is True


def test_our_own_carrier_in_the_store_is_never_counted_as_a_sibling(tmp_path, patched):
    # Otherwise this Body races itself and D3 can never be True.
    patched([_carrier(SELF_AGENT, SELF_SID, 1)], AUTH_OK)
    _write_self(tmp_path, 1)
    val, ev = _measure(tmp_path)
    assert val is True
    assert ev["self_seen_in_store"] is True
    assert ev["fresh"] == []


def test_a_missing_local_self_carrier_is_False_not_True(tmp_path, patched):
    # "the only fresh carrier is mine" has no subject when we have none.
    patched([], AUTH_OK)
    val, ev = _measure(tmp_path)          # no _write_self
    assert val is False
    assert ev["self_carrier_fresh"] is False


def test_a_stale_local_self_carrier_is_False(tmp_path, patched):
    patched([], AUTH_OK)
    _write_self(tmp_path, 60 * 24 * 3)
    val, _ = _measure(tmp_path)
    assert val is False


# --------------------------------------------------------------------------
# 4. The population is reported. A bare boolean here is unfalsifiable.
# --------------------------------------------------------------------------

def test_evidence_carries_the_scanned_population_on_every_verdict(tmp_path, patched):
    rows = [_carrier("b", "s1", 5), _carrier("c", "s2", 60 * 48)]
    patched(rows, AUTH_OK)
    _write_self(tmp_path, 1)
    for kw in ({}, {"claim_read_authoritative": False}):
        _val, ev = _measure(tmp_path, **kw)
        assert ev["carriers_scanned"] == 2, kw
        assert ev["fresh_minutes"] is not None, kw
        assert ev["read_via"] == "authoritative", kw
        assert ev["reason"], kw


def test_a_true_verdict_states_the_denominator_it_scanned(tmp_path, patched):
    patched([_carrier("b", "s1", 60 * 24)], AUTH_OK)
    _write_self(tmp_path, 1)
    _val, ev = _measure(tmp_path)
    assert "1 scanned" in ev["reason"] and "1 stale" in ev["reason"]


# --------------------------------------------------------------------------
# 5. The reuse contract. No private copy of the carrier read path.
# --------------------------------------------------------------------------

def test_the_enumerator_is_worker_stalls_and_not_a_local_reimplementation():
    src = (_SCRIPTS / "reducer_promotion.py").read_text(encoding="utf-8")
    assert "worker_stall" in src
    # A private sibling glob is the drift this reuse exists to prevent: it would
    # read the local mirror and silently reintroduce the DANGEROUS direction.
    assert "body-heartbeat-*.json" not in src, (
        "a glob over sibling carriers here is a second read path -- reuse "
        "worker_stall.enumerate_carriers")


def test_an_unloadable_enumerator_is_None_not_a_local_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "_load_worker_stall", lambda: None)
    _write_self(tmp_path, 1)
    val, ev = _measure(tmp_path)
    assert val is None
    assert "private copy" in ev["reason"]


def test_the_window_is_worker_stalls_constant_not_a_new_one(tmp_path, patched):
    real_ws = _load("_ws_window", "worker_stall.py")
    patched([], AUTH_OK)
    _write_self(tmp_path, 1)
    _val, ev = _measure(tmp_path)
    assert ev["fresh_minutes"] == float(real_ws.DEFAULT_STALE_MINUTES)


def test_measure_discriminators_passes_D1_and_D2_through_unmeasured(tmp_path, patched):
    # D1 and D2 have other owners (liveness_check / worker_reducer_liveness).
    # Measuring them here would be a second predicate for one question.
    patched([], AUTH_OK)
    _write_self(tmp_path, 1)
    d, ev = mod.measure_discriminators(
        tmp_path, SELF_AGENT, SELF_SID,
        peers_alive_from_this_box=None, claim_read_authoritative=True, now=NOW)
    assert set(d) == set(mod.DISCRIMINATORS)
    assert d["peers_alive_from_this_box"] is None
    assert d["claim_read_authoritative"] is True
    assert d["only_fresh_carrier_is_mine"] is True
    assert "only_fresh_carrier_is_mine" in ev


# --------------------------------------------------------------------------
# 6. The happy path -- last, and the least valuable test here.
# --------------------------------------------------------------------------

def test_a_measured_D3_reaches_decide_and_can_promote(tmp_path, patched):
    patched([_carrier("bravo", "sid-b", 60 * 24 * 4)], AUTH_OK)
    _write_self(tmp_path, 1)
    d, _ev = mod.measure_discriminators(
        tmp_path, SELF_AGENT, SELF_SID,
        peers_alive_from_this_box=True, claim_read_authoritative=True, now=NOW)
    r = mod.decide({"enabled": True, "fence_verified_at": "2026-08-15T00:00:00",
                    "eligible_machines": ["box-a"]},
                   "box-a", "wind-down", 4, 99999.0, d, t_takeover_s=3900.0)
    assert r["verdict"] == mod.VERDICT_PROMOTE, r["reason"]
