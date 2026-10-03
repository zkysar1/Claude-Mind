"""Erase what a member forgot, once its undo window has closed ( u4).

A member's forget keeps the removed text in one retained record for RETENTION_DAYS so it can
be undone (``knowledge_retention``). The owner's ruling is that after that the text is erased.
This module is the erase: :func:`sweep` visits the retention directory and, for every live
record whose window is over, finishes the world's side, deletes the record, and writes a
receipt that holds no text. It runs inside the drain, under the environment's drain lock, so it
does not race a member's undo or forget that the drain applies (a direct caller of the applier is
not serialized by that lock: ``inbound_drain``).

THE RULE THAT HOLDS EVERYTHING ELSE: a retained record is not deleted while the sweep can still
reach text it has not made blank. Each record is settled world-first, and a step that fails, or
cannot be shown to have finished, keeps the record and counts ``failed``: the next pass tries
again. Two cases are ``pending`` instead, an obligation nothing on this box can meet. One keeps
the record, because it is the only thing that says what the world holds (a guardrail's rule, or
a page that may still be the member's). The other erases it, because the text is out of the
sweep's reach (a copy in the archive file, a field name the writer refuses) and keeping the
record would protect nothing. Either way the sweep goes on to the next record.

WHAT THE WORLD'S SIDE IS, PER KIND
  node         the page is blanked when the forget stopped before blanking it and no live node
               shows the page, and an index entry that came back over a blanked page (a ghost, as
               a merge from a stale index brings one) is dropped again. A page that ANY index
               entry resolves to is shown, whichever key holds it, and is left alone. An orphan page
               that is neither the retained copy nor the tombstone cannot be shown to be the
               member's text, so it is ``pending`` and nothing is erased. Without the tree there
               is nothing to say about a page, so that is a failure and the record stays.
  hypothesis   the record stays (a pipeline record is never deleted) and is reduced to its
               identity and lifecycle: the statement (``claim``, ``title``) and every other
               field that holds prose are replaced with the forgotten marker, and a nested
               value that holds prose, in a key or a value, is emptied to its own type. A
               forgotten record whose window closed is settled whether or not a retained record
               is left, which is how the residue the pipeline merge leaves (a marker stamped,
               the text not blanked) is finished. Prose is decided by the SHAPE of a value: a
               string of only ASCII identifier characters, of any length, is an id, an enum or a
               ref, an ISO date or timestamp is lifecycle, and anything else is text. Two short
               name rules sit beside it: the statement fields are blanked whatever they hold, and
               the identity names are never blanked. A name list for the rest cannot keep up with
               the schema (the live pipeline carries well over a hundred field names) and a
               Chinese claim has no spaces to find. A marker is matched in full: a sentence that
               only starts like one is text.
  guardrail    the record stays (a retired guardrail is a record the store keeps) and is reduced the way a
               hypothesis is, by the same shape rule: the rule, which the store holds immutable (guard-6210),
               is blanked through the store's erase mode, and every other field that holds prose is replaced
               with the forgotten marker. That mode exists for one case, a single box on the local backend,
               where no other copy is merged with this one: an erase in place would fork the record at a
               merge (rb-5511). On any other backend the store refuses it, the sweep counts the record
               ``pending`` every pass, keeps its retained record and changes nothing. A rule a member brought
               back with Undo, or corrected before forgetting, is also held by the records those left behind,
               so the sweep walks them (a tag on the successor, and the reason the predecessor was retired
               with, must agree) and blanks each before the record that names it. A guardrail that is still
               active, or no longer in the store, has nothing to blank.

THE ARCHIVE STEP IS A RECEIPT, NOT A COPY. archive-before-delete asks for an archive of what is
about to be destroyed; an archive of a member's forgotten text is the very thing the ruling
removes. So the enumeration and the receipt carry no text, no item id (a hypothesis id carries a
slug of its claim), no digest of the text (a digest of short text confirms a guess) and no file
name (it is a digest of the item id, which a guess can confirm); an erase is named by a random
id, and carries the record's kind, its window and a length. The retained record is verified (its
digest read back) before anything depends on it, re-read just before the delete (guard-3881), and
its removal is read back. A write-ahead ``erasing`` line precedes the delete and an ``erased``
line follows it, so a stop between them, or a delete the backend refused, is visible as an
``erasing`` with no ``erased`` after it. Why this is the method and what it leaves open:
core/config/rationale/knowledge-erase-sweep.md.

WHAT THIS DOES NOT REACH (named so nobody reads "erased" as more than it is): the world's
``.history`` snapshots and the store's own version history keep older copies of a pipeline or
guardrails file, and the erase's own writes add snapshots of their own; a copy of the record in the append-only
archive file that sits beside a live copy cannot be written through the pipeline writer and is
reported as residue, as is a field name the writer will not take; the spool volume's snapshots and
backups are unverified; nothing is overwritten before the unlink, since the file systems this
runs on do not make that reliable. The archive-file and field-name residue is counted ``pending``
and never called "erased"; the history snapshots and the volume are named here and not counted,
because this module cannot see them. So are the copies an export makes of an item (the home's bundle file and wiki
folder, Vinheim's saved copy and its earlier versions): the next export replaces them, and nothing here triggers
one or touches one (rationale: "What it leaves open").
"""

