"""Retention of one forgotten learned item: where its text survives, and for how long ().

A member's forget does not erase. It takes the item out of everything the resident reads and
the member sees, and keeps the removed text in ONE place for :data:`RETENTION_DAYS`, so the
member can undo (the owner's ruling on g-335-1726: a soft forget with a 30-day undo, then
erase). This module is that place: the shape of a retained record, where it is written, how
it is read back, and whether it is still inside its window. ``knowledge-edit-apply.py`` is the
writer that uses it.

ONE COPY, OUTSIDE THE WORLD. The caller passes the directory; this module never derives one.
A record kept under the resident's own world would leave the forgotten text where the
resident can read and search it, and "forgotten" would then mean "no longer indexed" and
nothing more. The applier takes the directory from its caller, which owns a per-environment
directory that is not part of the resident's world.

A RECORD IS A RECOVERY LAYER, SO IT IS VERIFIED BEFORE ANYTHING DEPENDS ON IT (guard-6223).
:func:`write_record` reads the record back and compares it, and the digest of the retained
bytes is stored beside them and checked on every read. The writer takes no destructive step
until that verification has passed.

WHAT THIS MODULE NEVER DOES: delete. Erasing a record past its window is ``knowledge_erase.py``
(g-335-1726 u4): a destructive act that goes through the storage backend's own delete, never a
bare unlink, behind a receipt that holds no text (archive-before-delete cannot archive the text
the ruling erases; core/config/rationale/knowledge-erase-sweep.md). :func:`mark_undone` replaces
an undone record's text with a marker in place, so an undo leaves no second copy behind either.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterator, Mapping

__all__ = [
    "RETENTION_DAYS",
    "SCHEMA",
    "now_utc",
    "record_name",
    "record_path",
    "build_record",
    "write_record",
    "read_record",
    "retained_content",
    "is_live",
    "in_window",
    "judged_at",
    "list_records",
    "mark_undone",
]

#: How long a forgotten item can be brought back.
RETENTION_DAYS = 30

#: The record layout version. A reader refuses any other, so a future shape is never
#: half-read as this one.
SCHEMA = 1

_SUFFIX = ".json"


def now_utc() -> datetime.datetime:
    """The current instant, timezone-aware UTC. The box that runs the drain may not have
    ``TZ=UTC`` set."""
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(moment: datetime.datetime) -> str:
    return moment.astimezone(datetime.timezone.utc).replace(microsecond=0).isoformat()


def strictly_after(now: datetime.datetime, *prior: Any) -> datetime.datetime:
    """``now`` in whole UTC seconds, moved past every ``prior`` stamp that is not behind it.

    A member's forget and undo of one record are ordered by their stamps when two copies of
    the record merge, and a stamp is whole seconds. Two events in the same second, or one
    stamped by a clock that runs behind the clock that stamped the event before it, would tie
    or invert and let a stale copy win. Each prior stamp is one the record already carries; one
    that is missing or does not parse, or that no later stamp can follow, is ignored, as nothing can
    be ordered against it.
    """
    moment = now.astimezone(datetime.timezone.utc).replace(microsecond=0)
    for stamp in prior:
        try:
            seen = datetime.datetime.fromisoformat(str(stamp))
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=datetime.timezone.utc)
            seen = seen.astimezone(datetime.timezone.utc)
            if seen >= moment:
                moment = seen.replace(microsecond=0) + datetime.timedelta(seconds=1)
        except (ValueError, OverflowError):
            continue
    return moment


def _json_default(value: Any) -> str:
    """YAML hands back a date for an unquoted date scalar; a record stores it as ISO text."""
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return str(value)


def record_name(kind: str, item_id: str) -> str:
    """One item's record file name. A digest, so an id is never a path component."""
    digest = hashlib.sha256(f"{kind}\0{item_id}".encode("utf-8")).hexdigest()[:16]
    return f"{kind}-{digest}{_SUFFIX}"


def record_path(retention_dir: str | Path, kind: str, item_id: str) -> Path:
    return Path(retention_dir) / record_name(kind, item_id)


def build_record(kind: str, item_id: str, content: bytes, *, restore: Mapping[str, Any],
                 now: datetime.datetime) -> dict:
    """The retained record for one item's bytes.

    ``restore`` is whatever the writer needs to put the item back where it was; this module
    stores it and never reads it. The round trip through JSON is deliberate: the dict
    returned is exactly what :func:`read_record` will return, so :func:`write_record` can
    check its own write with ``==``.
    """
    record = {
        "schema": SCHEMA,
        "kind": kind,
        "item_id": item_id,
        "forgotten_at": _iso(now),
        "undo_until": _iso(now + datetime.timedelta(days=RETENTION_DAYS)),
        "sha256": hashlib.sha256(content).hexdigest(),
        "content_b64": base64.b64encode(content).decode("ascii"),
        "restore": dict(restore),
    }
    return json.loads(json.dumps(record, default=_json_default))


