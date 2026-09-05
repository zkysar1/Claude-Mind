"""Two-direction tests for the stale body-row reaper ().

THE FALSE-POSITIVE DIRECTION IS THE POINT. The filing goal names it explicitly:
"a LIVE row belonging to a still-running sibling Body is never reclaimed — test
the false-positive direction explicitly, not just the removal". An over-eager
reaper pops live workers' claims and manufactures the exact stranded-work class
the sweep exists to fix, and unlike a missed reap it does not self-heal: rows
are written at CLAIM time, not per tick, so a wrongly-reaped live Body stays
invisible for the rest of its goal (guard-741).

So the KEEP cases outnumber the REAP case here on purpose, and every KEEP
branch has its own test rather than being covered by a representative one.
"""

import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import body_row_reaper as R  # noqa: E402

LIVE_ROW = {
    "goal_id": "g-999-01",
    "claimed_at": "2026-08-07T23:58:02",
    "phase": "4",
    "title": "some goal",
}
SID = "3ebc753b-4acc-42d5-b94e-9d8c3bda7421"
OTHER_SID = "a2ac1676-b5e2-4a15-bbaf-d34e6885e7de"


def _decide(carrier_verdict, holds_claim=False, ev=None, row=LIVE_ROW,
            sid=SID, self_sid=None):
    return R.decide_row(
        sid=sid,
        row=row,
        carrier_verdict=carrier_verdict,
        carrier_evidence=ev or {},
        holds_live_claim=holds_claim,
        self_sid=self_sid,
    )


# ── the ONE reaping direction ────────────────────────────────────────────────

def test_stale_carrier_with_no_live_claim_is_reaped():
    """The orphan: the Body stopped ticking AND owns no non-terminal claim."""
    d = _decide(R.CV_STALE, holds_claim=False)
    assert d["verdict"] == R.R_REAP
    assert R.is_reaping(d["verdict"]) is True


def test_reap_requires_no_live_claim_not_merely_staleness():
    """Staleness alone must never license a reap — the claim join is what
    guarantees reaping cannot orphan in-flight work."""
    assert _decide(R.CV_STALE, holds_claim=False)["verdict"] == R.R_REAP
    assert _decide(R.CV_STALE, holds_claim=True)["verdict"] != R.R_REAP


# ── the KEEP directions (the ones that matter) ───────────────────────────────

def test_fresh_carrier_is_never_reaped():
    """THE false-positive guard: a still-running sibling Body keeps its row."""
    d = _decide(R.CV_FRESH_CORRECT, holds_claim=False)
    assert d["verdict"] == R.K_ALIVE
    assert R.is_reaping(d["verdict"]) is False


def test_fresh_carrier_is_kept_even_with_no_claim():
    """A Body between claims is alive, not orphaned. Freshness outranks the
    absence of a claim — otherwise every worker in its setup window is reapable."""
    assert _decide(R.CV_FRESH_CORRECT, holds_claim=False)["verdict"] == R.K_ALIVE


def test_stalled_with_claim_is_kept_for_the_stall_probe():
    """WorkerStallProbe's territory. Reaping here would delete the row the
    alert is about, and would pop a claim that is still live."""
    d = _decide(R.CV_STALE, holds_claim=True)
    assert d["verdict"] == R.K_STALLED_WITH_CLAIM
    assert R.is_reaping(d["verdict"]) is False


def test_absent_carrier_is_kept_death_unproven():
    """Not establishing life is not the same as establishing death."""
    assert _decide(R.CV_ABSENT)["verdict"] == R.K_NO_CARRIER


def test_unreadable_carrier_is_kept_instrument_fault():
    """The probe's own breakage must not look like the condition it hunts."""
    assert _decide(R.CV_UNREADABLE)["verdict"] == R.K_UNREADABLE


def test_own_session_row_is_never_reaped():
    """Self-preservation must not depend on this process's own heartbeat having
    ticked recently — that is exactly what breaks under an API storm."""
    d = _decide(R.CV_STALE, holds_claim=False, sid=OTHER_SID, self_sid=OTHER_SID)
    assert d["verdict"] == R.K_SELF_SID


def test_null_residue_is_reported_not_reaped():
    """Pre- null keys carry no claim and drain via the endpoint's own
    null-sibling sweep; they are reported so that drain stays observable."""
    d = _decide(R.CV_STALE, row=None)
    assert d["verdict"] == R.K_NULL_RESIDUE


# ── guard-358: a carrier cannot vouch for a body that did not write it ───────

def test_fresh_wrong_carrier_is_kept():
    assert _decide(R.CV_FRESH_WRONG)["verdict"] == R.K_SID_MISMATCH


def test_stale_carrier_written_by_another_sid_is_kept():
    """The subtle half. A MISMATCHED carrier that is also stale collapses to
    plain `stale` upstream, so only the evidence's `carrier_sid` distinguishes
    it. Without this branch a carrier describing a different body would be read
    as a death certificate for this one."""
    d = _decide(R.CV_STALE, holds_claim=False, ev={"carrier_sid": "deadbeef"})
    assert d["verdict"] == R.K_SID_MISMATCH
    assert R.is_reaping(d["verdict"]) is False


