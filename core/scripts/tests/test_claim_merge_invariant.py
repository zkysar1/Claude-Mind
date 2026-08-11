"""Regression tests for the  single-claim invariant in
coordination_merge._merge_goal (claimed_by / claimed_at merge rule).

Design residual of g-115-1899-a (rb-3043): the own-cloud CAS layer makes
claim WRITES conflict-safe, but the both-diverged reconcile resolved
claimed_by via the LWW base (newer last_modified wins; equal timestamps fall
to the _canon content tiebreak). Two agents claiming the same goal
near-simultaneously on different boxes therefore each "won" at their own
layer — the reconcile silently handed the goal to the SECOND claimer (or to
whichever side serialized larger), violating first-claim-wins. The claim
endpoint does NOT stamp goal.last_modified (aspirations_write.py claim()
sets only claimed_by/claimed_at), so the equal-last_modified content-tiebreak
shape is the realistic live one, not an edge case.

The fix under test: a dedicated claim-pair rule in _merge_goal —
  * both sides carry a LIVE claim by DIFFERENT agents -> first-writer-wins
    on claimed_at (older claim stands); a stamped claim beats a
    timestamp-less one (endpoint stale-take-back parity); full tie ->
    lexicographic claimed_by. claimed_by/claimed_at move as a PAIR.
  * one-side-null KEEPS the non-null claim UNLESS the null side is provably
    newer than the claim, i.e. its last_modified strictly postdates the
    claim's claimed_at (g-115-2547). claim()/release() never stamp
    last_modified, so claimed_at is the claim's recency signal: a newer null
    side is a genuine release-then-edit (claim cleared); an older-or-equal
    null side — including the both-lack-last_modified content-tiebreak that
    dropped live claims — is a stale PRE-claim snapshot (claim kept, guarding
    against cross-box double-claim). A pure release with no later edit is
    resurrected here and self-heals via the claim-timeout take-back.
  * merged NON-recurring terminal status clears the claim pair (merge-layer
    mirror of the write-path claim-clearing invariant, aspirations.py
    cmd_update_goal Rule 3).

Pure functions — no I/O, no daemon. Governing invariant remains BYTE
commutativity (guard-907): merge(a, b) == merge(b, a) exactly, plus
multiround convergence so the fenced-PUT retry loop terminates.
"""
import json
import sys
from pathlib import Path

import pytest  # noqa: F401 — harness parity with sibling suites

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coordination_merge as cm  # noqa: E402

# Fleet adoption landed (, 2026-07-16): the claim-pair rule now
# lives in coordination_merge._merge_goal on origin — these are hard
# regression tests. (History: the rule was first a cc-02-local fix,
# , superseded at the  unwedge merge; this file carried
# the spec as strict=False xfail pins until the fleet fix shipped.)


def _goal(gid="g-115-9999", **kw):
    g = {"id": gid, "status": "pending", "recurring": False,
         "last_modified": "2026-07-11T03:00:00",
         "created_at": "2026-07-11T03:00:00"}
    g.update(kw)
    return g


def _merged_pair(a, b):
    """Merge both orders; assert byte commutativity; return one merged dict."""
    ab = cm._merge_goal(a, b)
    ba = cm._merge_goal(b, a)
    assert json.dumps(ab, sort_keys=True) == json.dumps(ba, sort_keys=True), \
        "claim merge must stay byte-commutative (guard-907)"
    return ab


# --- headline race: second claimer must NOT steal the goal -------------------

def test_concurrent_claim_first_writer_wins_newer_lww_loser():
    """B claims 40s after A and carries the NEWER last_modified — the old LWW
    base handed B the goal. First-writer-wins must return it to A."""
    a = _goal(claimed_by="alpha", claimed_at="2026-07-11T08:00:00",
              status="in-progress", last_modified="2026-07-11T08:00:00")
    b = _goal(claimed_by="zeta", claimed_at="2026-07-11T08:00:40",
              status="in-progress", last_modified="2026-07-11T08:00:40")
    m = _merged_pair(a, b)
    assert m["claimed_by"] == "alpha"
    assert m["claimed_at"] == "2026-07-11T08:00:00"


