#!/usr/bin/env python3
"""Refuse any test that writes WORKING MEMORY for a LIVE fleet agent ().

WHY THIS EXISTS. Three post-state-update-gate test files carried an autouse
fixture that saved, null-wrote, then restored `fresh_eyes_last_fire` for the LIVE
agent `zeta`. Every branch was `except Exception: pass` with no `finally`, so any
crash between neutralize and restore destroyed a real cross-agent signal
permanently and silently. The pattern spread by COPYING: daemon_dispatch
inherited it from mode_only, which inherited it from committed_files_only. Two
instances is what made the family visible; this check is what stops the fourth.

THE ROSTER IS DERIVED AT CHECK TIME, NEVER HARDCODED (guard-1699). A check
pinning "zeta" goes stale the moment an agent is retired or added, and would then
silently test nothing while still reporting green — the exact fixture-rot this
check guards against, reproduced one level up.

WHY THE PREDICATE IS NARROW, and why that is deliberate rather than lax. Setting
MIND_AGENT to a real agent name is COMMON and mostly harmless: 59 of the test
files do it, overwhelmingly for path routing or as a module-level default. A
check on that broad shape would be RED on arrival, and a checklist entry that
fails from the day it lands trains every reader to skim the section it lives in
(the reason g-115-4887 deliberately did NOT ship this check before the fix). So
the predicate requires BOTH halves of the actual defect: a live-roster agent name
AND a working-memory WRITE in the same file.

AST, NOT GREP. A plain grep matches the writer's name inside COMMENTS and
DOCSTRINGS — measured: test_dependent_unblock_windows_path.py merely MENTIONS
wm-append.sh in prose and was a false positive under two successive grep
refinements. Docstrings are excluded structurally here instead.

Exit 0 = clean. Exit 1 = at least one offender (named on stdout). Exit 2 = the
roster could not be derived, which fails LOUD rather than passing vacuously: a
check that cannot see the roster has no opinion and must not report success
(rb-245 — verify the population before trusting a zero).
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runtime_bash import bash_cmd  # noqa: E402  (guard-580)

PROJECT_ROOT = Path(__file__).resolve().parents[1].parent
TESTS_DIR = PROJECT_ROOT / "core" / "scripts" / "tests"

WM_WRITER_NAMES = {"WM_SET_SH", "WM_APPEND_SH"}
WM_WRITER_PATHS = ("wm-set.sh", "wm-append.sh")


def live_roster() -> set[str]:
    """Agent names in the live team-state agent_status roster, at check time."""
    r = subprocess.run(
        bash_cmd(PROJECT_ROOT / "core" / "scripts" / "team-state-read.sh",
                 "--field", "agent_status", "--json"),
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"team-state-read failed rc={r.returncode}: {r.stderr[:200]}")
    data = json.loads(r.stdout)
    if not isinstance(data, dict) or not data:
        raise RuntimeError(f"agent_status resolved to an empty/unexpected shape: {type(data).__name__}")
    return set(data.keys())


def _docstring_nodes(tree: ast.AST) -> set[int]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
    return out


def analyze(path: Path, roster: set[str]) -> list[str]:
    """Return the live-roster agent names this file binds, if it also writes WM."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return []
    docs = _docstring_nodes(tree)

    writes_wm = False
    bound: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in WM_WRITER_NAMES:
            writes_wm = True
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            if any(w in node.value for w in WM_WRITER_PATHS):
                writes_wm = True
        # MIND_AGENT bound to a literal: {"MIND_AGENT": "x"} or env["MIND_AGENT"] = "x"
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "MIND_AGENT"
                        and isinstance(v, ast.Constant) and isinstance(v.value, str)):
                    bound.add(v.value)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (isinstance(tgt, ast.Subscript) and isinstance(tgt.slice, ast.Constant)
                        and tgt.slice.value == "MIND_AGENT"
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)):
                    bound.add(node.value.value)
                elif (isinstance(tgt, ast.Name) and tgt.id in ("AGENT", "AGENT_NAME", "TEST_AGENT")
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)):
                    bound.add(node.value.value)

    return sorted(bound & roster) if writes_wm else []


def main(argv: list[str]) -> int:
    scan_dir = TESTS_DIR
    if len(argv) > 1:
        scan_dir = Path(argv[1])
    try:
        roster = live_roster()
    except Exception as exc:
        print(f"FAIL: could not derive the live roster — {exc}")
        print("  A check that cannot see its population must not report success.")
        return 2

    offenders = []
    scanned = 0
    for p in sorted(scan_dir.glob("*.py")):
        scanned += 1
        bad = analyze(p, roster)
        if bad:
            offenders.append((p, bad))

    print(f"roster ({len(roster)}, derived at check time): {sorted(roster)}")
    print(f"scanned: {scanned} file(s) under {scan_dir}")
    if offenders:
        print(f"FAIL: {len(offenders)} test file(s) write working memory for a LIVE roster agent:")
        for p, bad in offenders:
            print(f"  - {p.relative_to(PROJECT_ROOT) if PROJECT_ROOT in p.parents else p} -> {bad}")
        print("  Point the fixture at an OFF-ROSTER agent (see g-115-4887 and")
        print("  core/scripts/tests/test_post_state_update_gate_mode_only.py::_isolate_wm_cooldown).")
        return 1
    print("PASS: no test writes working memory for a live-roster agent.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
