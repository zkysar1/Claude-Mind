"""What a member changed in what their resident learned, and what became of it.

ONE PRODUCER (g-335-1726). A member's edit, forget or undo of one learned item is
queued by the spool's writer as one JSON record in ``<env>/inbound/``, and
``inbound_drain.py`` moves it through the spool lanes. The lane a record sits in IS its
state, and when a knowledge record finishes the drain also writes the ``outcome`` it
decided into the record (``{result, at, new_handle?, undo_until?}``; ``undo_until`` is
set on an applied forget only: the end of the window in which the member can take it
back). This module turns those lanes into the list a member reads: what did I change,
and did it take?

It is stdlib-only and imports nothing from this repo, so the world-off knowledge read,
which runs outside it, can vendor it byte-for-byte. The lane names below are
therefore restated, not imported; ``tests/test_knowledge_changes.py`` pins them to
the drain's.

WHAT A STATE MEANS
  queued      inbound/. Not claimed yet. Also where the drain LEAVES a record it will
              not claim now (a box that cannot resolve handles, an environment another
              drain holds): unclaimed, such a record drains by itself later.
  processing  processing/. Claimed. ALSO where the drain leaves a record whose apply
              FAILED, deliberately not retried, so this state never means "being
              applied right now".
  applied     processed/.
  refused     rejected/.
The lane is the authority for the state. The outcome only explains it, and only a
record in processed/ or rejected/ has one that counts: a record left claimed after
its outcome was written, or requeued from there, is not finished. A record that
finished before the drain wrote outcomes has none, and is shown finished with no
result.

failed/ and quarantine/ are NOT read. The drain never moves a record into failed/ (a
failed apply stays in processing/), and quarantine/ only receives what the drain could
not parse or did not recognise, which a knowledge record by construction is not.

ONE INSTRUCTION, ONE ROW. The drain never deletes: a name collision is suffixed, and
``--requeue-stale`` sets a duplicate copy aside in rejected/, with no outcome, while
the original stays queued. Rows are keyed on the record id (the name up to its first
dot, the drain's own idiom), and when copies of one id sit in several lanes the most
settled copy is shown: processed, then rejected with an outcome, then a queued or
claimed copy, then rejected without one. Otherwise one queued edit would also read as
refused.

NEVER RETURNED: the account id, the environment key, an edit's base digest, the
record's source, or any field not named in ``_row``. Every string returned is
shape-checked, and an edit's text is capped.

A point-in-time read: a record moving between lanes during the call can be missing
from that call's result. It is present in the next one.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

# inbound_drain.INBOUND / PROCESSING / PROCESSED / REJECTED, in the order a record
# moves through them.
INBOUND = "inbound"
PROCESSING = "processing"
PROCESSED = "processed"
REJECTED = "rejected"

LANE_STATES = {
    INBOUND: "queued",
    PROCESSING: "processing",
    PROCESSED: "applied",
    REJECTED: "refused",
}
FINISHED_LANES = (PROCESSED, REJECTED)

KNOWLEDGE_KIND = "knowledge"

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
# Records are parsed to learn their kind, and the lanes also hold every verb and
# directive the environment received, so the read is bounded by files opened, not
# only by rows returned.
DEFAULT_MAX_READS = 1000
TEXT_CAP = 500

_RECORD_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
# An op, or an outcome's result: "applied", "malformed_record", or an applier refusal
# code. The set is open, so this checks the shape and leaves the meaning to the reader.
_TOKEN = re.compile(r"[a-z][a-z0-9_]{0,63}")
_HANDLE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_TIMESTAMP = re.compile(r"[0-9][0-9T:.+Z-]{9,39}")


def _record_id(name: str) -> str | None:
    """The record id a lane entry carries, or None when it is not a record.

    A writer's in-flight temp file (``.tmp-*``) ends in ``.json`` too. Its id is the
    empty string before its leading dot, which the id pattern refuses.
    """
    if not name.endswith(".json"):
        return None
    rid = name.split(".")[0]
    return rid if _RECORD_ID.fullmatch(rid) else None


def _shaped(value: object, pattern: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _read(path: Path) -> dict | None:
    """The record at ``path``, or None when it is gone (the drain moved it after the
    listing), unreadable, or not a JSON object."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return record if isinstance(record, dict) else None