from __future__ import annotations

import datetime
import json
import os
import re
import secrets
import traceback
from pathlib import Path

import knowledge_retention
from knowledge_projection import FORGOTTEN_FIELD, RESTORED_FIELD, is_forgotten, item_text_fields

__all__ = ["RECEIPTS", "APPLIER_NAMES", "EXPORT_NAMES", "EraseError", "blank_plan", "sweep"]

#: The receipt file, beside the lanes: outside the retention directory it describes and outside
#: the world. Append-only, one JSON object per line, no text.
RECEIPTS = "erasures.jsonl"
SCHEMA = 1

#: A write that never finished leaves ``<record>.json.tmp`` holding the text. A live write takes
#: milliseconds, so one this old is residue.
TMP_RESIDUE_SECONDS = 3600

#: What the sweep calls on the applier, so a rename there fails here by name, before any write.
APPLIER_NAMES = (
    "_TOMBSTONE", "_TOMBSTONE_HEAD", "_FORGOTTEN_TEXT", "_FORGOTTEN_HEAD", "_load_export_mod", "_today",
    "_mapping_nodes", "_index_snapshot", "_drop_index_entry", "_replace_file", "_restore_path",
    "_write_hypothesis_field", "_write_guardrail_field", "_SUPERSEDED_REASON", "_SUPERSEDES_TAG",
    "_RESTORES_TAG", "_tags",
)

#: What the sweep calls on the knowledge export module the applier loads, checked with the applier's
#: own names before the pass touches anything: a drift found halfway would leave a pass half done.
EXPORT_NAMES = ("_read_jsonl", "_resolve_world", "_resolve_tree_dir")

#: Never blanked, whatever they hold: the record's identity and lifecycle, the member's category,
#: and the two stamps that say what happened to it.
_NEVER_BLANKED = frozenset({"id", "slug", "stage", "horizon", "type", "category", "outcome",
                            FORGOTTEN_FIELD, RESTORED_FIELD})

#: The name a retained record, or the temp file its write goes through, carries (``record_name``).
#: Unreadable residue is purged only under such a name: a file the sweep's own writer did not
#: name is not the sweep's to delete.
_RECORD_NAME = re.compile(r"[a-z]+-[0-9a-f]{16}\.json(\.tmp)?")

#: A value of only these characters, of ANY length, is an id, an enum, a date or a ref, never a
#: sentence. That is the rule's limit: a hyphenated sentence or a bare URL passes as one.
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:+/\-]+")

#: An ISO date, or a date and a time written with a space or a T: lifecycle data, never prose.
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?")

#: What follows the head of a forgotten marker: the date it was written, then a full stop.
_MARKER_TAIL = re.compile(r"\d{4}-\d{2}-\d{2}\.")

_LIVE, _ARCHIVE = "pipeline.jsonl", "pipeline-archive.jsonl"
_RESTORE_NOTE = ("none: the retained text is deleted by design. "
                 "See core/config/rationale/knowledge-erase-sweep.md.")


class EraseError(RuntimeError):
    """The sweep cannot run at all: its applier or export is not the one it was written against, or
    the world it would sweep cannot be found."""


def _aware(moment: datetime.datetime) -> datetime.datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=datetime.timezone.utc)


