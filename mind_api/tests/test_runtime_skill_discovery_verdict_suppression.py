"""Daemon-side mirror of core/scripts/tests/test_skill_discovery_verdict_suppression.py.

Pins the verdict-aware suppression gate in the DAEMON's
mind_api/src/endpoints/skill_discovery.py `_classify()` — the branch ported
2026-08-01 by g-115-4326.

WHY A SEPARATE DAEMON-SIDE TEST, WHEN A CLI ONE ALREADY EXISTS AND THE
BYTE-COMPAT TESTS COMPARE THE TWO IMPLEMENTATIONS:

The byte-compat fixture (test_runtime_skill_discovery.py::TestByteCompat)
builds a world with NO aspirations.jsonl. `_collect_triage_verdicts` therefore
returns {} on every run, `verdicts` is falsy, and the suppression branch is
NEVER ENTERED on either side. Byte-compat proves the KEY SHAPE matches
(verdict_suppressed present and None; verdict_suppressed_count present and 0) —
which is exactly the parity break g-115-4326 found and fixed — but it cannot
reach the LOGIC. A daemon port of that logic that silently always-suppressed,
or never-suppressed, would pass byte-compat against a CLI doing the same
nothing.

That gap matters more here than it would elsewhere, for the reason the CLI
test's own docstring gives: the suppression's safety property is a branch that
does NOT fire in the happy path, so a regression making suppression
unconditional is SILENT — the audit simply stops flagging skills, which reads
as a clean report rather than a failure. Under daemon-only architecture the
daemon is the ONLY live path, so an untested daemon copy of that branch is the
one that actually decides what production does.

PARITY BY CONSTRUCTION: these cases feed the daemon's `_classify` the SAME
strategy dict the CLI's `load_strategy()` produces, and assert the SAME
outcomes the CLI test asserts. If the two implementations ever diverge on this
branch, one of the two suites fails.

NEGATIVE CONTROL (mirrors the CLI test): deleting the
`not new_signal_since_verdict` condition from the daemon's `_classify()` must
make test_new_signal_after_verdict_still_flags FAIL. A guard that cannot fail
is worse than no guard.

Run: STORAGE_BACKEND=local py -3 -m pytest \
       mind_api/tests/test_runtime_skill_discovery_verdict_suppression.py -v
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_SCRIPTS = REPO_ROOT / "core" / "scripts"
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

from mind_api.src.endpoints import skill_discovery as DSD  # noqa: E402

NOW = datetime(2026, 7, 27, 0, 0, 0)


def _load_cli_module():
    """Load core/scripts/skill-discovery.py by spec — filename has hyphens.

    The CLI module is the source of the strategy dict, so both suites exercise
    their implementation against identical config rather than a hand-built
    approximation that could drift from the shipped file.
    """
    spec = importlib.util.spec_from_file_location(
        "skill_discovery_cli", CORE_SCRIPTS / "skill-discovery.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load skill-discovery.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cli():
    return _load_cli_module()


def _strategy(cli_mod, enabled=True, window_days=60, present=True):
    """Live strategy with the verdict_suppression section under test."""
    s = copy.deepcopy(cli_mod.load_strategy())
    if not present:
        s.pop("verdict_suppression", None)
    else:
        s["verdict_suppression"] = {"enabled": enabled,
                                    "window_days": window_days}
    return s


def _classify(dates, forged_days_ago, strategy, verdicts=None):
    """Call the DAEMON's _classify() with one skill whose invocations are `dates`."""
    name = "probe-skill"
    forged = {"forged_date": (NOW - timedelta(days=forged_days_ago)).isoformat()}
    return DSD._classify(
        skill_name=name, forged_info=forged, quality_data={},
        relations_data={}, journal_dates={name: list(dates)},
        companion_dates={}, ledger_dates={}, strategy=strategy,
        now=NOW, verdicts=verdicts,
    )


def _cold_fixture():
    """A skill last used 120d ago -> cold_after_use with action_required."""
    return [NOW - timedelta(days=120)], 400


def _declining_fixture():
    """Declining: sparse last-14d vs dense prior-30d, last use 7d ago.

    Kept OUT of cold_after_use (days_since_last=7 < staleness 30) so the
    `declining` branch is the one that sets action_required — the only status
    under the shipped config that can carry an invocation NEWER than a <=60d
    verdict. Same reachability constraint the CLI test documents: window_days
    (60) == action_cold_days (60), so the rb-3132 branch is unreachable via
    cold_after_use.
    """
    dates = [NOW - timedelta(days=7)]                          # last window: 1
    dates += [NOW - timedelta(days=d) for d in range(20, 40)]  # prior: dense
    return sorted(dates), 400


def _verdict(days_ago, goal_id="g-115-2389", verdict="KEEP"):
    return {"probe-skill": {"verdict": verdict, "goal_id": goal_id,
                            "verdict_date": NOW - timedelta(days=days_ago)}}


