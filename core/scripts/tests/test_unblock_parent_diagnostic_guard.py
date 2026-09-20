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


# ---------------------------------------------------------------------------
#  /  (2026-09-20): the guard read the WRONG FIELD.
#
# `guard-5228` makes `aspirations-update-goal.sh outcome_note` a REPLACE, so
# the framework steers every append-only narrative into `progress_note` via
# `goal-field-append.sh`. This guard indexed `outcome_note` alone — the one
# field the convention discourages writing to — so a parent that recorded its
# non-discharge in the sanctioned place was invisible to it.
#
# NEITHER HALF OF THE FIX WORKS ALONE. Widening the field scope with the old
# marker list still matched only 2 of 311 terminal goals and missed this one;
# the new marker "is not proven" lives in the very field the old scope did not
# read. Both tested below, and the pairing is tested too.
# ---------------------------------------------------------------------------

_MEASURED_PROGRESS_SENTENCE = (
    "Still true and unchanged: nobody should launch to re-prove a static "
    "fact. End-to-end delivery into a live vessel's ReportApi is NOT proven "
    "by any of the above and closes inside this goal's own run."
)
# Same parent, unrelated side question — a first-person aside, NOT a statement
# about the dependent. It must not fire, which is why the marker is the
# declarative "is not proven" and not the bare "not proven".
_MEASURED_INCIDENTAL_ASIDE = (
    'So I have PROVEN "no EC2 instance is in the group" and I have NOT proven '
    "what else holds it — presumably the ALB's own ENIs."
)


def _index(outcome_note=None, progress_note=None):
    """Build the real index from one synthetic parent goal."""
    asp = {"id": "asp-000", "goals": [{"id": "g-000-01",
                                       "outcome_note": outcome_note,
                                       "progress_note": progress_note}]}
    return ups._build_parent_narrative_index([(asp, "world")])


class TestFieldScope:
    def test_statement_in_progress_note_fires(self):
        # SENSITIVITY: the measured instance's shape — the non-discharge is
        # recorded in progress_note while outcome_note says nothing about it.
        idx = _index(outcome_note="STAGE 2 IS MEASURED. p50 21.233s, n=20.",
                     progress_note=_MEASURED_PROGRESS_SENTENCE)
        r = ups._diagnostic_parent_guard("g-000-01", idx)
        assert r is not None
        assert "is not proven" in r

    def test_statement_in_outcome_note_still_fires(self):
        # SPECIFICITY TWIN: widening the scope must not cost the original path.
        idx = _index(outcome_note="The dependent goal stays blocked.",
                     progress_note="Routine progress.")
        assert ups._diagnostic_parent_guard("g-000-01", idx) is not None

    def test_outcome_note_alone_would_have_missed_it(self):
        # MUTATION PROOF (guard-385): reconstruct the PRE-FIX index — the old
        # builder's exact body — and assert it does NOT fire on the same goal.
        # Without this the two tests above would still pass if someone quietly
        # reverted the scope, because the marker alone looks sufficient.
        pre_fix_idx = {"g-000-01": "STAGE 2 IS MEASURED. p50 21.233s, n=20."}
        assert ups._diagnostic_parent_guard("g-000-01", pre_fix_idx) is None
        post_fix_idx = _index(
            outcome_note="STAGE 2 IS MEASURED. p50 21.233s, n=20.",
            progress_note=_MEASURED_PROGRESS_SENTENCE)
        assert ups._diagnostic_parent_guard("g-000-01", post_fix_idx) is not None

    def test_non_string_narrative_does_not_take_the_whole_sweep_down(self):
        # The index is built ONCE for the whole corpus, so an unhandled type
        # here aborts EVERY goal's evaluation, not just this record's — against
        # the module's fail-quiet/always-exit-0 contract. Pre-fix the same bad
        # record only broke its own parent lookup, so concatenating the two
        # fields WIDENS the blast radius unless both halves are coerced.
        for bad in (123, ["a"], {"k": "v"}):
            idx = _index(outcome_note=bad, progress_note=None)
            assert isinstance(idx["g-000-01"], str), bad
            assert ups._diagnostic_parent_guard("g-000-01", idx) is None, bad

    def test_marker_does_not_match_across_the_field_seam(self):
        # The "\n" join is load-bearing: a marker split across the two fields
        # exists in NEITHER of them and must not be synthesised by the join.
        idx = _index(outcome_note="... the result is not",
                     progress_note="proven by the run.")
        assert ups._diagnostic_parent_guard("g-000-01", idx) is None
        # Control: the same marker wholly inside one field still fires.
        whole = _index(outcome_note=None,
                       progress_note="the delivery is not proven by any of it")
        assert ups._diagnostic_parent_guard("g-000-01", whole) is not None

    def test_empty_narrative_through_the_builder_passes_through(self):
        # The builder joins the two fields with "\n", so a goal carrying
        # NEITHER note yields "\n" — truthy. The guard's emptiness test must
        # be on the STRIPPED value or every note-less parent would be read as
        # having said something.
        idx = _index(outcome_note=None, progress_note=None)
        assert idx["g-000-01"].strip() == ""
        assert ups._diagnostic_parent_guard("g-000-01", idx) is None


class TestIsNotProvenMarker:
    def test_declarative_form_fires(self):
        assert _guard(_MEASURED_PROGRESS_SENTENCE) is not None

    def test_first_person_aside_does_not_fire(self):
        # SPECIFICITY TWIN, and the reason the marker is not the bare
        # "not proven": this sentence is from the SAME parent note.
        assert _guard(_MEASURED_INCIDENTAL_ASIDE) is None

    def test_bare_not_proven_is_deliberately_not_a_marker(self):
        # Pins the CHOICE, not just the behaviour. If someone later adds
        # "not proven", the aside above starts firing and this fails loudly
        # rather than the discrimination silently eroding.
        assert "not proven" not in ups._PERSISTS_MARKERS
        assert "is not proven" in ups._PERSISTS_MARKERS
