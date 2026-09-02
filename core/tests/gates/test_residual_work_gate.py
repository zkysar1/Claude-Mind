"""Behavior tests for the residual-work completion gate ( Layer B).

Hermetic: every input is passed in (in-memory queues, tmp world_dir for the
override ledger); no subprocess, no live store. Production arg shape
(guard-920): calls mirror the daemon's in-lock invocation — outcome_note from
the goal record, items = the --source TARGET queue, other_items = the OTHER queue.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from gates.residual_work import (  # via conftest sys.path
    ACTIVE_STATUSES,
    build_successor_goal,
    evaluate,
    find_existing_successor,
)


def _world(*goals):
    return [{"id": "asp-001", "status": "active", "goals": list(goals)}]


def _goal(gid, status="pending", **over):
    return {"id": gid, "status": status, "title": f"t {gid}",
            "description": "", **over}


def _eval(note, *, items=None, other_items=None, override=None,
          world_dir=None, priority=None, category=None):
    """Production arg shape — every kwarg the daemon passes."""
    return evaluate(
        goal_id="g-001-99",
        outcome_note=note,
        override=override,
        items=items if items is not None else _world(),
        other_items=other_items,
        world_dir=world_dir,
        agent_name="alpha",
        goal_priority=priority,
        goal_category=category,
    )


# ---------------------------------------------------------------------------
# Marker scan
# ---------------------------------------------------------------------------

class TestMarkers:
    def test_no_code_written_blocks(self):
        r = _eval("Premises validated. No product code was written this pass.")
        assert r["would_block"] is True
        assert "no_code_written" in r["matched_markers"]

    def test_spec_only_blocks(self):
        r = _eval("Delivered spec only; implementation not started.")
        assert r["would_block"] is True
        assert "spec_only" in r["matched_markers"]

    def test_criteria_only_blocks(self):
        assert _eval("Verification criteria only.")["would_block"] is True

    def test_drafted_not_sent_blocks(self):
        r = _eval("The email was drafted, not sent.")
        assert r["would_block"] is True
        assert "drafted_not_sent" in r["matched_markers"]

    def test_out_of_scope_this_pass_blocks(self):
        r = _eval("Badge rendering was out of scope for this pass.")
        assert r["would_block"] is True

    def test_deferred_to_blocks(self):
        assert _eval("Cleanup deferred to a later cycle.")["would_block"] is True

    def test_remainder_blocks(self):
        assert _eval("The remainder was not attempted.")["would_block"] is True

    def test_negated_follow_up_does_not_fire(self):
        r = _eval("All shipped. No follow-up needed.")
        assert r["would_block"] is False
        assert r["matched_markers"] == []

    def test_without_follow_up_does_not_fire(self):
        assert _eval("Landed cleanly without follow-up.")["would_block"] is False

    def test_clean_outcome_passes(self):
        r = _eval("Implemented, tested, committed as abc1234; suite green.")
        assert r["would_block"] is False
        assert r["matched_markers"] == []

    def test_empty_note_skips(self):
        r = _eval("")
        assert r["would_block"] is False
        assert r["skipped_reason"] == "empty_outcome_note"


# ---------------------------------------------------------------------------
# Accept path 1: live carrier citation (validated against queue state)
# ---------------------------------------------------------------------------

class TestCarrierCitation:
    def test_live_carrier_lifts_block(self):
        items = _world(_goal("g-001-05", "pending"))
        r = _eval("No code was written; residual carried by g-001-05.",
                  items=items)
        assert r["would_block"] is False
        assert r["carrier_refs_found"] == [
            {"goal_id": "g-001-05", "live": True, "status": "pending"}]

    def test_in_progress_carrier_lifts_block(self):
        items = _world(_goal("g-001-05", "in-progress"))
        r = _eval("Spec only — remainder tracked by g-001-05.", items=items)
        assert r["would_block"] is False

    def test_completed_carrier_does_not_lift(self):
        items = _world(_goal("g-001-05", "completed"))
        r = _eval("No code was written; residual carried by g-001-05.",
                  items=items)
        assert r["would_block"] is True
        assert r["carrier_refs_found"][0]["live"] is False

    def test_nonexistent_carrier_does_not_lift(self):
        r = _eval("No code was written; residual carried by g-999-99.")
        assert r["would_block"] is True
        assert r["carrier_refs_found"][0]["status"] is None

    def test_other_queue_carrier_lifts_block(self):
        other_items = _world(_goal("g-001-07", "pending"))
        r = _eval("No code was written; successor filed as g-001-07.",
                  items=_world(), other_items=other_items)
        assert r["would_block"] is False

    def test_self_citation_is_not_a_carrier(self):
        items = _world(_goal("g-001-99", "in-progress"))
        r = _eval("No code was written on g-001-99; remainder carried by "
                  "g-001-99 itself.", items=items)
        assert r["would_block"] is True

    def test_goal_id_far_from_carrier_vocab_is_not_a_citation(self):
        # An id mentioned incidentally (no carrier vocabulary within the
        # window) must not launder the block — pad well past CARRIER_WINDOW.
        pad = "x" * 200
        items = _world(_goal("g-001-05", "pending"))
        r = _eval(f"No code was written. {pad} see g-001-05.", items=items)
        assert r["would_block"] is True
        assert r["carrier_refs_found"] == []


# ---------------------------------------------------------------------------
# Accept path 2: owner decline
# ---------------------------------------------------------------------------

class TestOwnerDecline:
    def test_owner_explicitly_declined(self):
        r = _eval("Follow-up considered; the owner explicitly declined the "
                  "remaining work (board msg-123).")
        assert r["would_block"] is False
        assert r["owner_decline_found"] is True

    def test_declined_by_owner(self):
        r = _eval("Remainder declined by the owner on 2026-08-13.")
        assert r["would_block"] is False

    def test_user_declined(self):
        r = _eval("Spec only; user declined the implementation half.")
        assert r["would_block"] is False


# ---------------------------------------------------------------------------
# Accept path 3: audited override
# ---------------------------------------------------------------------------

class TestOverride:
    def test_override_lifts_block_and_writes_ledger(self, tmp_path: Path):
        r = _eval("No code was written (quoting the incident narrative).",
                  override="marker false-positive: note QUOTES the phrase",
                  world_dir=tmp_path)
        assert r["would_block"] is False
        assert r["override_applied"].startswith("marker false-positive")
        ledger = tmp_path / "residual-work-overrides.jsonl"
        assert ledger.exists()
        rec = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
        assert rec["goal_id"] == "g-001-99"
        assert rec["matched_markers"] == ["no_code_written"]
        assert rec["justification"].startswith("marker false-positive")

    def test_override_without_markers_writes_no_ledger(self, tmp_path: Path):
        r = _eval("All done.", override="unnecessary", world_dir=tmp_path)
        assert r["would_block"] is False
        assert not (tmp_path / "residual-work-overrides.jsonl").exists()

    def test_missing_world_dir_fails_open(self):
        r = _eval("No code was written.", override="fp", world_dir=None)
        assert r["would_block"] is False

    # --- precedence: EXPLICIT override outranks INFERRED owner-decline ----
    # . These three were RED before the accept paths stopped
    # early-returning in trust order.

    def test_override_beats_prose_that_trips_owner_decline(self,
                                                           tmp_path: Path):
        """The measured  silent bypass. The note ARGUES AGAINST the
        owner-decline exit, `OWNER_DECLINE_RE` matches it anyway, and before
        the fix that inference returned first — so the close was accepted on
        the exact exit its own text rejected, `override_applied` stayed None,
        and the promised audit row was never written. An accept-path false
        positive is worse than a block-marker one: it lets the close through
        AND drops the audit trail."""
        note = ("No code was written. The gate's other exit, an owner "
                "decline, is equally inaccurate — nothing is being declined.")
        r = _eval(note, override="marker FP: quoting the incident",
                  world_dir=tmp_path)
        assert r["would_block"] is False
        assert r["override_applied"] == "marker FP: quoting the incident", (
            "the inferred owner-decline pre-empted the explicit override")
        ledger = tmp_path / "residual-work-overrides.jsonl"
        assert ledger.exists(), (
            "override accepted the close but wrote NO audit row — the silent "
            "bypass this fix exists to remove")
        rec = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
        assert rec["justification"].startswith("marker FP")

    def test_override_is_audited_even_when_a_live_carrier_exists(
            self, tmp_path: Path):
        """"Always write the ledger row when an override was passed" is
        unconditional. A live carrier previously returned first, so a
        superfluous-but-passed override left no trace of having been used."""
        items = _world(_goal("g-001-07", "pending"))
        r = _eval("No code was written; residual carried by g-001-07.",
                  items=items, override="belt and braces", world_dir=tmp_path)
        assert r["would_block"] is False
        assert r["override_applied"] == "belt and braces"
        assert (tmp_path / "residual-work-overrides.jsonl").exists()

    def test_owner_decline_still_accepts_without_an_override(self):
        """Reordering must not disable path 3 — it is still an accept path,
        just the least-trusted one."""
        r = _eval("Remainder declined by the owner on 2026-08-13.")
        assert r["would_block"] is False
        assert r["owner_decline_found"] is True


# ---------------------------------------------------------------------------
# Layer-D suggestion
# ---------------------------------------------------------------------------

class TestSuggestion:
    def test_title_lifts_the_residual_clause(self):
        r = _eval("Analysis complete. No product code was written this pass. "
                  "Suite untouched.")
        assert r["successor_title"] == (
            "Residual: No product code was written this pass. (from g-001-99)")
        assert "g-001-99" in r["successor_description"]

    def test_long_clause_truncated(self):
        clause = ("No code was written because " + "reason " * 40).strip()
        r = _eval(clause + ".")
        assert len(r["successor_title"]) < 120
        assert r["successor_title"].startswith("Residual: ")

    def test_priority_and_category_inherit(self):
        r = _eval("No code was written.", priority="HIGH",
                  category="product-development")
        assert r["successor_priority"] == "HIGH"
        assert r["successor_category"] == "product-development"

    def test_priority_defaults_medium_on_junk(self):
        r = _eval("No code was written.", priority="urgent!!")
        assert r["successor_priority"] == "MEDIUM"


# ---------------------------------------------------------------------------
# Dedup (find_existing_successor) — 3 strategies, active-only, cross-queue
# ---------------------------------------------------------------------------

class TestDedup:
    def test_origin_signal_strategy(self):
        succ = _goal("g-001-07", "pending",
                     origin_signal="residual:g-001-99")
        hit = find_existing_successor(_world(succ), "g-001-99")
        assert hit["_match_strategy"] == "origin_signal"

    def test_title_prefix_strategy(self):
        succ = _goal("g-001-08", "pending",
                     title="Residual: finish X (from g-001-99)")
        hit = find_existing_successor(_world(succ), "g-001-99")
        assert hit["_match_strategy"] == "title_prefix"

    def test_description_proximity_strategy(self):
        succ = _goal("g-001-09", "in-progress",
                     description="carries the residual work of g-001-99")
        hit = find_existing_successor(_world(succ), "g-001-99")
        assert hit["_match_strategy"] == "description_proximity"

    def test_resolved_successor_never_matches(self):
        succ = _goal("g-001-07", "completed",
                     origin_signal="residual:g-001-99")
        assert find_existing_successor(_world(succ), "g-001-99") is None

    def test_other_queue_scanned(self):
        succ = _goal("g-001-07", "pending",
                     origin_signal="residual:g-001-99")
        hit = find_existing_successor(_world(), "g-001-99",
                                      other_items=_world(succ))
        assert hit["_source"] == "other"

    def test_unrelated_goal_does_not_match(self):
        other = _goal("g-001-07", "pending",
                      title="Layer-B gate build referencing g-001-99",
                      description="the incident goal g-001-99 is context "
                                  "only, nothing about carrying work")
        assert find_existing_successor(_world(other), "g-001-99") is None


# ---------------------------------------------------------------------------
# Successor record
# ---------------------------------------------------------------------------

class TestBuildSuccessor:
    def test_shape(self):
        gr = _eval("No code was written.", priority="HIGH",
                   category="product-development")
        g = build_successor_goal("g-001-99", gr, "g-001-42")
        assert g["id"] == "g-001-42"
        assert g["origin_signal"] == "residual:g-001-99"
        assert g["status"] == "pending"
        assert g["priority"] == "HIGH"
        assert g["category"] == "product-development"
        assert g["participants"] == ["agent"]
        assert g["tags"] == ["residual", "residual-gate-routed"]
        assert g["alloc_nonce"]
        assert g["verification"] == {"outcomes": [], "checks": [],
                                     "preconditions": []}
        # The built goal must itself be dedup-visible: filing then re-running
        # find_existing_successor on the same items is an idempotent skip.
        items = _world(g)
        assert find_existing_successor(
            items, "g-001-99")["_match_strategy"] == "origin_signal"

    def test_active_statuses_constant(self):
        assert ACTIVE_STATUSES == ("pending", "in-progress")


# ---------------------------------------------------------------------------
# Accept path 4 — provenance disclaimer vs genuine residual ()
# ---------------------------------------------------------------------------

# The exact clause that tripped the gate closing  (zeta, cc-02,
# 2026-08-20). Verbatim on purpose: guard-920 — a regression test must
# replicate the literal production input, not a contract-ideal paraphrase.
G335_1320_CLAUSE = (
    "No code was written by me and no PR was opened; the work landed in "
    "Vinheim-Web-App PR #229 (commit 410d953, 'feat(sessions): the unified "
    "session view')."
)


class TestProvenanceAcceptPath:
    def test_g335_1320_clause_no_longer_blocks(self):
        """THE REGRESSION: attribution + artifact ref must not block."""
        r = _eval(G335_1320_CLAUSE)
        assert r["would_block"] is False
        assert r["provenance_found"] is True
        # The marker still MATCHES — the fix is an accept path, not a
        # weakened marker. Losing the match would hide the case from audit.
        assert "no_code_written" in r["matched_markers"]

    def test_positive_control_genuine_residual_still_blocks(self):
        """THE CONTROL: same marker, no attribution, no artifact -> blocks.

        If this ever goes green the fix has over-widened and the gate has
        stopped catching real stranded work — which is the failure mode the
        gate exists to prevent, and strictly worse than the false positive
        being fixed here.
        """
        r = _eval("No code was written; the implementation still needs doing.")
        assert r["would_block"] is True
        assert r["provenance_found"] is False

    def test_attribution_without_artifact_still_blocks(self):
        """Prose alone is unfalsifiable — both conjuncts are required."""
        r = _eval("No code was written; the work landed in another repo.")
        assert r["would_block"] is True
        assert r["provenance_found"] is False

    def test_artifact_without_attribution_still_blocks(self):
        """A bare sha near a residual is a cross-reference, not provenance."""
        r = _eval("No code was written. Context for the reviewer: 410d953.")
        assert r["would_block"] is True
        assert r["provenance_found"] is False

    def test_provenance_is_clause_scoped_not_note_scoped(self):
        """A citation in a DIFFERENT sentence must not suppress the marker.

        This is the blanket-bypass failure mode: long outcome_notes routinely
        cite some merged PR about an unrelated matter, and a note-wide scan
        would let any of them lift a genuine residual.
        """
        note = ("Background: the parser rewrite landed in PR #101 "
                "(commit 1a2b3c4). Separately, no code was written for the "
                "retry path and it still needs implementing.")
        r = _eval(note)
        assert r["would_block"] is True
        assert r["provenance_found"] is False

    def test_explicit_override_still_outranks_provenance(self):
        """Precedence (): an EXPLICIT signal always outranks an
        INFERRED one, and a passed override is always audited."""
        r = _eval(G335_1320_CLAUSE, override="justified")
        assert r["override_applied"] == "justified"
        assert r["would_block"] is False

    def test_hex_word_is_not_read_as_a_commit(self):
        """'defaced' is 7 chars of [a-f] — the digit requirement excludes it."""
        r = _eval("No code was written; the work landed in the defaced repo.")
        assert r["provenance_found"] is False
        assert r["would_block"] is True


# ---------------------------------------------------------------------------
# Bare-noun markers: `remainder` / `successor` ()
# ---------------------------------------------------------------------------

# VERBATIM from world/residual-work-overrides.jsonl. guard-920: a regression
# test must replicate the literal production input, not a paraphrase — and
# here the ledger IS the ground truth the goal was scoped against, so a
# reworded clause would test a case that never happened.
#
# class B — the author is DECLINING to file a carrier. Negated or subjunctive.
LEDGER_DECLINE_CLAUSES = [
    "- Did not file a successor.",
    'No successor goal filed: filing one to "fix the union" would re-create '
    "the trap",
    "Did not file a Case-A remainder goal for the resolution — verified the",
    "Filing a successor would re-open finished work",
    "Filing a fix goal off one anecdote would route a successor into changing "
    "a commutativity invariant on the shared work queue.",
]

# class A — the NOUN with a non-work referent (an API, a code comment, a lane
# label, a data field). No negation appears anywhere in these, which is the
# whole reason a negation guard cannot reach them.
LEDGER_BARE_NOUN_CLAUSES = [
    "FireCustomEvent is deprecated, and its successor",
    "The list was amended **twice today**: asp-363 (echo, revenue-lane "
    "successor,",
    "Successor to g-326-68 (whose record vanished); this outcome cites "
    "g-326-68 so the three",
    "returning `(verdict, reason)` TUPLES, and the whole non-conflicting "
    "remainder of that function",
    "frees the successor.",
    "timestamps and the appended remainder now reads 2026-08-30 instead of "
    "its true",
]


class TestBareNounMarkers:
    @pytest.mark.parametrize("clause", LEDGER_DECLINE_CLAUSES)
    def test_filing_decline_does_not_fire(self, clause):
        """class B: 'did not file a successor' is the OPPOSITE of a residual."""
        r = _eval(clause)
        assert "successor" not in r["matched_markers"]
        assert "remainder" not in r["matched_markers"]

    @pytest.mark.parametrize("clause", LEDGER_BARE_NOUN_CLAUSES)
    def test_bare_noun_without_work_context_does_not_fire(self, clause):
        """class A: the noun refers to an API / comment / label / data field.

        These carry no negation at all, so they are the proof that class A is
        an ANCHORING problem and not a negation problem — the decision this
        goal asked to be recorded.
        """
        r = _eval(clause)
        assert "successor" not in r["matched_markers"]
        assert "remainder" not in r["matched_markers"]

    # --- positive controls: over-suppression is the worse failure ----------

    def test_genuine_filed_successor_still_fires(self):
        """THE CONTROL the goal names. If this goes silent the markers have
        been disabled rather than narrowed, and real stranded work ships."""
        r = _eval("Filed a successor: g-115-1234 carries the rest.")
        assert "successor" in r["matched_markers"]

    def test_negated_WORK_verb_still_fires(self):
        """The trap that makes a blanket negation guard wrong.

        'The remainder was NOT attempted' is a negated clause that asserts
        residual work EXISTS. The negation must scope to the FILING verb
        (did not file), never to the work verb (not attempted). This is also
        a pre-existing control in TestMarkers — pinned again here because the
        bare-noun guard is what could silently break it.
        """
        r = _eval("The remainder was not attempted.")
        assert "remainder" in r["matched_markers"]
        assert r["would_block"] is True

    def test_successor_goal_needed_still_fires(self):
        assert "successor" in _eval(
            "A successor goal is needed for the untouched half."
        )["matched_markers"]

    def test_remainder_unfinished_still_fires(self):
        assert "remainder" in _eval(
            "The remainder of the migration remains unfinished."
        )["matched_markers"]

    def test_owner_declined_remainder_still_reaches_accept_path_3(self):
        """`declin*` is WORK context, not a decline-to-file: it asserts a
        remainder exists and routes it to the owner-decline accept path.
        Suppressing the marker here would silently disable path 3."""
        r = _eval("Remainder declined by the owner on 2026-08-13.")
        assert "remainder" in r["matched_markers"]
        assert r["owner_decline_found"] is True
        assert r["would_block"] is False

    # --- scan semantics ----------------------------------------------------

    def test_class_a_mention_does_not_mask_a_later_real_residual(self):
        """Why the guard uses finditer, not search.

        `search` stops at the FIRST hit. A note that opens with an API-sense
        'successor' and later names genuine undone work would be waved
        through on the strength of the irrelevant first mention — a
        false NEGATIVE introduced by the false-positive fix.
        """
        note = ("FireCustomEvent is deprecated, and its successor "
                "LogCustomEvent is live. Separately, a successor goal is "
                "still needed for the retry path.")
        r = _eval(note)
        assert "successor" in r["matched_markers"]
        assert r["would_block"] is True

    def test_phrase_markers_are_untouched_by_the_noun_guard(self):
        """Scope control: the guard applies to the two bare NOUNS only.
        `follow_up` keeps its own lookbehinds and every phrase marker is
        unchanged — verified by asserting one of each still behaves."""
        assert _eval("All shipped. No follow-up needed.")["would_block"] is False
        assert _eval("Cleanup deferred to a later cycle.")["would_block"] is True
