#!/usr/bin/env python3
"""Drop the rows of git-ignored trees from an agent's uncommitted-edits.jsonl.

Until g-115-11716 bash-edit-record.sh logged every hook touch of core/logs/ (and
every bytecode file under core/.pycache/), so one agent's log held 159,548 rows
and 27 MB, 97% of them paths git ignores. Fixing the recorder stops new rows; the
rows already there are re-read by every Bash call and re-uploaded with the log
until something removes them. This is that something.

  py -3 core/scripts/uncommitted-edits-prune.py [--agent NAME] [--dry-run]

Only a row positively classed as a skipped tree under core/ or .claude/ is
dropped (_edit_record_skip.is_skipped_path); every other row, parseable or not, is
kept (rb-1794: absence of proof keeps the row). Order is preserved.

archive-before-delete: the whole original is gzipped under
<agent>/temp/uncommitted-edits-prune-<UTC>/ (.archive-marker first, so the temp
purge keeps the directory), read back and compared by sha256 and length, and only
then is the log replaced. RECEIPT.json records the counts and how to restore.

Both recorders append without a lock, so the swap cannot be made atomic against
them. A row they append between the read and the swap is copied across before the
swap; a row written through a handle opened before the swap lands in the old file,
which stays open for --settle-s seconds and is copied into the new one. Those rows
are copied as they are, junk or not: a row the archive does not hold is never
dropped (a second run removes any such junk). The one loss left is a recorder that
held the old file open across the swap and wrote more than --settle-s later
(sub-millisecond between its open and its write). A last line with no newline is a
write in flight, not a row: it is copied only if its writer finishes it inside
that window, and the summary counts the bytes left behind.

Written and tested for POSIX only: the swap renames over a file this process
still has open. It has not been run on Windows.

Exit: 0 pruned, nothing to prune, or dry run; 1 failed (the message says whether
the log was touched: only a failure after the swap leaves it replaced, and the
archive then holds the original); 2 usage.
"""
import argparse
import gzip
import hashlib
import json
import os
import socket
import stat
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _edit_record_skip import is_skipped_path  # noqa: E402

LOG_NAME = "uncommitted-edits.jsonl"
ATTEMPTS = 3


class PruneError(Exception):
    pass


def _is_skipped_row(raw):
    try:
        return is_skipped_path(str(json.loads(raw).get("file") or ""))
    except Exception:  # unparseable, or not an object: no proof, so the row stays
        return False


def _split_rows(data):
    """(complete rows without their newline, bytes consumed). A row still being
    written has no newline yet and is left for the next read."""
    end = data.rfind(b"\n") + 1
    return data[:end].split(b"\n")[:-1], end


def _catch_up(src, out, pos):
    """Copy the complete rows appended to `src` past `pos` into `out`, as they are:
    a row the archive does not hold is never dropped. Returns (new pos, rows copied)."""
    size = os.fstat(src.fileno()).st_size
    if size <= pos:
        return pos, 0
    src.seek(pos)
    chunk = src.read(size - pos)
    rows, consumed = _split_rows(chunk)
    out.write(chunk[:consumed])
    out.flush()
    return pos + consumed, sum(1 for r in rows if r.strip())


def _write_archive(data, gz_path, mode):
    with open(gz_path, "wb") as raw:
        with gzip.GzipFile(filename=LOG_NAME, mode="wb", fileobj=raw, mtime=0) as gz:
            gz.write(data)
        raw.flush()
        os.fsync(raw.fileno())
    os.chmod(gz_path, mode)


def _verify_archive(gz_path, sha256, length):
    h = hashlib.sha256()
    n = 0
    try:
        with gzip.open(gz_path, "rb") as g:
            for chunk in iter(lambda: g.read(1 << 20), b""):
                h.update(chunk)
                n += len(chunk)
    except (OSError, EOFError, zlib.error) as e:  # a torn or corrupt archive is a refusal, not a crash
        raise PruneError(f"archive {gz_path} could not be read back ({type(e).__name__}: {e}); "
                         f"the log was not touched")
    if h.hexdigest() != sha256 or n != length:
        raise PruneError(f"archive {gz_path} read back as {n} bytes sha256 {h.hexdigest()}, "
                         f"expected {length} bytes sha256 {sha256}; the log was not touched")


def _count_rows(path):
    with open(path, "rb") as f:
        return sum(1 for line in f if line.strip())


def _prefix(raw):
    try:
        return "/".join(str(json.loads(raw).get("file") or "").replace("\\", "/").split("/")[:2])
    except Exception:
        return ""


