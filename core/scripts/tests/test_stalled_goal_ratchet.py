"""Tests for stalled-goal-ratchet — the class-agnostic non-executability clock.

The module exists because every OTHER clock in the fleet ages on a field that
re-blocking re-stamps, so a goal re-deferred every few days is permanently
young to every sweep. Two tests are load-bearing and the rest support them:

  * `test_rewriting_the_defer_reason_cannot_reset_the_clock` — the AGE cannot
    be laundered (MAX over clocks, not most-recent).
  * `test_relabelling_as_human_blocked_moves_debt_it_does_not_delete_it` — the
    BUCKET cannot be laundered either (second ratchet key).

If either ever passes trivially, this module has become another age check and
should be deleted rather than kept.

Fixtures use REAL shapes measured off the live corpus on 2026-09-04 (129
deferred goals; 20 status=blocked of which 4 carried a null `blocked_since`
with `blocked_by=[]`), not invented ones — the null-clock bucket in particular
was found by measurement and would not have been guessed.

NOTE ON THE SECOND ARGUMENT. It is `blocked_ids` — the set the SELECTOR calls
blocked — not the set it offered. The first draft of this module used the
complement of the scored set and was wrong by 66 goals; the test
`test_a_merely_unselected_healthy_goal_is_not_counted` pins that specific
error, because the naive predicate looks correct in every other test here.

The pure core takes `now` and `blocked_ids` as arguments, so every test here
pins time exactly and touches no store, no daemon and no baseline file.
"""

import datetime as dt
import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "_stalled_goal_ratchet", SCRIPTS / "stalled-goal-ratchet.py")
sgr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sgr)

NOW = dt.datetime(2026, 9, 4, 13, 0, 0)


def _iso(days_ago):
    return (NOW - dt.timedelta(days=days_ago)).isoformat(timespec="seconds")


def _goal(gid, **kw):
    g = {"id": gid, "status": "pending", "title": "t", "_source": "world"}
    g.update(kw)
    return g


def _ids(*goals):
    """The blocked-id set for these goals — the common case in these tests."""
    return {g.get("id") or g.get("goal_id") for g in goals}


# ══════════════════════════ THE ANTI-LAUNDERING PROPERTIES ════════════════

def test_rewriting_the_defer_reason_cannot_reset_the_clock():
    """THE FIRST REASON THIS MODULE EXISTS.

    `cmd_update_goal` re-stamps `defer_reason_set_at` on every new
    defer_reason, so a goal re-deferred every few days reads as fresh to every
    existing sweep — with nobody lying, since each individual defer really is
    new. `blocked_since` is written once and not re-stamped.

    Taking the MAX over clocks means the durable one still wins, so the
    measured stall does not shorten when the reason is rewritten. If this test
    fails, re-defers buy unlimited time and the metric is worthless.
    """
    laundered = _goal("g-1",
                      blocked_since=_iso(30),        # durable, set once
                      defer_reason_set_at=_iso(1),   # re-stamped an hour ago
                      defer_reason="precondition_unmet: freshly reworded")
    age, basis = sgr.stall_age_days(laundered, NOW)
    assert round(age) == 30, f"clock was reset by the rewrite: {age}d via {basis}"
    assert basis == "blocked_since"
    assert sgr.classify(laundered, _ids(laundered), NOW) == "stalled"


def test_relabelling_as_human_blocked_moves_debt_it_does_not_delete_it():
    """THE SECOND REASON THIS MODULE EXISTS.

    `human_blocked:` is excluded from `stalled_goals` for a good reason (no
    agent can clear it, so counting it would create an unratchetable floor).
    That exclusion is itself a laundering vector: rewrite the prefix and the
    goal leaves the drift count for free.

    The second baseline key closes it. Relabelling must MOVE the debt — one
    counter down, the other up — never reduce the total.
    """
    before = _goal("g-launder", blocked_since=_iso(30),
                   defer_reason="precondition_unmet: waiting on something")
    c1 = sgr.census([before], _ids(before), NOW)
    assert (c1["drift_total"], c1["human_blocked_total"]) == (1, 0)

    after = dict(before, defer_reason="human_blocked: owner will do it")
    c2 = sgr.census([after], _ids(after), NOW)
    assert (c2["drift_total"], c2["human_blocked_total"]) == (0, 1)

    def total(c):
        return c["drift_total"] + c["human_blocked_total"]

    assert total(c1) == total(c2) == 1, "relabelling erased debt"


