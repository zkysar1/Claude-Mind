#!/usr/bin/env python3
"""Tests for the narrative-daily-journal git merge driver ().

Covers the four verification outcomes on the goal:
  1. two boxes writing the same agent's same-day journal integrate cleanly
  2. union is justified per-section (deletions are NOT resurrected)
  3. no entry from either side is lost or duplicated
  4. a same-heading divergence on both sides still surfaces as a REAL conflict
Plus guard-526: '## ' inside a fenced code block is NOT document structure.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_DRIVER = _HERE.parent / "git-merge-journal-md.py"

_spec = importlib.util.spec_from_file_location("git_merge_journal_md", _DRIVER)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["git_merge_journal_md"] = _mod
_spec.loader.exec_module(_mod)

merge_sections = _mod.merge_sections
split_sections = _mod.split_sections
main = _mod.main


# --- the canonical  case -------------------------------------------

OURS = "# 2026-07-27\n\n## 02:58 — Goal: g-a\n\nours a\n\n## 03:36 — Goal: g-b\n\nours b\n"
THEIRS = "# 2026-07-27\n\n## 01:08 — Goal: g-c\n\ntheirs c\n\n## 01:24 — Goal: g-d\n\ntheirs d\n"


def test_addadd_unions_all_four_sections_chronologically():
    """The real incident: 4 sections, 0 markers, no record loss."""
    merged, conflicts = merge_sections("", OURS, THEIRS)
    assert conflicts == []
    # every section survives
    for h in ("01:08 — Goal: g-c", "01:24 — Goal: g-d",
              "02:58 — Goal: g-a", "03:36 — Goal: g-b"):
        assert "## " + h in merged, h
    # chronological, not ours-then-theirs
    order = [merged.index("## " + h) for h in
             ("01:08 — Goal: g-c", "01:24 — Goal: g-d",
              "02:58 — Goal: g-a", "03:36 — Goal: g-b")]
    assert order == sorted(order)
    # no conflict markers, preamble kept exactly once
    assert "<<<<<<<" not in merged and ">>>>>>>" not in merged
    assert merged.count("# 2026-07-27") == 1


def test_no_duplication_and_no_loss_of_bodies():
    merged, conflicts = merge_sections("", OURS, THEIRS)
    assert conflicts == []
    for body in ("ours a", "ours b", "theirs c", "theirs d"):
        assert merged.count(body) == 1, body


# --- outcome 4: same-heading divergence must NOT be coalesced ----------------

def test_same_heading_different_body_is_a_real_conflict():
    a = "## 09:00 — Goal: g-x\n\nours version\n"
    b = "## 09:00 — Goal: g-x\n\ntheirs version\n"
    merged, conflicts = merge_sections("", a, b)
    assert conflicts == ["09:00 — Goal: g-x"]
    # Markers are written into the RESULT (git driver contract), not withheld.
    assert "<<<<<<< ours" in merged and ">>>>>>> theirs" in merged
    assert "ours version" in merged and "theirs version" in merged


def test_conflict_still_unions_the_nonconflicting_sections():
    """A divergence in one section must not block auto-union of the others."""
    a = "## 09:00 — G\n\nours\n\n## 10:00 — only-ours\n\nkeep me\n"
    b = "## 09:00 — G\n\ntheirs\n\n## 11:00 — only-theirs\n\nkeep me too\n"
    merged, conflicts = merge_sections("", a, b)
    assert conflicts == ["09:00 — G"]
    assert "only-ours" in merged and "keep me" in merged
    assert "only-theirs" in merged and "keep me too" in merged
    assert merged.count("<<<<<<<") == 1, "only the diverging section is marked"


def test_same_heading_identical_body_is_kept_once():
    a = "## 09:00 — Goal: g-x\n\nsame\n"
    merged, conflicts = merge_sections("", a, a)
    assert conflicts == []
    assert merged.count("## 09:00 — Goal: g-x") == 1
    assert merged.count("same") == 1


# --- guard-526: fenced code blocks are not document structure ----------------

def test_hash_inside_fenced_block_is_not_a_heading():
    text = (
        "## 10:00 — Goal: g-y\n\n"
        "```bash\n"
        "## this is a shell comment, not a heading\n"
        "echo hi\n"
        "```\n\n"
        "tail of body\n"
    )
    pre, secs = split_sections(text)
    assert len(secs) == 1, "fenced '## ' must not split the section"
    assert "shell comment" in secs[0][1]
    assert "tail of body" in secs[0][1]


def test_fenced_hash_survives_a_merge_without_splitting():
    a = "## 10:00 — Goal: g-y\n\n```\n## not a heading\n```\n"
    b = "## 11:00 — Goal: g-z\n\nplain\n"
    merged, conflicts = merge_sections("", a, b)
    assert conflicts == []
    assert "## not a heading" in merged
    # the fenced line must not have become its own ordered section
    assert merged.index("## 10:00") < merged.index("## 11:00")


def test_tilde_fence_also_tracked():
    text = "## 10:00 — G\n\n~~~\n## fenced\n~~~\n"
    pre, secs = split_sections(text)
    assert len(secs) == 1


# --- deletion safety: union must not resurrect ------------------------------

def test_section_deleted_on_one_side_is_not_resurrected():
    base = "## 08:00 — Goal: g-old\n\nold body\n"
    ours = base + "## 09:00 — Goal: g-new\n\nnew body\n"
    theirs = "## 09:00 — Goal: g-new\n\nnew body\n"  # deleted g-old
    merged, conflicts = merge_sections(base, ours, theirs)
    assert conflicts == []
    assert "g-old" not in merged, "an intentional deletion must not be resurrected"
    assert "g-new" in merged


def test_addadd_has_no_base_so_nothing_is_treated_as_deleted():
    merged, conflicts = merge_sections("", OURS, THEIRS)
    assert conflicts == []
    assert merged.count("## ") == 4


# --- preamble handling -------------------------------------------------------

def test_differing_nonempty_preambles_conflict():
    a = "# title A\n\n## 09:00 — G\n\nx\n"
    b = "# title B\n\n## 09:00 — G\n\nx\n"
    merged, conflicts = merge_sections("", a, b)
    assert conflicts == ["<preamble>"]
    assert "<<<<<<< ours" in merged
    assert "title A" in merged and "title B" in merged


def test_empty_preamble_takes_the_other_side():
    a = "## 09:00 — G\n\nx\n"
    b = "# title\n\n## 10:00 — H\n\ny\n"
    merged, conflicts = merge_sections("", a, b)
    assert conflicts == []
    assert merged.startswith("# title")


# --- untimed headings keep stable relative order -----------------------------

def test_untimed_sections_sort_after_timed_and_keep_order():
    a = "## zzz last\n\na\n## 09:00 — G\n\nb\n"
    merged, conflicts = merge_sections("", a, "")
    assert conflicts == []
    assert merged.index("## 09:00") < merged.index("## zzz last")


# --- driver protocol: %A is the output, and is untouched on conflict --------

def test_main_writes_merged_result_to_ours_path(tmp_path):
    base = tmp_path / "base.md"; base.write_text("", encoding="utf-8")
    ours = tmp_path / "ours.md"; ours.write_text(OURS, encoding="utf-8")
    theirs = tmp_path / "theirs.md"; theirs.write_text(THEIRS, encoding="utf-8")
    rc = main(["drv", str(base), str(ours), str(theirs), "agents/x/journal/a.md"])
    assert rc == 0
    out = ours.read_text(encoding="utf-8")
    assert "theirs c" in out and "ours a" in out


def test_main_writes_markers_into_ours_on_conflict(tmp_path):
    a = "## 09:00 — G\n\nours\n"
    b = "## 09:00 — G\n\ntheirs\n"
    base = tmp_path / "base.md"; base.write_text("", encoding="utf-8")
    ours = tmp_path / "ours.md"; ours.write_text(a, encoding="utf-8")
    theirs = tmp_path / "theirs.md"; theirs.write_text(b, encoding="utf-8")
    rc = main(["drv", str(base), str(ours), str(theirs), "p.md"])
    assert rc == 1
    out = ours.read_text(encoding="utf-8")
    # git driver contract: best-effort result IS written, with markers, so the
    # human sees the divergence in the working tree instead of an ours-only file.
    assert "<<<<<<< ours" in out and ">>>>>>> theirs" in out
    assert "ours" in out and "theirs" in out


def test_missing_base_file_is_tolerated(tmp_path):
    ours = tmp_path / "ours.md"; ours.write_text(OURS, encoding="utf-8")
    theirs = tmp_path / "theirs.md"; theirs.write_text(THEIRS, encoding="utf-8")
    rc = main(["drv", str(tmp_path / "nope.md"), str(ours), str(theirs), "p.md"])
    assert rc == 0


def test_main_rejects_short_argv():
    assert main(["drv", "a"]) == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# --- fresh-eyes findings ( review) --------------------------------

def test_duplicate_heading_within_one_side_is_not_dropped():
    """F-001: dict(secs) kept only the LAST block for a repeated heading, so an
    earlier entry vanished inside a SINGLE side — the exact data loss this
    driver exists to prevent. Realistic: two goals closing in the same minute."""
    dup = "## 09:00 — Goal: g-x\n\nFIRST entry\n\n## 09:00 — Goal: g-x\n\nSECOND entry\n"
    merged, conflicts = merge_sections("", dup, "")
    assert conflicts == []
    assert "FIRST entry" in merged, "earlier duplicate-heading entry was dropped"
    assert "SECOND entry" in merged


def test_duplicate_heading_identical_on_both_sides_kept_once():
    dup = "## 09:00 — G\n\nA\n\n## 09:00 — G\n\nB\n"
    merged, conflicts = merge_sections("", dup, dup)
    assert conflicts == []
    assert merged.count("A") == 1 and merged.count("B") == 1


def test_duplicate_heading_diverging_across_sides_conflicts_with_both_kept():
    a = "## 09:00 — G\n\nA1\n\n## 09:00 — G\n\nA2\n"
    b = "## 09:00 — G\n\nB1\n"
    merged, conflicts = merge_sections("", a, b)
    assert conflicts == ["09:00 — G"]
    for frag in ("A1", "A2", "B1"):
        assert frag in merged, frag


def test_unreadable_theirs_refuses_instead_of_silently_dropping(tmp_path):
    """F-002: an unreadable side was collapsed to '' and reported a CLEAN rc=0
    merge, discarding that box's whole day. Refusing is never data loss."""
    base = tmp_path / "base.md"; base.write_text("", encoding="utf-8")
    ours = tmp_path / "ours.md"; ours.write_text(OURS, encoding="utf-8")
    missing_theirs = tmp_path / "not-there.md"
    rc = main(["drv", str(base), str(ours), str(missing_theirs), "p.md"])
    assert rc == 1, "a missing/unreadable %B must refuse, not merge to ours-only"
    # ours must be left intact for manual resolution
    assert "ours a" in ours.read_text(encoding="utf-8")


