""" — the third verdict: `evaluable` separates "measured and unmet"
from "could not evaluate at all".

Before this field, predicate.py returned `passed: False` for BOTH, so a
precondition whose command was not in ALLOWED_COMMAND_PREFIXES was
indistinguishable at every consumer from one that ran and failed. The first is
transient and self-clears; the second is PERMANENT — no world change ever flips
it, and both re-probe sweeps re-derive the same false every 2h forever.
Measured 2026-09-05: 3 live goals (2 HIGH, one in the standing strategic-focus
lane) were frozen out of every selector that way.

The two directions are tested as a PAIR on purpose. Flagging the unevaluable
case is worthless if a genuinely-unmet condition also starts reading
unevaluable — that would launder real failures into "probably a config bug",
which is this same conflation pointed the other way.
"""
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import predicate as P  # noqa: E402


def _bash():
    from _runtime_bash import BASH  # guard-580: never a bare "bash" argv
    return BASH


# --------------------------------------------------------------------------
# check 3 — a non-allowlisted command yields the unevaluable verdict
# --------------------------------------------------------------------------

def test_non_allowlisted_command_is_unevaluable():
    r = P.evaluate({"type": "command_succeeds", "id": "pc1",
                    "command": "curl -sf --max-time 10 http://example.invalid/status"})
    assert r.passed is False, "fail direction must be unchanged (fail closed)"
    assert r.evaluable is False, "an allowlist refusal never measured anything"
    assert "allowlist" in r.reason


def test_non_allowlisted_metric_threshold_is_unevaluable():
    r = P.evaluate({"type": "metric_threshold", "id": "pc2", "min": 1,
                    "command": "gh api repos/acme/widget/commits --jq length"})
    assert r.passed is False
    assert r.evaluable is False
    assert "allowlist" in r.reason


def test_unevaluable_survives_to_dict():
    """Consumers read the JSON, not the dataclass."""
    d = P.evaluate({"type": "command_succeeds", "id": "pc3",
                    "command": "sudo rm -rf /"}).to_dict()
    assert d["evaluable"] is False
    assert d["passed"] is False


# --------------------------------------------------------------------------
# check 4 — a genuinely-unmet ALLOWLISTED condition still yields a bare
#           passed:false (no conflation in the other direction)
# --------------------------------------------------------------------------

def test_allowlisted_command_that_fails_stays_evaluable():
    r = P.evaluate({"type": "command_succeeds", "id": "pc4",
                    "command": "bash core/scripts/definitely-not-a-real-script-xyz.sh"})
    assert r.passed is False
    assert r.evaluable is True, (
        "the command RAN and returned non-zero — that is a measurement of a "
        "transient world and it self-clears; calling it unevaluable would "
        "launder a real failure")
    assert r.observed_value is not None, "an exit code was actually observed"


def test_allowlisted_command_that_passes_is_evaluable_and_passed():
    r = P.evaluate({"type": "command_succeeds", "id": "pc5",
                    "command": "bash core/scripts/commons-policy-check.sh"})
    assert r.evaluable is True
    assert r.passed is True


def test_measured_false_after_time_stays_evaluable():
    """A non-command predicate that measured the world and said no."""
    r = P.evaluate({"type": "after_time", "id": "pc6",
                    "anchor": "2099-01-01T00:00:00", "delay_seconds": 0})
    assert r.passed is False
    assert r.evaluable is True


# --------------------------------------------------------------------------
# the rest of the cannot-evaluate family
# --------------------------------------------------------------------------

@pytest.mark.parametrize("pred", [
    {"type": "nonexistent_type", "id": "u1"},
    {"type": "command_succeeds", "id": "u2"},                       # no command
    {"type": "metric_threshold", "id": "u3",
     "command": "bash core/scripts/x.sh"},                          # no min/max
    {"type": "metric_threshold", "id": "u4", "min": 1,
     "command": "bash core/scripts/x.sh", "extract": "bogus_mode"},
    {"type": "file_check", "id": "u5"},                             # no path
    {"type": "file_check", "id": "u6", "path": "x", "condition": "bogus"},
    {"type": "after_time", "id": "u7"},                             # no anchor
    {"type": "after_time", "id": "u8", "anchor": "not-a-timestamp",
     "delay_seconds": 0},
    {"type": "file_exists_after", "id": "u9"},                      # no path/after_ref
    {"type": "goal_completed_after", "id": "u10"},                  # no goal_id/after_ref
    {"type": "vcs_commits_since", "id": "u11"},                     # no cutoff spec
    {"type": "pr_merged", "id": "u12", "pr": 1},                    # no repo
    {"type": "pr_merged", "id": "u13", "repo": "acme/widget"},      # no pr
])
def test_static_validation_failures_are_unevaluable(pred):
    r = P.evaluate(pred)
    assert r.passed is False
    assert r.evaluable is False, f"{pred} was never measured; reason={r.reason!r}"


