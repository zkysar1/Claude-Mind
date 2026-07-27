"""Tests for blocked-signal-resolution-check.py ().

The sweep flags status=blocked goals whose block signals (blocked_by /
blocker_ref) have ALL resolved — the gap the defer_reason sweep family cannot
see. Detective only; these tests pin the polymorphic-input normalization, the
referent-kind resolver, and the verdict ladder.

Every fixture below is a SHAPE MEASURED ON THE LIVE FLEET on 2026-07-26, not an
invented one — the whole reason this script exists is that the real inputs are
polymorphic in ways a hand-written checker would not anticipate:

  g-350-36    blocker_ref as a BARE STRING goal-id, no blocked_by      -> all_resolved
  g-350-95    blocker_ref dict, TTL passed, unblock_goal pq pending    -> all_resolved
  g-250-03-c  blocked_by resolved BUT blocker_ref still live           -> disagreement
  g-335-144   blocked_by as a BARE STRING (not a list)                 -> still_blocked
  g-335-228   blocker_ref naming a LIVE (pending) foxtrot pq           -> still_blocked
  g-354-21    ONE live signal, no blocked_by                           -> still_blocked

The two rows that mention a pq are stated as the STORE OF RECORD has them, not as
the local tree does — reading the local cache made both look nonexistent and
produced two false `dangling_ref` verdicts (see the guard-980 section at the
bottom). Tests that pass an EMPTY pq index still exercise the dangling MECHANISM;
they are not claims about live data.

Pattern: same importlib + sys.path shape as test_defer_drift_check.py (the
script name has hyphens, so it cannot be a plain `import`).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "blocked-signal-resolution-check.py"

# Fixed reference time so days_blocked and TTL comparisons are deterministic.
NOW = dt.datetime(2026, 7, 26, 14, 0, 0)


def _import():
    spec = importlib.util.spec_from_file_location(
        "blocked_signal_resolution_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["blocked_signal_resolution_check"] = mod
    spec.loader.exec_module(mod)
    return mod


def _goal(**kw):
    g = {
        "id": "g-999-01",
        "status": "blocked",
        "blocked_since": "2026-07-20T00:00:00",
        "_source": "world",
        "_aspiration_id": "asp-999",
        "title": "test goal",
        "intended_agent": "either",
    }
    g.update(kw)
    return g


def _index(*goals):
    return {g["id"]: (g.get("_source", "world"), g) for g in goals}


def _classify(goal, index=None, pq=None):
    mod = _import()
    return mod._classify(goal, index or {}, pq or {}, NOW)


def _classify_bsrc(goal, index=None, pq=None, pq_complete=True):
    """_classify with an explicit pq-corpus-completeness flag."""
    mod = _import()
    return mod._classify(goal, index or {}, pq or {}, NOW, pq_complete)


# ── Polymorphic normalization: the defect the script exists to survive ──

def test_norm_blocked_by_bare_string_is_one_id_not_per_character():
    """THE core defect. A bare-string blocked_by iterated as a list yields one
    phantom id per CHARACTER, none of which ever resolve — so the goal reads
    'not resolved' forever and is silently excluded from every verdict."""
    mod = _import()
    assert mod._norm_blocked_by("g-335-260") == ["g-335-260"]
    # The bug this pins: naive list() would give 9 single-char ids.
    assert len(mod._norm_blocked_by("g-335-260")) == 1


def test_norm_blocked_by_accepts_list_and_absent():
    mod = _import()
    assert mod._norm_blocked_by(["g-1", "g-2"]) == ["g-1", "g-2"]
    assert mod._norm_blocked_by(None) == []
    assert mod._norm_blocked_by("") == []
    assert mod._norm_blocked_by("   ") == []


def test_norm_blocked_by_drops_non_string_members():
    """An unexpected shape must not become a confident wrong id."""
    mod = _import()
    assert mod._norm_blocked_by(["g-1", 42, None, {"id": "g-2"}]) == ["g-1"]
    assert mod._norm_blocked_by(17) == []


def test_norm_blocker_ref_all_three_observed_shapes():
    mod = _import()
    assert mod._norm_blocker_ref(None)[0] == "none"
    assert mod._norm_blocker_ref("g-350-59")[0] == "str"
    assert mod._norm_blocker_ref({"expires_at": "x"})[0] == "dict"
    assert mod._norm_blocker_ref(["a"])[0] == "other"


# ── Referent-kind resolution: lookup first, spelling second ──

def test_classify_ref_prefers_store_lookup_over_spelling():
    mod = _import()
    idx = _index(_goal(id="g-350-59", status="completed"))
    resolved, _why, kind = mod._classify_ref("g-350-59", idx, {})
    assert (resolved, kind) == (True, "goal")


def test_classify_ref_pq_terminal_and_live():
    mod = _import()
    assert mod._classify_ref("pq-x", {}, {"pq-x": "resolved"})[0] is True
    assert mod._classify_ref("pq-x", {}, {"pq-x": "pending"})[0] is False


def test_classify_ref_missing_ids_are_dangling_not_resolved():
    """A reference nobody can resolve must never read as satisfied — it can
    NEVER auto-clear, which is the point of surfacing it."""
    mod = _import()
    for rid in ("pq-fox-vinheim-chardef-authoring", "g-404-99"):
        resolved, _why, kind = mod._classify_ref(rid, {}, {})
        assert resolved is None and kind == "dangling", rid


def test_classify_ref_board_and_opaque_are_undecidable_not_dangling():
    mod = _import()
    for rid in ("coordination:msg-20260725-085534-foxtrot-5196", "msg-abc"):
        assert mod._classify_ref(rid, {}, {})[2] == "board"
    assert mod._classify_ref("some-external-thing", {}, {})[2] == "opaque"


# ── Verdict ladder, on live-measured shapes ──

def test_bare_string_blocker_ref_to_completed_goal_is_all_resolved():
    """: sat blocked 6.7d after its only block signal completed."""
    e = _classify(
        _goal(id="g-350-36", blocker_ref="g-350-59",
              blocked_since="2026-07-19T21:11:34"),
        _index(_goal(id="g-350-59", status="completed")))
    assert e["verdict"] == "all_resolved"
    assert e["resolution_basis"] == "referent_terminal"
    assert e["days_blocked"] == 6.7


def test_passed_ttl_is_all_resolved_but_flagged_ttl_expired_not_terminal():
    """. A passed expires_at means the record FAIL-OPENED by design; it
    is NOT proof the premise cleared. The basis field must keep the two apart so
    a reader can weight them differently before acting."""
    e = _classify(_goal(id="g-350-95", blocker_ref={
        "expires_at": "2026-07-25T14:20:00",
        "unblock_goal": "pq-fox-roblox-clone-stale-reconcile"}))
    assert e["verdict"] == "all_resolved"
    assert e["resolution_basis"] == "ttl_expired"
    # The dangling half must still reach the reader, not be swallowed.
    assert "DANGLING" in e["blocker_ref_why"]


def test_future_ttl_with_no_other_signal_is_still_blocked():
    e = _classify(_goal(blocker_ref={"expires_at": "2026-07-28T09:00:00"}))
    assert e["verdict"] == "still_blocked"


def test_resolved_blocked_by_plus_live_blocker_ref_is_disagreement():
    """-c — and precisely the goal the naive blocked_by-only predicate
    would wrongly unblock. Two signals present, they disagree, so do NOT
    unblock: the disagreement IS the finding."""
    e = _classify(
        _goal(id="g-250-03-c", blocked_by=["g-250-127"],
              blocker_ref={"expires_at": "2026-07-28T09:00:00",
                           "unblock_goal": "coordination:msg-1"}),
        _index(_goal(id="g-250-127", status="completed")))
    assert e["verdict"] == "disagreement"
    assert e["blocked_by_resolved"] is True


def test_one_present_signal_never_reads_as_disagreement():
    """REGRESSION GUARD (fix found by running the script). `bb_resolved` is
    vacuously True when blocked_by is absent, which made every single-signal
    blocked goal report as 'signals disagree'. 4 of the 6 first-run
    disagreements were this — ordinary blocked goals, working as intended,
    surfaced as findings. Disagreement requires two signals to actually BE
    there. Both single-signal orientations are pinned."""
    live_ref_only = _classify(_goal(id="g-354-21", blocker_ref={
        "expires_at": "2026-07-27T19:20:22"}))
    assert live_ref_only["verdict"] == "still_blocked"

    pending_bb_only = _classify(
        _goal(id="g-319-05", blocked_by=["g-319-04"]),
        _index(_goal(id="g-319-04", status="pending")))
    assert pending_bb_only["verdict"] == "still_blocked"


def test_vacuous_bb_still_enables_all_resolved_on_blocker_ref_alone():
    """The other half of the same contract: absent blocked_by MUST stay
    vacuously satisfied for the all_resolved conjunction, or the two genuinely
    eligible goals (which carry no blocked_by at all) become undetectable."""
    e = _classify(_goal(blocker_ref="g-2"),
                  _index(_goal(id="g-2", status="completed")))
    assert e["verdict"] == "all_resolved"


def test_dangling_pq_reference_is_reported_as_dangling():
    """: can never auto-clear, so it sits blocked forever unless
    someone repoints or removes the reference."""
    e = _classify(_goal(id="g-335-228",
                        blocker_ref="pq-fox-vinheim-chardef-authoring"))
    assert e["verdict"] == "dangling_ref"


def test_unknown_blocked_by_id_is_dangling():
    e = _classify(_goal(blocked_by=["g-404-99"]))
    assert e["verdict"] == "dangling_ref"
    assert e["blocked_by_status"] == {"g-404-99": "NOT-FOUND"}


def test_bare_string_blocked_by_resolves_end_to_end():
    """The normalization must survive the whole ladder, not just the helper."""
    e = _classify(_goal(id="g-335-144", blocked_by="g-335-260"),
                  _index(_goal(id="g-335-260", status="pending")))
    assert e["blocked_by"] == ["g-335-260"]
    assert e["blocked_by_raw_type"] == "str"
    assert e["verdict"] == "still_blocked"

    terminal = _classify(_goal(blocked_by="g-1"),
                         _index(_goal(id="g-1", status="completed")))
    assert terminal["verdict"] == "all_resolved"


# ── Population boundaries ──

def test_non_blocked_goals_are_never_classified():
    for st in ("pending", "in-progress", "completed", "skipped"):
        assert _classify(_goal(status=st, blocked_by=["g-1"])) is None


def test_blocked_with_no_signal_is_left_to_the_complement_sweep():
    """reason-less-blocked-check.py owns that population (precheck 0.5b.11). A
    goal is in exactly one of the two, never both — never double-report."""
    assert _classify(_goal()) is None
    assert _classify(_goal(blocked_by=None, blocker_ref=None)) is None
    assert _classify(_goal(blocked_by=[], blocker_ref="")) is None


def test_every_terminal_status_counts_as_resolved():
    mod = _import()
    for st in mod.TERMINAL_STATUSES:
        e = _classify(_goal(blocker_ref="g-1"),
                      _index(_goal(id="g-1", status=st)))
        assert e["verdict"] == "all_resolved", st


def test_unparseable_timestamps_never_raise():
    """guard-420 tolerant parse: a bad stamp degrades the field, never the run."""
    e = _classify(_goal(blocked_since="not-a-date",
                        blocker_ref={"expires_at": "garbage",
                                     "unblock_goal": "g-1"}),
                  _index(_goal(id="g-1", status="completed")))
    assert e["days_blocked"] is None
    assert e["verdict"] == "all_resolved"


def test_blocker_ref_dict_with_no_resolvable_signal_is_undecidable():
    e = _classify(_goal(blocker_ref={"type": "resource-contention",
                                     "note": "waiting on a human"}))
    assert e["verdict"] == "undecidable"
    assert "neither expires_at nor unblock_goal" in e["blocker_ref_why"]


def test_unblocking_goal_spelling_variant_is_honored():
    """Both spellings are present in the wild; accepting only one silently
    drops half the population."""
    for key in ("unblock_goal", "unblocking_goal"):
        e = _classify(_goal(blocker_ref={key: "g-1"}),
                      _index(_goal(id="g-1", status="completed")))
        assert e["verdict"] == "all_resolved", key
        assert e["resolution_basis"] == "referent_terminal"


# ── Store-of-record fail-safe (guard-980 regression, found by fresh-eyes) ──
#
# `_load_pq_index` originally globbed the LOCAL tree. Under own-cloud the local
# tree is a read-through cache, so it saw only the RESIDENT agent: 1 file / 10
# ids locally vs 5 files / 87 ids in the store of record. That under-read
# manufactured TWO false `dangling_ref` verdicts on the first live run
# (pq-fox-vinheim-chardef-authoring, pq-fox-roblox-clone-stale-reconcile — both
# LIVE in foxtrot's store, the first `status: pending`), and both were reported
# to the owning agent as "repoint or remove the reference" before being caught.
# Advising an agent to delete a valid blocker is worse than reporting nothing.

def test_unresolved_pq_is_never_dangling_when_corpus_incomplete():
    """THE fail-safe. With any agent's pq store unreadable, absence is
    ignorance rather than evidence, so the dangling verdict MUST be withheld."""
    mod = _import()
    resolved, why, kind = mod._classify_ref("pq-fox-anything", {}, {},
                                            pq_complete=False)
    assert resolved is None
    assert kind == "opaque", "must NOT be 'dangling' on an incomplete corpus"
    assert "INCOMPLETE" in why


def test_unresolved_pq_is_dangling_only_when_corpus_complete():
    """The other half — the verdict must still be reachable, or the fail-safe
    has silently deleted the whole dangling bucket."""
    mod = _import()
    assert mod._classify_ref("pq-x", {}, {}, pq_complete=True)[2] == "dangling"


def test_pq_complete_flag_threads_through_the_whole_ladder():
    """Signature-drift guard: the flag has to survive _classify ->
    _resolve_blocker_ref -> _classify_ref, not just the leaf call. g-335-228's
    real shape (bare-str blocker_ref naming a pq) is the carrier."""
    g = _goal(id="g-335-228", blocker_ref="pq-fox-vinheim-chardef-authoring")
    assert _classify_bsrc(g, pq_complete=False)["verdict"] == "undecidable"
    assert _classify_bsrc(g, pq_complete=True)["verdict"] == "dangling_ref"


def test_live_pq_reference_reads_as_still_blocked_not_dangling():
    """The actual  truth once the store of record is read: the pq
    exists and is pending, so the goal is correctly blocked and must not be
    reported at all."""
    e = _classify(_goal(id="g-335-228",
                        blocker_ref="pq-fox-vinheim-chardef-authoring"),
                  pq={"pq-fox-vinheim-chardef-authoring": "pending"})
    assert e["verdict"] == "still_blocked"


def test_load_pq_index_returns_pair_and_reads_store_of_record():
    """Contract test: the loader returns (index, missing_agents) — a bare dict
    return would make `pq_complete` silently truthy and re-arm the bug — and on
    THIS box it must resolve more than the resident agent alone (10 was the
    local-glob number; the store of record holds ~87 across 5 agents)."""
    mod = _import()
    result = mod._load_pq_index()
    assert isinstance(result, tuple) and len(result) == 2
    index, missing = result
    assert isinstance(index, dict) and isinstance(missing, list)
    assert len(index) > 10, (
        f"only {len(index)} pq ids — looks like a local-glob regression "
        f"(resident-agent-only); expected the fleet corpus")
