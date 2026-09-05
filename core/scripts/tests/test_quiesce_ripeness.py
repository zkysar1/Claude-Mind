"""Tests for the quiesce-window ripeness evaluator ().

The cases that matter here are the ones where a WRONG implementation still
returns a plausible verdict: a stale manifest row summed into the batch, a
hold that cannot be told from a never-wired check, and the criterion-(b)
prefix trap where matching the marker the convention NAMES finds nothing
forever while the real population is non-empty.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quiesce_ripeness import (  # noqa: E402
    BATCH_FLOOR_MINUTES,
    STAMP_SENTINEL,
    apply_stamp,
    evaluate,
    is_quiesce_frozen_defer,
    parse_minutes,
    parse_rows,
    render_stamp,
)

HEADER = (
    "| # | Item | Why quiesce is REQUIRED | Shape | Who | Est. | Window-ready? |\n"
    "|---|---|---|---|---|---|---|\n"
)


def row(qid, item, est, ready, why="because", shape="1", who="agent"):
    return f"| {qid} | {item} | {why} | {shape} | {who} | {est} | {ready} |\n"


# --------------------------------------------------------------------------
# estimate parsing
# --------------------------------------------------------------------------

def test_parse_minutes_takes_low_end_of_en_dash_range():
    # The convention writes ranges with an EN DASH. The low end is deliberate:
    # criterion (a) is a floor, and crossing it on the optimistic end is how a
    # half-batch gets a window called.
    assert parse_minutes("~70–90 min") == 70
    assert parse_minutes("~20–30 min (echo's estimate)") == 20


def test_parse_minutes_handles_ascii_hyphen_and_plain():
    assert parse_minutes("~70-90 min") == 70
    assert parse_minutes("~45 min") == 45
    assert parse_minutes("~5 min + restart observation") == 5


def test_parse_minutes_none_when_unscoreable():
    assert parse_minutes("TBD") is None
    assert parse_minutes("") is None


# --------------------------------------------------------------------------
# row parsing
# --------------------------------------------------------------------------

def test_two_column_outcome_rows_are_not_manifest_rows():
    """Outcome tables are keyed by the SAME Q-ids as the manifest.

    They are separated by COLUMN COUNT, not by position in the file -- position
    moves every time a window is held, so a positional rule would rot silently.
    """
    md = HEADER + row("Q1", "real item", "~45 min", "**YES**")
    md += "\n| item | outcome |\n|---|---|\n| Q1 | done, 4/4 passed |\n"
    rows = parse_rows(md)
    assert [r["qid"] for r in rows] == ["Q1"]
    assert rows[0]["est_minutes"] == 45


def test_goal_id_extracted_from_item_cell():
    md = HEADER + row("Q1", "**`g-115-2050` — prune**", "~45 min", "**YES**")
    assert parse_rows(md)[0]["goal_id"] == "g-115-2050"


def test_row_without_goal_id_is_allowed():
    # Q2 in the live file is a bare pytest subset with no goal behind it.
    md = HEADER + row("Q2", "**`daemon_integration` pytest subset**", "~10 min", "**YES**")
    r = parse_rows(md)[0]
    assert r["goal_id"] is None and r["ready_claimed"] is True


def test_done_beats_yes_in_the_same_cell():
    """A tombstone cell can carry BOTH tokens, and DONE must win.

    The fixture must actually contain "YES" or this test is vacuous -- an
    earlier version used a DONE-only cell, which passes whatever the ordering
    is, and survived a mutation that removed the guard entirely. The both-token
    shape is the natural one: a row annotated in place when its work ran, so
    the original readiness claim is still sitting there next to the tombstone.
    """
    cell = "**YES** — superseded; ✅ DONE 2026-07-26T20:14, tombstone, do not re-run."
    assert "YES" in cell and "DONE" in cell, "fixture must carry both tokens"
    md = HEADER + row("Q4", "item", "~30 min", cell)
    r = parse_rows(md)[0]
    assert r["done"] is True and r["ready_claimed"] is False
    # ...and it must not reach the ready set or the batch total.
    res = evaluate(md, {})
    assert res["counts"]["tombstoned"] == 1 and res["counts"]["ready"] == 0


# --------------------------------------------------------------------------
# the stale-row problem
# --------------------------------------------------------------------------

def test_terminal_goal_is_reported_stale_not_counted():
    md = HEADER + row("Q5", "**`g-115-4742` — wsl shutdown**", "~30 min", "**YES**")
    res = evaluate(md, {"g-115-4742": {"status": "completed", "priority": "MEDIUM"}})
    assert res["counts"]["stale_row"] == 1
    assert res["counts"]["ready"] == 0
    assert res["total_ready_minutes"] == 0
    assert res["verdict"] == "HOLD"
    assert res["stale_row"][0]["qid"] == "Q5"


def test_stale_rows_alone_cannot_manufacture_ripeness():
    """The failure this evaluator exists to prevent: summing YES cells whose
    work already ran, and calling a five-terminal window for an empty batch."""
    md = HEADER
    for q in ("Q1", "Q2", "Q3"):
        md += row(q, f"**`g-100-{q[1:]}` — done work**", "~45 min", "**YES**")
    done = {f"g-100-{q[1:]}": {"status": "completed"} for q in ("Q1", "Q2", "Q3")}
    assert evaluate(md, done)["verdict"] == "HOLD"
    # The naive reading would have said GO on 135 minutes. It is now unreachable
    # from an empty map by a SECOND defense: the wholesale-lookup-failure guard
    # refuses to score at all, rather than scoring optimistically. (This assertion
    # read `== "GO"` until that guard landed — it was demonstrating the failure
    # the guard now prevents, so it is updated, not deleted.)
    assert evaluate(md, {})["verdict"] == "CANNOT-EVALUATE"


def test_wholesale_lookup_failure_refuses_to_score():
    """Regression pin for a defect found by fresh-eyes on this module's own
    first version (2026-08-13), reproducing its exact measured numbers.

    Individually, an absent goal is "unknown, not terminal". But if the manifest
    names goals and NOT ONE resolved, the lookup FAILED — and scoring it as
    "nothing is terminal" counts every stale row. Measured on the live manifest:
    5 ready/95 min/2 stale became 7 ready/115 min/0 stale, still printing GO,
    indistinguishable from a healthy run.
    """
    md = HEADER
    md += row("Q1", "**`g-1-1` — live**", "~45 min", "**YES**")
    md += row("Q5", "**`g-1-5` — already done**", "~10 min", "**YES**")
    md += row("Q7", "**`g-1-7` — already done**", "~10 min", "**YES**")

    healthy = {"g-1-1": {"status": "pending"},
               "g-1-5": {"status": "completed"},
               "g-1-7": {"status": "completed"}}
    h = evaluate(md, healthy)
    assert h["counts"]["ready"] == 1 and h["counts"]["stale_row"] == 2

    broken = evaluate(md, {})  # every lookup failed
    assert broken["verdict"] == "CANNOT-EVALUATE"
    # It must NOT report the optimistic 3-ready/65-min reading.
    assert broken["counts"]["ready"] == 0
    assert broken["total_ready_minutes"] == 0
    assert "failed rather than finding nothing" in broken["reason"]


def test_wholesale_guard_does_not_fire_when_manifest_names_no_goals():
    """A manifest of goal-less rows (Q2 is one) legitimately has an empty status
    map. The guard must key on 'named goals did not resolve', never on 'the map
    is empty' — otherwise it refuses every valid goal-less manifest."""
    md = HEADER + row("Q2", "**`daemon_integration` pytest subset**", "~45 min", "**YES**")
    res = evaluate(md, {})
    assert res["verdict"] == "GO" and res["counts"]["ready"] == 1


