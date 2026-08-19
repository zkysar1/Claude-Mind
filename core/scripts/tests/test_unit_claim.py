"""Two-Body simulation for the unit-level claim ().

Verification item (b) of that goal requires a mechanism "by which a second Body
starting the same unit is refused or warned BEFORE it writes code, demonstrated
on a real two-Body sequence or a test that simulates one". This file is that
simulation, replayed from the measured incident: two alpha Bodies, one goal
(g-326-422), one unit (reflection_focal_points.j2), built concurrently — PR #32
landed while PR #33 was still building the same template, and #33 was closed as
a duplicate.

`decide()` and `live_claims()` are pure, so the whole sequence runs without a
board, a daemon, or a second box. The records fed in are the real board record
shape (id / author / session_id / timestamp / type / text / tags).

TWO INVARIANTS PULL IN OPPOSITE DIRECTIONS and both are pinned:
  * a second Body on the SAME unit must be REFUSED — that is the point;
  * a second Body on a DIFFERENT unit, the SAME Body re-entering, and any
    EXPIRED claim must all pass — an over-blocking gate would stall the very
    multi-unit goals it exists to protect, and the goal is 11 units wide.

Non-vacuity is proven explicitly (guard-2435: a control that cannot fail is not
a control): the refusal is shown to depend on the claim record actually being
present, not on some unconditional deny.

guard-1165: no module-level os.environ mutation and no sys.modules stubs.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from unit_claim import (  # noqa: E402
    DEFAULT_LEASE_HOURS,
    decide,
    live_claims,
    parse_marker,
)

# The incident, verbatim.
GOAL = "g-326-422"
UNIT = "reflection_focal_points.j2"
BODY_A = "d1aec55b-2316-426c-8ada-0fd2e80c00eb"
BODY_B = "cd5fd3b9-5b97-439a-9914-196c1c8f5c00"

NOW = datetime(2026, 8, 19, 4, 0, 0)
LEASE = DEFAULT_LEASE_HOURS


def rec(verb, goal, unit, sid, when, *, author="alpha", msg_id="msg-x"):
    """One board record in the real stored shape."""
    return {
        "id": msg_id,
        "author": author,
        "session_id": sid,
        "timestamp": when.strftime("%Y-%m-%dT%H:%M:%S"),
        "channel": "coordination",
        "type": "claim" if verb == "CLAIM" else "release",
        "text": f"UNIT-{verb} goal={goal} unit={unit}",
        "reply_to": None,
        "tags": ["unit-claim", goal],
    }


def verdict(records, *, sid=BODY_B, goal=GOAL, unit=UNIT, now=NOW):
    return decide(records, goal_id=goal, unit=unit, my_sid=sid,
                  now=now, lease_hours=LEASE)["verdict"]


# ---------------------------------------------------------------------------
# The sequence that was measured
# ---------------------------------------------------------------------------

def test_second_body_same_unit_is_refused():
    """THE incident. Body A holds the unit; Body B must not start it."""
    board = [rec("CLAIM", GOAL, UNIT, BODY_A, NOW - timedelta(minutes=20))]
    assert verdict(board, sid=BODY_B) == "held"


def test_refusal_is_not_vacuous():
    """The same call with the claim REMOVED must pass.

    Without this, a mechanism that refused unconditionally would satisfy the
    test above and be worse than useless — it would block all 11 units.
    """
    assert verdict([], sid=BODY_B) == "free"


def test_second_body_different_unit_is_free():
    """Over-blocking is the failure mode that would stall a multi-unit goal."""
    board = [rec("CLAIM", GOAL, UNIT, BODY_A, NOW - timedelta(minutes=20))]
    assert verdict(board, sid=BODY_B, unit="hypothesis_discovery.j2") == "free"


def test_same_unit_on_a_different_goal_is_free():
    """Keys are (goal, unit). A same-named unit elsewhere is unrelated work."""
    board = [rec("CLAIM", GOAL, UNIT, BODY_A, NOW - timedelta(minutes=20))]
    assert verdict(board, sid=BODY_B, goal="g-115-5205") == "free"


def test_holder_re_entering_is_already_mine_not_refused():
    """An autocompact resume re-enters the loop mid-iteration and re-runs this.

    Refusing the holder its own unit would make the mechanism fire hardest
    against the Body that is correctly doing the work.
    """
    board = [rec("CLAIM", GOAL, UNIT, BODY_A, NOW - timedelta(minutes=20))]
    assert verdict(board, sid=BODY_A) == "already-mine"


# ---------------------------------------------------------------------------
# Lease + release
# ---------------------------------------------------------------------------

def test_expired_claim_does_not_wedge_the_unit():
    """A dead Body must not hold a unit forever."""
    board = [rec("CLAIM", GOAL, UNIT, BODY_A, NOW - timedelta(hours=LEASE + 1))]
    assert verdict(board, sid=BODY_B) == "free"


def test_claim_just_inside_the_lease_still_holds():
    """Boundary, so the expiry test above cannot pass for the wrong reason."""
    board = [rec("CLAIM", GOAL, UNIT, BODY_A, NOW - timedelta(hours=LEASE - 0.1))]
    assert verdict(board, sid=BODY_B) == "held"


def test_release_by_the_holder_frees_the_unit():
    board = [
        rec("CLAIM", GOAL, UNIT, BODY_A, NOW - timedelta(minutes=40)),
        rec("RELEASE", GOAL, UNIT, BODY_A, NOW - timedelta(minutes=10)),
    ]
    assert verdict(board, sid=BODY_B) == "free"


def test_release_by_a_different_body_does_not_free_the_unit():
    """Otherwise a peer could free a live unit and re-create the collision."""
    board = [
        rec("CLAIM", GOAL, UNIT, BODY_A, NOW - timedelta(minutes=40)),
        rec("RELEASE", GOAL, UNIT, BODY_B, NOW - timedelta(minutes=10)),
    ]
    assert verdict(board, sid=BODY_B) == "held"


def test_reclaim_after_release_holds_again():
    """A release older than the newest claim is spent, not a standing free."""
    board = [
        rec("CLAIM", GOAL, UNIT, BODY_A, NOW - timedelta(minutes=90)),
        rec("RELEASE", GOAL, UNIT, BODY_A, NOW - timedelta(minutes=60)),
        rec("CLAIM", GOAL, UNIT, BODY_A, NOW - timedelta(minutes=30)),
    ]
    assert verdict(board, sid=BODY_B) == "held"


def test_a_peers_release_cannot_resurrect_a_shadowed_live_claim():
    """[A claims, B claims anyway, B releases] must still read HELD — by A.

    Newest-claim-only bookkeeping resolves this to "free": B's release cancels
    the record that shadowed A's, and A's still-live claim was already
    discarded. The unit then reads free while A is mid-build — the exact
    duplicate this module prevents. B's claim here is what --force or a sync
    race produces, so the sequence is reachable in production.
    """
    board = [
        rec("CLAIM", GOAL, UNIT, BODY_A, NOW - timedelta(minutes=50), msg_id="a1"),
        rec("CLAIM", GOAL, UNIT, BODY_B, NOW - timedelta(minutes=30), msg_id="b1"),
        rec("RELEASE", GOAL, UNIT, BODY_B, NOW - timedelta(minutes=10), msg_id="b2"),
    ]
    live = live_claims(board, now=NOW, lease_hours=LEASE)
    holder = live.get((GOAL, UNIT))
    assert holder is not None, "A's live claim was lost when B released its own"
    assert holder["session_id"] == BODY_A
    # A itself re-entering still gets its own unit back, not a refusal.
    assert verdict(board, sid=BODY_A) == "already-mine"


def test_record_order_does_not_change_the_verdict():
    """Two boxes interleave on sync, so arrival order is not causal order."""
    records = [
        rec("CLAIM", GOAL, UNIT, BODY_A, NOW - timedelta(minutes=90), msg_id="m1"),
        rec("RELEASE", GOAL, UNIT, BODY_A, NOW - timedelta(minutes=60), msg_id="m2"),
        rec("CLAIM", GOAL, UNIT, BODY_A, NOW - timedelta(minutes=30), msg_id="m3"),
    ]
    assert verdict(records, sid=BODY_B) == "held"
    assert verdict(list(reversed(records)), sid=BODY_B) == "held"


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def test_missing_session_id_refuses_rather_than_assuming_ours():
    """Mirrors aspirations.py cmd_update_goal `_sid_unprovable`.

    If the check goes quiet whenever the caller omits the sid, unsetting
    MIND_SID defeats it entirely.
    """
    board = [rec("CLAIM", GOAL, UNIT, BODY_A, NOW - timedelta(minutes=20))]
    assert verdict(board, sid="") == "unprovable"


def test_agent_name_alone_cannot_separate_two_bodies():
    """Both Bodies post as author `alpha`; only the session id differs.

    This is why the verdict keys on session_id — an agent-name comparison is
    False for the two-body collision (the same reason _merge_goal needs
    _diff_body alongside _diff_agent).
    """
    board = [rec("CLAIM", GOAL, UNIT, BODY_A, NOW - timedelta(minutes=20),
                 author="alpha")]
    assert board[0]["author"] == "alpha"
    assert verdict(board, sid=BODY_B) == "held"


# ---------------------------------------------------------------------------
# The marker is structured, never prose (verification item (c))
# ---------------------------------------------------------------------------

def test_marker_parses():
    assert parse_marker(f"UNIT-CLAIM goal={GOAL} unit={UNIT}") == ("CLAIM", GOAL, UNIT)
    assert parse_marker(f"UNIT-RELEASE goal={GOAL} unit={UNIT}") == ("RELEASE", GOAL, UNIT)


def test_a_prose_handoff_note_is_not_a_claim():
    """The goal is explicit that a prose remedy is wrong by construction.

    A note naming the next unit is a MAGNET — it steers every reader to the same
    unit. This asserts such a note carries no claim weight here.
    """
    prose = ("reflection_focal_points.j2 (52 lines) or hypothesis_discovery.j2 "
             "(65) look like the next rungs")
    assert parse_marker(prose) is None
    board = [dict(rec("CLAIM", GOAL, UNIT, BODY_A, NOW - timedelta(minutes=20)),
                  text=prose)]
    assert live_claims(board, now=NOW, lease_hours=LEASE) == {}
    assert verdict(board, sid=BODY_B) == "free"


def test_marker_must_be_a_whole_line():
    """A marker quoted inside a sentence must not register as a claim."""
    quoted = f"I was going to post UNIT-CLAIM goal={GOAL} unit={UNIT} but did not"
    assert parse_marker(quoted) is None


def test_marker_embedded_in_a_multiline_post_is_found():
    """The real post carries the marker plus an optional note line."""
    text = f"UNIT-CLAIM goal={GOAL} unit={UNIT}\nforced: resuming after a lost sid"
    assert parse_marker(text) == ("CLAIM", GOAL, UNIT)


def test_unparseable_timestamp_KEEPS_the_claim():
    """Fail direction: an unageable claim HOLDS rather than reading free.

    Adopted from goal-pickup-coordination-check.supersede_released_claims —
    "a false yield is cheaper than a missed race". Dropping the record would
    report the unit free, failing in the duplicate-producing direction.
    `--force` clears it, an escape hatch that probe does not have.
    """
    bad = dict(rec("CLAIM", GOAL, UNIT, BODY_A, NOW), timestamp="not-a-time")
    assert (GOAL, UNIT) in live_claims([bad], now=NOW, lease_hours=LEASE)
    assert verdict([bad], sid=BODY_B) == "held"


def test_unparseable_release_supersedes_nothing():
    """A release that cannot be shown to postdate a claim must not clear it."""
    board = [
        rec("CLAIM", GOAL, UNIT, BODY_A, NOW - timedelta(minutes=30)),
        dict(rec("RELEASE", GOAL, UNIT, BODY_A, NOW), timestamp="not-a-time"),
    ]
    assert verdict(board, sid=BODY_B) == "held"


def test_a_well_formed_claim_wins_the_holder_slot_over_a_malformed_peer():
    """The malformed record still blocks, but does not misattribute the unit."""
    board = [
        dict(rec("CLAIM", GOAL, UNIT, BODY_B, NOW, msg_id="bad"), timestamp="nope"),
        rec("CLAIM", GOAL, UNIT, BODY_A, NOW - timedelta(minutes=5), msg_id="good"),
    ]
    holder = live_claims(board, now=NOW, lease_hours=LEASE)[(GOAL, UNIT)]
    assert holder["session_id"] == BODY_A
