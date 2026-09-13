"""Branch proof for GS-1's revert-ownership decision ().

Drives `decide()` directly — it is pure, so every branch is reachable without a store, a
stop sequence, or a second Body.

The invariant under test is ONE-DIRECTIONAL and that asymmetry is the whole design: a goal
is revertible ONLY on POSITIVE evidence that this session owns it (`claimed_by_sid` equal
to our SID). Every other shape skips. So the tests come in two families:

  * a small number proving the one shape that DOES revert (without them the module could
    satisfy every safety test by reverting nothing, forever);
  * a larger number proving each not-ours shape is left alone — a missing key, a null key,
    a partner Body's SID, an unknown own SID, an unreadable payload.

The headline regression (this goal's outcome 3) is `test_stripped_projection_reverts_nothing`:
feed a projection with the ownership keys stripped and assert ZERO reverts. Before this
module that path fell through a three-arm cascade to an `in_flight`-only test and reverted
everything it could not identify.

`test_partner_body_same_agent_is_not_ours` is the one that pins the actual historical
defect: the old second arm compared `claimed_by`, which every Body of one agent writes
identically, so a sibling's goal passed an `alpha == alpha` test and was reverted
mid-execution (guard-1460).
"""

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from gs1_ownership import decide  # noqa: E402

MINE = "7a6a353a-15f4-45c3-af87-a3794390093f"
PARTNER = "7593d753-2642-4d9d-b6c0-1684594da2f3"


def _row(goal_id, **extra):
    """An in-progress projection row. Claim keys are ABSENT unless named, which is the
    healthy unclaimed shape — the canonical writers POP the claim triple (guard-4920)."""
    row = {"id": goal_id, "status": "in-progress", "title": f"t {goal_id}", "source": "world"}
    row.update(extra)
    return row


# --- the shape that DOES revert (non-vacuity: without these, "revert nothing" passes) ---


def test_own_claim_is_reverted():
    out = decide([_row("g-1", claimed_by_sid=MINE, claimed_by="alpha")], MINE)
    assert out["revert"] == ["g-1"]
    assert out["counts"]["revert"] == 1
    assert out["degraded"] is False


def test_own_claim_reverted_alongside_a_partners_untouched():
    goals = [
        _row("g-mine", claimed_by_sid=MINE, claimed_by="alpha"),
        _row("g-theirs", claimed_by_sid=PARTNER, claimed_by="alpha"),
    ]
    out = decide(goals, MINE)
    assert out["revert"] == ["g-mine"], "only the goal this session owns may revert"
    assert [s["goal_id"] for s in out["skip"]] == ["g-theirs"]


def test_goal_id_key_is_accepted_as_well_as_id():
    """The query surfaces `id`; sibling tools emit `goal_id`. Reading only one would make
    every row anonymous and, before the rewrite, pass an empty goal-id into a status
    mutation."""
    row = {"goal_id": "g-alt", "status": "in-progress", "claimed_by_sid": MINE}
    assert decide([row], MINE)["revert"] == ["g-alt"]


# --- outcome 3: the stripped projection reverts NOTHING ---


def test_stripped_projection_reverts_nothing():
    """THE REGRESSION THIS GOAL WAS FILED ON. A projection with the ownership keys removed
    must revert zero goals, not fall through to a weaker arm."""
    goals = [_row(f"g-{i}") for i in range(240)]
    out = decide(goals, MINE)
    assert out["revert"] == [], "a projection without ownership keys must revert NOTHING"
    assert out["counts"]["skip"] == 240
    assert out["counts"]["key_absent"] == 240


def test_stripped_projection_is_reported_as_degraded():
    """Reverting nothing is the safety half; SAYING SO is the other. A silent zero is
    indistinguishable from a healthy stop with no orphans."""
    out = decide([_row("g-1"), _row("g-2")], MINE)
    assert out["degraded"] is True
    assert "--full" in out["degraded_reason"]


def test_in_flight_is_never_consulted():
    """The old third arm reverted anything absent from a partner's in_flight, which holds
    at most ONE goal per agent — it shielded one goal and exposed the rest."""
    goals = [_row("g-1", in_flight=None), _row("g-2", in_flight="g-other")]
    assert decide(goals, MINE)["revert"] == []


# --- each not-ours shape is left alone ---


def test_partner_body_same_agent_is_not_ours():
    """THE HISTORICAL DEFECT. Same agent name, different Body: the old `claimed_by` arm
    read `alpha == alpha` and reverted a live sibling's work (guard-1460)."""
    out = decide([_row("g-1", claimed_by_sid=PARTNER, claimed_by="alpha")], MINE)
    assert out["revert"] == []
    assert "another Body" in out["skip"][0]["reason"]


