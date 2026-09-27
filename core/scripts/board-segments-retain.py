#!/usr/bin/env python3
"""MOVE board date segments older than N days into <channel>-archive.jsonl ().

THE GAP. g-358-183's flip (BOARD_SEGMENTED_CHANNELS) sends every new post on a
named channel to a <channel>-YYYY-MM-DD.jsonl date segment, and nothing else
bounds those segments. store-hygiene's only board entry is a 5000-line rotate
PER FILE, a no-op on a ~686-post day, and jsonl_hygiene has no whole-file mode.
Without this script, the live half of a channel grows forever, and every reader
that passes include_archive=False parses all of it on every run.

THE VERB IS MOVE, NEVER DELETE. Today board retention means keep-forever, via
the archive: rotation moves posts into <channel>-archive.jsonl, and board
archives carry no age cap. Deleting old segments would destroy coordination
history the fleet keeps, and claim posts are load-bearing evidence
(guard-1460). So each segment's records are appended to its channel archive
and verified there by ID SET against the STORE copy (guard-2603); only then is
the segment removed. From the gate-firings twin (gate-firings-segments-expire.py,
g-358-10) this script takes the CLI discipline and leaves the verb: dry run by
default, a lane-agreement check before anything is touched, and a verified cold
copy plus RECEIPT before the delete. A segment removal is a store deletion, so
archive-before-delete.md applies even though its records survive in the channel
archive. That archive is live and fence-only, so it is inside the blast radius.

N = RETENTION_DAYS = 10. Every record-reader except the daemon read endpoint
passes include_archive=False, so after the flip it sees only the live file plus
segments, and N must cover its window. Census at HEAD (2026-09-27):
  - insight-trigger-gate: 24h
  - session-digest: 24h
  - gates/goal_duplication: 48h
  - insight-trigger-sweep: 24h, audit 168h
  - notification_outreach: 168h default
  - wm-contamination-check: 7d
  - goal-selector directive admission: the whole live half, filtered by
    directive expiry (seen up to 72h)
  - peer_liveness: 3h
The maximum is 7d. N=10 adds 3 days for the boundary day and a missed tick.
Longer windows, such as the 96h directive-honor read and the 720h scans, go
through mind_api/src/endpoints/board.py. That endpoint's archive reach also
fires when the cutoff predates the earliest SEGMENT record (changed in the same
commit), and that is what keeps a moved day readable. N=10 also matches today's
live window (about 9 days of coordination under the 5000-line rotate), so the
flip does not grow what those readers parse.

BATCH_DAYS = 7. Each move re-PUTs the channel archive once, about 46 MB for
coordination. So a channel is moved only when its oldest expired segment is at
least BATCH_DAYS past the cutoff, and then every expired segment goes in a
single archive write. That is about one archive PUT per channel per week
instead of one per day. Readers do not notice: a segment waiting for its batch
is still in the live half.

OLDEST FIRST, CONTIGUOUS. A channel's batch stops at the first segment that
cannot be moved, so what reached the archive is always the oldest run of
segments. board.py's reach keys on the earliest segment still present, so a
hole in the middle of the run would hide the moved days after it.

IDEMPOTENT ACROSS BOXES. Board rotation is not serialized (g-358-119), and
neither is this script. The archive write is one locked_modify_jsonl cycle, the
same writer rotation uses. The archive is class (b) fence-only, and that helper
refreshes and re-reads inside every conflict retry. The cycle appends only keys
the archive does not already hold, so a repeat run, or a second box, finds the
ids already archived and only deletes. Existing re-archived duplicates in the
archive (g-358-119) are left alone; this script never rewrites them.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _board_paths  # noqa: E402  -- SSOT for segment and archive names

RETENTION_DAYS = 10
BATCH_DAYS = 7


def segment_date(name: str):
    """(channel, date) for a real board date segment, else None.

    The shape test is _board_paths.segment_parent, the reader's own matcher. A
    loose glob would admit <channel>-archive.jsonl and <channel>-reads.jsonl
    receipts, and moving either would be destructive.
    """
    channel = _board_paths.segment_parent(name)
    if channel is None:
        return None
    try:
        return channel, _dt.date.fromisoformat(name[len(channel) + 1:-len(".jsonl")])
    except ValueError:
        return None


def channel_segments(board_dir):
    """{channel: [(date, path), ...]} for every date segment, oldest first."""
    out = {}
    for p in sorted(Path(board_dir).glob("*.jsonl")):
        hit = segment_date(p.name)
        if hit is None or not p.is_file():
            continue
        out.setdefault(hit[0], []).append((hit[1], p))
    for segs in out.values():
        segs.sort()
    return out


def plan(segments, cutoff: _dt.date, batch_days: int):
    """Per channel: the expired segments (strictly older than `cutoff`, so the
    boundary day is kept) and whether the batch is due.

    A channel's NEWEST segment is never moved. It anchors board.py's archive
    reach: the endpoint opens the archive when the cutoff predates the earliest
    segment still present. On a channel quiet long enough for every segment to
    expire, moving them all would leave no anchor, and the base file's older
    posts would hide the moved days from every read. Keeping one costs one small
    file per channel."""
    rows = {}
    for channel, segs in segments.items():
        expired = [(d, p) for d, p in segs[:-1] if d < cutoff]
        due = bool(expired) and (cutoff - expired[0][0]).days >= batch_days
        rows[channel] = {"present": len(segs), "expired": expired, "due": due}
    return rows


def record_key(rec: dict):
    """Dedup identity: the post id. A record without one falls back to its
    canonical serialization, so it is still archived exactly once."""
    rid = rec.get("id")
    return ("id", rid) if rid else ("json", json.dumps(rec, sort_keys=True))


def parse_records(raw: bytes):
    """(records, malformed_line_count) from JSONL bytes. Board readers skip a
    malformed line, so it is reported here, and kept in the cold copy."""
    records, malformed = [], 0
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        if isinstance(obj, dict):
            records.append(obj)
        else:
            malformed += 1
    return records, malformed


def append_new(archive_path: Path, records: list):
    """Append each record whose key the archive lacks, in ONE locked cycle.
    Returns (appended, already_archived)."""
    from _fileops import locked_modify_jsonl
    counts = {}

    def _append(arch):
        arch = arch or []
        have = {record_key(r) for r in arch if isinstance(r, dict)}
        new = []
        for r in records:
            k = record_key(r)
            if k not in have:
                have.add(k)
                new.append(r)
        counts["appended"] = len(new)
        counts["already"] = len(records) - len(new)
        return arch + new

    locked_modify_jsonl(archive_path, _append)
    return counts["appended"], counts["already"]


def store_keys(be, path: Path) -> set:
    """Keys in the STORE copy of `path`. Never the local mirror: *-archive.jsonl
    is excluded from pull_sweep, so a box's local archive can be frozen
    (g-358-117)."""
    records, _ = parse_records(be.read_authoritative_bytes(str(path.resolve())))
    return {record_key(r) for r in records}


def _md5(p: Path) -> str:
    h = hashlib.md5()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cold_copy(name: str, data: bytes, archive_dir: Path) -> dict:
    """Write `data`, the bytes the lane check read, into the cold dir, and
    verify the copy by size and md5.

    It copies from those bytes rather than re-opening the segment, so a peer
    that removes the file mid-run cannot make this copy something other than
    what gets archived. A byte-identical copy already present is reused, so a
    re-run after a failed delete proceeds. A DIFFERENT file of the same name
    refuses: never overwrite what a receipt already names.
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    dst = archive_dir / name
    want = (hashlib.md5(data).hexdigest(), len(data))
    if dst.exists():
        if (_md5(dst), dst.stat().st_size) != want:
            raise FileExistsError(f"cold copy differs, refusing to overwrite: {dst}")
    else:
        dst.write_bytes(data)
        if (_md5(dst), dst.stat().st_size) != want:
            raise OSError(f"cold copy verify FAILED for {name}")
    return {"name": name, "bytes": want[1], "md5": want[0], "copied_to": str(dst)}