def test_a_genuinely_new_block_is_young_even_with_an_old_sibling_field_absent():
    """The mirror case — the property must not make everything look stalled.
    A goal blocked for the first time yesterday is YOUNG, and stays young."""
    fresh = _goal("g-2", defer_reason_set_at=_iso(1),
                  defer_reason="precondition_unmet: waiting on a build")
    assert sgr.classify(fresh, _ids(fresh), NOW) == "young"


def test_max_not_most_recent_regardless_of_field_order():
    """Order-independence: the oldest clock wins whichever field carries it.
    A future edit that reorders STALL_CLOCK_FIELDS must not change the answer.
    """
    a = _goal("g-3", blocked_since=_iso(2), defer_reason_set_at=_iso(20))
    b = _goal("g-4", blocked_since=_iso(20), defer_reason_set_at=_iso(2))
    assert round(sgr.stall_age_days(a, NOW)[0]) == 20
    assert round(sgr.stall_age_days(b, NOW)[0]) == 20


# ═══════════════════════════ THE POPULATION ═══════════════════════════════

def test_a_merely_unselected_healthy_goal_is_not_counted():
    """PINS THE 66-GOAL ERROR. This module's first predicate was "non-terminal
    and absent from the scored set", which put 423 goals in the drift bucket —
    66 of them healthy recurring goals like "Check agent email inbox", simply
    not due yet. The scored set is ONE agent's vantage and legitimately omits
    claimed, cross-routed and not-yet-due work.

    The selector's own `blocked[]` is the population owner, so a goal it does
    NOT call blocked is executable no matter how old its stamps are. Without
    this test the naive predicate passes every other test in this file.
    """
    recurring = _goal("g-115-01", title="Check agent email inbox",
                      recurring=True,
                      blocked_since=_iso(60),        # ancient leftover stamp
                      defer_reason_set_at=_iso(60))
    assert sgr.classify(recurring, set(), NOW) == "executable"
    c = sgr.census([recurring], set(), NOW)
    assert c["drift_total"] == 0 and c["breakdown"]["executable"] == 1


def test_blocked_goals_are_counted_however_the_id_field_is_spelled():
    """Rows arrive keyed `id` from the store and `goal_id` from query
    projections. Missing the alias makes `gid` None, which is never in
    blocked_ids — so a genuinely stalled goal would silently read `executable`
    and UNDER-count. That is the dangerous direction for this metric.
    """
    g = {"goal_id": "g-11", "status": "pending", "blocked_since": _iso(30)}
    assert sgr.classify(g, {"g-11"}, NOW) == "stalled"


def test_terminal_wins_over_blocked_membership():
    """A closed goal is done, not stuck — even if the selector's blocked list
    still names it (the two lists are read at slightly different moments)."""
    for st in ("completed", "skipped", "expired", "archived", "superseded"):
        g = _goal("g-9", status=st, blocked_since=_iso(99))
        assert sgr.classify(g, {"g-9"}, NOW) == "terminal", st


# ═══════════════════════════════ no_clock ═════════════════════════════════

def test_status_blocked_with_null_clock_is_no_clock_at_any_age():
    """MEASURED SHAPE: 4 of 20 status=blocked goals carried blocked_since=None
    with blocked_by=[] (g-250-124, g-115-3578, g-306-406, g-350-10).

    No age-based escape anywhere can fire on these — goal-selector's own
    comment on the dependency path calls the null case "fail-closed".
    """
    g = _goal("g-250-124", status="blocked", blocked_since=None, blocked_by=[],
              defer_reason="precondition_unmet: something")
    assert sgr.classify(g, _ids(g), NOW) == "no_clock"


