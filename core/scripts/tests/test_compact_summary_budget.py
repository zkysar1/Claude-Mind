"""Regression pin for the bounded compact summary ().

WHY THIS FILE EXISTS AND WHAT IT MUST NOT BE. The projection it guards went
2.57x over the Read-tool cap and stayed there for months. The goal that
motivated the fix names the trap explicitly: "A test that asserts only
'summary < full' passes today against the broken behaviour." So the central
test here (test_old_projection_would_fail_this_pin) asserts the PRE-FIX
projection FAILS the same assertion the post-fix one passes. A pin that
cannot fail against the defect it names is not a pin.

The seeded corpus is deliberately LARGER than the live one at the time of
writing (2,249 goals across 22 aspirations, 672,651 B unbounded), because a
bound tuned to today's corpus is exactly the defect being fixed.
"""

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

from _compact_summary import (  # noqa: E402
    BUDGET_FRACTION,
    DEFER_REASON_MAX,
    READ_TOOL_CAP,
    SUMMARY_KEEP,
    build_summary,
    goal_tier,
)

DEFAULT_BUDGET = int(READ_TOOL_CAP * BUDGET_FRACTION)


def encoded(obj):
    """Serialize exactly as load-aspirations-compact.sh writes the file."""
    return json.dumps(obj, separators=(',', ':'),
                      ensure_ascii=True).encode('utf-8')


def make_goal(gid, **over):
    goal = {
        'id': gid,
        # Titles dominate the byte census (42.3% of the live file), so the
        # seed uses a realistically long one rather than a short stub.
        'title': ('Recurring: sweep the thing that needs sweeping and report '
                  'its positive control so the zero is evidence ' + gid),
        'status': 'pending',
        'priority': 'MEDIUM',
        'category': 'framework-maintenance',
        'participants': ['agent'],
        'description': 'x' * 400,      # must be projected away
        'outcome_note': 'y' * 2000,    # must be projected away
    }
    goal.update(over)
    return goal


def make_corpus(n_asp=30, per_asp=400):
    """A corpus larger than the live one that motivated the fix."""
    corpus = []
    for a in range(n_asp):
        goals = []
        for i in range(per_asp):
            gid = 'g-%03d-%04d' % (a, i)
            if i % 40 == 0:
                goals.append(make_goal(gid, recurring=True, status='completed',
                                       interval_hours=168, achievedCount=7,
                                       lastAchievedAt='2026-08-01T00:00:00'))
            elif i % 7 == 0:
                goals.append(make_goal(gid, status='skipped'))
            elif i % 11 == 0:
                goals.append(make_goal(gid, status='completed'))
            elif i % 13 == 0:
                goals.append(make_goal(gid, status='in-progress'))
            elif i % 17 == 0:
                goals.append(make_goal(gid, priority='HIGH'))
            else:
                goals.append(make_goal(gid))
        corpus.append({'id': 'asp-%03d' % a, 'status': 'active',
                       'title': 'Seeded aspiration %d' % a, 'goals': goals})
    return corpus


# --- the bound -----------------------------------------------------------

def test_summary_fits_under_cap_on_oversized_corpus():
    corpus = make_corpus()
    total = sum(len(a['goals']) for a in corpus)
    assert total > 2249, 'seed must exceed the live corpus that broke the cap'
    summary, stats = build_summary(corpus)
    size = len(encoded(summary))
    assert size <= DEFAULT_BUDGET, (
        'summary %d B exceeds budget %d B' % (size, DEFAULT_BUDGET))
    assert size < READ_TOOL_CAP


@pytest.mark.parametrize('n_asp,per_asp', [(22, 102), (30, 400), (60, 800),
                                           (200, 500)])
def test_bound_holds_as_the_store_grows(n_asp, per_asp):
    """The bound comes from the CAP, so growth changes what is dropped, never
    whether the file fits. This is the property a constant tuned to today's
    corpus would not have.

    ASSERTS AGAINST READ_TOOL_CAP FIRST, DELIBERATELY. An earlier draft checked
    only `<= DEFAULT_BUDGET`, and DEFAULT_BUDGET is derived from
    BUDGET_FRACTION -- the very constant a regression would move. Mutating the
    fraction to 99.0 moved the threshold with it and this test passed while the
    file blew past the cap. A pin whose threshold drifts with the thing it
    pins is not a pin; that is the same defect shape as a bound tuned to
    today's corpus. READ_TOOL_CAP is external and fixed, so it cannot drift."""
    size = len(encoded(build_summary(make_corpus(n_asp, per_asp))[0]))
    assert size < READ_TOOL_CAP, (
        '%d B breaches the Read-tool cap at %d aspirations x %d goals'
        % (size, n_asp, per_asp))
    assert size <= DEFAULT_BUDGET


def test_old_projection_would_fail_this_pin():
    """THE DISCRIMINATOR. Reproduces the pre-fix projection verbatim and
    asserts it BREACHES the cap on the same corpus the new one survives. If
    this test ever passes trivially, the pin above has stopped discriminating
    and is worthless."""
    corpus = make_corpus()
    old_keep = SUMMARY_KEEP - {'achievedCount'}
    old = []
    for asp in corpus:
        entry = {k: v for k, v in asp.items() if k != 'goals'}
        entry['goals'] = [
            {k: v for k, v in g.items() if k in old_keep}
            for g in asp.get('goals', [])
            if not (g.get('status') == 'completed' and not g.get('recurring'))
        ]
        old.append(entry)
    old_size = len(encoded(old))
    assert old_size > READ_TOOL_CAP, (
        'the pre-fix projection must breach the cap on this corpus, else this '
        'pin proves nothing (measured %d B)' % old_size)
    new_size = len(encoded(build_summary(corpus)[0]))
    assert new_size < old_size


