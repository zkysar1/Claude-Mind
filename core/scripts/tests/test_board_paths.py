"""test_board_paths.py — regression guard for the board reader-side path-list
seam (`core/scripts/_board_paths.py`, g-358-110).

WHY THIS SUITE EXISTS. The seam's whole job is to stop a segmented/evicting
board from SILENTLY starving a reader. Every failure it guards is silent by
construction — a short window served as a full one — so nothing downstream
reports it. `_gate_log.py` states the ordering constraint this implements:
"a false all-clear is the worst available failure direction, which is why this
seam lands BEFORE any writer change." On the board the blast radius is the
fleet's own claim path: a reader that misses a partner's claim double-executes.

THE LOAD-BEARING ARM IS `test_eviction_*`. Under date segmentation, archival
becomes "drop a whole old segment", so a path enumerated microseconds ago can be
gone by the time it is opened — a reader must tolerate a segment DISAPPEARING
between two reads, not merely appearing. That arm carries a MUTATION CONTROL
(guard-3928: mutate a COPY, never the production file): the same scenario run
against a copy of the seam with the eviction handling stripped must RAISE. A
test that passes against both the real module and a broken one is testing
nothing, and that is exactly the shape a green suite hides.

Self-contained: every case builds its own board directory under tmp_path and
never touches the live world.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _board_paths as bp  # noqa: E402

MODULE_SRC = SCRIPTS / "_board_paths.py"


# ---------------------------------------------------------------- helpers ---

def _write(path: Path, records, trailing_newline: bool = True) -> Path:
    """Write records as JSONL. `trailing_newline=False` reproduces the shape
    guard-6846 measured on real board dumps (last byte `}`, no final \\n)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(r) for r in records)
    if trailing_newline:
        body += "\n"
    path.write_text(body, encoding="utf-8")
    return path


def _rec(mid: str, ts: str) -> dict:
    return {"id": mid, "timestamp": ts, "author": "alpha", "text": mid}


# ------------------------------------------------------- the path list -----

def test_order_is_archive_then_live_then_segments_oldest_first(tmp_path):
    board = tmp_path / "board"
    _write(board / "findings-archive.jsonl", [_rec("a1", "2026-08-01T00:00:00")])
    _write(board / "findings.jsonl", [_rec("l1", "2026-09-01T00:00:00")])
    # Written out of order on purpose: enumeration must sort, not trust the FS.
    _write(board / "findings-2026-09-12.jsonl", [_rec("s2", "2026-09-12T00:00:00")])
    _write(board / "findings-2026-09-03.jsonl", [_rec("s1", "2026-09-03T00:00:00")])

    names = [p.name for p in bp.channel_paths(board, "findings")]
    assert names == [
        "findings-archive.jsonl",
        "findings.jsonl",
        "findings-2026-09-03.jsonl",
        "findings-2026-09-12.jsonl",
    ], names


def test_sibling_stores_are_never_admitted_as_segments(tmp_path):
    """The exclusions are measured, not hypothetical.

    In the live $WORLD_PATH/board (alpha, cc-04, 2026-09-17) a loose
    `<channel>-*.jsonl` glob admits `-archive.jsonl`, `-archive-archive.jsonl`
    and `-reads.jsonl`. `-reads.jsonl` is the READ-RECEIPT store that backs
    `--unread-only`; admitting it would inject receipts into a message read as
    though they were posts.
    """
    board = tmp_path / "board"
    _write(board / "coordination.jsonl", [_rec("live", "2026-09-10T00:00:00")])
    _write(board / "coordination-reads.jsonl", [{"id": "receipt", "agent": "alpha"}])
    _write(board / "coordination-archive-archive.jsonl", [])
    _write(board / "coordination-notes.jsonl", [_rec("x", "2026-09-10T00:00:00")])
    # POSITIVE CONTROL: a real date segment in the same directory IS admitted,
    # so a green result cannot come from the matcher rejecting everything.
    _write(board / "coordination-2026-09-11.jsonl", [_rec("seg", "2026-09-11T00:00:00")])

    names = [p.name for p in bp.channel_paths(board, "coordination")]
    assert "coordination-2026-09-11.jsonl" in names           # the control
    assert "coordination-reads.jsonl" not in names
    assert "coordination-archive-archive.jsonl" not in names
    assert "coordination-notes.jsonl" not in names
    assert names == ["coordination.jsonl", "coordination-2026-09-11.jsonl"], names


