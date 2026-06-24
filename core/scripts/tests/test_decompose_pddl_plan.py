"""Tests for core/scripts/decompose-pddl-plan.py ().

Covers the shape-routing (verb-first, category fallback, unmapped), the
fail-open applicable=false paths (no overlay / no shape-map), and the full
solvable path against a tiny hermetic STRIPS fixture (pyperplan + the generic
validator). The fixture keeps the test self-contained — it does NOT depend on
the world overlay (own-cloud).
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import importlib.util

SCRIPTS = Path(__file__).resolve().parent.parent
MODULE_PATH = SCRIPTS / "decompose-pddl-plan.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("decompose_pddl_plan", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load_module()


# ---- pick_shape unit tests (pure function) --------------------------------

SMAP = {
    "verb_shape": {"review": "r-generic", "review-hypotheses": "hypothesis",
                   "research-topic": "research", "wire": "framework-code",
                   "tree": "maintenance"},
    "category_shape": {"framework-patterns": "framework-code",
                       "Agent-Health": "hypothesis"},
    "shape_files": {},
}


def test_pick_shape_verb_first():
    shape, how = M.pick_shape(SMAP, "agent-health", "review-hypotheses", "resolve X")
    assert shape == "hypothesis"
    assert how.startswith("verb/skill")


def test_pick_shape_longest_verb_key_wins():
    # both "review" and "review-hypotheses" are substrings of the title; the
    # longer (more specific) key must win.
    shape, how = M.pick_shape(SMAP, "", "", "please review-hypotheses now")
    assert shape == "hypothesis"


def test_pick_shape_category_fallback_case_insensitive():
    # no verb match -> category fallback, case-insensitive key match.
    shape, how = M.pick_shape(SMAP, "agent-health", "xyz", "nothing matches verb")
    assert shape == "hypothesis"
    assert how == "category-default"


def test_pick_shape_unmapped():
    shape, how = M.pick_shape(SMAP, "no-such-cat", "no-such-verb", "nope")
    assert shape is None
    assert how == "unmapped"


def test_plan_steps_total_on_malformed_lines(tmp_path):
    # _plan_steps MUST be a total function (rb-1915): lines that strip to empty
    # -- "()", "( )", a blank-after-comment, a partial/corrupted write -- are
    # skipped, never IndexError on split()[0]. Regression guard for fresh-eyes
    # F1 (msg-20260622-071558-alpha-2374): the uncaught crash broke the
    # documented exit-0 fail-open contract /decompose Step 5.6 relies on.
    p = tmp_path / "m.soln"
    p.write_text("()\n( )\n;a comment\n(do-a)\n(do-b arg1 arg2)\n", encoding="utf-8")
    assert M._plan_steps(p) == ["do-a", "do-b"]
    # Empty + comment-only files yield an empty plan, not a crash.
    e = tmp_path / "e.soln"
    e.write_text(";only a comment\n\n()\n", encoding="utf-8")
    assert M._plan_steps(e) == []


# ---- integration: applicable=false fail-open paths ------------------------

def _run(args):
    r = subprocess.run([sys.executable, str(MODULE_PATH)] + args,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"helper must always exit 0 (fail-open); got {r.returncode}, stderr={r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_applicable_false_when_overlay_dir_missing(tmp_path):
    missing = tmp_path / "does-not-exist"
    out = _run(["--category", "framework-patterns", "--verb", "wire",
                "--title", "x", "--pddl-dir", str(missing)])
    assert out["applicable"] is False


def test_applicable_false_when_no_shape_map(tmp_path):
    # dir exists but has no shape-map.json -> applicable=false (fresh world).
    out = _run(["--category", "framework-patterns", "--verb", "wire",
                "--title", "x", "--pddl-dir", str(tmp_path)])
    assert out["applicable"] is False
    assert "shape-map" in out["reason"]


def test_unmapped_shape_is_applicable_false(tmp_path):
    (tmp_path / "shape-map.json").write_text(json.dumps(SMAP), encoding="utf-8")
    out = _run(["--category", "no-cat", "--verb", "no-verb", "--title", "no",
                "--pddl-dir", str(tmp_path)])
    assert out["applicable"] is False
    assert out["reason"] == "goal shape unmapped"


# ---- integration: full solvable path against a hermetic STRIPS fixture -----

DOMAIN = """(define (domain t)
  (:requirements :strips)
  (:predicates (start) (done-a) (done-b))
  (:action do-a :parameters () :precondition (start) :effect (done-a))
  (:action do-b :parameters () :precondition (done-a) :effect (done-b)))
"""
PROBLEM = """(define (problem tp)
  (:domain t)
  (:init (start))
  (:goal (and (done-a) (done-b))))
"""


def _validator_src():
    # the validator ships in the world overlay; locate it relative to a known
    # marker, else skip the validator-dependent assertion.
    import os
    wd = os.environ.get("WORLD_DIR")
    if wd:
        cand = Path(wd) / "conventions" / "pddl-domain" / "validate_plan_generic.py"
        if cand.is_file():
            return cand
    return None


def test_solvable_path_against_fixture(tmp_path):
    try:
        import pyperplan  # noqa: F401
    except Exception:
        import pytest
        pytest.skip("pyperplan not installed")
    vsrc = _validator_src()
    smap = {"verb_shape": {"build": "t"}, "category_shape": {},
            "shape_files": {"t": ["t-domain.pddl", "t-problem.pddl"]}}
    (tmp_path / "shape-map.json").write_text(json.dumps(smap), encoding="utf-8")
    (tmp_path / "t-domain.pddl").write_text(DOMAIN, encoding="utf-8")
    (tmp_path / "t-problem.pddl").write_text(PROBLEM, encoding="utf-8")
    if vsrc:
        shutil.copy(vsrc, tmp_path / "validate_plan_generic.py")
    out = _run(["--category", "", "--verb", "build", "--title", "build a thing",
                "--pddl-dir", str(tmp_path)])
    assert out["applicable"] is True
    assert out["shape"] == "t"
    assert out["solvable"] is True
    assert out["plan"] == ["do-a", "do-b"]
    assert out["plan_length"] == 2
    if vsrc:
        assert out["valid"] is True
