"""test_goal_selector_human_blocked.py —  regression.

Pins the `human_blocked:` structured-defer class (design: zeta's g-115-1644 brief).

THE BUG (confirmed in code): a free-form `defer_reason` naming a genuinely
non-agent-provisionable block (user approve-click, legal counsel) WITHOUT a
`deferred_until` and WITHOUT a STRUCTURED_DEFER_PREFIX is governed by the 120h
fail-open TTL. Once past 120h it (a) falls through to the candidate pool in
collect_candidates AND (b) is absent from blocked[] in collect_blocked. A
human-gated block can NEVER auto-clear, so it sits permanently past-TTL: the
selector never returns all_blocked while it exists -> quiescence never fires ->
the loop spins full-cost iterations in gated plateaus.

THE FIX: `human_blocked:` is a 4th STRUCTURED_DEFER_PREFIX. goal-selector exempts
it from the 120h fall-through in BOTH collect functions:
  - collect_candidates: always excluded (never a candidate, regardless of age).
  - collect_blocked: always appended to blocked[] with a synthesized blocker_ref
    (via _synth_blocker_ref_from_structured_defer path (a)), so all_blocked can
    be asserted and quiescence C2 (blocker_ref_required) passes.

SPECIFICITY: the fix is keyed on the `human_blocked:` prefix ONLY. The other
three structured prefixes (precondition_unmet:, blocked_on_dependency, Circuit
breaker:) keep the fail-open 120h expiry — their sweeps auto-clear them, so a
past-TTL one SHOULD fall through. And a free-form (un-migrated) human-only defer
keeps the OLD behavior until the migration sweep re-prefixes it. Both pinned
below as controls.

Pattern mirrors test_goal_selector_dep_failclosed.py: build synthetic
aspirations, call collect_candidates / collect_blocked directly.
"""

from __future__ import annotations

import importlib
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

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
collect_blocked = gs.collect_blocked
synth_ref = gs._synth_blocker_ref_from_structured_defer

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT

TTL = 120.0  # defer_reason_timeout_hours under test