def test_partial_lookup_success_still_scores():
    """Only a TOTAL failure refuses. One resolved goal proves the lookup works,
    so the rest are genuinely unknown and keep the permissive treatment."""
    md = HEADER + row("Q1", "**`g-1-1`**", "~20 min", "**YES**")
    md += row("Q2", "**`g-9-9` — unreadable**", "~20 min", "**YES**")
    res = evaluate(md, {"g-1-1": {"status": "pending"}})
    assert res["verdict"] == "GO" and res["counts"]["ready"] == 2


def test_unknown_goal_is_not_treated_as_terminal():
    """An unreadable goal is unknown, not done. Dropping it would quietly
    shrink the batch, and a zero from a failed lookup reads exactly like a
    real zero (rb-245)."""
    # The map must be NON-empty (one other goal resolved), or the
    # wholesale-lookup-failure guard fires instead — that guard keys on "no
    # named goal resolved at all", which is a different condition from "this
    # one goal is unknown". Both defenses are wanted; this test isolates the
    # per-goal one.
    md = HEADER + row("Q1", "**`g-999-99` — unknown**", "~45 min", "**YES**")
    md += row("Q2", "**`g-1-1` — resolvable**", "~5 min", "**YES**")
    res = evaluate(md, {"g-1-1": {"status": "pending"}})
    assert res["verdict"] != "CANNOT-EVALUATE"
    assert res["counts"]["ready"] == 2 and res["counts"]["stale_row"] == 0


