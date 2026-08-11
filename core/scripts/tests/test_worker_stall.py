"""Tests for peer-side worker-stall detection ().

The load-bearing case is the NEGATIVE one: a cleanly-finished body leaves a
carrier that is stale forever, and a detector that alerts on staleness alone
fires on it every tick until someone deletes the file. Measured on the live
fleet at build time: 9 carriers, 7 live bodies and 2 dead ones aged 25.4h and
28.7h. `test_stale_without_claim_never_alerts` and
`test_scan_dead_body_is_silent` are that case.
"""
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import worker_stall as ws  # noqa: E402

CORE_SCRIPTS = Path(__file__).resolve().parent.parent


def _enum(rows, complete=True, read_via="authoritative", agents=1, reason=None,
          read_errors=0, first_read_error=None):
    """Build the (rows, meta) pair enumerate_carriers now returns.

    The pair exists because a bare list could mean BOTH "no carriers" and "I
    could not enumerate" -- three routes reached `[]` and only one raised, so
    an exception-scoped fix would have covered 1 of 3 (guard-2521).
    """
    return rows, {
        "read_via": read_via,
        "complete": complete,
        "agents_enumerated": agents,
        "reason": reason,
        "carrier_read_errors": read_errors,
        "first_carrier_read_error": first_read_error,
    }


def _load_watchdog():
    # agent-watchdog.py is hyphenated -- load via importlib for its symbols.
    spec = importlib.util.spec_from_file_location(
        "agent_watchdog", CORE_SCRIPTS / "agent-watchdog.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


WD = _load_watchdog()


# ---------------------------------------------------------------- classify_body

def test_fresh_carrier_is_alive_regardless_of_claim():
    # Freshness alone CLEARS a body -- no claim lookup can override it.
    assert ws.classify_body(0.0, True) == ws.V_ALIVE
    assert ws.classify_body(0.0, False) == ws.V_ALIVE
    assert ws.classify_body(59.9, True) == ws.V_ALIVE


def test_stale_with_live_claim_is_the_alert():
    assert ws.classify_body(60.1, True) == ws.V_STALLED_WITH_CLAIM
    assert ws.classify_body(10_000.0, True) == ws.V_STALLED_WITH_CLAIM


def test_stale_without_claim_never_alerts():
    """THE LOAD-BEARING NEGATIVE. A finished body's carrier is stale forever."""
    for age in (60.1, 120.0, 1524.6, 1724.0, 100_000.0):
        v = ws.classify_body(age, False)
        assert v == ws.V_STALE_NO_CLAIM
        assert not ws.is_alerting(v), f"age={age} must never alert without a claim"


def test_boundary_is_inclusive_at_threshold():
    # At exactly the threshold the body is not YET stale (mirrors the
    # strict-greater boundary the sibling stalled classifier uses).
    assert ws.classify_body(60.0, True) == ws.V_ALIVE
    assert ws.classify_body(60.0000001, True) == ws.V_STALLED_WITH_CLAIM


def test_unreadable_carrier_is_never_a_stall():
    """An instrument fault must not present as the condition being hunted."""
    v = ws.classify_body(None, True)
    assert v == ws.V_UNREADABLE
    assert not ws.is_alerting(v)
    assert not ws.is_alerting(ws.classify_body(None, False))


def test_custom_threshold_is_honoured():
    assert ws.classify_body(30.0, True, stale_minutes=15.0) == ws.V_STALLED_WITH_CLAIM
    assert ws.classify_body(30.0, True, stale_minutes=45.0) == ws.V_ALIVE


def test_is_alerting_is_the_only_escalation_predicate():
    alerting = [v for v in (ws.V_ALIVE, ws.V_STALLED_WITH_CLAIM,
                            ws.V_STALE_NO_CLAIM, ws.V_UNREADABLE)
                if ws.is_alerting(v)]
    assert alerting == [ws.V_STALLED_WITH_CLAIM]


# -------------------------------------------------------------- live_claim_sids

def _write_store(p: Path, goals):
    p.write_text(json.dumps({"id": "asp-999", "goals": goals}) + "\n", encoding="utf-8")


def test_live_claim_sids_excludes_terminal_goals(tmp_path):
    store = tmp_path / "aspirations.jsonl"
    _write_store(store, [
        {"id": "g-1", "status": "in-progress", "claimed_by_sid": "live-sid"},
        {"id": "g-2", "status": "completed", "claimed_by_sid": "done-sid"},
        {"id": "g-3", "status": "skipped", "claimed_by_sid": "skip-sid"},
        {"id": "g-4", "status": "expired", "claimed_by_sid": "exp-sid"},
    ])
    m = ws.live_claim_sids(store)
    assert m == {"live-sid": "g-1"}


def test_live_claim_sids_fails_open_to_empty(tmp_path):
    # Fail-open direction is load-bearing: an empty map can only SUPPRESS
    # alerts, never manufacture one.
    assert ws.live_claim_sids(tmp_path / "nope.jsonl") == {}
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json\n", encoding="utf-8")
    assert ws.live_claim_sids(bad) == {}


def test_live_claim_sids_skips_unclaimed(tmp_path):
    store = tmp_path / "a.jsonl"
    _write_store(store, [{"id": "g-1", "status": "pending"}])
    assert ws.live_claim_sids(store) == {}


# ------------------------------------------------------------------------ scan

def _carrier(agents_root: Path, agent: str, sid: str, host: str, ts: dt.datetime):
    d = agents_root / agent / "session"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"body-heartbeat-{sid}.json").write_text(
        json.dumps({"sid": sid, "agent": agent, "host": host,
                    "ts": ts.strftime("%Y-%m-%dT%H:%M:%S")}),
        encoding="utf-8",
    )


