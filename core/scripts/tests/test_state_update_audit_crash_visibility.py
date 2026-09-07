"""Regression pins for  — two defects, one live close.

DEFECT 1 (the crash). `cmd_backpressure` computed a dead-end candidate's
`value_range` as `[min(failed), max(failed)]`. `failed_values` holds
unconstrained meta values, so an element can be a dict — and a bare `min()`
over dicts raises `TypeError: '<' not supported between instances of 'dict'
and 'dict'`. Observed live on a real close.

The blast radius is wider than the one audit: `cmd_run_all` calls
`r2 = cmd_backpressure(bp_args)` with NO guard, so the raise aborted r3
(temporal-credit) and r4 (relative-advantage) as well. One bad value cost
THREE audits, not one. `test_run_all_continues_past_dict_failed_values`
pins that.

DEFECT 2 (the reporting). `main()` had no try/except, so the raise printed a
traceback and exited 1 — and that 1 COLLIDES with this script's DESIGNED rc=1
(a HARD_FAIL_FLAGS member). `iteration-close.sh` read rc=1, failed to parse
the empty stdout, and announced "audit ran + snapshot recorded: unparsable":
a crash rendered as coverage, with the enclosing phase still returning 0.
Fixing only defect 1 leaves the NEXT crash reading as a pass, so both halves
are pinned here.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS_DIR / "state-update-audit.py"


def _import():
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "state_update_audit_g5086", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["state_update_audit_g5086"] = mod
    spec.loader.exec_module(mod)
    return mod


MOD = _import()

# The shape that crashed: a list of dicts where numbers were expected.
LIVE_CRASH_VALUES = [
    {"scorer": "goal-selector", "weight": 0.40},
    {"scorer": "goal-selector", "weight": 0.55},
]


# -- _numeric_range ----------------------------------------------------------

def test_fixture_really_is_the_crashing_shape():
    """Positive control: without this, every test below could pass vacuously.

    A regression pin whose fixture does not actually reproduce the defect is
    indistinguishable from a pin that never fired. Assert the OLD expression
    still raises on this exact payload.
    """
    with pytest.raises(TypeError):
        min(LIVE_CRASH_VALUES)


def test_numeric_range_dict_values_return_null_range():
    assert MOD._numeric_range(LIVE_CRASH_VALUES) == [None, None]


def test_numeric_range_empty_list():
    # The pre-existing empty case; the null-range shape must stay identical to
    # it so downstream consumers see no new variant.
    assert MOD._numeric_range([]) == [None, None]


def test_numeric_range_all_numeric_is_min_max():
    assert MOD._numeric_range([3, 1.5, 2]) == [1.5, 3]


def test_numeric_range_single_value():
    assert MOD._numeric_range([0.4]) == [0.4, 0.4]


def test_numeric_range_mixed_uses_numbers_only():
    # Non-numeric entries are EXCLUDED, never coerced: coercion would invent an
    # ordering the data does not have (a stringified dict sorts, meaninglessly).
    assert MOD._numeric_range(
        [{"a": 1}, 0.2, "0.9", [1], None, 0.7]) == [0.2, 0.7]


def test_numeric_range_excludes_bool():
    # bool is an int subclass, so True/False would otherwise land in a value
    # range as 1/0 — a coercion artefact, not a measurement.
    assert MOD._numeric_range([True, False]) == [None, None]
    assert MOD._numeric_range([True, 0.5]) == [0.5, 0.5]


# -- cmd_backpressure (canonical code path, real call shape) -----------------

def _bp_payload(failed_values):
    return json.dumps({
        "rollback_actions": [],
        "dead_end_candidates": [{
            "strategy_file": "goal-selection-strategy.yaml",
            "field": "weights.opportunity_boost",
            "failed_values": failed_values,
            "evidence": "test regression",
            "rollback_count": 2,
        }],
        "graduated": [],
    })


def _patched_run(bp_payload, calls):
    """Record every _run argv+stdin; answer the backpressure check."""
    def fake_run(argv, input_text=None, timeout=None):
        calls.append((list(argv), input_text))
        if argv[0] == "meta-backpressure.sh":
            return bp_payload, "", 0
        return "", "", 0
    return fake_run


def _dead_end_adds(calls):
    return [(a, i) for a, i in calls if a[:2] == ["meta-dead-ends.sh", "add"]]


def test_cmd_backpressure_survives_dict_failed_values(monkeypatch):
    calls = []
    monkeypatch.setattr(
        MOD, "_run", _patched_run(_bp_payload(LIVE_CRASH_VALUES), calls))
    result = MOD.cmd_backpressure(argparse.Namespace(learning_value=0.5))
    # The dead-end is still REGISTERED — the guard drops the unusable range,
    # not the record.
    assert result["dead_ends"] == ["weights.opportunity_boost"]
    adds = _dead_end_adds(calls)
    assert len(adds) == 1
    assert json.loads(adds[0][1])["value_range"] == [None, None]


def test_cmd_backpressure_numeric_failed_values_still_ranged(monkeypatch):
    """The guard is surgical: real numeric values still produce a real range."""
    calls = []
    monkeypatch.setattr(
        MOD, "_run", _patched_run(_bp_payload([0.4, 0.55, 0.5]), calls))
    MOD.cmd_backpressure(argparse.Namespace(learning_value=0.5))
    assert json.loads(_dead_end_adds(calls)[0][1])["value_range"] == [0.4, 0.55]


# -- cmd_run_all cascade (the "three audits, not one" harm) ------------------

def test_run_all_continues_past_dict_failed_values(monkeypatch):
    ran = []

    def _velocity(a):
        ran.append("velocity")
        return {"flags": [], "learning_value": 0.5}

    def _temporal(a):
        ran.append("temporal-credit")
        return {"flags": []}

    def _relative(a):
        ran.append("relative-advantage")
        return {"flags": []}

    monkeypatch.setattr(MOD, "cmd_velocity", _velocity)
    monkeypatch.setattr(MOD, "cmd_temporal_credit", _temporal)
    monkeypatch.setattr(MOD, "cmd_relative_advantage", _relative)
    calls = []
    monkeypatch.setattr(
        MOD, "_run", _patched_run(_bp_payload(LIVE_CRASH_VALUES), calls))

    result = MOD.cmd_run_all(argparse.Namespace(
        outcome_class="deep", goal="g-115-5086", category="framework",
        experience_id=None, learning_value=0.5,
    ))

    # r2 raising used to abort r3 and r4. All four must run.
    assert ran == ["velocity", "temporal-credit", "relative-advantage"]
    assert set(result["results"]) == {
        "velocity", "backpressure", "temporal_credit", "relative_advantage"}


# -- main() crash visibility -------------------------------------------------

def _run_main(monkeypatch, capsys, subcommand, fn):
    monkeypatch.setattr(
        MOD, "DISPATCH", dict(MOD.DISPATCH, **{subcommand: fn}))
    monkeypatch.setattr(MOD, "log_script_decision", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["state-update-audit.py", subcommand])
    with pytest.raises(SystemExit) as exc:
        MOD.main()
    return exc.value.code, capsys.readouterr()


def _boom(args):
    raise TypeError(
        "'<' not supported between instances of 'dict' and 'dict'")


def test_main_emits_parseable_json_on_crash(monkeypatch, capsys):
    """The load-bearing assertion: stdout PARSES. Empty stdout was the defect."""
    rc, cap = _run_main(monkeypatch, capsys, "backpressure", _boom)
    payload = json.loads(cap.out)
    assert payload["flags"] == ["check_failed"]
    assert payload["error"]["type"] == "TypeError"
    assert "CRASHED" in payload["summary"]
    assert rc == 1


def test_main_crash_keeps_traceback_on_stderr(monkeypatch, capsys):
    # stdout stays pure JSON for the caller; the diagnostic goes to stderr.
    _rc, cap = _run_main(monkeypatch, capsys, "backpressure", _boom)
    assert "Traceback" in cap.err
    assert "TypeError" in cap.err


def test_crash_flag_is_an_existing_hard_fail_member():
    """rc=1 on a crash is not invented — check_failed is already in the SSOT.

    Reusing it (rather than adding a flag) is what keeps the exit-code contract
    unchanged while the visibility improves.
    """
    assert "check_failed" in MOD.HARD_FAIL_FLAGS
    assert MOD._has_hard_failure(["check_failed"]) is True


def test_main_success_path_unchanged(monkeypatch, capsys):
    rc, cap = _run_main(
        monkeypatch, capsys, "backpressure",
        lambda a: {"subcommand": "backpressure", "summary": "ok", "flags": []})
    assert rc == 0
    assert json.loads(cap.out)["flags"] == []


def test_main_flagged_nonfatal_path_still_exits_zero(monkeypatch, capsys):
    # An informational (non-HARD_FAIL) flag must NOT be dragged to rc=1 by the
    # new except block.
    rc, cap = _run_main(
        monkeypatch, capsys, "backpressure",
        lambda a: {"subcommand": "backpressure",
                   "flags": ["dead_ends_registered"]})
    assert rc == 0
    assert json.loads(cap.out)["flags"] == ["dead_ends_registered"]


# -- iteration-close.sh reporting half ---------------------------------------

ITERATION_CLOSE = SCRIPTS_DIR / "iteration-close.sh"


def test_iteration_close_no_longer_calls_a_crash_a_completed_audit():
    """Source-level pin on the reporting half (a bash branch, no unit harness).

    The exact string below announced EVERY rc=1 — including a crash with empty
    stdout — as a completed, recorded audit. rc=1 means a HARD_FAIL_FLAGS
    member fired, which state-update-audit.py documents as "the audit could
    NOT complete", so the banner asserted the opposite of what it measured.
    """
    text = ITERATION_CLOSE.read_text(encoding="utf-8")
    assert "audit ran + snapshot recorded" not in text


def test_iteration_close_distinguishes_unparsable_from_flagged():
    text = ITERATION_CLOSE.read_text(encoding="utf-8")
    assert "the audit CRASHED" in text
    # Both rc=1 sub-branches must WARN — neither may read as coverage.
    assert "WARN: state-update-audit rc=1 with NON-JSON stdout" in text
    assert "WARN: state-update-audit rc=1 -- hard-fail flag(s)" in text


def test_iteration_close_does_not_mirror_the_hard_fail_flag_set():
    """HARD_FAIL_FLAGS has one owner. A bash copy would drift silently."""
    text = ITERATION_CLOSE.read_text(encoding="utf-8")
    for flag in ("bad_output", "bad_experience_json", "meta_dir_missing",
                 "velocity_yaml_parse_failed"):
        assert flag not in text
