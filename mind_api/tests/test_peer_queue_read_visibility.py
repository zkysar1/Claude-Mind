"""A cross-agent READ must not fail silently ( item 2).

The write path already refuses: `OwnCloudBackend._put` consults the ownership
SSOT and raises `no_claim` before touching an agent dir this box does not own.
The read path answered HTTP 200 from a mirror the framework already knew was
structurally behind — measured 2026-09-06/07, a goal zeta had CLOSED came back
`status=pending` with an empty outcome_note 6h48m later, which reads exactly
like the owner losing a write (guard-6156, guard-6166).

THE TWO SHAPES ARE TESTED SEPARATELY BECAUSE THEY FAIL DIFFERENTLY:

  Part A  the helper's verdict   — is the claim TRUE?
  Part B  the query wiring       — does it reach the rows, and the ZERO?
  Part C  the read wiring        — is a local absence distinguishable?

guard-5501 governs the shape throughout: a diagnostic's silence is not evidence
until you have proved the diagnostic can FIRE. Every assertion that something is
absent is paired, in the same file, with an INDUCED case proving the same code
path emits it — and every assertion that it fires is paired with a negative
control proving the emit is not unconditional. Under the suite's pinned
STORAGE_BACKEND=local (guard-955) the real ownership consult reports provenance
"local-backend", which fails open to silence — so without induced positives this
whole file would pass against a completely inert change.
"""
from __future__ import annotations

import json

import pytest

from mind_api.src import peer_queue_read as pqr
from mind_api.src.endpoints import aspirations as asp_read_ep
from mind_api.src.endpoints import aspirations_query as query_ep


# --- fixtures ---------------------------------------------------------------

class _Paths:
    def __init__(self, world, agent):
        self.world = world
        self.agent = agent


class _Ctx:
    def __init__(self, world, agent, query=None, headers=None):
        self.paths = _Paths(world, agent)
        self.query = query or {}
        self.headers = headers or {"x-mind-agent": "zeta"}


