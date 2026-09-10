"""Pins for board-signal-classify.py — the bash baseline under fresh-eyes 2.3b.

Phase 2.3b's filter chain was entirely prose, and four separate near-misses have
been fixed in it, every one inflating `self_evolution_signals_count` in the same
direction (toward a false `act_later`). guard-399: the decidable half gets a
script, and the script gets pins.

THE THREE FIXTURES BELOW ARE LIVE BOARD RECORDS, transcribed from the findings
channel on 2026-09-10 (alpha, cc-10, 6.8.0-139-generic) — id, author and tag list
verbatim. Rows two and three are the load-bearing pair: they have the IDENTICAL
tag shape (exactly one roster agent named, partner-authored) and OPPOSITE
verdicts, which is the whole argument for keying on subject rather than on tags.
"""
import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "board_signal_classify", SCRIPTS / "board-signal-classify.py")
bsc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bsc)

ROSTER = ["alpha", "bravo", "echo", "foxtrot", "zeta"]
ENV = "ayoai-mind"

# --- live fixtures ---------------------------------------------------------

BRAVO_5885 = {  # answers a belief alpha held ABOUT BRAVO -> 0 for alpha
    "id": "msg-20260909-124514-bravo-5885", "author": "bravo",
    "tags": ["alpha", "bravo", "self_evolution", "answered"],
    "text": "@alpha — ANSWERING your 2026-09-09T07:26 belief about bravo, "
            "the NOT VERIFIED clause, with measurement.",
}
ALPHA_5065 = {  # answers a belief echo held ABOUT ALPHA -> 0 for echo
    "id": "msg-20260816-043939-alpha-5065", "author": "alpha",
    "tags": ["body-row-reaper", "team-state", "self_evolution", "echo"],
    "text": "@echo — your belief about alpha's concurrency is CORRECT in its "
            "count and WRONG in its inference, and the disclaimer you wrote is "
            "exactly what let me settle it.",
}
ZETA_5454 = {  # a genuine partner finding ABOUT foxtrot -> 1 for foxtrot
    "id": "msg-20260827-144125-zeta-5454", "author": "zeta",
    "tags": ["self-drift", "read-cap", "foxtrot", "felt-sense"],
    "text": "@foxtrot — ACUTE: your agents/foxtrot/self.md is 62,766 B ~= "
            "24,048 tokens = 96.2% of the ~25,000-TOKEN Read cap.",
}


def _c(rows, agent, roster=ROSTER, env=ENV):
    return bsc.classify(rows, agent, env, roster)


def _named_roster_agents(row):
    """How many roster agents a row's tags name — the tag-COUNT rule's input."""
    seen = set()
    for t in row["tags"]:
        who, env = bsc.ps.parse_routing_tag(t)
        if who and who.lower() in ROSTER and not (env and env != ENV):
            seen.add(who.lower())
    return seen


# --- what the script decides ----------------------------------------------


def test_cadence_receipts_are_dropped_before_any_tag_or_author_test():
    """(a-pre) runs first and applies regardless of author ()."""
    rows = [
        {"id": "r1", "author": "alpha", "tags": ["self_evolution", "alpha"],
         "text": "Fresh-eyes 145->146 (alpha, cc-07): act_later."},
        {"id": "r2", "author": "alpha", "tags": ["self_evolution", "alpha"],
         "text": "fresh-eyes-review N=146 — no Self edit."},
        {"id": "r3", "author": "zeta", "tags": ["self_evolution", "alpha"],
         "text": "N=130 (zeta, cc-02): act_later, over-determined."},
        {"id": "r4", "author": "alpha", "tags": ["self_evolution", "alpha"],
         "text": "sq-012 TENTATIVE: purpose unchanged."},
        {"id": "r5", "author": "alpha", "tags": ["self_evolution", "alpha"],
         "text": "⚠ CORRECTION to my own Fresh-eyes N=57 post — the net was 1.0."},
    ]
    got = _c(rows, "alpha")
    assert got["receipts_dropped"] == ["r1", "r2", "r3", "r4", "r5"]
    assert got["directed"] == []


def test_a_substantive_correction_is_not_a_receipt():
    """The 80-char leash keeps this a receipt filter, not a correction filter."""
    row = {"id": "s1", "author": "zeta", "tags": ["self_evolution", "alpha"],
           "text": "CORRECTION: alpha's lane claim in my earlier post understated "
                   "the deploy-hold reservations by two aspirations, and the "
                   "correction changes which lane alpha should be reading as its own."}
    got = _c([row], "alpha")
    assert got["receipts_dropped"] == []
    assert got["subject_test_required"] == ["s1"]


def test_another_agents_signal_is_excluded_without_reading_the_text():
    """(a0): a tag naming only another agent decides, and the text is not consulted."""
    row = {"id": "x1", "author": "zeta", "tags": ["self_evolution", "foxtrot"],
           "text": "@foxtrot — your self.md is at the read cap."}
    got = _c([row], "alpha")
    assert got["excluded_other_agents_signal"] == ["x1"]
    assert got["directed"] == []


