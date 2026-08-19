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
    """A ref this reader cannot resolve stays undecidable — but the message
    must say WHY, and must not claim the ref is empty when it is not.

    g-115-3505 changed the wording. The old text was "carries neither
    expires_at nor unblock_goal — no resolvable signal", which is mechanically
    true of this reader yet substantively wrong about the ref: every live
    instance measured 2026-07-27 was content-rich ({ref, why}, {blocker_type,
    blocking_goal, denied_action, principal, probe}, ...). A goal filed off
    that summary alone would claim the block is unresolvable, which is false
    (rb-245 — an undecidable count against a field-name assumption). The
    verdict is unchanged; only the diagnosis is now honest.
    """
    e = _classify(_goal(blocker_ref={"type": "resource-contention",
                                     "note": "waiting on a human"}))
    assert e["verdict"] == "undecidable"
    why = e["blocker_ref_why"]
    assert "SCHEMA VARIANT" in why, why
    # The unresolvable-by-THIS-READER framing must survive the reword.
    assert "expires_at" in why and "unblock_goal" in why, why
    # The present keys must be named — that is what lets a reader tell a
    # variant apart from an empty ref without opening the goal.
    assert "'note'" in why and "'type'" in why, why


def test_genuinely_empty_blocker_ref_dict_says_empty_not_variant():
    """The complement branch. An empty dict really IS signal-free, and must
    NOT be described as a schema variant — otherwise the new message would
    send a reader hunting for a payload that does not exist, which is the
    same misdirection in the opposite direction. g-115-3505."""
    e = _classify(_goal(blocker_ref={"expires_at": None}))
    assert e["verdict"] == "undecidable"
    why = e["blocker_ref_why"]
    assert "empty" in why.lower(), why
    assert "SCHEMA VARIANT" not in why, why


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


# ── blocker_ref.external_id: the partner-response blocker class () ──
#
# _resolve_blocker_ref's dict branch consulted exactly two fields — expires_at
# and unblock_goal/unblocking_goal — and never external_id. A blocker_ref of
# type partner-response names its referent THERE AND NOWHERE ELSE, so that whole
# class was undetectable by the one checker built to find blocked goals whose
# signals have all cleared.
#
# Two shapes, both measured on the ZDS-Mind production queue 2026-07-28 and
# cleared by hand:
#   (a) {type, external_id} alone      -> (None, 'opaque')     -> "undecidable"
#       No TTL to fail open and no field consulted that could ever clear it, so
#       the goal sits blocked FOREVER. One such goal was blocked 5 days past its
#       referent's completion.
#   (b) {type, external_id, expires_at future} -> (False, 'unresolved')
#       Waits out the clock even though the referent completed. One such goal
#       was blocked 10 days past completion.
#
# The control below (same ref, unblock_goal instead of external_id) resolves
# True, which is what isolates external_id as the sole difference rather than
# something about partner-response refs generally.

FUTURE_TTL = "2026-07-30T00:00:00"   # after NOW (2026-07-26)
PASSED_TTL = "2026-07-01T00:00:00"   # before NOW


def _resolve_ref(ref, index=None, pq=None, resolver=None):
    mod = _import()
    return mod._resolve_blocker_ref(
        "dict", ref, ref, index or {}, pq or {}, NOW,
        external_resolver=resolver)


_DONE = {"id": "g-999-77", "status": "completed"}
_LIVE = {"id": "g-999-78", "status": "pending"}


def test_external_id_terminal_referent_resolves():
    """Shape (a): the field is read at all. Pre-fix this was (None, 'opaque')."""
    resolved, why, basis = _resolve_ref(
        {"type": "partner-response", "external_id": "g-999-77"},
        _index(_DONE))
    assert resolved is True, f"external_id referent not read: {why}"
    assert basis == "referent_terminal_external", basis


def test_external_id_control_unblock_goal_isolates_the_field():
    """The control that makes the test above a claim about external_id and not
    about partner-response refs in general: identical ref, different field."""
    resolved, _, basis = _resolve_ref(
        {"type": "partner-response", "unblock_goal": "g-999-77"}, _index(_DONE))
    assert (resolved, basis) == (True, "referent_terminal"), basis


def test_terminal_referent_beats_a_future_ttl():
    """Shape (b): pre-fix this returned (False, 'unresolved') — external_id was
    unread, and the mere PRESENCE of expires_at pushed the fallthrough to a
    definite 'unresolved', so the goal waited out a TTL its referent had
    already made moot.

    Scope note (measured, not assumed): this case does NOT pin the ordering of
    the ext check against `expired`. The TTL here is in the future, so `expired`
    is False and the two clauses cannot contend. Mutation-testing an ext check
    ranked BELOW `expired` leaves this test GREEN. The ordering is pinned by the
    passed-TTL test below; keeping the two apart is what makes each one's claim
    true."""
    resolved, why, basis = _resolve_ref(
        {"type": "partner-response", "external_id": "g-999-77",
         "expires_at": FUTURE_TTL},
        _index(_DONE))
    assert resolved is True, f"clock beat a completed referent: {why}"
    assert basis == "referent_terminal_external", basis