def test_undecodable_side_refuses(tmp_path):
    base = tmp_path / "base.md"; base.write_text("", encoding="utf-8")
    ours = tmp_path / "ours.md"; ours.write_text(OURS, encoding="utf-8")
    bad = tmp_path / "bad.md"; bad.write_bytes(b"\xff\xfe\x00\x00bad bytes")
    rc = main(["drv", str(base), str(ours), str(bad), "p.md"])
    assert rc == 1


# --- : two spurious-conflict defects, both live for weeks ----------
# Both were found while evaluating this driver's pure core as the merge handler
# for the knowledge-tree .md class. Each test below FAILS if its fix is
# reverted -- that is the point, so neither can pass vacuously.

def test_identical_section_with_differing_neighbours_merges_clean():
    """DEFECT A. split_sections reattaches a trailing blank line to the block
    that PRECEDES it, so byte-identical section content compares UNEQUAL raw
    whenever the two sides' NEIGHBOURING sections differ -- the normal
    cross-box case. The comparison must happen in _join form, which is already
    what the conflict emitter and final assembly use.

    base is DELIBERATELY empty. With a base that carries the section, the
    base-aware fast-forward branch (defect B's fix, below) rescues this input
    before the raw comparison is reached, and the pin passes even with THIS fix
    reverted -- measured, the first version of this test was vacuous for exactly
    that reason. An empty base is also the add/add daily-journal shape and the
    own-cloud mirror's real call shape, so this is the case that matters."""
    base = ""
    ours = "## A\n1\n\n## B\n2\n\n## C\n3\n"
    theirs = "## B\n2\n"

    # The defect is only pinned if the RAW forms genuinely differ here; if they
    # were equal this test would pass even with the fix reverted.
    ours_b = _mod._group(split_sections(ours)[1])["B"]
    theirs_b = _mod._group(split_sections(theirs)[1])["B"]
    assert ours_b != theirs_b, "precondition: raw blocks must differ for this to bite"
    assert _mod._join(ours_b) == _mod._join(theirs_b), "joined forms are identical"

    merged, conflicts = merge_sections(base, ours, theirs)
    assert conflicts == [], "identical content must not conflict on neighbour drift"
    assert "<<<<<<<" not in merged
    for marker in ("## A", "## B", "## C"):
        assert marker in merged


