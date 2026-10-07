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

import io
import json
import os
import shlex
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

def _run_gate(goal, tmp_path, env_extra=None, extra_args=(), env_drop=(), stdin_text=None):
    gj = tmp_path / "goal.json"
    gj.write_text(json.dumps(goal), encoding="utf-8")
    env = dict(os.environ)
    for k in env_drop:
        env.pop(k, None)
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
        capture_output=True, text=True, env=env, timeout=120, input=stdin_text,
    )


def _decisions(stdout):
    """(check, decision) for every JSON line the gate printed. With check B on by
    default, a close can print a note-marker line AND a tier line, so a bare
    '"decision": "pass"' substring no longer says which check passed."""
    out = []
    for ln in stdout.splitlines():
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if isinstance(d, dict):
            out.append((d.get("check"), d.get("decision")))
    return out


# THE SHIPPED POSTURE (): check A off, check B on. These tests read the
# REAL core/config/aspirations.yaml, and drop both env overrides first, because an
# override can only switch a flag ON and a set one would hide a wrong shipped value
# (guard-6333). Mutation pins: reverting note_marker_enabled turns only the refusal
# test red; flipping enabled turns only the check-A test red; the clean-note test is
# the control and stays green under both.
_SHIPPED = ("CLOSE_REVIEW_GATE_ENABLED", "CLOSE_REVIEW_NOTE_MARKER_ENABLED")
_NOT_DONE = "REOPENED BY ITS OWN CRITERIA - do not re-close on a diagnosis."


def test_shipped_config_REFUSES_a_HIGH_not_done_note(tmp_path):
    r = _run_gate(_goal(outcome_note=_NOT_DONE), tmp_path, {"MIND_AGENT": "nobody"},
                  env_drop=_SHIPPED)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "REFUSED" in r.stderr
    assert ("note-marker", "block") in _decisions(r.stdout)


def test_shipped_config_passes_a_clean_note(tmp_path):
    """The control for the refusal above: same call, a note that says the work is
    done. It passes whether check B is on or off."""
    r = _run_gate(_goal(outcome_note="All three outcomes MET; sources below."), tmp_path,
                  {"MIND_AGENT": "nobody"}, env_drop=_SHIPPED)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "REFUSED" not in r.stderr


def test_shipped_config_leaves_check_A_off(tmp_path):
    """A tier-2 goal with no verdict closes: check A ships off until the blockers
    recorded in aspirations.yaml land."""
    r = _run_gate(_goal(priority="HIGH"), tmp_path, {"MIND_AGENT": "nobody"},
                  env_drop=_SHIPPED)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not [d for d in _decisions(r.stdout) if d[0] == "tier"]


