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
