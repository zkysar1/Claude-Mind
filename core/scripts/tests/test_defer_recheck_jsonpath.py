"""test_defer_recheck_jsonpath.py — regression test for .

Asserts that defer-recheck.py's 4th narrative-pattern handler (precon_jsonpath)
plus the multi-goal gated-on clause extractor (DEP_GATED_CLAUSE) recognize
their respective shapes and produce expected actions.

  precon_jsonpath: "precondition_unmet: <jsonpath><op><value>"
                   → "clear" when source resolves AND value satisfies condition
                   → "skipped" with diagnostic when source unavailable, path
                     missing, or condition not met.
  gated-clause:    "Gated on g-X <descriptor> + g-Y <descriptor>"
                   → _extract_dep_ids returns BOTH g-ids, excluding any
                     g-ids in parentheses outside the gating clause.

Cases covered (verification.outcomes (a) for g-115-342: PRECON_JSONPATH
handler implemented + tests):
  1. PRECON_JSONPATH_RE matches summary.treesRefined>0 form
  2. _try_precon_jsonpath clears when actual > expected (synthetic data
     via monkey-patched source resolver)
  3. _try_precon_jsonpath skips when actual does NOT satisfy
  4. _try_precon_jsonpath skips when path missing in source data
  5. _try_precon_jsonpath skips when source unavailable
  6. _walk_jsonpath returns _MISSING sentinel for missing keys (not None,
     which would collide with a real None value)
  7. _compare_numeric handles all six operators (>, <, >=, <=, ==, !=)
  8. _extract_dep_ids picks up BOTH g-ids in "gated on g-X + g-Y" form
  9. _extract_dep_ids EXCLUDES parenthetical g-ids outside the gating clause
 10. _try_new_patterns dispatcher attaches pattern="precon_jsonpath" field

Pattern: same importlib + sys.path shape as test_defer_recheck_patterns.py.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))


def _import_defer_recheck():
    """Load defer-recheck.py via importlib (hyphen-free attribute name)."""
    spec = importlib.util.spec_from_file_location(
        "defer_recheck_mod", CORE_SCRIPTS / "defer-recheck.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load spec for defer-recheck.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_precon_jsonpath_regex_matches_summary_form():
    mod = _import_defer_recheck()
    m = mod.PRECON_JSONPATH_RE.search(
        "precondition_unmet: summary.treesRefined>0 in any completed processor run post-deploy"
    )
    assert m is not None, "expected pattern to match summary.X>0 form"
    assert m.group(1) == "summary.treesRefined"
    assert m.group(2) == ">"
    assert m.group(3) == "0"


def test_precon_jsonpath_clears_when_condition_met(monkeypatch):
    mod = _import_defer_recheck()
    # Monkey-patch the source resolver to return synthetic processor-run data
    # with treesRefined=5 — satisfies summary.treesRefined>0.
    fake_data = {"summary": {"treesRefined": 5, "treesGenerated": 2}}
    monkeypatch.setattr(mod, "_resolve_jsonpath_source",
                        lambda path: (fake_data, "processor-run.summary"))
    r = mod._try_precon_jsonpath("precondition_unmet: summary.treesRefined>0")
    assert r is not None, "expected pattern to match"
    assert r["action"] == "clear", f"expected clear, got {r}"
    assert "summary.treesRefined=5" in r["reason"]
    assert "> 0" in r["reason"]


def test_precon_jsonpath_skips_when_condition_not_met(monkeypatch):
    mod = _import_defer_recheck()
    # Mirror current real state: treesRefined=0 fails the >0 condition.
    fake_data = {"summary": {"treesRefined": 0}}
    monkeypatch.setattr(mod, "_resolve_jsonpath_source",
                        lambda path: (fake_data, "processor-run.summary"))
    r = mod._try_precon_jsonpath("precondition_unmet: summary.treesRefined>0")
    assert r is not None
    assert r["action"] == "skipped"
    assert "does not satisfy" in r["reason"]
    assert "summary.treesRefined=0" in r["reason"]


def test_precon_jsonpath_skips_when_path_missing(monkeypatch):
    mod = _import_defer_recheck()
    # Source returns data but the requested path is absent — distinguish
    # from "source unavailable" so future readers can debug schema drift.
    fake_data = {"summary": {"treesGenerated": 0}}  # no treesRefined key
    monkeypatch.setattr(mod, "_resolve_jsonpath_source",
                        lambda path: (fake_data, "processor-run.summary"))
    r = mod._try_precon_jsonpath("precondition_unmet: summary.treesRefined>0")
    assert r is not None
    assert r["action"] == "skipped"
    assert "missing" in r["reason"]


def test_precon_jsonpath_skips_when_source_unavailable(monkeypatch):
    mod = _import_defer_recheck()
    # Source resolver returns (None, error_string) — handler must surface
    # the error in the skip reason for diagnosability.
    monkeypatch.setattr(mod, "_resolve_jsonpath_source",
                        lambda path: (None, "processor-run.sh exit=2; stderr=permission denied"))
    r = mod._try_precon_jsonpath("precondition_unmet: summary.treesRefined>0")
    assert r is not None
    assert r["action"] == "skipped"
    assert "source unavailable" in r["reason"]
    assert "permission denied" in r["reason"]


def test_walk_jsonpath_returns_sentinel_for_missing_keys():
    mod = _import_defer_recheck()
    data = {"summary": {"treesRefined": 0, "nullField": None}}
    # Real None must NOT collide with the missing sentinel — distinguishing
    # "explicit null" from "key absent" matters for diagnostic clarity.
    assert mod._walk_jsonpath(data, "summary.nullField") is None
    assert mod._walk_jsonpath(data, "summary.absent") is mod._MISSING
    assert mod._walk_jsonpath(data, "absent.path") is mod._MISSING


def test_compare_numeric_all_operators():
    mod = _import_defer_recheck()
    assert mod._compare_numeric(5, ">", 0) is True
    assert mod._compare_numeric(0, ">", 0) is False
    assert mod._compare_numeric(0, ">=", 0) is True
    assert mod._compare_numeric(-1, "<", 0) is True
    assert mod._compare_numeric(0, "<=", 0) is True
    assert mod._compare_numeric(5, "==", 5) is True
    assert mod._compare_numeric(5, "!=", 0) is True
    # Non-numeric actual: False (not raising) — caller already verified
    # that path resolved; non-numeric here means schema drift, surface as skip.
    assert mod._compare_numeric("not-a-number", ">", 0) is False
    assert mod._compare_numeric(None, ">", 0) is False


def test_extract_dep_ids_picks_up_both_in_gated_clause():
    mod = _import_defer_recheck()
    text = ("Gated on g-268-09 measurement results + g-271-12 BitNet capacity "
            "unlock. Naive prefetch implementation without both would worsen "
            "saturation cascade (g-115-330 sibling pattern). Auto-clears when both g")
    ids = mod._extract_dep_ids(text)
    # Both gating g-ids must be present.
    assert "g-268-09" in ids, f"expected g-268-09 in {ids}"
    assert "g-271-12" in ids, f"expected g-271-12 in {ids}"
    # Parenthetical g-id outside the gating clause must NOT be a dep —
    # it's a contextual reference, not a blocker.
    assert "g-115-330" not in ids, (
        f"g-115-330 is a parenthetical sibling pattern, not a dep. Got {ids}")


def test_extract_dep_ids_handles_single_gated_on_form():
    mod = _import_defer_recheck()
    # Single goal-id after "gated on" — must still extract.
    ids = mod._extract_dep_ids("Gated on g-268-09 only")
    assert "g-268-09" in ids


def test_try_new_patterns_dispatcher_attaches_jsonpath_field(monkeypatch):
    mod = _import_defer_recheck()
    fake_data = {"summary": {"treesRefined": 5}}
    monkeypatch.setattr(mod, "_resolve_jsonpath_source",
                        lambda path: (fake_data, "processor-run.summary"))
    r = mod._try_new_patterns("precondition_unmet: summary.treesRefined>0", {})
    assert r is not None, "dispatcher must match precon_jsonpath after other handlers fail"
    assert r.get("pattern") == "precon_jsonpath"
    assert r["action"] == "clear"


# Standalone runner — match the bare-pytest-style of sibling regression tests
# (no pytest dependency assumed in CI). monkeypatch is supplied as a tiny
# context-manager class to keep import-free parity with sibling tests.

class _MonkeyPatch:
    """Minimal monkey-patch context: setattr + tracked rollback."""

    def __init__(self):
        self._restore = []

    def setattr(self, obj, name, value):
        self._restore.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old in reversed(self._restore):
            setattr(obj, name, old)


def _run_with_mp(fn):
    mp = _MonkeyPatch()
    try:
        fn(mp)
    finally:
        mp.undo()


def _run_all():
    cases = [
        ("regex matches summary form",
            test_precon_jsonpath_regex_matches_summary_form),
        ("clears when condition met",
            lambda: _run_with_mp(test_precon_jsonpath_clears_when_condition_met)),
        ("skips when condition not met",
            lambda: _run_with_mp(test_precon_jsonpath_skips_when_condition_not_met)),
        ("skips when path missing",
            lambda: _run_with_mp(test_precon_jsonpath_skips_when_path_missing)),
        ("skips when source unavailable",
            lambda: _run_with_mp(test_precon_jsonpath_skips_when_source_unavailable)),
        ("walk_jsonpath returns sentinel for missing",
            test_walk_jsonpath_returns_sentinel_for_missing_keys),
        ("compare_numeric all operators",
            test_compare_numeric_all_operators),
        ("extract_dep_ids picks up both in gated clause",
            test_extract_dep_ids_picks_up_both_in_gated_clause),
        ("extract_dep_ids single gated form",
            test_extract_dep_ids_handles_single_gated_on_form),
        ("dispatcher attaches jsonpath pattern field",
            lambda: _run_with_mp(test_try_new_patterns_dispatcher_attaches_jsonpath_field)),
    ]
    failed = 0
    names = []
    for name, fn in cases:
        try:
            fn()
            names.append(name)
        except AssertionError as e:
            failed += 1
            print(f"FAIL: {name}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR: {name}: {type(e).__name__}: {e}")
    if failed == 0:
        print(f"TEST PASS: {len(cases)} cases — " + "; ".join(n.split()[0] for n in names))
    else:
        print(f"TEST FAIL: {failed}/{len(cases)} cases failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_run_all())