def test_note_marker_pass_is_logged(tmp_path):
    """A clean note logs a note-marker pass, so a quiet field window reads as
    ran-and-passed (guard-5501). Turned on by env so this pins the emit alone, not
    the shipped flag."""
    r = _run_gate(_goal(outcome_note="All three outcomes MET; sources below."), tmp_path,
                  {"CLOSE_REVIEW_NOTE_MARKER_ENABLED": "1", "MIND_AGENT": "nobody"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert ("note-marker", "pass") in _decisions(r.stdout)


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
    assert ("tier", "pass") in _decisions(r.stdout)


def test_gate_tier0_costs_nothing_when_enabled(tmp_path):
    """Outcome 4: a recurring routine sweep closes with zero added review cost."""
    r = _run_gate(_goal(recurring=True, outcome_class="routine"), tmp_path,
                  {"CLOSE_REVIEW_GATE_ENABLED": "1", "MIND_AGENT": "nobody"},
                  extra_args=("--artifacts-count", "0"))
    assert r.returncode == 0
    assert '"tier": 0' in r.stdout


def test_gate_override_passes_and_is_recorded(tmp_path):
    """Outcome 3: the override turns a BLOCK into a logged pass. Since  it does
    so only where team-state lists no other mind, so the roster here is the closer alone."""
    r = _run_gate(_goal(priority="HIGH"), tmp_path,
                  {"CLOSE_REVIEW_GATE_ENABLED": "1", "MIND_AGENT": "nobody"},
                  extra_args=("--override-close-review", "test justification",
                              "--roster-json", _roster(tmp_path, "nobody")))
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
    assert ("tier", "pass") in _decisions(r.stdout)
    assert "test-reviewer" in r.stdout


# THE POST-HOC LANE (). A closer listed in review_closer_roles or
# review_sampled_roles is reviewed AFTER the close (), so check A passes it and
# names the lane. The role is BODY_ROLE, which bash-agent-inject.py exports only on the
# worker fork path, so unset means reducer-or-unknown and is checked. conftest.py pops
# BODY_ROLE for the whole session (), so a test sees one only when it sets it.
# The role under test comes from the shipped config: if the lists stop naming it, the
# pass test goes red instead of staying green against a list nobody ships.
_A_ON = {"CLOSE_REVIEW_GATE_ENABLED": "1", "MIND_AGENT": "nobody"}


def _shipped_post_hoc_roles():
    import yaml
    cfg = yaml.safe_load((SCRIPTS.parent / "config" / "aspirations.yaml")
                         .read_text(encoding="utf-8"))["close_review_gate"]
    return [r for key in ("review_closer_roles", "review_sampled_roles")
            for r in (cfg.get(key) or [])]


def _tier_lines(stdout):
    """Every check-A line the gate printed, parsed. Check B prints its own line too."""
    out = []
    for ln in stdout.splitlines():
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("check") == "tier":
            out.append(d)
    return out


def test_post_hoc_role_PASSES_check_A_and_names_the_lane(tmp_path):
    roles = _shipped_post_hoc_roles()
    assert roles, "the shipped config lists no post-hoc role"
    r = _run_gate(_goal(priority="HIGH"), tmp_path, {**_A_ON, "BODY_ROLE": roles[0]})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "REFUSED" not in r.stderr
    (line,) = _tier_lines(r.stdout)
    assert (line["decision"], line.get("lane"), line.get("role")) == \
        ("pass", "post-hoc", roles[0].strip().lower())


@pytest.mark.parametrize("role_env", [{}, {"BODY_ROLE": ""}, {"BODY_ROLE": "  "}],
                         ids=["absent", "empty", "blank"])
def test_UNSET_role_is_still_REFUSED(tmp_path, role_env):
    """The same goal as the pass test above, with no role: reducer-or-unknown is checked."""
    r = _run_gate(_goal(priority="HIGH"), tmp_path, {**_A_ON, **role_env})
    assert r.returncode == 1, r.stdout + r.stderr
    assert [d["decision"] for d in _tier_lines(r.stdout)] == ["block"]


def test_UNLISTED_role_is_still_REFUSED(tmp_path):
    assert "reducer" not in [r.strip().lower() for r in _shipped_post_hoc_roles()]
    r = _run_gate(_goal(priority="HIGH"), tmp_path, {**_A_ON, "BODY_ROLE": "reducer"})
    assert r.returncode == 1, r.stdout + r.stderr
    assert [d["decision"] for d in _tier_lines(r.stdout)] == ["block"]


def test_post_hoc_roles_MALFORMED_lists_exempt_no_one():
    """A list the gate cannot read as strings contributes nothing. The last case is the
    positive control: without it, a reader that always returned the empty set passes."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_crg_roles", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    f = mod._post_hoc_roles
    assert f(None) == frozenset()
    assert f({}) == frozenset()
    assert f({"review_sampled_roles": "worker"}) == frozenset()
    assert f({"review_sampled_roles": [1, None, ["worker"], {"r": "worker"}]}) == frozenset()
    assert f({"review_closer_roles": ["", "  "]}) == frozenset()
    assert f({"review_closer_roles": [" Worker "],
              "review_sampled_roles": ["x", 3]}) == {"worker", "x"}


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


def _extract_winpath_def() -> str:
    """Pull iteration-close.sh's own `_winpath` definition, verbatim.

    The extracted gate block CALLS `_winpath` (g-115-8706 routed every
    `python3 <file-arg>` through it), and the helper is defined near the top of
    the script -- far outside the block. Running the block alone therefore hit
    `_winpath: command not found`, `$(...)` expanded to empty, and python3 ran
    with argv[0] unset: the gate looked dead when it was merely unresolvable
    (13 tests red, g-115-8995).

    EXTRACTED, NEVER HAND-COPIED, and this is the whole point: a pasted copy is
    a second definition free to drift from the one production uses, which is
    the defect class this file's own header warns about (guard-920). If the
    helper is renamed or its body changes, the prelude changes with it.
    """
    lines = ITERATION_CLOSE.read_text(encoding="utf-8").splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.rstrip() == "_winpath() {"]
    assert len(starts) == 1, f"expected exactly 1 _winpath def, found {len(starts)}"
    start = starts[0]
    for j in range(start + 1, len(lines)):
        if lines[j].rstrip() == "}":
            return "\n".join(lines[start:j + 1])
    raise AssertionError("_winpath def has no closing brace")


def _run_block(tmp_path, stub_rc, *, overrides=None, goal_status="completed",
               summary="", note_file=""):
    """Execute the extracted block against a stub gate. Returns (rc, stderr, argv).
    The stub also keeps what reached its stdin, in stdin.txt beside argv.txt."""
    script_dir = tmp_path / "scripts"
    script_dir.mkdir(exist_ok=True)
    argv_log = tmp_path / "argv.txt"
    (script_dir / "close-review-gate.py").write_text(
        "import sys, pathlib\n"
        f"pathlib.Path({str(argv_log)!r}).write_text(repr(sys.argv[1:]))\n"
        f"pathlib.Path({str(tmp_path / 'stdin.txt')!r}).write_text(sys.stdin.read())\n"
        f"sys.exit({stub_rc})\n",
        encoding="utf-8",
    )
    core_root = tmp_path / "core"
    (core_root / "logs").mkdir(parents=True, exist_ok=True)

    ov = overrides or {}
    harness = "\n".join([
        "set -u",
        # The block's own helper prelude, read from the same file the block
        # came from -- see _extract_winpath_def.
        _extract_winpath_def(),
        f"SCRIPT_DIR={str(script_dir)!r}",
        f"CORE_ROOT={str(core_root)!r}",
        f'GOAL_STATUS="{goal_status}"',
        'GOAL_ID="g-357-40"',
        'SOURCE="world"',
        f'OVERRIDE_CLOSE_REVIEW="{ov.get("close", "")}"',
        f'OVERRIDE_NOTE_MARKER="{ov.get("note", "")}"',
        f"SUMMARY={shlex.quote(summary)}",
        f"OUTCOME_NOTE_FILE={shlex.quote(note_file)}",
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


# ─── a self-serve refusal () ──────────────────────────────────────
# A check-A refusal requests the review itself: it stamps review_requested unless a
# request is already open, so the queue offers the goal to an independent reviewer and
# the refusal has a next move that is not the override. The override is honored only
# where team-state lists no other mind. The subprocess tests read a --goal-json record,
# which the gate never writes. The store write itself is pinned in-process with
# bash_cmd spied and pointed at /bin/true, the shape the load_goal pin above uses, so
# no test here reaches a store.

_ASKED = "2026-10-06T09:00:00"


def _verdict_file(tmp_path, gid, verdict, reviewed_at, reviewer="peer-mind"):
    """One verdict as the producer writes it: a list trail whose last entry wins."""
    d = tmp_path / "audit-reports" / "close-reviews"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{gid}.json").write_text(json.dumps(
        [{"verdict": verdict, "reviewer": reviewer, "reviewed_at": reviewed_at}]),
        encoding="utf-8")


def _roster(tmp_path, *names):
    """A roster file in team-state's agent_status shape, for --roster-json."""
    p = tmp_path / "roster.json"
    p.write_text(json.dumps({n: {"last_active": _ASKED} for n in names}), encoding="utf-8")
    return str(p)


@pytest.mark.parametrize("goal_kw,verdict,expect", [
    ({}, None, "stamp"),
    ({"review_requested": _ASKED}, None, "open"),
    ({"review_requested": _ASKED}, ("REJECT", "2026-10-06T10:00:00"), "restamp"),
    ({"review_requested": _ASKED}, ("REJECT", "2026-10-06T08:00:00"), "open"),
], ids=["no-request", "open-request", "answered-by-REJECT", "REJECT-before-the-ask"])
def test_refusal_decides_the_request_and_never_writes_a_goal_json_record(
        tmp_path, goal_kw, verdict, expect):
    """No request: stamp one. An open request: leave it. A request a REJECT answered:
    stamp it again, because the queue no longer lists it. A REJECT from before the ask
    answers nothing, so that request is still open. The record came from --goal-json, so
    nothing is written in any case."""
    if verdict:
        _verdict_file(tmp_path, "g-999-01", *verdict)
    r = _run_gate(_goal(priority="HIGH", **goal_kw), tmp_path, _A_ON)
    assert r.returncode == 1, r.stdout + r.stderr
    (line,) = _tier_lines(r.stdout)
    assert (line["decision"], line["request"], line["request_written"]) == \
        ("block", expect, None)
    if expect == "open":
        assert line["review_requested"] == _ASKED
        assert "already open" in r.stderr
    else:
        assert line["review_requested"] not in (None, _ASKED)
        assert "was not written" in r.stderr


def test_a_releasing_verdict_that_PREDATES_the_request_does_not_release(tmp_path):
    """A request made after an APPROVE asks for a fresh review, so the old APPROVE no
    longer releases the close: the same rule the queue lists requests by."""
    _verdict_file(tmp_path, "g-999-01", "APPROVE", "2026-10-06T08:00:00")
    r = _run_gate(_goal(priority="HIGH", review_requested=_ASKED), tmp_path, _A_ON)
    assert r.returncode == 1, r.stdout + r.stderr
    (line,) = _tier_lines(r.stdout)
    assert (line["decision"], line["stale"], line["request"]) == ("block", True, "open")
    assert "predates its review request" in r.stderr


def test_a_releasing_verdict_that_ANSWERS_the_request_releases(tmp_path):
    """The positive control for the stale case: the same APPROVE, written after the ask.
    `answered` pins that the request rule ran: a queue that failed to load would pass this
    close too, by the old rule, with answered None."""
    _verdict_file(tmp_path, "g-999-01", "APPROVE", "2026-10-06T10:00:00")
    r = _run_gate(_goal(priority="HIGH", review_requested=_ASKED), tmp_path, _A_ON)
    assert r.returncode == 0, r.stdout + r.stderr
    (line,) = _tier_lines(r.stdout)
    assert (line["decision"], line["reviewer"], line["answered"], line["answers_fault"]) == \
        ("pass", "peer-mind", True, None)


@pytest.mark.parametrize("closer_kw,reachable", [
    ({}, False),
    ({"executed_by": "nobody"}, True),
], ids=["no-closer", "executed_by"])
def test_a_refusal_says_whether_a_reviewer_can_be_offered_the_request(
        tmp_path, closer_kw, reachable):
    """The queue offers a request only on a goal that names its closer (completed_by,
    executed_by or claimed_by) and declines one that names none. So a refusal on such a goal
    says so and hands over the claim that records executed_by, instead of promising a
    reviewer; one that names its closer says the queue offers the request."""
    r = _run_gate(_goal(priority="HIGH", review_requested=_ASKED, **closer_kw),
                  tmp_path, _A_ON)
    assert r.returncode == 1, r.stdout + r.stderr
    (line,) = _tier_lines(r.stdout)
    assert (line["request"], line["request_reachable"]) == ("open", reachable)
    assert ("NO CLOSER IS RECORDED" in r.stderr) is (not reachable)
    assert ("aspirations-claim.sh g-999-01 --source" in r.stderr) is (not reachable)
    assert ("offers the request to an independent reviewer" in r.stderr) is reachable


def test_override_is_REFUSED_where_another_mind_is_listed(tmp_path):
    r = _run_gate(_goal(priority="HIGH"), tmp_path, _A_ON,
                  extra_args=("--override-close-review", "no peer around",
                              "--roster-json", _roster(tmp_path, "nobody", "peer-mind")))
    assert r.returncode == 1, r.stdout + r.stderr
    (line,) = _tier_lines(r.stdout)
    assert (line["decision"], line["override_refused"], line["request"]) == \
        ("block", ["peer-mind"], "stamp")
    assert "was NOT honored" in r.stderr
    assert not (tmp_path / "close-review-overrides.jsonl").exists(), \
        "an override the gate refused reached the override ledger"


@pytest.mark.parametrize("roster", ["solo", "unreadable"])
def test_override_is_honored_with_no_other_mind_or_an_unreadable_roster(tmp_path, roster):
    """The positive control for the refusal above. An unreadable roster is the gate's own
    fault, so it fails open and honors the override, saying which case it was."""
    path = _roster(tmp_path, "nobody") if roster == "solo" else str(tmp_path / "absent.json")
    r = _run_gate(_goal(priority="HIGH"), tmp_path, _A_ON,
                  extra_args=("--override-close-review", "solo deployment",
                              "--roster-json", path))
    assert r.returncode == 0, r.stdout + r.stderr
    (line,) = _tier_lines(r.stdout)
    assert (line["decision"], line["roster"]) == ("override", roster)
    rows = (tmp_path / "close-review-overrides.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1 and json.loads(rows[0])["roster"] == roster


@pytest.mark.parametrize("goal_kw,verdict,expect", [
    ({}, None, "stamp"),
    ({"review_requested": _ASKED}, None, "open"),
    ({"review_requested": _ASKED}, ("REJECT", "2026-10-06T10:00:00"), "restamp"),
], ids=["no-request", "open-request", "answered-by-REJECT"])
def test_a_store_read_refusal_STAMPS_the_request_through_the_store_writer(
        tmp_path, monkeypatch, capsys, goal_kw, verdict, expect):
    """The production path: the goal comes from the store, so the request is written,
    through aspirations-update-goal.sh with the script as bash_cmd's first positional
    (guard-920: the literal call shape). An open request writes nothing."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_crg_stamp", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    seen = []

    def _spy(script, *args):
        seen.append((Path(script).name, args))
        return ["/bin/true"]   # the write "lands" and touches no store

    monkeypatch.setattr(mod, "bash_cmd", _spy)
    monkeypatch.setattr(mod, "load_goal", lambda gid, src: _goal(priority="HIGH", **goal_kw))
    monkeypatch.setenv("CLOSE_REVIEW_GATE_ENABLED", "1")
    monkeypatch.setenv("CLOSE_REVIEW_LEDGER_DIR", str(tmp_path))
    monkeypatch.setenv("MIND_AGENT", "nobody")
    if verdict:
        _verdict_file(tmp_path, "g-999-01", *verdict)
    assert mod.main(["--goal", "g-999-01", "--source", "world"]) == 1
    (line,) = _tier_lines(capsys.readouterr().out)
    writes = [a for name, a in seen if name == "aspirations-update-goal.sh"]
    if expect == "open":
        assert (line["request"], line["request_written"], writes) == ("open", None, [])
        return
    assert (line["request"], line["request_written"]) == (expect, True)
    (args,) = writes
    assert args == ("--source", "world", "g-999-01", "review_requested",
                    line["review_requested"]), args
    assert line["review_requested"] != _ASKED


def test_a_failed_stamp_still_refuses_and_prints_the_write(tmp_path, monkeypatch, capsys):
    """A request that could not be written leaves the close refused, and the refusal
    hands over the exact command that writes it."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_crg_stamp_fail", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "bash_cmd", lambda script, *a: ["/bin/false"])
    monkeypatch.setattr(mod, "load_goal", lambda gid, src: _goal(priority="HIGH"))
    monkeypatch.setenv("CLOSE_REVIEW_GATE_ENABLED", "1")
    monkeypatch.setenv("CLOSE_REVIEW_LEDGER_DIR", str(tmp_path))
    monkeypatch.setenv("MIND_AGENT", "nobody")
    assert mod.main(["--goal", "g-999-01", "--source", "world"]) == 1
    out = capsys.readouterr()
    (line,) = _tier_lines(out.out)
    assert (line["request"], line["request_written"]) == ("stamp", False)
    assert "REVIEW REQUEST NOT WRITTEN" in out.err
    assert "aspirations-update-goal.sh --source world g-999-01 review_requested" in out.err


def test_do_verify_runs_the_close_review_gate_AFTER_closure_evidence():
    """A check-A refusal now writes a review request, so it must come after the
    closure-evidence gate: a reviewer is asked only about a close whose evidence rows
    already pass. Both stay before the intent marker that precedes the status write."""
    lines = ITERATION_CLOSE.read_text(encoding="utf-8").splitlines()

    def _at(match):
        hits = [i for i, ln in enumerate(lines) if match(ln.strip())]
        assert len(hits) == 1, hits
        return hits[0]

    evidence = _at(lambda s: s == 'if [[ "$GOAL_STATUS" == "completed" && -f '
                                  '"$SCRIPT_DIR/closure-evidence-gate.py" ]]; then')
    review = _at(lambda s: s == _BLOCK_HEAD)
    # The intent marker's own guard line occurs twice in the file; its comment once.
    intent = _at(lambda s: s.startswith("# g-284-06 Step 0: Ordered-write intent marker"))
    assert evidence < review < intent, (evidence, review, intent)


# ─── a refused close waits for its verdict () ─────────────────────
# A check-A refusal writes the close's outcome note for the reviewer when the goal
# carries none: the --outcome-note-file, else the summary do_verify pipes in. The
# write goes through closure-evidence-write.sh, which exits 0 whatever it did, so
# whether the note landed is read back from the store. As for the stamp above, the
# store-read path runs in-process with bash_cmd spied onto /bin/true and load_goal
# stubbed, so no test here reaches a store.

_NOTE = "Shipped the hold.\nEvidence: 18 tests pass."


def _note_gate(monkeypatch, tmp_path, goal, *, lands=True):
    """The gate module with its store calls stubbed. Returns (mod, writes): writes holds
    the args of each closure-evidence-write.sh call. load_goal reads a record the spy
    updates. `lands` is True for the sent note landing as the writer lands it (a
    --summary-file read the way its `$(cat)` reads it, trailing newlines trimmed), False
    for nothing landing, or the text that landed instead."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_crg_note", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    writes, record = [], dict(goal)

    def _spy(script, *args):
        if Path(script).name == "closure-evidence-write.sh":
            writes.append(args)
            if lands is True:
                record["outcome_note"] = (
                    args[args.index("--summary") + 1] if "--summary" in args else
                    Path(args[args.index("--summary-file") + 1])
                    .read_text(encoding="utf-8").rstrip("\n"))
            elif lands:
                record["outcome_note"] = lands
        return ["/bin/true"]   # every write "succeeds" and touches no store

    monkeypatch.setattr(mod, "bash_cmd", _spy)
    monkeypatch.setattr(mod, "load_goal", lambda gid, src: dict(record))
    monkeypatch.setenv("CLOSE_REVIEW_GATE_ENABLED", "1")
    monkeypatch.setenv("CLOSE_REVIEW_LEDGER_DIR", str(tmp_path))
    monkeypatch.setenv("MIND_AGENT", "nobody")
    return mod, writes


def _stdin(monkeypatch, text):
    """What do_verify pipes in: the gate reads sys.stdin.buffer, so a bare StringIO won't do."""
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(text.encode("utf-8"))))


