#!/usr/bin/env python3
"""append-only-stub-guard.py — refuse to stage an append-only store that lost
rows HEAD holds (g-115-11674).

WHY. When an integrate fails while the tree is being appended to, a store's
working copy can come back as a near-empty NEW file (measured on cc-06: one
heartbeat row against 493,329 at HEAD). The churn self-heal and the goal commit
then staged it, so a commit recorded the deletion. The merge driver restores
HISTORY from the other side's full copy, but rows this box had not pushed yet
are in neither side and are gone, and a delete-aware merge rule would keep the
deletion itself.

WHAT. For each given path an append-only merge handler owns
(merge_append_only_jsonl / merge_rotated_board_jsonl): if the working copy is
smaller than the HEAD blob AND lacks lines HEAD holds, and the missing lines are
not a front-slice rotation (coordination_merge._front_evicted_lines), rewrite the
working copy as the union of HEAD's rows and its own, under the store's write
lock, so the commit keeps both. A path that cannot be repaired is printed on
stdout: the caller must NOT stage it this time.

Never blocks a commit. A store it checked and could not repair is logged and
left out of this commit. A file it cannot classify (no handler, not at HEAD, an
unreadable size) is staged as before, and so is everything when the merge handler
registry cannot be imported: the guard says so and leaves nothing out. It looks
only at a working copy smaller than HEAD's blob, so a stub that has since grown
past it is not caught.

Usage: append-only-stub-guard.py --repo <dir> <path> [<path>...]
  stdout: paths the caller must not stage (one per line)
  stderr: one line per finding
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

TAG = "[append-only-stub-guard]"


def _log(msg: str) -> None:
    print(f"{TAG} {msg}", file=sys.stderr)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True)


def _lines(data: bytes) -> list:
    return [ln for ln in data.decode("utf-8", "replace").splitlines() if ln.strip()]


def check_path(repo: Path, rel: str, handlers, front_evicted, union, lock) -> bool:
    """Return True when the caller may stage `rel`."""
    try:
        h = handlers(rel)
    except Exception:
        return True
    if getattr(h, "__name__", "") not in ("merge_append_only_jsonl", "merge_rotated_board_jsonl"):
        return True
    work = repo / rel
    if not work.is_file():
        return True  # a deletion is the caller's own decision
    size = _git(repo, "cat-file", "-s", f"HEAD:{rel}")
    if size.returncode != 0:
        return True  # not at HEAD: nothing to lose
    try:
        if work.stat().st_size >= int(size.stdout.strip() or 0):
            return True  # an append-only store only shrinks by rotation or loss
    except (OSError, ValueError):
        return True
    head = _git(repo, "show", f"HEAD:{rel}")
    if head.returncode != 0:
        _log(f"{rel}: HEAD blob unreadable — not staged this time")
        return False
    head_lines = _lines(head.stdout)
    work_bytes = work.read_bytes()
    work_lines = _lines(work_bytes)
    held = set(work_lines)
    lost = [ln for ln in head_lines if ln not in held]
    if not lost:
        return True
    if front_evicted(head_lines, work_lines) == set(lost):
        return True  # a rotation moved the oldest rows out
    new = len(set(work_lines) - set(head_lines))
    lock_path = work.with_suffix(".lock")
    try:
        lock.acquire(lock_path)
    except Exception as exc:  # noqa: BLE001 — fail toward not staging
        _log(f"REFUSED {rel}: lost {len(lost)} of {len(head_lines)} HEAD row(s), "
             f"lock unavailable ({exc}) — not staged this time")
        return False
    try:
        current = work.read_bytes()  # re-read under the lock: appends may have landed
        merged = union(head.stdout, current)
        tmp = work.with_name(work.name + ".stub-guard.tmp")
        tmp.write_bytes(merged)
        os.replace(tmp, work)
    except Exception as exc:  # noqa: BLE001
        _log(f"REFUSED {rel}: lost {len(lost)} of {len(head_lines)} HEAD row(s), "
             f"repair failed ({exc}) — not staged this time")
        return False
    finally:
        lock.release(lock_path)
    _log(f"REPAIRED {rel}: the working copy lost {len(lost)} of {len(head_lines)} HEAD row(s) "
         f"(not a rotation) — restored them and kept {new} new row(s) before staging")
    return True


class _Lock:
    def __init__(self):
        from _fileops import acquire_lock, release_lock
        self.acquire = acquire_lock
        self.release = release_lock


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("paths", nargs="*")
    args = ap.parse_args(argv)
    repo = Path(args.repo)
    if not args.paths:
        return 0
    try:
        from coordination_merge import (
            _front_evicted_lines, merge_append_only_jsonl, merge_handler_for,
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"merge handlers unavailable ({exc}) — guard skipped")
        return 0
    lock = None
    for rel in args.paths:
        rel = rel.replace("\\", "/")
        if not rel.endswith(".jsonl"):
            continue
        if lock is None:
            try:
                lock = _Lock()
            except Exception as exc:  # noqa: BLE001
                _log(f"lock helpers unavailable ({exc})")

                class _NoLock:
                    def acquire(self, p):
                        raise RuntimeError("no lock helper")

                    def release(self, p):
                        pass
                lock = _NoLock()
        try:
            ok = check_path(repo, rel, merge_handler_for, _front_evicted_lines,
                            merge_append_only_jsonl, lock)
        except Exception as exc:  # noqa: BLE001
            _log(f"{rel}: check failed ({exc}) — not staged this time")
            ok = False
        if not ok:
            print(rel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