@pytest.mark.parametrize("carrier_sid", ["deadbeef", ""])
def test_stale_mismatched_carrier_is_kept_whatever_the_sid_SPELLING(carrier_sid):
    """The defining property is that the key is PRESENT, not that it is truthy.

    guard-3080: pin the property, not the one known bad instance. The producer
    (stranded-claim-sweep._body_carrier_verdict) writes `carrier_sid` on
    `str(doc.get("sid") or "") != sid`, so it fires for an UNIDENTIFIED writer
    too and stores `""` — every falsy value this key can hold, since the value
    is `str(...)[:8]`.

    The pre-fix predicate `ev.get("carrier_sid")` passed the "deadbeef" case
    above and REAPED this one, so the suite was green over a live leak on a
    DELETE path. Worse, the asymmetry ran the wrong way: the same empty sid
    arriving as `fresh-wrong` was kept by the left-hand clause, so an anonymous
    carrier was distrusted when fresh and trusted when stale.

    This test fails against the pre-fix predicate for carrier_sid="" and passes
    for "deadbeef" — which is what makes it a regression pin rather than a
    restatement of the test above.
    """
    d = _decide(R.CV_STALE, holds_claim=False, ev={"carrier_sid": carrier_sid})
    assert d["verdict"] == R.K_SID_MISMATCH
    assert R.is_reaping(d["verdict"]) is False


def test_fresh_wrong_and_stale_agree_on_an_anonymous_carrier():
    """Both spellings of a mismatch must reach the same verdict for the same
    evidence. Asserting the two ARE EQUAL (rather than each being K_SID_MISMATCH
    separately) is what pins the symmetry itself: a future edit that re-splits
    the branch would have to break this to pass."""
    fresh = _decide(R.CV_FRESH_WRONG, holds_claim=False, ev={"carrier_sid": ""})
    stale = _decide(R.CV_STALE, holds_claim=False, ev={"carrier_sid": ""})
    assert fresh["verdict"] == stale["verdict"] == R.K_SID_MISMATCH


# ── fail-safe on the unknown ─────────────────────────────────────────────────

@pytest.mark.parametrize("token", [None, "", "some-future-verdict", "REAP"])
def test_unrecognised_carrier_verdict_never_reaps(token):
    """A sixth token added upstream must not silently acquire delete power."""
    d = _decide(token, holds_claim=False)
    assert R.is_reaping(d["verdict"]) is False
    assert d["verdict"] == R.K_UNREADABLE


def test_is_reaping_is_the_only_mutation_predicate():
    """Pins the verdict->action mapping so a rename cannot silently widen it."""
    every = {
        R.R_REAP, R.K_SELF_SID, R.K_NO_CARRIER, R.K_ALIVE,
        R.K_STALLED_WITH_CLAIM, R.K_UNREADABLE, R.K_SID_MISMATCH,
        R.K_NULL_RESIDUE,
    }
    assert {v for v in every if R.is_reaping(v)} == {R.R_REAP}


# ── aggregate ────────────────────────────────────────────────────────────────

def test_decide_separates_reapable_from_kept():
    rows = {
        "sid-orphan": dict(LIVE_ROW),
        "sid-alive": dict(LIVE_ROW),
        "sid-claimed": dict(LIVE_ROW),
    }
    verdicts = {
        "sid-orphan": (R.CV_STALE, {"carrier_age_minutes": 4000.0}),
        "sid-alive": (R.CV_FRESH_CORRECT, {"carrier_age_minutes": 2.0}),
        "sid-claimed": (R.CV_STALE, {"carrier_age_minutes": 4000.0}),
    }
    claims = {"sid-claimed": "g-999-02"}
    out = R.decide(rows, verdicts, claims, self_sid=None)

    assert out["scanned"] == 3
    assert [d["sid"] for d in out["reapable"]] == ["sid-orphan"]
    assert out["verdict_counts"] == {
        R.R_REAP: 1, R.K_ALIVE: 1, R.K_STALLED_WITH_CLAIM: 1,
    }


def test_decide_on_empty_rows_is_vacuous_not_an_error():
    out = R.decide({}, {}, {}, self_sid=None)
    assert out["scanned"] == 0
    assert out["reapable"] == []


def test_decide_missing_verdict_entry_keeps_the_row():
    """A sid whose carrier lookup produced nothing at all must not be reaped —
    the same fail-safe as an unrecognised token, reached by a different route."""
    out = R.decide({SID: dict(LIVE_ROW)}, {}, {}, self_sid=None)
    assert out["reapable"] == []
    assert out["decisions"][0]["verdict"] == R.K_UNREADABLE


