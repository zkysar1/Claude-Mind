"""test_defer_citation_parity_patterns.py — regression test for .

defer-citation-parity-check.py asserts a dependency from a verb next to a goal
id. Its `after` alternative could not tell 'unblocks after g-X lands' (a future
dependency) from 'RE-DERIVED ... after the g-X close' (a past event the prose
only cites), so it flagged the past shape (g-326-01, g-115-8866, g-376-87) and
every reducer iteration paid a disposition for the same false positives.

Pinned here, on the module's own DEP_PATTERNS tuple (the one main() loops over):
  - the past-event shapes are NOT asserted (negative fixtures, the three live
    sentences and the optional-suffix id);
  - the future/present shapes ARE still asserted (positive fixtures, so the
    narrowing cannot zero the check, guard-4315);
  - the full suffixed id comes back, never a truncated one (the lookahead sits
    before the id group for that reason);
  - the other dependency verbs are untouched;
  - the one recorded cost of the narrowing: the noun form used in the future
    ('re-run after the g-X close') is no longer flagged.

defer-citation-parity-check.py has a hyphenated filename, so it is loaded with
spec_from_file_location, the same shape as test_defer_recheck_patterns.py.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent


def _load():
    path = CORE_SCRIPTS / "defer-citation-parity-check.py"
    spec = importlib.util.spec_from_file_location("defer_citation_parity_under_test", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


def _asserted(text: str) -> list[str]:
    """The ids main() would treat as dependency-asserted, in first-seen order."""
    out: list[str] = []
    for pat in MOD.DEP_PATTERNS:
        for m in pat.finditer(text):
            if m.group(1) not in out:
                out.append(m.group(1))
    return out


@pytest.mark.parametrize("text", [
    # the three live past-event sentences
    "RE-DERIVED 2026-09-24T02:26Z (foxtrot, both axes) after the g-326-871 close",
    "Re-probed 2026-09-04 against the store after the g-369-112 close fired a "
    "defer-invalidation advisory",
    "Re-derived the dependency after g-376-120 closed",
    # the other past forms the alternative excludes
    "checked after the g-1-2 closure",
    "rebased after g-1-2 landed",
    "re-measured after g-1-2 shipped",
    "after the goal g-1-2 was closed",
    "after g-1-2 had merged",
    # the optional suffix must not let the engine match a truncated id
    "after g-335-1704-a closed",
])
def test_past_event_is_not_a_dependency(text):
    assert _asserted(text) == []


@pytest.mark.parametrize("text,ids", [
    ("unblocks after g-376-120 lands", ["g-376-120"]),
    ("clears after g-376-120 ships", ["g-376-120"]),
    ("resume after g-376-120 closes", ["g-376-120"]),
    ("retry after the goal g-376-120 completes", ["g-376-120"]),
    ("re-run after g-376-120", ["g-376-120"]),
    ("AFTER THE G-376-120 LANDS", ["G-376-120"]),
    # the full suffixed id comes back, not 
    ("after g-335-1704-a lands", ["g-335-1704-a"]),
])
def test_future_or_present_after_is_still_a_dependency(text, ids):
    assert _asserted(text) == ids


@pytest.mark.parametrize("text,ids", [
    ("once g-1-2 lands", ["g-1-2"]),
    ("waiting on g-1-2 close", ["g-1-2"]),
    ("blocked by the goal g-1-2", ["g-1-2"]),
    ("g-1-2 ships next week", ["g-1-2"]),
])
def test_other_dependency_verbs_are_untouched(text, ids):
    assert _asserted(text) == ids


def test_recorded_cost_noun_form_future_use_is_not_flagged():
    # The ambiguity the narrowing resolves toward 'not asserted'. If this is ever
    # judged the wrong side, flip it HERE and in DEP_AFTER_ID together.
    assert _asserted("re-run after the g-376-120 close") == []


def test_after_is_no_longer_inside_dep_verb_before():
    # A bare `after` in DEP_VERB_BEFORE would re-assert the past shapes and make the
    # lookahead in DEP_AFTER_ID dead code.
    assert MOD.DEP_VERB_BEFORE.search("RE-DERIVED after the g-326-871 close") is None
    assert MOD.DEP_VERB_BEFORE.search("unblocks after g-376-120 lands") is None
