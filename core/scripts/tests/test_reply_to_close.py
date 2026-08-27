"""Tests for reply_to_close.decide ().

The load-bearing cases are the REFUSALS. A false close silently deletes real work
(guard-1227); a false refusal costs the user one extra line.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from reply_to_close import (  # noqa: E402
    ACTION_COMPLETE, ACTION_DROP_USER_LEG, ACTION_NOOP,
    decide, find_goal_ids, find_verbs, outcome_note,
)

OWNER = "owner@example.com"

# A realistic digest body. NOTE it contains the literal words "done" and
# "not needed" because it documents the contract -- this is the whole reason the
# verb must be read from the `new` slice only.
DIGEST = """\
Your 72h user-blocker digest -- 2 items awaiting you.

  g-115-6477  Confirm the storage migration window
  g-364-35    Approve the new billing contact

HOW TO CLOSE AN ITEM BY REPLY: reply to this email with a line that is exactly
"done" or "not needed", and quote the single digest line for the item you mean
(or type its goal id). One item per reply.
"""


def _q(body):
    """Render `body` as it appears quoted beneath a reply."""
    return "\n".join("> " + ln for ln in body.splitlines())


def call(new, *, verified=True, sender=OWNER, full=None, mid="msg-1", dup=False):
    return decide(
        verified_sender=verified, sender=sender, owner_address=OWNER,
        new_slice=new, full_slice=(new if full is None else full),
        message_id=mid, already_processed=dup,
    )


# --------------------------------------------------------------- happy paths
def test_done_with_explicit_id_completes():
    d = call("done g-115-6477")
    assert d["action"] == ACTION_COMPLETE
    assert d["goal_id"] == "g-115-6477"


def test_not_needed_drops_user_leg():
    d = call("not needed g-364-35")
    assert d["action"] == ACTION_DROP_USER_LEG
    assert d["goal_id"] == "g-364-35"


def test_hyphenated_and_capitalised_verb_accepted():
    assert call("Not-Needed g-364-35")["action"] == ACTION_DROP_USER_LEG
    assert call("Done. g-115-6477")["action"] == ACTION_COMPLETE


def test_id_recovered_from_a_single_quoted_digest_line():
    """The sanctioned phrasing: bare verb + quote just the one line."""
    quoted = _q("  g-115-6477  Confirm the storage migration window")
    d = call("done", full="done\n" + quoted)
    assert d["action"] == ACTION_COMPLETE
    assert d["goal_id"] == "g-115-6477"
    assert "quoted digest line" in d["reason"]


# ------------------------------------------------------- the rb-5258 defence
def test_quoting_the_whole_digest_does_not_supply_the_verb():
    """The digest TEXT contains 'done' and 'not needed'. Quoting it is not consent.

    This is the defect rb-5258 records, in its exact shape: reading the parent's
    words as the child's intent. With no verb of its own the reply must no-op.
    """
    d = call("Thanks, I'll look at these tomorrow.", full="Thanks...\n" + _q(DIGEST))
    assert d["action"] == ACTION_NOOP
    assert "no recognised verb" in d["reason"]


def test_verb_in_quoted_text_alone_never_fires():
    d = call("", full=_q("done g-115-6477"))
    assert d["action"] == ACTION_NOOP


def test_prose_merely_containing_the_word_does_not_fire():
    for line in ("I am not sure this is done yet",
                 "is this done?",
                 "that one is probably not needed but check first"):
        assert call(line + " g-115-6477")["action"] == ACTION_NOOP, line


# ------------------------------------------------------------- ambiguity
def test_bare_verb_quoting_the_whole_digest_is_ambiguous_not_a_guess():
    d = call("done", full="done\n" + _q(DIGEST))
    assert d["action"] == ACTION_NOOP
    assert "ambiguous" in d["reason"] and d["ack"] is True


def test_two_ids_in_the_reply_text_is_ambiguous():
    d = call("done g-115-6477 g-364-35")
    assert d["action"] == ACTION_NOOP and "ambiguous" in d["reason"]


def test_two_different_verbs_is_ambiguous():
    d = call("done g-115-6477\nnot needed")
    assert d["action"] == ACTION_NOOP and "more than one verb" in d["reason"]


def test_repeated_verb_is_ambiguous():
    d = call("done\ndone")
    assert d["action"] == ACTION_NOOP and "ambiguous" in d["reason"]


def test_verb_with_no_id_anywhere_is_a_noop_with_ack():
    d = call("done")
    assert d["action"] == ACTION_NOOP and d["ack"] is True
    assert "no goal id" in d["reason"]


# ------------------------------------------------------------ sender gates
def test_unverified_sender_is_ignored_and_logged():
    d = call("done g-115-6477", verified=False)
    assert d["action"] == ACTION_NOOP
    assert d.get("logged") is True and d["ack"] is False


def test_verified_but_not_the_owner_is_ignored():
    d = call("done g-115-6477", sender="someone-else@example.com")
    assert d["action"] == ACTION_NOOP and d.get("logged") is True


def test_sender_gate_precedes_content_parsing():
    """An unverified sender controls the content, so content must never be read
    first -- the refusal reason must be about the SENDER even when the body is a
    perfectly well-formed command."""
    d = call("done g-115-6477", verified=False)
    assert "sender" in d["reason"]


def test_owner_match_is_case_insensitive_and_trimmed():
    assert call("done g-115-6477", sender="  OWNER@EXAMPLE.COM ")["action"] == ACTION_COMPLETE


# ------------------------------------------------------------- idempotency
def test_duplicate_delivery_is_a_silent_noop():
    d = call("done g-115-6477", dup=True)
    assert d["action"] == ACTION_NOOP
    assert d["ack"] is False, "a redelivery must not re-mail the user"
    assert "duplicate" in d["reason"]


# ------------------------------------------------------------- helpers
def test_signature_block_is_not_parsed():
    assert call("done g-115-6477\n--\ndone g-364-35")["goal_id"] == "g-115-6477"


def test_attribution_line_is_ignored():
    d = call("done\nOn Mon, Aug 24, 2026 at 9:00 AM Agent wrote:",
             full="done\n" + _q("  g-115-6477  Confirm the window"))
    assert d["action"] == ACTION_COMPLETE


def test_goal_id_width_range():
    ids = find_goal_ids("g-1-01 g-115-6477 g-326-99 g-9999-9999 gx-1-01 g-1-1")
    assert "g-115-6477" in ids and "g-326-99" in ids and "g-1-01" in ids
    assert "g-1-1" not in ids, "suffix is 2-4 digits"


def test_find_verbs_reads_only_whole_lines():
    assert find_verbs("done") == ["done"]
    assert find_verbs("all done here") == []


# ------------------------------------------------------- guard-1227 trace
def test_outcome_note_names_rule_and_evidence():
    d = call("done g-115-6477", mid="msg-abc")
    note = outcome_note(d)
    assert "msg-abc" in note, "the message id is the recoverable evidence"
    assert "reply-to-close" in note and "g-353-51" in note


def test_outcome_note_distinguishes_user_only_skip():
    d = call("not needed g-364-35", mid="msg-xyz")
    assert "no agent leg" in outcome_note(d, user_only=True)
    assert "agent leg is retained" in outcome_note(d, user_only=False)


# ------------------------------------- the SHIPPED footer, not a fixture
def _load_digest_composer():
    """Import the digest composer by path (its filename has hyphens)."""
    import importlib.util
    p = os.path.join(os.path.dirname(__file__), "..",
                     "user-blocker-escalation-check.py")
    spec = importlib.util.spec_from_file_location("_ubec", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_shipped_digest_footer_cannot_self_trigger():
    """The real digest body must contain NO line that reads as a verb.

    A mail client that fails to mark quoted lines drops the footer verbatim into
    the reply's `new` slice. If any footer line began with "done" or "not needed",
    the instructions would become a command against whichever goal id the same
    mail quotes -- the digest would close its own items. This asserts against the
    SHIPPED text, so editing the footer into an unsafe shape fails here.
    """
    mod = _load_digest_composer()
    goal = {"id": "g-115-6477", "title": "Confirm the storage migration window",
            "description": "Reply done or not needed to close this item.",
            "priority": "HIGH", "created_at": "2026-08-01T00:00:00",
            "user_leg_scope": "deployment-approval"}
    cand = {"aspiration_id": "asp-115"}
    body = mod._compose_digest_body([(cand, goal, 183.8, "created_at")], 72.0)

    assert "done" in body.lower(), "sanity: the footer really does mention the verbs"
    assert find_verbs(body) == [], (
        "a digest line reads as a bare verb -- see the INVARIANT comment above the "
        "footer in user-blocker-escalation-check.py")


def test_shipped_footer_quoted_into_a_reply_is_still_inert():
    """End-to-end: the whole digest quoted under a contentless reply does nothing."""
    mod = _load_digest_composer()
    goal = {"id": "g-115-6477", "title": "Confirm the window", "description": "",
            "priority": "HIGH", "created_at": "2026-08-01T00:00:00"}
    body = mod._compose_digest_body([({"aspiration_id": "asp-115"}, goal, 12.0,
                                      "created_at")], 72.0)
    d = call("Got it, thanks.", full="Got it, thanks.\n" + _q(body))
    assert d["action"] == ACTION_NOOP


# ------------------------------------------- write ORDER (guard-1227)
def _apply_mod():
    import importlib.util
    p = os.path.join(os.path.dirname(__file__), "..", "reply_to_close_apply.py")
    spec = importlib.util.spec_from_file_location("_rtca", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _plan(new_text, participants):
    """The dry-run write plan for a reply, with the store lookup stubbed out.

    Asserts on BEHAVIOUR (the returned plan), not on source text. An earlier
    version of these two tests grepped the module source and one of them matched
    its own explanatory comment -- a proxy assertion that failed against correct
    code (the guard-920 shape: test the real thing, not a stand-in for it).
    """
    m = _apply_mod()
    m._goal_record = lambda gid: ({"id": gid, "participants": list(participants),
                                   "status": "pending"}, "world")
    d = call(new_text)
    return m.apply_decision(d, dry_run=True)


def test_outcome_note_is_written_before_any_status_change():
    """A terminal status must never land before the note explaining it.

    The two fields are separate writer calls and either can fail on its own -- the
    uncommitted-work gate refused exactly this status write during the build. If
    status went first, a failure on the note would leave the goal terminal and
    unexplained: it drops out of the selector AND the blocked list, so nobody finds
    it (guard-1227). Reversed, a partial failure is visible and re-runnable.
    """
    plan = _plan("done g-115-6477", ["agent", "user"])["writes"]
    assert plan[0] == "outcome_note", plan
    assert "status" in plan and plan.index("outcome_note") < plan.index("status")


def test_user_only_not_needed_skips_rather_than_reassigning():
    """'not needed' on a user-only goal must not hand the work to the agent."""
    plan = _plan("not needed g-364-35", ["user"])["writes"]
    assert "status" in plan, plan
    assert "participants" not in plan, (
        "a user-only 'not needed' must not rewrite participants -- that would "
        "assign the agent work the owner just declined")


def test_shared_goal_not_needed_drops_only_the_user_leg():
    plan = _plan("not needed g-364-35", ["agent", "user"])["writes"]
    assert "participants" in plan and "status" not in plan, plan


def test_unknown_goal_id_is_a_reportable_noop_not_a_crash():
    m = _apply_mod()
    m._goal_record = lambda gid: (None, None)
    r = m.apply_decision(call("done g-999-9999"), dry_run=True)
    assert r["action"] == ACTION_NOOP and r["ack"] is True
    assert "not found" in r["reason"]
