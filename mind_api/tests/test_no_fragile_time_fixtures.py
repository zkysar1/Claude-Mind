""" regression-class guard: meta-test for time-fragile fixtures.

Background (mind_api/docs/development-history/HARDENING.md §17.9 cleanup #3): the canonical fixture helper
`_make_asp_with_recurring_goal()` in test_runtime_aspirations_complete_by.py
used to hardcode `lastAchievedAt="2026-05-13T10:00:00"`. The complete_by
endpoint resets `currentStreak` to 1 when `elapsed > 2x interval_hours`
(48h for the 24h fixture). As wall-clock crossed 2026-05-15T10:00:00 the
hardcoded value aged past 48h and silently flipped this on-time-cycle
fixture into the overdue-reset path → `currentStreak == 1` instead of
the expected 3. The targeted test (`test_complete_by_recurring_cycle`)
started failing for what looked like a logic bug but was actually a
time-relative fixture sliding under it.

Fix (cleanup #3, commit 9bc3231): `lastAchievedAt` now computed as
`(datetime.now() - timedelta(hours=1)).isoformat()`, monotonically
within-interval. This meta-test pins the relative-time discipline so a
future regression that "simplifies back to a hardcoded ISO string"
re-introduces the same silent-flip bug.

Scope: this test is INTENTIONALLY narrow. It guards the CANONICAL
incident shape — the `_make_asp_with_recurring_goal` fixture helper.

⚠ THE "no other instances" CLAIM BELOW IS FALSIFIED BY EVENTS — read it as
history, not as current coverage (g-115-5539, 2026-08-28). A SECOND instance
of this exact class fired on 2026-08-16, three months after that sweep, in a
different file: `test_archive_sweep_roundtrip` in
test_runtime_experience_write.py leaned on conftest experience records dated
2026-05-10/11 being recent, and wall-clock crossed the 90-day
memory-pipeline staleness threshold on 2026-08-08, flipping an expected
no-op sweep into `archived == 2`. It has since been fixed the right way
(records re-stamped relative to now, not re-pinned).

The lesson is about the GUARD, not that fixture: a point-in-time sweep
cannot certify a class going forward, because the population grows after the
sweep. This meta-test pins ONE file and ONE helper (TARGET_FILE /
HELPER_NAME below), so every fixture added since 2026-05-18 is outside it —
a gate is only as broad as its entry points (guard-3448). Measured
2026-08-28 for whoever widens it: 478 hardcoded ISO dates sit on
time-sensitive field names across mind_api/tests, core/scripts/tests and
core/tests; 321 of those, in 82 files, are in files that ALSO mention an
age/staleness threshold. That 321 is a file-level co-occurrence heuristic
and NOT a bomb list — most are LWW-merge comparands dated relative to EACH
OTHER, which is correct and must not be "fixed". Do not act on the number
without discriminating; it is an upper bound on where to look, nothing more.

The other fixtures that hardcode
dates either:
  - assign to fields not consumed by relative-time logic (labels), or
  - intentionally set monotonically-past values (sibling test
    test_complete_by_recurring_streak_reset at line ~286 sets
    lastAchievedAt to a far-past date so the test ALWAYS sees overdue
    — that's the right pattern when the test branch under exercise
    IS the overdue path).

If you add a new test fixture that uses lastAchievedAt / last_active /
last_retrieved / outcome_date / claimed_at as time-sensitive input,
compute it relative to datetime.now() unless the test SPECIFICALLY
needs a monotonically-overdue or monotonically-fresh value.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TARGET_FILE = REPO_ROOT / "mind_api" / "tests" / "test_runtime_aspirations_complete_by.py"
HELPER_NAME = "_make_asp_with_recurring_goal"


def test_canonical_fixture_uses_relative_time():
    """The canonical recurring-goal fixture MUST compute lastAchievedAt
    from datetime.now(), not a hardcoded ISO string. A hardcoded value
    silently ages past the elapsed-time threshold and flips the test
    branch — the g-115-744 incident shape.
    """
    assert TARGET_FILE.exists(), f"target test file missing: {TARGET_FILE}"
    text = TARGET_FILE.read_text(encoding="utf-8")

    # Find the helper body. Walk from `def _make_asp_with_recurring_goal`
    # until the next top-level `def ` or end-of-file.
    helper_start_re = re.compile(
        rf"^def\s+{re.escape(HELPER_NAME)}\b", re.MULTILINE)
    m = helper_start_re.search(text)
    assert m, (f"helper {HELPER_NAME} not found in {TARGET_FILE.name} — "
               "if you renamed it, update this test or move the guard.")
    body_start = m.end()
    next_def = re.search(r"^def\s+\w+", text[body_start:], re.MULTILINE)
    body = text[body_start: body_start + (next_def.start() if next_def else len(text))]

    # The smoking gun: a hardcoded ISO date assigned to lastAchievedAt.
    fragile_re = re.compile(
        r'"lastAchievedAt"\s*:\s*"20\d\d-\d\d-\d\dT\d\d:\d\d:\d\d"')
    fragile_hits = fragile_re.findall(body)
    assert not fragile_hits, (
        f"{HELPER_NAME} contains hardcoded lastAchievedAt: {fragile_hits}. "
        f"This re-introduces the g-115-744 time-fragile-fixture bug "
        f"(cleanup #3, commit 9bc3231). Use "
        f"`(datetime.now() - timedelta(hours=1)).isoformat()` so the "
        f"fixture remains within-interval as wall-clock advances.")

    # Affirmative check: the helper DOES use a relative-time computation
    # for lastAchievedAt. Catches the case where someone removes the
    # field entirely (which would also break the on-time-cycle test
    # path, just less subtly).
    relative_re = re.compile(
        r'"lastAchievedAt"\s*:\s*\(?\s*datetime\.now\(\)\s*-\s*timedelta')
    assert relative_re.search(body), (
        f"{HELPER_NAME} no longer computes lastAchievedAt relative to "
        f"datetime.now(). If you intentionally restructured the fixture, "
        f"either update this guard or document the new pattern in "
        f"mind_api/docs/development-history/HARDENING.md §17.9 cleanup #3.")