def test_scan_dead_body_is_silent(tmp_path, monkeypatch):
    """A 25h-old carrier with no claim -- the live-measured dead-body shape."""
    monkeypatch.setattr(ws, "enumerate_carriers", lambda root: _enum([
        {"agent": "alpha", "sid": "b18c61fa", "read_via": "authoritative",
         "doc": {"sid": "b18c61fa", "host": "DESKTOP-X",
                 "ts": (dt.datetime(2026, 8, 6, 16, 0)
                        - dt.timedelta(minutes=1524.6)).strftime("%Y-%m-%dT%H:%M:%S")}},
    ]))
    store = tmp_path / "a.jsonl"
    _write_store(store, [{"id": "g-1", "status": "pending"}])
    rep = ws.scan(tmp_path, store, now=dt.datetime(2026, 8, 6, 16, 0))
    assert rep["scanned"] == 1
    assert rep["alerts"] == []
    assert rep["bodies"][0]["verdict"] == ws.V_STALE_NO_CLAIM


def test_scan_stalled_worker_alerts_and_names_the_goal(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "enumerate_carriers", lambda root: _enum([
        {"agent": "foxtrot", "sid": "3ebc753b", "read_via": "authoritative",
         "doc": {"sid": "3ebc753b", "host": "cc-08",
                 "ts": (dt.datetime(2026, 8, 6, 16, 0)
                        - dt.timedelta(minutes=125)).strftime("%Y-%m-%dT%H:%M:%S")}},
    ]))
    store = tmp_path / "a.jsonl"
    _write_store(store, [
        {"id": "g-306-240", "status": "in-progress", "claimed_by_sid": "3ebc753b"},
    ])
    rep = ws.scan(tmp_path, store, now=dt.datetime(2026, 8, 6, 16, 0))
    assert len(rep["alerts"]) == 1
    a = rep["alerts"][0]
    assert a["verdict"] == ws.V_STALLED_WITH_CLAIM
    assert a["held_goal"] == "g-306-240"
    assert a["host"] == "cc-08"
    assert a["carrier_age_minutes"] == pytest.approx(125.0, abs=0.2)


def test_scan_fires_before_the_stranded_claim_grace(tmp_path, monkeypatch):
    """The whole point of the threshold: alert BEFORE the 120m silent release,
    and before the ~89m at which a human noticed in the motivating incident."""
    monkeypatch.setattr(ws, "enumerate_carriers", lambda root: _enum([
        {"agent": "foxtrot", "sid": "s1", "read_via": "authoritative",
         "doc": {"sid": "s1", "host": "cc-08",
                 "ts": (dt.datetime(2026, 8, 6, 16, 0)
                        - dt.timedelta(minutes=61)).strftime("%Y-%m-%dT%H:%M:%S")}},
    ]))
    store = tmp_path / "a.jsonl"
    _write_store(store, [{"id": "g-x", "status": "in-progress", "claimed_by_sid": "s1"}])
    rep = ws.scan(tmp_path, store, now=dt.datetime(2026, 8, 6, 16, 0))
    assert len(rep["alerts"]) == 1
    assert rep["alerts"][0]["carrier_age_minutes"] < 89.0
    assert rep["alerts"][0]["carrier_age_minutes"] < 120.0


def test_scan_reports_degraded_read(tmp_path, monkeypatch):
    """A local-mirror scan must be distinguishable from a complete one -- a
    degraded all-clear is not an all-clear (guard-1760)."""
    monkeypatch.setattr(ws, "enumerate_carriers", lambda root: _enum([
        {"agent": "zeta", "sid": "s", "read_via": "local-mirror",
         "doc": {"sid": "s", "host": "cc-02",
                 "ts": dt.datetime(2026, 8, 6, 16, 0).strftime("%Y-%m-%dT%H:%M:%S")}},
    ]))
    store = tmp_path / "a.jsonl"
    _write_store(store, [])
    rep = ws.scan(tmp_path, store, now=dt.datetime(2026, 8, 6, 16, 0))
    assert rep["degraded_read"] is True


def test_scan_unreadable_carrier_does_not_alert(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "enumerate_carriers", lambda root: _enum([
        {"agent": "zeta", "sid": "s", "read_via": "authoritative", "doc": {"sid": "s"}},
    ]))
    store = tmp_path / "a.jsonl"
    _write_store(store, [{"id": "g", "status": "in-progress", "claimed_by_sid": "s"}])
    rep = ws.scan(tmp_path, store, now=dt.datetime(2026, 8, 6, 16, 0))
    assert rep["alerts"] == []
    assert rep["bodies"][0]["verdict"] == ws.V_UNREADABLE


