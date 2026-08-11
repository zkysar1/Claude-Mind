"""test_release_announce_pairing.py — bind BOTH release producers to the REAL
consumer (g-306-194, addendum).

WHY THIS FILE EXISTS. Two independent places emit a "I released this goal" board
post, and a THIRD place parses those posts to clear the claim/release lien:

    producer A  core/scripts/stranded-claim-sweep.py :: _announce_release
    producer B  .claude/skills/aspirations-consolidate/SKILL.md Step 8.9
    consumer    core/scripts/goal-pickup-coordination-check.py
                :: classify_board_mentions -> _released_ids
                :: supersede_released_claims

Nothing bound them. g-306-169 shipped producer A with six tests, but all six pin
the PRODUCER half against a fake `_rt` — they assert the call was made, never
that the emitted post is one the consumer can READ. The consumer leg was checked
once by hand in a throwaway script that was not kept. So either side could drift
and both files' own suites would stay green: `_released_ids`' three legs could be
re-specified, or a producer's type/tags/prefix could change, and no test compares
them.

Producer B is the live proof that this is not hypothetical. Until g-306-194 it
posted ONE message per session — "Session ending: released all held claims",
`--type status`, no tags — which matches NONE of the consumer's three legs. Every
claim it released stayed an unpaired lien. And because a post EXISTED, a reader
asking "did we announce?" saw yes: the defect was invisible from the producer
side, which is exactly what a producer-only test certifies.

WHAT IS ASSERTED, per producer:
  1. the emitted post classifies as kind == "release" for the goal id
  2. it does NOT classify as a claim (an incoherent post stays a claim — the
     conservative direction — so this is a real discriminator, not a tautology)
  3. it SUPERSEDES a prior claim by the same author (the lien actually clears)

Both consumer functions are IMPORTED, never re-implemented (guard-2323). The
whole point is to compare against the code that really runs.

HOW PRODUCER B IS REACHED. It is SKILL.md pseudocode, so there is no function to
call; the test extracts the emitted `--type`, `--tags` and echoed text from the
file itself. That is deliberate: pseudocode drift is precisely the failure mode
here, and parsing the file is the only mechanism that can observe it. The
extractor FAILS LOUDLY if it cannot find the step or the invocation, so a rename
or a move produces a red test rather than a silently vacuous pass (guard-2582 —
a checker's silence is not coverage until you confirm the file is in its
population).
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
REPO = CORE_SCRIPTS.parent.parent
SKILL_MD = REPO / ".claude" / "skills" / "aspirations-consolidate" / "SKILL.md"

GOAL_ID = "g-315-518"
AUTHOR = "alpha"          # the releasing agent == the prior claim holder
READER = "bravo"          # the consumer's `me`; must differ (own posts are skipped)
CLAIM_TS = "2026-08-07T14:28:22"
RELEASE_TS = "2026-08-07T18:53:22"


def _load(mod_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(mod_name, CORE_SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


CONSUMER = _load("goal_pickup_coordination_check",
                 "goal-pickup-coordination-check.py")


# ---------------------------------------------------------------- producer A


class _CapturingRt:
    """Stand-in for the sweep's `_rt` that records the call instead of posting."""

    class RtError(Exception):
        pass

    def __init__(self):
        self.calls = []

    def rt_call(self, method, path, query=None, body=None, **kw):
        self.calls.append({"method": method, "path": path,
                           "query": dict(query or {}), "body": body})
        return "{}"


def _producer_a_post():
    """Run the REAL _announce_release and return what it actually emitted."""
    sweep = _load("stranded_claim_sweep", "stranded-claim-sweep.py")
    cap = _CapturingRt()
    sweep._rt = cap
    result = sweep._announce_release(
        GOAL_ID, "world", AUTHOR,
        {"reason": "foreign-session grace expired", "age_minutes": 265,
         "claimed_by_sid": "8ef15025-11ab-4b0f-9527-e620696e1ec6"},
    )
    assert result.get("posted") is True, result
    assert len(cap.calls) == 1, cap.calls
    call = cap.calls[0]
    q = call["query"]
    return {
        "type": str(q.get("type") or ""),
        "tags": [t for t in str(q.get("tags") or "").split(",") if t],
        "text": str(call["body"] or ""),
        "author": str(q.get("author") or ""),
    }


# ---------------------------------------------------------------- producer B


_STEP_HEAD = re.compile(r"^8\.9\.\s", re.M)
_NEXT_STEP = re.compile(r"^9\.\s", re.M)


def _step_8_9_block() -> str:
    text = SKILL_MD.read_text(encoding="utf-8", errors="replace")
    head = _STEP_HEAD.search(text)
    assert head, (
        "Step 8.9 not found in %s — the extractor is pointed at nothing, so a "
        "PASS here would be vacuous. If the step was renumbered, update "
        "_STEP_HEAD; do not delete this assertion." % SKILL_MD)
    tail = _NEXT_STEP.search(text, head.end())
    block = text[head.start(): tail.start() if tail else len(text)]
    assert "board-post.sh" in block, (
        "Step 8.9 contains no board-post.sh invocation — either the release "
        "announce was removed (the g-306-194 defect, reintroduced) or it moved.")
    return block


