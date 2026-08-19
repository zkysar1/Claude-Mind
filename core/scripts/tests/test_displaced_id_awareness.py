"""Collision-reid displacement awareness at id-lookup time ().

WHY THIS EXISTS. `coordination_merge.py::_merge_id_keyed_jsonl` re-ids a record
when two boxes independently mint the same `guard-N`/`rb-N` for DIFFERENT
records. The merge preserves both RECORDS and stamps the loser
`displaced_from: <lost id>`. Nothing preserves REFERENCES to those records, so
a citation written before the merge keeps naming an id that now belongs to an
unrelated record.

That failure does not announce itself. The lookup does not 404 -- it returns a
well-formed, entirely unrelated record. Measured on this world 2026-08-18: 68
real displacement events, 12 ids resolving to unrelated content, ~435 live
citations of those 12, and one already-burned reader (the
directive-lane-series-bravo tree node carries a hand-written CORRECTION for
guard-3785 that blames its own authoring and abandons the knowledge, when the
successor guard-3786 was one `displaced_from` lookup away).

THE TWO-BRANCH ASSERTION IS THE LOAD-BEARING ONE. `rb_read` and `guard_read`
carry byte-identical id-lookup blocks. A fix applied to one and not the other
is exactly the half-fix shape guard-742 names, and it would pass any test that
only exercised the rb path.
"""
import pathlib
import re
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from mind_api.src.endpoints._jsonl_common import (  # noqa: E402
    displacement_notice, find_displacers, not_found_detail,
)

DISPLACED = {"id": "guard-3786", "rule": "A GOAL'S TITLE NAMES A METHOD",
             "displaced_from": "guard-3785"}
KEEPER = {"id": "guard-3785", "rule": "INSTANCE WARMTH IS NOT ARTIFACT AGE"}
UNRELATED = {"id": "guard-4000", "rule": "something else"}
ITEMS = [KEEPER, DISPLACED, UNRELATED]


def test_finds_the_record_displaced_off_an_id():
    assert [r["id"] for r in find_displacers(ITEMS, "guard-3785")] == ["guard-3786"]


def test_no_false_positive_on_an_undisplaced_id():
    assert find_displacers(ITEMS, "guard-4000") == []
    assert find_displacers(ITEMS, "guard-9999") == []


def test_tolerates_non_dict_lines():
    """_parse_jsonl survivors can include bare scalars; must not raise."""
    assert find_displacers(["a string", 42, None, DISPLACED], "guard-3785") == [DISPLACED]


def test_multiple_records_displaced_off_one_id():
    """Measured live: guard-1861 and rb-001 are each claimed by >1 record."""
    second = {"id": "guard-3900", "displaced_from": "guard-3785"}
    got = find_displacers([DISPLACED, second], "guard-3785")
    assert len(got) == 2
    notice = displacement_notice("guard-3785", got)
    assert "guard-3786" in notice and "guard-3900" in notice


def test_notice_names_the_successor_and_warns():
    n = displacement_notice("guard-3785", find_displacers(ITEMS, "guard-3785"))
    assert "guard-3786" in n
    assert "AMBIGUOUS" in n


def test_404_detail_names_successor_when_id_was_vacated():
    """A vacated id must not 404 over a recoverable answer."""
    orphan = [{"id": "rb-6000", "displaced_from": "rb-5999"}]
    d = not_found_detail("rb-5999", find_displacers(orphan, "rb-5999"))
    assert "rb-6000" in d and "displaced" in d


def test_404_detail_stays_plain_when_genuinely_absent():
    assert not_found_detail("rb-1", []) == "Record rb-1 not found"


def test_BOTH_id_branches_are_wired_not_just_one():
    """guard-742: rb_read and guard_read are separate byte-identical blocks."""
    src = (PROJECT_ROOT / "mind_api/src/world/reasoning_bank.py").read_text(
        encoding="utf-8")
    assert src.count("find_displacers(items, rec_id)") == 2, (
        "expected the displacement lookup in BOTH rb_read and guard_read")
    assert src.count("_displacement_notice") == 2
    assert src.count("not_found_detail(rec_id, displacers)") == 2


def test_annotation_does_not_mutate_the_shared_cache_record():
    """items come from the shared jsonl cache; annotating in place would leak
    the notice into every later reader of that record."""
    src = (PROJECT_ROOT / "mind_api/src/world/reasoning_bank.py").read_text(
        encoding="utf-8")
    # every annotation site must copy first
    for m in re.finditer(r"rec\[.__displacement_notice.\]", src):
        window = src[max(0, m.start() - 260):m.start()]
        assert "rec = dict(rec)" in window, (
            "annotation site does not copy the cached record first")