@pytest.mark.parametrize("via", ["summary", "outcome-note-file"])
def test_a_store_read_refusal_WRITES_the_close_s_note_for_the_reviewer(
        tmp_path, monkeypatch, capsys, via):
    """The note do_verify would land at the status write, written now through the one
    closure-narrative writer (argv pinned). The --outcome-note-file wins over the summary,
    the order the closure-evidence gate reads them in."""
    mod, writes = _note_gate(monkeypatch, tmp_path, _goal(priority="HIGH"))
    nf = tmp_path / "note.md"
    argv = ["--goal", "g-999-01", "--source", "world", "--summary-stdin"]
    if via == "outcome-note-file":
        nf.write_text("\n" + _NOTE + "\n\n", encoding="utf-8")   # blank lines either side
        argv += ["--outcome-note-file", str(nf)]
        _stdin(monkeypatch, "The loop's one-line summary.")
    else:
        _stdin(monkeypatch, _NOTE)
    assert mod.main(argv) == 1
    out = capsys.readouterr()
    (line,) = _tier_lines(out.out)
    assert (line["note"], line["note_from"], line["note_written"], line["note_error"]) == \
        ("write", via, True, None)
    text = ("--summary-file", str(nf)) if via == "outcome-note-file" else ("--summary", _NOTE)
    assert writes == [("--goal", "g-999-01", "--source", "world", *text,
                       "--prefix", "[close-review-gate]", "--no-supersede")], writes
    assert "OUTCOME NOTE WRITTEN" in out.err
    # The request was written too, so the refusal says the selector now holds the goal.
    assert "block_reason awaiting_review" in out.err