def test_reap_threshold_is_longer_than_the_sweeps_claim_hold_window():
    """The two thresholds answer different questions and the reaper's must be
    the more conservative: holding a claim is reversible next sweep, deleting a
    row is not."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_scs", SCRIPT_DIR / "stranded-claim-sweep.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert R.DEFAULT_REAP_STALE_MINUTES > mod.DEFAULT_CARRIER_FRESH_MINUTES
    # And the sweep must consume the reaper's constant rather than carry a
    # second literal that could drift (communication-clarity rule 5).
    assert mod._REAP_STALE_MINUTES_DEFAULT == R.DEFAULT_REAP_STALE_MINUTES


# ── integration: the apply path must RE-READ, not act on the scan snapshot ───

def _load_sweep():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_scs_apply", SCRIPT_DIR / "stranded-claim-sweep.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _wire(monkeypatch, sweep, claims_sequence, carrier=("stale", {})):
    """Stub every I/O edge of _reap_stale_body_rows. `claims_sequence` yields a
    successive claim map per read, so a test can make state CHANGE between the
    scan and the write — which is the only way to observe whether the apply path
    re-reads or reuses."""
    import _team_state
    import worker_stall
    import worker_close_in_flight_clear as wc

    row = {SID: dict(LIVE_ROW)}
    monkeypatch.setattr(
        _team_state, "read_shard_authoritative_with_provenance",
        lambda w, a: ({"in_flight_bodies": row}, "authoritative"),
    )
    seq = list(claims_sequence)
    calls = {"claims": 0, "cleared": []}

    def _claims(*_stores):
        i = min(calls["claims"], len(seq) - 1)
        calls["claims"] += 1
        return seq[i], "authoritative"

    # Stubbed at read_claims_UNION, i.e. the function the sweep actually calls
    # (). Stubbing the single-store `read_claims` underneath it made
    # `calls["claims"]` count STORES rather than READS, so the sequence advanced
    # twice per `_read_claims()` and `>= 2` stopped meaning "the map was re-read
    # before writing" — which is the only thing these tests are asking. The union
    # itself is pinned separately in test_worker_stall.py.
    monkeypatch.setattr(worker_stall, "read_claims_union", _claims)
    monkeypatch.setattr(sweep, "_body_carrier_verdict", lambda a, s, f: carrier)

    def _clear(_agent, sid):
        # MUST actually mutate the shard the reader sees. The first version of
        # this stub only recorded the call, and the positive control failed —
        # correctly — because the guard-2305 read-back saw the row still there
        # and refused to report a success the store had not confirmed. A stub
        # that cannot be observed by the verifying read makes the verification
        # untestable, which is the one thing it must not be.
        calls["cleared"].append(sid)
        row.pop(sid, None)
        return "cleared"

    monkeypatch.setattr(wc, "clear_body_row", _clear)
    calls["row"] = row
    return calls


def test_apply_declines_when_a_claim_appears_between_scan_and_write(monkeypatch):
    """guard-3020, the defect this test exists for. The scan sees an orphan; by
    the time the write is composed the sid holds a live claim. Reusing the scan's
    claim map would delete a row that is no longer an orphan."""
    sweep = _load_sweep()
    calls = _wire(monkeypatch, sweep, [{}, {SID: "g-999-09"}])

    out = sweep._reap_stale_body_rows(
        agent="bravo", self_sid=None, stale_minutes=180.0, apply_changes=True
    )

    assert out["reap_candidates"] == 1, "scan should have seen the orphan"
    assert out["reaped"] == 0, "but the write must decline on the fresh read"
    assert calls["cleared"] == [], "clear_body_row must not have been called"
    assert out["decisions"][0]["apply_result"] == "recheck-declined"
    assert calls["claims"] >= 2, "the claim map must be read again before writing"


def test_apply_reaps_when_the_orphan_is_still_an_orphan(monkeypatch):
    """The positive control for the test above — same wiring, state unchanged.
    Without this, a function that declined unconditionally would also pass."""
    sweep = _load_sweep()
    calls = _wire(monkeypatch, sweep, [{}, {}])

    out = sweep._reap_stale_body_rows(
        agent="bravo", self_sid=None, stale_minutes=180.0, apply_changes=True
    )

    assert out["reaped"] == 1
    assert calls["cleared"] == [SID]
    assert out["decisions"][0]["apply_result"] == "reaped"


def test_dry_run_never_calls_the_clear_primitive(monkeypatch):
    sweep = _load_sweep()
    calls = _wire(monkeypatch, sweep, [{}, {}])

    out = sweep._reap_stale_body_rows(
        agent="bravo", self_sid=None, stale_minutes=180.0, apply_changes=False
    )

    assert out["reap_candidates"] == 1
    assert out["reaped"] == 0
    assert calls["cleared"] == []


def test_unreadable_claims_decline_rather_than_reap_everything(monkeypatch):
    """An unreadable claim map must never read as 'no claim held' — that would
    flip every stale row straight to reapable.

    This covers the RAISE path only. `read_claims` does not in fact raise on an
    unreadable store — it returns ({}, "none") — so the sibling test
    `test_unanswered_claim_half_declines_the_reap` covers the path production
    actually takes (g-306-270). Both are needed: the try/except and the
    provenance check are separate branches and each was assumed to cover the
    other.
    """
    sweep = _load_sweep()
    import worker_stall

    def _boom(_path):
        raise OSError("store unreachable")

    # The union must run for real, or _boom is never reached (it is the
    # per-store reader underneath it).
    real_union = worker_stall.read_claims_union
    sweep_calls = _wire(monkeypatch, sweep, [{}, {}])
    monkeypatch.setattr(worker_stall, "read_claims", _boom)
    monkeypatch.setattr(worker_stall, "read_claims_union", real_union)
    out = sweep._reap_stale_body_rows(
        agent="bravo", self_sid=None, stale_minutes=180.0, apply_changes=True
    )
    assert out["reaped"] == 0
    assert sweep_calls["cleared"] == []
    assert any("claims-read" in e for e in out["errors"])


def test_write_that_reports_cleared_but_does_not_clear_is_not_counted(monkeypatch):
    """guard-2305: a team-state write can print success over a no-op, so the
    returned token is not evidence. Only the read-back decides. This test exists
    because the FIRST version of the positive control above failed exactly here —
    a stub that returned 'cleared' without mutating was correctly refused."""
    sweep = _load_sweep()
    import worker_close_in_flight_clear as wc

    calls = _wire(monkeypatch, sweep, [{}, {}])
    # Report success, change nothing — the no-op-behind-a-success-message shape.
    monkeypatch.setattr(wc, "clear_body_row", lambda a, s: "cleared")

    out = sweep._reap_stale_body_rows(
        agent="bravo", self_sid=None, stale_minutes=180.0, apply_changes=True
    )

    assert out["reaped"] == 0, "an unverified write must not be counted"
    assert out["decisions"][0]["apply_result"] == "verify-failed-row-still-present"
    assert any("readback" in e for e in out["errors"])


def test_unreadable_shard_at_write_is_distinct_from_a_vanished_row(monkeypatch):
    """guard-2418: 'I could not look' and 'it is gone' take the same action but
    mean opposite things. Collapsing them would report a broken shard read as
    evidence the row was already cleared."""
    sweep = _load_sweep()
    import _team_state

    calls = _wire(monkeypatch, sweep, [{}, {}])
    state = {"n": 0}

    def _flaky(_w, _a):
        state["n"] += 1
        if state["n"] == 1:  # scan succeeds, the pre-write re-read does not
            return {"in_flight_bodies": calls["row"]}, "authoritative"
        raise OSError("shard unreachable")

    monkeypatch.setattr(
        _team_state, "read_shard_authoritative_with_provenance", _flaky
    )
    out = sweep._reap_stale_body_rows(
        agent="bravo", self_sid=None, stale_minutes=180.0, apply_changes=True
    )
    assert out["reaped"] == 0
    assert calls["cleared"] == [], "must not write when it cannot re-read"
    assert out["decisions"][0]["apply_result"] == "shard-unreadable-at-write"


def test_unverifiable_readback_is_not_counted_as_reaped(monkeypatch):
    """The write may have succeeded, but an unverifiable write is not a verified
    one — and this branch must not crash on the None the read now returns."""
    sweep = _load_sweep()
    import _team_state

    calls = _wire(monkeypatch, sweep, [{}, {}])
    state = {"n": 0}

    def _flaky(_w, _a):
        state["n"] += 1
        if state["n"] <= 2:  # scan + pre-write re-read succeed
            return {"in_flight_bodies": calls["row"]}, "authoritative"
        raise OSError("shard unreachable")  # the read-BACK fails

    monkeypatch.setattr(
        _team_state, "read_shard_authoritative_with_provenance", _flaky
    )
    out = sweep._reap_stale_body_rows(
        agent="bravo", self_sid=None, stale_minutes=180.0, apply_changes=True
    )
    assert calls["cleared"] == [SID], "the write itself should have been attempted"
    assert out["reaped"] == 0, "but it cannot be counted without verification"
    assert out["decisions"][0]["apply_result"] == "readback-unreadable"


# ── integration: a SYNTHETIC candidate through the REAL clear_body_row ───────
# sq-019 / . Every reap-direction test above substitutes clear_body_row,
# so the handler -> side-effect leg was covered only by stubs: nothing drove a
# real candidate through the real removal primitive AND the guard-2305 read-back.
# The trigger -> handler leg IS exercised live at loop entry, but with a
# permanently EMPTY candidate set, and a live population of zero cannot supply a
# positive control (guard-3122). This test mints the one it needs.
#
# ISOLATION IS THE WHOLE DIFFICULTY, not an incidental detail. Minting a fixture
# row in the SHARED team-state is the guard-2611 phantom-shard defect, and
# clear_body_row's own docstring records an instance: a parametrized test one
# file over passed `no-such-agent-xyz` and a full-suite run left a real
# `no-such-agent-xyz` shard in the shared store, which then tripped
# test_active_agents_tripwire from an unrelated gate suite. So this runs against
# a tmp world behind an in-process DaemonFixture, which pins MIND_WORLD,
# MIND_META and STORAGE_BACKEND (the last is guard-955: an own-cloud write from
# a tmp world collides on the PRODUCTION S3 key).

def _seed_body_row(world: Path, agent: str, sid: str) -> Path:
    import yaml
    shard_dir = world / "team-state" / "agents"
    shard_dir.mkdir(parents=True, exist_ok=True)
    shard = shard_dir / (agent + ".yaml")
    shard.write_text(yaml.safe_dump({"in_flight_bodies": {sid: dict(LIVE_ROW)}}),
                     encoding="utf-8")
    (world / "aspirations.jsonl").write_text("", encoding="utf-8")  # no live claims
    return shard


def test_synthetic_candidate_through_the_REAL_clear_body_row_removes_the_KEY():
    """The positive control the live path cannot supply.

    Only the carrier verdict is stubbed — that is the TRIGGER leg, already
    covered by the branch tests above. clear_body_row, the daemon write beneath
    it, the shard read and the guard-2305 read-back all run for real.
    """
    import tempfile
    import yaml

    sys.path.insert(0, str(SCRIPT_DIR / "tests"))
    from _daemon_fixture import DaemonFixture

    agent, sid = "alpha", SID
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd) / "world"
        world.mkdir(parents=True)
        shard = _seed_body_row(world, agent, sid)

        before = yaml.safe_load(shard.read_text(encoding="utf-8"))
        assert sid in before["in_flight_bodies"], "precondition: the row exists"

        import importlib
        import _paths

        # RELOADING _paths IS LOAD-BEARING, and the first version of this test
        # omitted it and READ LIVE STATE. _paths.py resolves
        # `WORLD_DIR = _resolve_external(...)` at module scope, i.e. ONCE per
        # process, and pytest has already imported it long before this fixture
        # pins MIND_WORLD. The sweep's `from _paths import WORLD_DIR` is
        # deliberately lazy (cycle-proof), but a lazy import still reads the
        # CACHED module's frozen constant -- so merely loading the sweep inside
        # the fixture, which is what this test first claimed was sufficient,
        # changes nothing.
        #
        # Measured: without the reload the sweep read the REAL shard
        # (rows_examined=2, including this very session's own row) and issued a
        # real REAPING call for a live SID. It was harmless ONLY because the
        # WRITE went to the fixture daemon's tmp world while the READ came from
        # the real one, so the guard-2305 read-back reported
        # `verify-failed-row-still-present` and refused to count it. That is the
        # safety net doing its job, not isolation working.
        #
        # The try/finally spans the WHOLE fixture: on any failure path _paths
        # must still be re-resolved, or every later test in this process
        # inherits a WORLD_DIR pointing at a tmp dir that is about to be
        # deleted. finally runs after __exit__ has restored the env, so the
        # restoring reload below re-resolves against the real world.
        try:
            with DaemonFixture(world, agent=agent):
                importlib.reload(_paths)
                sweep = _load_sweep()
                sweep._body_carrier_verdict = lambda a, s, f: ("stale", {})
                out = sweep._reap_stale_body_rows(
                    agent=agent, self_sid=None, stale_minutes=180.0,
                    apply_changes=True,
                )
        finally:
            importlib.reload(_paths)

        assert out["rows_examined"] == 1, "the synthetic row must be seen: %r" % out
        cand = out["decisions"][0]

        # ANTI-VACUITY, and the trap this test would otherwise fall into:
        # clear_body_row short-circuits to "not-resident" when the agent dir does
        # not resolve, WITHOUT writing anything. A test that asserted only
        # "the key is gone" would pass on a row that was never there to begin
        # with, and a test that asserted only reaped==1 could pass on a
        # short-circuit. The token proves the real write path actually ran.
        assert cand.get("clear_token") == "cleared", (
            "the REAL primitive must have written, not short-circuited on the "
            "residency gate; token=%r result=%r" % (cand.get("clear_token"),
                                                    cand.get("apply_result")))
        assert cand["apply_result"] == "reaped", cand
        assert out["reaped"] == 1, out

        after = yaml.safe_load(shard.read_text(encoding="utf-8")) or {}
        bodies = after.get("in_flight_bodies") or {}

        # THE KEY IS ABSENT, NOT NULL (). This leg used to SET NULL,
        # which left one permanent null-valued key per SID on a shared synced
        # store. `not bodies.get(sid)` would be satisfied by BOTH, so the
        # membership test is the assertion that actually discriminates.
        assert sid not in bodies, (
            "the row must be REMOVED; a null-valued key is the g-306-186 residue "
            "this write exists to avoid. bodies=%r" % bodies)


def test_the_real_path_leaves_the_SHARED_team_state_untouched():
    """Guards the guard: proves the isolation above, rather than assuming it.

    If the tmp-world pin ever stops taking, the test above would still pass
    while writing to the live store — the exact guard-2611 failure it is
    written to avoid, and one that shows up as an unrelated suite going red.
    """
    from _paths import WORLD_DIR
    shared = Path(WORLD_DIR) / "team-state" / "agents" / "alpha.yaml"
    fingerprint = shared.stat().st_mtime_ns if shared.exists() else None

    test_synthetic_candidate_through_the_REAL_clear_body_row_removes_the_KEY()

    now = shared.stat().st_mtime_ns if shared.exists() else None
    assert now == fingerprint, (
        "the shared alpha shard was written during an isolated tmp-world test — "
        "the MIND_WORLD pin is not holding (guard-2611)")


# ── the claim map's SCOPE () ────────────────────────────────────────

def test_agent_queue_only_claim_is_not_reaped(monkeypatch):
    """A SID whose ONLY non-terminal claim sits in the agent queue must KEEP.

    The reaping branch's whole safety argument is `holds_live_claim == False`,
    so a claim the map cannot see is a claim that does not protect its Body.
    Reading the world queue alone made this exact row reapable. Both halves are
    asserted: the sweep must actually READ the owning agent's queue, and the
    claim it finds there must reach the verdict.
    """
    sweep = _load_sweep()
    import worker_stall

    seen = []

    def _per_store(path):
        seen.append(str(path))
        # The world queue is empty; the claim lives ONLY in the agent queue.
        if "/bravo/" in str(path).replace("\\", "/"):
            return {SID: "g-999-11"}, "authoritative"
        return {}, "authoritative"

    # Capture the REAL union before _wire stubs it, then put it back: this test
    # is about the union running for real over a stubbed per-store reader.
    real_union = worker_stall.read_claims_union
    _wire(monkeypatch, sweep, [{}, {}])          # stubs rows / carrier / clear
    monkeypatch.setattr(worker_stall, "read_claims", _per_store)
    monkeypatch.setattr(worker_stall, "read_claims_union", real_union)

    out = sweep._reap_stale_body_rows(
        agent="bravo", self_sid=None, stale_minutes=180.0, apply_changes=True
    )

    norm = [p.replace("\\", "/") for p in seen]
    assert any("/bravo/" in p for p in norm), f"agent queue was never read: {seen}"
    assert any("/bravo/" not in p for p in norm), f"world queue was never read: {seen}"
    assert out["reap_candidates"] == 0, out["decisions"]
    assert out["decisions"][0]["verdict"] == R.K_STALLED_WITH_CLAIM, out["decisions"]


def test_unanswered_claim_half_declines_the_reap(monkeypatch):
    """`provenance == "none"` means no layer answered — decline, never degrade.

    read_claims does not RAISE on an unreadable store; it returns ({}, "none").
    So the sweep's try/except could not see that case and an unread claim map
    degraded silently to "no claim", which reaps everything stale. An absent
    claim and an unread claim are indistinguishable to the reaper, and only one
    of them is safe.
    """
    sweep = _load_sweep()
    import worker_stall

    calls = _wire(monkeypatch, sweep, [{}, {}])
    monkeypatch.setattr(worker_stall, "read_claims_union", lambda *s: ({}, "none"))

    out = sweep._reap_stale_body_rows(
        agent="bravo", self_sid=None, stale_minutes=180.0, apply_changes=True
    )

    assert out["reap_candidates"] == 0, out
    assert calls["cleared"] == [], "an unread claim map must not license a write"
    assert any("provenance=none" in e for e in out["errors"]), out["errors"]


# ── the FOURTH state: a goal that resolves NOWHERE () ───────────────
#
# `_goal_terminal` is tri-state and collapsed two opposite meanings into False:
# "the queue holds this goal and it is not finished" (keep) and "no store holds
# this goal at all" (maximally finished, row immortal). These pin the split.
#
# The KEEP cases outnumber the REAP case here too, and for a sharper reason than
# the file header gives: this is the only predicate in the module that reaps on
# the ABSENCE of evidence, so every way it can be wrong is a deletion.

def _decide_v(known, terminal=None, holds_claim=False, carrier=None, row=LIVE_ROW):
    """One row through the REAL `decide`, so `_goal_vanished` and the branch
    ordering are exercised together rather than hand-fed to `decide_row`."""
    out = R.decide(
        {SID: row},
        {SID: (carrier or R.CV_FRESH_CORRECT, {})},
        {SID: "g-other"} if holds_claim else {},
        None,
        terminal,
        known,
    )
    return out["decisions"][0], out


def test_vanished_goal_is_reaped_even_though_the_body_is_ALIVE():
    """THE NEW REACH, and the carrier is deliberately `fresh-correct`.

    Every pre-existing reaping path needs the BODY to look gone: `R_REAP` wants a
    stale carrier, and the terminal branch only fires when a store says the goal
    finished. A Body that is perfectly alive and has simply MOVED ON hits
    `K_ALIVE` and keeps its phantom row forever. So a fresh carrier here is not
    incidental — it is what makes this test discriminating: nothing but the
    vanished predicate can produce a reap from this input.
    """
    d, out = _decide_v(known={"g-other"}, terminal=set())
    assert d["verdict"] == R.R_REAP_VANISHED_GOAL, d
    assert d["goal_vanished"] is True
    assert d["goal_is_terminal"] is False
    assert R.is_reaping(d["verdict"]) is True
    assert len(out["reapable"]) == 1


def test_goal_that_EXISTS_and_is_unfinished_keeps_its_row():
    """The live-non-terminal positive control the filing goal asks for.

    Without this the suite could not tell a working predicate from one that
    reaps every row it is shown, and that failure mode passes every REAP test.
    """
    d, out = _decide_v(known={"g-999-01"}, terminal=set())
    assert d["goal_vanished"] is False
    assert d["verdict"] == R.K_ALIVE, d
    assert out["reapable"] == []


def test_unreadable_known_set_declines_the_reap_and_degrades_to_today():
    """`None` = NOT MEASURED, and it must behave exactly as before the fix.

    This is the degradation the filing goal makes mandatory: an unreadable store
    must never be able to mint a reap. `None` is not a tidy default here — it is
    the whole safety contract, because this module cannot tell a complete census
    from a partial one.
    """
    d, out = _decide_v(known=None, terminal=set())
    assert d["goal_vanished"] is None, "unmeasured must not read as False"
    assert d["verdict"] == R.K_ALIVE
    assert out["reapable"] == []


def test_a_PARTIAL_census_is_the_hazard_this_None_exists_to_prevent():
    """Same row, same code, two censuses — only the narrower one deletes.

    Not a restatement of the test above: that one asserts `None` is safe, this
    one shows what a caller passing a set instead of `None` actually buys. It is
    the guard-3379 failure in miniature, and on the live tree the omitted layer
    was the archive, worth ~2,480 ids.
    """
    complete, _ = _decide_v(known={"g-999-01", "g-other"}, terminal=set())
    partial, _ = _decide_v(known={"g-other"}, terminal=set())
    assert complete["verdict"] == R.K_ALIVE
    assert partial["verdict"] == R.R_REAP_VANISHED_GOAL
    assert complete["verdict"] != partial["verdict"], (
        "a narrowed census must be observably different, or the caller-side "
        "obligation in `decide`'s docstring is untestable")


def test_vanished_goal_with_a_LIVE_CLAIM_is_never_reaped():
    """ re-opened one door over — caught by the existing suite.

    `holds_live_claim` is per-SID, not per-goal, so a Body can hold a live claim
    on goal Y while a stale row names a vanished goal X. Reaping then hides a
    demonstrably working Body for the rest of its goal (guard-741), and rows are
    written at CLAIM time so it never comes back. The branch was written ungated
    first and two EXISTING tests failed immediately; this pins the gate directly
    so the next reader does not have to rediscover it from those two.
    """
    d, out = _decide_v(known={"g-other"}, terminal=set(), holds_claim=True)
    assert d["goal_vanished"] is True, "the predicate should still SEE it"
    assert d["verdict"] != R.R_REAP_VANISHED_GOAL
    assert d["verdict"] == R.K_ALIVE, d
    assert out["reapable"] == []


def test_vanished_and_claim_held_falls_THROUGH_to_the_carrier_verdict():
    """The gate preserves old behaviour byte-for-byte, rather than minting a
    new keep token: with a claim held, the row gets exactly the verdict it
    would have got before this change existed."""
    stale, _ = _decide_v(known={"g-other"}, terminal=set(), holds_claim=True,
                         carrier=R.CV_STALE)
    assert stale["verdict"] == R.K_STALLED_WITH_CLAIM, stale


def test_terminal_evidence_outranks_vanished_inference():
    """Ordering: where both could fire, the verdict names the STRONGER reason.

    Positive evidence a store asserted beats an inference from silence — and the
    two tokens must stay tellable apart in `verdict_counts`, which is the only
    way anyone can later count how often the risky predicate fired.
    """
    d, _ = _decide_v(known=set(), terminal={"g-999-01"})
    assert d["verdict"] == R.R_REAP_TERMINAL_GOAL, d
    assert R.R_REAP_VANISHED_GOAL != R.R_REAP_TERMINAL_GOAL
    assert R.R_REAP_VANISHED_GOAL in R.REAPING_VERDICTS


def test_self_sid_still_outranks_the_vanished_branch():
    """The running session's own row is belt-and-braces protected, and the new
    branch must not slip above it — a self row naming a finished goal is a miss
    in the CLEAN-close path and belongs fixed there, not masked here."""
    d = R.decide_row(sid=SID, row=LIVE_ROW, carrier_verdict=R.CV_FRESH_CORRECT,
                     self_sid=SID, goal_vanished=True)
    assert d["verdict"] == R.K_SELF_SID


@pytest.mark.parametrize("row", [
    pytest.param({"claimed_at": "2026-08-07T23:58:02"}, id="no-goal_id"),
    pytest.param(None, id="null-residue"),
])
def test_a_row_the_predicate_cannot_key_on_is_never_vanished(row):
    """guard-1704: a signal added to a predicate over a population must be
    DEFINED for every member. A row carrying no id cannot be looked up, so the
    honest answer is `None` — never `True`, which here would mean deleting a row
    precisely because it told us nothing."""
    assert R._goal_vanished(row, {"g-999-01"}) is None


# ── the vanished predicate END TO END, through the real sweep () ────

def _wire_ids(monkeypatch, known, known_via="authoritative", terminal=None):
    """Pin BOTH id censuses so these tests do not depend on the live queues."""
    import worker_stall
    monkeypatch.setattr(worker_stall, "read_terminal_goal_ids",
                        lambda *s: (set(terminal or ()), "authoritative"))
    monkeypatch.setattr(worker_stall, "read_known_goal_ids",
                        lambda *s: (set(known or ()), known_via))


def test_vanished_row_is_ACTUALLY_REAPED_through_the_apply_path(monkeypatch):
    """The wiring test, and the one that catches an inert fix.

    `decide` defaults `known_goal_ids` to None, so the guard-3020 re-check
    immediately before the delete re-runs the decision — and if that call omits
    the census, every vanished candidate comes back `goal_vanished=None` and is
    dropped as `recheck-declined`. The scan would keep listing candidates and the
    sweep would keep reaping none, which from the outside is indistinguishable
    from healthy conservatism (guard-1943: pinning the decision says nothing
    about the wiring). Asserting `cleared` — not `reap_candidates` — is the whole
    point of this test; the first draft of the integration failed exactly here.
    """
    sweep = _load_sweep()
    calls = _wire(monkeypatch, sweep, [{}], carrier=(R.CV_FRESH_CORRECT, {}))
    _wire_ids(monkeypatch, known={"g-other"})

    out = sweep._reap_stale_body_rows(
        agent="bravo", self_sid=None, stale_minutes=180.0, apply_changes=True
    )

    assert out["reap_candidates"] == 1, out["decisions"]
    assert calls["cleared"] == [SID], (
        "the row was identified but never cleared — the apply-time re-check is "
        "not passing the known-id census, so the predicate is inert")
    assert out["reaped"] == 1, out
    assert out["decisions"][0]["verdict"] == R.R_REAP_VANISHED_GOAL


def test_unanswered_known_half_declines_the_reap(monkeypatch):
    """Sibling of the claim-half test: `provenance == "none"` means no layer
    answered, so the census is partial and MUST NOT license a delete. This is
    the one direction where degrading quietly would delete live rows."""
    sweep = _load_sweep()
    calls = _wire(monkeypatch, sweep, [{}], carrier=(R.CV_FRESH_CORRECT, {}))
    _wire_ids(monkeypatch, known=set(), known_via="none")

    out = sweep._reap_stale_body_rows(
        agent="bravo", self_sid=None, stale_minutes=180.0, apply_changes=True
    )

    assert out["reap_candidates"] == 0, out["decisions"]
    assert calls["cleared"] == [], "an unread census must not license a write"
    assert any("known-read" in e and "provenance=none" in e
               for e in out["errors"]), out["errors"]
    assert out["decisions"][0]["goal_vanished"] is None


def test_the_census_DOMAIN_is_reported_beside_the_verdict(monkeypatch):
    """guard-6002: a sweep reporting 0 is not a clean population until its
    DOMAIN is stated beside the zero. `known_population` and `known_via` are
    that domain — without them "no row named a vanished goal" and "the census
    came back nearly empty" print identically."""
    sweep = _load_sweep()
    _wire(monkeypatch, sweep, [{}], carrier=(R.CV_FRESH_CORRECT, {}))
    _wire_ids(monkeypatch, known={"g-999-01", "g-a", "g-b"})

    out = sweep._reap_stale_body_rows(
        agent="bravo", self_sid=None, stale_minutes=180.0, apply_changes=False
    )

    assert out["reap_candidates"] == 0
    assert out["known_population"] == 3, out
    assert out["known_via"] == "authoritative", out