def write_receipt(archive_dir: Path, moved: list, channels: dict, cutoff, days,
                  board_dir) -> str:
    """RECEIPT.md at the cold dir's TOP LEVEL (archive-before-delete step 6).
    The name is load-bearing: temp-drain-purge.sh preserves a directory that
    carries it. A second run into the same directory APPENDS."""
    path = archive_dir / "RECEIPT.md"
    stamp = _dt.datetime.now().isoformat(timespec="seconds")
    lines = [
        f"# RECEIPT - board date segments moved into their channel archives, {stamp}",
        "",
        f"WHAT: {len(moved)} date segment file(s) removed from `{board_dir}` by "
        "`core/scripts/board-segments-retain.py --apply` (g-358-221). Their RECORDS "
        "were not deleted: each one is in `<channel>-archive.jsonl`, and the board "
        "read endpoint reaches it there.",
        "",
        f"WHY: retention is {days} days, so today's cutoff is {cutoff.isoformat()}. "
        "Every file below is dated strictly before it.",
        "",
        "HOW: for each file, the store copy (where one existed) matched the local "
        "bytes, the file was copied here and verified by size and md5, and its "
        "records were appended to the channel archive by id (ids already archived "
        "were skipped). Every id was then confirmed present in the STORE copy of "
        "that archive. Only after that confirmation was the file removed, through "
        "`StorageBackend.delete()`, which drops both lanes and re-verifies them.",
        "",
        "ENUMERATION (name / bytes / md5 / records / malformed lines):",
    ]
    for m in moved:
        lines.append(f"  - {m['name']}  {m['bytes']}  {m['md5']}  {m['records']}  "
                     f"{m['malformed']}")
    lines.append("")
    lines.append("ARCHIVE APPEND per channel (new records / already archived):")
    for channel, c in channels.items():
        if c["moved"]:
            lines.append(f"  - {_board_paths.archive_name(channel)}  "
                         f"{c['appended']}  {c['already_archived']}")
    lines += [
        "",
        "RESTORE: to READ these posts, read the channel with a --since window that "
        "reaches their day; no restore is needed. To put a segment FILE back, copy "
        "it from here into the board directory named above, NOT into "
        "<channel>.jsonl and NOT into the archive (both would duplicate ids). On an "
        "own-cloud box, push it with `python3 core/scripts/owncloud_sync.py --file "
        "<absolute path>` and confirm the store copy's md5 matches this receipt. A "
        "restored segment older than the cutoff is removed again at the next due "
        "tick; since its ids are already archived, that pass only deletes it. To "
        "keep it, raise RETENTION_DAYS first.",
        "",
    ]
    prior = path.read_text(encoding="utf-8") + "\n" if path.exists() else ""
    path.write_text(prior + "\n".join(lines), encoding="utf-8")
    return str(path)


