"""Pin the sq-018 lane-depth gate invariants in aspirations-spark/SKILL.md.

WHY THIS FILE EXISTS
--------------------
g-115-3936 (commits 4791b0dbf, 68e51d4a6) shipped six behavioural fixes to the
sq-018 handler's lane-depth gate. Every one was proven by hand against the live
asp-115 queue. None was pinned, so any later edit to that SKILL.md could revert
them silently.

That is not a hypothetical. The four original defects survived SIX drain rounds
across four agents precisely because nothing in the suite could fail on them.
Same shape as g-318-85: a gate improvement disarmed its own downstream consumer
and the suite could not notice.

ROUTING: sq-018 step 2.1's Q2 sent this here rather than to /verify-learning.
Measured 2026-07-31 — zero test files referenced the lane-depth gate, but eight
referenced aspirations-spark, and test_pending_phase_6_spark_sentinel.py already
asserts on SKILL.md body text. So core/scripts/tests/ owns SKILL.md-text
invariants in this family. A grep-based check would also be strictly weaker: the
load-bearing invariant (c) is a NEGATIVE assertion, which a grep cannot express.

MUTATION SENSITIVITY IS THE POINT, NOT PRESENCE
-----------------------------------------------
A test that passes on both the fixed and the broken text is worthless here — it
is exactly the failure mode of the six silent rounds. So every predicate below
is a pure function tested TWICE: once against the live SKILL.md (must hold) and
once against `PRE_FIX_REGION`, an inline fixture carrying the defective forms
(must be rejected). If someone weakens a predicate into a tautology, its
rejection test fails. The fixture is inline rather than read from git history so
the guarantee survives a shallow clone.

Source: g-115-3936 spark, sq-018 step 2.1 routing. Related: g-115-3790 (defect
(b), closed by the same commit), rb-5977, guard-1802.
"""

from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent          # core/scripts/tests
CORE_SCRIPTS = TESTS_DIR.parent                      # core/scripts
PROJECT_ROOT = CORE_SCRIPTS.parent.parent            # core/scripts -> core -> repo root
SPARK_SKILL_MD = PROJECT_ROOT / ".claude" / "skills" / "aspirations-spark" / "SKILL.md"

ROUTE_MARKER = "2.1. ROUTE BEFORE YOU FILE"
GATE_MARKER = "2.5. LANE-DEPTH GATE"
FILE_STEP_MARKER = "3. FILE the check as a Maintain-style goal"


# --------------------------------------------------------------------------
# Region extraction
# --------------------------------------------------------------------------


def _skill_body() -> str:
    assert SPARK_SKILL_MD.exists(), f"aspirations-spark/SKILL.md not found at {SPARK_SKILL_MD}"
    return SPARK_SKILL_MD.read_text(encoding="utf-8")


def _slice(body: str, start: str, end: str) -> str:
    i = body.find(start)
    j = body.find(end, i + 1) if i >= 0 else -1
    assert i >= 0, f"marker not found in SKILL.md: {start!r}"
    assert j > i, f"end marker {end!r} not found after {start!r}"
    return body[i:j]


def _gate_region(body: str) -> str:
    """The step-2.5 lane-depth gate body."""
    return _slice(body, GATE_MARKER, FILE_STEP_MARKER)


def _route_region(body: str) -> str:
    """The step-2.1 routing body."""
    return _slice(body, ROUTE_MARKER, GATE_MARKER)


# --------------------------------------------------------------------------
# Pure predicates — each returns True when the invariant HOLDS
# --------------------------------------------------------------------------


def counter_is_text_based(region: str) -> bool:
    """(a) The lane count matches on CONTENT, not an origin_signal proxy.

    The population that actually seals the lane is every open goal the
    duplication gate blocks against, i.e. anything citing verify-learning.
    Measured divergence at the time of the fix: narrow 9 vs wide 45.
    """
    cites_is_text = "'verify-learning' in ((g.get('title')" in region
    lane_uses_cites = "lane=[g for g in open_ if cites(g)]" in region
    lane_not_signal_based = (
        "lane=[g for g in open_ if (g.get('origin_signal')" not in region
    )
    return cites_is_text and lane_uses_cites and lane_not_signal_based