def test_concurrent_claim_equal_last_modified_realistic_shape():
    """claim() does not stamp last_modified, so live concurrent claims tie on
    it and the old code fell to the _canon content tiebreak — winner was
    whichever record serialized larger, not whoever claimed first."""
    a = _goal(claimed_by="zzz-agent", claimed_at="2026-07-11T08:00:00",
              status="in-progress")
    b = _goal(claimed_by="aaa-agent", claimed_at="2026-07-11T08:02:00",
              status="in-progress")
    m = _merged_pair(a, b)
    # zzz-agent claimed FIRST — must win despite any content-size ordering.
    assert m["claimed_by"] == "zzz-agent"
    assert m["claimed_at"] == "2026-07-11T08:00:00"


def test_claim_pair_moves_together():
    """Never mix A's claimed_by with B's claimed_at."""
    a = _goal(claimed_by="alpha", claimed_at="2026-07-11T08:00:00",
              last_modified="2026-07-11T09:00:00")
    b = _goal(claimed_by="zeta", claimed_at="2026-07-11T08:30:00",
              last_modified="2026-07-11T09:30:00")
    m = _merged_pair(a, b)
    assert (m["claimed_by"], m["claimed_at"]) == ("alpha", "2026-07-11T08:00:00")


def test_claimed_at_tie_breaks_lexicographic_deterministic():
    a = _goal(claimed_by="zeta", claimed_at="2026-07-11T08:00:00")
    b = _goal(claimed_by="alpha", claimed_at="2026-07-11T08:00:00")
    m = _merged_pair(a, b)
    assert m["claimed_by"] == "alpha"  # lexicographic-smaller wins the tie
    assert m["claimed_at"] == "2026-07-11T08:00:00"


def test_stamped_claim_beats_timestampless_claim():
    """Endpoint parity: a claimed_at-less claim is legacy/manual residue the
    live endpoint treats as steal-able — the stamped claim must win."""
    a = _goal(claimed_by="ghost")  # no claimed_at
    b = _goal(claimed_by="zeta", claimed_at="2026-07-11T08:05:00")
    m = _merged_pair(a, b)
    assert m["claimed_by"] == "zeta"
    assert m["claimed_at"] == "2026-07-11T08:05:00"


# --- release semantics must keep working -------------------------------------

def test_release_newer_than_claim_stays_released():
    """A release-then-edit (claim pair popped, last_modified advanced to 09:00
    by the later edit) beats a stale live-claim snapshot: the null side is
    PROVABLY NEWER than the claim's claimed_at (08:00), so the g-115-2547 rule
    clears the claim. Outcome unchanged from the pre-g-115-2547 LWW base."""
    released = _goal(last_modified="2026-07-11T09:00:00")
    stale_claim = _goal(claimed_by="zeta", claimed_at="2026-07-11T08:00:00",
                        last_modified="2026-07-11T08:00:00")
    m = _merged_pair(released, stale_claim)
    assert "claimed_by" not in m
    assert "claimed_at" not in m


def test_reclaim_after_release_wins():
    """A claim NEWER than the release is a legitimate re-claim — it stands.
    (null side last_modified 08:30 is NOT newer than the re-claim's claimed_at
    09:00, so the claim is kept.)"""
    released = _goal(last_modified="2026-07-11T08:30:00")
    reclaim = _goal(claimed_by="alpha", claimed_at="2026-07-11T09:00:00",
                    last_modified="2026-07-11T09:00:00")
    m = _merged_pair(released, reclaim)
    assert m.get("claimed_by") == "alpha"
    assert m.get("claimed_at") == "2026-07-11T09:00:00"


# --- one-side-null claim preservation () ---------------------------

def test_one_side_null_both_lack_last_modified_keeps_claim():
    """ headline: a claim written locally, merged against a remote
    snapshot that lacks the claim AND last_modified — the realistic own-cloud
    shape, since claim() never stamps last_modified. The equal-(missing)-
    last_modified content tiebreak used to drop the LIVE claim, double-claiming
    the goal across boxes. The non-null claim must survive: the null side is
    not provably newer (both lack last_modified -> _newer(None, claimed_at) is
    False)."""
    claim = _goal(claimed_by="alpha", claimed_at="2026-07-11T08:00:00",
                  status="in-progress")
    snapshot = _goal(status="pending")            # no claim
    claim.pop("last_modified")
    snapshot.pop("last_modified")
    m = _merged_pair(claim, snapshot)
    assert m.get("claimed_by") == "alpha"
    assert m.get("claimed_at") == "2026-07-11T08:00:00"


