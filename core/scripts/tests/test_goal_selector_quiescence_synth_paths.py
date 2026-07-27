"""test_goal_selector_quiescence_synth_paths.py —  regression.

Pins the extension of _synth_blocker_ref_from_structured_defer that closes the
quiescence C2/C3 coverage gap for three blocker shapes the original a/b/c paths
missed, plus the C3 roll-forward for long-lived structured defers.

THE GAP (verified live 2026-07-05: 9 C2-miss + 1 C3-expired blocked goals):
  (d) bare blocked_by, NO structured defer_reason  (g-250-124/126, g-307-10,
      g-313-04, g-335-03/05/07)                                     -> None
  (e) narrative defer_reason, no structured prefix  (g-306-24)      -> None
  (f) bare-STRING blocker_ref                       (g-312-02)      -> None
      (the string was actively DISCARDED — synth returned None, so the L1659
       call site OVERWROTE the stored string with None)
  C3: structured defer set >120h ago               (g-313-02)       -> expired ref
      (path (a) anchored expiry to set_at+120h; once past, C3 tripped forever)

THE FIX: paths (d)/(e)/(f) synth a type=resource ref (self-healing now+120h
rolling expiry); path (a) rolls its expiry forward to now+120h when the
set_at-anchored window has lapsed. All external_ids stay md5(stable-key)[:12]
so C4 hysteresis (which hashes external_ids) is preserved.

SAFETY (first-principles): path (d) synthesizes UNCONDITIONALLY for any
non-empty blocked_by — NO head-executability tracing. Safe because quiescence
only evaluates in the all-blocked state, where an agent-executable dependency
head would be a candidate and break all-blocked BEFORE quiescence is reached.

Evaluation order (first match wins): (f) bare-string, (b) deferred_until future,
(a) structured prefix, (c) rne, (d) blocked_by, (e) narrative defer.

Pattern mirrors test_goal_selector_human_blocked.py.
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


def _future_iso(days_ahead):
    return (datetime.now() + timedelta(days=days_ahead)).strftime("%Y-%m-%dT%H:%M:%S")


def _goal(goal_id, *, status="pending", defer_reason=None, set_at_hours_ago=None,
          blocked_by=None, blocker_ref=None, deferred_until=None,
          skill=None, category="framework-architecture", fire_when=None):
    g = {
        "id": goal_id, "title": f"test goal {goal_id}", "status": status,
        "priority": "MEDIUM", "category": category,
        "participants": ["agent"], "recurring": False,
    }
    if skill is not None:
        g["skill"] = skill
    if fire_when is not None:
        g["fire_when"] = fire_when
    if defer_reason is not None:
        g["defer_reason"] = defer_reason
        ts = _ts(set_at_hours_ago)
        if ts is not None:
            g["defer_reason_set_at"] = ts
    if blocked_by is not None:
        g["blocked_by"] = blocked_by
    if blocker_ref is not None:
        g["blocker_ref"] = blocker_ref
    if deferred_until is not None:
        g["deferred_until"] = deferred_until
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


def _blocked_entries(aspirations):
    done, live = _global_ids(aspirations)
    blocked = collect_blocked(
        aspirations,
        global_done_ids=done, global_live_ids=live,
        defer_reason_timeout_hours=TTL)
    return {b["goal_id"]: b for b in blocked}


def _is_future(ref):
    exp = ref.get("expires_at")
    return bool(exp) and datetime.fromisoformat(str(exp)) > datetime.now()


# ── Path (f): bare-STRING blocker_ref coercion () ──

def test_path_f_bare_string_blocker_ref_coerced():
    g = _goal("g-f", blocker_ref="worldbuilders-apikey-missing")
    ref = synth_ref(g)
    assert isinstance(ref, dict), f"bare-string ref must be coerced to a dict, got {ref!r}"
    assert ref["type"] == "resource"
    assert str(ref["external_id"]).startswith("legacy-ref:")
    assert ref["synthesized"] is True
    assert ref["original_ref"] == "worldbuilders-apikey-missing", "original string must be preserved"
    assert _is_future(ref), "C3: coerced ref must have a future expiry"


def test_path_f_dict_blocker_ref_returned_unchanged():
    # A FRESH real dict ref (future expires_at) passes through untouched — path f
    # coerces non-dicts, and the stale-dict roll-forward below only fires on a
    # LAPSED expiry, so a future-expiry dict is returned verbatim.
    real = {"type": "user_action", "external_id": "u:xyz",
            "expires_at": _future_iso(5), "synthesized": False}
    g = _goal("g-dict", blocker_ref=real)
    assert synth_ref(g) == real


def test_path_f_stale_dict_blocker_ref_rolled_forward():
    # A STORED typed dict ref whose expires_at has LAPSED is rolled forward to a
    # future expiry (C3 self-heal,  sibling) rather than returned stale
    # — the  /  case (user_action refs created 2026-05-03,
    # expired 2026-05-08, still-active blocker) that tripped quiescence C3 into
    # false denial + B7 churn. type/external_id/created_at are preserved so the
    # C4 hysteresis hash stays stable; only expires_at advances.
    stale = {"type": "user_action", "external_id": "user-initiated-game-session",
             "created_at": "2026-05-03T21:28:50", "expires_at": "2026-05-08T21:28:50",
             "synthesized": False}
    g = _goal("g-stale-dict", blocker_ref=stale)
    ref = synth_ref(g)
    assert isinstance(ref, dict)
    assert _is_future(ref), "C3: a lapsed stored dict ref must be rolled to a future expiry"
    assert ref["external_id"] == "user-initiated-game-session", "C4: external_id preserved"
    assert ref["type"] == "user_action", "type preserved"
    assert ref["created_at"] == "2026-05-03T21:28:50", "created_at preserved"
    assert ref.get("expiry_rolled_forward") is True


def test_path_f_bare_string_external_id_stable():
    # C4: the same bare string must hash to the same external_id across calls.
    g1 = _goal("g-f1", blocker_ref="apikey-missing")
    g2 = _goal("g-f2", blocker_ref="apikey-missing")
    assert synth_ref(g1)["external_id"] == synth_ref(g2)["external_id"]


# ── Path (e): narrative defer_reason, no structured prefix () ──

def test_path_e_narrative_defer_synthed():
    g = _goal("g-e", defer_reason="Requires solver-v0 baseline (cur-02 unlock)",
              set_at_hours_ago=50)
    ref = synth_ref(g)
    assert isinstance(ref, dict)
    assert ref["type"] == "resource"
    assert str(ref["external_id"]).startswith("narrative-defer:")
    assert ref["synthesized"] is True
    assert _is_future(ref)


def test_path_e_narrative_defer_external_id_stable():
    g1 = _goal("g-e1", defer_reason="Requires solver-v0 baseline", set_at_hours_ago=1)
    g2 = _goal("g-e2", defer_reason="Requires solver-v0 baseline", set_at_hours_ago=99)
    # external_id hashes the defer TEXT (not set_at) -> stable across set_at.
    assert synth_ref(g1)["external_id"] == synth_ref(g2)["external_id"]


# ── Path (d): bare blocked_by dependency ( etc) ──

def test_path_d_bare_blocked_by_synthed():
    g = _goal("g-d", blocked_by=["g-pred-1"])
    ref = synth_ref(g)
    assert isinstance(ref, dict)
    assert ref["type"] == "resource"
    assert str(ref["external_id"]).startswith("dependency:")
    assert ref["synthesized"] is True
    assert _is_future(ref)


def test_path_d_external_id_order_independent():
    # external_id md5s the SORTED blocked_by -> order-independent (C4 stable).
    g1 = _goal("g-d1", blocked_by=["g-a", "g-b"])
    g2 = _goal("g-d2", blocked_by=["g-b", "g-a"])
    assert synth_ref(g1)["external_id"] == synth_ref(g2)["external_id"]


def test_path_d_empty_blocked_by_no_synth():
    # empty blocked_by must NOT synth a dependency ref (falls to path e or None).
    g = _goal("g-empty", blocked_by=[])
    assert synth_ref(g) is None


# ── Path (a) C3 roll-forward () ──

def test_path_a_rollforward_stale_structured_defer_is_future():
    # structured defer set 200h ago -> set_at+120h is PAST -> rolled to now+120h.
    g = _goal("g-a-stale", defer_reason="blocked_on_dependency: g-x", set_at_hours_ago=200)
    ref = synth_ref(g)
    assert isinstance(ref, dict)
    assert str(ref["external_id"]).startswith("structured-defer:")
    assert _is_future(ref), "C3: stale structured defer must be rolled to a future expiry"


def test_path_a_rollforward_preserves_external_id():
    # C4 CRITICAL: rolling the expiry must NOT change the external_id (it hashes
    # the defer text, not the expiry). Fresh and stale defers with the SAME text
    # must produce the SAME external_id so the hysteresis hash stays stable.
    fresh = _goal("g-fresh", defer_reason="blocked_on_dependency: g-x", set_at_hours_ago=1)
    stale = _goal("g-stale", defer_reason="blocked_on_dependency: g-x", set_at_hours_ago=200)
    assert synth_ref(fresh)["external_id"] == synth_ref(stale)["external_id"]


def test_path_a_fresh_structured_defer_not_rolled():
    # A fresh structured defer (set 1h ago) keeps set_at+120h — roll-forward is a
    # no-op when the set_at-anchored window is still future (created_at == set_at).
    g = _goal("g-a-fresh", defer_reason="precondition_unmet: dep-x", set_at_hours_ago=1)
    ref = synth_ref(g)
    exp = datetime.fromisoformat(ref["expires_at"])
    set_at = datetime.fromisoformat(g["defer_reason_set_at"])
    delta_h = (exp - set_at).total_seconds() / 3600
    assert 119.9 < delta_h < 120.1, f"fresh defer must keep ~120h from set_at, got {delta_h}h"


# ── Precedence: paths are ordered; earlier ones win when markers co-occur ──

def test_precedence_structured_prefix_beats_blocked_by():
    # A goal with BOTH a structured-prefix defer AND blocked_by -> path (a) wins
    # (structured-defer), NOT path (d). Path (a) is evaluated before path (d).
    g = _goal("g-both", defer_reason="blocked_on_dependency: g-x",
              set_at_hours_ago=1, blocked_by=["g-x"])
    ref = synth_ref(g)
    assert str(ref["external_id"]).startswith("structured-defer:")


def test_precedence_blocked_by_beats_narrative_defer():
    # blocked_by (path d) is evaluated before a non-structured defer (path e).
    g = _goal("g-dn", defer_reason="waiting on something", set_at_hours_ago=1,
              blocked_by=["g-y"])
    ref = synth_ref(g)
    assert str(ref["external_id"]).startswith("dependency:")


def test_precedence_future_deferred_until_beats_blocked_by():
    # path (b) deferred_until (future) is evaluated before path (d) blocked_by.
    du = _future_iso(30)
    g = _goal("g-du", blocked_by=["g-z"], deferred_until=du)
    ref = synth_ref(g)
    assert ref["expires_at"] == du
    assert str(ref["external_id"]).startswith("time-gate:")


def test_no_markers_returns_none():
    # No defer, no blocked_by, no ref -> None (unchanged).
    assert synth_ref(_goal("g-none")) is None


# ── collect_blocked integration: the end-to-end C2 + C3 guarantee ──

def test_collect_blocked_all_gap_shapes_get_valid_future_refs():
    # End-to-end: an all-blocked queue of the gap shapes yields blocked[] entries
    # that ALL carry a dict blocker_ref with a FUTURE expiry (quiescence C2 + C3
    # both pass). Mirrors the live 2026-07-05 verification (C2 9->0, C3 1->0).
    now = datetime.now()
    asps = _asp([
        # (d) explicit_status ("blocked") with blocked_by — branch 1, never
        # reaches the dependency branch, so relies on the L1659 synth ().
        _goal("g-dep-explicit", status="blocked", blocked_by=["g-pred-x"]),
        # (d) pending dependency on a LIVE (not-done) predecessor — branch 3.
        _goal("g-dep-pending", status="pending", blocked_by=["g-live-pred"]),
        # (e) narrative defer, explicit_status ().
        _goal("g-narr", status="blocked", defer_reason="Requires solver-v0 baseline"),
        # (f) bare-string blocker_ref, explicit_status ().
        _goal("g-legacy", status="blocked", blocker_ref="apikey-missing"),
        # (a) roll-forward: pending, structured defer set >120h ago, unmet dep
        # (branch 3) — path (a) ref rolled to a future expiry ( shape).
        _goal("g-stale-struct", status="pending",
              defer_reason="blocked_on_dependency: g-live-pred",
              set_at_hours_ago=200, blocked_by=["g-live-pred"]),
        # the live predecessor the two pending goals wait on (a candidate itself).
        _goal("g-live-pred", status="pending"),
    ])
    blk = _blocked_entries(asps)
    for gid in ("g-dep-explicit", "g-dep-pending", "g-narr", "g-legacy", "g-stale-struct"):
        assert gid in blk, f"{gid} must be in blocked[], got {sorted(blk)}"
        ref = blk[gid].get("blocker_ref")
        assert isinstance(ref, dict), f"{gid}: C2 requires a dict blocker_ref, got {ref!r}"
        exp = ref.get("expires_at")
        assert exp and datetime.fromisoformat(str(exp)) > now, \
            f"{gid}: C3 requires a future expiry, got {exp}"


# ── INFRASTRUCTURE-blocked synth ( / bravo msg-2949) ──
# The residual C2/C3 gap the  paths above missed: a goal blocked by an
# INFRASTRUCTURE known_blocker (goal.skill/category in the blocker's affected set)
# whose known_blocker carries NO blocker_ref of its own AND which has no
# structured-defer field (so _synth_blocker_ref_from_structured_defer returns None)
# reached collect_blocked with blocker_ref=None -> quiescence C2 never passed on a
# capability-limited runner (no aws/gpu/roblox/efs-key) -> perpetual B7 churn. The
# fix synths a type=resource ref at collect_blocked checks 2 (skill) + 2b (category),
# mirroring the not_my_lane synth (branch 7).

def _infra_blocker(blocker_id, *, skills=None, categories=None,
                   blocker_ref=None, reason="infra down"):
    b = {"blocker_id": blocker_id, "reason": reason, "resolution": None}
    if skills is not None:
        b["affected_skills"] = skills
    if categories is not None:
        b["affected_categories"] = categories
    if blocker_ref is not None:
        b["blocker_ref"] = blocker_ref
    return b


def _blocked_with(aspirations, known_blockers):
    done, live = _global_ids(aspirations)
    blocked = collect_blocked(
        aspirations, known_blockers=known_blockers,
        global_done_ids=done, global_live_ids=live,
        defer_reason_timeout_hours=TTL)
    return {b["goal_id"]: b for b in blocked}


def test_infra_skill_blocked_no_ref_synthed():
    # THE FIX: goal.skill in a ref-LESS known_blocker's affected_skills, no goal
    # defer -> entry.blocker_ref was None (C2 miss) -> now a type=resource synth.
    asps = _asp([_goal("g-infra", skill="run-processor")])
    blk = _blocked_with(asps, [_infra_blocker("blk-1", skills=["run-processor"])])
    assert "g-infra" in blk, f"g-infra must be blocked, got {sorted(blk)}"
    e = blk["g-infra"]
    assert e["block_reason"] == "infrastructure"
    ref = e.get("blocker_ref")
    assert isinstance(ref, dict), f"C2 requires a dict blocker_ref, got {ref!r}"
    assert ref["type"] == "resource"
    assert str(ref["external_id"]).startswith("infrastructure:")
    assert ref["synthesized"] is True
    assert _is_future(ref), "C3 requires a future expiry"


def test_infra_known_blocker_ref_preferred_over_synth():
    # The known_blocker's OWN blocker_ref is used verbatim when present — the synth
    # is a fallback, not an override (existing behavior preserved).
    real = {"type": "user_action", "external_id": "u:infra-real",
            "expires_at": _future_iso(5), "synthesized": False}
    asps = _asp([_goal("g-infra2", skill="run-processor")])
    blk = _blocked_with(
        asps, [_infra_blocker("blk-2", skills=["run-processor"], blocker_ref=real)])
    assert blk["g-infra2"]["blocker_ref"] == real


def test_infra_category_blocked_no_ref_synthed():
    # Category fallback (check 2b): skill-less goal whose category is in a ref-less
    # known_blocker's affected_categories gets the same infra synth.
    asps = _asp([_goal("g-infra-cat")])  # no skill -> category fallback path
    blk = _blocked_with(
        asps, [_infra_blocker("blk-3", categories=["framework-architecture"])])
    e = blk["g-infra-cat"]
    assert e["block_reason"] == "infrastructure"
    ref = e.get("blocker_ref")
    assert isinstance(ref, dict), f"C2 requires a dict blocker_ref, got {ref!r}"
    assert str(ref["external_id"]).startswith("infrastructure:")
    assert _is_future(ref)


def test_infra_external_id_stable_per_blocker():
    # C4 hysteresis: two goals blocked by the SAME blocker_id hash to the SAME
    # external_id (keyed on blocker_id, not goal id) so the hash stays stable.
    asps = _asp([_goal("g-i1", skill="run-processor"),
                 _goal("g-i2", skill="run-processor")])
    blk = _blocked_with(asps, [_infra_blocker("blk-shared", skills=["run-processor"])])
    assert (blk["g-i1"]["blocker_ref"]["external_id"]
            == blk["g-i2"]["blocker_ref"]["external_id"])


def test_infra_structured_defer_ref_not_clobbered():
    # Precedence: an infra-blocked goal that ALSO carries a structured defer keeps
    # its structured-defer synth ref (a dict from L1817) — the infra synth fires
    # only when entry.blocker_ref is not already a dict (mirrors not_my_lane).
    asps = _asp([_goal("g-i-both", skill="run-processor",
                       defer_reason="precondition_unmet: dep-x", set_at_hours_ago=1)])
    blk = _blocked_with(asps, [_infra_blocker("blk-4", skills=["run-processor"])])
    ref = blk["g-i-both"]["blocker_ref"]
    assert isinstance(ref, dict)
    assert str(ref["external_id"]).startswith("structured-defer:"), \
        f"structured-defer ref must be preserved, got {ref['external_id']}"


# ── precondition_unmet + explicit_status branch synth () ──
# The remaining two collect_blocked branches that set block_reason without synthing
# a blocker_ref: check 6 (precondition_unmet — bravo msg-2949 named it as the second
# residual C2 class after infrastructure) and check 1 (explicit_status — defensive:
# 's all-gap-shapes test only covered explicit_status WITH defer fields).
# Both use the generalized _synth_block_ref(kind, key). rb-3004 "audit the whole
# branch set" applied.

# A structured precondition that fails deterministically (nonexistent file) — feeds
# collect_blocked check 6 via the fire_when sugar, no external state needed.
_FAILING_PC = {"type": "file_check",
               "path": "/nonexistent/g-115-1888-precondition.does-not-exist",
               "condition": "exists"}


def test_precondition_unmet_no_defer_synthed():
    # THE bravo-evidenced fix: a goal whose LIVE structured precondition fails
    # (check 6) but which carries no defer fields kept blocker_ref=None (C2 miss)
    # -> now a type=resource "precondition:" synth.
    asps = _asp([_goal("g-pc", fire_when=_FAILING_PC)])
    blk = _blocked_entries(asps)
    assert "g-pc" in blk, f"g-pc must be blocked (precondition_unmet), got {sorted(blk)}"
    e = blk["g-pc"]
    assert e["block_reason"] == "precondition_unmet"
    ref = e.get("blocker_ref")
    assert isinstance(ref, dict), f"C2 requires a dict blocker_ref, got {ref!r}"
    assert ref["type"] == "resource"
    assert str(ref["external_id"]).startswith("precondition:")
    assert ref["synthesized"] is True
    assert _is_future(ref), "C3 requires a future expiry"


def test_precondition_external_id_stable():
    # C4 hysteresis: the same failing predicate set hashes to the same external_id
    # across goals (keyed on sorted failed predicate ids, not goal id).
    asps = _asp([_goal("g-pc1", fire_when=_FAILING_PC),
                 _goal("g-pc2", fire_when=_FAILING_PC)])
    blk = _blocked_entries(asps)
    assert (blk["g-pc1"]["blocker_ref"]["external_id"]
            == blk["g-pc2"]["blocker_ref"]["external_id"])


def test_explicit_status_no_defer_synthed():
    # Defensive (check 1): a goal explicitly set status="blocked" with no defer
    # fields kept blocker_ref=None -> now an "explicit-status:" synth.
    asps = _asp([_goal("g-es", status="blocked")])
    blk = _blocked_entries(asps)
    assert "g-es" in blk
    e = blk["g-es"]
    assert e["block_reason"] == "explicit_status"
    ref = e.get("blocker_ref")
    assert isinstance(ref, dict), f"C2 requires a dict blocker_ref, got {ref!r}"
    assert str(ref["external_id"]).startswith("explicit-status:")
    assert ref["synthesized"] is True
    assert _is_future(ref)


def test_explicit_status_with_defer_keeps_structured_ref():
    # Precedence: an explicit_status goal that ALSO carries a structured defer keeps
    # its L1817 structured-defer synth ref — the explicit-status synth fires only
    # when entry.blocker_ref is not already a dict.
    asps = _asp([_goal("g-es-defer", status="blocked",
                       defer_reason="precondition_unmet: dep-x", set_at_hours_ago=1)])
    blk = _blocked_entries(asps)
    ref = blk["g-es-defer"]["blocker_ref"]
    assert isinstance(ref, dict)
    assert str(ref["external_id"]).startswith("structured-defer:"), \
        f"structured-defer ref must win over explicit-status synth, got {ref['external_id']}"
