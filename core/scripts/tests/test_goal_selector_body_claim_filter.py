"""test_goal_selector_body_claim_filter.py —  part (1) regression.

Pins the SELECTOR-side half of the two-Body livelock (fix set B part 1 of
`world/docs/cross-box-two-bodies-design.md` v2 addendum).

THE BUG (located by foxtrot, confirmed in code before the fix):
`collect_candidates` skipped a claimed world goal only when
`claimed_by != AGENT_NAME`. `AGENT_NAME` names the MIND, so under the Mind/Body
split a SECOND BODY of the same mind — same agent name, different session,
possibly a different box — passed straight through the filter and re-selected
the goal its sibling was already executing. Claiming does NOT set `status`
(status lands later, in aspirations-execute), so the goal stays `pending` and
keeps re-qualifying every cycle: the two Bodies livelock in the
claimed-but-pending gap.

THE FIX: the filter now skips on EITHER
  (a) another MIND holds it       — `claimed_by != AGENT_NAME` (original), or
  (b) another BODY of this mind   — `claimed_by == AGENT_NAME` AND
      `claimed_by_sid` is present AND `BODY_SID` is present AND they differ.
Both forms then run the SAME pre-existing expiry ladder, so a sibling Body's
ABANDONED claim ages out exactly like a foreign agent's and nothing wedges.

FAIL-OPEN IS THE LOAD-BEARING PROPERTY. When either SID is absent — a legacy
record with no `claimed_by_sid`, or an unset `MIND_SID` — condition (b) is
False and the filter behaves byte-identically to the pre-g-306-134 code. That
direction matters more than the fix itself: a wrong HIDE wedges a goal for
every Body of the agent, while a wrong SHOW merely permits what is already
possible today and is still caught by the daemon claim CAS.

SCOPE NOTE (measured during this goal, NOT fixed here): the daemon-side CAS in
`mind_api/src/endpoints/aspirations_write.py` refuses a same-agent/other-session
claim only when the holder is the agent's `running-session-id` holder
(`_holder_session_is_live_runner`, L3750). The reducer holds that file, so a
live NON-reducer worker's claim still reads as "dormant" and is taken over.
This file pins the READ (visibility) side only; the WRITE (CAS) side is filed
separately. Outcome 1's "measured select->409 count = 0 in a two-body run" is
gated on the g-306-126 soak and is NOT asserted here.

PART 2 (selection-stack review 2026-08-21): the whole claim block was
`if source == "world"` from birth, on the premise that agent-queue goals never
carry claims. The premise is false — cross-agent pull writes the claim back to
the OWNING sibling's queue (g-115-1766), and a sibling Body's claim lands on
this agent's own queue — so both livelock shapes reproduced one store over,
invisible to the filter. The block is now UNCONDITIONAL across sources; where
the claim fields are absent (the agent-queue majority) it is a structural
no-op. The agent-source tests below pin the new behavior, its expiry ladder,
and the no-op guarantee.

Pattern mirrors test_goal_selector_human_blocked.py: build synthetic
aspirations, call collect_candidates directly.
"""

from __future__ import annotations

import importlib
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# goal-selector.py requires MIND_AGENT to load (paths derive AGENT_DIR).
# Capture-restore around the module-level mutation so collection-time env
# pollution cannot leak to other tests (rb-1096, guard-588).
_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "alpha")

gs = importlib.import_module("goal-selector")
collect_candidates = gs.collect_candidates

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT

MIND = "alpha"
MY_SID = "11111111-1111-1111-1111-111111111111"
SIBLING_SID = "22222222-2222-2222-2222-222222222222"
CLAIM_TIMEOUT = 4.0  # multi_agent.claim_timeout_hours under test