def test_non_dict_predicate_is_unevaluable():
    r = P.evaluate("not a dict")
    assert r.passed is False and r.evaluable is False


def test_evaluator_exception_is_unevaluable(monkeypatch):
    def boom(_p):
        raise RuntimeError("kaboom")
    monkeypatch.setitem(P.PREDICATE_TYPES, "after_time", boom)
    r = P.evaluate({"type": "after_time", "id": "e1"})
    assert r.passed is False and r.evaluable is False
    assert "evaluator error" in r.reason


# --------------------------------------------------------------------------
# back-compat: the field defaults to True, so every consumer that reads only
# `passed` behaves exactly as it did before the field existed (guard-3328)
# --------------------------------------------------------------------------

def test_evaluable_defaults_true():
    assert P.PredicateResult(True, "x").evaluable is True
    assert P.PredicateResult(False, "x").evaluable is True


def test_positional_construction_still_binds_the_same_fields():
    """`evaluable` was inserted BEFORE evaluated_at but AFTER reason, so the
    3-positional form used throughout predicate.py keeps its meaning."""
    r = P.PredicateResult(False, "some_type", "pid-1")
    assert (r.passed, r.type, r.predicate_id) == (False, "some_type", "pid-1")
    assert r.evaluable is True


# --------------------------------------------------------------------------
# outcome 3 + the sanctioned remedy for a refused command
# --------------------------------------------------------------------------

def test_allowlist_is_not_widened():
    """The allowlist is a safety boundary; the remedy for a refused command is
    a `bash core/scripts/<wrapper>.sh`, never a new prefix (guard-5859)."""
    for banned in ("gh ", "curl ", "py -3 -c", "python -c", "sudo", "sh -c"):
        assert not any(p.startswith(banned) for p in P.ALLOWED_COMMAND_PREFIXES), \
            f"{banned!r} must not be an allowlist prefix"


def test_the_three_sanctioned_wrappers_are_allowlisted():
    for cmd in ("bash core/scripts/http-probe.sh http://h/p --timeout 10",
                "bash core/scripts/commons-policy-check.sh --permits-egress",
                "bash core/scripts/gh-commit-landed.sh acme/widget main abc1234"):
        assert P._command_allowed(cmd), cmd


@pytest.mark.parametrize("script", ["http-probe.sh", "commons-policy-check.sh",
                                    "gh-commit-landed.sh"])
def test_wrapper_scripts_parse(script):
    path = SCRIPTS / script
    assert path.exists(), f"{script} missing"
    r = subprocess.run([str(_bash()), "-n", path.as_posix()],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("script,args,expect_rc", [
    # usage errors are rc=2 and must NEVER read as a passing precondition
    ("http-probe.sh", [], 2),
    ("gh-commit-landed.sh", [], 2),
    ("gh-commit-landed.sh", ["not-owner-slash-name", "main", "abc"], 2),
])
def test_wrapper_usage_errors_never_exit_zero(script, args, expect_rc):
    r = subprocess.run([str(_bash()), (SCRIPTS / script).as_posix(), *args],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == expect_rc, (r.returncode, r.stdout, r.stderr)


def test_commons_policy_wrapper_reports_a_resolved_policy():
    """The gate form must MEASURE the dial, not fail to resolve it. This is the
    portability half: `PYTHONPATH=/c/...` (an MSYS path) reaches native python
    verbatim under the environment predicate-eval.sh builds and dies
    ModuleNotFoundError, while the same script from an interactive shell is
    fine — so assert on the resolved verdict, not merely on a clean exit."""
    r = subprocess.run([str(_bash()),
                        (SCRIPTS / "commons-policy-check.sh").as_posix()],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "VERDICT=resolved" in r.stdout, r.stdout
    assert "policy=" in r.stdout