# --------------------------------------------------------------------------
# criterion (a)
# --------------------------------------------------------------------------

def test_criterion_a_fires_at_the_floor_not_above_it():
    md = HEADER + row("Q1", "item", f"~{BATCH_FLOOR_MINUTES} min", "**YES**")
    assert evaluate(md, {})["criterion_a"] is True


def test_criterion_a_holds_below_the_floor():
    md = HEADER + row("Q1", "item", f"~{BATCH_FLOOR_MINUTES - 1} min", "**YES**")
    res = evaluate(md, {})
    assert res["criterion_a"] is False and res["verdict"] == "HOLD"


def test_not_ready_rows_do_not_count_toward_the_floor():
    md = HEADER + row("Q1", "item", "~10 min", "**YES**")
    md += row("Q9", "item", "~90 min", "not yet — blocked on a measurement")
    res = evaluate(md, {})
    assert res["total_ready_minutes"] == 10 and res["verdict"] == "HOLD"


def test_unscoreable_estimate_is_reported_not_silently_zero():
    md = HEADER + row("Q1", "item", "TBD", "**YES**")
    res = evaluate(md, {})
    assert res["counts"]["unscoreable_estimate"] == 1
    assert res["unscoreable_estimate"][0]["qid"] == "Q1"
    # It still sits in the ready set -- it is ready, just uncounted.
    assert res["counts"]["ready"] == 1


# --------------------------------------------------------------------------
# criterion (b) -- and the prefix trap
# --------------------------------------------------------------------------

def test_criterion_b_high_priority_overrides_the_floor():
    """The convention: '(b) overrides (a): do not hoard a window while
    blocking work waits.'"""
    md = HEADER + row("Q1", "**`g-1-1` — small but HIGH**", "~5 min", "**YES**")
    res = evaluate(md, {"g-1-1": {"status": "pending", "priority": "HIGH"}})
    assert res["criterion_a"] is False
    assert res["criterion_b"] is True
    assert res["verdict"] == "GO"


def test_criterion_b_defer_leg_fires_with_an_empty_manifest():
    """A goal frozen waiting on a window is grounds to call one even when no
    manifest row is ready -- otherwise the frozen goal waits forever."""
    res = evaluate(HEADER, {}, ["g-115-2050"])
    assert res["verdict"] == "GO" and res["criterion_b"] is True
    assert "g-115-2050" in res["reason"]


def test_criterion_b_reason_names_what_matched_not_the_nominal_marker():
    """Regression pin for the measured trap. The live frozen goal carries
    `human_blocked: requires a fleet-quiesced window ...`, NOT the
    `precondition_unmet:fleet_quiesced_window` prefix the convention names, so
    a verdict citing that prefix cites evidence it does not have."""
    res = evaluate(HEADER, {}, ["g-115-2050"])
    assert "precondition_unmet:fleet_quiesced_window" not in res["reason"]
    assert "g-115-2050" in res["reason"]


def test_high_priority_on_a_stale_row_does_not_fire_criterion_b():
    """A completed HIGH row must not keep calling windows forever."""
    md = HEADER + row("Q1", "**`g-1-1` — done**", "~5 min", "**YES**")
    res = evaluate(md, {"g-1-1": {"status": "completed", "priority": "HIGH"}})
    assert res["criterion_b"] is False and res["verdict"] == "HOLD"


# --------------------------------------------------------------------------
# hold is a real verdict, not an absence
# --------------------------------------------------------------------------

def test_empty_manifest_holds_with_a_stated_reason():
    """The whole point of wiring this: a HOLD must be DISTINGUISHABLE from a
    check that never ran. Both used to produce silence."""
    res = evaluate(HEADER, {})
    assert res["verdict"] == "HOLD"
    assert res["counts"]["rows_parsed"] == 0
    assert "floor" in res["reason"]


def test_stamp_is_written_on_HOLD_not_only_on_GO():
    """The load-bearing case, and the whole point of the stamp.

    The convention's no-chatter design makes a never-wired evaluator and a
    standing hold produce byte-identical silence. A stamp that only appeared
    when ripe would reproduce the original defect exactly (guard-2352).
    """
    res = evaluate(HEADER, {})
    assert res["verdict"] == "HOLD"
    stamp = render_stamp(res, "2026-08-13T05:00:00")
    assert "HOLD" in stamp and "2026-08-13T05:00:00" in stamp
    assert "floor" in stamp  # the reason travels with it, not just the verdict


