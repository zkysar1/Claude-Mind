"""Tests for guardrail-retire truth-event capture ( outcome 2).

The confidence-calibration ledger had exactly ONE capture surface --
adjudication-lane.py::cmd_resolve. That surface's SCOPE_STORES are
reasoning_bank + guardrails, and the measured problem g-306-399 recorded is that
declared confidence is nearly absent from both (0.67% and 0.06%), so most of its
rows carry `declared_confidence: null`. This is the second surface.

WHAT IS PINNED HERE, and why each one is a real regression rather than a
restatement of the code:

  1. The verdict MAPPING, including `revise -> revised` and NOT `refuted`. The
     sibling surface shipped with an inverted mapping (g-115-9063) that wrote
     "survived" on every row where the entry was WRONG, producing a ledger from
     which a calibration curve could only ever report perfect calibration. The
     inverse mistake is just as available here.
  2. The ONE exclusion: a `retire` with no reason. g-306-399 excludes
     utilization-only retirements ("popularity is not truth") and this lane's
     retire verdict is staleness/relevance-scored. A retire that CITES a reason
     is a content judgement and is kept -- so the exclusion is on the evidence,
     never on the verdict name.
  3. That keep/refresh still produce rows. They are the SURVIVED denominator; a
     ledger recording only refutations is the g-115-9063 defect pointing the
     other way.
  4. WIRING (guard-1943: a passing unit test proves the function, never the
     wiring). The mapping being correct says nothing about whether anything
     calls it, or about WHERE. Placement is load-bearing here: `apply()` returns
     a mutation PLAN and never writes (guard-832), so a recorder there would log
     verdicts that were merely COMPUTED -- including plans the wrapper then
     failed to execute. That is guard-4238's adjacent trap, and the assertions
     below pin the recorder AFTER the mutation loop and behind exec_rc.
"""
import importlib.util
import re
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]

_SPEC = importlib.util.spec_from_file_location(
    "guardrail_retire_for_test", _SCRIPTS / "guardrail_retire.py")
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)

# Imported rather than restated so this test fails loudly if the two
# vocabularies ever diverge (same posture as the adjudication-lane sibling).
_LEDGER_SPEC = importlib.util.spec_from_file_location(
    "_confidence_ledger_for_test", _SCRIPTS / "_confidence_ledger.py")
_ledger = importlib.util.module_from_spec(_LEDGER_SPEC)
_LEDGER_SPEC.loader.exec_module(_ledger)

WRAPPER = (_SCRIPTS / "guardrail-retire.sh").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 1. the mapping
# --------------------------------------------------------------------------

def test_keep_and_refresh_score_the_entry_as_survived():
    """POSITIVE CONTROL for this whole file (guard-2421): without it, a function
    hardcoded to return None would satisfy every exclusion assertion below."""
    assert mod.truth_event_for("keep", "") == ("survived", None)
    assert mod.truth_event_for("refresh", "") == ("survived", None)


def test_revise_is_revised_not_refuted():
    """`_verdict_mutations` calls revise "still relevant, just stale-worded" --
    the CLAIM stood, the wording did not. Collapsing it into `refuted` would
    score a surviving entry as a failure and bias the curve pessimistic."""
    assert mod.truth_event_for("revise", "")[0] == "revised"
    assert mod.truth_event_for("revise", "wording drifted")[0] == "revised"


def test_retire_with_a_cited_reason_is_refuted_and_keeps_the_evidence():
    reason = "superseded by guard-6019; its prescription does not work"
    assert mod.truth_event_for("retire", reason) == ("refuted", reason)


def test_every_mapped_verdict_is_in_the_ledger_vocabulary():
    """The ledger silently rewrites an unknown verdict to "unknown"
    (record_truth_event: `verdict if verdict in VERDICTS else "unknown"`), so a
    typo here would not raise -- it would quietly fill the ledger with
    unusable rows. Checked against the imported constant, not a copy."""
    for v in ("keep", "refresh", "revise", "retire"):
        ev = mod.truth_event_for(v, "a stated reason")
        assert ev is not None, v
        assert ev[0] in _ledger.VERDICTS, (v, ev[0], _ledger.VERDICTS)


# --------------------------------------------------------------------------
# 2. the one exclusion
# --------------------------------------------------------------------------

def test_bare_retire_is_excluded_as_utilization_only():
    """The goal's own exclusion. This lane's retire is staleness/relevance
    scored, so a retire that cites nothing is the utilization-only shape."""
    assert mod.truth_event_for("retire", "") is None
    assert mod.truth_event_for("retire", None) is None


def test_whitespace_reason_does_not_launder_a_bare_retire():
    """A reason of spaces is not evidence. Without the strip() this is the
    trivial bypass of the exclusion above."""
    assert mod.truth_event_for("retire", "   \t \n ") is None


def test_unknown_verdict_is_skipped_not_guessed():
    for v in ("", None, "bogus", "RETIRE", "delete"):
        assert mod.truth_event_for(v, "reason") is None, v


# --------------------------------------------------------------------------
# 3. wiring — placement is the property, not existence
# --------------------------------------------------------------------------

def test_wrapper_calls_the_shared_recorder_and_the_pure_mapper():
    """A scoped CALL into both shared components, never a second implementation
    of either (guard-2676)."""
    assert "from guardrail_retire import truth_event_for" in WRAPPER
    assert "from _confidence_ledger import record_truth_event" in WRAPPER
    # The mapping must not be re-typed into the wrapper alongside the import.
    assert "'survived'" not in WRAPPER, "verdict mapping duplicated into the wrapper"


def test_capture_runs_after_the_mutation_loop_and_only_on_success():
    """guard-4238's adjacent trap: a recorder ahead of the mutator it depends on
    records intent, not outcome. `apply()` returns a PLAN, so the only honest
    capture point is after the wrapper's mutation loop has run and succeeded."""
    loop_end = WRAPPER.index('done <<< "$MUTS"')
    gate = WRAPPER.index('[ "$exec_rc" = "0" ]')
    # Anchor on the IMPORT, not on a bare "record_truth_event" scan: the comment
    # block above the call names the function in prose, and the first hit is
    # therefore the comment rather than the call. Measured while writing this
    # test -- it failed against correct code. Same shape as scanning a docstring
    # that names the very token it promises the code does not use.
    call = WRAPPER.index("from _confidence_ledger import record_truth_event")
    assert loop_end < gate < call, (
        f"capture must follow the mutation loop and its exec_rc gate; "
        f"loop_end={loop_end} gate={gate} call={call}")


def test_capture_is_apply_only_never_restore():
    """restore un-does a prior verdict; it is not a fresh judgement, and
    recording it would double-count the entry."""
    assert '[ "$CMD" = "apply" ]' in WRAPPER


def test_engine_apply_does_not_record():
    """The placement property from the other side: if a future edit moves
    capture into the engine, this fails even though every mapping test above
    would still pass."""
    engine = (_SCRIPTS / "guardrail_retire.py").read_text(encoding="utf-8")
    start = engine.index("def apply(")
    end = engine.index("\ndef ", start + 1)
    assert "record_truth_event" not in engine[start:end]


def test_reason_travels_by_env_not_shell_interpolation():
    """guard-165. A --reason carrying a quote or a $( would otherwise break the
    heredoc or execute inside it."""
    assert "MIND_GR_REASON=" in WRAPPER
    assert re.search(r"os\.environ\.get\(\s*'MIND_GR_REASON'", WRAPPER)
    assert "$_gr_reason\"" not in WRAPPER.split("$_PY -c")[-1], (
        "reason interpolated into the python source")
