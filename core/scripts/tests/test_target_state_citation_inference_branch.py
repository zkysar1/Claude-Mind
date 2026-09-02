"""test_target_state_citation_inference_branch.py —  regression test.

Covers the branch g-115-4447 OPENED and did not test. Companion to
test_target_state_citation_position.py, which pins the citation drop itself;
this file pins what happens to the goals that drop routes somewhere new.

THE BRANCH. `_citation_only_doc_paths` empties `target_files` when a goal's only
extracted path is a cited doc. `extract_and_infer_targets` then guards on:

    if ex["target_files"] or not ex["identifiers"]:
        return ex                                    # _target_state.py:1243

so a goal that used to short-circuit here (it HAD a target file) now falls
through into identifier inference — a filesystem walk it previously skipped.
None of g-115-4447's 12 tests reach past that line.

THE OUTCOME THAT WOULD BE A DEFECT is the cited doc coming back as a target by
the other door: dropped by position, re-inferred by co-occurrence. Two outcomes
are both FINE — no inferred target, or an inferred CODE target — and the tests
below keep those three apart rather than pinning one arbitrary answer.

WHY EACH GUARD GETS ITS OWN INPUT (guard-4637). Two guards are described as
protecting this, and it is tempting to write one pin and mutate both. That pin
would be VACUOUS, and measurably so:

  A. `_INFER_FILE_EXTS` is code-only (no `.md`), so a doc is never even opened.
  B. `_INFER_MIN_IDENTIFIERS = 2` + `_looks_like_class_name` gate whether
     inference runs at all.

A alone is sufficient to keep the doc out, on every input. So a doc-reappearance
pin passes with B fully reverted — B cannot decide that outcome, because A
returns first. Pinning both against the same assertion would report coverage
that does not exist. Instead each guard is pinned on the outcome IT decides:
A on "the doc is never a target", B on "inference runs at all". Both mutations
below are per-guard and each goes red on its own.

MEASURED, not assumed (alpha worker Body, cc-08, `uname -r` 6.8.0-137-generic,
2026-08-31), verification outcome 3 of the goal — the walk cost this branch adds:

    CITED  (falls into inference) : 802.7 ms   inferred=True,  2 targets found
    SCOPED (short-circuits)       :   0.0 ms   inferred=False, 1 target
    delta                          : ~803 ms, over PROJECT_ROOT

So the cost is real and roughly a second, bounded by `_INFER_MAX_FILES_PER_ROOT`
(2500). Recorded rather than acted on: the walk returned two genuine code
targets on that run, so it is buying something, and the probe is advisory. The
number is here so a future reader arguing either way starts from a measurement.
On that same real-tree run the cited doc was NOT returned.

MUTATION-PROVED, three times, each via core/scripts/mutation-proof-test.sh
(guard-1475: do not hand-roll) with --junit-xml so the RED is ATTRIBUTED to a
named test rather than inferred from an exit status. All three returned
verdict PASS / attribution "measured" / residue_check "clean":

  A  `.md` added to _INFER_FILE_EXTS
     -> test_doc_alone_in_the_root_is_not_returned RED, and the failure message
        names the doc it handed back:
        "['/tmp/.../core/config/conventions/temp-store.md'] assert not True"
  B  _INFER_MIN_IDENTIFIERS 2 -> 1
     -> test_one_identifier_infers_nothing RED ("assert True is False")
  C  the fall-through guard forced closed (`if True:` at _target_state.py:1243)
     -> test_acceptable_outcome_2_an_inferred_code_target RED

C exists because of guard-2435: A and B prove the two "must not" pins are not
vacuous and say NOTHING about whether the POSITIVE control was ever load-bearing.
It is — closing the branch this file is about makes it red. Each mutation targets
the single behavioural test for that guard, never the constant-assertion beside
it (`test_the_minimum_is_two` would go red under B tautologically and would
prove nothing about behaviour).

Cross-refs:
  - g-115-4712 (this test), g-115-4447 (the citation drop that opened the branch)
  - board msg-20260802-230307-echo-5962 (fresh-eyes finding on g-115-4447's diff)
  - _target_state.py: extract_and_infer_targets, _infer_targets_from_identifiers,
    _INFER_FILE_EXTS, _INFER_MIN_IDENTIFIERS, _looks_like_class_name
  - guard-4637 (per-fix mutation), guard-2257 ("X cannot happen" assertions must
    be shown to go red), guard-1475 (mutation-proof before trusting a pin)
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# Same lazy import pattern as test_target_state_citation_position.py.
TS_PATH = CORE_SCRIPTS / "_target_state.py"
spec = importlib.util.spec_from_file_location("_target_state", TS_PATH)
ts_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ts_mod)

extract_targets = ts_mod.extract_targets
extract_and_infer_targets = ts_mod.extract_and_infer_targets

CITED_DOC = "core/config/conventions/temp-store.md"

# Two class-shaped identifiers: `_looks_like_class_name` wants ^[A-Z][a-zA-Z0-9]{4,}$
# and `_CAMEL_RE` wants two humps, so both of these qualify and the pair clears
# `_INFER_MIN_IDENTIFIERS`.
TITLE = "Fix: the drain cycle stalls when StuckDetector and DrainCycle disagree"
DESC_CITED = (
    "The stall happens when StuckDetector reports idle while DrainCycle is "
    f"still advancing. See {CITED_DOC} for detail."
)
# One class-shaped identifier — below the minimum. Used to isolate guard B.
#
# The TITLE has to drop `DrainCycle` too, not just the description: extract_targets
# concatenates title + description, so a one-identifier body under the shared TITLE
# still yields two and inference runs anyway. Caught by this file's own test on its
# first run — the pin was asserting "no inference" against an input that had two
# identifiers all along, which is exactly the vacuous-precondition shape guard-4637
# asks to assert against in the body.
TITLE_ONE_IDENT = "Fix: the drain cycle stalls when StuckDetector reports idle"
DESC_ONE_IDENT = (
    "The stall happens when StuckDetector reports idle. "
    f"See {CITED_DOC} for detail."
)


def _seed_doc(root: Path) -> Path:
    """Write the cited doc, carrying BOTH identifiers.

    Load-bearing: it means guard B would PASS on this file, so only guard A's
    extension allowlist can be keeping it out. Without both identifiers here the
    guard-A mutation below would stay green for the wrong reason.
    """
    docs = root / "core" / "config" / "conventions"
    docs.mkdir(parents=True, exist_ok=True)
    p = docs / "temp-store.md"
    p.write_text(
        "# temp store\nStuckDetector and DrainCycle are both described here.\n",
        encoding="utf-8",
    )
    return p


def _seed_code(root: Path) -> Path:
    p = root / "drain_engine.py"
    p.write_text(
        "class Runner:\n"
        "    # StuckDetector and DrainCycle both live here\n"
        "    def go(self):\n"
        "        return 'StuckDetector', 'DrainCycle'\n",
        encoding="utf-8",
    )
    return p


class TestTheBranchIsActuallyReached:
    """Preconditions. If these fail, every assertion below is vacuous.

    guard-4637 asks for the isolating precondition to be asserted in the test
    body so a later edit that neutralises the case fails loudly instead of
    quietly passing. That is what this class is: it proves the citation drop
    really does route this goal past the short-circuit and into inference.
    """

    def test_citation_drop_empties_target_files(self):
        ex = extract_targets(TITLE, DESC_CITED)
        assert ex["target_files"] == [], ex["target_files"]

    def test_identifiers_survive_so_the_guard_does_not_short_circuit(self):
        ex = extract_targets(TITLE, DESC_CITED)
        assert "StuckDetector" in ex["identifiers"]
        assert "DrainCycle" in ex["identifiers"]
        # The literal guard at _target_state.py:1243 — False means "fall through".
        assert not (ex["target_files"] or not ex["identifiers"])

    def test_both_identifiers_are_class_shaped(self):
        """Guard B's own predicate, pinned directly.

        If `_looks_like_class_name` ever stops matching these, inference silently
        stops running and every test below passes for the wrong reason.
        """
        ex = extract_targets(TITLE, DESC_CITED)
        shaped = [i for i in ex["identifiers"] if ts_mod._looks_like_class_name(i)]
        assert len(shaped) >= ts_mod._INFER_MIN_IDENTIFIERS, shaped


class TestCitedDocIsNeverInferredBack:
    """GUARD A. The one outcome that is a defect.

    The regression this forbids, named per guard-2257: adding a doc extension to
    `_INFER_FILE_EXTS` (or removing the allowlist check) would let the walk open
    the cited doc, find both identifiers in it, and hand it back as a target —
    re-introducing by inference exactly the target g-115-4447 dropped by
    position. That mutation is applied in the mutation-proof run for this file
    and this class goes red under it.
    """

    def test_doc_alone_in_the_root_is_not_returned(self, tmp_path):
        """The isolating case: the doc is the ONLY file inference could find.

        No code file exists, so nothing else can win the match. If the doc comes
        back, it came back through the door this test guards.
        """
        _seed_doc(tmp_path)
        out = extract_and_infer_targets(TITLE, DESC_CITED, search_roots=[tmp_path])
        assert not any(str(p).endswith("temp-store.md") for p in out["target_files"]), \
            out["target_files"]
        assert out["target_files"] == []
        assert out["target_files_inferred"] is False

    def test_doc_is_not_returned_even_when_a_code_file_also_matches(self, tmp_path):
        """The realistic case: both files match; only the code file may win."""
        _seed_doc(tmp_path)
        _seed_code(tmp_path)
        out = extract_and_infer_targets(TITLE, DESC_CITED, search_roots=[tmp_path])
        assert not any(str(p).endswith("temp-store.md") for p in out["target_files"]), \
            out["target_files"]

    def test_the_allowlist_excludes_documentation_extensions(self):
        """Direct pin on guard A's mechanism, independent of any walk."""
        assert ".md" not in ts_mod._INFER_FILE_EXTS
        assert ".txt" not in ts_mod._INFER_FILE_EXTS
        assert ".py" in ts_mod._INFER_FILE_EXTS