def test_one_sided_edit_fast_forwards_to_the_editing_side():
    """DEFECT B. The both-sides branch never consulted base_map, so ANY
    one-sided section edit conflicted even when the other side was provably
    unchanged since base. That is the EDITED-section shape: a ledger whose
    rows accumulate under a stable heading conflicted on every cross-box
    append."""
    merged, conflicts = merge_sections("## B\n2\n", "## B\n2 plus ours\n", "## B\n2\n")
    assert conflicts == [], "theirs == base, so ours holds the only edit"
    assert "2 plus ours" in merged
    assert "<<<<<<<" not in merged


def test_one_sided_edit_fast_forwards_mirror():
    """The other direction must behave identically -- a fix that only handles
    'ours edited' would be asymmetric, and this driver must be commutative."""
    merged, conflicts = merge_sections("## B\n2\n", "## B\n2\n", "## B\n2 plus theirs\n")
    assert conflicts == []
    assert "2 plus theirs" in merged


def test_fast_forward_branch_does_not_swallow_a_genuine_conflict():
    """CONTROL for defect B's fix: when NEITHER side matches base, both edited
    and the conflict must still surface. A fix that made everything merge
    clean would be worse than the bug."""
    merged, conflicts = merge_sections(
        "## B\nbase\n", "## B\nours edit\n", "## B\ntheirs edit\n")
    assert conflicts == ["B"]
    assert "<<<<<<<" in merged


def test_clean_merges_are_commutative():
    """The own-cloud mirror needs both boxes to independently compute the same
    bytes (unlike git, which converges on one commit). Commutativity is only
    meaningful on a CLEAN merge -- a conflict block embeds ours/theirs labels,
    so argument order legitimately changes that text."""
    cases = [
        ("## B\n2\n", "## A\n1\n\n## B\n2\n\n## C\n3\n", "## B\n2\n"),
        ("## B\n2\n", "## B\n2 plus ours\n", "## B\n2\n"),
        ("", "## 09:00 a\nours\n", "## 10:00 b\ntheirs\n"),
    ]
    for base, a, b in cases:
        fwd, cf = merge_sections(base, a, b)
        rev, cr = merge_sections(base, b, a)
        assert cf == [] and cr == [], "precondition: these cases must merge clean"
        assert sorted(x for x in fwd.split("\n") if x.strip()) == \
               sorted(x for x in rev.split("\n") if x.strip()), \
            "merge(a,b) and merge(b,a) must carry the same content"