def _attempt(log, archive_dir, settle_s, poll_s):
    """One read-archive-rewrite-swap pass. None when the log was replaced by
    someone else meanwhile (the caller starts over)."""
    src = open(log, "rb")
    swapped = False
    try:
        st = os.fstat(src.fileno())
        ident = (st.st_dev, st.st_ino)
        mode = stat.S_IMODE(st.st_mode)
        data = src.read()
        rows, pos = _split_rows(data)
        drop = [_is_skipped_row(r) for r in rows]
        summary = {"before": {"bytes": len(data), "rows": sum(1 for r in rows if r.strip())},
                   "dropped_rows": sum(drop)}
        by_prefix = {}
        for r, d in zip(rows, drop):
            if d:
                p = _prefix(r)
                by_prefix[p] = by_prefix.get(p, 0) + 1
        summary["dropped_by_prefix"] = dict(sorted(by_prefix.items(), key=lambda kv: -kv[1]))
        if not any(drop):
            summary["status"] = "nothing-to-prune"
            return summary

        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / ".archive-marker").write_text(
            f"uncommitted-edits-prune of {log} on {socket.gethostname()}: RECEIPT.json names the restore\n")
        sha256 = hashlib.sha256(data).hexdigest()
        gz_path = archive_dir / (LOG_NAME + ".gz")
        _write_archive(data, gz_path, mode)
        _verify_archive(gz_path, sha256, len(data))
        summary["archive"] = {"path": str(gz_path), "original_bytes": len(data), "original_sha256": sha256,
                              "gz_bytes": gz_path.stat().st_size}

        tmp = log.with_name(f"{log.name}.prune-{os.getpid()}.tmp")
        tail = 0
        try:
            with open(tmp, "wb") as out:
                for row, d in zip(rows, drop):
                    if not d:
                        out.write(row + b"\n")
                while True:  # rows appended since the read
                    new_pos, n = _catch_up(src, out, pos)
                    tail += n
                    if new_pos == pos:
                        break
                    pos = new_pos
                os.fsync(out.fileno())
            os.chmod(tmp, mode)
            now = os.stat(log)
            if (now.st_dev, now.st_ino) != ident:
                return None
            os.replace(tmp, log)
            swapped = True
        finally:
            if tmp.exists():
                tmp.unlink()

        # `src` is still the old file. A recorder that opened it before the swap
        # writes into it, so its rows are copied into the new file until it is quiet.
        late = 0
        with open(log, "ab") as new:
            deadline = time.monotonic() + settle_s
            while True:
                pos, n = _catch_up(src, new, pos)
                late += n
                if time.monotonic() >= deadline:
                    break
                time.sleep(poll_s)
        summary["rows_appended_during_run"] = {"before_swap": tail, "after_swap": late}
        summary["unterminated_tail_bytes_left_behind"] = os.fstat(src.fileno()).st_size - pos
        summary["status"] = "pruned"
        summary["after"] = {"bytes": log.stat().st_size, "rows": _count_rows(log)}
        return summary
    except OSError as e:
        raise PruneError(f"{type(e).__name__}: {e}; " + (
            "the log was replaced and the archive holds the original" if swapped else "the log was not touched")) from e
    finally:
        src.close()


def prune(log, archive_dir, dry_run=False, settle_s=0.5, poll_s=0.05):
    """Prune `log`; see the module docstring. Returns the summary dict."""
    log = Path(log)
    archive_dir = Path(archive_dir)
    if dry_run:
        with open(log, "rb") as f:
            data = f.read()
        rows, _ = _split_rows(data)
        skipped = [r for r in rows if _is_skipped_row(r)]
        return {"status": "dry-run", "before": {"bytes": len(data), "rows": sum(1 for r in rows if r.strip())},
                "would_drop_rows": len(skipped)}
    for _ in range(ATTEMPTS):
        summary = _attempt(log, archive_dir, settle_s, poll_s)
        if summary is not None:
            break
    else:
        raise PruneError(f"{log} was replaced by another writer on each of {ATTEMPTS} attempts; the log is unchanged")
    if summary["status"] == "pruned":
        receipt = dict(summary, host=socket.gethostname(), log=str(log),
                       pruned_at=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                       rule="rows whose file is under a directory _edit_record_skip.skip_dir refuses, inside core/ and .claude/",
                       restore=("gunzip -c uncommitted-edits.jsonl.gz > <somewhere else> and take the rows you need from it. "
                                "Do NOT put it back over the live log: that restores the junk rows the recorder no longer "
                                "writes, and the log is re-read by every Bash call and re-uploaded on every change."))
        (archive_dir / "RECEIPT.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        summary["receipt"] = str(archive_dir / "RECEIPT.json")
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description="Drop git-ignored-tree rows from an agent's uncommitted-edits.jsonl.")
    ap.add_argument("--agent", help="agent whose log to prune (default: $MIND_AGENT)")
    ap.add_argument("--log", help="log file (default: <agent>/session/uncommitted-edits.jsonl)")
    ap.add_argument("--archive-root", help="where the archive directory goes (default: <agent>/temp)")
    ap.add_argument("--dry-run", action="store_true", help="count what would be dropped; write nothing")
    ap.add_argument("--settle-s", type=float, default=0.5, help="seconds the old file stays open after the swap")
    args = ap.parse_args(argv)

    log, root = args.log, args.archive_root
    if not (log and root):
        agent = args.agent or os.environ.get("MIND_AGENT", "")
        if not agent:
            print("uncommitted-edits-prune: no agent: pass --agent or set MIND_AGENT", file=sys.stderr)
            return 2
        from _paths import agent_dir
        base = agent_dir(agent)
        log = log or str(base / "session" / LOG_NAME)
        root = root or str(base / "temp")
    if not os.path.isfile(log):
        print(json.dumps({"status": "no-log", "log": log}))
        return 0
    # The pid keeps two runs in one second from sharing, and so overwriting, an archive.
    archive_dir = Path(root) / (f"uncommitted-edits-prune-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{os.getpid()}")
    try:
        summary = prune(log, archive_dir, dry_run=args.dry_run, settle_s=args.settle_s)
    except (PruneError, OSError) as e:
        print(f"uncommitted-edits-prune: FAILED on {log}: {e}", file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
