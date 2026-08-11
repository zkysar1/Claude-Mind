"""Branch proof for the worker-close in_flight clear (-d).

`decide()` is pure, so every branch is reachable without a daemon, a second box,
or a forked Body. The fail-safe invariant under test is the one the design turns
on: ONLY an affirmative `claimed_by_sid == this Body` authorizes a clear. Every
other input -- absent row, missing goal_id, unset MIND_SID, unreadable owner,
foreign owner -- must LEAVE THE ROW ALONE.

That asymmetry is the whole point. `in_flight` is agent-keyed with no sid, so a
worker and its reducer share one row; a wrong clear blanks a LIVE reducer's row
and makes a working agent look idle to every partner's selector (guard-2305),
which is strictly worse than the stale row this fix removes.

guard-1165: no module-level os.environ mutation and no sys.modules stubs.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import worker_close_in_flight_clear as wc  # noqa: E402

MINE = "sid-mine"
THEIRS = "sid-theirs"
ROW = {"goal_id": "g-1-1", "title": "t", "claimed_at": "2026-08-03T00:00:00",
       "phase": "4"}


# ── decide(): the only path that authorizes a clear ──────────────────────────

def test_matching_sid_is_the_one_authorizing_case():
    r = wc.decide("zeta", MINE, ROW, MINE)
    assert r["verdict"] == "ours"
    assert r["goal_id"] == "g-1-1"


# ── decide(): every other branch must NOT authorize ──────────────────────────

def test_absent_row_is_a_noop():
    r = wc.decide("zeta", MINE, None, MINE)
    assert r["verdict"] == "absent"


def test_row_without_goal_id_is_unknown_owner():
    r = wc.decide("zeta", MINE, {"title": "t"}, MINE)
    assert r["verdict"] == "unknown-owner"


def test_unset_local_sid_never_authorizes():
    """bash-agent-inject FAILS OPEN on timeout, so MIND_SID can be absent.
    An absent SID must read as 'unknown owner', never as 'mine'."""
    r = wc.decide("zeta", None, ROW, MINE)
    assert r["verdict"] == "unknown-owner"
    assert "SID" in r["reason"]


def test_unreadable_owner_sid_never_authorizes():
    """A legacy pre- claim carries no claimed_by_sid. Unknown
    ownership leaves the row alone rather than guessing."""
    r = wc.decide("zeta", MINE, ROW, None)
    assert r["verdict"] == "unknown-owner"


def test_foreign_owner_is_left_alone():
    r = wc.decide("zeta", MINE, ROW, THEIRS)
    assert r["verdict"] == "not-ours"
    assert THEIRS in r["reason"]


def test_only_an_affirmative_sid_match_ever_yields_ours():
    """Exhaustive fail-safe sweep over the whole input space.

    'ours' is authorized ONLY when a real goal_id, a real local SID, and a real
    owner SID are all present AND the two SIDs are equal. Every falsy/absent/
    mismatched combination must leave the row alone. Asserting the CONDITION
    (rather than a hardcoded winner) is deliberate: the first draft asserted
    `sid == owner == MINE` and reddened on the equally-valid THEIRS==THEIRS
    match -- the test was wrong, not the code.
    """
    ours = 0
    for sid in (None, "", MINE, THEIRS):
        for row in (None, {}, {"title": "t"}, ROW):
            for owner in (None, "", MINE, THEIRS):
                v = wc.decide("zeta", sid, row, owner)["verdict"]
                assert v in ("ours", "absent", "not-ours", "unknown-owner")
                authorized = bool(row and row.get("goal_id")) and bool(sid) \
                    and bool(owner) and sid == owner
                assert (v == "ours") == authorized, (sid, row, owner, v)
                ours += int(v == "ours")
    assert ours == 2  # MINE/MINE and THEIRS/THEIRS, both against the real row


# ── run(): the clear + guard-2305 read-back ──────────────────────────────────

def _wire(monkeypatch, *, row, owner, clear_ok=True, after=None):
    reads = {"n": 0}

    def fake_read(agent):
        reads["n"] += 1
        return row if reads["n"] == 1 else after

    monkeypatch.setattr(wc, "read_in_flight", fake_read)
    monkeypatch.setattr(wc, "claiming_sid", lambda gid: owner)
    # clear_in_flight now returns the endpoint's decoded body (or None on a
    # failed call), not a bool -- the caller has to distinguish "cleared" from
    # "declined, the row had moved" ().
    monkeypatch.setattr(
        wc, "clear_in_flight",
        lambda agent, if_goal=None: ({"cleared": True, "skipped_goal_id": None}
                                     if clear_ok else None))
    # run() also calls clear_body_row, and it was NOT stubbed until  —
    # so every run of this file reached the REAL daemon against the REAL world
    # and left `agent_status.zeta.in_flight_bodies.sid-mine` in the SHARED store
    # (measured: it was sitting there, alongside two live session ids). "zeta" is
    # a resident agent on some boxes, so the residency gate inside clear_body_row
    # correctly let it through — the gate defends against phantom AGENTS, not
    # against a unit test naming a real one. Stubbed here for the same reason its
    # two siblings above are: run()'s ownership decision is what this file tests,
    # and the body-row leg is incidental to it.
    monkeypatch.setattr(wc, "clear_body_row", lambda agent, sid: "cleared")
    return reads


def test_run_clears_and_confirms_via_readback(monkeypatch):
    _wire(monkeypatch, row=ROW, owner=MINE, after=None)
    out = wc.run("zeta", MINE)
    assert out["verdict"] == "cleared"
    assert out["goal_id"] == "g-1-1"


def test_run_reports_clear_failed_when_the_call_fails(monkeypatch):
    _wire(monkeypatch, row=ROW, owner=MINE, clear_ok=False)
    assert wc.run("zeta", MINE)["verdict"] == "clear-failed"


def test_an_unparseable_body_is_not_reported_as_a_proven_clear(monkeypatch):
    """ / F-004: a 2xx we could not parse must not read as success.

    `resp.get("cleared", True)` defaults OPTIMISTIC, so before the UNPARSEABLE
    marker a bare {} was indistinguishable from {"cleared": true} and produced
    verdict "cleared" with reason "read-back confirms cleared" — for a response
    that was never decoded. The read-back cannot rescue it: after a race it
    returns None, which reads as success (run()'s own comment says so).
    """
    _wire(monkeypatch, row=ROW, owner=MINE, after=None)
    monkeypatch.setattr(wc, "clear_in_flight",
                        lambda agent, if_goal=None: {wc.UNPARSEABLE: True})
    out = wc.run("zeta", MINE)
    assert out["verdict"] == "clear-unverified", (
        "an unparseable body was reported as a proven verdict")
    assert "unparseable" in out["reason"]


def test_a_well_formed_body_still_reports_a_proven_clear(monkeypatch):
    """Positive control for the test above: the marker must not make the
    ordinary success path ambiguous."""
    _wire(monkeypatch, row=ROW, owner=MINE, after=None)
    assert wc.run("zeta", MINE)["verdict"] == "cleared"


def test_run_reports_clear_failed_when_readback_still_shows_the_row(monkeypatch):
    """guard-2305: a plain success from the write is NOT evidence it landed."""
    _wire(monkeypatch, row=ROW, owner=MINE, clear_ok=True, after=dict(ROW))
    assert wc.run("zeta", MINE)["verdict"] == "clear-failed"


def test_run_does_not_clear_when_owner_differs(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(wc, "read_in_flight", lambda a: ROW)
    monkeypatch.setattr(wc, "claiming_sid", lambda g: THEIRS)

    def boom(agent, if_goal=None):
        called["n"] += 1
        return {"cleared": True, "skipped_goal_id": None}

    monkeypatch.setattr(wc, "clear_in_flight", boom)
    assert wc.run("zeta", MINE)["verdict"] == "not-ours"
    assert called["n"] == 0, "must never issue a clear for another Body's row"


def test_run_does_not_clear_when_the_row_is_absent(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(wc, "read_in_flight", lambda a: None)
    monkeypatch.setattr(wc, "claiming_sid", lambda g: MINE)
    monkeypatch.setattr(
        wc, "clear_in_flight",
        lambda a, if_goal=None: called.__setitem__("n", called["n"] + 1) or {})
    assert wc.run("zeta", MINE)["verdict"] == "absent"
    assert called["n"] == 0


# ── The TOCTOU the ownership test alone never closed () ─────────────
#
# These drive the REAL shared modifier (`make_clear_in_flight_modifier`, the
# same object both twins install into `locked_modify_yaml`) through a fake
# endpoint, so the guard under test is the production one rather than a
# restatement of it. `decide()` is deliberately NOT involved: it answers
# correctly here and always did -- the defect lived in the gap between its
# snapshot and the write.


def _endpoint(live_row):
    """A stand-in for POST /v1/team-state/clear-in-flight over a mutable row.

    Applies the real modifier, exactly as the endpoint does inside the row
    lock, and returns the same body shape.
    """
    import _team_state as ts

    def _call(agent, if_goal=None):
        status = {}
        mod = ts.make_clear_in_flight_modifier("tester", if_goal=if_goal,
                                               status=status)
        live_row["row"] = mod(live_row["row"])
        return {"ok": True, "agent": agent, "cleared": status["cleared"],
                "skipped_goal_id": status["skipped_goal_id"]}

    return _call


def test_a_sibling_claim_landing_mid_window_survives_the_clear(monkeypatch):
    """THE regression test for .

    Interleaving: we read in_flight and confirm the row is ours, and BEFORE the
    write lands a reducer claims a different goal into the same agent-keyed row.
    The foreign row must survive. Remove `if_goal` from the clear call (or drop
    the comparison from the modifier) and this fails -- the reducer's row is
    blanked and the caller still reports success, which is the original bug.
    """
    live = {"row": {"in_flight": dict(ROW)}}
    monkeypatch.setattr(wc, "claiming_sid", lambda gid: MINE)
    monkeypatch.setattr(wc, "clear_in_flight", _endpoint(live))

    reads = {"n": 0}
    foreign = {"goal_id": "g-9-9", "title": "reducer work", "phase": "4"}

    def racing_read(agent):
        reads["n"] += 1
        if reads["n"] == 1:
            return dict(ROW)          # ownership read: the row is still ours
        live["row"]["in_flight"] = dict(foreign)   # the reducer claims, mid-window
        return dict(foreign)

    monkeypatch.setattr(wc, "read_in_flight", racing_read)
    # The reducer's claim lands after our ownership read but before the write.
    live["row"]["in_flight"] = dict(foreign)

    out = wc.run("zeta", MINE)

    assert live["row"]["in_flight"] == foreign, (
        "the reducer's live in_flight row was destroyed by a worker close")
    assert out["verdict"] == "raced"
    assert "g-9-9" in out["reason"]


def test_the_clear_carries_the_goal_it_verified(monkeypatch):
    """The CAS is only a guard if the verified goal actually reaches the write.

    Without this, a future edit could drop `if_goal=` at the call site and every
    other test here would still pass -- the endpoint would simply go back to
    clearing unconditionally.
    """
    seen = {}
    monkeypatch.setattr(wc, "read_in_flight", lambda a: dict(ROW))
    monkeypatch.setattr(wc, "claiming_sid", lambda g: MINE)

    def capture(agent, if_goal=None):
        seen["if_goal"] = if_goal
        return {"cleared": True, "skipped_goal_id": None}

    monkeypatch.setattr(wc, "clear_in_flight", capture)
    wc.run("zeta", MINE)
    assert seen["if_goal"] == "g-1-1", (
        "the clear must name the goal the ownership test verified")


def test_uncontended_close_still_clears_through_the_real_modifier(monkeypatch):
    """Positive control: with no race, the CAS must not block the ordinary path.

    A guard that also refuses the normal case would pass the race test above and
    still be wrong.
    """
    live = {"row": {"in_flight": dict(ROW)}}
    monkeypatch.setattr(wc, "claiming_sid", lambda gid: MINE)
    monkeypatch.setattr(wc, "clear_in_flight", _endpoint(live))

    reads = {"n": 0}

    def quiet_read(agent):
        reads["n"] += 1
        return dict(ROW) if reads["n"] == 1 else live["row"].get("in_flight")

    monkeypatch.setattr(wc, "read_in_flight", quiet_read)

    out = wc.run("zeta", MINE)
    assert out["verdict"] == "cleared"
    assert "in_flight" not in live["row"]


# ── Every 2xx that carries no CAS verdict must be marked () ─────────
#
# WHY THESE EXIST AT ALL. Every run() test above monkeypatches `clear_in_flight`
# WHOLESALE, so not one of them ever exercises its parsing -- which is exactly
# how this gap survived . That fix closed the branch that RAISES
# JSONDecodeError; two neighbouring shapes reached the same bare {} WITHOUT
# raising:
#   (a) an empty / whitespace-only / absent body, which the old `or "{}"`
#       fallback turned into a literal "{}" that parsed FINE, and
#   (b) valid JSON that is not an object (array, string, number, `null`),
#       dropped by the isinstance check.
# Both then met run()'s optimistic `resp.get("cleared", True)` and reported a
# PROVEN clear for a response no `cleared` field ever reached. Tests written
# against a fix test the fix; the neighbouring input shape is where its general
# claim goes unchecked.
#
# So these drive the REAL clear_in_flight over raw body strings -- the layer
# under test is the production parser, not a restatement of it.

NO_VERDICT_BODIES = [
    pytest.param("", id="empty"),
    pytest.param("   ", id="whitespace"),
    pytest.param(None, id="absent"),
    pytest.param("[]", id="json-array"),
    pytest.param('"a string"', id="json-string"),
    pytest.param("null", id="json-null"),
    pytest.param("not json{", id="unparseable"),
]


def _wire_raw(monkeypatch, body, *, after=None):
    """Wire run() so the REAL clear_in_flight parses `body` off the wire."""
    reads = {"n": 0}

    def fake_read(agent):
        reads["n"] += 1
        return dict(ROW) if reads["n"] == 1 else after

    monkeypatch.setattr(wc, "read_in_flight", fake_read)
    monkeypatch.setattr(wc, "claiming_sid", lambda gid: MINE)
    monkeypatch.setattr(wc._rt, "rt_call", lambda *a, **k: body)


@pytest.mark.parametrize("body", NO_VERDICT_BODIES)
def test_clear_in_flight_marks_every_body_that_carries_no_cleared_field(
        monkeypatch, body):
    """Unit-level: the parser itself must return the ambiguity marker."""
    monkeypatch.setattr(wc._rt, "rt_call", lambda *a, **k: body)
    assert wc.clear_in_flight("zeta", if_goal="g-1-1") == {wc.UNPARSEABLE: True}


@pytest.mark.parametrize("body", NO_VERDICT_BODIES)
def test_run_never_reports_a_proven_clear_without_a_cleared_field(
        monkeypatch, body):
    """End-to-end: 6 of these 7 shapes reported verdict "cleared" before the fix.

    Asserting the VERDICT rather than a substring of `reason` is deliberate --
    the goal's own check asks that reverting reddens these "for the missing
    marker specifically, not merely for a changed string".
    """
    _wire_raw(monkeypatch, body)
    out = wc.run("zeta", MINE)
    assert out["verdict"] == "clear-unverified", (
        f"body {body!r} produced verdict {out['verdict']!r} -- a proven clear "
        "asserted from a response that carried no CAS verdict")


def test_an_object_that_merely_omits_cleared_keeps_the_optimistic_default(
        monkeypatch):
    """The deliberate  call, preserved -- and the reason the fix keys
    on "did an OBJECT arrive?" rather than "did it have the field?".

    A version-skewed daemon holding an old module can send a well-formed object
    without `cleared`; daemons keep old modules until they restart. Flipping the
    default to False would report clear-failed for a perfectly good clear, so
    this shape must still read as success.
    """
    _wire_raw(monkeypatch, '{"ok": true, "agent": "zeta"}')
    assert wc.run("zeta", MINE)["verdict"] == "cleared"


@pytest.mark.parametrize("body,expected", [
    ('{"cleared": true, "skipped_goal_id": null}', "cleared"),
    ('{"cleared": false, "skipped_goal_id": null}', "clear-failed"),
])
def test_well_formed_bodies_still_map_correctly(monkeypatch, body, expected):
    """Regression guard on the two shapes that were always right: a fix that
    marked everything ambiguous would pass every test above and be useless."""
    _wire_raw(monkeypatch, body)
    assert wc.run("zeta", MINE)["verdict"] == expected


# ── local_sid(): explicit arg wins, blank never becomes an identity ──────────

def test_explicit_sid_wins_over_env(monkeypatch):
    monkeypatch.setenv("MIND_SID", THEIRS)
    assert wc.local_sid(MINE) == MINE


def test_blank_env_sid_reads_as_none(monkeypatch):
    monkeypatch.setenv("MIND_SID", "   ")
    assert wc.local_sid(None) is None


def test_env_sid_used_when_no_explicit(monkeypatch):
    monkeypatch.setenv("MIND_SID", MINE)
    assert wc.local_sid(None) == MINE


# ── CLI seam: a turn-end hook must never be blocked ─────────────────────────

def _cli(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "worker_close_in_flight_clear.py"), *args],
        capture_output=True, text=True, timeout=60,
    )


@pytest.mark.parametrize("agent", ["zeta", "no-such-agent-xyz"])
def test_cli_always_exits_zero_and_emits_json(agent):
    """Runs at a turn-end: any non-zero exit or unparseable stdout would put a
    hook failure in the path of every worker close."""
    p = _cli("--agent", agent)
    assert p.returncode == 0
    out = json.loads(p.stdout)
    # All EIGHT verdicts the module docstring documents. `raced` and
    # `clear-unverified` were missing here: the enumeration fell behind the code,
    # which is the same defect class as  itself. That omission matters
    # more after , because clear-unverified is now reachable from six
    # further real response shapes rather than from an unparseable body alone.
    assert out["verdict"] in ("absent", "not-ours", "unknown-owner", "cleared",
                             "raced", "clear-failed", "clear-unverified",
                             "error")