def test_terminal_referent_outranks_a_passed_ttl_in_the_basis():
    """THE ordering test. Referent completed AND clock lapsed — both say the
    block can clear, so `resolved` is True either way and the bug hides in the
    BASIS. The module's doctrine is that a reader weights `referent_terminal`
    above `ttl_expired` and re-probes the latter before acting; reporting
    ttl_expired here tells the reader to go re-verify a premise that genuinely
    cleared.

    Verified by mutation: ranking the ext check below `expired` flips this to
    'ttl_expired' while every other test in this section stays green."""
    resolved, _, basis = _resolve_ref(
        {"type": "partner-response", "external_id": "g-999-77",
         "expires_at": PASSED_TTL},
        _index(_DONE))
    assert resolved is True
    assert basis == "referent_terminal_external", (
        f"a completed referent was reported as {basis!r} — the clock outranked "
        f"the fact, so the reader is told to re-probe a settled premise")


def test_pending_external_referent_stays_unresolved():
    """Over-unblock control. A live referent must NOT clear the block, and must
    read as 'unresolved' (a definite no) rather than 'opaque' (a shrug) — the
    opaque fallthrough tests `ext_resolved is None`, and using truthiness there
    would swallow this False and re-hide the goal."""
    resolved, _, basis = _resolve_ref(
        {"type": "partner-response", "external_id": "g-999-78"}, _index(_LIVE))
    assert (resolved, basis) == (False, "unresolved"), basis


def test_cross_world_id_without_resolver_gets_its_own_basis():
    """'opaque' means a shape this reader does not understand; a cross-world ref
    IS understood and merely unreachable. Folding them together makes a real
    operational population uncountable."""
    resolved, _, basis = _resolve_ref(
        {"type": "partner-response", "external_id": "zds:g-500-11"})
    assert resolved is None
    assert basis == "external_unresolvable", basis


def test_cross_world_id_with_injected_resolver_settles():
    """The resolver is injected, not imported — this checker runs in worlds with
    no cross-world reader at all. Both verdicts, so neither branch is dead."""
    done, _, done_basis = _resolve_ref(
        {"type": "partner-response", "external_id": "zds:g-500-11"},
        resolver=lambda rid: "completed")
    live, _, live_basis = _resolve_ref(
        {"type": "partner-response", "external_id": "zds:g-500-11"},
        resolver=lambda rid: "pending")
    assert (done, done_basis) == (True, "referent_terminal_external")
    assert (live, live_basis) == (False, "unresolved"), live_basis


# ── unparseable / falsy expires_at () ──
#
# Two fresh-eyes findings from bravo, both in the same dict branch:
# bravo-fec-unparseable-ttl-asserts-definite-verdict-202607271800 and
# bravo-fec-empty-dict-message-overclaims. Neither was introduced by ;
# both were surfaced by probing around it.

def test_unparseable_ttl_is_opaque_not_a_definite_verdict():
    """rb-245 class: a definite verdict drawn against a field the reader just
    failed to parse. Pre-fix, {'expires_at': 'garbage'} returned
    (False, 'unresolved') — 'this block is CONFIRMED still live' — because the
    opaque guard tested `not exp_raw` and a garbage string is truthy. Every
    other undecidable shape in this function returns opaque; an unreadable TTL
    is one of those. The precheck 0.5b.12 sweep consumes the difference, so the
    overclaim propagates into filed goals."""
    resolved, why, basis = _resolve_ref({"expires_at": "garbage"})
    assert (resolved, basis) == (None, "opaque"), (resolved, basis)
    assert "unparseable" in why, why


def test_parseable_ttl_still_yields_a_definite_verdict():
    """The control that keeps the fix from over-reaching: a TTL that PARSES is a
    real signal and must keep producing definite verdicts in both directions.
    Widening opaque to every present expires_at would silently disable the TTL
    fail-open the schema exists to provide."""
    future, _, fb = _resolve_ref({"expires_at": FUTURE_TTL})
    passed, _, pb = _resolve_ref({"expires_at": PASSED_TTL})
    assert (future, fb) == (False, "unresolved"), fb
    assert (passed, pb) == (True, "ttl_expired"), pb


def test_unparseable_ttl_does_not_suppress_a_real_referent_signal():
    """An unreadable TTL contributes nothing — it must not also SUBTRACT. A
    terminal unblock_goal alongside garbage still resolves."""
    resolved, _, basis = _resolve_ref(
        {"expires_at": "garbage", "unblock_goal": "g-999-77"}, _index(_DONE))
    assert (resolved, basis) == (True, "referent_terminal"), basis