def test_scan_local_mirror_enumeration_finds_carriers(tmp_path):
    """Exercise the real fallback enumerator, not a monkeypatch.

    The `or rows == []` escape this assertion used to carry is DELETED. It made
    the test pass on exactly the defect the enumerator had -- a silent empty
    result -- so the corpus itself sanctioned the bug (guard-1639: assert the
    collection is non-empty before trusting what you conclude from it). The
    carrier is on disk, so finding it is the only acceptable outcome; if the
    authoritative path is what answers in this env, `complete` is True and the
    row is still there.
    """
    agents = tmp_path / "agents"
    _carrier(agents, "zeta", "sid-a", "cc-02", dt.datetime(2026, 8, 6, 15, 55))
    store = tmp_path / "a.jsonl"
    _write_store(store, [])
    rows, meta = ws.enumerate_carriers(agents)
    assert rows, f"carrier is on disk but enumeration returned empty; meta={meta}"
    assert any(r["sid"] == "sid-a" for r in rows), rows
    assert meta["read_via"] in ("authoritative", "local-mirror"), meta


# ── Fresh-eyes regressions (found reviewing this file's own first version) ───

def test_partial_pagination_failure_does_not_duplicate(tmp_path, monkeypatch):
    """Fail on the SECOND page, not the first.

    The original enumerate_carriers appended authoritative rows into the same
    list the local-mirror fallback then extended, so a mid-pagination failure
    yielded the same sid twice -- once per read path -- and double-counted it in
    alerts. Total-success and total-failure both hide this, which is exactly
    what the first test pass exercised.
    """
    agents = tmp_path / "agents"
    _carrier(agents, "zeta", "sid-a", "cc-02", dt.datetime(2026, 8, 6, 15, 55))

    class _Pag:
        def paginate(self, **kw):
            yield {"Contents": [{"Key": "e/agents/zeta/session/body-heartbeat-sid-a.json"}]}
            raise RuntimeError("throttled on page 2")

    class _S3:
        def list_objects_v2(self, **kw):
            return {"CommonPrefixes": [{"Prefix": "e/agents/zeta/"}]}

        def get_paginator(self, _n):
            return _Pag()

        def get_object(self, **kw):
            raise RuntimeError("unused")

    class _B:
        bucket = "b"
        s3 = _S3()

        def _s3_key(self, p):
            return "e/agents/_probe/session/body-heartbeat-x.json"

    import types
    monkeypatch.setitem(
        sys.modules, "storage_backend",
        types.SimpleNamespace(get_backend=lambda: _B()),
    )
    rows, meta = ws.enumerate_carriers(agents)
    sids = [r["sid"] for r in rows]
    assert len(sids) == len(set(sids)), f"duplicate carriers after partial failure: {sids}"
    assert all(r["read_via"] == "local-mirror" for r in rows), rows
    # A mirror answer is never `complete` -- it is a read-through cache, so it
    # cannot see a carrier this box never pulled (guard-980).
    assert meta["read_via"] == "local-mirror"
    assert meta["complete"] is False, meta


def test_read_claims_reports_provenance(tmp_path):
    """The condemning half of the join must say which layer produced it.

    A stale local store is fully readable and confidently wrong, so 'I got a
    claim map' is not the same as 'I got the current claim map' (guard-1753).
    """
    store = tmp_path / "a.jsonl"
    _write_store(store, [{"id": "g", "status": "in-progress", "claimed_by_sid": "s"}])
    claims, via = ws.read_claims(store)
    assert claims == {"s": "g"}
    assert via in ("authoritative", "local-mirror"), via
    empty, via2 = ws.read_claims(tmp_path / "missing.jsonl")
    assert empty == {}
    assert via2 in ("authoritative", "none"), via2


def test_read_claims_distinguishes_empty_from_unreadable(tmp_path, monkeypatch):
    """An empty map is an ANSWER; provenance must not be keyed off truthiness.

    The first version wrote `"local-mirror" if local else "none"`, so a store
    that was read fine and simply holds no live claims reported the SAME
    `"none"` as a store that could not be opened at all. That defeats the one
    question the field exists to answer (guard-1753). Force the authoritative
    leg to fail so both cases take the local path, which is where they were
    indistinguishable.
    """
    import types
    monkeypatch.setitem(
        sys.modules, "storage_backend",
        types.SimpleNamespace(
            get_backend=lambda: (_ for _ in ()).throw(RuntimeError("backend down"))),
    )
    readable_but_empty = tmp_path / "empty.jsonl"
    _write_store(readable_but_empty, [{"id": "g-1", "status": "pending"}])
    claims_a, via_a = ws.read_claims(readable_but_empty)
    claims_b, via_b = ws.read_claims(tmp_path / "does-not-exist.jsonl")

    assert claims_a == {} and claims_b == {}, (claims_a, claims_b)
    assert via_a == "local-mirror", f"a readable store is an answer, got {via_a!r}"
    assert via_b == "none", f"an unreadable store is not, got {via_b!r}"
    assert via_a != via_b, "empty and unreadable must be distinguishable"


def _force_local_leg(monkeypatch):
    """Make read_claims take the local path, as the sibling tests above do."""
    import types
    monkeypatch.setitem(
        sys.modules, "storage_backend",
        types.SimpleNamespace(
            get_backend=lambda: (_ for _ in ()).throw(RuntimeError("backend down"))),
    )