def test_agent_name_alone_never_authorises_a_revert():
    """`claimed_by` present, `claimed_by_sid` absent: the agent name is not an ownership
    signal under the Mind/Body split, so this is NOT positive evidence."""
    out = decide([_row("g-1", claimed_by="alpha")], MINE)
    assert out["revert"] == []


def test_null_filled_claim_is_skipped_and_named():
    """guard-4920's damage shape: a `--field null` clear null-fills instead of popping.
    Still no positive evidence, and worth naming because nothing else can undo it."""
    out = decide([_row("g-1", claimed_by_sid=None, claimed_by="alpha")], MINE)
    assert out["revert"] == []
    assert out["counts"]["key_null"] == 1
    assert "guard-4920" in out["skip"][0]["reason"]


def test_null_key_is_not_counted_as_absent():
    """Present-with-null and absent are different defects; collapsing them would hide the
    one a human must repair."""
    out = decide([_row("g-1", claimed_by_sid=None)], MINE)
    assert out["counts"]["key_absent"] == 0
    assert out["counts"]["key_null"] == 1
    assert out["degraded"] is False, "a null key is damage, not a degraded projection"


def test_unknown_own_sid_reverts_nothing():
    """Without our own SID no row can be proven ours, so the honest answer is zero reverts
    — never a fallback to the agent name."""
    out = decide([_row("g-1", claimed_by_sid=MINE, claimed_by="alpha")], "")
    assert out["revert"] == []
    assert out["degraded"] is True


def test_unreadable_sid_value_is_skipped():
    out = decide([_row("g-1", claimed_by_sid=12345)], MINE)
    assert out["revert"] == []
    assert "unreadable" in out["skip"][0]["reason"]


def test_non_list_payload_is_degraded_not_empty():
    """An unreadable payload must not report a confident 'nothing to do'."""
    for payload in (None, {}, "[]", 7):
        out = decide(payload, MINE)
        assert out["revert"] == []
        assert out["degraded"] is True, f"{payload!r} must not read as an empty batch"


def test_non_dict_row_is_skipped_without_aborting_the_batch():
    """One bad row must not cost the good ones their decision."""
    out = decide(["nonsense", _row("g-ok", claimed_by_sid=MINE)], MINE)
    assert out["revert"] == ["g-ok"]
    assert len(out["skip"]) == 1


# --- the empty and live-shaped cases ---


def test_empty_batch_is_not_degraded():
    """No orphans is the NORMAL stop. Flagging it would make the signal unreadable."""
    out = decide([], MINE)
    assert out == {
        "revert": [],
        "skip": [],
        "sources": {},
        "degraded": False,
        "degraded_reason": "",
        "counts": {"total": 0, "revert": 0, "skip": 0, "key_absent": 0, "key_null": 0},
    }


def test_source_is_carried_for_each_revertible_goal():
    """`aspirations-update-goal.sh` needs --source. Carrying it here keeps the caller from
    re-joining two structures by hand — and defaults to the world queue rather than
    emitting a revert the caller cannot address."""
    goals = [
        _row("g-w", claimed_by_sid=MINE),
        dict(_row("g-a", claimed_by_sid=MINE), source="agent"),
        {"id": "g-none", "status": "in-progress", "claimed_by_sid": MINE},
    ]
    out = decide(goals, MINE)
    assert out["revert"] == ["g-w", "g-a", "g-none"]
    assert out["sources"] == {"g-w": "world", "g-a": "agent", "g-none": "world"}


def test_sources_names_only_revertible_goals():
    """A skipped goal must not appear in sources — it is not ours to address."""
    out = decide([_row("g-theirs", claimed_by_sid=PARTNER)], MINE)
    assert out["sources"] == {}


def test_live_measured_shape_2026_09_12():
    """The shape actually measured on cc-08 while fixing this (2026-09-12): in-progress
    rows DO carry the key. The goal was filed believing they do not, because every census
    behind it sampled PENDING rows, which are unclaimed by definition and so omit the key
    correctly. Pinning the real shape stops that confound being re-derived."""
    goals = [_row("g-326-897", claimed_by_sid="faec5e55-9532-41cb-a2c4-43e9120532b2", claimed_by="alpha")]
    out = decide(goals, MINE)
    assert out["revert"] == [], "a live partner Body's in-progress goal is not ours"
    assert out["degraded"] is False, "keys present — this projection is healthy"