def test_no_clock_is_reported_but_not_counted():
    """357 of 499 blocked goals have no usable timestamp, because a goal
    blocked by an unmet `blocked_by` while `status` stays `pending` never
    receives `blocked_since`. Their stall duration is UNKNOWN, not long —
    folding them into a measured 22 would swamp the signal 16:1.

    So they ride in `breakdown` as a COVERAGE figure and are excluded from the
    ratchet. Visible, not counted.
    """
    g = _goal("g-5", status="blocked")
    c = sgr.census([g], _ids(g), NOW)
    assert c["breakdown"]["no_clock"] == 1, "the blind spot must stay visible"
    assert c["drift_total"] == 0, "an unmeasurable goal must not inflate drift"


def test_unparseable_timestamp_is_no_clock_not_a_crash():
    """A corrupt stamp must degrade to the honest bucket, never raise — this
    runs over a live store where a hand-edited field is always possible."""
    for bad in ("not-a-date", "", None, 12345, "2026-13-45T99:99:99"):
        g = _goal("g-6", blocked_since=bad)
        assert sgr.classify(g, {"g-6"}, NOW) == "no_clock"


# ══════════════════════════ the human_blocked key ═════════════════════════

def test_human_blocked_is_split_out_not_swallowed():
    """It must leave `stalled_goals` (no agent can clear it, so counting it
    there creates a floor the fleet cannot ratchet down) AND land in its own
    counter (or the exclusion becomes the laundering vector)."""
    g = _goal("g-7", defer_reason="human_blocked: waiting on the owner",
              defer_reason_set_at=_iso(40))
    assert sgr.classify(g, _ids(g), NOW) == "human_blocked"
    c = sgr.census([g], _ids(g), NOW)
    assert c["drift_total"] == 0, "human_blocked must not inflate the ratchet"
    assert c["human_blocked_total"] == 1, "...but must ratchet on its own key"
    assert c["breakdown"]["human_blocked"] == 1


def test_human_blocked_prefix_is_case_and_whitespace_tolerant():
    """LLM-authored defers drift casing and leading space across rewrites —
    the same reason defer_classifier matches its prefixes case-insensitively."""
    for text in ("human_blocked: x", "HUMAN_BLOCKED: x", "  Human_Blocked: x"):
        g = _goal("g-8", defer_reason=text, blocked_since=_iso(40))
        assert sgr.classify(g, {"g-8"}, NOW) == "human_blocked", text


def test_human_blocked_only_applies_to_blocked_goals():
    """An executable goal carrying a stale human_blocked: string is not the
    operator's queue depth — it is a goal the selector is offering."""
    g = _goal("g-hb-live", defer_reason="human_blocked: stale text")
    assert sgr.classify(g, set(), NOW) == "executable"
    assert sgr.census([g], set(), NOW)["human_blocked_total"] == 0


# ══════════════════════════════ threshold ═════════════════════════════════

def test_threshold_boundary_is_strictly_greater_than():
    below = _goal("a", blocked_since=_iso(13.9))
    above = _goal("b", blocked_since=_iso(14.1))
    assert sgr.classify(below, {"a", "b"}, NOW, 14.0) == "young"
    assert sgr.classify(above, {"a", "b"}, NOW, 14.0) == "stalled"


def test_threshold_is_a_parameter_not_a_constant():
    g = _goal("c", blocked_since=_iso(10))
    assert sgr.classify(g, {"c"}, NOW, threshold_days=7.0) == "stalled"
    assert sgr.classify(g, {"c"}, NOW, threshold_days=30.0) == "young"


# ═════════════════ the audit-baselines admission property ═════════════════

def test_drift_total_monotonically_improves_when_a_goal_is_fixed():
    """`audit-baselines.md` admits a metric ONLY if it is a non-negative
    integer count that monotonically improves as items are fixed. This asserts
    that property directly, over the three ways a stall actually ends.
    """
    goals = [_goal("g-a", blocked_since=_iso(30)),
             _goal("g-b", blocked_since=_iso(30)),
             _goal("g-c", blocked_since=_iso(30))]
    blocked = {"g-a", "g-b", "g-c"}
    assert sgr.census(goals, blocked, NOW)["drift_total"] == 3

    # (1) unblocked -> the selector stops calling it blocked
    assert sgr.census(goals, blocked - {"g-a"}, NOW)["drift_total"] == 2
    # (2) closed
    goals[1]["status"] = "completed"
    assert sgr.census(goals, blocked - {"g-a"}, NOW)["drift_total"] == 1
    # (3) re-scoped, so the block is genuinely new
    goals[2]["blocked_since"] = _iso(1)
    assert sgr.census(goals, blocked - {"g-a"}, NOW)["drift_total"] == 0


