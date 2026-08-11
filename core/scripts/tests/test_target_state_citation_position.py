"""test_target_state_citation_position.py -  regression test.

Pins the contract added 2026-08-02 (echo):

  A `.md` path that appears ONLY in CITATION position ("see <doc>.md",
  "per <doc>.md", "documented in <doc>.md") is not an implementation
  TARGET, so extract_targets drops it from target_files.

WHY. Measured live during g-335-640 (alpha, cc-04, 2026-08-01): a new
product goal proposing a watchdog that exists in no form was REFUSED by
goal-duplication-gate with verdict already_present, hit_ratio 1.0. The
description ended with a courtesy citation of a convention the author had
written 20 minutes earlier in the same goal, and quoted a code symbol that
appears in that convention precisely BECAUSE the convention explains the
bug. One identifier, one target file, both present -> hit_ratio 1.0.

The failure scales with GOOD behaviour: an agent that encodes its diagnosis
into a durable store and then cites that store as evidence is doing exactly
what the citation conventions ask for, and is therefore the agent most
likely to be refused when it files the remedy.

VARIABLE ISOLATION (the originating goal changed two things at once and
could not say which mattered). Measured 2026-08-02, echo, cc-03 /
Linux 6.8.0-136-generic, against the production call shape:
    V0  symbol + path-citation  -> hit_ratio 1.0, BLOCK
    V1  drop the symbol only    -> PASS
    V2  reword the citation only-> PASS
Neither edit is individually NECESSARY; the block requires BOTH a path and
a symbol. So fixing either axis clears it, and position is the one that can
be fixed without asking authors to strip diagnostic content from goals
(which guard-1058 (b)/(e) explicitly warn against).

NOT guard-1058(e). That case has cited files AND a real target file, where
the aggregate outvotes the file that must change; its discriminator is
per_file_hits (target scores 0, cited files carry the hits). Here the cited
doc is the ONLY extracted target, so there is no target entry to compare
against and that discriminator has nothing to read.

SCOPE IS DELIBERATELY NARROW — the originating goal warned that an
over-broad doc exclusion would blind the probe to doc-targeted goals, which
are common in this fleet. Hence the positive controls below: they are the
half of this contract that keeps the fix honest.

Cross-refs:
  - g-115-4447 (this fix), g-335-640 (originating incident)
  - guard-1058(e), guard-1470 (fourth case), guard-865, guard-1731
  - _target_state.py:_citation_only_doc_paths + _CITATION_CUE_RE
  - g-001-191 / _DOCUMENTATION_ONLY_PATTERNS (the path-keyed sibling)
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# Same lazy import pattern as test_target_state.py.
TS_PATH = CORE_SCRIPTS / "_target_state.py"
spec = importlib.util.spec_from_file_location("_target_state", TS_PATH)
ts_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ts_mod)

extract_targets = ts_mod.extract_targets
_citation_only_doc_paths = ts_mod._citation_only_doc_paths

TITLE = "Idea: add a navigation watchdog that aborts a stalled drain cycle"

# Shared body. Carries a code symbol so target_files is the ONLY variable
# under test — without an identifier the gate skips for a different reason
# and the test would pass vacuously.
BODY = (
    "The drain cycle can stall when the downstream queue stops advancing. "
    "Nothing aborts it today. The stall is the one `cmd_temp_pressure` shows."
)


class TestCitationPositionDropped:
    """The  regression: a cited doc is not a target."""

    def test_see_citation_dropped(self):
        desc = BODY + " See core/config/conventions/temp-store.md for detail."
        out = extract_targets(TITLE, desc)
        assert "core/config/conventions/temp-store.md" not in out["target_files"]

    def test_per_citation_dropped(self):
        desc = BODY + " Per core/config/conventions/temp-store.md this recurs."
        out = extract_targets(TITLE, desc)
        assert "core/config/conventions/temp-store.md" not in out["target_files"]

    def test_documented_in_citation_dropped(self):
        desc = BODY + " Documented in core/config/conventions/temp-store.md."
        out = extract_targets(TITLE, desc)
        assert "core/config/conventions/temp-store.md" not in out["target_files"]

    def test_article_between_cue_and_path(self):
        """"documented in the <path>" — one article may intervene."""
        desc = BODY + " Documented in the core/config/conventions/temp-store.md file."
        out = extract_targets(TITLE, desc)
        assert "core/config/conventions/temp-store.md" not in out["target_files"]

    def test_identifier_survives_the_drop(self):
        """Dropping the path must not also strip the identifier.

        The identifier is the goal's diagnostic content; guard-1058(b) warns
        against rewrites that discard it. Only the TARGET is removed.
        """
        desc = BODY + " See core/config/conventions/temp-store.md for detail."
        out = extract_targets(TITLE, desc)
        assert "cmd_temp_pressure" in out["identifiers"]

    def test_line_hint_pruned_with_dropped_path(self):
        """A dropped path must not leave an orphaned line hint behind.

        line_hints are computed before the path filter; leaving one would
        make probe_target_state read a file that is not a target.
        """
        desc = BODY + " See core/config/conventions/temp-store.md:42 for detail."
        out = extract_targets(TITLE, desc)
        assert "core/config/conventions/temp-store.md" not in out["line_hints"]


class TestPositiveControls:
    """The half that keeps the fix from being an over-broad doc exclusion.

    Each of these MUST keep the path as a target. If any starts failing, the
    citation rule has grown too greedy and is blinding the probe to genuine
    doc-targeted goals.
    """

    def test_scope_positioned_doc_is_still_a_target(self):
        """The canonical doc-targeted goal: no citation cue, so it is scope."""
        desc = (
            "Add a Verified Values section to "
            "core/config/conventions/temp-store.md recording `cmd_temp_pressure`."
        )
        out = extract_targets(TITLE, desc)
        assert "core/config/conventions/temp-store.md" in out["target_files"]

    def test_cited_once_but_scoped_elsewhere_is_kept(self):
        """Mixed use -> the scope mention wins; EVERY occurrence must be cited."""
        desc = (
            "Edit core/config/conventions/temp-store.md to add `cmd_temp_pressure`. "
            "See core/config/conventions/temp-store.md for the current shape."
        )
        out = extract_targets(TITLE, desc)
        assert "core/config/conventions/temp-store.md" in out["target_files"]

    def test_code_path_in_citation_position_is_kept(self):
        """Scope limit: the rule is .md-only, so code targets are untouched."""
        desc = BODY + " See core/scripts/temp-pressure.py for the mechanism."
        out = extract_targets(TITLE, desc)
        assert "core/scripts/temp-pressure.py" in out["target_files"]

    def test_cue_in_a_prior_sentence_does_not_reach(self):
        """The cue must immediately precede the path.

        Without a proximity bound, one "see" anywhere in a long description
        would demote every .md path after it.
        """
        desc = (
            "See the discussion above for background on the stall. The change "
            "lands in core/config/conventions/temp-store.md and adds "
            "`cmd_temp_pressure` there."
        )
        out = extract_targets(TITLE, desc)
        assert "core/config/conventions/temp-store.md" in out["target_files"]


class TestHelperDirectly:
    """Unit-level contract on the helper, independent of extract_targets."""

    def test_returns_only_fully_cited_paths(self):
        text = (
            "See a/one.md for detail. Edit b/two.md now. See b/two.md too. "
            "See c/three.py for detail."
        )
        got = _citation_only_doc_paths(text)
        assert got == {"a/one.md"}, got

    def test_empty_text(self):
        assert _citation_only_doc_paths("") == set()