# -- Baseline: the fixtures actually reach the statuses the cases assume ----
def test_fixtures_reach_expected_statuses(cli):
    dates, forged = _cold_fixture()
    assert _classify(dates, forged, _strategy(cli))["status"] == "cold_after_use"
    dates, forged = _declining_fixture()
    r = _classify(dates, forged, _strategy(cli))
    assert r["status"] == "declining", r["status"]
    assert r["action_required"] is True


# -- 1. Recent verdict + no invocation since -> SUPPRESSED ------------------
def test_recent_verdict_no_new_signal_suppresses(cli):
    dates, forged = _cold_fixture()
    r = _classify(dates, forged, _strategy(cli), _verdict(days_ago=30))
    assert r["action_required"] is False
    vs = r["verdict_suppressed"]
    assert vs is not None
    assert vs["verdict"] == "KEEP"
    assert vs["verdict_goal"] == "g-115-2389"
    assert vs["verdict_age_days"] == 30.0
    assert vs["window_days"] == 60


# -- 2. rb-3132 — invocation AFTER the verdict -> MUST re-flag --------------
# THE LOAD-BEARING CASE. Deleting `not new_signal_since_verdict` from the
# daemon's _classify() must make this test fail (negative control).
def test_new_signal_after_verdict_still_flags(cli):
    dates, forged = _declining_fixture()          # last invocation 7d ago
    r = _classify(dates, forged, _strategy(cli), _verdict(days_ago=30))
    assert r["action_required"] is True, "rb-3132: recurrence must re-flag"
    assert r["verdict_suppressed"] is None


# -- 3. Verdict older than the window -> NOT suppressed ---------------------
def test_verdict_older_than_window_does_not_suppress(cli):
    dates, forged = _cold_fixture()
    r = _classify(dates, forged, _strategy(cli), _verdict(days_ago=90))
    assert r["action_required"] is True
    assert r["verdict_suppressed"] is None


# -- 4. No verdict at all -> unchanged pre-port behavior --------------------
# This is the case the byte-compat fixture exercises (no aspirations.jsonl), so
# it is the one place the two suites overlap. Kept because it pins that the
# port did not change the no-verdict path the rest of the fleet relies on.
def test_no_verdict_leaves_behavior_unchanged(cli):
    dates, forged = _cold_fixture()
    for verdicts in (None, {}, {"other-skill": {
            "verdict": "KEEP", "goal_id": "g-1",
            "verdict_date": NOW - timedelta(days=1)}}):
        r = _classify(dates, forged, _strategy(cli), verdicts)
        assert r["action_required"] is True
        assert r["verdict_suppressed"] is None


# -- 5. Section disabled or absent -> today's behavior, zero cost -----------
def test_disabled_or_absent_section_never_suppresses(cli):
    dates, forged = _cold_fixture()
    for strat in (_strategy(cli, enabled=False), _strategy(cli, present=False)):
        r = _classify(dates, forged, strat, _verdict(days_ago=30))
        assert r["action_required"] is True
        assert r["verdict_suppressed"] is None


# -- 6. Only an explicit KEEP settles — the collector must not emit others --
# The daemon's _collect_triage_verdicts is the ported twin of the CLI
# collector; _classify by contract suppresses on whatever the collector hands
# it, so the KEEP-only discrimination is pinned at the collector.
def test_collector_only_matches_keep_verdicts():
    import inspect
    src = inspect.getsource(DSD._collect_triage_verdicts)
    assert r"\bKEEP\b" in src, "collector must match an explicit KEEP token"
    for other in ("RETIRE", "REVISIT", "DEFER"):
        assert r"\b" + other + r"\b" not in src


# -- 7. No silent caps — every suppression is auditable ---------------------
def test_suppression_is_reported_not_invisible(cli):
    """A suppressed flag must remain visible in the record, so the audit
    reports `verdict_suppressed_count` rather than silently shrinking."""
    dates, forged = _cold_fixture()
    r = _classify(dates, forged, _strategy(cli), _verdict(days_ago=30))
    assert "verdict_suppressed" in r
    assert "g-115-3084" in r["verdict_suppressed"]["reason"]
    assert "rb-3132" in r["verdict_suppressed"]["reason"]


# -- 8. Key ORDER is load-bearing for byte-compat ---------------------------
# json.dumps preserves insertion order and the byte-compat tests compare
# rendered bytes, so verdict_suppressed must sit between action_required and
# triage_hints exactly as the CLI emits it. Pinned here because a reordering
# would break byte-compat with a diff that points at the whole record rather
# than at the moved key.
def test_verdict_suppressed_key_position_matches_cli(cli):
    dates, forged = _cold_fixture()
    strat = _strategy(cli)
    daemon_keys = list(_classify(dates, forged, strat).keys())
    cli_keys = list(cli.classify(
        skill_name="probe-skill",
        forged_info={"forged_date": (NOW - timedelta(days=forged)).isoformat()},
        quality_data={}, relations_data={},
        journal_dates={"probe-skill": list(dates)},
        companion_dates={}, ledger_dates={}, strategy=strat,
        now=NOW, verdicts=None).keys())
    assert daemon_keys == cli_keys
    i = daemon_keys.index("verdict_suppressed")
    assert daemon_keys[i - 1] == "action_required"
    assert daemon_keys[i + 1] == "triage_hints"
