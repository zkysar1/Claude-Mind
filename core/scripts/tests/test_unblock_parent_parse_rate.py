"""Pins the PARSE RATE of unblock-parent-status-sweep, not merely the parser ().

The defect was not that the regexes were wrong in isolation — each matched the
shape it was written for. It was that NOTHING THE FRAMEWORK ACTUALLY EMITS had
those shapes, so the parse rate against the live corpus was near-zero while
every unit test of the individual patterns stayed green. A test suite that only
asserts "this regex matches this string" cannot see that; this file asserts the
recovered COUNT over a fixture of real emitted shapes.

Every fixture below is copied from a live goal, not invented. The shapes come
from two real emitters:
  - recurring-starvation-check._origin_signal ->
      "unblock:recurring-starved-<goal-id>"            (world-source)
      "unblock:recurring-starved-<owner>-<goal-id>"    (agent-source, qualified)
  - the origin-signal gate's Layer-D auto-derive, which slugifies the TITLE, so
    an id lands mid-string and a possessive becomes "g-354-21-s".

POSITIVE CONTROL: test_fixture_is_red_against_the_old_regexes re-implements the
two pre-fix patterns and asserts the corpus scores 0 through them. Without it
this file would pass just as happily against the broken parser.
"""
import importlib.util
import os
import re
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SWEEP = SCRIPTS / "unblock-parent-status-sweep.py"


