"""A body row whose GOAL is finished is a phantom —  item 3.

THE GAP. `body_row_reaper.decide_row` asked exactly one question: is the BODY
alive? Its answer table reaps on ONE path — `CV_STALE` and no live claim.
`CV_ABSENT` returns `K_NO_CARRIER` and `CV_FRESH_CORRECT` returns `K_ALIVE`, so
a row survives forever whenever its Body is alive, or whenever its Body left no
carrier at all. Neither branch could reach the case that actually accumulates:
a Body that is perfectly healthy and has simply MOVED ON, leaving a row naming a
goal that finished hours ago.

MEASURED 2026-09-03 on the live shard: alpha carried 7 `in_flight_bodies` rows,
two of them naming goals `completed` on 2026-09-01 — g-358-42 (claimed 14:19)
and g-357-31 (claimed 20:14), i.e. 37h and 31h of phantom. Both are named in
g-306-412's own verification.

WHY IT MATTERS RATHER THAN BEING UNTIDY.
`goal-pickup-coordination-check._partner_in_flight` sorts body rows by
`claimed_at` DESCENDING and returns `candidates[0]`, with no age or staleness
guard anywhere in the function. A phantom therefore wins outright during any
fleet-quiet window, and it never expires on its own.

WHY NOT REUSE THE CLAIM MAP, which the reaper already receives.
`worker_stall._claims_from_lines` records `sid -> goal_id` with `setdefault`, so
a sid holding TWO non-terminal goals keeps only the first. "This row's goal is
absent from the claim map" is therefore NOT evidence the goal finished — it is
equally the second goal of a busy Body — and a DELETE cannot be built on that
absence (guard-2418). The predicate needs the store to SAY terminal, which is
what `read_terminal_goal_ids` returns.

THE TRI-STATE IS THE POINT. `goal_is_terminal` is True / False / None, and only
True reaps. None means the question was not answerable (no id set passed, no
`goal_id` on the row) and leaves every pre-existing branch untouched, so a
caller that cannot answer changes nothing.

PURE BY CONSTRUCTION: every case here drives the pure decision functions with no
daemon, no network and no shard — the property `decide_row`'s own docstring
claims ("every branch reachable with no daemon and no I/O") and the reason this
file is fast enough to run on every sweep-related change.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import body_row_reaper as reaper  # noqa: E402
import worker_stall  # noqa: E402

SELF_SID = "11111111-1111-1111-1111-111111111111"
OTHER_SID = "22222222-2222-2222-2222-222222222222"
DONE_GOAL = "g-358-42"
LIVE_GOAL = "g-306-412"


def _row(goal_id: str | None = DONE_GOAL) -> dict:
    row = {"claimed_at": "2026-09-01T14:19:03", "phase": "4",
           "title": "a claimed goal"}
    if goal_id is not None:
        row["goal_id"] = goal_id
    return row


# --- 1. THE FIX: a LIVE Body holding a FINISHED goal is still a phantom -----
def test_terminal_goal_is_reaped_even_when_the_body_is_alive():
    d = reaper.decide_row(
        sid=OTHER_SID, row=_row(), carrier_verdict=reaper.CV_FRESH_CORRECT,
        holds_live_claim=False, self_sid=SELF_SID, goal_is_terminal=True)
    assert d["verdict"] == reaper.R_REAP_TERMINAL_GOAL, (
        "a fresh carrier proves the BODY is alive, which says nothing about "
        f"whether the WORK is over; got {d}")
    assert reaper.is_reaping(d["verdict"]), (
        "the new verdict must be recognised by the is_reaping SSOT, or the "
        "integration silently keeps every row it decides to reap")


# --- 2. the live-shard shape: finished goal, no carrier at all --------------
def test_terminal_goal_is_reaped_when_the_carrier_is_absent():
    """`CV_ABSENT` returns K_NO_CARRIER, which is a KEEP — this is the exact
    reason the two 2026-09-01 rows survived 37h and 31h."""
    d = reaper.decide_row(
        sid=OTHER_SID, row=_row(), carrier_verdict=reaper.CV_ABSENT,
        holds_live_claim=False, self_sid=SELF_SID, goal_is_terminal=True)
    assert d["verdict"] == reaper.R_REAP_TERMINAL_GOAL, d


# --- 3. a LIVE goal is untouched -------------------------------------------
def test_non_terminal_goal_with_live_body_is_kept():
    d = reaper.decide_row(
        sid=OTHER_SID, row=_row(LIVE_GOAL),
        carrier_verdict=reaper.CV_FRESH_CORRECT,
        holds_live_claim=True, self_sid=SELF_SID, goal_is_terminal=False)
    assert d["verdict"] == reaper.K_ALIVE, d
    assert not reaper.is_reaping(d["verdict"]), d


# --- 4. UNMEASURED changes nothing -----------------------------------------
def test_unmeasured_terminal_state_leaves_every_prior_branch_intact():
    """The None arm is what makes this change additive.

    Each pair below is the same input with and without an answer to the new
    question; the verdicts must be identical, or the predicate has quietly
    taken over decisions it was not supposed to touch.
    """
    cases = [
        (reaper.CV_FRESH_CORRECT, False, reaper.K_ALIVE),
        (reaper.CV_ABSENT, False, reaper.K_NO_CARRIER),
        (reaper.CV_STALE, False, reaper.R_REAP),
        (reaper.CV_STALE, True, reaper.K_STALLED_WITH_CLAIM),
        (reaper.CV_UNREADABLE, False, reaper.K_UNREADABLE),
    ]
    for cv, holds, expected in cases:
        for terminal in (None, False):
            d = reaper.decide_row(
                sid=OTHER_SID, row=_row(), carrier_verdict=cv,
                holds_live_claim=holds, self_sid=SELF_SID,
                goal_is_terminal=terminal)
            assert d["verdict"] == expected, (
                f"carrier={cv} holds={holds} terminal={terminal!r} -> "
                f"{d['verdict']}, expected {expected}")


# --- 5. self-preservation is ORDERED FIRST, deliberately --------------------
def test_own_row_is_kept_even_when_its_goal_is_terminal():
    """Pins the ordering as a decision rather than an accident.

    A row of the RUNNING session naming a finished goal is a miss in the
    CLEAN-close path (`worker_close_in_flight_clear`), and that is where it
    belongs; reaping it here would mask that defect and weaken the
    belt-and-braces self-preservation the API-storm shape motivated.
    """
    d = reaper.decide_row(
        sid=SELF_SID, row=_row(), carrier_verdict=reaper.CV_ABSENT,
        holds_live_claim=False, self_sid=SELF_SID, goal_is_terminal=True)
    assert d["verdict"] == reaper.K_SELF_SID, d


# --- 6. a row with no goal_id cannot be judged -----------------------------
def test_row_without_goal_id_is_unmeasured_not_terminal():
    assert reaper._goal_terminal(_row(None), {DONE_GOAL}) is None
    assert reaper._goal_terminal(_row(DONE_GOAL), None) is None
    assert reaper._goal_terminal(_row(DONE_GOAL), {DONE_GOAL}) is True
    assert reaper._goal_terminal(_row(LIVE_GOAL), {DONE_GOAL}) is False
    assert reaper._goal_terminal("not-a-dict", {DONE_GOAL}) is None


# --- 7. decide() threads the set through, and reports the tri-state --------
def test_decide_threads_terminal_ids_and_emits_the_tristate():
    rows = {OTHER_SID: _row(), SELF_SID: _row(LIVE_GOAL)}
    verdicts = {OTHER_SID: (reaper.CV_FRESH_CORRECT, {}),
                SELF_SID: (reaper.CV_FRESH_CORRECT, {})}
    got = reaper.decide(rows, verdicts, {}, SELF_SID, {DONE_GOAL})
    by_sid = {d["sid"]: d for d in got["decisions"]}
    assert by_sid[OTHER_SID]["verdict"] == reaper.R_REAP_TERMINAL_GOAL
    assert by_sid[OTHER_SID]["goal_is_terminal"] is True
    assert by_sid[SELF_SID]["goal_is_terminal"] is False
    assert [c["sid"] for c in got["reapable"]] == [OTHER_SID]

    # ...and with no id set the SAME inputs reap nothing.
    none_run = reaper.decide(rows, verdicts, {}, SELF_SID)
    assert none_run["reapable"] == []
    assert all(d["goal_is_terminal"] is None for d in none_run["decisions"])


# --- 8. the parser: positive control beside the negative -------------------
def test_terminal_parser_separates_finished_from_live():
    """A parser written for this change converts absence into a confident value
    unless something proves it can see a POSITIVE (guard-2298), so the live goal
    is asserted in the same call as the finished one."""
    lines = [json.dumps({"id": "asp-358", "goals": [
        {"id": DONE_GOAL, "status": "completed"},
        {"id": LIVE_GOAL, "status": "pending"},
        {"id": "g-1-1", "status": "skipped"},
        {"id": "g-1-2", "status": "expired"},
        {"id": "g-1-3", "status": "blocked"},
        {"status": "completed"},              # no id -> not collectable
    ]}), "", "{ not json"]
    got = worker_stall._terminal_goal_ids_from_lines(lines)
    assert got == {DONE_GOAL, "g-1-1", "g-1-2"}, got
    assert LIVE_GOAL not in got, "a pending goal is not terminal"
    assert "g-1-3" not in got, (
        "`blocked` is NOT terminal -- it is resumable, and reaping its row "
        "would delete live contention")


# --- 9. the union reaches the AGENT queue too (the  lesson) -------
def test_union_finds_a_terminal_goal_that_only_the_agent_queue_carries(tmp_path):
    world = tmp_path / "world.jsonl"
    agent = tmp_path / "agent.jsonl"
    world.write_text(json.dumps({"id": "asp-1", "goals": [
        {"id": LIVE_GOAL, "status": "pending"}]}) + "\n", encoding="utf-8")
    agent.write_text(json.dumps({"id": "asp-2", "goals": [
        {"id": "g-9-9", "status": "completed"}]}) + "\n", encoding="utf-8")
    ids, prov = worker_stall.read_terminal_goal_ids(world, agent)
    assert ids == {"g-9-9"}, ids
    assert prov == "local-mirror", prov


# --- 10. provenance separates "read fine, nothing terminal" from "no read" --
def test_unreadable_store_reports_none_not_an_empty_answer(tmp_path):
    readable = tmp_path / "readable.jsonl"
    readable.write_text(json.dumps({"id": "asp-1", "goals": [
        {"id": LIVE_GOAL, "status": "pending"}]}) + "\n", encoding="utf-8")

    ids, prov = worker_stall.read_terminal_goal_ids(readable)
    assert (ids, prov) == (set(), "local-mirror"), (
        "an empty answer from a store that WAS read must not look like a "
        f"failed read; got {ids!r}/{prov!r}")

    ids, prov = worker_stall.read_terminal_goal_ids(tmp_path / "absent.jsonl")
    assert (ids, prov) == (set(), "none"), (ids, prov)

    # The union reports the WEAKEST half, so one good read cannot launder a
    # failed one into a confident "nothing is terminal".
    ids, prov = worker_stall.read_terminal_goal_ids(
        readable, tmp_path / "absent.jsonl")
    assert prov == "none", prov


# --- 11. the extraction did not change read_claims' contract ---------------
def test_extracted_reader_preserves_read_claims_provenance(tmp_path):
    """`_read_queue_lines` was lifted OUT of `read_claims` ( item 3).

    The lift is only safe if `read_claims` still separates its three outcomes,
    so they are asserted here rather than assumed from the refactor being small.
    """
    store = tmp_path / "q.jsonl"
    store.write_text(json.dumps({"id": "asp-1", "goals": [
        {"id": LIVE_GOAL, "status": "pending",
         "claimed_by_sid": OTHER_SID},
        {"id": DONE_GOAL, "status": "completed",
         "claimed_by_sid": SELF_SID},
    ]}) + "\n", encoding="utf-8")
    claims, prov = worker_stall.read_claims(store)
    assert claims == {OTHER_SID: LIVE_GOAL}, claims
    assert prov == "local-mirror", prov
    assert SELF_SID not in claims, "a terminal goal is not a live claim"

    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert worker_stall.read_claims(empty) == ({}, "local-mirror")
    assert worker_stall.read_claims(tmp_path / "gone.jsonl") == ({}, "none")
