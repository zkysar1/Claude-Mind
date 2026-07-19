"""Regression tests for the 8 single-claim invariant in
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

# Fleet adoption landed (6, 2026-07-16): the claim-pair rule now
# lives in coordination_merge._merge_goal on origin — these are hard
# regression tests. (History: the rule was first a cc-02-local fix,
# 8, superseded at the 2 unwedge merge; this file carried
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


# --- one-side-null claim preservation (7) ---------------------------

def test_one_side_null_both_lack_last_modified_keeps_claim():
    """7 headline: a claim written locally, merged against a remote
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
    """Same agent on both sides is not a race — base LWW rides."""
    a = _goal(claimed_by="zeta", claimed_at="2026-07-11T08:00:00",
              last_modified="2026-07-11T08:00:00")
    b = _goal(claimed_by="zeta", claimed_at="2026-07-11T08:00:05",
              last_modified="2026-07-11T08:10:00")
    m = _merged_pair(a, b)
    assert m["claimed_by"] == "zeta"
    assert m["claimed_at"] == "2026-07-11T08:00:05"  # base (newer LWW) rides


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