def _seed(base, records):
    base.mkdir(parents=True, exist_ok=True)
    (base / "aspirations.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return base


GOAL_W = {"id": "g-001-01", "title": "world goal", "status": "pending",
          "category": "framework"}
GOAL_A = {"id": "g-001-109", "title": "peer goal", "status": "pending",
          "category": "framework"}


@pytest.fixture
def ctx(tmp_path):
    """World + agent queues, each with exactly one goal."""
    world = _seed(tmp_path / "world",
                  [{"id": "asp-001", "title": "W", "goals": [GOAL_W]}])
    agent = _seed(tmp_path / "agents" / "zeta",
                  [{"id": "asp-100", "title": "A", "goals": [GOAL_A]}])
    return _Ctx(world, agent)


@pytest.fixture(autouse=True)
def _clean_verdict_cache():
    """The verdict is memoized process-wide; a leaked entry would make a later
    test pass for the previous test's reason."""
    pqr.reset_cache()
    yield
    pqr.reset_cache()


def _own(monkeypatch, owned, provenance="live-claims"):
    monkeypatch.setattr(pqr, "_owned_now",
                        lambda: (frozenset(owned), provenance))


def _name(monkeypatch, name):
    monkeypatch.setattr(pqr, "agent_name_for", lambda _p: name)


def _body(resp):
    """The response payload as text, whichever constructor produced it."""
    b = getattr(resp, "body", None)
    if isinstance(b, bytes):
        return b.decode("utf-8")
    if isinstance(b, str):
        return b
    return json.dumps(getattr(resp, "payload", ""))


# --- Part A — the helper's verdict ------------------------------------------

def test_positive_control_an_unowned_agent_dir_is_unverified(monkeypatch):
    """INDUCED FIRING. Without this the rest of the file proves nothing."""
    _own(monkeypatch, set())          # this box owns nothing — a worker Body
    _name(monkeypatch, "zeta")
    assert pqr.agent_queue_unverified("/anything/agents/zeta/x.jsonl") == "zeta"


def test_negative_control_an_owned_agent_dir_is_silent(monkeypatch):
    """The emit is NOT unconditional: owning the dir returns None."""
    _own(monkeypatch, {"zeta", "alpha"})
    _name(monkeypatch, "zeta")
    assert pqr.agent_queue_unverified("/anything/agents/zeta/x.jsonl") is None


@pytest.mark.parametrize("provenance",
                         ["local-backend", "transient-error", "unknown-machine", ""])
def test_only_live_claims_provenance_may_accuse(monkeypatch, provenance):
    """FAIL-OPEN, and this is the load-bearing half (guard-1562).

    `owned` is EMPTY in every case here, so a verdict keyed on membership alone
    would accuse on all four. The write gate states the same carve-out for
    itself: on these this box may in fact own the dir and merely failed to prove
    it, and asserting a structural impossibility there would be the same
    confident-and-wrong error the fix removes. "local-backend" is also the
    single-agent case this goal's third check protects — a local box must see no
    behaviour change at all.
    """
    _own(monkeypatch, set(), provenance)
    _name(monkeypatch, "zeta")
    assert pqr.agent_queue_unverified("/anything/agents/zeta/x.jsonl") is None


def test_a_raising_consult_is_silent_not_fatal_at_the_boundary(monkeypatch):
    """A read must never fail because the disclosure could not be computed.

    Pinned at the PUBLIC boundary because that is what the endpoints call. This
    test failed on the first run against a version whose fail-open lived only
    inside `_owned_now`, and the code was fixed rather than the test.
    """
    def _boom():
        raise RuntimeError("ownership store unreachable")
    monkeypatch.setattr(pqr, "_owned_now", _boom)
    _name(monkeypatch, "zeta")
    with pytest.raises(RuntimeError):
        pqr._owned_now()                      # the patch really does raise
    # ...and the helper still answers rather than propagating it.
    assert pqr.agent_queue_unverified("/anything/agents/zeta/x.jsonl") is None


def test_a_raising_ownership_store_is_absorbed_by_the_inner_layer(monkeypatch):
    """The other half: the REAL consult failing (an own-cloud outage, a missing
    module) must resolve to silence, not to an exception."""
    import owncloud_sync

    def _boom():
        raise RuntimeError("claims object unreachable")
    monkeypatch.setattr(owncloud_sync, "_owned_agents_with_provenance", _boom)
    _name(monkeypatch, "zeta")
    assert pqr._owned_now() == (frozenset(), "")
    assert pqr.agent_queue_unverified("/anything/agents/zeta/x.jsonl") is None


def test_a_path_outside_any_agent_dir_never_accuses(monkeypatch, tmp_path):
    """The world queue is not agent-keyed; it must not be stamped."""
    import _paths
    monkeypatch.setattr(_paths, "agents_root", lambda: str(tmp_path / "agents"))
    _own(monkeypatch, set())
    assert pqr.agent_name_for(tmp_path / "world" / "aspirations.jsonl") is None
    assert pqr.agent_queue_unverified(tmp_path / "world" / "aspirations.jsonl") is None


def test_agent_name_is_resolved_from_the_real_agents_root(monkeypatch, tmp_path):
    """Exercises the real resolution rather than the patched shortcut the
    wiring tests use — otherwise nothing covers it (guard-1943)."""
    import _paths
    monkeypatch.setattr(_paths, "agents_root", lambda: str(tmp_path / "agents"))
    p = tmp_path / "agents" / "zeta" / "aspirations.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text("", encoding="utf-8")
    assert pqr.agent_name_for(p) == "zeta"


def test_the_verdict_is_cached_so_the_cost_is_paid_once(monkeypatch):
    """Outcome 3 forbids an unmeasured per-read cost. The consult was MEASURED
    at 330 ms on cc-10, so it must not run per read."""
    calls = []

    def _counted():
        calls.append(1)
        return frozenset(), "live-claims"
    monkeypatch.setattr(pqr, "_owned_agents_with_provenance", None, raising=False)
    monkeypatch.setattr(pqr, "_owned_now", _counted)
    _name(monkeypatch, "zeta")
    for _ in range(5):
        pqr.agent_queue_unverified("/anything/agents/zeta/x.jsonl")
    assert len(calls) == 5, "sanity: the patched seam is the one being counted"

    # And the REAL _owned_now memoizes: patch the underlying consult instead.
    pqr.reset_cache()
    monkeypatch.undo()
    real_calls = []

    import owncloud_sync

    def _consult():
        real_calls.append(1)
        return set(), "live-claims"
    monkeypatch.setattr(owncloud_sync, "_owned_agents_with_provenance", _consult)
    for _ in range(5):
        pqr._owned_now()
    assert len(real_calls) == 1, f"consulted {len(real_calls)}x; TTL cache is inert"
    pqr.reset_cache()
    pqr._owned_now()
    assert len(real_calls) == 2, "reset_cache() must force a re-resolve"


def test_the_detail_names_the_authoritative_remedy():
    """A refusal that does not say what to do instead is a dead end."""
    d = pqr.unverified_detail("zeta", "the queried")
    assert "backend-cat.sh" in d and "agents/zeta/aspirations.jsonl" in d
    assert "--exit-on-drift" in d
    assert "NOT evidence of absence" in d
    assert pqr.UNVERIFIED in d


# --- Part B — the query endpoint --------------------------------------------

def test_positive_control_agent_rows_are_stamped_and_world_rows_are_not(
        monkeypatch, ctx):
    """The stamp rides the PAYLOAD, so it survives the pipes that destroy a
    stderr warning (guard-5596). And it must discriminate: a world row read from
    the same request is not affected by the agent queue's unverifiability."""
    _own(monkeypatch, set())
    _name(monkeypatch, "zeta")
    ctx.query = {"goal_status": "pending"}
    resp = query_ep.query(ctx)
    rows = json.loads(_body(resp))
    by_source = {r["source"]: r for r in rows}
    assert set(by_source) == {"world", "agent"}, rows
    assert by_source["agent"]["read_from"] == pqr.UNVERIFIED
    assert "read_from" not in by_source["world"]


def test_negative_control_an_owned_queue_stamps_nothing(monkeypatch, ctx):
    """Proves the stamp is not unconditional — the failure a positive-only test
    cannot see."""
    _own(monkeypatch, {"zeta"})
    _name(monkeypatch, "zeta")
    ctx.query = {"goal_status": "pending"}
    rows = json.loads(_body(query_ep.query(ctx)))
    assert rows and all("read_from" not in r for r in rows)


def test_the_stamp_survives_full_mode(monkeypatch, ctx):
    """--full is the mode the Multi-Agent Safety Rule mandates (it is the only
    one carrying claimed_by), so it is the mode a cross-agent probe actually
    uses. A stamp present only in the projection would miss every real caller."""
    _own(monkeypatch, set())
    _name(monkeypatch, "zeta")
    ctx.query = {"goal_field_name": "id", "goal_field_value": "g-001-109",
                 "full": "true"}
    rows = json.loads(_body(query_ep.query(ctx)))
    assert len(rows) == 1 and rows[0]["read_from"] == pqr.UNVERIFIED
    assert rows[0]["status"] == "pending"      # the record itself is unchanged


def test_an_empty_identity_lookup_on_an_unverifiable_queue_refuses(
        monkeypatch, ctx):
    """THE MEASURED INCIDENT. A zero whose two explanations imply OPPOSITE
    actions is not a measurement (rb-245); the row stamp cannot reach it because
    there is no row."""
    _own(monkeypatch, set())
    _name(monkeypatch, "zeta")
    ctx.query = {"goal_field_name": "id", "goal_field_value": "g-999-99"}
    resp = query_ep.query(ctx)
    assert resp.status == 409, _body(resp)
    body = _body(resp)
    assert "agent_queue_unverified_empty" in body
    assert "g-999-99" in body and "backend-cat.sh" in body


def test_an_empty_identity_lookup_on_an_owned_queue_still_returns_empty(
        monkeypatch, ctx):
    """The refusal must not fire when the queue IS verifiable — otherwise it is
    a bare 'identity lookups now 409 on miss' regression."""
    _own(monkeypatch, {"zeta"})
    _name(monkeypatch, "zeta")
    ctx.query = {"goal_field_name": "id", "goal_field_value": "g-999-99"}
    resp = query_ep.query(ctx)
    assert json.loads(_body(resp)) == []


def test_an_empty_NON_identity_query_is_not_refused(monkeypatch, ctx):
    """guard-1562 — enumerate what would NEWLY fire. This box owns zero agent
    dirs, so refusing every empty result would refuse the ordinary dedup and
    status sweeps whose empty answer is common and legitimate."""
    _own(monkeypatch, set())
    _name(monkeypatch, "zeta")
    for q in ({"title_contains": "nothing matches this"},
              {"description_contains": "nor this"},
              {"goal_status": "expired"}):
        ctx.query = dict(q)
        resp = query_ep.query(ctx)
        assert json.loads(_body(resp)) == [], q


def test_a_NON_empty_identity_lookup_is_not_refused(monkeypatch, ctx):
    """The refusal keys on the ZERO, not on the lookup shape."""
    _own(monkeypatch, set())
    _name(monkeypatch, "zeta")
    ctx.query = {"goal_field_name": "id", "goal_field_value": "g-001-109"}
    rows = json.loads(_body(query_ep.query(ctx)))
    assert [r["goal_id"] for r in rows] == ["g-001-109"]


def test_goal_id_alias_reaches_the_refusal_too(monkeypatch, ctx):
    """`goal_id` is the key every projection EMITS, so it is the name a caller
    filters on (measured twice, by two agents, a day apart — g-115-5752). It is
    aliased to `id` before matching, and the refusal must key on the ALIASED
    name or it covers only half its own population."""
    _own(monkeypatch, set())
    _name(monkeypatch, "zeta")
    ctx.query = {"goal_field_name": "goal_id", "goal_field_value": "g-999-99"}
    resp = query_ep.query(ctx)
    assert resp.status == 409, _body(resp)


def test_an_absent_agent_queue_is_not_stamped(monkeypatch, tmp_path):
    """No agent source, no claim about one."""
    world = _seed(tmp_path / "world",
                  [{"id": "asp-001", "title": "W", "goals": [GOAL_W]}])
    c = _Ctx(world, tmp_path / "agents" / "zeta")   # nothing written there
    _own(monkeypatch, set())
    _name(monkeypatch, "zeta")
    c.query = {"goal_status": "pending"}
    rows = json.loads(_body(query_ep.query(c)))
    assert rows and all("read_from" not in r for r in rows)


# --- Part C — the read endpoint ---------------------------------------------

def test_positive_control_a_peer_miss_is_distinguishable_from_a_real_miss(
        monkeypatch, ctx):
    """Check 2 of this goal, verbatim: a locally-absent record and an
    authoritatively-absent record must be distinguishable in the consumer's
    output. Same HTTP status, different error key and body."""
    _own(monkeypatch, set())
    _name(monkeypatch, "zeta")
    ctx.query = {"source": "agent", "id": "asp-999"}
    resp = asp_read_ep.read(ctx)
    assert resp.status == 404
    body = _body(resp)
    assert "not_found_unverified" in body
    assert "NOT a verified absence" in body
    assert "backend-cat.sh" in body


def test_negative_control_an_owned_miss_stays_the_plain_not_found(
        monkeypatch, ctx):
    _own(monkeypatch, {"zeta"})
    _name(monkeypatch, "zeta")
    ctx.query = {"source": "agent", "id": "asp-999"}
    resp = asp_read_ep.read(ctx)
    assert resp.status == 404
    body = _body(resp)
    assert "not_found_unverified" not in body
    assert "not_found" in body


def test_a_world_miss_is_never_reported_as_a_peer_problem(monkeypatch, ctx):
    """The world store is shared and not agent-keyed; blaming a peer claim for
    a world miss would be a confident wrong answer."""
    _own(monkeypatch, set())
    _name(monkeypatch, "zeta")
    ctx.query = {"source": "world", "id": "asp-999"}
    resp = asp_read_ep.read(ctx)
    assert resp.status == 404
    assert "not_found_unverified" not in _body(resp)


def test_a_HIT_is_returned_unchanged_on_an_unverifiable_queue(monkeypatch, ctx):
    """One-directional, like the guard-1555 archived-hint it mirrors: the
    disclosure upgrades a MISS and must never alter a hit."""
    _own(monkeypatch, set())
    _name(monkeypatch, "zeta")
    ctx.query = {"source": "agent", "id": "asp-100"}
    resp = asp_read_ep.read(ctx)
    assert getattr(resp, "status", 200) == 200
    assert json.loads(_body(resp))["id"] == "asp-100"
