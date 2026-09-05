#!/usr/bin/env python3
"""Shared chain lookup for the evolution event streams ().

Both writers of `previous_revision_id` derive it from state that does not
outlive the write, so a file's chain restarts instead of continuing:

  evolution-git-sweep.py  seeds `prev_rid_for_this_file = None` per file PER RUN
                          and then iterates only the commits inside the --since
                          window, so the first new entry for a file in a window
                          records a null predecessor no matter how many rows
                          that file already has.
  evolution-record.py     copies the pointer out of the pre-edit sidecar, which
                          for file kinds carrying no front-matter revision chain
                          (script_edit, rule_edit) is simply absent.

Neither ever asks the STORE for that file's last row. This module is that
missing lookup, in one place, so the two callers cannot drift.

MEASURED before the fix (alpha, DESKTOP-O91DLK2, 2026-09-03) across the five
streams, counting rows whose previous_revision_id is null although an EARLIER
row for the same file_path exists:

    stream                git-sweep rows   live rows
    self-evolution                 9            0
    skill-evolution              147          202
    rule-evolution                83          219
    script-evolution               -       10998
    program-evolution              0            0

Read-only and tolerant by construction: an unreadable stream, an unparsable
line, or a row missing `ts`/`file_path` is skipped rather than raised. A caller
that gets an empty index behaves exactly as it did before this module existed,
which is what keeps a fresh world and a broken read on the same safe path.
"""

import json
from pathlib import Path


def _norm_path(p):
    """Normalize a file_path for joins: posix separators only.

    Same normalization evolution-git-sweep.py::_norm_path applies, for the same
    reason — a future Windows writer drifting to backslashes must not silently
    break the join.
    """
    return (p or "").replace("\\", "/")


def chain_index(stream_path):
    """Return dict[file_path] -> list of (ts, revision_id), ascending by ts.

    `ts` is the naive `YYYY-MM-DDTHH:MM:SS` written by both writers, so plain
    string ordering is chronological ordering.
    """
    index = {}
    path = Path(stream_path)
    if not path.exists():
        return index
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rid = e.get("revision_id")
                ts = e.get("ts")
                if not rid or not ts:
                    continue
                index.setdefault(_norm_path(e.get("file_path")), []).append((ts, rid))
    except OSError:
        return {}
    for rows in index.values():
        rows.sort(key=lambda tr: tr[0])
    return index


def latest_rid(index, file_path):
    """Most recent revision_id recorded for `file_path`, or None."""
    rows = index.get(_norm_path(file_path))
    return rows[-1][1] if rows else None


def latest_rid_before(index, file_path, ts):
    """Most recent revision_id for `file_path` STRICTLY OLDER than `ts`, or None.

    The git sweep can be re-run over an OLD --since window after newer rows are
    already in the store. Seeding such a run with the file's globally-latest row
    would point an old entry at a predecessor that comes AFTER it, which is a
    worse chain than the null it replaces — so the seed is bounded by the entry's
    own timestamp rather than taken from the end of the list.
    """
    rows = index.get(_norm_path(file_path))
    if not rows:
        return None
    prev = None
    for row_ts, rid in rows:
        if row_ts < ts:
            prev = rid
        else:
            break
    return prev