def test_read_claims_union_sees_an_agent_queue_only_claim(tmp_path, monkeypatch):
    """: a SID whose ONLY claim is agent-queue-side still holds a claim.

    This is the whole point of the union. `body_row_reaper` reaps on exactly one
    condition — `holds_live_claim == False` — so a claim the map cannot see is a
    claim that does not protect its Body. Reading the world queue alone made an
    agent-queue-only claim indistinguishable from no claim at all, and the reap
    that follows orphans in-flight work rather than cleaning up after it.
    """
    _force_local_leg(monkeypatch)
    world = tmp_path / "world.jsonl"
    agent = tmp_path / "agent.jsonl"
    _write_store(world, [{"id": "g-1", "status": "in-progress", "claimed_by_sid": "w"}])
    _write_store(agent, [{"id": "g-2", "status": "pending", "claimed_by_sid": "a"}])

    world_only, _ = ws.read_claims(world)
    assert "a" not in world_only, "premise: the world queue cannot see this claim"

    merged, via = ws.read_claims_union(world, agent)
    assert merged == {"w": "g-1", "a": "g-2"}, merged
    assert via == "local-mirror", via

    # And the direction that matters: unioning can only ADD claims, so it can
    # only turn a reap into a keep. Every world-visible claim survives.
    assert set(world_only).issubset(merged), (world_only, merged)


def test_read_claims_union_reports_the_weakest_provenance(tmp_path, monkeypatch):
    """One readable half must never launder an unreadable one.

    The caller declines the reap on `provenance == "none"`, so reporting the
    better half here would hand it a partial map wearing a good-read label —
    which is the guard-1753 masquerade, and would make the union hollow exactly
    when a store is down.
    """
    _force_local_leg(monkeypatch)
    world = tmp_path / "world.jsonl"
    _write_store(world, [{"id": "g-1", "status": "in-progress", "claimed_by_sid": "w"}])

    merged, via = ws.read_claims_union(world, tmp_path / "does-not-exist.jsonl")
    assert merged == {"w": "g-1"}, merged
    assert via == "none", f"an unread half must degrade the verdict, got {via!r}"

    _, via2 = ws.read_claims_union(world, world)
    assert via2 == "local-mirror", via2
    assert ws.read_claims_union()[1] == "none", "no stores answered is not a good read"


def test_read_claims_union_normalises_an_unrecognised_provenance(tmp_path, monkeypatch):
    """An unknown provenance must NORMALISE to "none", not pass through.

    Found by the fresh-eyes pass on the commit that added the union. Ranking an
    unknown value lowest is only half the job: the caller declines on the VALUE
    (`via == "none"` in stranded-claim-sweep), so propagating the raw string
    would rank it weakest AND clear the decline test — the ranking and the
    predicate disagreeing about what "untrusted" means. Unreachable today
    (read_claims returns three literals) and pinned anyway, because the two
    halves live in different files and only this test couples them.
    """
    world = tmp_path / "world.jsonl"
    _write_store(world, [{"id": "g-1", "status": "in-progress", "claimed_by_sid": "w"}])
    monkeypatch.setattr(ws, "read_claims", lambda p: ({"w": "g-1"}, "some-future-layer"))

    merged, via = ws.read_claims_union(world)
    assert merged == {"w": "g-1"}, "claims still merge — only the label is normalised"
    assert via == "none", f"an unrecognised provenance must not be trusted, got {via!r}"

    # And the ranking half still holds: an unknown half degrades a good one.
    seen = iter(["authoritative", "some-future-layer"])
    monkeypatch.setattr(ws, "read_claims", lambda p: ({}, next(seen)))
    assert ws.read_claims_union(world, world)[1] == "none"


def test_scan_marks_degraded_when_claims_are_not_authoritative(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "enumerate_carriers", lambda root: _enum([
        {"agent": "zeta", "sid": "s", "read_via": "authoritative",
         "doc": {"sid": "s", "ts": "2026-08-06T15:59:00"}},
    ]))
    monkeypatch.setattr(ws, "read_claims", lambda p: ({}, "local-mirror"))
    rep = ws.scan(tmp_path, tmp_path / "a.jsonl", now=dt.datetime(2026, 8, 6, 16, 0))
    assert rep["claims_read_via"] == "local-mirror"
    assert rep["degraded_read"] is True


# ── Fresh-eyes round 2: the silent all-clear () ────────────────────
# guard-2521: the defect was "a wrong VALUE reaches the caller", so these are
# parametrized per ROUTE to that value, not per fix. THREE routes reached a
# bare `[]` and only ONE of them raised, so widening the fallback's exception
# trigger -- the obvious fix -- would have covered 1 of 3.

def _backend(monkeypatch, s3_obj):
    import types
    monkeypatch.setitem(
        sys.modules, "storage_backend",
        types.SimpleNamespace(get_backend=lambda: types.SimpleNamespace(
            bucket="b", s3=s3_obj,
            _s3_key=lambda p: "e/agents/_probe/session/body-heartbeat-x.json")),
    )


class _OkPag:
    def paginate(self, **kw):
        return iter(())