def move_channel(be, board_dir: Path, channel: str, expired: list,
                 archive_dir: Path, rep: dict) -> list:
    """Move one channel's expired segments, oldest first. Returns receipt rows.
    Every refusal stops the channel, which keeps the moved run contiguous."""
    crep = rep["channels"][channel]
    agreed = []
    for _d, p in expired:
        try:
            local = p.read_bytes()
        except FileNotFoundError:
            # Gone since enumeration: a concurrent run (this box or a peer)
            # moved it. It is no longer in the live half, so it cannot open a
            # hole in the run, and skipping it is correct.
            crep["vanished"].append(p.name)
            continue
        except OSError as e:
            crep["errors"].append(f"{p.name}: local read failed ({e}); stopping")
            break
        try:
            store = be.read_authoritative_bytes(str(p.resolve()))
        except FileNotFoundError:
            store = None  # a peer already moved it; the local copy is what is left
        except Exception as e:  # noqa: BLE001 -- an unreadable lane is never deleted blind
            crep["errors"].append(f"{p.name}: store read failed ({e}); stopping")
            break
        if store is not None and store != local:
            crep["errors"].append(
                f"{p.name}: lanes diverged (store md5 {hashlib.md5(store).hexdigest()} "
                f"!= local {hashlib.md5(local).hexdigest()}); not moving it or anything newer")
            break
        agreed.append((p, local))
    if not agreed:
        return []

    rows, records = [], []
    for p, data in agreed:
        try:
            row = cold_copy(p.name, data, archive_dir)
        except Exception as e:  # noqa: BLE001 -- abort before any archive write
            crep["errors"].append(f"{p.name}: cold copy failed ({e}); nothing moved")
            return []
        recs, malformed = parse_records(data)
        row.update(records=len(recs), malformed=malformed,
                   keys={record_key(r) for r in recs})
        rows.append(row)
        records.extend(recs)

    archive = board_dir / _board_paths.archive_name(channel)
    # Read the store archive first. A repeat run (or a second box) finds every
    # key already there and skips the write: locked_modify_jsonl rewrites the
    # whole archive unconditionally, about 46 MB on coordination.
    try:
        held = store_keys(be, archive)
    except FileNotFoundError:
        held = set()
    except Exception as e:  # noqa: BLE001 -- unverified means not deleted
        crep["errors"].append(f"archive store read failed ({e}); nothing moved")
        return []
    if any(record_key(r) not in held for r in records):
        appended, already = append_new(archive, records)
        try:
            held = store_keys(be, archive)
        except Exception as e:  # noqa: BLE001 -- unverified means not deleted
            crep["errors"].append(f"archive store read failed ({e}); nothing deleted")
            return []
    else:
        appended, already = 0, len(records)
    crep["appended"], crep["already_archived"] = appended, already
    for row in rows:
        missing = row["keys"] - held
        if missing:
            crep["errors"].append(
                f"{row['name']}: {len(missing)} record(s) not in the store archive; "
                "not deleting it or anything newer")
            rows = rows[:rows.index(row)]
            break

    moved = []
    for row in rows:
        p = board_dir / row["name"]
        try:
            be.delete(str(p.resolve()))
            if p.exists():
                raise OSError("still present locally after delete")
        except Exception as e:  # noqa: BLE001
            crep["errors"].append(f"{row['name']}: delete failed ({e}); stopping")
            break
        crep["moved"].append(row["name"])
        moved.append({k: v for k, v in row.items() if k != "keys"})
    return moved


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Move board date segments older than N days into their channel archive.")
    ap.add_argument("--apply", action="store_true",
                    help="perform the move (default: dry run, report only)")
    ap.add_argument("--archive-dir",
                    help="REQUIRED with --apply: cold directory for a verified copy of each "
                         "segment plus RECEIPT.md, outside the governed roots and outside "
                         "an agent's temp/ (reaped at 120 min)")
    ap.add_argument("--channel", action="append", default=None,
                    help="only these channel(s) (repeatable); default every channel")
    ap.add_argument("--retention-days", type=int, default=RETENTION_DAYS)
    ap.add_argument("--batch-days", type=int, default=BATCH_DAYS)
    ap.add_argument("--board-dir", help="override the resolved board directory (tests)")
    ap.add_argument("--today", help="override today's date, ISO (tests)")
    a = ap.parse_args(argv)

    if a.board_dir:
        board_dir = Path(a.board_dir)
    else:
        from _paths import WORLD_DIR
        board_dir = Path(WORLD_DIR) / "board" if WORLD_DIR else None
    if board_dir is None:
        # Never let "unresolved" look like "no segments".
        print("[board-retain] WORLD_DIR unresolved - cannot enumerate segments",
              file=sys.stderr)
        return 3

    today = _dt.date.fromisoformat(a.today) if a.today else _dt.date.today()
    cutoff = today - _dt.timedelta(days=a.retention_days)
    segments = channel_segments(board_dir)
    if a.channel:
        segments = {c: s for c, s in segments.items() if c in set(a.channel)}
    rows = plan(segments, cutoff, a.batch_days)

    rep = {
        "board_dir": str(board_dir), "retention_days": a.retention_days,
        "batch_days": a.batch_days, "today": today.isoformat(),
        "cutoff": cutoff.isoformat(),
        # The unfiltered population beside the filtered one (guard-2298):
        # "0 due" next to "0 present" found nothing to look at.
        "segments_present": sum(r["present"] for r in rows.values()),
        "channels": {c: {"present": r["present"],
                         "expired": [p.name for _, p in r["expired"]],
                         "due": r["due"], "moved": [], "vanished": [], "appended": 0,
                         "already_archived": 0, "errors": []}
                     for c, r in sorted(rows.items())},
        "applied": False, "receipt": None, "errors": [],
    }
    due = [c for c, r in sorted(rows.items()) if r["due"]]

    if not a.apply or not due:
        rep["action"] = "would-move" if due else "noop"
        print(json.dumps(rep, indent=2))
        return 0
    if not a.archive_dir:
        rep["action"] = "refused-no-archive-dir"
        rep["errors"].append(
            "--apply requires --archive-dir: removing a segment is a store deletion, "
            "and board/ is snapshot-blacklisted, so .history holds no copy "
            "(archive-before-delete step 2).")
        print(json.dumps(rep, indent=2))
        return 2

    import storage_backend  # local import: tests pin STORAGE_BACKEND first
    be = storage_backend.get_backend()
    archive_dir = Path(a.archive_dir)
    moved = []
    for channel in due:
        moved.extend(move_channel(be, board_dir, channel, rows[channel]["expired"],
                                  archive_dir, rep))
    if moved:
        rep["receipt"] = write_receipt(archive_dir, moved, rep["channels"], cutoff,
                                       a.retention_days, board_dir)
    errors = [e for c in rep["channels"].values() for e in c["errors"]]
    rep["applied"] = True
    rep["action"] = "moved" if not errors else ("partial" if moved else "refused")
    print(json.dumps(rep, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