def _ts(hours_ago):
    """ISO local timestamp `hours_ago` hours in the past (None passes through)."""
    if hours_ago is None:
        return None
    return (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def _goal(goal_id, defer_reason=None, set_at_hours_ago=None):
    g = {
        "id": goal_id,
        "title": f"test goal {goal_id}",
        "status": "pending",
        "priority": "MEDIUM",
        "category": "framework-architecture",
        "participants": ["agent"],
        "recurring": False,
    }
    if defer_reason is not None:
        g["defer_reason"] = defer_reason
        ts = _ts(set_at_hours_ago)
        if ts is not None:
            g["defer_reason_set_at"] = ts
    return g


def _asp(goals):
    return [{"id": "asp-test", "status": "active", "priority": "MEDIUM", "goals": goals}]


def _global_ids(aspirations):
    done, live = set(), set()
    for asp in aspirations:
        if asp.get("status") != "active":
            continue
        for g in asp.get("goals", []):
            st = g.get("status")
            if st in ("completed", "decomposed"):
                done.add(g["id"])
            if st not in gs.TERMINAL_GOAL_STATUSES:
                live.add(g["id"])
    return done, live


def _candidate_ids(aspirations):
    done, live = _global_ids(aspirations)
    cands = collect_candidates(
        aspirations, source="world",
        global_done_ids=done, global_live_ids=live,
        defer_reason_timeout_hours=TTL)
    return {c["goal"]["id"] for c in cands}


def _blocked_entries(aspirations):
    done, live = _global_ids(aspirations)
    blocked = collect_blocked(
        aspirations,
        global_done_ids=done, global_live_ids=live,
        defer_reason_timeout_hours=TTL)
    return {b["goal_id"]: b for b in blocked}


# ── Acceptance criterion 1 + 5: human_blocked: past TTL -> blocked, not candidate ──

def test_human_blocked_past_ttl_excluded_from_candidates():
    asp = _asp([_goal("g-hb", "human_blocked: awaiting user approval", set_at_hours_ago=200)])
    assert "g-hb" not in _candidate_ids(asp)


def test_human_blocked_past_ttl_in_blocked_with_synth_ref():
    asp = _asp([_goal("g-hb", "human_blocked: awaiting user approval", set_at_hours_ago=200)])
    blk = _blocked_entries(asp)
    assert "g-hb" in blk, f"human_blocked past-TTL must be in blocked[], got {list(blk)}"
    ref = blk["g-hb"].get("blocker_ref")
    assert isinstance(ref, dict), f"expected synth blocker_ref dict, got {ref!r}"
    assert ref.get("type") == "resource"
    assert str(ref.get("external_id", "")).startswith("structured-defer:")
    assert ref.get("synthesized") is True


# ── Fresh + no-timestamp human_blocked: always blocked (never expires) ──

def test_human_blocked_fresh_also_blocked():
    asp = _asp([_goal("g-hb", "human_blocked: awaiting legal counsel", set_at_hours_ago=2)])
    assert "g-hb" not in _candidate_ids(asp)
    assert "g-hb" in _blocked_entries(asp)


def test_human_blocked_no_set_at_still_blocked():
    # No defer_reason_set_at: free-form would expire immediately (fail-open) and
    # leak as a candidate. human_blocked: must NOT — the missing timestamp is
    # irrelevant because it never enters the expiry branch.
    asp = _asp([_goal("g-hb", "human_blocked: self-IAM grant", set_at_hours_ago=None)])
    assert "g-hb" not in _candidate_ids(asp)
    assert "g-hb" in _blocked_entries(asp)


def test_human_blocked_case_insensitive():
    asp = _asp([_goal("g-hb", "Human_Blocked: awaiting approval", set_at_hours_ago=200)])
    assert "g-hb" not in _candidate_ids(asp)
    assert "g-hb" in _blocked_entries(asp)


# ── Specificity controls: the fix must NOT change other defer classes ──

def test_freeform_human_only_past_ttl_still_falls_through():
    # Un-migrated free-form human-only defer keeps the OLD behavior (falls
    # through to candidate pool past TTL) until the migration sweep re-prefixes
    # it. This is the bug the migration fixes — pinned so the prefix-specificity
    # of the goal-selector exemption is explicit.
    asp = _asp([_goal("g-ff", "awaiting user approval", set_at_hours_ago=200)])
    assert "g-ff" in _candidate_ids(asp)
    assert "g-ff" not in _blocked_entries(asp)


def test_other_structured_prefix_past_ttl_still_falls_through():
    # precondition_unmet: keeps the fail-open 120h expiry (its sweep auto-clears
    # it). The human_blocked: exemption must be prefix-specific, not blanket-
    # exempt every structured prefix.
    asp = _asp([_goal("g-pc", "precondition_unmet: dep-x", set_at_hours_ago=200)])
    assert "g-pc" in _candidate_ids(asp)
    assert "g-pc" not in _blocked_entries(asp)


def test_human_blocked_within_ttl_freeform_comparison():
    # Within TTL, BOTH a free-form and a human_blocked: defer are excluded from
    # candidates (free-form via valid-deferral skip, human_blocked: via the new
    # exemption). The difference only manifests PAST TTL (above).
    asp = _asp([
        _goal("g-ff", "awaiting approval", set_at_hours_ago=10),
        _goal("g-hb", "human_blocked: awaiting approval", set_at_hours_ago=10),
    ])
    cands = _candidate_ids(asp)
    assert "g-ff" not in cands
    assert "g-hb" not in cands


# ── Direct synth check: human_blocked: now yields a path-(a) blocker_ref ──

def test_synth_blocker_ref_for_human_blocked():
    g = _goal("g-hb", "human_blocked: x", set_at_hours_ago=200)
    ref = synth_ref(g)
    assert isinstance(ref, dict)
    assert ref.get("type") == "resource"
    assert str(ref.get("external_id", "")).startswith("structured-defer:")


# ── : deferred_until (path b) takes precedence over a structured-prefix
#    defer_reason (path a) when BOTH are present and deferred_until is future ──


def _future_iso(days_ahead):
    return (datetime.now() + timedelta(days=days_ahead)).strftime("%Y-%m-%dT%H:%M:%S")


def _past_iso(days_ago):
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def test_synth_deferred_until_future_wins_over_structured_prefix():
    # THE  REGRESSION: a goal carrying BOTH a structured-prefix
    # defer_reason AND a far-future deferred_until must derive
    # expires_at == deferred_until (path b), NOT set_at+120h (path a). The
    # explicit deferred_until is the authoritative structural expiry. Before the
    # fix, path (a) matched first and returned the shorter 120h expiry — already
    # PAST for long-deferred goals — tripping quiescence-gate C3 (expires_at must
    # be future) into false quiescence denial + B7 backoff churn.
    du = _future_iso(90)  # ~3 months out, far beyond set_at+120h (~5 days)
    g = _goal("g-both", "precondition_unmet: dep-x", set_at_hours_ago=1)
    g["deferred_until"] = du
    ref = synth_ref(g)
    assert isinstance(ref, dict)
    assert ref.get("expires_at") == du, (
        f"deferred_until must win: expected expires_at={du}, got {ref.get('expires_at')}")
    assert str(ref.get("external_id", "")).startswith("time-gate:"), (
        f"path (b) must fire (time-gate), got external_id={ref.get('external_id')}")
    assert not str(ref.get("external_id", "")).startswith("structured-defer:")


def test_synth_deferred_until_past_falls_through_to_structured_prefix():
    # Control: when deferred_until is already PAST, path (b) is skipped (not
    # future) and path (a) fires unchanged — the structured-prefix 120h fail-open.
    g = _goal("g-both-past", "precondition_unmet: dep-x", set_at_hours_ago=1)
    g["deferred_until"] = _past_iso(2)
    ref = synth_ref(g)
    assert isinstance(ref, dict)
    assert str(ref.get("external_id", "")).startswith("structured-defer:"), (
        f"past deferred_until must fall through to path (a), got {ref.get('external_id')}")


def test_synth_structured_prefix_no_deferred_until_unchanged():
    # Control: a structured-prefix defer with NO deferred_until still gets path
    # (a) (structured-defer, set_at+120h) — the reorder must not change this.
    g = _goal("g-a-only", "precondition_unmet: dep-x", set_at_hours_ago=1)
    ref = synth_ref(g)
    assert isinstance(ref, dict)
    assert str(ref.get("external_id", "")).startswith("structured-defer:")
    exp = datetime.fromisoformat(ref["expires_at"])
    set_at = datetime.fromisoformat(g["defer_reason_set_at"])
    delta_h = (exp - set_at).total_seconds() / 3600
    assert 119.9 < delta_h < 120.1, f"expected ~120h expiry, got {delta_h}h"


def test_synth_deferred_until_only_no_structured_prefix_unchanged():
    # Control: deferred_until future with a NON-structured defer_reason still gets
    # path (b) (time-gate) — behavior unchanged by the reorder.
    du = _future_iso(30)
    g = _goal("g-b-only", "awaiting some free-form thing", set_at_hours_ago=1)
    g["deferred_until"] = du
    ref = synth_ref(g)
    assert isinstance(ref, dict)
    assert ref.get("expires_at") == du
    assert str(ref.get("external_id", "")).startswith("time-gate:")