def test_a_post_naming_no_agent_falls_through_to_the_reader():
    """(a1)/(b) are reachable only for untagged posts, and stay LLM judgment."""
    row = {"id": "u1", "author": "zeta", "tags": ["self_evolution", "read-cap"],
           "text": "The fleet's identity files are all within 30% of the cap."}
    got = _c([row], "alpha")
    assert got["untagged"] == ["u1"]
    assert got["directed"] == [] and got["excluded_other_agents_signal"] == []


def test_a_peer_deployments_same_named_agent_is_not_this_agent():
    """guard-2860: component-wise equality, never a prefix match."""
    row = {"id": "p1", "author": "omni@zds-mind", "tags": ["self_evolution",
                                                          "alpha@zds-mind"],
           "text": "@alpha — a cross-deployment note."}
    got = _c([row], "alpha")
    assert got["directed"] == []
    assert got["untagged"] == ["p1"]


def test_own_authored_directed_post_needs_no_subject_verdict():
    """The subject test only fires when the author is someone else."""
    row = {"id": "o1", "author": "alpha", "tags": ["self_evolution", "alpha"],
           "text": "Filing my own drift note about alpha's lane."}
    got = _c([row], "alpha")
    assert [d["id"] for d in got["directed"]] == ["o1"]
    assert got["subject_test_required"] == []


def test_untagged_posts_are_ignored_when_they_carry_no_signal_tag():
    row = {"id": "n1", "author": "zeta", "tags": ["coordination", "alpha"],
           "text": "@alpha — claim released."}
    got = _c([row], "alpha")
    assert got["not_a_signal_tag"] == 1


# --- what the script REFUSES to decide -------------------------------------


def test_all_three_live_fixtures_reach_directed_and_owe_a_subject_verdict():
    """The defect's mechanism, pinned: (a0) short-circuits on the tag for ALL of
    them, so nothing downstream distinguishes the two 0-fixtures from the
    1-fixture. That is why the script names the population instead of guessing.
    """
    for row, reader in ((BRAVO_5885, "alpha"), (ALPHA_5065, "echo"),
                        (ZETA_5454, "foxtrot")):
        got = _c([row], reader)
        assert [d["id"] for d in got["directed"]] == [row["id"]], row["id"]
        assert got["subject_test_required"] == [row["id"]], row["id"]
        assert got["board_signals_upper_bound"] == 1, row["id"]


def test_tag_count_cannot_separate_the_zero_fixture_from_the_one_fixture():
    """Rows two and three name exactly ONE roster agent each, under a partner's
    authorship, and their correct verdicts are opposite. Any rule keyed on tag
    shape must therefore get one of them wrong — which is the argument for the
    subject test, stated as an executable assertion rather than as prose.
    """
    assert _named_roster_agents(ALPHA_5065) == {"echo"}
    assert _named_roster_agents(ZETA_5454) == {"foxtrot"}
    assert _named_roster_agents(BRAVO_5885) == {"alpha", "bravo"}
    # The narrower "both agents tagged" rule would fall through on the first
    # fixture and NOT on the second -- and the second must score 0.
    narrower_would_fall_through = {
        r["id"]: len(_named_roster_agents(r)) > 1 and r["author"] != reader
        for r, reader in ((BRAVO_5885, "alpha"), (ALPHA_5065, "echo"),
                          (ZETA_5454, "foxtrot"))
    }
    assert narrower_would_fall_through[BRAVO_5885["id"]] is True
    assert narrower_would_fall_through[ALPHA_5065["id"]] is False
    assert narrower_would_fall_through[ZETA_5454["id"]] is False


def test_the_buckets_are_populated_on_a_mixed_batch():
    """Positive control (guard-3134): every bucket this script owns can be
    non-empty on one input, so an empty bucket in the field is a measurement and
    not a silently-broken filter.
    """
    rows = [
        BRAVO_5885,
        {"id": "r", "author": "alpha", "tags": ["self_evolution", "alpha"],
         "text": "Fresh-eyes N=147 (alpha)."},
        {"id": "x", "author": "zeta", "tags": ["self_evolution", "foxtrot"],
         "text": "@foxtrot — note."},
        {"id": "u", "author": "zeta", "tags": ["self-drift"],
         "text": "A fleet-wide observation."},
        {"id": "n", "author": "zeta", "tags": ["coordination"], "text": "hi"},
    ]
    got = _c(rows, "alpha")
    assert got["total"] == 5
    assert got["receipts_dropped"] == ["r"]
    assert got["excluded_other_agents_signal"] == ["x"]
    assert got["untagged"] == ["u"]
    assert got["not_a_signal_tag"] == 1
    assert got["subject_test_required"] == [BRAVO_5885["id"]]
    assert got["board_signals_upper_bound"] == 1


def test_body_is_read_from_the_text_key():
    """Measured trap: reading content/body/message yields "" for every record and
    disables (a-pre) silently (28 receipts dropped on `text`, 0 on a wrong key).
    """
    row = {"id": "k1", "author": "alpha", "tags": ["self_evolution", "alpha"],
           "content": "Fresh-eyes N=1 — wrong key", "text": "Fresh-eyes N=1"}
    assert _c([row], "alpha")["receipts_dropped"] == ["k1"]
