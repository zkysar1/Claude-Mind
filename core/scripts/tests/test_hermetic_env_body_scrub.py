#!/usr/bin/env python3
""" — the hermetic test-env helpers MUST strip the BODY_ namespace.

WHY THIS PIN EXISTS, AND WHY IT IS NOT THE TESTS THAT ALREADY BROKE.

`wm.wm_path()` resolves BODY_WM_PATH FIRST and MIND_AGENT_DIR only SECOND. Every
worker Body exports BODY_WM_PATH (the bash-agent-inject hook), so any test that
builds a subprocess env without dropping that namespace sends the code under test
to the LIVE per-Body working-memory.yaml instead of its sandbox. Two consequences,
and the second is the worse one:

  1. the test fails on state that never round-trips (measured: 17 of 35 in
     test_stale_sentinel_canary.py + test_per_goal_experience_check.py); and
  2. the run WRITES to the live Body WM, which is the payload merged at
     generalize-down. A sibling fixture did exactly that on cc-08, 2026-08-16 --
     it put `goals_completed_this_session: 0` over the canonical LIST slot and
     killed worker-loop Phase 4b for that Body.

THE FAILING TESTS THEMSELVES ARE NOT AN ADEQUATE PIN, which is the whole reason
this file exists. They only go red where BODY_WM_PATH happens to be exported --
a worker Body. On a reducer box, or in CI, they pass whether or not the scrub is
present, so the defect is invisible exactly where most runs happen. That
conditional coverage is how this survived at least four separate encounters
(g-115-4887, g-115-7389, g-115-5210, g-115-8319). This check is deterministic on
every box because it never reads the ambient environment at all.

STRUCTURAL, NOT TEXTUAL. The tuple is read with `ast` rather than grepped, so
reordering, reflowing or re-commenting the literal cannot break the pin and
cannot fake it either -- it asserts the VALUE, not the source text.

POSITIVE CONTROLS (guard-4166). The headline assertion is an ABSENCE ("no BODY_
key survives"), and an absence is exactly what a dead scanner also produces: a
zero-file discovery, a mis-parsed empty tuple, and a correct strip are
indistinguishable from the absence alone. So three positive-existence assertions
run alongside it -- a non-empty file population, a known-good prefix present in
every tuple, and a non-BODY key surviving the filter. Under a mutant that removes
"BODY_", the absence assertion flips red while all three controls stay green;
that asymmetry is the evidence the pin is live rather than vacuous.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
CONST = "_FRAMEWORK_ENV_PREFIXES"


def _tuples_by_file() -> dict[str, tuple[str, ...]]:
    """Every {CONST} literal in this directory, read structurally via ast."""
    found: dict[str, tuple[str, ...]] = {}
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if CONST not in names:
                continue
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                continue
            if isinstance(value, (tuple, list)):
                found[path.name] = tuple(str(v) for v in value)
    return found


class TestHermeticEnvBodyScrub(unittest.TestCase):

    def test_population_is_non_empty(self):
        """POSITIVE CONTROL: a zero-file scan would satisfy every other
        assertion here vacuously. Assert we actually found the helpers."""
        found = _tuples_by_file()
        self.assertGreater(
            len(found), 0,
            f"discovered no {CONST} definitions under {TESTS_DIR} -- the "
            "scanner is broken, so the BODY_ assertions below prove nothing",
        )

    def test_every_tuple_parsed_to_something_real(self):
        """POSITIVE CONTROL: a mis-parse yielding () would pass the BODY_
        check only if the check were written as a text search. Assert each
        tuple carries a prefix we know is there for an unrelated reason."""
        for name, prefixes in _tuples_by_file().items():
            self.assertIn(
                "MIND_", prefixes,
                f"{name}: {CONST} parsed to {prefixes!r}, which lacks the "
                "long-standing MIND_ entry -- the parse is wrong, not the data",
            )

    def test_every_tuple_strips_the_body_namespace(self):
        """THE ASSERTION UNDER TEST. Absence-shaped, so it is meaningful only
        beside the two controls above."""
        for name, prefixes in _tuples_by_file().items():
            self.assertIn(
                "BODY_", prefixes,
                f"{name}: {CONST} does not strip the BODY_ namespace. "
                "BODY_WM_PATH is the FIRST branch of wm.wm_path(), so a "
                "subprocess inheriting it writes the LIVE per-Body "
                "working-memory.yaml instead of the test sandbox (g-115-8338).",
            )

    def test_filter_actually_drops_body_keys_and_keeps_others(self):
        """Semantic half: the prefix tuple is only useful through the filter
        every _hermetic_env applies. Drive that filter over a synthetic env so
        the pin covers behaviour, not just a constant.

        The surviving-key assertion is the third positive control: a filter that
        dropped EVERYTHING would satisfy the BODY_ half perfectly."""
        synthetic = {
            "BODY_WM_PATH": "/live/working-memory.yaml",
            "BODY_ROLE": "worker",
            "PATH": "/usr/bin",
            "HOME": "/root",
        }
        for name, prefixes in _tuples_by_file().items():
            filtered = {
                k: v for k, v in synthetic.items() if not k.startswith(prefixes)
            }
            self.assertNotIn("BODY_WM_PATH", filtered, f"{name}: leaked BODY_WM_PATH")
            self.assertNotIn("BODY_ROLE", filtered, f"{name}: leaked BODY_ROLE")
            self.assertIn("PATH", filtered, f"{name}: over-stripped -- dropped PATH")
            self.assertIn("HOME", filtered, f"{name}: over-stripped -- dropped HOME")


if __name__ == "__main__":
    unittest.main()