def test_census_counts_are_a_partition_of_the_population():
    """Every goal lands in exactly one bucket — no double counting, no drops.
    A bucket that silently swallows rows would make drift_total unfalsifiable.
    """
    goals = [
        _goal("p1", blocked_since=_iso(30)),
        _goal("p2", status="completed"),
        _goal("p3", defer_reason="human_blocked: x"),
        _goal("p4", status="blocked"),
        _goal("p5", blocked_since=_iso(1)),
        _goal("p6", blocked_since=_iso(30)),
    ]
    c = sgr.census(goals, {"p1", "p2", "p3", "p4", "p5"}, NOW)
    assert sum(c["breakdown"].values()) == len(goals) == c["scanned"]


def test_non_dict_rows_are_skipped_without_crashing():
    c = sgr.census([_goal("ok", blocked_since=_iso(30)), None, "junk", 42],
                   {"ok"}, NOW)
    assert c["drift_total"] == 1 and c["scanned"] == 1


# ════════════════════════════ reporting shape ═════════════════════════════

def test_rows_are_ordered_worst_first():
    """A reader triages from the top; oldest must lead."""
    goals = [_goal("young-ish", blocked_since=_iso(15)),
             _goal("ancient", blocked_since=_iso(40)),
             _goal("middle", blocked_since=_iso(20))]
    rows = sgr.census(goals, _ids(*goals), NOW)["rows"]["stalled"]
    assert [r["goal_id"] for r in rows] == ["ancient", "middle", "young-ish"]


def test_basis_is_reported_so_a_reader_can_tell_the_clocks_apart():
    """`basis` is what lets a reader distinguish a re-stamped defer from a
    durable block without re-deriving the rule."""
    g = _goal("g-b1", blocked_since=_iso(30), defer_reason_set_at=_iso(2))
    row = sgr.census([g], _ids(g), NOW)["rows"]["stalled"][0]
    assert row["basis"] == "blocked_since" and row["age_days"] == 30.0


def test_real_corpus_mix_reproduces_the_measured_shape():
    """End-to-end over the shapes actually measured on 2026-09-04."""
    goals = [
        # the never-self-clearing classes, all past threshold
        _goal("g-335-934", blocked_since=_iso(15.9), handoff_to="foxtrot",
              defer_reason="precondition_unmet: native-windows-dep"),
        _goal("g-250-306", blocked_since=_iso(14.2), blocked_by=["g-350-108"]),
        _goal("g-315-446", blocked_since=_iso(21.3),
              defer_reason="precondition_unmet: re-run once the wall breaks"),
        # null-clock blocked goals
        _goal("g-250-124", status="blocked", blocked_by=[],
              defer_reason="precondition_unmet: x"),
        _goal("g-115-3578", status="blocked", blocked_by=[]),
        # operator-gated: its own key
        _goal("g-350-108", defer_reason="human_blocked: owner will do it later",
              defer_reason_set_at=_iso(19.9)),
        # healthy median goal, blocked but young
        _goal("g-fresh", defer_reason_set_at=_iso(3.6),
              defer_reason="precondition_unmet: accumulating samples"),
        # and one the selector still offers
        _goal("g-live", blocked_since=_iso(30)),
    ]
    blocked = _ids(*goals) - {"g-live"}
    c = sgr.census(goals, blocked, NOW)
    assert c["breakdown"] == {"terminal": 0, "executable": 1, "human_blocked": 1,
                              "no_clock": 2, "stalled": 3, "young": 1}
    assert c["drift_total"] == 3            # stalled only
    assert c["human_blocked_total"] == 1    # its own ratchet