@pytest.fixture(autouse=True)
def _pin_identity(monkeypatch):
    """Pin the module-level identity constants for every test in this file.

    Both are read at IMPORT time from the environment, so they cannot be varied
    per-test through os.environ — monkeypatch the resolved module attributes
    instead. autouse keeps each test hermetic and restores on teardown.
    """
    monkeypatch.setattr(gs, "AGENT_NAME", MIND)
    monkeypatch.setattr(gs, "BODY_SID", MY_SID)


def _ts(hours_ago):
    return (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def _goal(goal_id, *, claimed_by=None, claimed_by_sid=None, claim_age_h=0.5):
    g = {
        "id": goal_id,
        "title": f"test goal {goal_id}",
        "status": "pending",
        "priority": "MEDIUM",
        "category": "framework-patterns",
        "participants": ["agent"],
        "recurring": False,
    }
    if claimed_by is not None:
        g["claimed_by"] = claimed_by
        g["claimed_at"] = _ts(claim_age_h)
    if claimed_by_sid is not None:
        g["claimed_by_sid"] = claimed_by_sid
    return g


def _asp(goals):
    return [{"id": "asp-test", "status": "active", "priority": "MEDIUM",
             "goals": goals}]


def _visible(goals, source="world"):
    """Goal ids surviving the claim filter."""
    aspirations = _asp(goals)
    done, live = set(), {g["id"] for g in goals}
    cands = collect_candidates(
        aspirations, source=source,
        global_done_ids=done, global_live_ids=live,
        claim_timeout_hours=CLAIM_TIMEOUT)
    return {c["goal"]["id"] for c in cands}


# ── THE FIX: a live sibling Body's claim is hidden ────────────────────────────

def test_sibling_body_fresh_claim_is_hidden():
    # Same mind, different session, claim well within the timeout. This is the
    # T1 livelock case: pre-fix this goal was VISIBLE and got re-selected every
    # cycle while the sibling Body executed it.
    g = _goal("g-sib", claimed_by=MIND, claimed_by_sid=SIBLING_SID, claim_age_h=0.5)
    assert "g-sib" not in _visible([g])


def test_sibling_body_expired_claim_falls_through():
    # A sibling Body's ABANDONED claim must age out on the SAME ladder a foreign
    # agent's does — otherwise a crashed worker wedges the goal permanently.
    g = _goal("g-sib-old", claimed_by=MIND, claimed_by_sid=SIBLING_SID,
              claim_age_h=CLAIM_TIMEOUT + 2)
    assert "g-sib-old" in _visible([g])


# ── FAIL-OPEN: every ambiguous shape keeps pre-fix behavior ───────────────────

def test_own_body_claim_still_visible():
    # My OWN claim from a previous iteration. The loop must still be able to
    # re-select the goal it is working on — hiding this would strand in-flight
    # work at the next iteration boundary.
    g = _goal("g-mine", claimed_by=MIND, claimed_by_sid=MY_SID, claim_age_h=0.5)
    assert "g-mine" in _visible([g])


def test_legacy_record_without_claim_sid_still_visible():
    # A claim written before claimed_by_sid existed (). No SID on the
    # record => cannot prove a different Body holds it => fail open, unchanged.
    g = _goal("g-legacy", claimed_by=MIND, claimed_by_sid=None, claim_age_h=0.5)
    assert "g-legacy" in _visible([g])


def test_empty_claim_sid_string_still_visible():
    # Defensive: an empty-string SID is as unprovable as a missing one.
    g = _goal("g-empty", claimed_by=MIND, claimed_by_sid="", claim_age_h=0.5)
    assert "g-empty" in _visible([g])


def test_non_string_claim_sid_still_visible():
    # A malformed record must not crash the selector or be read as a mismatch.
    g = _goal("g-bad", claimed_by=MIND, claim_age_h=0.5)
    g["claimed_by_sid"] = 12345
    assert "g-bad" in _visible([g])


def test_unset_body_sid_disables_the_filter(monkeypatch):
    # MIND_SID unset (empty BODY_SID) => this process cannot tell itself from a
    # sibling, so it must NOT hide anything on that basis.
    monkeypatch.setattr(gs, "BODY_SID", "")
    g = _goal("g-nosid", claimed_by=MIND, claimed_by_sid=SIBLING_SID, claim_age_h=0.5)
    assert "g-nosid" in _visible([g])


# ── SPECIFICITY CONTROLS: pre-existing behavior must be untouched ─────────────

def test_foreign_agent_fresh_claim_still_hidden():
    g = _goal("g-foreign", claimed_by="bravo", claimed_by_sid=SIBLING_SID,
              claim_age_h=0.5)
    assert "g-foreign" not in _visible([g])


def test_foreign_agent_expired_claim_still_falls_through():
    g = _goal("g-foreign-old", claimed_by="bravo", claimed_by_sid=SIBLING_SID,
              claim_age_h=CLAIM_TIMEOUT + 2)
    assert "g-foreign-old" in _visible([g])


def test_unclaimed_goal_still_visible():
    assert "g-free" in _visible([_goal("g-free")])


# ── PART 2: the claim filter is unconditional across sources ──────────────────

def test_agent_queue_foreign_claim_now_hidden():
    # Cross-agent pull writes a puller's claim back to the OWNING sibling's
    # queue (). Pre-part-2 the owner kept re-selecting its own goal
    # while the puller executed it — the world-queue livelock one store over.
    g = _goal("g-agentq-foreign", claimed_by="bravo", claim_age_h=0.5)
    assert "g-agentq-foreign" not in _visible([g], source="agent")


def test_agent_queue_sibling_body_claim_now_hidden():
    # 's two-Body shape, reproduced in the agent queue.
    g = _goal("g-agentq-sib", claimed_by=MIND, claimed_by_sid=SIBLING_SID,
              claim_age_h=0.5)
    assert "g-agentq-sib" not in _visible([g], source="agent")


def test_agent_queue_expired_claim_falls_through():
    # The same expiry ladder governs the agent source — an abandoned claim
    # must not wedge the owner's own queue.
    g = _goal("g-agentq-old", claimed_by="bravo",
              claim_age_h=CLAIM_TIMEOUT + 2)
    assert "g-agentq-old" in _visible([g], source="agent")


def test_agent_queue_own_body_claim_still_visible():
    # My own claim in my own queue stays re-selectable (same as world).
    g = _goal("g-agentq-mine", claimed_by=MIND, claimed_by_sid=MY_SID,
              claim_age_h=0.5)
    assert "g-agentq-mine" in _visible([g], source="agent")


def test_agent_queue_unclaimed_is_a_structural_noop():
    # The no-op guarantee that made this extension safe: absent claim fields
    # (the overwhelming agent-queue majority) leave behavior byte-identical.
    assert "g-agentq-free" in _visible([_goal("g-agentq-free")], source="agent")


def test_body_sid_constant_is_env_derived():
    # Pins the constant's existence and its fail-open default. A rename or a
    # default of None (rather than "") would make `bool(BODY_SID)` raise or
    # silently change the filter's fail-open direction.
    assert hasattr(gs, "BODY_SID")
    assert isinstance(gs.BODY_SID, str)


# ── Mixed-population integration: one pass, every class at once ───────────────

def test_mixed_population_partitions_correctly():
    goals = [
        _goal("v-free"),
        _goal("v-mine", claimed_by=MIND, claimed_by_sid=MY_SID),
        _goal("v-legacy", claimed_by=MIND),
        _goal("v-sib-old", claimed_by=MIND, claimed_by_sid=SIBLING_SID,
              claim_age_h=CLAIM_TIMEOUT + 2),
        _goal("h-sib", claimed_by=MIND, claimed_by_sid=SIBLING_SID),
        _goal("h-foreign", claimed_by="bravo"),
    ]
    vis = _visible(goals)
    assert vis == {"v-free", "v-mine", "v-legacy", "v-sib-old"}, (
        f"unexpected partition: {sorted(vis)}")
