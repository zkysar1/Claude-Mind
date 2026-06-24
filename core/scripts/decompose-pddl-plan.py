#!/usr/bin/env python3
"""decompose-pddl-plan.py -- optional PDDL symbolic-plan constraint for /decompose ().

Given a goal's decomposition SHAPE (derived from its category + primary verb/skill
via the world overlay's shape-map.json), select the matching STRIPS domain+problem,
obtain a validated plan, and emit JSON describing whether the shape's decomposition
is symbolically solvable and what canonical primitive ordering the validated plan
prescribes. The /decompose skill consumes this to CONSTRAIN its LLM-generated tree
leaves when solvable, or to file a CREATE_BLOCKER when unsolvable; the LLM tree is
ALWAYS retained as the R3 fallback.

This is framework LOGIC over domain-overlay DATA: the routing strings and the
domain/problem files all live in the world overlay (world/conventions/pddl-domain/),
so this core script carries no deployment-domain terms. When the overlay is absent
(a fresh world) or a goal's shape is unmapped, it emits applicable=false and
/decompose degrades to pure-LLM decomposition (the R3 fallback). The plans for the
shipped shapes are static, so the cached <problem>.soln is reused; pyperplan is
re-run only when the .soln is missing or older than its domain/problem (keeping
own-cloud writes off the common path). rb-2207: the plan is independently
re-validated (validate_plan_generic.py) -- a planner's own "solved" is not trusted.
guard-795-safe: pure-local pyperplan subprocess, no network/cloud.

Usage:
  py -3 core/scripts/decompose-pddl-plan.py --category <c> --verb <v> --title <t> [--goal-id <id>]
  (--pddl-dir <path> overrides the overlay location; test seam only.)
Exit code: 0 ALWAYS (fail-open -- a planner/overlay/validator fault must never block
decomposition; it degrades to applicable=false or solvable=false). JSON to stdout;
the /decompose skill branches on the JSON content, not the exit code.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from _paths import WORLD_DIR
except Exception:  # pragma: no cover - _paths always importable in-tree
    WORLD_DIR = None


def _emit(obj):
    print(json.dumps(obj))
    raise SystemExit(0)


def _load_map(pddl_dir):
    f = pddl_dir / "shape-map.json"
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def pick_shape(smap, category, verb, title):
    """Verb/skill match first (substring over verb+title), category fallback.

    Longest verb key wins so a specific key (review-hypotheses) beats a generic
    one (review) when both are present. Category lookup is case-insensitive.
    """
    hay = " ".join(x for x in (verb or "", title or "") if x).lower()
    vs = smap.get("verb_shape", {}) or {}
    for key in sorted(vs, key=len, reverse=True):
        if key.lower() in hay:
            return vs[key], "verb/skill:" + key
    cs = smap.get("category_shape", {}) or {}
    if category:
        for k, v in cs.items():
            if k.lower() == category.lower():
                return v, "category-default"
    return None, "unmapped"


def _plan_steps(soln_path):
    steps = []
    for line in soln_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith(";"):
            # Total function (rb-1915): a line that strips to empty -- "()",
            # "( )", or a partial/corrupted write -- yields no token; skip it
            # rather than IndexError on split()[0]. (fresh-eyes F1: the crash
            # broke the docstring's documented exit-0 fail-open contract.)
            parts = line.strip("()").split()
            if parts:
                steps.append(parts[0])
    return steps


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", default="")
    ap.add_argument("--verb", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--goal-id", default="")
    ap.add_argument("--pddl-dir", default="")  # test seam (override overlay dir)
    a = ap.parse_args(argv)

    if a.pddl_dir:
        pddl_dir = Path(a.pddl_dir)
    elif WORLD_DIR:
        pddl_dir = Path(WORLD_DIR) / "conventions" / "pddl-domain"
    else:
        _emit({"applicable": False, "reason": "WORLD_DIR unresolved"})

    if not pddl_dir.is_dir():
        _emit({"applicable": False, "reason": "no pddl-domain overlay in this world"})

    smap = _load_map(pddl_dir)
    if not smap:
        _emit({"applicable": False, "reason": "shape-map.json absent or unreadable"})

    shape, how = pick_shape(smap, a.category, a.verb, a.title)
    if not shape:
        _emit({"applicable": False, "reason": "goal shape unmapped",
               "category": a.category, "verb": a.verb})

    files = (smap.get("shape_files", {}) or {}).get(shape)
    if not files or len(files) != 2:
        _emit({"applicable": False, "reason": "shape " + str(shape) + " has no domain/problem mapping"})

    dom = pddl_dir / files[0]
    prob = pddl_dir / files[1]
    if not (dom.is_file() and prob.is_file()):
        _emit({"applicable": False, "reason": "domain files absent for shape " + shape, "shape": shape})

    soln = prob.with_name(prob.name + ".soln")
    # Reuse the cached plan for these static shapes; regenerate only when the
    # .soln is missing or stale relative to its domain/problem. Keeps own-cloud
    # writes off the common decompose path.
    need_regen = (not soln.is_file()) or (
        soln.stat().st_mtime < max(dom.stat().st_mtime, prob.stat().st_mtime))
    if need_regen:
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pyperplan", "-s", "bfs", str(dom), str(prob)],
                capture_output=True, text=True, timeout=120, cwd=str(pddl_dir))
        except Exception as e:
            _emit({"applicable": True, "shape": shape, "shape_source": how,
                   "solvable": False, "blocker_reason": "pyperplan invocation failed: " + str(e)})
        if not soln.is_file():
            _emit({"applicable": True, "shape": shape, "shape_source": how, "solvable": False,
                   "blocker_reason": "pyperplan produced no solution (rc=%s) -- shape %s has no valid plan"
                   % (r.returncode, shape)})

    # Fail-open (docstring contract): a locked/deleted/corrupted soln must
    # degrade to solvable=false, never crash the script. read_text can raise on
    # an own-cloud sync lock/delete; _plan_steps is now total on content but the
    # I/O remains fallible. (fresh-eyes F1.)
    try:
        plan = _plan_steps(soln)
    except Exception as e:
        _emit({"applicable": True, "shape": shape, "shape_source": how, "solvable": False,
               "blocker_reason": "solution file unreadable or malformed: " + str(e)})

    # rb-2207: independently re-validate the plan. A planner's own "solved" is
    # not evidence the plan is valid; validate_plan_generic.py is that evidence.
    valid, edges, detail = "unverified", None, ""
    vp = pddl_dir / "validate_plan_generic.py"
    if vp.is_file():
        try:
            v = subprocess.run([sys.executable, str(vp), str(dom), str(prob), str(soln)],
                               capture_output=True, text=True, timeout=60, cwd=str(pddl_dir))
            valid = (v.returncode == 0)
            m = re.search(r"precondition_edges_checked:\s*(\d+)", v.stdout)
            edges = int(m.group(1)) if m else None
            lines = v.stdout.strip().splitlines()
            detail = lines[-1] if lines else ""
        except Exception as e:
            valid, detail = "unverified", "validator error: " + str(e)

    if valid is False:
        _emit({"applicable": True, "shape": shape, "shape_source": how, "solvable": True,
               "valid": False, "plan": plan, "plan_length": len(plan),
               "blocker_reason": "plan failed independent validation: " + detail})

    _emit({"applicable": True, "shape": shape, "shape_source": how, "solvable": True,
           "valid": valid, "plan": plan, "plan_length": len(plan), "edges": edges, "detail": detail})


if __name__ == "__main__":
    main()