def test_one_side_null_older_snapshot_keeps_claim():
    """A stale PRE-claim snapshot is not a release. The comparison is null-side
    last_modified vs the claim's CLAIMED_AT (not vs the claim side's pre-claim
    last_modified): snapshot lm 07:30 is newer than the claim side's pre-claim
    lm 07:00 but OLDER than claimed_at 08:00, so it is still a pre-claim
    snapshot and the claim is kept. Guards the 'compare against claimed_at'
    design decision — comparing against the claim side's last_modified would
    wrongly drop this live claim."""
    claim = _goal(claimed_by="alpha", claimed_at="2026-07-11T08:00:00",
                  status="in-progress", last_modified="2026-07-11T07:00:00")
    snapshot = _goal(status="pending", last_modified="2026-07-11T07:30:00")
    m = _merged_pair(claim, snapshot)
    assert m.get("claimed_by") == "alpha"
    assert m.get("claimed_at") == "2026-07-11T08:00:00"


def test_same_claimer_both_sides_untouched():
    """Same agent on both sides, NEITHER carrying a body id, is not a race —
    base LWW rides.

    Narrowed by g-306-132-c: same-agent is no longer unconditionally
    non-racing. Two BODIES of one agent (same claimed_by, different
    claimed_by_sid) DO conflict — see the same-agent-different-body section
    below. This case stays untouched because with no sids on either side the
    merge cannot tell two bodies from one body's own re-write, and inventing
    a conflict from incomplete data is the unsafe direction."""
    a = _goal(claimed_by="zeta", claimed_at="2026-07-11T08:00:00",
              last_modified="2026-07-11T08:00:00")
    b = _goal(claimed_by="zeta", claimed_at="2026-07-11T08:00:05",
              last_modified="2026-07-11T08:10:00")
    m = _merged_pair(a, b)
    assert m["claimed_by"] == "zeta"
    assert m["claimed_at"] == "2026-07-11T08:00:05"  # base (newer LWW) rides


# --- same agent, DIFFERENT body (-c, trace T5) ----------------------
#
# Two boxes running the SAME agent used to merge silently: `ca_by != cb_by` was
# False and the one-side-null elif was False too (both truthy), so the pair rode
# the LWW base and BOTH bodies kept their own view and executed the goal.
# guard-1460 is the read-side statement of the same defect.
#
# Every case below goes through _merged_pair, which merges in BOTH operand
# orders and asserts byte-commutativity — the "deterministic winner in BOTH
# operand orders" this fix is verified against.

SID_A = "11111111-1111-1111-1111-111111111111"
SID_B = "22222222-2222-2222-2222-222222222222"


def test_same_agent_different_body_older_claim_wins():
    """The headline: one mind, two bodies. Older claimed_at takes the goal."""
    a = _goal(claimed_by="omni", claimed_at="2026-08-03T08:00:00",
              claimed_by_sid=SID_A, status="in-progress",
              last_modified="2026-08-03T08:00:00")
    # Body B claims later but carries the NEWER last_modified — the shape that
    # let the LWW base hand it the goal.
    b = _goal(claimed_by="omni", claimed_at="2026-08-03T08:00:40",
              claimed_by_sid=SID_B, status="in-progress",
              last_modified="2026-08-03T08:00:40")
    m = _merged_pair(a, b)
    assert m["claimed_at"] == "2026-08-03T08:00:00"
    assert m["claimed_by_sid"] == SID_A, "the older body must hold the goal"