def _stamp(moment: datetime.datetime) -> str:
    return moment.astimezone(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _is_marker(value: str, head: str) -> bool:
    """Whether ``value`` is exactly a forgotten marker: the head, a date and a full stop. A value
    that only STARTS with the head is somebody's sentence."""
    return value.startswith(head) and _MARKER_TAIL.fullmatch(value[len(head):]) is not None


def _prose(value, head: str) -> bool:
    """Whether ``value`` holds text that is not the forgotten marker. Decided by shape (see the
    module docstring); a container holds prose when anything inside it does, a mapping's KEYS
    included, since nothing stops a writer keying a mapping by a sentence."""
    if isinstance(value, str):
        return (bool(value) and not _is_marker(value, head) and _IDENTIFIER.fullmatch(value) is None
                and _TIMESTAMP.fullmatch(value) is None)
    if isinstance(value, dict):
        return any(_prose(k, head) or _prose(v, head) for k, v in value.items())
    if isinstance(value, list):
        return any(_prose(v, head) for v in value)
    return False


def blank_plan(record: dict, *, tombstone: str, head: str, kind: str = "hypothesis") -> dict:
    """``{field: replacement}`` for everything a forgotten ``kind`` record still holds that is
    not identity, lifecycle or a marker. Empty when the record is already blank, which is what
    makes a second pass change nothing. The statement fields are blanked whatever their shape,
    since a one-word claim would otherwise read as an id."""
    statement = set(item_text_fields(kind))
    plan: dict = {}
    for name, value in record.items():
        if name in _NEVER_BLANKED:
            continue
        if name in statement:
            if value not in (None, "") and not (isinstance(value, str) and _is_marker(value, head)):
                plan[name] = tombstone
        elif isinstance(value, str):
            if _prose(value, head):
                plan[name] = tombstone
        elif isinstance(value, (dict, list)) and _prose(value, head):
            plan[name] = type(value)()
    return plan


def _result(apply: bool) -> dict:
    return {"erased": 0, "world_changed": 0, "pending": 0, "failed": 0, "would_erase": 0,
            "dry_run": not apply, "entries": []}


def _note(out: dict, kind: str, record: str, action: str, detail: str = "", **world) -> None:
    """One entry of the pass's account. ``record`` is the digest file name of a record that is
    still on disk (so an operator can find it), the receipt's own random id for one that is
    gone, or ``-`` for a pipeline record that has no file: a name for that one would be a digest
    of its id, and an id can be confirmed from its digest. It carries codes and counts only:
    never text, and never an item id."""
    out["entries"].append({"kind": kind, "record": record, "action": action, "detail": detail,
                           **({"world": world} if world else {})})


def _why(exc: BaseException) -> str:
    """What went wrong, with no data in it: the exception's type and the code location it was
    raised from. Its message is left out, since a message can carry a path that names an item."""
    frames = traceback.extract_tb(exc.__traceback__)
    where = f" at {Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else ""
    return f"{type(exc).__name__}{where}"


def _attempt(out: dict, kind: str, name: str, fn, *args, **kwargs):
    """Run one record's work. An exception is that record's failure, counted and named without any
    data in it, and the pass goes on: one record that cannot be settled must not hide the rest, or
    the counts of what was already done. ``SystemExit`` is not caught, since it ends the process."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - a record's failure, not the pass's
        out["failed"] += 1
        _note(out, kind, name, "failed", _why(exc))
        return None


def _receipt(env_dir: Path, line: dict) -> None:
    """Append one line, flushed to disk before returning. Plain local I/O on purpose: the spool
    is never a governed path, and a receipt that could be queued behind a sync layer is not one."""
    data = (json.dumps(line, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(Path(env_dir) / RECEIPTS, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _delete(path: Path) -> bool:
    """The storage backend's own delete, read back: whether the file was there to delete.
    LocalBackend and not the process-wide backend, for the reason the drain lock gives; a local
    delete is final. ``False`` means another pass got there first."""
    from storage_backend import LocalBackend  # noqa: PLC0415 - only an applying sweep needs it

    return bool(LocalBackend().delete(str(path)))


def _erase_file(env_dir: Path, path: Path, line: dict, unchanged, out: dict, now, *, apply: bool,
                kind: str, world: dict) -> bool:
    """Enumerate, re-check, write-ahead receipt, delete, read back, closing receipt. Returns
    whether the file is gone. ``unchanged`` re-reads the file against what was enumerated.

    The receipt names the erase by a random id and never by the file: the file name is a digest
    of the item's kind and id, so printing it would let anyone holding a guess at the id confirm
    it. A file that is still on disk (skipped, failed, a dry run) is named, since an operator
    needs to find it and its name is what a listing of the directory shows anyway."""
    if not apply:
        out["would_erase"] += 1
        _note(out, kind, path.name, "would-erase", "dry run", **world)
        return False
    event = secrets.token_hex(8)
    try:
        if not unchanged():
            _note(out, kind, path.name, "skipped", "the record changed while the sweep ran")
            return False
        _receipt(env_dir, {**line, "event": "erasing", "id": event, "at": _stamp(now), "world": world})
        existed = _delete(path)
    except OSError as exc:
        out["failed"] += 1
        _note(out, kind, path.name, "failed", f"not erased: {type(exc).__name__}", **world)
        return False
    outcome = "erased" if existed else "gone"  # gone: another pass deleted it first, and counts it
    out["erased"] += existed
    try:
        _receipt(env_dir, {**line, "event": outcome, "id": event, "at": _stamp(now), "world": world,
                           "restore": _RESTORE_NOTE})
    except OSError as exc:
        out["failed"] += 1
        _note(out, kind, event, "failed",
              f"{outcome}, but the closing receipt was not written: {type(exc).__name__}", **world)
        return True
    if existed:
        _note(out, kind, event, "erased", **world)
    else:
        _note(out, kind, event, "skipped", "already erased by another pass")
    return True


def _erase_retained(env_dir: Path, path: Path, record: dict, out: dict, now, *, apply: bool,
                    world: dict) -> bool:
    """Erase one retained record: what is enumerated is its kind, window and length."""
    content = knowledge_retention.retained_content(record) or b""
    line = {"schema": SCHEMA, "kind": record.get("kind"),
            "forgotten_at": record.get("forgotten_at"), "undo_until": record.get("undo_until"),
            "bytes": len(content)}

    def unchanged() -> bool:
        again = knowledge_retention.read_record(path)
        return (again is not None
                and (again.get("forgotten_at"), again.get("undo_until"), again.get("sha256"))
                == (record.get("forgotten_at"), record.get("undo_until"), record.get("sha256")))

    return _erase_file(env_dir, path, line, unchanged, out, now, apply=apply,
                       kind=str(record.get("kind")), world=world)


def _residue(env_dir: Path, path: Path, out: dict, now, *, apply: bool) -> None:
    """A file in the retention directory that no reader can use and that may still hold text:
    write residue (``.json.tmp``) and a ``.json`` that does not read as a record. A record of a
    NEWER schema is not unreadable, it is unknown, and is kept: deleting what this code cannot
    evaluate would be the opposite of a careful erase. So is an unreadable record that names a
    node or a guardrail: its world side (a page to blank, a rule to blank) cannot be checked
    without it, and a write that never finished (``.tmp``) holds no such promise because the forget
    changes nothing in the world until its record is whole. Only a file named the way the retention
    writer names its own (``_RECORD_NAME``) is purged; one placed there by hand is left alone."""
    try:
        before = path.stat()
    except OSError:
        return
    parsed = None
    if not path.name.endswith(".tmp"):
        try:
            parsed = json.loads(path.read_bytes())
        except (OSError, ValueError):
            pass
    if (isinstance(parsed, dict) and isinstance(parsed.get("schema"), int)
            and not isinstance(parsed.get("schema"), bool)
            and parsed["schema"] > knowledge_retention.SCHEMA):
        out["pending"] += 1
        _note(out, "residue", path.name, "pending", "a record of a newer schema: kept, not evaluated")
        return
    kind = parsed.get("kind") if isinstance(parsed, dict) else None
    if kind in ("node", "guardrail"):
        out["pending"] += 1
        _note(out, "residue", path.name, "pending",
              f"an unreadable {kind} record: kept, because its world side cannot be checked without it")
        return
    if _RECORD_NAME.fullmatch(path.name) is None:
        return  # not a name the retention writer gives: left alone and not counted
    limit = TMP_RESIDUE_SECONDS if path.name.endswith(".tmp") else knowledge_retention.RETENTION_DAYS * 86400
    if now.timestamp() - before.st_mtime < limit:
        return
    line = {"schema": SCHEMA, "kind": "residue", "bytes": before.st_size}

    def unchanged() -> bool:
        try:
            again = path.stat()
        except OSError:
            return False
        return (again.st_size, again.st_mtime_ns) == (before.st_size, before.st_mtime_ns)

    _erase_file(env_dir, path, line, unchanged, out, now, apply=apply, kind="residue", world={})


#: Verdicts of a settle that are an obligation the sweep cannot meet, not a failure to retry: the
#: retained record stays, and the pass counts it ``pending``.
_PENDING = {"page_differs": "the page at its path holds text that is not the retained copy and no node "
                            "shows it: it may still be the member's, and only a person can say",
            "not_local": "the rule stays in the store: this backend can merge the store with another copy, "
                         "and a rule erased in place would fork the record at the next merge (guard-6210)"}


def _page_marker(applier) -> "re.Pattern[bytes]":
    """What a forgotten page holds: the applier's own tombstone, with any date. Built from its
    template, so the two cannot drift, and matched in full: a page that merely STARTS like the
    tombstone and goes on is somebody's text."""
    before, after = applier._TOMBSTONE.split("{today}")
    return re.compile((re.escape(before) + r"\d{4}-\d{2}-\d{2}" + re.escape(after)).encode("utf-8"))


def _settle_node(applier, export, tree_dir, record: dict, *, apply: bool) -> tuple[str, dict]:
    """Make the world hold nothing of a forgotten node. ``("ok", did)`` when it holds nothing,
    else ``(why, {})`` and the retained record must stay.

    The retained record is never let go while a page may still hold the text. So "ok" is only
    said when the page is gone, is the tombstone, is SHOWN by a live node (the forget never took,
    or another key shares the file, and blanking it would destroy a live node), or was blanked
    here. An orphan page that is not the retained copy cannot be shown to be the member's text,
    so it is ``page_differs`` and left for a person: not erased, not blanked."""
    if tree_dir is None:
        return "tree_unavailable", {}  # nothing can be said about a page without the tree it is in
    page = applier._restore_path(export, tree_dir, record.get("restore") or {})
    if page is None:
        return "ok", {}  # the record names no page inside the tree: nothing here the sweep may touch
    nodes = applier._mapping_nodes(tree_dir)
    if nodes is None:
        # Without a readable index a shown node cannot be told from a forgotten one, and blanking
        # a page the index shows would destroy it.
        return "index_unreadable", {}
    try:
        current = page.read_bytes() if page.is_file() else None
    except OSError:
        return "page_unreadable", {}
    if current is None:
        return "ok", {}  # no page, so nothing holds the text
    key = record["item_id"]
    blank = _page_marker(applier).fullmatch(current) is not None
    # Every key whose entry resolves to this page, not only the forgotten one: a node re-registered
    # under another key, or a shared file, shows the page just as well.
    showing = {k for k, entry in nodes.items()
               if applier._restore_path(export, tree_dir, {"index_entry": entry}) == page}
    if blank:
        if key not in showing:
            return "ok", {}
        # A ghost: the entry came back over the blanked page. Hide it again.
        snapshot = applier._index_snapshot(tree_dir, key)
        if snapshot is None:
            return "index_changed", {}  # the index moved between the two reads: decide next pass
        entry, parent, _slot = snapshot
        if apply:
            try:
                dropped = applier._drop_index_entry(tree_dir, key, entry, parent, applier._today())
            except Exception:  # noqa: BLE001 - the lock and the sync layer raise their own errors
                dropped = False
            if not dropped:
                return "ghost_not_dropped", {}
        return "ok", {"ghost_dropped": True}
    if showing:
        return "ok", {}  # a shown node: its forget never took, and there is nothing to blank
    if current == knowledge_retention.retained_content(record):
        if apply and not applier._replace_file(
                page, applier._TOMBSTONE.format(today=applier._today()).encode("utf-8")):
            return "page_not_blanked", {}
        return "ok", {"page_blanked": True}
    return "page_differs", {}


def _scan(export, world: Path) -> dict:
    """Every record the two pipeline files hold, ``{"live": {id: record}, "archive": {id: record}}``,
    the first match for an id. Read once per pass: a forgotten record keeps its marker for good, so
    reading both files per marked id would grow with every forget a member ever made."""
    scan: dict = {"live": {}, "archive": {}}
    for label, name in (("live", _LIVE), ("archive", _ARCHIVE)):
        for rec in export._read_jsonl(world / name):
            rid = str(rec.get("id") or "").strip()
            if rid:
                scan[label].setdefault(rid, rec)
    return scan


def _copies(scan: dict, key: str) -> dict:
    """``{"live": record, "archive": record}`` for the files that hold ``key``."""
    return {label: found[key] for label, found in scan.items() if key in found}


def _window_closed(stamp, now: datetime.datetime) -> bool:
    """Whether a marker's own stamp says the undo window is over. A stamp that does not read
    leaves nothing an undo could be named by, so it counts as over. A stamp so far ahead that its
    window cannot be added up is a window that is not over."""
    try:
        moment = _aware(datetime.datetime.fromisoformat(str(stamp)))
    except ValueError:
        return True
    try:
        return now > moment + datetime.timedelta(days=knowledge_retention.RETENTION_DAYS)
    except OverflowError:
        return False


def _writable(name: str) -> bool:
    """Whether the pipeline writer lands a write on the field this name says. It refuses a dotted
    name and strips a name's whitespace, so a padded ``"category "`` would overwrite ``category``."""
    return bool(name) and "." not in name and name == name.strip()


def _settle_hypothesis(applier, export, world: Path, key: str, copies: dict, *, apply: bool,
                       tick=lambda: None) -> tuple[str, dict, int]:
    """Make the world's copies of a forgotten hypothesis (``copies``, as the pass read them) hold
    no text. ``(verdict, did, residue)``: ``"ok"`` or why the retained record must stay, what was
    (or would be) changed, and how many copies hold text this writer cannot reach. ``tick`` is
    called before every write, since each is a daemon call that can take many seconds."""
    tombstone = applier._FORGOTTEN_TEXT.format(today=applier._today())
    head = applier._FORGOTTEN_HEAD
    live, archived = copies.get("live"), copies.get("archive")
    reached = live if live is not None else archived  # the copy the pipeline writer lands a write on
    if reached is not None and not is_forgotten(reached):
        # Shown: the forget never took, so nothing here is the member's to blank.
        return "ok", {}, 0
    targets = []
    residue = 0
    if live is not None:
        targets.append(live)
    if archived is not None:
        if live is None:
            targets.append(archived)  # the pipeline writer reaches an archive-only record
        elif blank_plan(archived, tombstone=tombstone, head=head):
            residue += 1  # shadowed by the live copy, and the writer would reach only the live one
    fields = 0
    for rec in targets:
        plan = blank_plan(rec, tombstone=tombstone, head=head)
        if not all(_writable(name) for name in plan):
            residue += 1  # the writer will not land a write on this name, so this copy keeps that text
            plan = {name: value for name, value in plan.items() if _writable(name)}
        fields += len(plan)
        if apply:
            # Every field is tried: one that fails must not leave the others holding their text.
            landed = []
            for name, value in plan.items():
                tick()
                landed.append(applier._write_hypothesis_field(key, name, value))
            if not all(landed):
                return "blank_failed", {}, residue
    if apply and fields:
        again = _copies(_scan(export, world), key)  # read fresh: what the writer left, not what we planned
        for label in ("live", "archive"):
            rec = again.get(label)
            if rec is None or (label == "archive" and "live" in again):
                continue
            left = {name for name in blank_plan(rec, tombstone=tombstone, head=head) if _writable(name)}
            if left:
                return "text_remains", {}, residue
    return "ok", ({"fields_blanked": fields} if fields else {}), residue


def _hypothesis_one(applier, export, world: Path, env_dir: Path, key: str, held, scan: dict, out: dict,
                    now, *, apply: bool, tick) -> None:
    """One forgotten hypothesis: closed or not, then its world copies, then its retained record."""
    copies = _copies(scan, key)
    if held is not None:
        path, record = held
        name = path.name
        closed = not knowledge_retention.in_window(record, now)
    else:
        path, record, name = None, None, "-"
        stamp = next((copies[c].get(FORGOTTEN_FIELD) for c in ("live", "archive")
                      if c in copies and is_forgotten(copies[c])), None)
        closed = stamp is not None and _window_closed(stamp, now)
    if not closed:
        return
    verdict, did, residue = _settle_hypothesis(applier, export, world, key, copies, apply=apply, tick=tick)
    if verdict != "ok":
        out["failed"] += 1
        _note(out, "hypothesis", name, "failed", verdict)
        return
    if did and apply:
        out["world_changed"] += 1
    if residue:
        out["pending"] += 1
        _note(out, "hypothesis", name, "pending",
              f"{residue} copy(ies) of this record still hold text the pipeline writer cannot reach")
    if record is not None:
        _erase_retained(env_dir, path, record, out, now, apply=apply, world=did)
    elif did:
        _note(out, "hypothesis", name, "blanked" if apply else "would-blank", **did)


def _hypotheses(applier, export, world: Path, env_dir: Path, retained: dict, out: dict, now, *,
                apply: bool, tick=lambda: None) -> None:
    """Every forgotten hypothesis whose window is over: its world copies first, then its retained
    record. The set is the retained records PLUS every marked copy in the pipeline, so a marker
    left without its text blanked is finished whether or not a retained record survives.

    A retained record is not let go on a pipeline that is not there: the reader returns nothing
    for a missing file, which would read as "this record holds no text" and erase the retained
    copy of a record that is merely not mounted."""
    if retained and not (world / _LIVE).is_file():
        out["failed"] += 1
        _note(out, "hypothesis", "-", "failed", "pipeline_missing")
        return
    scan = _scan(export, world)
    marked = {rid for found in scan.values() for rid, rec in found.items() if is_forgotten(rec)}
    for key in sorted(marked | set(retained)):
        held = retained.get(key)
        name = held[0].name if held is not None else "-"
        tick()
        _attempt(out, "hypothesis", name, _hypothesis_one, applier, export, world, env_dir, key, held, scan,
                 out, now, apply=apply, tick=tick)


_GUARDRAILS = "guardrails.jsonl"

#: The most predecessors a lineage walk follows (a restore of a restore of a correction ...). Nobody builds
#: a chain this long by hand, and a bound is what stops a pair of looped tags running a pass forever.
_LINEAGE_DEPTH = 32


def _guardrail_records(export, world: Path) -> dict:
    """``{id: record}`` for every guardrail in the store, the first match for an id."""
    found: dict = {}
    for rec in export._read_jsonl(world / _GUARDRAILS):
        rid = str(rec.get("id") or "").strip()
        if rid:
            found.setdefault(rid, rec)
    return found


def _superseded_reason(applier) -> "re.Pattern[str]":
    """The reason a member's correction retires a guardrail with, with any id: built from the applier's
    own template, so the two cannot drift, and matched in full."""
    before, after = applier._SUPERSEDED_REASON.split("{new_id}")
    return re.compile(re.escape(before) + r"guard-\d+" + re.escape(after))


def _predecessors(applier, node: dict, found: dict) -> list:
    """The retired guardrails ``node`` restored or replaced. A tag alone names one, and a tag is a
    string anybody may write, so the link is believed only when the predecessor agrees: a restore
    needs the predecessor to have been retired by a member's forget, a correction needs the reason it
    was retired with to name ``node``. An active guardrail is never one."""
    me = str(node.get("id") or "").strip()
    head = applier._FORGOTTEN_HEAD
    linked = []
    for tag in applier._tags(node):
        if not isinstance(tag, str):
            continue
        tag = tag.lower()
        for prefix in (applier._RESTORES_TAG, applier._SUPERSEDES_TAG):
            if not tag.startswith(prefix):
                continue
            pred = found.get(tag[len(prefix):])
            reason = pred.get("retirement_reason") if pred is not None else None
            if pred is None or pred is node or pred.get("status") != "retired" or not isinstance(reason, str):
                continue
            if prefix == applier._RESTORES_TAG:
                agrees = _is_marker(reason, head)
            else:
                agrees = reason == applier._SUPERSEDED_REASON.format(new_id=me)
            if agrees:
                linked.append(pred)
    return linked


def _lineage(applier, found: dict, head: dict) -> list | None:
    """Every record the forget of ``head`` leaves text in, each after the records it restored or
    replaced and ``head`` last, so a record is blanked only once the ones its tags lead to are. ``None``
    when the chain is deeper than :data:`_LINEAGE_DEPTH`."""
    order: list = []
    seen: set = set()

    def visit(node: dict, depth: int) -> bool:
        key = str(node.get("id") or "").strip()
        if key in seen:
            return True
        if depth > _LINEAGE_DEPTH:
            return False
        seen.add(key)
        if not all(visit(pred, depth + 1) for pred in _predecessors(applier, node, found)):
            return False
        order.append(node)
        return True

    return order if visit(head, 0) else None


def _guardrail_plan(applier, record: dict, *, tombstone: str, head: str) -> dict:
    """:func:`blank_plan` for a guardrail, less the reason a correction retired it with: that is the
    framework's own sentence (an id and fixed words, no member text), and the lineage walk reads it."""
    plan = blank_plan(record, tombstone=tombstone, head=head, kind="guardrail")
    reason = record.get("retirement_reason")
    if isinstance(reason, str) and _superseded_reason(applier).fullmatch(reason):
        plan.pop("retirement_reason", None)
    return plan


def _write_order(plan: dict) -> list:
    """The statement first, because the store may refuse it (not local) and nothing else should land
    before that is known, and the tags last, because they carry the links a later pass would walk."""
    statement = set(item_text_fields("guardrail"))
    return sorted(plan, key=lambda name: (name not in statement, name == "tags"))


def _settle_guardrail(applier, export, world: Path, record: dict, *, apply: bool,
                      tick=lambda: None) -> tuple[str, dict]:
    """Make the world hold no text of a forgotten guardrail. ``(verdict, did)``: ``"ok"`` or why
    the retained record must stay, and what was (or would be) changed.

    The forgotten record and every record its lineage left behind are blanked, predecessors first. A
    record that fails stops the walk with the later ones untouched, so no record is blanked before
    the ones it names, and the retained record stays for the next pass. ``not_local`` is the store
    refusing the rule's erase on a backend that can merge it with another copy: ``pending``. The rule
    is a record's first write, so for a record whose rule is still to blank nothing else has changed
    when the refusal comes. A dry run cannot ask the store, so what it reports for a guardrail with a
    rule to blank is flagged ``backend_unchecked``."""
    if not (world / _GUARDRAILS).is_file():
        return "guardrails_missing", {}  # a store that is not mounted reads as one holding nothing
    tombstone = applier._FORGOTTEN_TEXT.format(today=applier._today())
    head = applier._FORGOTTEN_HEAD
    found = _guardrail_records(export, world)
    forgotten = found.get(str(record["item_id"]))
    if forgotten is None or forgotten.get("status") != "retired":
        return "ok", {}  # gone, or shown: the forget never took, so nothing here is the member's to blank
    chain = _lineage(applier, found, forgotten)
    if chain is None:
        return "lineage_too_deep", {}
    fields = records = 0
    plans = []
    for node in chain:
        plan = _guardrail_plan(applier, node, tombstone=tombstone, head=head)
        if not all(_writable(name) for name in plan):
            return "unwritable_name", {}  # a write would land on a neighbour, or on nothing: nothing is written
        plans.append((str(node["id"]).strip(), plan))
        fields += len(plan)
        records += bool(plan)
    if apply:
        for node_id, plan in plans:
            failed = False
            for name in _write_order(plan):  # every field is tried: one that fails must not leave the others
                tick()
                outcome = applier._write_guardrail_field(node_id, name, plan[name])
                if outcome == "not_local":
                    return "not_local", {}
                failed = failed or outcome != "ok"
            if failed:
                return "blank_failed", {}
        if fields:
            again = _guardrail_records(export, world)  # read fresh: what the writer left, not what we planned
            for node in chain:
                fresh = again.get(str(node["id"]).strip())
                if fresh is not None and _guardrail_plan(applier, fresh, tombstone=tombstone, head=head):
                    return "text_remains", {}
    did = {"fields_blanked": fields, "records": records} if fields else {}
    if did and not apply and any(set(item_text_fields("guardrail")) & set(plan) for _, plan in plans):
        did["backend_unchecked"] = True
    return "ok", did


def _classify(env_dir: Path, path: Path, out: dict, now, live: dict, *, apply: bool) -> None:
    """Sort one file of the retention directory: residue to purge, or a live record to settle."""
    if path.name.endswith(".json.tmp"):
        _residue(env_dir, path, out, now, apply=apply)
    elif path.suffix == ".json":
        record = knowledge_retention.read_record(path)
        if record is None:
            _residue(env_dir, path, out, now, apply=apply)
        elif knowledge_retention.is_live(record):
            live[(record["kind"], record["item_id"])] = (path, record)


def _settle_other(applier, export, tree_dir, env_dir: Path, kind: str, path: Path, record: dict, out: dict,
                  now, *, apply: bool, world: Path, tick=lambda: None) -> None:
    """One retained record that is not a hypothesis and whose window is over."""
    if kind == "node":
        verdict, did = _settle_node(applier, export, tree_dir, record, apply=apply)
    elif kind == "guardrail":
        verdict, did = _settle_guardrail(applier, export, world, record, apply=apply, tick=tick)
    else:
        out["pending"] += 1
        _note(out, kind, path.name, "pending", "a kind this sweep does not know")
        return
    if verdict in _PENDING:
        out["pending"] += 1
        _note(out, kind, path.name, "pending", _PENDING[verdict])
        return
    if verdict != "ok":
        out["failed"] += 1
        _note(out, kind, path.name, "failed", verdict)
        return
    if did and apply:
        out["world_changed"] += 1
    _erase_retained(env_dir, path, record, out, now, apply=apply, world=did)


def sweep(env_dir, retention_dir, applier, *, apply: bool, now: datetime.datetime | None = None,
          tick=None) -> dict:
    """One pass over ``retention_dir``. ``applier`` is the loaded ``knowledge-edit-apply`` module,
    which stays the one authority on how a forgotten item is blanked. Returns counts and an
    ``entries`` list that carries no text and no item id.

    A dry run (``apply=False``) writes and deletes nothing and reports what it would. ``tick`` is
    called before each record and before each daemon write, so a caller that holds a lock can show
    it is still held through a pass that outlasts the lock's staleness.

    Everything the pass depends on is resolved before anything is deleted, so a drifted applier or
    export, or a world that cannot be found (an ``EraseError``, never the export's own exit), ends
    the pass with nothing done. After that a record that cannot be settled is that record's failure,
    and the pass goes on.
    """
    missing = [name for name in APPLIER_NAMES if not hasattr(applier, name)]
    if missing:
        raise EraseError("the knowledge applier lacks " + ", ".join(missing))
    env_dir, retention_dir = Path(env_dir), Path(retention_dir)
    now = _aware(now or knowledge_retention.now_utc())
    tick = tick or (lambda: None)
    out = _result(apply)
    export = applier._load_export_mod()
    gone = [name for name in EXPORT_NAMES if not hasattr(export, name)]
    if gone:
        raise EraseError("the knowledge export lacks " + ", ".join(gone))
    try:
        world = export._resolve_world()  # resolved before a file is touched
    except SystemExit:  # the export exits with a message of its own, and a message is not this pass's to pass on
        raise EraseError("the knowledge export could not resolve the world: WORLD_PATH is not set") from None
    tree_dir = export._resolve_tree_dir(world)
    try:
        files = sorted(p for p in retention_dir.iterdir() if p.is_file())
    except OSError:
        return out
    live: dict = {}
    for path in files:
        tick()
        _attempt(out, "residue", path.name, _classify, env_dir, path, out, now, live, apply=apply)
    for (kind, _key), (path, record) in sorted(live.items(), key=lambda kv: kv[1][0].name):
        if kind == "hypothesis" or knowledge_retention.in_window(record, now):
            continue
        tick()
        _attempt(out, kind, path.name, _settle_other, applier, export, tree_dir, env_dir, kind, path, record,
                 out, now, apply=apply, world=world, tick=tick)
    _attempt(out, "hypothesis", "-", _hypotheses, applier, export, world, env_dir,
             {k[1]: v for k, v in live.items() if k[0] == "hypothesis"}, out, now, apply=apply, tick=tick)
    return out