def test_archive_can_be_skipped_for_the_cheap_in_window_read(tmp_path):
    board = tmp_path / "board"
    _write(board / "findings-archive.jsonl", [_rec("a1", "2026-08-01T00:00:00")])
    _write(board / "findings.jsonl", [_rec("l1", "2026-09-01T00:00:00")])
    assert [p.name for p in bp.channel_paths(board, "findings", include_archive=False)] \
        == ["findings.jsonl"]


def test_enumeration_is_fresh_so_a_new_segment_appears(tmp_path):
    board = tmp_path / "board"
    _write(board / "findings.jsonl", [_rec("l1", "2026-09-01T00:00:00")])
    first = [p.name for p in bp.channel_paths(board, "findings")]
    _write(board / "findings-2026-09-16.jsonl", [_rec("s1", "2026-09-16T00:00:00")])
    second = [p.name for p in bp.channel_paths(board, "findings")]
    assert first == ["findings.jsonl"]
    assert second == ["findings.jsonl", "findings-2026-09-16.jsonl"]


def test_unresolved_board_dir_is_loud_and_empty(capsys):
    assert bp.channel_paths(None, "findings") == []
    err = capsys.readouterr().err
    assert "board_dir unresolved" in err, err


# ----------------------------------------------------------- reading -------

def test_records_are_parsed_per_file_never_concatenated(tmp_path):
    """guard-6846: board JSONL does not reliably end with a newline; byte-
    concatenating two files glues the last record of one onto the first of the
    next, which raises `Extra data` and stops the scan at the FIRST boundary."""
    board = tmp_path / "board"
    _write(board / "findings.jsonl",
           [_rec("l1", "2026-09-01T00:00:00"), _rec("l2", "2026-09-02T00:00:00")],
           trailing_newline=False)
    _write(board / "findings-2026-09-03.jsonl",
           [_rec("s1", "2026-09-03T00:00:00")], trailing_newline=False)

    recs, missing = bp.read_paths(bp.channel_paths(board, "findings"))
    assert missing == []
    assert [r["id"] for r in recs] == ["l1", "l2", "s1"]


def test_malformed_line_does_not_take_out_the_channel(tmp_path):
    board = tmp_path / "board"
    p = board / "findings.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(_rec("ok1", "2026-09-01T00:00:00")) + "\n"
        + "{not json at all\n"
        + json.dumps(_rec("ok2", "2026-09-02T00:00:00")) + "\n",
        encoding="utf-8")
    recs, missing = bp.read_paths([p])
    assert [r["id"] for r in recs] == ["ok1", "ok2"]
    assert missing == []


# -------------------------------------------------- eviction (load-bearing) -

def _eviction_scenario(board: Path):
    """Enumerate, then DELETE a segment before reading — the rolling-window
    behaviour measured on this store. Returns (paths, victim)."""
    _write(board / "findings.jsonl", [_rec("l1", "2026-09-01T00:00:00")])
    _write(board / "findings-2026-09-02.jsonl", [_rec("s1", "2026-09-02T00:00:00")])
    _write(board / "findings-2026-09-03.jsonl", [_rec("s2", "2026-09-03T00:00:00")])
    paths = bp.channel_paths(board, "findings")
    victim = board / "findings-2026-09-02.jsonl"
    victim.unlink()                      # evicted between enumeration and read
    return paths, victim


def test_eviction_between_enumeration_and_read_is_reported_not_raised(tmp_path):
    board = tmp_path / "board"
    paths, victim = _eviction_scenario(board)

    recs, missing = bp.read_paths(paths)          # must not raise

    assert [r["id"] for r in recs] == ["l1", "s2"], recs
    assert [m["path"] for m in missing] == [str(victim)], missing
    assert [m["reason"] for m in missing] == ["evicted"], missing
    note = bp.coverage_note(recs, missing)
    assert "evicted mid-read" in note and "NOT fully covered" in note, note