def test_present_but_falsy_key_is_not_reported_as_an_empty_dict():
    """{'expires_at': ''} is not an empty dict. Calling it empty sends the next
    reader hunting for a missing field that is sitting right there — the same
    overclaim g-115-3505 fixed in the sibling branch one line above."""
    _, why, basis = _resolve_ref({"expires_at": ""})
    assert basis == "opaque", basis
    assert "is empty" not in why, f"still claims emptiness: {why}"
    assert "expires_at" in why, f"does not name the present-but-empty key: {why}"


def test_genuinely_empty_dict_still_says_empty():
    """Control for the message change: a dict that IS empty must keep saying so,
    or the reword just moves the inaccuracy to the other case."""
    _, why, _ = _resolve_ref({})
    assert "empty" in why, why


def test_throwing_resolver_fails_open_and_is_diagnosable():
    """The resolver is injected foreign code reaching another world — the single
    likeliest dependency here to be down or throwing. _resolve_blocker_ref runs
    per-goal, so an unhandled raise takes out the WHOLE scan, not just this ref;
    a detective sweep that dies on its own optional dependency is worse than one
    that reports less (guard-142).

    Found by the post-close fresh-eyes probe on this very change, not by design —
    the first version propagated."""
    def boom(rid):
        raise RuntimeError("cross-world reader is down")

    resolved, why, basis = _resolve_ref(
        {"type": "partner-response", "external_id": "zds:g-500-11"},
        resolver=boom)
    assert (resolved, basis) == (None, "external_unresolvable"), basis
    # Recorded, not swallowed — a broken resolver must be diagnosable from the
    # report rather than merely quiet.
    assert "RuntimeError" in why and "resolver raised" in why, why


def test_board_prefixed_external_id_is_not_read_as_cross_world():
    """_BOARD_PREFIXES legitimately contain ':' ('coordination:', 'findings:',
    ...). A bare colon test would route them into the cross-world branch and
    strip the board classification _classify_ref gives them."""
    resolved, _, basis = _resolve_ref(
        {"type": "partner-response", "external_id": "coordination:msg-1"})
    assert (resolved, basis) == (None, "opaque"), basis


def test_dangling_external_id_surfaces_as_dangling():
    """A dangling referent can never auto-clear, so it must keep outranking the
    generic undecidable bases — same treatment unblock_goal already gets."""
    resolved, _, basis = _resolve_ref(
        {"type": "partner-response", "external_id": "g-404-99"})
    assert (resolved, basis) == (None, "dangling"), basis


def test_passed_ttl_still_outranks_an_unresolvable_cross_world_ref():
    """The TTL fail-open is a real design guarantee and must survive the new
    bases: unreachable referent + lapsed clock is still all_resolved, and still
    labelled ttl_expired so a reader knows to re-probe before acting."""
    resolved, _, basis = _resolve_ref(
        {"type": "partner-response", "external_id": "zds:g-500-11",
         "expires_at": PASSED_TTL})
    assert (resolved, basis) == (True, "ttl_expired"), basis


def test_external_id_reaches_the_verdict_ladder_end_to_end():
    """Integration path, not just the resolver in isolation: a resolver fix that
    never reaches `verdict` would leave the goal exactly as invisible."""
    goal = _goal(id="g-999-79", blocker_ref={"type": "partner-response",
                                             "external_id": "g-999-77"})
    entry = _classify(goal, _index(_DONE))
    assert entry["verdict"] == "all_resolved", entry
    assert entry["resolution_basis"] == "referent_terminal_external", entry


# ─────────────────────────────────────────────────────────────────────────────
# Routing-breadcrumb cooldown ()
#
# The sweep is detective-only, so the LLM does the lane routing — and with N
# agents each running precheck, the SAME unresolved hit was routed to the
# coordination board once per agent per round. Measured: 7 posts from 3 agents
# over ~29h on goals that never changed. The two sibling sweeps
# (inbox-alert-age-check , handoff-aging-check ) already
# solve this with a shared+durable board breadcrumb; this pins that the same
# mechanism now works here.
#
# These tests use the `board_log_path` seam rather than the daemon so they are
# hermetic — no board reads, no board writes, no live fleet state.
# ─────────────────────────────────────────────────────────────────────────────

def _routing_log(tmp_path, name, tags, age_hours, now=NOW):
    """Write a one-post board fixture aged `age_hours` before `now`."""
    import json
    p = tmp_path / name
    ts = (now - dt.timedelta(hours=age_hours)).isoformat(timespec="seconds")
    p.write_text(json.dumps([{"timestamp": ts, "tags": tags}]), encoding="utf-8")
    return p