def _import_sweep():
    spec = importlib.util.spec_from_file_location("_ups", SWEEP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Real shapes, real ids. (goal, expected_parent_id_when_bound_to "alpha")
LIVE_CORPUS = [
    # --- recurring-starvation, world-source: id at END, not after the prefix.
    # Rejected pre-fix by rule 1 (prefix requirement) and rule 2 (no "for").
    ({"id": "g-248-124",
      "title": "Unblock: recurring goal g-248-28 has stopped firing "
               "(353.3h = 3.15x its expected cadence)",
      "origin_signal": "unblock:recurring-starved-g-248-28"}, "g-248-28"),
    ({"id": "g-326-135",
      "title": "Unblock: recurring goal g-326-84 has stopped firing "
               "(12.2h = 3.05x its expected cadence)",
      "origin_signal": "unblock:recurring-starved-g-326-84"}, "g-326-84"),
    # --- recurring-starvation, AGENT-source: owner-qualified. Parses only when
    # this process is bound to that same owner (see the foreign-owner tests).
    ({"id": "g-001-354",
      "title": "Unblock: recurring goal g-001-02 has stopped firing "
               "(272.3h = 3.03x its expected cadence)",
      "origin_signal": "unblock:recurring-starved-alpha-g-001-02"}, "g-001-02"),
    # --- id immediately after the prefix but NOT end-anchored -> rule 1 missed
    # it purely on the trailing slug.
    ({"id": "g-335-1010",
      "title": "Unblock: g-335-983 is CLOSED but its deliverable is unshipped "
               "— two competing open PRs (#180, #187)",
      "origin_signal": "unblock:g-335-983-unshipped-duplicate-prs"}, "g-335-983"),
    # --- id MID-string in the auto-derived slug.
    ({"id": "g-335-944",
      "title": "Unblock: merge PR #130 (g-335-936 PDF font-embedding "
               "assertion) — blocked only by a GitHub Actions bump",
      "origin_signal": "unblock:merge-pr-130-g-335-936-pdf-font-embedding"},
     "g-335-936"),
    # --- SLUGIFIED POSSESSIVE: "'s" becomes "-s". The embedded
    # scan must stop at the non-digit and yield , never -s.
    ({"id": "g-354-99",
      "title": "Unblock: re-derive g-354-21's user leg against the newly "
               "materialized grant",
      "origin_signal": "unblock:re-derive-g-354-21-s-user-leg-against-the-newly"},
     "g-354-21"),
]

# Must NOT parse — skip rather than guess. NOTE none of these carries a `for`
# anchor, deliberately: `for` is a HIGHER-priority rule and resolves a two-id
# title on purpose (see test_for_anchor_still_disambiguates_a_multi_id_title).
# A fixture with both would be asserting the opposite of that test.
AMBIGUOUS_CORPUS = [
    {"id": "g-999-01",
     "title": "Unblock: g-111-11 and g-222-22 both block this lane",
     "origin_signal": "unblock:g-111-11-and-g-222-22-both-block-this-lane"},
    {"id": "g-999-02",
     "title": "Unblock: reconcile g-333-33 and g-444-44 before either lands",
     "origin_signal": "unblock:reconcile-g-333-33-and-g-444-44-before-either"},
]

# Genuinely unparseable — no id anywhere. Real shapes from the live corpus.
UNPARSEABLE_CORPUS = [
    {"id": "g-115-9001",
     "title": "Unblock: outcome-observation hook watches only one workspace "
              "root — other repos are invisible",
     "origin_signal": "unblock:outcome-observation-hook-watches-only-one-root"},
    {"id": "g-328-36b",
     "title": "Unblock: run own-cloud bootstrap pull on the affected box",
     "origin_signal": "unblock:owncloud-bootstrap-pull-affected-box"},
]


@pytest.fixture
def bound_alpha(monkeypatch):
    monkeypatch.setenv("MIND_AGENT", "alpha")


# ------------------------------------------------------- the rate itself

def test_parse_rate_over_live_shapes(bound_alpha):
    """THE load-bearing assertion: the COUNT, not any single pattern."""
    mod = _import_sweep()
    got = {g["id"]: mod._parse_parent_id(g) for g, _ in LIVE_CORPUS}
    want = {g["id"]: expected for g, expected in LIVE_CORPUS}
    assert got == want, (
        "parse rate regressed over real emitted shapes; "
        f"recovered {sum(1 for v in got.values() if v)}/{len(LIVE_CORPUS)}")


def test_fixture_is_red_against_the_old_regexes():
    """POSITIVE CONTROL. Re-implements the two pre-fix patterns verbatim and
    asserts they recover ZERO of the live corpus.

    Without this, every assertion above would pass against the broken parser
    if the fallback rules were silently removed — the fixture would simply be
    testing rule 3 (discovered_by), which none of these goals carry.
    """
    old_origin = re.compile(r"^unblock:(g-\d+-\d+)\s*$")
    old_title_for = re.compile(r"\bfor\s+(g-\d+-\d+)\b")
    recovered = []
    for g, _ in LIVE_CORPUS:
        if old_origin.match((g.get("origin_signal") or "").strip()):
            recovered.append(g["id"])
        elif old_title_for.search(g.get("title") or ""):
            recovered.append(g["id"])
    assert recovered == [], (
        f"the pre-fix regexes recovered {recovered} — this fixture no longer "
        f"demonstrates the defect, so it cannot prove the fix")


def test_ambiguous_titles_skip_rather_than_guess(bound_alpha):
    """Two referents and no `for` anchor => unparseable, never a coin flip."""
    mod = _import_sweep()
    for g in AMBIGUOUS_CORPUS:
        assert mod._parse_parent_id(g) is None, (
            f"{g['id']} carries two goal-ids and must not resolve to either")


def test_for_anchor_still_disambiguates_a_multi_id_title(bound_alpha):
    """The `for` anchor outranks the ambiguity skip — that is why it is kept
    as a higher-priority rule rather than folded into the single-id scan."""
    mod = _import_sweep()
    g = {"id": "g-999-03",
         "title": "Unblock: blocked by g-111-11, needed for g-222-22",
         "origin_signal": ""}
    assert mod._parse_parent_id(g) == "g-222-22"


def test_genuinely_unparseable_stays_unparseable(bound_alpha):
    """Widening must not manufacture a parent where the emitter recorded none.
    A false parent is worse than no parent: it sweeps against a stranger."""
    mod = _import_sweep()
    for g in UNPARSEABLE_CORPUS:
        assert mod._parse_parent_id(g) is None, g["id"]


# ------------------------------- cross-agent id-space collision (g-001-NN)

AGENT_QUALIFIED = {
    "id": "g-001-354",
    "title": "Unblock: recurring goal has stopped firing",
    "origin_signal": "unblock:recurring-starved-alpha-g-001-02",
}


def test_agent_qualified_key_parses_for_its_own_owner(monkeypatch):
    monkeypatch.setenv("MIND_AGENT", "alpha")
    assert _import_sweep()._parse_parent_id(AGENT_QUALIFIED) == "g-001-02"


@pytest.mark.parametrize("me", ["bravo", ""])
def test_agent_qualified_key_is_refused_for_a_foreign_owner(monkeypatch, me):
    """g-001-NN is NOT unique fleet-wide — every agent has its own .

    Stripping the owner qualifier and returning a bare id would be resolved
    against THIS box's index, where both outcomes are wrong: it matches our
    identically-numbered goal (wrong status, silently), or it is absent — and
    absence defaults to "archived", which is IN TERMINAL_STATES, so the sweep
    would close the Unblock on the strength of a goal it never read.

    Empty binding is refused too: unable to prove the key is ours is not the
    same as proving it is.
    """
    monkeypatch.setenv("MIND_AGENT", me)
    assert _import_sweep()._parse_parent_id(AGENT_QUALIFIED) is None


def test_world_source_key_is_never_treated_as_agent_qualified(monkeypatch):
    """World ids ARE globally unique, so the bare form must keep parsing for
    any binding — the qualifier guard must not over-reach onto it."""
    mod = _import_sweep()
    g = {"id": "g-248-124",
         "title": "Unblock: recurring goal g-248-28 has stopped firing",
         "origin_signal": "unblock:recurring-starved-g-248-28"}
    for me in ("alpha", "bravo", ""):
        monkeypatch.setenv("MIND_AGENT", me)
        assert mod._parse_parent_id(g) == "g-248-28", me


# ------------------------------------ recurring parents: freshness, not status

def _hours_ago(mod, h):
    import datetime as dt
    return (dt.datetime.now() - dt.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%S")


def test_recurring_parent_resolved_only_when_cadence_resumed():
    mod = _import_sweep()
    # Fired well within cadence -> the starvation Unblock is moot.
    ok, why = mod._recurring_parent_resolved(12.0, _hours_ago(mod, 13.4))
    assert ok is True and "resumed" in why
    # Still starved at > 3x -> the Unblock is still live.
    ok, why = mod._recurring_parent_resolved(112.0, _hours_ago(mod, 451.9))
    assert ok is False and "still starved" in why


def test_recurring_threshold_matches_the_filing_detector():
    """The resolution predicate must be the exact negation of the predicate
    that FILED the Unblock, or the two components disagree about the same goal:
    a stricter value strands Unblocks the detector would no longer file, a
    looser one closes ones it still would."""
    mod = _import_sweep()
    assert mod.STARVATION_MULTIPLIER == 3.0
    interval = 10.0
    just_inside, _ = mod._recurring_parent_resolved(interval, _hours_ago(mod, 29.0))
    just_outside, _ = mod._recurring_parent_resolved(interval, _hours_ago(mod, 31.0))
    assert just_inside is True and just_outside is False


@pytest.mark.parametrize("interval,last", [
    (None, "2026-08-09T19:21:35"),   # no interval
    ("", "2026-08-09T19:21:35"),
    (0, "2026-08-09T19:21:35"),      # non-positive interval
    (12.0, None),                    # never fired / missing stamp
    (12.0, "not-a-timestamp"),
])
def test_undecidable_recurrence_returns_none_not_a_guess(interval, last):
    """None means "cannot decide" and the caller skips. A guess here would
    close a goal on the strength of a field it could not read."""
    resolved, why = _import_sweep()._recurring_parent_resolved(interval, last)
    assert resolved is None
    assert why, "an undecidable verdict must still explain itself"


def test_recurrence_index_omits_non_recurring_goals():
    """Membership IS the recurring test at the call site, so a non-recurring
    goal leaking in would silently reroute it off the status predicate."""
    mod = _import_sweep()
    asps = [({"id": "asp-1", "status": "active", "goals": [
        {"id": "g-1-1", "recurring": True, "interval_hours": 6.0,
         "lastAchievedAt": "2026-08-09T00:00:00"},
        {"id": "g-1-2", "status": "pending"},
        {"id": "g-1-3", "recurring": False, "interval_hours": 6.0},
    ]}, "world")]
    idx = mod._build_recurrence_index(asps)
    assert set(idx) == {"g-1-1"}
    assert idx["g-1-1"] == (6.0, "2026-08-09T00:00:00")
