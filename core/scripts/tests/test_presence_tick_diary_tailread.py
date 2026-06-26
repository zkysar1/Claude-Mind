#!/usr/bin/env python3
"""test_presence_tick_diary_tailread.py -- regression for  (asp-333 A1).

presence-tick.py is a LIVE PostToolUse hook (settings.json matcher='*', fires
on EVERY tool call). Step 5 reads the last record's `phase` from the
agent-private execution-diary.jsonl. It previously did
`f.read().splitlines()` of the FULLY-UNBOUNDED diary -- O(filesize) per tool
call. Live evidence (2026-06-25): the diary had grown to 24,577 entries, so
every tool call slurped a multi-MB file just to grab one field.

Fix: `_read_last_line(path, tail_bytes=8192)` seeks the final tail_bytes block
(each diary record is ~140 bytes, so the last complete line is always within
it) and returns the last NON-empty line -- bounded O(tail_bytes) regardless of
file size. A truncated first line in the seeked block is discarded (only the
last line is used); errors='replace' tolerates a tail cut mid-multibyte-char.

These cases pin the helper's contract directly (importlib load -- the module
filename is hyphenated). The sibling test_presence_tick_stdin_timeout.py drives
the whole script as a subprocess for the stdin-hang guard; this file unit-tests
the diary tail-read in isolation.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # core/scripts
sys.path.insert(0, str(SCRIPT_DIR))  # so presence-tick's `from _stdio import` resolves

# presence-tick.py is hyphenated -> load by path
_spec = importlib.util.spec_from_file_location("presence_tick", SCRIPT_DIR / "presence-tick.py")
_pt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pt)
_read_last_line = _pt._read_last_line


def test_returns_last_nonempty_line(tmp_path):
    p = tmp_path / "diary.jsonl"
    p.write_text("first\nsecond\nthird\n", encoding="utf-8")
    assert _read_last_line(str(p)) == "third"


def test_empty_file_returns_empty(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("", encoding="utf-8")
    assert _read_last_line(str(p)) == ""


def test_trailing_blank_lines_skipped(tmp_path):
    # A trailing newline (or several) must not yield "" -- the LAST NON-EMPTY
    # line is the contract. The original f.read().splitlines() also did this;
    # the seek-tail rewrite must preserve it.
    p = tmp_path / "trailing.jsonl"
    p.write_text("alpha\nbeta\n\n\n", encoding="utf-8")
    assert _read_last_line(str(p)) == "beta"


def test_large_file_correct_last_line(tmp_path):
    # >200KB of records: a correct return proves the tail seek lands on the
    # real last line (pre-fix this slurped the whole file; post-fix it must
    # still return the identical value -- correctness is preserved, cost is not).
    p = tmp_path / "big.jsonl"
    lines = [f'{{"seq":{i},"phase":"p{i}"}}' for i in range(20000)]  # ~440KB
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert _read_last_line(str(p)) == '{"seq":19999,"phase":"p19999"}'


def test_small_tail_bytes_uses_only_tail(tmp_path):
    # With tail_bytes smaller than the file, the seeked block starts MID-FILE
    # and its first (truncated) line must be discarded -- only the genuine last
    # line is returned. A correct return here proves the discard-truncated-first
    # behavior (the bytes before the seek are never read).
    p = tmp_path / "tail.jsonl"
    p.write_text("AAAA\nBBBB\nCCCC\nDDDD\nEEEE\n", encoding="utf-8")
    # tail_bytes=7 captures only "DDDD\nEE..." region -> truncated head dropped,
    # last complete line still "EEEE".
    assert _read_last_line(str(p), tail_bytes=7) == "EEEE"


def test_json_record_roundtrips_to_phase(tmp_path):
    # End-to-end shape: a realistic diary line returns intact and json.loads +
    # .get('phase') reproduces Step 5's downstream use.
    import json
    p = tmp_path / "real.jsonl"
    p.write_text(
        '{"ts":"2026-06-25T06:48:00","goal_id":"g-333-03","phase":"phase-4-execute"}\n',
        encoding="utf-8",
    )
    last = _read_last_line(str(p))
    assert json.loads(last).get("phase") == "phase-4-execute"


def test_multibyte_split_tolerated(tmp_path):
    # errors='replace' must tolerate a tail cut mid-multibyte-char in the
    # DISCARDED head of the block; the last line (pure ASCII here) is unaffected.
    p = tmp_path / "mb.jsonl"
    p.write_text("中文テストdata\nlastline\n", encoding="utf-8")
    # tail_bytes small enough to slice into the multibyte head region.
    assert _read_last_line(str(p), tail_bytes=10) == "lastline"


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    import tempfile
    failures = []
    for fn in fns:
        with tempfile.TemporaryDirectory() as d:
            try:
                fn(Path(d))
            except Exception as e:  # noqa: BLE001
                failures.append(f"{fn.__name__}: {e}")
                traceback.print_exc()
    if failures:
        print(f"FAIL ({len(failures)}/{len(fns)})")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print(f"PASS ({len(fns)}/{len(fns)} cases)")
    sys.exit(0)