@pytest.mark.parametrize("route", ["empty_roster", "bad_probe_key", "mirror_empty"])
def test_no_route_returns_a_bare_empty_claiming_completeness(route, tmp_path, monkeypatch):
    """Every route to `[]` must state that it could not answer.

    Production shape matters here: real boto3 ALWAYS returns a paginator from
    get_paginator, so a mock that raises there triggers the fallback and gives
    a FALSE CLEAR. That is how the first probe of this defect exonerated it
    (rb-5235 -- canonical INVOCATION, not just canonical binary).
    """
    agents = tmp_path / "agents"
    if route == "mirror_empty":
        agents.mkdir(parents=True)          # exists, but holds no agent dirs
    else:
        (agents / "zeta" / "session").mkdir(parents=True)

    import types
    if route == "bad_probe_key":
        monkeypatch.setitem(
            sys.modules, "storage_backend",
            types.SimpleNamespace(get_backend=lambda: types.SimpleNamespace(
                bucket="b", s3=types.SimpleNamespace(),
                _s3_key=lambda p: "no-probe-segment-here.json")),
        )
    else:
        _backend(monkeypatch, types.SimpleNamespace(
            list_objects_v2=lambda **kw: {"IsTruncated": False, "KeyCount": 0},
            get_paginator=lambda _n: _OkPag(),
            get_object=lambda **kw: (_ for _ in ()).throw(AssertionError("unreached")),
        ))

    rows, meta = ws.enumerate_carriers(agents)
    assert rows == [], f"route {route} unexpectedly found carriers: {rows}"
    assert meta["complete"] is False, (
        f"route {route} returned an EMPTY list marked complete -- that is the "
        f"confident all-clear this fix exists to remove; meta={meta}")
    assert meta["reason"], f"route {route} gave no reason for an unanswerable scan"


def test_authoritative_roster_with_no_carriers_is_a_real_answer(tmp_path, monkeypatch):
    """The other side of the ranking (guard-1686): do NOT fall through on a
    merely-empty result. A fleet with zero live bodies is a legitimate empty,
    and treating it as failure would make `complete` meaningless."""
    import types
    agents = tmp_path / "agents"
    (agents / "zeta" / "session").mkdir(parents=True)
    _backend(monkeypatch, types.SimpleNamespace(
        list_objects_v2=lambda **kw: {"CommonPrefixes": [{"Prefix": "e/agents/zeta/"}]},
        get_paginator=lambda _n: _OkPag(),
        get_object=lambda **kw: (_ for _ in ()).throw(AssertionError("unreached")),
    ))
    rows, meta = ws.enumerate_carriers(agents)
    assert rows == []
    assert meta["complete"] is True, meta
    assert meta["read_via"] == "authoritative"
    assert meta["agents_enumerated"] == 1


def test_scan_reports_unanswerable_enumeration_as_degraded(tmp_path, monkeypatch):
    """A zero scan must never read as healthy when nothing could be enumerated."""
    monkeypatch.setattr(ws, "enumerate_carriers",
                        lambda root: ([], {"read_via": "none", "complete": False,
                                           "agents_enumerated": 0, "reason": "prefix wrong"}))
    monkeypatch.setattr(ws, "read_claims", lambda p: ({}, "authoritative"))
    rep = ws.scan(tmp_path, tmp_path / "a.jsonl", now=dt.datetime(2026, 8, 6, 16, 0))
    assert rep["scanned"] == 0 and rep["alerts"] == []
    assert rep["degraded_read"] is True, (
        "scanned=0 with an unanswerable enumeration reported as a clean scan")
    assert rep["enumeration"]["complete"] is False


@pytest.mark.parametrize("stamp", ["2026-08-06T14:00:00Z", "2026-08-06T14:00:00z",
                                   "2026-08-06T14:00:00+00:00", "2026-08-06T14:00:00"])
def test_parse_iso_never_returns_an_aware_datetime(stamp):
    """An aware datetime raises TypeError against a naive `now`, and that raise
    escaped the WHOLE scan. Fixed at the value (guard-2521), so no caller can
    receive one regardless of the offset form."""
    parsed = ws._parse_iso(stamp)
    assert parsed is not None, stamp
    assert parsed.tzinfo is None, f"{stamp!r} produced an aware datetime"
    (dt.datetime(2026, 8, 6, 16, 0) - parsed)   # must not raise