def test_same_agent_different_body_claimed_at_tie_is_deterministic():
    """Same instant: claimed_by carries no ordering information (it is
    identical), so the tie must break on the body id or the merge is not a
    function of its inputs."""
    a = _goal(claimed_by="omni", claimed_at="2026-08-03T08:00:00",
              claimed_by_sid=SID_B)
    b = _goal(claimed_by="omni", claimed_at="2026-08-03T08:00:00",
              claimed_by_sid=SID_A)
    m = _merged_pair(a, b)
    assert m["claimed_by_sid"] == SID_A  # lexicographic-smaller sid wins


def test_same_agent_one_sided_sid_is_not_a_conflict():
    """Only one side carries a body id — most likely one body merged against
    its own pre-sid snapshot (claimed_by_sid postdates the claim schema).
    Manufacturing a conflict here would hand the goal to whichever copy
    happens to carry the field."""
    a = _goal(claimed_by="omni", claimed_at="2026-08-03T08:00:00",
              claimed_by_sid=SID_A, last_modified="2026-08-03T08:00:00")
    b = _goal(claimed_by="omni", claimed_at="2026-08-03T08:00:05",
              last_modified="2026-08-03T08:10:00")
    m = _merged_pair(a, b)
    assert m["claimed_by"] == "omni"
    assert m["claimed_at"] == "2026-08-03T08:00:05"  # base LWW rides, as before


def test_same_agent_same_body_is_not_a_conflict():
    """One body re-writing its own claim must stay on the LWW base."""
    a = _goal(claimed_by="omni", claimed_at="2026-08-03T08:00:00",
              claimed_by_sid=SID_A, last_modified="2026-08-03T08:00:00")
    b = _goal(claimed_by="omni", claimed_at="2026-08-03T08:00:05",
              claimed_by_sid=SID_A, last_modified="2026-08-03T08:10:00")
    m = _merged_pair(a, b)
    assert m["claimed_at"] == "2026-08-03T08:00:05"
    assert m["claimed_by_sid"] == SID_A


# --- claimed_by_sid is part of the claim UNIT --------------------------------
#
# Before -c the field was not merged at all: it rode `out = dict(win)`
# from the LWW base. Whenever the conflict winner was not the LWW winner, the
# merged record paired the WINNER's claimed_by/claimed_at with the LOSER's body
# id — a claim attributed to a session that never made it. Reachable in the
# pre-existing different-AGENT branch too, not only the new same-agent one.

def test_sid_travels_with_the_pair_across_agents():
    a = _goal(claimed_by="alpha", claimed_at="2026-08-03T08:00:00",
              claimed_by_sid=SID_A, last_modified="2026-08-03T08:00:00")
    b = _goal(claimed_by="zeta", claimed_at="2026-08-03T08:00:40",
              claimed_by_sid=SID_B, last_modified="2026-08-03T08:00:40")
    m = _merged_pair(a, b)
    assert (m["claimed_by"], m["claimed_by_sid"]) == ("alpha", SID_A), \
        "winner's claimed_by must not pair with the loser's body id"


def test_terminal_status_clears_sid_with_the_pair():
    done = _goal(status="completed", last_modified="2026-08-03T08:00:00")
    live = _goal(status="in-progress", claimed_by="omni",
                 claimed_at="2026-08-03T08:05:00", claimed_by_sid=SID_A,
                 last_modified="2026-08-03T08:05:00")
    m = _merged_pair(done, live)
    assert m["status"] == "completed"
    assert "claimed_by" not in m and "claimed_by_sid" not in m, \
        "a completed goal must not carry a body id"


def test_release_clears_sid_with_the_pair():
    """Null side provably newer than the claim -> genuine release."""
    claim = _goal(claimed_by="omni", claimed_at="2026-08-03T08:00:00",
                  claimed_by_sid=SID_A, last_modified="2026-08-03T07:00:00")
    released = _goal(last_modified="2026-08-03T09:00:00")
    m = _merged_pair(claim, released)
    assert "claimed_by" not in m and "claimed_by_sid" not in m


