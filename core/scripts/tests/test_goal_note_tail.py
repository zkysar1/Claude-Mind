"""Unit tests for goal-note-tail ().

The script exists because guard-2043 forbids a TRUNCATED read of an append-ordered
field -- the corrective block is at the END -- which left a claimer of one of the 41
over-cap goals with only "read 100+ KB whole" or "release it unstarted". This reader
is the third move: it drops the BEGINNING and reports exactly what it dropped.

That inversion is only safe if the split is LOSSLESS, so that is what these tests
pin. `test_blocks_concatenate_back_to_the_input_exactly` is the load-bearing one:
if it holds, then --blocks N can never silently lose content, because the omitted
bytes are precisely the input minus the shown bytes and the tool reports them.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

_spec = importlib.util.spec_from_file_location("_gnt", SCRIPTS / "goal-note-tail.py")
gnt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gnt)

split = gnt.split_blocks


def _mk(*pairs):
    """Compose a field the way goal_field_append.compose() does, repeatedly."""
    out = ""
    for text, marker in pairs:
        out = (out + "\n\n" if out else "") + text + "\n" + f"[appended:{marker}]"
    return out


# ------------------------------------------------------------ losslessness --

def test_blocks_concatenate_back_to_the_input_exactly():
    """THE invariant. Every other guarantee rests on this one."""
    field = _mk(("first body", "m1"), ("second body", "m2"), ("third body", "m3"))
    assert "".join(t for _, t in split(field)) == field


def test_losslessness_holds_with_an_unmarked_head():
    field = "hand-written preamble\n\n" + _mk(("appended body", "m1"))
    assert "".join(t for _, t in split(field)) == field


def test_losslessness_holds_with_unmarked_trailing_text():
    field = _mk(("body", "m1")) + "\n\nclobbered on afterwards by update-goal"
    assert "".join(t for _, t in split(field)) == field


# ----------------------------------------------------------------- shape --

def test_markers_are_returned_in_append_order():
    field = _mk(("a", "m1"), ("b", "m2"), ("c", "m3"))
    assert [m for m, _ in split(field)] == ["m1", "m2", "m3"]


def test_unmarked_head_rides_with_the_first_block():
    """Documented, deliberate: over-include at the boundary. A reader must show
    MORE than asked, never less -- the head is at the BEGINNING, which is the
    end this tool omits, so folding it into block 1 keeps it inside the counted
    and reported omission rather than vanishing."""
    field = "PREAMBLE\n\n" + _mk(("body", "m1"))
    blocks = split(field)
    assert len(blocks) == 1
    assert blocks[0][0] == "m1"
    assert "PREAMBLE" in blocks[0][1]


def test_trailing_unmarked_text_becomes_its_own_block():
    field = _mk(("body", "m1")) + "\n\ntrailing"
    blocks = split(field)
    assert [m for m, _ in blocks] == ["m1", None]
    assert blocks[-1][1].strip() == "trailing"


def test_field_with_no_sentinels_is_one_unmarked_block():
    """The pre-helper case: a note hand-written before goal-field-append existed.
    It must be readable, not reported as zero blocks."""
    blocks = split("just a plain note, never appended to")
    assert len(blocks) == 1 and blocks[0][0] is None


def test_empty_field_yields_no_blocks():
    assert split("") == []


def test_whitespace_only_field_yields_no_blocks():
    assert split("   \n\n  ") == []


# ------------------------------------------------------- anchoring guards --

def test_a_sentinel_mentioned_mid_line_does_not_split():
    """Prose quoting the grammar (this very docstring class) must not be parsed
    as a block boundary -- the regex is line-anchored for exactly this reason."""
    field = "I appended it with [appended:m1] inline in a sentence.\n" + _mk(("real", "m2"))
    assert [m for m, _ in split(field)] == ["m2"]


def test_a_sentinel_with_trailing_text_on_the_line_does_not_split():
    field = "body\n[appended:m1] and then more words\n" + _mk(("real", "m2"))
    assert [m for m, _ in split(field)] == ["m2"]


def test_duplicate_markers_are_both_kept():
    """Observed live on : two identical markers from a retried write.
    Dedup here would silently drop a real block."""
    field = _mk(("a", "dup"), ("b", "dup"))
    assert [m for m, _ in split(field)] == ["dup", "dup"]
    assert "".join(t for _, t in split(field)) == field


# ------------------------------------------------------------ tail slicing --

@pytest.mark.parametrize("n,expect_shown", [(1, 1), (2, 2), (3, 3), (99, 3)])
def test_tail_slice_never_exceeds_available_blocks(n, expect_shown):
    field = _mk(("a", "m1"), ("b", "m2"), ("c", "m3"))
    blocks = split(field)
    shown = blocks[-n:]
    assert len(shown) == expect_shown
    omitted = blocks[: len(blocks) - len(shown)]
    assert len(omitted) + len(shown) == len(blocks)
    assert "".join(t for _, t in omitted) + "".join(t for _, t in shown) == field


def test_cap_mirrors_the_claim_gate():
    """CLAIM_SURFACE_CAP is a MIRROR of aspirations-claim.sh, not an independent
    policy. If the gate moves and this does not, every ratio printed is wrong --
    so pin the coupling here where a drift fails loudly."""
    claim = (SCRIPTS / "aspirations-claim.sh").read_text(encoding="utf-8")
    assert f"CAP = {gnt.CLAIM_SURFACE_CAP}" in claim
    assert gnt.SURFACE_FIELDS == ("description", "progress_note", "outcome_note")
    assert "FIELDS = ('description', 'progress_note', 'outcome_note')" in claim


# ----------------------------------------------- integration path (sq-019) --
# The tests above pin the pure helper. These pin the path production actually
# takes: argv -> main() -> block accounting -> emitted JSON. Hermetic: read_goal
# is stubbed, so no live store and no daemon is touched.

def _run_main(monkeypatch, capsys, row, argv):
    monkeypatch.setattr(gnt._gfa, "read_goal", lambda gid, src: row)
    rc = gnt.main(argv)
    return rc, capsys.readouterr().out


def _row(field_text, **extra):
    r = {"id": "g-000-01", "description": "d" * 10, "progress_note": field_text,
         "outcome_note": "", "status": "pending"}
    r.update(extra)
    return r


def test_json_output_accounting_is_internally_consistent(monkeypatch, capsys):
    """shown + omitted must reconcile to the whole field, in BOTH units. This is
    the invariant a reader relies on to know the omission is fully reported."""
    field = _mk(("a", "m1"), ("b", "m2"), ("c", "m3"))
    rc, out = _run_main(monkeypatch, capsys, _row(field),
                        ["g-000-01", "--blocks", "2", "--json"])
    assert rc == 0
    d = json.loads(out)                      # must parse: stdout is JSON-only
    assert d["blocks_shown"] + d["blocks_omitted"] == d["blocks_total"] == 3
    assert d["bytes_shown"] + d["bytes_omitted"] == d["field_bytes"]
    assert d["omitted_markers"] == ["m1"] and d["shown_markers"] == ["m2", "m3"]
    assert d["tail"].encode("utf-8").__len__() == d["bytes_shown"]


def test_json_stdout_carries_nothing_but_json(monkeypatch, capsys):
    """guard-3409: a stray human-readable line on stdout breaks every chained
    consumer. The advisory belongs in the payload, never beside it."""
    rc, out = _run_main(monkeypatch, capsys, _row(_mk(("a", "m1"))),
                        ["g-000-01", "--json"])
    assert rc == 0
    json.loads(out)                          # raises if anything precedes/follows


def test_over_cap_surface_is_reported_as_a_ratio(monkeypatch, capsys):
    big = "x" * (gnt.CLAIM_SURFACE_CAP + 1000)
    rc, out = _run_main(monkeypatch, capsys, _row(big), ["g-000-01", "--json"])
    d = json.loads(out)
    assert d["read_surface_bytes"] > gnt.CLAIM_SURFACE_CAP
    assert d["read_surface_vs_cap"] > 1.0


def test_empty_field_does_not_crash_and_reports_zeroes(monkeypatch, capsys):
    rc, out = _run_main(monkeypatch, capsys, _row(""), ["g-000-01", "--json"])
    d = json.loads(out)
    assert rc == 0 and d["blocks_total"] == 0 and d["bytes_omitted"] == 0
    assert d["tail"] == ""


def test_blocks_below_one_is_refused(monkeypatch, capsys):
    monkeypatch.setattr(gnt._gfa, "read_goal", lambda gid, src: _row("x"))
    assert gnt.main(["g-000-01", "--blocks", "0"]) == 2


def test_human_mode_names_the_omission_explicitly(monkeypatch, capsys):
    """The whole safety argument is that the omission is LOUD. If this line ever
    stops being emitted, a tail read becomes an undeclared truncation."""
    field = _mk(("a", "m1"), ("b", "m2"))
    rc, out = _run_main(monkeypatch, capsys, _row(field), ["g-000-01", "--blocks", "1"])
    assert rc == 0
    assert "OMITTED: 1 earlier block" in out
    assert "--blocks 2" in out               # tells the reader how to see it all
    assert "m1" in out                       # names WHICH block was dropped


def test_human_mode_says_so_when_nothing_was_omitted(monkeypatch, capsys):
    rc, out = _run_main(monkeypatch, capsys, _row(_mk(("a", "m1"))),
                        ["g-000-01", "--blocks", "9"])
    assert "OMITTED: nothing" in out


def test_recurring_goals_surface_their_firing_count(monkeypatch, capsys):
    """guard-6925: recurring goals are 6.98x enriched in the over-cap population,
    and achievedCount is the mechanism. A reader deciding whether a fold is a
    permanent fix or a rental needs that number in the same view as the size."""
    rc, out = _run_main(monkeypatch, capsys,
                        _row(_mk(("a", "m1")), recurring=True, achievedCount=517),
                        ["g-000-01"])
    assert "recurring achievedCount=517" in out
