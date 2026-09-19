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


# --- the census id sets () ------------------------------------
#
# WHY THESE ARE SEPARATE FROM make_corpus. The seeded corpus above carries NO
# archived_census at all, which is exactly why this whole file stayed green for
# months while the LIVE projection dropped 110 tier-0 rows and every pending-HIGH
# row. A synthetic seed only pins the defects it happens to model; the live-corpus
# detector is check 62 in core/config/verification-checklist.md. These tests add
# the missing model so the two agree.

TIER_ZERO_LABEL = 'always (recurring/in-progress/blocked)'


def make_census(asp_id, n_ids, statuses=('completed', 'skipped'), baseline=5):
    """An archived_census whose evicted_ids alone can exceed the whole budget.

    `by_status` carries a nonzero LEGACY baseline so the count-preservation test
    below proves the projection keeps `baseline + len(ids)`, not merely `len(ids)`.
    """
    per = max(1, n_ids // len(statuses))
    return {
        'by_status': {s: baseline for s in statuses},
        'census_note': 'seeded census for the budget pin',
        'evicted_ids': {
            s: ['g-%s-%05d' % (asp_id, i) for i in range(per)]
            for s in statuses
        },
    }


def make_census_corpus(n_asp=8, per_asp=60, n_ids=1400):
    corpus = make_corpus(n_asp, per_asp)
    for asp in corpus:
        asp['archived_census'] = make_census(asp['id'], n_ids)
    return corpus


def test_census_id_sets_cannot_crowd_out_tier_zero_rows():
    """The census rides in the SHELL, which is budgeted first and
    unconditionally. Unprojected, an eviction-heavy store starves the very rows
    the projection exists to carry."""
    corpus = make_census_corpus()

    # POSITIVE CONTROL: the seeded census must genuinely exceed the budget,
    # otherwise a pass here proves nothing about crowding.
    census_bytes = len(encoded([a['archived_census'] for a in corpus]))
    assert census_bytes > DEFAULT_BUDGET, (
        'seeded census is only %d B and must exceed the %d B budget for this '
        'test to discriminate' % (census_bytes, DEFAULT_BUDGET))

    summary, stats = build_summary(corpus)
    assert TIER_ZERO_LABEL not in stats['dropped_by_tier'], (
        'tier-0 rows were dropped for budget: %r' % (stats['dropped_by_tier'],))

    # Assert the rows are actually PRESENT, not merely absent from the drop
    # tally -- a counter and a projection can disagree.
    kept = {g['id'] for asp in summary for g in asp['goals']}
    expected = {g['id'] for asp in corpus for g in asp['goals']
                if goal_tier(g) == 0}
    assert expected, 'corpus must contain tier-0 rows'
    assert expected <= kept, 'missing tier-0 rows: %r' % sorted(expected - kept)
    assert len(encoded(summary)) <= DEFAULT_BUDGET


def test_pre_fix_shell_projection_drops_tier_zero(monkeypatch):
    """THE DISCRIMINATOR. Restores the pre-fix shell projection on the live
    module and asserts it FAILS the pin above on the same corpus. If this ever
    stops failing, the test above has stopped discriminating."""
    import _compact_summary as mod

    corpus = make_census_corpus()
    monkeypatch.setattr(
        mod, 'project_shell',
        lambda asp: {k: v for k, v in asp.items() if k != 'goals'})
    _, stats = mod.build_summary(corpus)
    assert TIER_ZERO_LABEL in stats['dropped_by_tier'], (
        'the pre-fix projection must drop tier-0 rows on this corpus, else the '
        'pin proves nothing (dropped: %r)' % (stats['dropped_by_tier'],))


def test_census_projection_preserves_counts():
    """`census_by_status` is the SSOT every census consumer reads through, and
    it returns the legacy baseline PLUS len(evicted_ids[status]). Writing its
    result back as `by_status` makes the projection count-preserving, so
    `census_completed` and the cadence checks read the same number from the
    summary as from the full compact."""
    from _goal_census import census_by_status, census_completed

    corpus = make_census_corpus()
    summary, _ = build_summary(corpus)
    by_id = {a['id']: a for a in summary}
    for asp in corpus:
        projected = by_id[asp['id']]
        assert census_by_status(projected) == census_by_status(asp)
        assert census_completed(projected) == census_completed(asp)


def test_census_projection_does_not_mutate_the_full_compact():
    """The ids are eviction TOMBSTONES: coordination_merge._merge_goals uses
    them to stop a cross-box merge resurrecting evicted goals, and the goal-id
    mint sites read all_evicted_ids so a fresh id never collides. Collapsing
    them in place would destroy both from under the full store."""
    from _goal_census import all_evicted_ids

    corpus = make_census_corpus()
    before = {a['id']: all_evicted_ids(a) for a in corpus}
    assert any(before.values()), 'corpus must carry evicted ids'
    build_summary(corpus)
    assert {a['id']: all_evicted_ids(a) for a in corpus} == before


def test_summary_carries_no_evicted_id_list():
    corpus = make_census_corpus()
    summary, _ = build_summary(corpus)
    for asp in summary:
        census = asp.get('archived_census')
        assert isinstance(census, dict)
        assert 'evicted_ids' not in census
        assert isinstance(census.get('by_status'), dict)


def test_census_projection_is_inert_without_a_census():
    """An aspiration with no census, a non-dict census, or a census carrying no
    evicted_ids must pass through byte-identically."""
    from _compact_summary import project_shell

    for census in (None, 'not-a-dict', {}, {'by_status': {'completed': 3}}):
        asp = {'id': 'asp-999', 'status': 'active', 'goals': []}
        if census is not None:
            asp['archived_census'] = census
        shell = project_shell(asp)
        assert shell == {k: v for k, v in asp.items() if k != 'goals'}