# --- the claimed_by_sid sub-branches, mutation-pinned () ------------
#
# Fresh-eyes mutation-tested all six claimed_by_sid sub-branches of _merge_goal
# against this suite (foxtrot, msg-20260803-062524-foxtrot-5036). Only the
# conflict SET and the terminal-clear POP were killed; FOUR survived — the
# conflict POP, the release-clear POP, and BOTH halves of the release-keep
# if/else. The code was correct throughout; the suite simply could not tell.
#
# Why they survived, and the rule for reading the cases below: `out` starts as
# dict(win), the LWW winner. Every pre-existing fixture happened to put the sid
# on the side `out` was already copied from, so setting it changed nothing and
# popping it removed nothing — the mutant and the original were observationally
# equivalent under the fixture though they differ in production (guard-2219).
# So each test here puts the sid on the side `out` does NOT start from. That is
# the whole design constraint; a case that ignores it re-creates the no-op.
#
# Correction to the filing: fixture (1) was specified as killing L1091 AND
# L1093. It cannot — those are the two arms of one if/else, so any single
# fixture takes exactly one of them. Four cases, one per surviving line.

SID_C = "33333333-3333-3333-3333-333333333333"


def test_conflict_winner_without_sid_does_not_inherit_losers_sid():
    """Conflict POP. alpha claims FIRST but carries no body id (a legacy
    pre-sid claim); zeta claims later, carries SID_B, and holds the newer
    last_modified — so `out` starts from zeta's record. First-writer-wins
    hands the claim to alpha, and without the pop the merged goal reads
    claimed_by=alpha beside zeta's body id: a claim attributed to a session
    that never made it, which is the mixed pair the unit rule exists to
    prevent. Sibling of test_sid_travels_with_the_pair_across_agents, which
    covers the arm where the winner HAS a sid."""
    a = _goal(claimed_by="alpha", claimed_at="2026-08-03T08:00:00",
              status="in-progress", last_modified="2026-08-03T08:00:00")
    b = _goal(claimed_by="zeta", claimed_at="2026-08-03T08:00:40",
              claimed_by_sid=SID_B, status="in-progress",
              last_modified="2026-08-03T08:00:40")
    m = _merged_pair(a, b)
    assert m["claimed_by"] == "alpha"
    assert "claimed_by_sid" not in m, \
        "a sid-less winner must not inherit the loser's body id"


def test_release_clear_drops_a_sid_the_lww_winner_actually_holds():
    """Release-clear POP. test_release_clears_sid_with_the_pair puts the sid
    on the LWW LOSER, so `out` never held it and the pop there is a no-op.
    Here the claim side is BOTH the sid holder and the LWW winner (a later
    edit advanced its last_modified past the release), while the null side is
    still provably newer than claimed_at — so the release fires and has to
    strip a sid `out` genuinely carries. Without the pop the goal keeps the
    releasing body's id with no claim beside it."""
    claim = _goal(claimed_by="omni", claimed_at="2026-08-03T08:00:00",
                  claimed_by_sid=SID_A, status="in-progress",
                  last_modified="2026-08-03T10:00:00")
    released = _goal(status="pending", last_modified="2026-08-03T09:00:00")
    m = _merged_pair(claim, released)
    assert "claimed_by" not in m and "claimed_at" not in m
    assert "claimed_by_sid" not in m, \
        "a released goal must not keep the releasing body's id"


def test_kept_claim_carries_its_body_id_when_the_snapshot_wins_lww():
    """Release-keep SET — the consequential one. This is the 
    double-claim guard with a body id attached.
    test_one_side_null_older_snapshot_keeps_claim is exactly this shape with
    no sid anywhere in the fixture, so it exercises only the no-op half.

    Here the stale pre-claim snapshot holds the NEWER last_modified, so `out`
    starts from the record with no sid at all. Without the SET the kept claim
    reads claimed_by=omni / claimed_at=08:00 and NO body id — and a sid-less
    claim then fails _diff_body's bool(ca_sid)/bool(cb_sid) guard on the NEXT
    merge, so a second body's LATER claim wins. That is g-306-132-c
    reintroduced through the merge itself."""
    claim = _goal(claimed_by="omni", claimed_at="2026-08-03T08:00:00",
                  claimed_by_sid=SID_A, status="in-progress",
                  last_modified="2026-08-03T07:00:00")
    snapshot = _goal(status="pending", last_modified="2026-08-03T07:30:00")
    m = _merged_pair(claim, snapshot)
    assert m["claimed_by"] == "omni"
    assert m["claimed_at"] == "2026-08-03T08:00:00"
    assert m["claimed_by_sid"] == SID_A, \
        "the kept claim must keep its body id, or the next merge cannot see it"