def drain_probe_has_recency_window(region: str) -> bool:
    """(b) A drain that COMPLETED recently is visible to the probe.

    An open-only probe cannot distinguish "drain ran and closed" from "nobody
    ever touched this lane" — success erases its own evidence, so the better
    the drain works the more confidently the gate re-prescribes it (rb-5977).
    """
    emits_recent = "DRAIN_RECENT" in region
    scans_completed = "status')=='completed'" in region
    has_time_cut = "timedelta(hours=" in region
    scans_all_goals = "for g in goals if isdrain(g)" in region
    return emits_recent and scans_completed and has_time_cut and scans_all_goals


def origin_signal_is_datestamped(region: str) -> bool:
    """(c) The drain origin_signal is DATE-templated, never depth-templated.

    LANE_DEPTH is non-monotonic — it drops on each drain and re-climbs — so a
    depth-templated signal regenerates identically at every repeat depth and
    origin_signal_completed refuses it forever. The negative half of this
    assertion is the load-bearing one.
    """
    datestamped = "maintain:drain-verify-learning-check-lane-<YYYYMMDD>" in region
    not_depth_templated = "check-lane-<LANE_DEPTH>" not in region
    return datestamped and not_depth_templated


def threshold_is_15(region: str) -> bool:
    """(d) The singleton/consolidate threshold is 15, judged on the WIDE count.

    8 was calibrated against the narrow counter's ~6 and is meaningless once
    the counter is text-based.
    """
    return "IF LANE_DEPTH < 15:" in region and "IF LANE_DEPTH < 8:" not in region


def append_branch_precedes_decline(region: str) -> bool:
    """Branch ORDER: append-to-open-drain must be tried BEFORE declining.

    When a drain goal is open, appending KEEPS the proposed check; declining
    DISCARDS it. So the decline branch is only correct when there is nowhere
    to put the check. Inverting these two silently throws away checks at
    exactly the moment one has somewhere useful to go.

    This ordering was introduced backwards during the g-115-3936 fix and was
    caught only by running the live probe — both branch conditions were
    non-empty simultaneously, which a reading of the pseudocode does not
    surface. That is why it is pinned.
    """
    i_append = region.find("ELIF DRAIN_GOAL is non-empty:")
    i_decline = region.find("ELIF DRAIN_RECENT is non-empty:")
    return 0 <= i_append < i_decline


def route_step_precedes_gate(body: str) -> bool:
    """(e) A routing step exists UPSTREAM of the lane-depth counter.

    Step 2.5 can only choose HOW to file, so it can never reduce arrivals.
    Only an upstream step that ends the spark with nothing can do that.
    """
    i_route = body.find(ROUTE_MARKER)
    i_gate = body.find(GATE_MARKER)
    return 0 <= i_route < i_gate


def decline_path_does_not_book_keep_a_spark(region: str) -> bool:
    """(f) The step-2.1 decline path must not credit sq-018 with a spark.

    Step 4's log template asserts a check was proposed (false on a decline)
    and step 5 increments sparks_generated, inflating the yield_rate the
    retire/promote review reads. Step 1's scope-filter SKIP already says not
    to increment; the decline path must agree with its sibling.
    """
    forbids_increment = "do NOT increment sparks_generated" in region
    no_blanket_continue = "and continue to steps 4-5." not in region
    return forbids_increment and no_blanket_continue


# --------------------------------------------------------------------------
# The pre-fix fixture — carries every defective form, compactly
# --------------------------------------------------------------------------

PRE_FIX_REGION = """
2.5. LANE-DEPTH GATE — make this producer self-limiting.
   Bash: bash core/scripts/aspirations-read.sh --source world --id asp-115 | py -3 -c "
   open_=[g for g in goals if g.get('status') in ('pending','in-progress')]
   lane=[g for g in open_ if (g.get('origin_signal') or '').startswith('maintain:sq-018')]
   isdrain=lambda g:(g.get('origin_signal') or '').startswith('maintain:drain-verify-learning-check-lane')
   drain=[g for g in open_ if isdrain(g)]
   print('LANE_DEPTH=%d' % len(lane))
   print('DRAIN_GOAL=%s' % (drain[0]['id'] if drain else ''))
   "
   IF LANE_DEPTH < 8:
       Proceed to step 3 — file a normal singleton.
   ELIF DRAIN_GOAL is non-empty:
       Append to the open drain goal.
   ELSE:
       File a drain goal with origin_signal
       maintain:drain-verify-learning-check-lane-<LANE_DEPTH>
"""

PRE_FIX_ROUTE_REGION = """
   If Q1 is yes, or Q2 routes elsewhere: SKIP steps 2.5 and 3 entirely. Log
   "sq-018: no spark", and continue to steps 4-5. Do NOT file.
"""