def _producer_b_post():
    """Extract what Step 8.9's pseudocode actually emits, from the file."""
    block = _step_8_9_block()

    post_line = None
    for line in block.splitlines():
        if "board-post.sh" in line and "--type" in line:
            post_line = line
            break
    assert post_line, (
        "Step 8.9 has a board-post.sh call but none carrying --type. The "
        "consumer's strongest leg is type==\"release\"; a typeless post "
        "defaults to `status` and matches nothing.")

    mtype = re.search(r"--type\s+(\S+)", post_line)
    assert mtype, "no --type value on the Step 8.9 board-post line"
    tags_m = re.search(r'--tags\s+"([^"]*)"', post_line)
    tags_raw = tags_m.group(1) if tags_m else ""

    # The echoed body — the line above the pipe, or the same line.
    echo_m = re.search(r'echo\s+"([^"]*)"', block)
    assert echo_m, "no echoed body found in Step 8.9"

    def _fill(s: str) -> str:
        return (s.replace("{goal.goal_id}", GOAL_ID)
                 .replace("{goal.id}", GOAL_ID)
                 .replace("{agent_name}", AUTHOR)
                 .replace("{MIND_SID[:8]}", "8ef15025"))

    return {
        "type": _fill(mtype.group(1)),
        "tags": [t for t in _fill(tags_raw).split(",") if t],
        "text": _fill(echo_m.group(1)),
        "author": AUTHOR,
    }


PRODUCERS = [
    pytest.param(_producer_a_post, id="A-stranded-claim-sweep._announce_release"),
    pytest.param(_producer_b_post, id="B-aspirations-consolidate-Step-8.9"),
]


# ------------------------------------------------------- the binding helper


def _classify(post, extra_messages=()):
    """Feed a producer's REAL emitted post to the REAL consumer."""
    messages = list(extra_messages) + [{
        "id": "msg-release",
        "author": post["author"],
        "timestamp": RELEASE_TS,
        "type": post["type"],
        "text": post["text"],
        "tags": post["tags"],
    }]
    return CONSUMER.classify_board_mentions(GOAL_ID, READER, messages)


_PRIOR_CLAIM = {
    "id": "msg-claim",
    "author": AUTHOR,
    "timestamp": CLAIM_TS,
    "type": "claim",
    "text": "Claiming %s: worker-learning phase B" % GOAL_ID,
    "tags": [GOAL_ID, AUTHOR],
}


def test_step_8_9_foreign_sid_guard_has_the_right_polarity():
    """The OTHER half of , and the one most likely to be "improved"
    into the exact defect it fixes.

    Step 8.9 must release only claims whose `claimed_by_sid` IS this session's
    ($MIND_SID). The tempting alternative — compare against
    `running-session-id` — is WRONG and the goal is explicit about why:
    running-session-id is BOX-LOCAL, so `claimed_by_sid != running-session-id`
    is ALSO true of a genuine live peer on another box, i.e. the naive guard
    releases exactly the claims the guard exists to protect.

    This is a structural pin, not a behavioural one — Step 8.9 is pseudocode and
    there is no function to call. It is deliberately narrow: it asserts the
    comparison operand, which is the single decision a future editor is most
    likely to get wrong, rather than pretending to test the whole step.

    ANCHORED TO NON-COMMENT LINES, and that is not a detail. The step's own
    comment EXPLAINS why running-session-id is the wrong operand, so a
    whole-block substring test cannot tell "warns against X" from "does X" — my
    first draft asserted over the raw block and went red against the correct
    file. Same shape as guard-1099, where an unanchored grep counted the
    comments quoting a deleted glob as live code and reported PASS."""
    block = _step_8_9_block()
    code = "\n".join(l for l in block.splitlines()
                     if not l.lstrip().startswith("#"))
    assert "$MIND_SID" in code, (
        "Step 8.9's executable lines no longer compare the holder against "
        "$MIND_SID — the foreign-SID guard is gone or keyed on something else.")
    assert "running-session-id" not in code, (
        "Step 8.9 now USES running-session-id in an executable line. That value "
        "is BOX-LOCAL, so as a release predicate it makes every genuine live "
        "peer look foreign — the g-306-194 defect, reintroduced as its own fix. "
        "(Mentioning it in a comment is fine and is why this check ignores "
        "comment lines.)")
    assert "continue" in code, (
        "the guard has no skip path — a comparison that never skips anything "
        "is decoration.")


@pytest.mark.parametrize("producer", PRODUCERS)
def test_emitted_post_reads_as_a_release(producer):
    hits = _classify(producer())
    kinds = [h["kind"] for h in hits]
    assert "release" in kinds, (
        "the post this producer really emits does NOT classify as a release for "
        "%s — it matches none of _released_ids' three legs. hits=%r" %
        (GOAL_ID, hits))


@pytest.mark.parametrize("producer", PRODUCERS)
def test_emitted_post_is_not_read_as_a_claim(producer):
    hits = _classify(producer())
    assert "claim" not in [h["kind"] for h in hits], (
        "the release post classifies as a CLAIM — it would ADD a lien instead "
        "of clearing one. hits=%r" % (hits,))


