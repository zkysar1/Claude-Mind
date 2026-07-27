"""test_skill_discovery_verdict_suppression.py — regression test for .

Pins the verdict-aware suppression gate in skill-discovery.py `classify()`:
a skill already triaged to KEEP must stop re-flagging while its metric is
UNCHANGED, but must re-flag the moment new signal appears.

WHY THIS NEEDS A PERSISTED TEST (sq-019, filed against g-115-3084's own close):
the suppression's safety property is a branch that does NOT fire in the happy
path. A regression making suppression unconditional would be SILENT — the audit
would simply stop flagging skills, which reads as a clean report, not a failure.
There is no loud symptom to notice. The original 3-branch proof ran in-turn in a
heredoc and was never persisted, so nothing re-ran it.

THE LOAD-BEARING CASE is rb-3132: a same-class recurrence AFTER a remediated
verdict FALSIFIES that verdict. Suppression is not a blanket mute — an
invocation dated after the verdict means the skill was used and went cold
AGAIN, which is new signal and must re-flag (test_new_signal_after_verdict_*).

REACHABILITY NOTE — the rb-3132 branch is UNREACHABLE via `cold_after_use`
under the shipped config, because window_days (60) == action_cold_days (60):
an invocation cannot be both newer than a <=60d verdict AND >=60d stale. It IS
reachable via `declining`, which is why the recurrence cases below are built on
a declining fixture rather than a cold one. If those two values are ever tuned
independently the reachability changes — this test constructs the declining
case explicitly rather than relying on cold_after_use, so it stays valid.

NEGATIVE CONTROL: deleting the `not new_signal_since_verdict` condition from
classify() must make test_new_signal_after_verdict_still_flags FAIL. A guard
that cannot fail is worse than no guard.

Run: py -3 -m pytest core/scripts/tests/test_skill_discovery_verdict_suppression.py -v
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

NOW = datetime(2026, 7, 27, 0, 0, 0)


def _load_module():
    """Load skill-discovery.py by spec — filename has hyphens."""
    spec = importlib.util.spec_from_file_location(
        "skill_discovery", CORE_SCRIPTS / "skill-discovery.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load skill-discovery.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SD = _load_module()


def _strategy(enabled=True, window_days=60, present=True):
    """Live strategy with the verdict_suppression section under test."""
    s = copy.deepcopy(SD.load_strategy())
    if not present:
        s.pop("verdict_suppression", None)
    else:
        s["verdict_suppression"] = {"enabled": enabled,
                                    "window_days": window_days}
    return s


def _classify(dates, forged_days_ago, strategy, verdicts=None):
    """Call classify() with one skill whose invocations are `dates`."""
    name = "probe-skill"
    forged = {"forged_date": (NOW - timedelta(days=forged_days_ago)).isoformat()}
    return SD.classify(
        skill_name=name, forged_info=forged, quality_data={},
        relations_data={}, journal_dates={name: list(dates)},
        companion_dates={}, ledger_dates={}, strategy=strategy,
        now=NOW, verdicts=verdicts,
    )


def _cold_fixture():
    """A skill last used 120d ago → cold_after_use with action_required."""
    return [NOW - timedelta(days=120)], 400


def _declining_fixture():
    """Declining: sparse last-14d vs dense prior-30d, last use 7d ago.

    Kept OUT of cold_after_use (days_since_last=7 < staleness 30) so the
    `declining` branch is the one that sets action_required — the only
    status under the shipped config that can carry an invocation NEWER
    than a <=60d verdict (see REACHABILITY NOTE in the module docstring).
    """
    dates = [NOW - timedelta(days=7)]                       # last window: 1
    dates += [NOW - timedelta(days=d) for d in range(20, 40)]  # prior: dense
    return sorted(dates), 400


def _verdict(days_ago, goal_id="g-115-2389", verdict="KEEP"):
    return {"probe-skill": {"verdict": verdict, "goal_id": goal_id,
                            "verdict_date": NOW - timedelta(days=days_ago)}}


# ── Baseline: the fixtures actually reach the statuses the cases assume ────
def test_fixtures_reach_expected_statuses():
    dates, forged = _cold_fixture()
    assert _classify(dates, forged, _strategy())["status"] == "cold_after_use"
    dates, forged = _declining_fixture()
    r = _classify(dates, forged, _strategy())
    assert r["status"] == "declining", r["status"]
    assert r["action_required"] is True


# ── 1. Recent verdict + no invocation since → SUPPRESSED ──────────────────
def test_recent_verdict_no_new_signal_suppresses():
    dates, forged = _cold_fixture()
    r = _classify(dates, forged, _strategy(), _verdict(days_ago=30))
    assert r["action_required"] is False
    vs = r["verdict_suppressed"]
    assert vs is not None
    assert vs["verdict"] == "KEEP"
    assert vs["verdict_goal"] == "g-115-2389"
    assert vs["verdict_age_days"] == 30.0
    assert vs["window_days"] == 60


# ── 2. rb-3132 — invocation AFTER the verdict → MUST re-flag ──────────────
# THE LOAD-BEARING CASE. Deleting `not new_signal_since_verdict` from
# classify() must make this test fail (negative control).
def test_new_signal_after_verdict_still_flags():
    dates, forged = _declining_fixture()          # last invocation 7d ago
    r = _classify(dates, forged, _strategy(), _verdict(days_ago=30))
    assert r["action_required"] is True, "rb-3132: recurrence must re-flag"
    assert r["verdict_suppressed"] is None


# ── 3. Verdict older than the window → NOT suppressed ─────────────────────
def test_verdict_older_than_window_does_not_suppress():
    dates, forged = _cold_fixture()
    r = _classify(dates, forged, _strategy(), _verdict(days_ago=90))
    assert r["action_required"] is True
    assert r["verdict_suppressed"] is None


# ── 4. No verdict at all → unchanged pre- behavior ──────────────
def test_no_verdict_leaves_behavior_unchanged():
    dates, forged = _cold_fixture()
    for verdicts in (None, {}, {"other-skill": {
            "verdict": "KEEP", "goal_id": "g-1",
            "verdict_date": NOW - timedelta(days=1)}}):
        r = _classify(dates, forged, _strategy(), verdicts)
        assert r["action_required"] is True
        assert r["verdict_suppressed"] is None


# ── 5. Section disabled or absent → today's behavior, zero cost ───────────
def test_disabled_or_absent_section_never_suppresses():
    dates, forged = _cold_fixture()
    for strat in (_strategy(enabled=False), _strategy(present=False)):
        r = _classify(dates, forged, strat, _verdict(days_ago=30))
        assert r["action_required"] is True
        assert r["verdict_suppressed"] is None


# ── 6. Only an explicit KEEP settles — the collector must not emit others ─
# collect_triage_verdicts() matches \bKEEP\b, so a RETIRE/REVISIT note never
# becomes a verdict record. Pin that at the collector, since classify() by
# contract suppresses on whatever the collector hands it.
def test_collector_only_matches_keep_verdicts():
    import inspect
    src = inspect.getsource(SD.collect_triage_verdicts)
    assert r"\bKEEP\b" in src, "collector must match an explicit KEEP token"
    for other in ("RETIRE", "REVISIT", "DEFER"):
        assert r"\b" + other + r"\b" not in src


# ── 7. No silent caps — every suppression is auditable ────────────────────
def test_suppression_is_reported_not_invisible():
    """A suppressed flag must remain visible in the record, so the audit
    reports `verdict_suppressed_count` rather than silently shrinking."""
    dates, forged = _cold_fixture()
    r = _classify(dates, forged, _strategy(), _verdict(days_ago=30))
    assert "verdict_suppressed" in r
    assert "g-115-3084" in r["verdict_suppressed"]["reason"]
    assert "rb-3132" in r["verdict_suppressed"]["reason"]