def test_stamp_replaces_the_line_and_does_not_accumulate():
    md = f"# doc\n\n{STAMP_SENTINEL}\n**Last evaluated: (never)**\n\ntail\n"
    once = apply_stamp(md, "**Last evaluated: A**")
    twice = apply_stamp(once, "**Last evaluated: B**")
    assert twice.count("Last evaluated") == 1
    assert "**Last evaluated: B**" in twice
    assert twice.count("\n") == md.count("\n")  # no line growth
    assert twice.endswith("tail\n")             # content after the stamp survives


def test_stamp_refuses_to_create_its_own_write_target():
    """Absent sentinel returns None rather than inserting one.

    A tool that manufactures its write target will stamp a renamed, moved, or
    half-migrated document without complaint.
    """
    assert apply_stamp("# doc\n\nno sentinel here\n", "**x**") is None


def test_stamp_survives_sentinel_as_last_line():
    md = f"# doc\n{STAMP_SENTINEL}\n"
    out = apply_stamp(md, "**Last evaluated: A**")
    assert out is not None and "**Last evaluated: A**" in out


def test_stamp_reports_stale_row_count_so_the_lag_is_visible_without_running_it():
    md = HEADER + row("Q5", "**`g-1-1` — done**", "~45 min", "**YES**")
    res = evaluate(md, {"g-1-1": {"status": "completed"}})
    assert "1 stale-row" in render_stamp(res, "2026-08-13T05:00:00")


def test_tombstoned_rows_are_counted_separately_from_not_ready():
    md = HEADER + row("Q3", "item", "~70–90 min", "✅ DONE 2026-07-26")
    md += row("Q9", "item", "~30 min", "no — needs a measurement first")
    c = evaluate(md, {})["counts"]
    assert c["tombstoned"] == 1 and c["not_ready"] == 1 and c["ready"] == 0


# --- criterion (b) defer predicate (, 2026-09-05) -------------------
# The gap these pin: the detector matched the bare substring "quiesce" while the
# master goal's own registration prefix says "quiet-window member of <master>".
# Two goals with the IDENTICAL registered prefix landed on opposite sides,
# because one happened to also write "quiesced-fleet-window" in its prose.


def test_the_registered_membership_prefix_is_matched_without_the_word_quiesce():
    # Verbatim shape of a live member's defer. It contains NO "quiesce".
    defer = (
        "human_blocked: quiet-window member of g-000-000 — NEXT WINDOW "
        "2026-09-05..07. WINDOW-READY: YES for the delete (~45 min)."
    )
    assert "quiesce" not in defer.lower(), "fixture must not smuggle in the old token"
    assert is_quiesce_frozen_defer(defer)


def test_CONTROL_the_old_bare_token_still_matches():
    # The widening is ADDITIVE. A defer that only ever said "quiesce" -- the
    # prose form the 2026-08-13 comment measured -- must not regress.
    assert is_quiesce_frozen_defer(
        "human_blocked: requires a fleet-quiesced window only the user can create"
    )


def test_the_SPACED_form_is_NOT_matched_because_it_names_a_different_window():
    # A live goal uses "quiet window" for a HEAD-quiet window (no git merges for
    # N minutes so a long suite run is not voided). Admitting it would report a
    # goal as fleet-quiesce-frozen that its author never registered.
    assert not is_quiesce_frozen_defer(
        "precondition_unmet: ~187 min needed vs a 116 min longest HEAD-quiet "
        "quiet window today, with 7 Bodies active and merging at every turn-end."
    )


def test_an_unrelated_defer_and_an_absent_one_are_both_false():
    assert not is_quiesce_frozen_defer("precondition_unmet: waiting on PR #1 to merge")
    assert not is_quiesce_frozen_defer("")
    assert not is_quiesce_frozen_defer(None)


def test_the_caller_consumes_THIS_predicate_and_does_not_re_inline_one():
    # The drift this whole fix is about: the shell had its own copy of the
    # substring test, so the module could widen and the live scan stay narrow.
    # Pin the wiring, not just the function -- guard-1943 (pinning the writer
    # says nothing about the wiring).
    src = (Path(__file__).resolve().parents[1] / "quiesce-ripeness-check.sh").read_text(
        encoding="utf-8"
    )
    assert "is_quiesce_frozen_defer" in src
    assert "'quiesce' in (g.get('defer_reason')" not in src