@pytest.mark.parametrize("with_file", [False, True], ids=["summary", "outcome-note-file"])
def test_a_note_already_on_the_goal_is_KEPT_and_never_rewritten(
        tmp_path, monkeypatch, capsys, with_file):
    """A note on the record is what the reviewer reads, so a refusal never replaces it. An
    --outcome-note-file replaces it only at the status write, and the refusal says so."""
    mod, writes = _note_gate(monkeypatch, tmp_path,
                             _goal(priority="HIGH", outcome_note="An earlier note."))
    argv = ["--goal", "g-999-01", "--source", "world", "--summary-stdin"]
    if with_file:
        nf = tmp_path / "note.md"
        nf.write_text(_NOTE, encoding="utf-8")
        argv += ["--outcome-note-file", str(nf)]
    _stdin(monkeypatch, _NOTE)
    assert mod.main(argv) == 1
    out = capsys.readouterr()
    (line,) = _tier_lines(out.out)
    assert (line["note"], line["note_written"], writes) == ("kept", None, [])
    assert "already carries an outcome_note (16 chars)" in out.err
    assert ("replaces it only at the status write" in out.err) is with_file


def test_a_note_that_does_not_land_still_refuses_and_prints_the_write(
        tmp_path, monkeypatch, capsys):
    """The writer exits 0 whatever it did, so only the read-back can tell. A note that is
    not on the record leaves the close refused, and the refusal prints the command."""
    mod, writes = _note_gate(monkeypatch, tmp_path, _goal(priority="HIGH"), lands=False)
    nf = tmp_path / "note.md"
    nf.write_text(_NOTE, encoding="utf-8")
    _stdin(monkeypatch, "")
    assert mod.main(["--goal", "g-999-01", "--source", "world", "--summary-stdin",
                     "--outcome-note-file", str(nf)]) == 1
    out = capsys.readouterr()
    (line,) = _tier_lines(out.out)
    assert (line["note"], line["note_written"], line["note_error"]) == \
        ("write", False, "not on the record after the write (rc=0)")
    assert len(writes) == 1, writes
    assert "OUTCOME NOTE NOT WRITTEN" in out.err
    assert (f"closure-evidence-write.sh --goal g-999-01 --source world --summary-file {nf}"
            in out.err)