def test_the_archive_is_among_the_stores_the_sweep_censuses(monkeypatch):
    """The layer whose omission inverts the predicate (guard-3379).

    Measured on the live tree: the archive held 2,480 of the 5,432 known ids, so
    a census that skipped it would have called every one of those goals
    "resolving nowhere". Asserting the PATH is passed is the only way to pin
    that, since a set alone cannot say where it came from.
    """
    sweep = _load_sweep()
    import worker_stall
    _wire(monkeypatch, sweep, [{}], carrier=(R.CV_FRESH_CORRECT, {}))
    monkeypatch.setattr(worker_stall, "read_terminal_goal_ids",
                        lambda *s: (set(), "authoritative"))

    seen = []

    def _known(*stores):
        seen.extend(str(p).replace("\\", "/") for p in stores)
        return {"g-999-01"}, "authoritative"

    monkeypatch.setattr(worker_stall, "read_known_goal_ids", _known)
    sweep._reap_stale_body_rows(agent="bravo", self_sid=None,
                                stale_minutes=180.0, apply_changes=False)

    assert any(p.endswith("aspirations-archive.jsonl") for p in seen), (
        f"the archive was never censused: {seen}")
    assert any(p.endswith("/bravo/aspirations.jsonl") for p in seen), seen
    assert any(p.endswith("/world/aspirations.jsonl")
               or (p.endswith("aspirations.jsonl") and "/bravo/" not in p
                   and "archive" not in p) for p in seen), seen