def test_one_malformed_carrier_does_not_silence_the_whole_scan(tmp_path, monkeypatch):
    """The blast-radius regression: one bad row must cost one row, not the fleet.

    Before the per-row guard, a single carrier raising took out every other
    body's verdict -- measured with 2 genuinely-stalled bodies producing ZERO
    events. The evidence must survive too (guard-1893): count the drop, keep
    the first error.
    """
    class _Boom:
        # A doc whose .get() raises -- stands in for any malformed shape,
        # without depending on _parse_iso still being the thing that breaks.
        def get(self, *a, **k):
            raise TypeError("malformed carrier doc")

    monkeypatch.setattr(ws, "enumerate_carriers", lambda root: _enum([
        {"agent": "zeta", "sid": "good-1", "read_via": "authoritative",
         "doc": {"ts": (dt.datetime(2026, 8, 6, 16, 0)
                        - dt.timedelta(minutes=200)).strftime("%Y-%m-%dT%H:%M:%S")}},
        {"agent": "foxtrot", "sid": "bad-row", "read_via": "authoritative",
         "doc": _Boom()},
        {"agent": "echo", "sid": "good-2", "read_via": "authoritative",
         "doc": {"ts": (dt.datetime(2026, 8, 6, 16, 0)
                        - dt.timedelta(minutes=300)).strftime("%Y-%m-%dT%H:%M:%S")}},
    ], agents=3))
    store = tmp_path / "a.jsonl"
    _write_store(store, [
        {"id": "g-A", "status": "in-progress", "claimed_by_sid": "good-1"},
        {"id": "g-C", "status": "in-progress", "claimed_by_sid": "good-2"},
    ])
    rep = ws.scan(tmp_path, store, now=dt.datetime(2026, 8, 6, 16, 0))

    assert rep["scanned"] == 2, "the two good rows must still be scanned"
    assert len(rep["alerts"]) == 2, f"both stalled bodies must alert: {rep['alerts']}"
    assert {a["held_goal"] for a in rep["alerts"]} == {"g-A", "g-C"}
    # The evidence survived rather than being swallowed.
    assert rep["rows_dropped"] == 1
    assert "TypeError" in (rep["first_drop_error"] or ""), rep["first_drop_error"]
    assert rep["degraded_read"] is True


def test_scan_flags_the_asymmetry_when_every_row_is_lost(tmp_path, monkeypatch):
    """guard-1893's detectable invariant: non-empty IN, empty OUT. An empty
    fleet and a fleet whose every row raised produce the same `bodies` list
    and must not read the same."""
    class _Boom:
        def get(self, *a, **k):
            raise ValueError("bad")

    monkeypatch.setattr(ws, "enumerate_carriers", lambda root: _enum([
        {"agent": "zeta", "sid": "x", "read_via": "authoritative", "doc": _Boom()},
    ]))
    monkeypatch.setattr(ws, "read_claims", lambda p: ({}, "authoritative"))
    rep = ws.scan(tmp_path, tmp_path / "a.jsonl", now=dt.datetime(2026, 8, 6, 16, 0))
    assert rep["carriers_found"] == 1 and rep["scanned"] == 0
    assert rep["enumeration_lost_everything"] is True
    assert rep["degraded_read"] is True


def test_probe_emits_a_blind_event_rather_than_going_silent(tmp_path, monkeypatch):
    """The caller half of guard-1893, and guard-1977: a diagnostic added to end
    a silent failure becomes the next silent layer unless its OWN failure mode
    is loud. A blind scan previously emitted zero events -- byte-identical to
    'all bodies healthy'."""
    probe = WD.WorkerStallProbe(WD.WatchdogContext(
        agent_name="zeta", agent_dir=tmp_path / "agents" / "zeta",
        project_root_path=tmp_path))
    probe.initialize()

    blind = {"bodies": [], "alerts": [], "scanned": 0, "carriers_found": 0,
             "rows_dropped": 0, "first_drop_error": None,
             "enumeration_lost_everything": False, "claims_read_via": "authoritative",
             "stale_minutes": 60.0, "degraded_read": True,
             "enumeration": {"read_via": "none", "complete": False,
                             "agents_enumerated": 0, "reason": "prefix wrong"}}
    monkeypatch.setattr(WD, "scan", lambda *a, **k: blind, raising=False)
    import worker_stall as _ws
    monkeypatch.setattr(_ws, "scan", lambda *a, **k: blind)

    names = [e.event for e in probe.check()]
    assert "worker_stall_probe_blind" in names, f"blind scan went silent: {names}"

    # Edge-triggered, not level-triggered: once per episode, not every tick.
    # Written as a plain `not in` rather than an `or`-chain -- an `or` here can
    # pass vacuously, which is the same shape as the `or rows == []` sanction
    # this goal deleted from test_scan_local_mirror_enumeration_finds_carriers.
    second = [e.event for e in probe.check()]
    assert "worker_stall_probe_blind" not in second, f"re-fired on tick 2: {second}"

    # And it must CLEAR: a blind probe that can never go healthy again is a
    # stuck alarm, which trains the reader to ignore it.
    healthy = dict(blind, degraded_read=False, enumeration={
        "read_via": "authoritative", "complete": True,
        "agents_enumerated": 3, "reason": None})
    monkeypatch.setattr(_ws, "scan", lambda *a, **k: healthy)
    assert "worker_stall_probe_blind_cleared" in [e.event for e in probe.check()]


# ── Unreadable carriers () ─────────────────────────────────────────
# The swallow at the per-object GET (`except Exception: doc = {}`) turned an
# unfetchable carrier into an `unreadable` VERDICT that no completeness field
# could see: the ROSTER listing succeeds, so `complete` stays True; nothing
# raises, so `rows_dropped` is 0; the rows survive into `bodies`, so
# `enumeration_lost_everything` is False. A fleet where NOT ONE carrier could
# be read therefore reported `alerts: [], degraded_read: false` -- a confident
# all-clear from a probe that had seen nothing.