@pytest.mark.parametrize("landed,written", [
    (_NOTE + "\n\n-- signed by a worker Body on its own box", True),
    (_NOTE.replace("\n", "\r\n") + "\r", True),
    ("Shipped the hold.\nA different evidence line.", False),
], ids=["signed-after", "crlf", "shares-only-a-line"])
def test_the_read_back_wants_the_whole_note(tmp_path, monkeypatch, capsys, landed, written):
    """The writer may append a signature or a provenance line after the note and its shell
    may keep a CR, but it never changes the text: the note followed by more has landed.
    Another writer's note that shares only a line with it has not, so the refusal says the
    note was not written (fresh-eyes review of g-375-149: a first-line test took it)."""
    mod, writes = _note_gate(monkeypatch, tmp_path, _goal(priority="HIGH"), lands=landed)
    _stdin(monkeypatch, _NOTE)
    assert mod.main(["--goal", "g-999-01", "--source", "world", "--summary-stdin"]) == 1
    out = capsys.readouterr()
    (line,) = _tier_lines(out.out)
    assert (line["note"], line["note_written"], len(writes)) == ("write", written, 1)
    assert ("OUTCOME NOTE NOT WRITTEN" in out.err) is (not written)


@pytest.mark.parametrize("goal_kw,summary,expect", [
    ({}, _NOTE, "write"),
    ({"outcome_note": "An earlier note."}, _NOTE, "kept"),
    ({}, "", "none"),
], ids=["no-note", "has-a-note", "no-summary"])
def test_a_goal_json_refusal_decides_the_note_and_never_writes_it(
        tmp_path, goal_kw, summary, expect):
    """The subprocess rc path, with the summary on stdin as do_verify sends it. The record
    came from --goal-json, so the decision is reported and nothing is written. No request
    was written either, so nothing holds the goal and the refusal does not say it does."""
    r = _run_gate(_goal(priority="HIGH", **goal_kw), tmp_path, _A_ON,
                  extra_args=("--summary-stdin",), stdin_text=summary)
    assert r.returncode == 1, r.stdout + r.stderr
    (line,) = _tier_lines(r.stdout)
    assert (line["note"], line["note_written"], line["note_error"]) == (expect, None, None)
    assert line["note_from"] == ("summary" if summary else None)
    assert {"write": "so the outcome note was not written",
            "kept": "already carries an outcome_note",
            "none": "carried no outcome note and no summary"}[expect] in r.stderr
    assert "awaiting_review" not in r.stderr


@pytest.mark.parametrize("note_file", ["", "/notes/close.md"], ids=["summary-only", "note-file"])
def test_do_verify_pipes_the_summary_and_forwards_the_note_file(tmp_path, note_file):
    """The gate writes the note a refused close would have landed, so do_verify hands it
    both: the summary on stdin, byte for byte, and the --outcome-note-file when given."""
    summary = "Shipped it; the reviewer's view.\nA second line with $HOME and `ticks`."
    rc, err, argv = _run_block(tmp_path, 0, summary=summary, note_file=note_file)
    assert rc == 0, err
    assert "--summary-stdin" in argv, argv
    assert ("--outcome-note-file" in argv) is bool(note_file), argv
    assert not note_file or repr(note_file) in argv, argv
    assert (tmp_path / "stdin.txt").read_text(encoding="utf-8") == summary