# --- what survives -------------------------------------------------------

def test_recurring_goals_are_never_dropped_for_status():
    """A recurring goal sits at status=completed between fires. Excluding on
    terminal status alone would drop the entire recurring population at
    whatever moment the cycle caught it -- and strategic-scan S1 surveys
    exactly that population."""
    goal = make_goal('g-1', recurring=True, status='completed')
    assert goal_tier(goal) == 0
    for status in ('skipped', 'expired', 'decomposed', 'superseded'):
        assert goal_tier(make_goal('g-x', recurring=True,
                                   status=status)) == 0


def test_terminal_non_recurring_goals_are_excluded():
    for status in ('completed', 'skipped', 'expired', 'decomposed',
                   'superseded'):
        assert goal_tier(make_goal('g-t', status=status)) is None


def test_retired_is_not_invented_as_terminal():
    """`retired` is NOT in the SSOT terminal set. Adding it here would fork the
    vocabulary away from aspirations.TERMINAL_GOAL_STATUSES."""
    assert goal_tier(make_goal('g-r', status='retired')) is not None


def test_achieved_count_is_carried():
    """strategic-scan S1 gates on `achievedCount >= 2`. The field was absent
    from every row of the old projection, so S1 selected 0 of 82 recurring
    goals and had never emitted a signal (g-115-5824 addendum)."""
    assert 'achievedCount' in SUMMARY_KEEP
    summary, _ = build_summary(make_corpus(4, 80))
    recurring = [g for a in summary for g in a['goals'] if g.get('recurring')]
    assert recurring, 'seed must contain recurring goals'
    assert all('achievedCount' in g for g in recurring)
    assert sum(1 for g in recurring if g.get('achievedCount', 0) >= 2) > 0


def test_verbose_fields_are_projected_away():
    summary, _ = build_summary(make_corpus(3, 50))
    for asp in summary:
        for goal in asp['goals']:
            assert 'description' not in goal
            assert 'outcome_note' not in goal
            assert set(goal) <= SUMMARY_KEEP


def test_defer_reason_is_truncated_visibly():
    corpus = [{'id': 'asp-0', 'status': 'active',
               'goals': [make_goal('g-1', defer_reason='z' * 900)]}]
    summary, _ = build_summary(corpus)
    reason = summary[0]['goals'][0]['defer_reason']
    assert len(reason) == DEFER_REASON_MAX + 3
    assert reason.endswith('...')


# --- honesty about what was dropped --------------------------------------

def test_omissions_are_recorded_inline_and_reconcile():
    """No silent caps: every dropped row is counted inline on its aspiration,
    and those counts must sum to the stats figure."""
    corpus = make_corpus()
    summary, stats = build_summary(corpus)
    inline = sum(a.get('goals_omitted', 0) for a in summary)
    assert inline == stats['goals_omitted_for_budget']
    assert inline > 0, 'this corpus must overflow, else the test is vacuous'
    kept = sum(len(a['goals']) for a in summary)
    assert kept + inline == stats['goals_eligible']
    assert stats['goals_included'] == kept


def test_no_omitted_key_when_nothing_dropped():
    corpus = [{'id': 'asp-0', 'status': 'active',
               'goals': [make_goal('g-1')]}]
    summary, stats = build_summary(corpus)
    assert stats['goals_omitted_for_budget'] == 0
    assert 'goals_omitted' not in summary[0]


# --- consumer shape ------------------------------------------------------

def test_top_level_shape_is_unchanged():
    """LLM pseudocode iterates `for asp in compact for g in asp.goals` and
    filters `asp.status == 'active'`. The truncation record is an inline key
    precisely so this shape survives -- a wrapper object would break every
    consumer at once in order to report that some rows are missing."""
    summary, _ = build_summary(make_corpus(5, 60))
    assert isinstance(summary, list)
    for asp in summary:
        assert isinstance(asp, dict)
        assert isinstance(asp.get('goals'), list)
        assert 'id' in asp and 'status' in asp


def test_aspiration_shells_always_survive():
    """Aspiration-level fields feed fresh-eyes completion_health and
    strategic-scan S3/S4a. They are budgeted before goal rows so a large goal
    population can never crowd them out."""
    corpus = make_corpus(200, 500)
    summary, _ = build_summary(corpus)
    assert len(summary) == len(corpus)
    assert [a['id'] for a in summary] == [a['id'] for a in corpus]


def test_projection_is_deterministic():
    """A projection that reshuffles between regenerations defeats the
    context-reads dedup that gates re-Reads of this file."""
    corpus = make_corpus(10, 120)
    assert encoded(build_summary(corpus)[0]) == encoded(build_summary(corpus)[0])


def test_empty_corpus_is_safe():
    summary, stats = build_summary([])
    assert summary == []
    assert stats['goals_total'] == 0