def test_fresh_breadcrumb_is_seen_and_keyed_on_the_goal_id(tmp_path):
    mod = _import()
    log = _routing_log(tmp_path, "fresh.json",
                       [mod.ROUTING_TAG, "g-111-11", "lane:either"], 2.0)
    recent, read_ok = mod._read_recent_routings(NOW, 24.0, log)
    assert read_ok is True
    assert recent == {"g-111-11": 2.0}, recent


def test_a_breadcrumb_older_than_the_window_does_not_suppress(tmp_path):
    """The load-bearing NEGATIVE control. Without it, a test suite cannot tell
    'the cooldown works' from 'any breadcrumb at all suppresses forever' — and
    the second is a silent-forever bug, since a stale post would permanently
    hide a hit the lane owner never acted on."""
    mod = _import()
    log = _routing_log(tmp_path, "stale.json",
                       [mod.ROUTING_TAG, "g-111-11"], 48.0)
    recent, read_ok = mod._read_recent_routings(NOW, 24.0, log)
    assert read_ok is True
    # It is SEEN (the reader reports it) but its age exceeds the window, so the
    # caller's `age < cooldown_hours` comparison must not suppress.
    assert recent["g-111-11"] == 48.0
    assert not (recent["g-111-11"] < 24.0)


def test_legacy_hand_written_routing_tags_are_honoured(tmp_path):
    """The duplicated posts this goal was filed over carried `blocked-signal` /
    `blocked-signal-sweep`, not the new marker. Recognising them means the
    cooldown respects a manual post instead of re-routing over it."""
    mod = _import()
    for legacy in ("blocked-signal", "blocked-signal-sweep"):
        log = _routing_log(tmp_path, "legacy-%s.json" % legacy,
                           [legacy, "g-222-22", "foxtrot"], 1.0)
        recent, _ = mod._read_recent_routings(NOW, 24.0, log)
        assert recent == {"g-222-22": 1.0}, (legacy, recent)


def test_a_post_without_a_goal_id_tag_cannot_suppress_anything(tmp_path):
    """Fail-open in the safe direction: a routing-marked post that never tagged
    its goal_id keys to nothing, so it suppresses nothing rather than
    suppressing everything."""
    mod = _import()
    log = _routing_log(tmp_path, "no-gid.json", [mod.ROUTING_TAG, "foxtrot"], 1.0)
    recent, read_ok = mod._read_recent_routings(NOW, 24.0, log)
    assert read_ok is True
    assert recent == {}, recent


def test_unreadable_board_reports_degraded_and_suppresses_nothing(tmp_path):
    """read_ok is returned SEPARATELY from the dict on purpose. An empty dict
    has two causes — a quiet board and a failed read — and a consumer that
    derived the flag from the dict's truthiness could never tell them apart,
    reporting a healthy quiet board as a failure (rb-245: a zero whose
    provenance is unknown is not a measurement)."""
    mod = _import()
    recent, read_ok = mod._read_recent_routings(
        NOW, 24.0, tmp_path / "does-not-exist.json")
    assert recent == {}
    assert read_ok is False


def test_quiet_board_is_read_ok_not_degraded(tmp_path):
    """The other half of the pair above: genuinely nothing routed recently must
    report read_ok=True, or every clean run would look like an outage."""
    import json
    mod = _import()
    p = tmp_path / "empty.json"
    p.write_text(json.dumps([]), encoding="utf-8")
    recent, read_ok = mod._read_recent_routings(NOW, 24.0, p)
    assert recent == {}
    assert read_ok is True


def test_most_recent_post_wins_when_a_goal_was_routed_twice(tmp_path):
    """The caller compares against the freshest evidence, so a goal routed at
    both 2h and 30h must key to 2h — otherwise the older post would let a
    just-routed hit re-route immediately."""
    import json
    mod = _import()
    p = tmp_path / "two.json"
    posts = []
    for age in (30.0, 2.0):
        ts = (NOW - dt.timedelta(hours=age)).isoformat(timespec="seconds")
        posts.append({"timestamp": ts, "tags": [mod.ROUTING_TAG, "g-333-33"]})
    p.write_text(json.dumps(posts), encoding="utf-8")
    recent, _ = mod._read_recent_routings(NOW, 24.0, p)
    assert recent == {"g-333-33": 2.0}, recent


def test_scan_window_covers_the_whole_cooldown_with_margin():
    """A read window narrower than the cooldown would drop the very breadcrumbs
    the comparison needs, silently shortening the cooldown to the window."""
    mod = _import()
    assert mod._routing_window_str(24.0) == "25h"
    assert mod._routing_window_str(0.5) == "2h"
    assert mod._routing_window_str(23.1) == "25h"