def _settled(lane: str, record: dict) -> int:
    """How far along this copy of a record is, for choosing among copies of one id."""
    if lane == PROCESSED:
        return 3
    if lane == REJECTED:
        # Without an outcome a rejected copy is either a refusal from before outcomes
        # were written or a requeue duplicate set aside; any other copy says more.
        return 2 if isinstance(record.get("outcome"), dict) else 0
    return 1


def _row(lane: str, rid: str, record: dict) -> dict:
    row = {
        "id": rid,
        "op": _shaped(record.get("op"), _TOKEN),
        "handle": _shaped(record.get("handle"), _HANDLE),
        "state": LANE_STATES[lane],
        "queued_at": _shaped(record.get("queued_at"), _TIMESTAMP),
        "result": None,
        "finished_at": None,
    }
    outcome = record.get("outcome")
    if lane in FINISHED_LANES and isinstance(outcome, dict):
        row["result"] = _shaped(outcome.get("result"), _TOKEN)
        row["finished_at"] = _shaped(outcome.get("at"), _TIMESTAMP)
        new_handle = _shaped(outcome.get("new_handle"), _HANDLE)
        if lane == PROCESSED and new_handle:
            row["new_handle"] = new_handle
        # Set on a forget only: the end of the window in which the member can take it back.
        undo_until = _shaped(outcome.get("undo_until"), _TIMESTAMP)
        if lane == PROCESSED and undo_until:
            row["undo_until"] = undo_until
    text = record.get("text")
    if isinstance(text, str):
        row["text"] = text[:TEXT_CAP]
        row["text_truncated"] = len(text) > TEXT_CAP
    return row


def list_changes(env_dir: str | os.PathLike[str], *, limit: int = DEFAULT_LIMIT,
                 max_reads: int = DEFAULT_MAX_READS) -> dict:
    """The knowledge changes queued for one environment, newest first.

    ``env_dir`` is the environment's spool directory, the one holding ``inbound/``.
    Returns ``{"changes": [row, ...], "truncated": bool}``. ``truncated`` is true when
    the read stopped at ``limit`` rows or ``max_reads`` files before it had looked at
    every record, so older changes may exist. An environment that never received a
    change has no lanes yet, and that is an empty list, not an error.
    """
    env = Path(env_dir)
    limit = max(1, min(limit, MAX_LIMIT))
    copies: dict[str, list[tuple[str, str]]] = {}
    for lane in LANE_STATES:
        try:
            with os.scandir(env / lane) as entries:
                # Regular files only, links not followed: a record is always one,
                # and opening a FIFO blocks until a writer appears, so a stray one
                # would hang the whole read. The listing usually carries the type,
                # so this costs no extra stat.
                names = [e.name for e in entries if e.is_file(follow_symlinks=False)]
        except OSError:  # the drain creates a lane on first use
            continue
        for name in names:
            rid = _record_id(name)
            if rid is not None:
                copies.setdefault(rid, []).append((lane, name))

    changes: list[dict] = []
    reads = 0
    truncated = False
    # A record id starts with its queue time (%Y%m%dT%H%M%S%f, UTC), so the
    # reverse lexical order is newest first.
    for rid in sorted(copies, reverse=True):
        if len(changes) >= limit or reads + len(copies[rid]) > max_reads:
            truncated = True
            break
        best: tuple[int, str, dict] | None = None
        for lane, name in sorted(copies[rid]):
            reads += 1
            record = _read(env / lane / name)
            if record is None or record.get("kind") != KNOWLEDGE_KIND:
                continue
            settled = _settled(lane, record)
            if best is None or settled > best[0]:
                best = (settled, lane, record)
        if best is not None:
            changes.append(_row(best[1], rid, best[2]))
    return {"changes": changes, "truncated": truncated}