def test_kept_claim_without_sid_strips_a_residue_sid_from_the_snapshot():
    """Release-keep POP. The claim is legacy/pre-sid; the stale snapshot
    carries a leftover body id and wins LWW, so `out` holds a sid the kept
    claim never had. Without the pop the merge MANUFACTURES the mixed pair —
    claimed_by=omni beside a body id that claimed nothing.

    A claim-less sid is residue rather than anything a writer emits today
    (claim/release/stale-take-back all move the three fields together). It is
    reachable precisely because this pop is what prevents it: _merge_goal is
    applied repeatedly against its own output (see the convergence tests), so
    a merge that preserves the residue keeps re-preserving it, and one box
    that produced it propagates it. A total merge must be closed over the
    shapes it can itself emit."""
    claim = _goal(claimed_by="omni", claimed_at="2026-08-03T08:00:00",
                  status="in-progress", last_modified="2026-08-03T07:00:00")
    snapshot = _goal(status="pending", claimed_by_sid=SID_C,
                     last_modified="2026-08-03T07:30:00")
    m = _merged_pair(claim, snapshot)
    assert m["claimed_by"] == "omni"
    assert m["claimed_at"] == "2026-08-03T08:00:00"
    assert "claimed_by_sid" not in m, \
        "a sid-less claim must not pick up a residue body id"


# --- terminal status clears the claim pair (write-path Rule 3 mirror) --------

def test_nonrecurring_terminal_clears_claim_pair():
    """Side A completed (claim popped at write time); side B holds a NEWER
    live claim. Terminal dominates status; the claim pair must not ride
    back onto the completed goal."""
    done = _goal(status="completed", last_modified="2026-07-11T08:00:00")
    claim = _goal(claimed_by="zeta", claimed_at="2026-07-11T09:00:00",
                  status="in-progress", last_modified="2026-07-11T09:00:00")
    m = _merged_pair(done, claim)
    assert m["status"] == "completed"
    assert "claimed_by" not in m
    assert "claimed_at" not in m


def test_recurring_claim_survives_terminal_cycle():
    """Recurring goals CYCLE through completed -> pending; status rides the
    LWW base and the claim pair must NOT be force-cleared for them."""
    done = _goal(status="completed", recurring=True,
                 last_modified="2026-07-11T08:00:00")
    claim = _goal(claimed_by="zeta", claimed_at="2026-07-11T09:00:00",
                  status="in-progress", recurring=True,
                  last_modified="2026-07-11T09:00:00")
    m = _merged_pair(done, claim)
    assert m.get("claimed_by") == "zeta"  # base LWW (newer side) rides


# --- convergence --------------------------------------------------------------

def test_multiround_convergence():
    """Re-merging the merged record against either input is a fixed point —
    the fenced-PUT retry loop must terminate."""
    a = _goal(claimed_by="alpha", claimed_at="2026-07-11T08:00:00",
              status="in-progress", last_modified="2026-07-11T08:00:00")
    b = _goal(claimed_by="zeta", claimed_at="2026-07-11T08:00:40",
              status="in-progress", last_modified="2026-07-11T08:00:40")
    m1 = _merged_pair(a, b)
    m2 = _merged_pair(m1, b)
    m3 = _merged_pair(m1, a)
    assert json.dumps(m1, sort_keys=True) == json.dumps(m2, sort_keys=True)
    assert json.dumps(m1, sort_keys=True) == json.dumps(m3, sort_keys=True)


def test_terminal_clear_multiround_convergence():
    done = _goal(status="completed", last_modified="2026-07-11T08:00:00")
    claim = _goal(claimed_by="zeta", claimed_at="2026-07-11T09:00:00",
                  status="in-progress", last_modified="2026-07-11T09:00:00")
    m1 = _merged_pair(done, claim)
    m2 = _merged_pair(m1, claim)
    assert json.dumps(m1, sort_keys=True) == json.dumps(m2, sort_keys=True)