# The self-introduced ordering defect, isolated: decline placed above append.
INVERTED_ORDER_REGION = """
   IF LANE_DEPTH < 15:
       Proceed to step 3.
   ELIF DRAIN_RECENT is non-empty:
       Decline to file an (N+1)th drain round.
   ELIF DRAIN_GOAL is non-empty:
       Append to the open drain goal.
"""


# --------------------------------------------------------------------------
# Live-text tests — the invariant must HOLD in the shipped SKILL.md
# --------------------------------------------------------------------------

GATE_PREDICATES = [
    counter_is_text_based,
    drain_probe_has_recency_window,
    origin_signal_is_datestamped,
    threshold_is_15,
    append_branch_precedes_decline,
]


@pytest.mark.parametrize("predicate", GATE_PREDICATES, ids=lambda p: p.__name__)
def test_gate_invariant_holds_in_live_skill_md(predicate):
    region = _gate_region(_skill_body())
    assert predicate(region), (
        f"sq-018 lane-depth gate invariant {predicate.__name__} no longer holds in "
        f"{SPARK_SKILL_MD}. See the predicate docstring for the defect it prevents."
    )


def test_route_step_precedes_gate_in_live_skill_md():
    assert route_step_precedes_gate(_skill_body()), (
        "step 2.1 (ROUTE BEFORE YOU FILE) must appear upstream of step 2.5; only an "
        "upstream step can reduce lane ARRIVALS rather than re-route them."
    )


def test_decline_path_does_not_book_keep_a_spark_in_live_skill_md():
    assert decline_path_does_not_book_keep_a_spark(_route_region(_skill_body())), (
        "the step 2.1 decline path must forbid incrementing sparks_generated and must "
        "not blanket-forward to steps 4-5 (whose log template asserts a check was proposed)."
    )


# --------------------------------------------------------------------------
# Mutation-sensitivity tests — each predicate must REJECT the broken form.
# Without these, a predicate weakened into a tautology passes forever.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "predicate",
    [
        counter_is_text_based,
        drain_probe_has_recency_window,
        origin_signal_is_datestamped,
        threshold_is_15,
    ],
    ids=lambda p: p.__name__,
)
def test_gate_predicate_rejects_pre_fix_text(predicate):
    assert not predicate(PRE_FIX_REGION), (
        f"{predicate.__name__} PASSED against the known-defective pre-fix text. The "
        f"predicate has been weakened into a tautology and can no longer catch a "
        f"regression — which is the exact failure mode this file exists to prevent."
    )


def test_append_before_decline_predicate_rejects_inverted_order():
    assert not append_branch_precedes_decline(INVERTED_ORDER_REGION), (
        "append_branch_precedes_decline PASSED against a region where the decline "
        "branch sits above the append branch — the ordering defect that would discard "
        "proposed checks whenever a drain goal is open."
    )


def test_route_predicate_rejects_body_without_route_step():
    body_without_route = "preamble\n" + GATE_MARKER + "\nbody\n"
    assert not route_step_precedes_gate(body_without_route), (
        "route_step_precedes_gate PASSED on a body with no step 2.1 at all."
    )


def test_decline_predicate_rejects_pre_fix_route_text():
    assert not decline_path_does_not_book_keep_a_spark(PRE_FIX_ROUTE_REGION), (
        "decline_path_does_not_book_keep_a_spark PASSED against the pre-fix decline "
        "path, which forwarded to steps 4-5 and credited a phantom spark."
    )


# --------------------------------------------------------------------------
# Region extraction must not silently degrade to an empty/whole-file slice —
# a vacuous region would make every live-text assertion above meaningless
# (rb-245: verify the denominator before believing a result).
# --------------------------------------------------------------------------


def test_gate_region_is_non_vacuous_and_bounded():
    body = _skill_body()
    region = _gate_region(body)
    assert len(region) > 800, f"gate region suspiciously small ({len(region)} chars)"
    assert len(region) < len(body), "gate region must be a proper slice, not the whole file"
    assert ROUTE_MARKER not in region, "gate region leaked upstream into step 2.1"


def test_route_region_is_non_vacuous_and_bounded():
    body = _skill_body()
    region = _route_region(body)
    assert len(region) > 400, f"route region suspiciously small ({len(region)} chars)"
    assert GATE_MARKER not in region, "route region leaked downstream into step 2.5"