@pytest.mark.parametrize("producer", PRODUCERS)
def test_emitted_post_supersedes_the_prior_same_author_claim(producer):
    hits = _classify(producer(), extra_messages=[_PRIOR_CLAIM])
    live, superseded = CONSUMER.supersede_released_claims(hits)
    assert [h["id"] for h in superseded] == ["msg-claim"], (
        "the prior claim by %s was NOT superseded, so the lien survives the "
        "release. live=%r superseded=%r" % (AUTHOR, live, superseded))
    assert "claim" not in [h["kind"] for h in live]


def test_the_prior_claim_alone_is_a_live_lien():
    """Negative control. Without any release post the claim MUST stay live —
    otherwise the three assertions above would pass against a consumer that
    never held a lien in the first place, and would be measuring nothing."""
    hits = CONSUMER.classify_board_mentions(GOAL_ID, READER, [_PRIOR_CLAIM])
    live, superseded = CONSUMER.supersede_released_claims(hits)
    assert [h["kind"] for h in live] == ["claim"]
    assert superseded == []


# The consumer's rule, read off _released_ids rather than guessed:
#   pairs  <=>  ( (type=="release" OR a release-marker tag) AND the id is in
#                 goal-id-shaped tags )
#               OR  the text matches ^RELEASING <id>
# The legs are REDUNDANT, so no single drift breaks the pairing — which is the
# design, and is why the matrix below asserts HOLDS for the singles instead of
# pretending each one is fatal. My first draft of this test asserted the
# opposite and went red; the consumer was right and the expectation was wrong.
_MUTATIONS = [
    # (drop_type, drop_id_tag, drop_prefix, expect_paired, why)
    pytest.param(False, False, False, True,
                 "unmutated — all three legs present",
                 id="none"),
    pytest.param(True, False, False, True,
                 "type downgraded to status; the ^RELEASING <id> prefix carries it",
                 id="drop-type"),
    pytest.param(False, True, False, True,
                 "id dropped from tags; the text prefix carries it",
                 id="drop-id-tag"),
    pytest.param(False, False, True, True,
                 "prefix gone; type==release + id tag still satisfy leg 1",
                 id="drop-prefix"),
    pytest.param(True, True, False, True,
                 "only the text prefix survives — and it is sufficient alone",
                 id="drop-type+id-tag"),
    pytest.param(True, False, True, False,
                 "id is in tags but nothing marks the post as a release",
                 id="drop-type+prefix"),
    pytest.param(False, True, True, False,
                 "typed release with no id anywhere the consumer reads",
                 id="drop-id-tag+prefix"),
]


@pytest.mark.parametrize("producer", PRODUCERS)
@pytest.mark.parametrize("drop_type,drop_id_tag,drop_prefix,expect_paired,why",
                         _MUTATIONS)
def test_pairing_survives_single_drift_and_dies_when_every_leg_is_gone(
        producer, drop_type, drop_id_tag, drop_prefix, expect_paired, why):
    """Two-way proof (guard-1988). The rows that expect_paired=False are the
    discriminators — without them the positive assertions above could pass
    against a consumer that says "release" to anything."""
    post = producer()
    if drop_type:
        post["type"] = "status"
    if drop_id_tag:
        post["tags"] = [t for t in post["tags"] if t != GOAL_ID]
    if drop_prefix:
        post["text"] = "Session ending: " + post["text"]

    hits = _classify(post, extra_messages=[_PRIOR_CLAIM])
    paired = "release" in [h["kind"] for h in hits]
    assert paired is expect_paired, (
        "%s: expected paired=%s, got %s. hits=%r" % (why, expect_paired,
                                                     paired, hits))

    live, superseded = CONSUMER.supersede_released_claims(hits)
    if expect_paired:
        assert [h["id"] for h in superseded] == ["msg-claim"]
    else:
        assert superseded == [], "lien cleared with no readable release"


@pytest.mark.parametrize("producer", PRODUCERS)
def test_the_literal_pre_fix_step_8_9_post_pairs_nothing(producer):
    """The strongest discriminator, and not a synthetic one: this is byte-for-byte
    what Step 8.9 emitted before g-306-194 — one post per SESSION, `--type
    status`, no tags, and no goal id anywhere. The consumer drops it before it
    even reaches _released_ids (classify_board_mentions requires the id in the
    text or the tags), so the claim stays a permanent lien. `producer` is
    accepted so this runs once per producer and reads as part of the same
    matrix — the pre-fix shape is identical either way."""
    producer()  # exercise the extractor so a broken producer still fails here
    hits = _classify({
        "type": "status",
        "tags": [],
        "text": "Session ending: released all held claims",
        "author": AUTHOR,
    }, extra_messages=[_PRIOR_CLAIM])
    assert [h["kind"] for h in hits] == ["claim"], (
        "the pre-fix post produced something other than a lone live claim; the "
        "regression this goal fixed would not be caught. hits=%r" % (hits,))
    live, superseded = CONSUMER.supersede_released_claims(hits)
    assert superseded == []
    assert [h["kind"] for h in live] == ["claim"]