def _pin_claims(monkeypatch, claims=None):
    """Hold the CLAIMS leg at authoritative so `degraded_read` reflects only the
    carrier terms. Without this the assertions below pass for the wrong reason:
    a tmp store is not in the object store, so `read_claims` falls back to the
    mirror and `claims_via != 'authoritative'` flips `degraded_read` on its own
    (measured while building these tests -- it made a one-bad-carrier scan read
    True and would have masked the outcome-3 case entirely)."""
    monkeypatch.setattr(ws, "read_claims", lambda p: (claims or {}, "authoritative"))


def _blank(sid, agent="a"):
    """A carrier whose GET failed: the swallow leaves an empty doc behind."""
    return {"agent": agent, "sid": sid, "read_via": "authoritative", "doc": {}}


def _live(sid, now, age_minutes, agent="a"):
    return {"agent": agent, "sid": sid, "read_via": "authoritative",
            "doc": {"host": "h",
                    "ts": (now - dt.timedelta(minutes=age_minutes)
                           ).strftime("%Y-%m-%dT%H:%M:%S")}}


def test_all_unreadable_carriers_are_not_a_clean_scan(tmp_path, monkeypatch):
    """Outcome 1. Every OTHER degradation term is deliberately held clean here --
    complete=True, dropped=0, lost_everything=False, claims authoritative, no
    mirror row -- so the flag can only be carried by the all-unreadable term.
    A test that let another term fire would pass without the fix."""
    _pin_claims(monkeypatch)
    now = dt.datetime(2026, 8, 6, 16, 0)
    monkeypatch.setattr(ws, "enumerate_carriers", lambda root: _enum(
        [_blank("s1"), _blank("s2"), _blank("s3")],
        read_errors=3, first_read_error="ClientError: AccessDenied"))
    store = tmp_path / "a.jsonl"
    _write_store(store, [])
    rep = ws.scan(tmp_path, store, now=now)

    assert rep["all_carriers_unreadable"] is True
    assert rep["degraded_read"] is True, rep
    # The pre-fix shape, pinned so a future refactor cannot quietly restore it:
    # these three read exactly as they did when the bug shipped.
    assert rep["enumeration"]["complete"] is True
    assert rep["rows_dropped"] == 0
    assert rep["enumeration_lost_everything"] is False
    # And the rows are still REPORTED -- suppressing them would trade one silent
    # failure for another.
    assert rep["scanned"] == 3
    assert all(b["verdict"] == ws.V_UNREADABLE for b in rep["bodies"])


def test_one_unreadable_carrier_does_not_void_the_scan(tmp_path, monkeypatch):
    """Outcome 3. Two healthy bodies still bound the fleet, so one dead carrier
    must not discount the whole report -- a finished body's carrier can be
    unreadable, and a flag that fires on the common case stops being read.

    The `carrier_read_errors == 1` assertion is the POSITIVE CONTROL (guard-2791):
    without it, `degraded_read is False` would pass identically if the counter
    were dead, which is the outcome most likely to be built on."""
    _pin_claims(monkeypatch)
    now = dt.datetime(2026, 8, 6, 16, 0)
    monkeypatch.setattr(ws, "enumerate_carriers", lambda root: _enum(
        [_live("s1", now, 5), _live("s2", now, 5), _blank("s3")],
        read_errors=1, first_read_error="ClientError: SlowDown"))
    store = tmp_path / "a.jsonl"
    _write_store(store, [])
    rep = ws.scan(tmp_path, store, now=now)

    assert rep["carrier_read_errors"] == 1, "counter is dead -- the False below proves nothing"
    assert rep["all_carriers_unreadable"] is False
    assert rep["degraded_read"] is False, rep


def test_unreadable_by_corrupt_timestamp_voids_with_zero_read_errors(tmp_path, monkeypatch):
    """guard-345: the same symptom via a route with NO read failure at all. The
    GET succeeded, the JSON parsed, and `ts` is junk -- `_parse_iso` returns None
    and the verdict is `unreadable` all the same. This is why the asymmetry is
    keyed on the VERDICT and not on `carrier_read_errors`; a counter-keyed fix
    would leave this route (and the missing-`ts` route) exactly as silent as
    before."""
    _pin_claims(monkeypatch)
    now = dt.datetime(2026, 8, 6, 16, 0)
    monkeypatch.setattr(ws, "enumerate_carriers", lambda root: _enum(
        [{"agent": "a", "sid": "s1", "read_via": "authoritative",
          "doc": {"host": "h", "ts": "not-a-date"}}]))
    store = tmp_path / "a.jsonl"
    _write_store(store, [])
    rep = ws.scan(tmp_path, store, now=now)

    assert rep["carrier_read_errors"] == 0, "no GET failed -- the counter must not claim one"
    assert rep["all_carriers_unreadable"] is True
    assert rep["degraded_read"] is True


def test_empty_fleet_is_not_reported_as_unreadable(tmp_path, monkeypatch):
    """The `bool(bodies)` guard. An empty fleet is a real answer, and flagging it
    would make the fix a false-alarm generator -- the mirror of the bug."""
    _pin_claims(monkeypatch)
    monkeypatch.setattr(ws, "enumerate_carriers", lambda root: _enum([]))
    store = tmp_path / "a.jsonl"
    _write_store(store, [])
    rep = ws.scan(tmp_path, store, now=dt.datetime(2026, 8, 6, 16, 0))
    assert rep["all_carriers_unreadable"] is False
    assert rep["degraded_read"] is False, rep


