#!/usr/bin/env python3
"""Tests for the close-review risk-tier classifier and gate ().

Covers the goal's four verification outcomes:
  1. classifier unit tests over ALL tier-2 trigger conditions
  2. the gate REFUSES a tier-2 close with no APPROVE verdict artifact
  3. the override path writes to the per-gate ledger
  4. a tier-0 recurring routine sweep costs nothing (no review demanded)

The gate is exercised as a SUBPROCESS with --goal-json, not by importing main().
That is deliberate: iteration-close.sh invokes it as a subprocess and reads its rc,
so the rc contract is the thing under test — importing would test a different
surface than production uses (probe-with-canonical-code-path.md, "canonical BINARY
is not canonical INVOCATION").
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

from goal_close_risk_tier import (  # noqa: E402
    classify,
    count_named_entities,
    touches_framework,
)

GATE = SCRIPTS / "close-review-gate.py"


def _goal(**kw):
    base = {"goal_id": "g-999-01", "title": "t", "description": "d",
            "priority": "MEDIUM", "participants": ["agent"]}
    base.update(kw)
    return base


# ─── 1. classifier: every tier-2 trigger, one test each ────────────────────

def test_trigger_entities_three_distinct_ids():
    g = _goal(description="fix g-115-1 alongside guard-22 and rb-333")
    r = classify(g)
    assert r["tier"] == 2
    assert r["triggers"]["entities"] is True


def test_entities_counts_DISTINCT_not_total():
    """A description repeating ONE id eight times names one thing to check."""
    assert count_named_entities("g-115-1 " * 8) == 1
    r = classify(_goal(description="g-115-1 " * 8))
    assert r["triggers"]["entities"] is False


def test_trigger_user_truth_via_participants():
    r = classify(_goal(participants=["agent", "user"]))
    assert r["tier"] == 2 and r["triggers"]["user_truth"] is True


def test_trigger_user_truth_via_directive_text():
    r = classify(_goal(description="User directive 2026-08-31: review before close"))
    assert r["tier"] == 2 and r["triggers"]["user_truth"] is True


def test_trigger_deliverable():
    r = classify(_goal(description="produces a new tree node for the reader"))
    assert r["tier"] == 2 and r["triggers"]["deliverable"] is True


def test_trigger_framework_files():
    r = classify(_goal(), files_touched=["core/scripts/x.py"])
    assert r["tier"] == 2 and r["triggers"]["framework"] is True
    r2 = classify(_goal(), files_touched=[".claude/rules/y.md"])
    assert r2["triggers"]["framework"] is True


def test_trigger_high_priority_non_recurring():
    r = classify(_goal(priority="HIGH"))
    assert r["tier"] == 2 and r["triggers"]["high_prio"] is True


def test_high_priority_RECURRING_does_not_fire_high_prio():
    """The trigger is HIGH *non-recurring*; a HIGH recurring sweep is not tier-2 by
    priority alone, or every daily HIGH sweep would demand a review artifact."""
    r = classify(_goal(priority="HIGH", recurring=True, outcome_class="deep"))
    assert r["triggers"]["high_prio"] is False


def test_trigger_first_of_aspiration():
    r = classify(_goal(), is_first_of_aspiration=True)
    assert r["tier"] == 2 and r["triggers"]["first_of_asp"] is True


def test_touches_framework_handles_backslashes_and_dot_slash():
    assert touches_framework(["core\\scripts\\a.py"]) is True
    assert touches_framework(["./core/scripts/a.py"]) is True
    assert touches_framework(["world/scripts/a.py"]) is False
    assert touches_framework(None) is False


# ─── 4. tier 0 — the zero-cost path ────────────────────────────────────────

def test_tier0_recurring_routine_no_artifacts():
    r = classify(_goal(recurring=True, outcome_class="routine"), artifacts_count=0)
    assert r["tier"] == 0


def test_tier0_SHORT_CIRCUITS_over_framework_touch():
    """A recurring routine sweep that touches a framework file stays tier 0 —
    otherwise every recurring framework-hygiene goal would stall the cadence."""
    r = classify(_goal(recurring=True, outcome_class="routine", priority="HIGH"),
                 files_touched=["core/scripts/x.py"], artifacts_count=0)
    assert r["tier"] == 0


def test_recurring_routine_WITH_artifacts_is_not_tier0():
    r = classify(_goal(recurring=True, outcome_class="routine"), artifacts_count=3)
    assert r["tier"] != 0


# ─── tier 1 default + fail-to-tier-1 ───────────────────────────────────────

def test_plain_goal_is_tier1():
    assert classify(_goal())["tier"] == 1


def test_bad_input_fails_to_tier1_never_tier2():
    """guard-142: the classifier must never fail CLOSED on its own bad input."""
    for bad in (None, "not a dict", 42, []):
        assert classify(bad)["tier"] == 1


# ─── 2 + 3. the gate's rc contract, as a subprocess ────────────────────────

def _run_gate(goal, tmp_path, env_extra=None, extra_args=()):
    gj = tmp_path / "goal.json"
    gj.write_text(json.dumps(goal), encoding="utf-8")
    env = dict(os.environ)
    env["STORAGE_BACKEND"] = "local"          # guard-955
    # Redirect the override audit ledger into tmp. Without this the two
    # override tests below APPEND TO THE PRODUCTION LEDGER — measured, 20
    #  / agent "nobody" rows in world/close-review-overrides.jsonl
    # (). The override RATE read off that file is the documented
    # precondition for enabling check B, so test rows corrupt the measurement
    # that decides whether this gate ships.
    # Set HERE, at the one chokepoint every subprocess test goes through,
    # rather than in an autouse fixture: an always-setting fixture would leave
    # the DEFAULT (env unset -> WORLD_DIR) branch untested, which is the branch
    # production takes (guard-1482). test_ledger_DEFAULTS_to_world_dir covers it.
    env["CLOSE_REVIEW_LEDGER_DIR"] = str(tmp_path)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(GATE), "--goal", goal.get("goal_id", "g-999-01"),
         "--goal-json", str(gj), *extra_args],
        capture_output=True, text=True, env=env, timeout=120,
    )


def test_gate_is_DORMANT_by_default(tmp_path):
    """Ship default: both flags off -> noop, rc 0, whatever the goal looks like."""
    r = _run_gate(_goal(priority="HIGH"), tmp_path)
    assert r.returncode == 0
    assert '"decision": "noop"' in r.stdout


def test_gate_REFUSES_tier2_without_verdict(tmp_path):
    r = _run_gate(_goal(priority="HIGH"), tmp_path,
                  {"CLOSE_REVIEW_GATE_ENABLED": "1", "MIND_AGENT": "nobody"})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "REFUSED" in r.stderr
    assert "high_prio" in r.stderr          # the trigger is named
    assert "--override-close-review" in r.stderr   # the remedy is named


def test_gate_PASSES_tier1_when_enabled(tmp_path):
    r = _run_gate(_goal(), tmp_path,
                  {"CLOSE_REVIEW_GATE_ENABLED": "1", "MIND_AGENT": "nobody"})
    assert r.returncode == 0
    assert '"decision": "pass"' in r.stdout


def test_gate_tier0_costs_nothing_when_enabled(tmp_path):
    """Outcome 4: a recurring routine sweep closes with zero added review cost."""
    r = _run_gate(_goal(recurring=True, outcome_class="routine"), tmp_path,
                  {"CLOSE_REVIEW_GATE_ENABLED": "1", "MIND_AGENT": "nobody"},
                  extra_args=("--artifacts-count", "0"))
    assert r.returncode == 0
    assert '"tier": 0' in r.stdout


def test_gate_override_passes_and_is_recorded(tmp_path):
    """Outcome 3: the override turns a BLOCK into a logged pass."""
    r = _run_gate(_goal(priority="HIGH"), tmp_path,
                  {"CLOSE_REVIEW_GATE_ENABLED": "1", "MIND_AGENT": "nobody"},
                  extra_args=("--override-close-review", "test justification"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert '"decision": "override"' in r.stdout


def test_gate_accepts_APPROVE_verdict_artifact(tmp_path):
    """POSITIVE CONTROL for the refusal test: the same tier-2 goal, but with an
    APPROVE artifact on disk, passes. Without this the refusal test cannot
    distinguish 'the gate reads the verdict' from 'the gate always blocks tier 2'
    — the two are indistinguishable from a single red result (guard-2298).

    Writes through the REAL production path resolution — which since g-357-41 is
    WORLD-scoped and GOAL-keyed (audit-reports/close-reviews/<goal-id>.json under
    the same CLOSE_REVIEW_LEDGER_DIR root _run_gate already isolates), not the
    closing agent's private dir. `reviewer` is deliberately NOT the closing agent:
    a self-approval no longer satisfies the gate.

    Note this version creates NO real agent directory. The previous one wrote
    into agent_dir("pytest-throwaway") and rmtree'd it in a finally — a live
    write outside tmp whose cleanup was the only thing standing between the
    suite and a stray agent dir. The world-scoped path removes that exposure
    rather than guarding it."""
    d = tmp_path / "audit-reports" / "close-reviews"
    d.mkdir(parents=True, exist_ok=True)
    (d / "g-999-01.json").write_text(json.dumps({
        "verdict": "APPROVE", "reviewer": "test-reviewer",
        "checks": ["criteria re-read", "entities spot-checked"],
    }), encoding="utf-8")
    r = _run_gate(_goal(priority="HIGH"), tmp_path,
                  {"CLOSE_REVIEW_GATE_ENABLED": "1",
                   "MIND_AGENT": "pytest-throwaway"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert '"decision": "pass"' in r.stdout
    assert "test-reviewer" in r.stdout


def test_note_marker_refusal_names_the_matched_context(tmp_path):
    """Check B: a HIGH-confidence not-done marker in the goal's own note refuses,
    and prints the match so the reader can judge it (never a silent hard deny).

    HIGH requires an UNQUOTED *strong* marker in the note's first 300 chars. The
    shipped strong set is REOPEN(ED|ING) / do-not-close / criteria-unmet — NOT
    "REVERTED" or "REVIEWED-NOT-CLOSED", which g-357-40's description names. That
    gap is why this gate REUSES closed_against_own_note instead of re-deriving the
    marker list from the goal's prose: a second copy would have shipped markers the
    detector does not have and silently agreed with itself."""
    g = _goal(outcome_note="REOPENED BY ITS OWN CRITERIA - do not re-close on a diagnosis.")
    r = _run_gate(g, tmp_path,
                  {"CLOSE_REVIEW_NOTE_MARKER_ENABLED": "1", "MIND_AGENT": "nobody"})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "REFUSED" in r.stderr
    assert "--override-note-marker" in r.stderr


def test_note_marker_QUOTED_does_not_refuse(tmp_path):
    """The false positive that matters: a note routinely QUOTES a not-done phrase
    while asserting the opposite. Quoted markers never reach HIGH, so this passes —
    the positive control for the refusal above."""
    g = _goal(outcome_note='SUPERSEDES the prior note, which ended "REOPENED". '
                           'That is no longer true; the work landed.')
    r = _run_gate(g, tmp_path,
                  {"CLOSE_REVIEW_NOTE_MARKER_ENABLED": "1", "MIND_AGENT": "nobody"})
    assert r.returncode == 0, r.stdout + r.stderr


def test_note_marker_override_passes(tmp_path):
    g = _goal(outcome_note="REOPENED BY ITS OWN CRITERIA - do not re-close on a diagnosis.")
    r = _run_gate(g, tmp_path,
                  {"CLOSE_REVIEW_NOTE_MARKER_ENABLED": "1", "MIND_AGENT": "nobody"},
                  extra_args=("--override-note-marker", "marker refuted in body"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert '"decision": "override"' in r.stdout


# ─── the store-read argv shape (regression pin) ────────────────────────────

def test_load_goal_passes_the_script_as_bash_cmds_FIRST_POSITIONAL(monkeypatch):
    """`bash_cmd(script, *args)` — a LIST as the first arg makes Path(list).as_posix()
    raise, load_goal's except swallows it, and the gate reports 'goal record
    unavailable' and FAILS OPEN on every close.

    Shipped exactly that way and it was invisible: a broken call and a genuinely
    absent goal produce byte-identical output at the call site, and every unit test
    here passes --goal-json so none of them exercise the store path at all. Caught
    only by running the wrapper against a real goal id and noticing that a goal
    which obviously exists came back degraded. Pinned hermetically here so it
    cannot come back (guard-1404: a failed call that renders as a benign result)."""
    # The script name is hyphenated, so it is not importable by name.
    import importlib.util
    spec = importlib.util.spec_from_file_location("_crg_under_test", GATE)
    _crg_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(_crg_mod)
    seen = {}

    def _spy(script, *args):
        seen["script"] = script
        seen["args"] = args
        return ["/bin/true"]

    monkeypatch.setattr(_crg_mod, "bash_cmd", _spy)
    _crg_mod.load_goal("g-999-01", "world")

    assert "script" in seen, "load_goal never invoked bash_cmd"
    assert not isinstance(seen["script"], (list, tuple)), (
        f"bash_cmd's first arg must be the script path, not a list of argv; "
        f"got {seen['script']!r}"
    )
    assert "--goal-field" in seen["args"], (
        f"query flags must ride as *args, not be folded into the script arg; "
        f"got args={seen['args']!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Outcome 2 of : "iteration-close.sh do_verify refuses a tier-2 close
# without APPROVE verdict artifact (demonstrated in a test)".
#
# The tests above prove the GATE refuses. That is not the same claim: it says
# nothing about whether do_verify CALLS the gate, forwards the override flags,
# or turns rc=1 into a non-zero return. Those are the wiring, and the wiring is
# where a dormant gate silently becomes a permanent no-op.
#
# The block is EXTRACTED VERBATIM from the production file rather than retyped,
# so this test cannot drift from what iteration-close.sh actually runs — a
# hand-copied fragment keeps passing after the real block changes, which is the
# exact failure a wiring test exists to prevent (guard-920).
# ─────────────────────────────────────────────────────────────────────────────

from _runtime_bash import BASH  # noqa: E402  guard-580: never a bare "bash"

ITERATION_CLOSE = SCRIPTS / "iteration-close.sh"
_BLOCK_HEAD = 'if [[ "$GOAL_STATUS" == "completed" && -f "$SCRIPT_DIR/close-review-gate.py" ]]; then'


def _extract_gate_block() -> str:
    """Pull the close-review gate block out of iteration-close.sh.

    Raises if it is absent — an absent block IS the regression (the gate
    silently unwired), so this must fail loudly rather than skip.
    """
    lines = ITERATION_CLOSE.read_text(encoding="utf-8").splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.strip() == _BLOCK_HEAD]
    assert len(starts) == 1, f"expected exactly 1 gate block, found {len(starts)}"
    start = starts[0]
    indent = len(lines[start]) - len(lines[start].lstrip())
    for j in range(start + 1, len(lines)):
        if lines[j].strip() == "fi" and (len(lines[j]) - len(lines[j].lstrip())) == indent:
            return "\n".join(ln[indent:] for ln in lines[start:j + 1])
    raise AssertionError("gate block has no matching fi")


def _run_block(tmp_path, stub_rc, *, overrides=None, goal_status="completed"):
    """Execute the extracted block against a stub gate. Returns (rc, stderr, argv)."""
    script_dir = tmp_path / "scripts"
    script_dir.mkdir(exist_ok=True)
    argv_log = tmp_path / "argv.txt"
    (script_dir / "close-review-gate.py").write_text(
        "import sys, pathlib\n"
        f"pathlib.Path({str(argv_log)!r}).write_text(repr(sys.argv[1:]))\n"
        f"sys.exit({stub_rc})\n",
        encoding="utf-8",
    )
    core_root = tmp_path / "core"
    (core_root / "logs").mkdir(parents=True, exist_ok=True)

    ov = overrides or {}
    harness = "\n".join([
        "set -u",
        f"SCRIPT_DIR={str(script_dir)!r}",
        f"CORE_ROOT={str(core_root)!r}",
        f'GOAL_STATUS="{goal_status}"',
        'GOAL_ID="g-357-40"',
        'SOURCE="world"',
        f'OVERRIDE_CLOSE_REVIEW="{ov.get("close", "")}"',
        f'OVERRIDE_NOTE_MARKER="{ov.get("note", "")}"',
        "do_verify_frag() {",
        _extract_gate_block(),
        "  return 0",
        "}",
        "do_verify_frag",
    ])
    p = subprocess.run([BASH, "-c", harness], capture_output=True, text=True, timeout=60)
    argv = argv_log.read_text(encoding="utf-8") if argv_log.exists() else None
    return p.returncode, p.stderr, argv


def test_do_verify_REFUSES_when_the_gate_returns_1(tmp_path):
    """Outcome 2: rc=1 from the gate must stop the close, not be swallowed."""
    rc, err, argv = _run_block(tmp_path, 1)
    assert rc == 1, f"do_verify returned {rc}; a refused close must be non-zero.\n{err}"
    assert "REFUSED" in err and "g-357-40" in err, err
    assert argv is not None, "the gate was never invoked — the wiring is dead"


def test_do_verify_PASSES_when_the_gate_returns_0(tmp_path):
    rc, err, _ = _run_block(tmp_path, 0)
    assert rc == 0, f"a passing gate must not block the close: {err}"
    assert "REFUSED" not in err


def test_do_verify_FAILS_OPEN_on_a_gate_fault(tmp_path):
    """guard-142: a gate must never fail closed on its own dependency error."""
    rc, err, _ = _run_block(tmp_path, 3)
    assert rc == 0, f"a gate FAULT (rc=3) must fail open, got rc={rc}: {err}"
    assert "WARN" in err and "NOT checked" in err, err


def test_do_verify_forwards_both_override_flags(tmp_path):
    """A remedy the caller strips is an unreachable remedy (guard-1532)."""
    rc, _, argv = _run_block(tmp_path, 0, overrides={"close": "why-a", "note": "why-b"})
    assert rc == 0
    assert "--override-close-review" in argv and "why-a" in argv, argv
    assert "--override-note-marker" in argv and "why-b" in argv, argv


def test_do_verify_skips_the_gate_for_a_non_completed_goal(tmp_path):
    """A skipped/blocked close pays nothing — the gate is completed-only."""
    rc, err, argv = _run_block(tmp_path, 1, goal_status="skipped")
    assert rc == 0, f"a non-completed close must not reach the gate: {err}"
    assert argv is None, f"gate was invoked for a non-completed goal: {argv}"


def test_ledger_DEFAULTS_to_world_dir(tmp_path, monkeypatch):
    """The env-unset branch — the one production takes (guard-1482).

    _run_gate sets CLOSE_REVIEW_LEDGER_DIR for safety, so without this test the
    default resolution would have zero coverage. WORLD_DIR is patched to tmp so
    asserting the default never appends to the real ledger.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("_crg_ledger_default", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    fake_world = tmp_path / "world"
    fake_world.mkdir()
    monkeypatch.setattr(mod, "WORLD_DIR", fake_world)
    monkeypatch.delenv("CLOSE_REVIEW_LEDGER_DIR", raising=False)

    mod._log_override({"gate": "close-review-gate", "goal_id": "g-000-00"})

    landed = fake_world / "close-review-overrides.jsonl"
    assert landed.is_file(), "default branch did not resolve to WORLD_DIR"
    assert json.loads(landed.read_text(encoding="utf-8").strip())["goal_id"] == "g-000-00"


def test_ledger_env_override_wins_over_world_dir(tmp_path, monkeypatch):
    """The redirect must actually beat WORLD_DIR — otherwise _run_gate's guard is decorative."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_crg_ledger_env", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    fake_world = tmp_path / "world"; fake_world.mkdir()
    redirect = tmp_path / "redirect"; redirect.mkdir()
    monkeypatch.setattr(mod, "WORLD_DIR", fake_world)
    monkeypatch.setenv("CLOSE_REVIEW_LEDGER_DIR", str(redirect))

    mod._log_override({"gate": "close-review-gate", "goal_id": "g-000-01"})

    assert (redirect / "close-review-overrides.jsonl").is_file(), "env override ignored"
    assert not (fake_world / "close-review-overrides.jsonl").exists(), \
        "wrote to WORLD_DIR despite the override — production would still be polluted"