class TestBothAcceptableOutcomesAreDistinguished:
    """The goal's outcome 2: two results are fine, one is not.

    These assert the ACCEPTABLE results positively, so the file cannot be read
    as "inference must never produce anything" — which would be a different and
    wrong contract, and would fight the g-248-56 feature this branch feeds.
    """

    def test_acceptable_outcome_1_no_inferred_target(self, tmp_path):
        """An empty root: nothing to infer, and that is a fine answer."""
        out = extract_and_infer_targets(TITLE, DESC_CITED, search_roots=[tmp_path])
        assert out["target_files"] == []
        assert out["target_files_inferred"] is False

    def test_acceptable_outcome_2_an_inferred_code_target(self, tmp_path):
        """A code file carrying both identifiers: inference may return it."""
        code = _seed_code(tmp_path)
        out = extract_and_infer_targets(TITLE, DESC_CITED, search_roots=[tmp_path])
        assert out["target_files_inferred"] is True
        assert [Path(p).name for p in out["target_files"]] == [code.name]
        assert out["inference_hits"][out["target_files"][0]] == 2
        # Inferred targets are medium-confidence, never high without line hints.
        assert out["confidence"] == "medium"


class TestIdentifierMinimumGovernsWhetherInferenceRuns:
    """GUARD B, pinned on the outcome it actually decides.

    Deliberately NOT pinned on doc-reappearance: guard A already blocks that on
    every input, so such a pin would pass with this guard fully reverted
    (guard-4637). What this guard decides is whether the walk runs at all, so
    that is what is asserted.

    The regression forbidden: lowering `_INFER_MIN_IDENTIFIERS` to 1 (or
    loosening `_looks_like_class_name`) makes a single incidental CamelCase word
    in a goal description enough to trigger a filesystem walk and attach an
    inferred target. That mutation is applied in the mutation-proof run and this
    class goes red under it.
    """

    def test_one_identifier_infers_nothing(self, tmp_path):
        # Precondition, asserted in-body (guard-4637): this input must really
        # carry exactly ONE class-shaped identifier, or the pin proves nothing.
        ex = extract_targets(TITLE_ONE_IDENT, DESC_ONE_IDENT)
        shaped = [i for i in ex["identifiers"] if ts_mod._looks_like_class_name(i)]
        assert shaped == ["StuckDetector"], shaped
        assert ex["target_files"] == [], "the branch must still be reached"

        _seed_code(tmp_path)
        out = extract_and_infer_targets(
            TITLE_ONE_IDENT, DESC_ONE_IDENT, search_roots=[tmp_path])
        assert out["target_files_inferred"] is False
        assert out["target_files"] == []

    def test_the_minimum_is_two(self):
        assert ts_mod._INFER_MIN_IDENTIFIERS == 2

    def test_a_file_matching_only_one_identifier_is_not_a_hit(self, tmp_path):
        """Co-occurrence, not any-match: one identifier in a file is not enough."""
        (tmp_path / "half.py").write_text(
            "# only StuckDetector appears here\n", encoding="utf-8")
        out = extract_and_infer_targets(TITLE, DESC_CITED, search_roots=[tmp_path])
        assert out["target_files_inferred"] is False


class TestNoSearchRootsIsUnchangedBehaviour:
    """The documented contract: empty search_roots means no walk, no extra cost.

    Pinned because this branch is the one that pays the ~803 ms measured above,
    and the cheap escape from it must keep working.
    """

    def test_empty_search_roots_returns_extract_targets_shape(self):
        out = extract_and_infer_targets(TITLE, DESC_CITED, search_roots=[])
        assert out["target_files"] == []
        assert out["target_files_inferred"] is False
        assert "inference_hits" not in out