def test_read_error_count_surfaces_in_both_meta_and_scan(tmp_path, monkeypatch):
    """Outcome 2, both halves. A caller reading only the summary must still see
    the failure; a caller reading the enumeration meta must too."""
    _pin_claims(monkeypatch)
    now = dt.datetime(2026, 8, 6, 16, 0)
    monkeypatch.setattr(ws, "enumerate_carriers", lambda root: _enum(
        [_live("s1", now, 5), _blank("s2")],
        read_errors=1, first_read_error="EndpointConnectionError: could not connect"))
    store = tmp_path / "a.jsonl"
    _write_store(store, [])
    rep = ws.scan(tmp_path, store, now=now)

    assert rep["enumeration"]["carrier_read_errors"] == 1
    assert "EndpointConnectionError" in rep["enumeration"]["first_carrier_read_error"]
    assert rep["carrier_read_errors"] == 1
    assert "EndpointConnectionError" in rep["first_carrier_read_error"]


def test_mirror_enumeration_counts_an_unreadable_carrier_file(tmp_path):
    """Exercise the REAL enumerator against the second swallow site, not a
    monkeypatch. A carrier file that is not valid JSON is the on-disk twin of a
    failed GET, and it was swallowed identically."""
    agents = tmp_path / "agents"
    _carrier(agents, "zeta", "good", "cc-02", dt.datetime(2026, 8, 6, 15, 55))
    bad = agents / "zeta" / "session" / "body-heartbeat-corrupt.json"
    bad.write_text("{not json at all", encoding="utf-8")

    rows, meta = ws.enumerate_carriers(agents)
    assert rows, f"carriers are on disk but enumeration returned empty; meta={meta}"
    if meta["read_via"] == "local-mirror":
        assert meta["carrier_read_errors"] >= 1, meta
        assert meta["first_carrier_read_error"], meta
    # Either path must expose the keys -- a caller cannot branch on read_via.
    assert "carrier_read_errors" in meta and "first_carrier_read_error" in meta


def test_probe_goes_blind_when_every_carrier_is_unreadable(tmp_path, monkeypatch):
    """The consumer half. The watchdog re-derives its own `blind` predicate
    rather than reading `degraded_read`, so it missed this case for the identical
    reason -- and a probe that emits no events on a fleet it cannot see is the
    silent all-clear one layer up."""
    probe = WD.WorkerStallProbe(WD.WatchdogContext(
        agent_name="zeta", agent_dir=tmp_path / "agents" / "zeta",
        project_root_path=tmp_path))
    probe.initialize()

    unreadable = {
        "bodies": [{"agent": "a", "sid": "s1", "host": None,
                    "carrier_age_minutes": None, "held_goal": None,
                    "verdict": ws.V_UNREADABLE, "read_via": "authoritative"}],
        "alerts": [], "scanned": 1, "carriers_found": 1,
        "rows_dropped": 0, "first_drop_error": None,
        "enumeration_lost_everything": False, "claims_read_via": "authoritative",
        "stale_minutes": 60.0, "degraded_read": True,
        "carrier_read_errors": 1,
        "first_carrier_read_error": "ClientError: AccessDenied",
        "all_carriers_unreadable": True,
        # complete=True is the point: every field the probe checked BEFORE the
        # fix reads clean here.
        "enumeration": {"read_via": "authoritative", "complete": True,
                        "agents_enumerated": 1, "reason": None,
                        "carrier_read_errors": 1,
                        "first_carrier_read_error": "ClientError: AccessDenied"},
    }
    import worker_stall as _ws
    monkeypatch.setattr(_ws, "scan", lambda *a, **k: unreadable)

    events = probe.check()
    names = [e.event for e in events]
    assert "worker_stall_probe_blind" in names, f"unreadable fleet went silent: {names}"
    # And the diagnostic must NAME its cause. Before the fix the two-term
    # fallback chain rendered it as the literal "None" here, because the roster
    # listing succeeded (reason=None) and nothing raised (first_drop_error=None).
    blind_ev = [e for e in events if e.event == "worker_stall_probe_blind"][0]
    assert "None)" not in blind_ev.summary, blind_ev.summary
    assert "AccessDenied" in blind_ev.summary, blind_ev.summary


# ── Registration ────────────────────────────────────────────────────────────
# The whole point of this goal is that a probe can be well-written and never
# execute. A probe absent from build_probes() is exactly that failure in its
# purest form (the orphan-sweep class), so it gets the same pin every sibling
# probe carries: test_clock_skew_probe, test_agent_watchdog_stalled and
# test_agent_watchdog_memory each assert their own registration.

def test_probe_is_registered(tmp_path):
    """A probe absent from build_probes never runs (the orphan-sweep class)."""
    probes = WD.build_probes(
        WD.WatchdogContext(
            agent_name="delta",
            agent_dir=tmp_path / "agents" / "delta",
            project_root_path=tmp_path,
        )
    )
    assert any(isinstance(x, WD.WorkerStallProbe) for x in probes)
    assert "worker-stall" in [x.name for x in probes]