def test_present_but_unreadable_is_a_fault_not_an_eviction(tmp_path):
    """The two reasons must stay distinguishable at the RESULT, not only in
    intent — that separation is what the mutation control below measures, and
    conflating them makes an I/O fault on a live segment read as routine
    archival. A directory triggers the OSError branch on any platform and
    without depending on the runner's uid (as root, chmod 000 still reads)."""
    board = tmp_path / "board"
    _write(board / "findings.jsonl", [_rec("l1", "2026-09-01T00:00:00")])
    bogus = board / "findings-2026-09-02.jsonl"
    bogus.mkdir()                      # present, openable-as-file it is not

    recs, missing = bp.read_paths([board / "findings.jsonl", bogus])

    assert [r["id"] for r in recs] == ["l1"]
    assert [m["reason"] for m in missing] == ["unreadable"], missing
    note = bp.coverage_note(recs, missing)
    assert "UNREADABLE (fault, not archival)" in note, note


def test_eviction_arm_fails_against_a_mutant_with_the_handling_removed(tmp_path):
    """MUTATION CONTROL (guard-3928 — mutate a COPY, never the production file).

    Strips the eviction branch from a COPY of the seam and re-runs the exact
    scenario above. The copy must get the answer WRONG. If it did not, the arm
    above would be passing for some other reason and the eviction handling would
    be unproven.
    """
    src = MODULE_SRC.read_text(encoding="utf-8")
    anchor = """        except FileNotFoundError as exc:
            # The segment was evicted between enumeration and open. Expected
            # under a rolling window; not a fault.
            missing.append({"path": str(p), "reason": "evicted", "error": str(exc)})
            continue
"""
    # Assert the anchor EXISTS before mutating. A silently-missed replacement
    # would produce a mutant identical to the original, and this control would
    # then "pass" while proving nothing — the failure this whole test is about.
    assert src.count(anchor) == 1, "eviction-handling anchor not found verbatim"
    mutant_src = src.replace(anchor, "")

    mut_dir = tmp_path / "mutant"
    mut_dir.mkdir()
    mut_path = mut_dir / "_board_paths_mutant.py"
    mut_path.write_text(mutant_src, encoding="utf-8")

    spec = importlib.util.spec_from_file_location("_board_paths_mutant", mut_path)
    mutant = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(spec and mutant or mutant)

    board = tmp_path / "board_mut"
    paths, victim = _eviction_scenario(board)

    # THE MUTANT MISCLASSIFIES rather than raising, and that is the whole point.
    # `FileNotFoundError` is a SUBCLASS of `OSError`, so deleting the eviction
    # branch does not make the read blow up — the vanished path silently falls
    # through to the fault branch. An arm asserting `pytest.raises` here PASSES
    # against the real module too and therefore proves nothing; this control
    # measures the CLASSIFICATION, which is the behaviour that actually differs.
    mut_recs, mut_missing = mutant.read_paths(paths)
    assert [m["reason"] for m in mut_missing] == ["unreadable"], mut_missing

    # The production module, on the IDENTICAL input in the SAME test, classifies
    # it as eviction. One scenario, two arms, one comparison.
    recs, missing = bp.read_paths(paths)
    assert [r["id"] for r in recs] == ["l1", "s2"]
    assert [m["reason"] for m in missing] == ["evicted"], missing
    assert [m["path"] for m in missing] == [str(victim)]


# ------------------------------------------------------- discontinuity -----

def test_continuity_gaps_finds_a_missing_middle_day(tmp_path):
    recs = [_rec("a", "2026-09-01T10:00:00"),
            _rec("b", "2026-09-04T10:00:00")]
    assert bp.continuity_gaps(recs) == ["2026-09-02", "2026-09-03"]
    note = bp.coverage_note(recs)
    assert "DISCONTINUOUS" in note and "2026-09-02" in note, note


def test_continuity_gaps_is_quiet_on_a_continuous_window():
    recs = [_rec("a", "2026-09-01T10:00:00"),
            _rec("b", "2026-09-02T10:00:00"),
            _rec("c", "2026-09-03T10:00:00")]
    assert bp.continuity_gaps(recs) == []
    assert bp.coverage_note(recs) == ""


def test_continuity_gaps_needs_a_span():
    assert bp.continuity_gaps([]) == []
    assert bp.continuity_gaps([_rec("a", "2026-09-01T10:00:00")]) == []


def test_continuity_gaps_survives_junk_timestamps():
    recs = [{"id": "x"}, {"id": "y", "timestamp": None},
            _rec("a", "2026-09-01T10:00:00"), _rec("b", "2026-09-03T10:00:00")]
    assert bp.continuity_gaps(recs) == ["2026-09-02"]