def _write_atomic(path: Path, data: bytes) -> None:
    """Write through a temp file and replace, mode 0600: a retained record is a member's text."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def write_record(retention_dir: str | Path, record: dict) -> Path:
    """Write ``record`` and return its path, only once it has read back as written.

    Raises ``OSError`` when the write fails and ``ValueError`` when it landed but does not
    read back equal, so a caller that catches both has no unverified record to rely on.
    """
    path = record_path(retention_dir, str(record["kind"]), str(record["item_id"]))
    _write_atomic(path, json.dumps(record, sort_keys=True).encode("utf-8"))
    if read_record(path) != record:
        raise ValueError(f"retained record {path.name} did not read back as written")
    return path


def retained_content(record: Mapping[str, Any]) -> bytes | None:
    """The retained bytes of a live record, or ``None`` when absent or failing their digest."""
    try:
        data = base64.b64decode(str(record.get("content_b64") or ""), validate=True)
    except ValueError:
        return None
    return data if hashlib.sha256(data).hexdigest() == record.get("sha256") else None


def read_record(path: str | Path) -> dict | None:
    """The record at ``path``, or ``None`` when it is missing, malformed, of another schema,
    names no item, or its retained bytes do not match their digest. ``None`` means nothing
    usable is kept here, and a caller must not treat it as "nothing was ever forgotten"."""
    try:
        record = json.loads(Path(path).read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        return None
    # The digest covers the retained bytes only, so a record with no item named, or restore
    # data that is not a mapping, would pass it and then fail every caller that trusts its shape.
    if not all(isinstance(record.get(f), str) and record[f] for f in ("kind", "item_id")):
        return None
    if not isinstance(record.get("restore", {}), dict):
        return None
    if "content_b64" in record and retained_content(record) is None:
        return None
    return record


def is_live(record: Mapping[str, Any]) -> bool:
    """Whether the record still holds the item's text (an undone record holds a marker)."""
    return "content_b64" in record


def in_window(record: Mapping[str, Any], now: datetime.datetime) -> bool:
    """Whether ``now`` is inside the undo window. A missing or unreadable bound is outside it."""
    try:
        until = datetime.datetime.fromisoformat(str(record["undo_until"]))
    except (KeyError, ValueError):
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=datetime.timezone.utc)
    return now <= until


def judged_at(sent: Any, now: datetime.datetime) -> datetime.datetime:
    """The moment a member's undo is judged against its window: when they sent it, and never
    later than ``now``.

    The window the member saw ("undo until X") can close while their undo waits for a stopped
    home to run, so judging at apply time refuses an undo that was sent in time. ``sent`` is the
    time the queued record carries; a stamp with no offset is UTC. One that is missing or does not
    parse is judged at ``now``, as is one after ``now`` (a clock running ahead), so a stamp can
    make the judgment more lenient than applying at ``now`` and never stricter.
    """
    try:
        moment = datetime.datetime.fromisoformat(str(sent))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=datetime.timezone.utc)
        return min(moment, now)
    except ValueError:
        return now


def list_records(retention_dir: str | Path) -> Iterator[tuple[Path, dict]]:
    """Every readable record in ``retention_dir``, in file-name order."""
    try:
        paths = sorted(p for p in Path(retention_dir).iterdir() if p.suffix == _SUFFIX and p.is_file())
    except OSError:
        return
    for path in paths:
        record = read_record(path)
        if record is not None:
            yield path, record


def mark_undone(path: str | Path, record: Mapping[str, Any], now: datetime.datetime) -> None:
    """Replace a record's retained text with an undone marker, in place, and verify it.

    The marker keeps who and when and drops the text, so an undone item is held in exactly
    one place again: the world it was restored to.
    """
    marker = {
        "schema": SCHEMA,
        "kind": record["kind"],
        "item_id": record["item_id"],
        "forgotten_at": record["forgotten_at"],
        "undone_at": _iso(now),
    }
    _write_atomic(Path(path), json.dumps(marker, sort_keys=True).encode("utf-8"))
    if read_record(path) != marker:
        raise ValueError(f"retained record {Path(path).name} did not read back as undone")
