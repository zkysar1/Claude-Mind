"""Tests for the medium-tier precheck battery and the execution discriminator
it writes (g-115-7847).

The centrepiece is test_the_discriminator_is_not_vacuous, which is guard-5163's
mandatory fixture pair, not a nice-to-have. That guardrail exists because the
failure mode here is self-concealing: a discriminator added in good faith, wired,
documented and committed, that happens to take the SAME value on both branches --
so every future reader sees an instrumented counter and stops looking. The only
thing that separates a real discriminator from decoration is a fixture, and it
must drive the REAL bash meter, because a mocked meter would be testing this
test's idea of the meter rather than the meter.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_helpers import BASH  # guard-580: never a bare "bash" argv[0]

_SCRIPTS = Path(__file__).resolve().parent.parent
_METER = _SCRIPTS / "aspirations-precheck-budget-meter.sh"


def _load(stem, filename):
    spec = importlib.util.spec_from_file_location(stem, _SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(_SCRIPTS))
    spec.loader.exec_module(mod)
    return mod


mb = _load("precheck_medium_battery", "precheck-medium-battery.py")


# ── the meter harness ────────────────────────────────────────────────────────

def _meter(agent_dir, *args):
    """Drive the real meter with AGENT_DIR pointed at a tmp dir.

    MIND_AGENT_DIR is _paths.sh's documented test-only override seam ("UNSET in
    production"), which is what keeps this fixture from writing into the live
    agents/ tree.
    """
    env = dict(os.environ)
    env["MIND_AGENT_DIR"] = str(agent_dir)
    env.pop("BODY_ROLE", None)
    r = subprocess.run(
        # guard-581: .as_posix(), never str(Path) — bash silently strips the
        # backslashes of a str(WindowsPath).
        [BASH, _METER.as_posix(), *args],
        capture_output=True, text=True, timeout=120, env=env,
        cwd=str(_SCRIPTS.parent.parent),
    )
    return r.returncode, r.stdout.strip()


def _summary_rows(agent_dir):
    log = Path(agent_dir) / "session" / "precheck-drops.jsonl"
    if not log.is_file():
        return []
    return [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip() and '"precheck-end"' in line
    ]


@pytest.fixture
def agent_dir(tmp_path):
    d = tmp_path / "agent"
    (d / "session").mkdir(parents=True)
    return d


# ── guard-5163: THE VACUITY TEST ─────────────────────────────────────────────

def test_the_discriminator_is_not_vacuous(agent_dir, tmp_path):
    """The two states `sweeps_ran` genuinely conflates must differ in the new field.

    Both branches model a real iteration in which the always-run tier metered
    normally. They differ ONLY in what the medium tier did:

      (i)  the lanes EXECUTED but never asked the meter for permission -- the
           hand-run case alpha measured on cc-04 ("five medium/deferrable lanes
           were run by hand and the very next meter row still read sweeps_ran=6")
      (ii) the lanes were NEVER INVOKED -- the 94.3h/208h dark window

    `sweeps_ran` cannot tell these apart, because it counts permission decisions
    and neither branch asked for one. That is the ambiguity. `tail_executed`
    counts completions reported by whatever actually ran the lane, so it must.
    """
    a_dir = agent_dir
    b_dir = tmp_path / "agent_b"
    (b_dir / "session").mkdir(parents=True)

    always_run = (
        "tree-debt-gate", "experience-archival-gate", "evolution-finalize-gate",
        "fresh-eyes-code-gate", "inbox-alert-age-check", "handoff-aging-check",
    )
    tail = ("defer-recheck", "precondition-defer-recheck", "blocker-recheck")

    # ── branch (i): tail EXECUTED, never checked ──
    _meter(a_dir, "start")
    for lane in always_run:
        _meter(a_dir, "check", lane)
    for lane in tail:
        _meter(a_dir, "executed", lane)      # ran; nobody asked permission
    _meter(a_dir, "end")

    # ── branch (ii): tail NEVER INVOKED ──
    _meter(b_dir, "start")
    for lane in always_run:
        _meter(b_dir, "check", lane)
    _meter(b_dir, "end")

    ran, never = _summary_rows(a_dir), _summary_rows(b_dir)
    assert len(ran) == 1 and len(never) == 1, (ran, never)
    ran, never = ran[0], never[0]

    # The old counter is BLIND to the difference -- this is the defect, asserted
    # rather than described. If this ever fails, the conflation is gone and the
    # discriminator may no longer be needed.
    assert ran["sweeps_ran"] == never["sweeps_ran"] == len(always_run)
    assert ran["always_run_count"] == never["always_run_count"] == len(always_run)
    assert ran["sweeps_dropped"] == never["sweeps_dropped"] == 0

    # The new field is NOT blind to it.
    assert ran["tail_executed"] == len(tail)
    assert never["tail_executed"] == 0
    assert ran["tail_executed"] != never["tail_executed"]


def test_executed_does_not_disturb_the_old_counters(agent_dir):
    """A reader comparing pre-fix rows to post-fix rows must see `sweeps_ran`
    unchanged. An `executed` marker carries decision='executed', so the run/drop
    sums cannot see it -- backward compatibility is a property of the encoding,
    and this pins it."""
    _meter(agent_dir, "start")
    _meter(agent_dir, "check", "tree-debt-gate")
    for _ in range(5):
        _meter(agent_dir, "executed", "defer-recheck")
    _meter(agent_dir, "end")

    row = _summary_rows(agent_dir)[0]
    assert row["sweeps_ran"] == 1
    assert row["sweeps_dropped"] == 0
    assert row["sweeps_executed"] == 5
    assert row["tail_executed"] == 5


def test_always_run_executions_are_excluded_from_tail_executed(agent_dir):
    """`tail_executed` answers "did the MEDIUM/DEFERRABLE tail happen?". An
    always-run lane reporting execution must not inflate it, or the field would
    read healthy on exactly the dark iterations it exists to expose."""
    _meter(agent_dir, "start")
    _meter(agent_dir, "executed", "tree-debt-gate")
    _meter(agent_dir, "executed", "handoff-aging-check")
    _meter(agent_dir, "executed", "defer-recheck")
    _meter(agent_dir, "end")

    row = _summary_rows(agent_dir)[0]
    assert row["sweeps_executed"] == 3
    assert row["tail_executed"] == 1


def test_executed_is_fail_open_with_no_session(agent_dir):
    """No `meter start` means no state file. The lane has ALREADY done its work by
    the time it reports, so the only correct failure is a silent one -- never a
    non-zero that propagates into the battery."""
    rc, _ = _meter(agent_dir, "executed", "defer-recheck")
    assert rc == 0
    assert _summary_rows(agent_dir) == []


# ── the battery ──────────────────────────────────────────────────────────────

def test_every_lane_resolves_to_the_medium_tier_in_the_meter():
    """A meter_name the meter does not recognise WARN-defaults to `medium`, which
    would be silently correct here and catastrophically wrong in the always-run
    battery (g-115-3124). Assert each name is a REAL medium arm, so the battery
    never rides the default."""
    src = _METER.read_text(encoding="utf-8")
    # Slice the MEDIUM arm exactly: between the always-run arm's echo and the
    # medium arm's own. A looser "is the name anywhere in the file" check would
    # pass for a name registered in the always-run arm, which is the one mistake
    # that actually matters here.
    after_always = src.split('echo "always-run" ;;', 1)[1]
    medium_arm = after_always.split('echo "medium" ;;', 1)[0]
    assert "aspirations-recover-recurring" in medium_arm, (
        "the arm slice is wrong — fix the test before trusting it"
    )
    for lane in mb.LANES:
        assert lane["meter_name"] in medium_arm, (
            f'{lane["meter_name"]} is not in sweep_tier()\'s medium arm — it would '
            f"WARN-default instead of being registered"
        )

    # Negative control: a name that must NOT be in the medium arm. Without it,
    # a slice that accidentally spanned the whole file would pass every assert
    # above and this test would be decoration (the guard-5163 shape, applied to
    # the test rather than to the field).
    assert "tree-debt-gate" not in medium_arm


def test_a_dropped_lane_is_reported_and_not_run():
    """The meter CAN drop a medium lane (zone_drop_rules). Honoring the drop is
    only half — a drop that vanishes from the report is guard-4093's collapse."""
    seen = []

    def runner(argv, timeout):
        seen.append(argv)
        if argv[0].endswith("budget-meter.sh") and argv[1] == "check":
            return 0, "drop", None
        return 0, "{}", None

    rep = {}
    mb._emit = lambda r, j: rep.update(r)
    mb.run(as_json=True, apply=False, lane_runner=runner)

    assert len(rep["dropped"]) == len(mb.LANES)
    assert rep["executed"] == []
    assert rep["completeness"] == "partial"   # dropped => we did not look
    assert not any("defer-recheck.sh" in a[0] for a in seen)


def test_unparseable_lane_output_is_blind_never_clean():
    def runner(argv, timeout):
        if argv[0].endswith("budget-meter.sh"):
            return 0, "run", None
        return 0, "not json at all", None

    rep = {}
    mb._emit = lambda r, j: rep.update(r)
    mb.run(as_json=True, apply=False, lane_runner=runner)

    assert rep["blind"], "unparseable output must be BLIND"
    assert rep["completeness"] == "partial"
    assert rep["executed"] == [], "a lane we could not parse did not execute"


def test_execution_is_recorded_only_on_the_success_path():
    """A blind lane must not emit an `executed` record. A witness that fires for
    work that did not happen is exactly the decoration guard-5163 refuses."""
    calls = []

    def runner(argv, timeout):
        if argv[0].endswith("budget-meter.sh"):
            calls.append(argv[1])
            return 0, "run", None
        return None, "", "timeout"          # every lane blind

    rep = {}
    mb._emit = lambda r, j: rep.update(r)
    mb.run(as_json=True, apply=False, lane_runner=runner)

    assert "executed" not in calls
    assert rep["executed"] == []


def test_worker_body_runs_lanes_but_writes_no_meter_records(monkeypatch):
    """iteration-open._meter's ruling, inherited: the LANES are safe from either
    role, the agent-wide meter write is not."""
    monkeypatch.setenv("BODY_ROLE", "worker")
    calls = []

    def runner(argv, timeout):
        calls.append(argv[0])
        return 0, "{}", None

    rep = {}
    mb._emit = lambda r, j: rep.update(r)
    mb.run(as_json=True, apply=False, lane_runner=runner)

    assert not any("budget-meter" in c for c in calls), "worker wrote the meter"
    assert any("defer-recheck.sh" in c for c in calls), "worker skipped the lanes"
    assert rep["executed"], "a worker still executes and reports its own lanes"


def test_apply_reaches_only_the_lanes_that_take_it():
    seen = []

    def runner(argv, timeout):
        if not argv[0].endswith("budget-meter.sh"):
            seen.append(argv)
        return 0, "{}", None

    mb._emit = lambda r, j: None
    mb.run(as_json=True, apply=True, lane_runner=runner)

    by_script = {a[0]: a for a in seen}
    for lane in mb.LANES:
        argv = by_script[lane["script"]]
        assert ("--apply" in argv) == lane["apply_flag"], lane["name"]


def test_recover_recurring_runs_both_sources_under_one_meter_decision():
    """The tier table calls this lane twice (world, agent). Two registry rows would
    double-count it in lanes_registered and meter it twice under one name."""
    lanes, checks = [], []

    def runner(argv, timeout):
        if argv[0].endswith("budget-meter.sh"):
            if argv[1] == "check":
                checks.append(argv[2])
            return 0, "run", None
        lanes.append(argv)
        return 0, "{}", None

    mb._emit = lambda r, j: None
    mb.run(as_json=True, apply=False, lane_runner=runner)

    recover = [a for a in lanes if a[0] == "aspirations-recover-recurring.sh"]
    assert len(recover) == 2
    assert {a[-1] for a in recover} == {"world", "agent"}
    assert checks.count("aspirations-recover-recurring") == 1


def test_blocker_age_is_read_from_config_not_pinned(tmp_path, monkeypatch):
    """The tier table documents this lane as
    `--max-age-hours <config.proactive_escalation.blocker_age_hours>`. A pinned
    number diverges silently the moment anyone tunes the config, and it already
    did in the direction that hurts: the first draft of this battery hardcoded 24
    while the config said 2, which would have re-checked only blockers twelve
    times older than intended while every doc still claimed 2.
    """
    assert mb._blocker_age_hours() == "2", "live config is 2"

    seen = []

    def runner(argv, timeout):
        if not argv[0].endswith("budget-meter.sh"):
            seen.append(argv)
        return 0, "{}", None

    mb._emit = lambda r, j: None
    monkeypatch.setattr(mb, "_blocker_age_hours", lambda: "37")
    mb.run(as_json=True, apply=True, lane_runner=runner)

    argv = [a for a in seen if a[0] == "blocker-recheck.sh"][0]
    assert "37" in argv, f"config value did not reach the lane: {argv}"


def test_blocker_age_falls_back_rather_than_raising(monkeypatch):
    """This resolves at loop entry. A malformed config must degrade, never raise."""
    monkeypatch.setattr(
        mb, "PROJECT_ROOT", Path("/nonexistent-project-root-for-this-test")
    )
    assert mb._blocker_age_hours() == "2"
