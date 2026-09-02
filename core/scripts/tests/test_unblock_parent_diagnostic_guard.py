""": a DIAGNOSTIC parent completes by CONFIRMING the problem.

The sweep's core predicate (parent terminal -> child Unblock is moot) assumes a
parent completes by RESOLVING the blocking condition. A parent whose deliverable
is a measurement completes by confirming it, and for that class the predicate is
anti-correlated with what it detects.

These pin the guard's two directions. The FIRE direction protects live work; the
PASS-THROUGH direction is what keeps the guard from disabling the sweep, and it
is the half most likely to rot, so it carries the most cases.
"""
import importlib.util
import pathlib
import sys

# The sweep imports `_paths` at module scope (unblock-parent-status-sweep.py:120).
# exec_module below runs that import, and this file is ALSO run directly by
# run-invisible-suites.sh, where core/scripts/tests/conftest.py never loads — so
# without this insert the direct run dies at collection with
# ModuleNotFoundError: No module named '_paths' while pytest passes (conftest.py
# already inserts the same directory). Scoped to this module, matching the
# sibling spec_from_file_location tests. ()
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

_SPEC = importlib.util.spec_from_file_location(
    "ups", pathlib.Path(__file__).resolve().parents[1] / "unblock-parent-status-sweep.py")
ups = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ups)


def _guard(note):
    return ups._diagnostic_parent_guard("g-000-01", {"g-000-01": note})


class TestFires:
    def test_measured_incident_phrasing(self):
        # The literal shape from the confirmed instances: the parent says its
        # own unblocking did not happen because the dependent is still blocked.
        r = _guard("Ran the targeted sweep; timed out at the ceiling, 0 ok, no "
                   "run dir. Unblock outcome: N/A because the dependent goal "
                   "stays blocked.")
        assert r is not None
        assert "g-115-8586" in r
        assert "stays blocked" in r

    def test_case_insensitive(self):
        assert _guard("The dependent goal STAYS BLOCKED.") is not None

    def test_each_marker_fires(self):
        # Every marker must be reachable — a typo'd entry would silently never
        # match and the guard would be narrower than it reads.
        for marker in ups._PERSISTS_MARKERS:
            assert _guard(f"prefix {marker} suffix") is not None, marker


class TestPassesThrough:
    def test_absent_parent(self):
        assert ups._diagnostic_parent_guard("g-000-99", {}) is None

    def test_empty_note(self):
        assert _guard("") is None
        assert ups._diagnostic_parent_guard("g-000-01", {"g-000-01": None}) is None

    def test_ordinary_resolution_note(self):
        assert _guard("Raised the ceiling; the sweep now completes and the "
                      "downstream consumer is receiving output again.") is None

    def test_mentioning_a_fixed_failure_does_not_fire(self):
        # The guard must key on statements about the DEPENDENT, not on mood
        # words. A parent that describes a failure it FIXED is a normal
        # resolution and must still sweep its child.
        assert _guard("The run failed with an error at first; root cause was a "
                      "stale credential, fixed, and it now succeeds.") is None

    def test_unblocked_is_not_not_unblocked(self):
        # Substring hazard in the other direction: "unblocked" must not trip
        # the "not unblocked" marker.
        assert _guard("Dependent goal was unblocked by this change.") is None
