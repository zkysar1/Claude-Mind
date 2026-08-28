#!/usr/bin/env python3
"""Renumber ONE aspiration (and its goal ids) inside an aspirations JSONL store.

Why this exists (measured 2026-08-28, a served deployment): the agent-local store minted
``asp-002`` while the world store already held an ``asp-002`` — the allocator scanned only
the store it was writing to (fixed in the daemon since; see
``_sibling_aspiration_stores``). Every consumer that resolves a bare ``asp-NNN`` /
``g-NNN-NN`` (the selector, claims, board posts, the tree) then saw two aspirations and two
goal-id sequences behind one name. The fix for the data already on disk is a renumber, and
a renumber by hand is exactly the store-bypass the framework forbids — so it is a script.

What it does:
  - finds the record whose ``id`` is ``--from`` in ``--file``;
  - rewrites, INSIDE THAT RECORD ONLY, every token ``asp-<from>`` → ``asp-<to>`` and every
    goal id ``g-<from>-NN`` → ``g-<to>-NN`` — in any string field at any depth (``id``,
    ``goals[].id``, ``aspiration``, ``blocked_by``, ``depends_on``, ``parent``,
    ``origin_signal``, free text — the field list is not enumerated because a missed field
    is a dangling reference nobody can see);
  - REPORTS (never rewrites) the same tokens found in the file's OTHER records and in any
    ``--scan`` files (working memory, team-state shards, claims): those may mean the
    colliding sibling, and only a reader can tell;
  - refuses when ``--to`` already exists in the file or in any ``--sibling`` store;
  - with ``--drop-stray-goal-records``, drops TOP-LEVEL records that are really goals of
    the renumbered family (``id`` = ``g-<from>-NN``, ``aspiration`` = ``asp-<from>`` or
    absent) whenever the target record already holds a goal with that id — the shape a
    hand-written store leaves behind (measured: six such duplicates carrying stale
    statuses beside the real goals). A stray with NO inner twin is kept and reported —
    the script never discards the only copy of anything.

Safety:
  - Dry-run by default: prints the JSON report and touches nothing. ``--apply`` writes.
  - Writes through ``_fileops.locked_write_jsonl`` so the lock, the ``.history`` snapshot
    and the changelog all fire. NO direct file writes.

Usage:
  python3 core/scripts/aspirations-renumber.py --file agents/<a>/aspirations.jsonl \\
      --from asp-002 --to asp-004 [--sibling world/aspirations.jsonl] \\
      [--scan agents/<a>/session/working-memory.yaml ...] [--apply]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import _paths  # type: ignore  # noqa: E402,F401
from _fileops import locked_write_jsonl, read_jsonl_with_recovery  # noqa: E402

_ASP_ID = re.compile(r"^asp-(\d{3,})$")


def _patterns(src: str, dst: str):
    """(regex, replacement) pairs for the aspiration token and its goal-id family."""
    src_n, dst_n = src[len("asp-"):], dst[len("asp-"):]
    return [
        (re.compile(rf"(?<![A-Za-z0-9])asp-{re.escape(src_n)}(?![0-9])"), f"asp-{dst_n}"),
        (re.compile(rf"(?<![A-Za-z0-9])g-{re.escape(src_n)}-(\d+)"), rf"g-{dst_n}-\1"),
    ]


def _rewrite(value: Any, patterns, path: str, changed: dict[str, int], goal_ids: dict[str, str]):
    """Deep-rewrite every string under ``value``; count replacements per field path."""
    if isinstance(value, str):
        out = value
        for regex, repl in patterns:
            out, n = regex.subn(repl, out)
            if n:
                changed[path] = changed.get(path, 0) + n
        if out != value and path.endswith(".id") and _looks_like_goal_id(value):
            goal_ids[value] = out
        return out
    if isinstance(value, list):
        return [
            _rewrite(item, patterns, f"{path}[{i}]" if isinstance(item, str) else f"{path}[]",
                     changed, goal_ids)
            for i, item in enumerate(value)
        ]
    if isinstance(value, dict):
        return {
            key: _rewrite(item, patterns, f"{path}.{key}" if path else key, changed, goal_ids)
            for key, item in value.items()
        }
    return value


def _looks_like_goal_id(text: str) -> bool:
    return re.fullmatch(r"g-\d{3,}-\d{2,}", text) is not None


def _count_refs(value: Any, patterns) -> int:
    if isinstance(value, str):
        return sum(len(regex.findall(value)) for regex, _ in patterns)
    if isinstance(value, list):
        return sum(_count_refs(item, patterns) for item in value)
    if isinstance(value, dict):
        return sum(_count_refs(item, patterns) for item in value.values())
    return 0


def _scan_file(path: Path, patterns) -> list[int]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [i for i, line in enumerate(lines, 1) if any(r.search(line) for r, _ in patterns)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--file", required=True, help="aspirations JSONL store holding --from")
    ap.add_argument("--from", dest="src", required=True, help="aspiration id to renumber")
    ap.add_argument("--to", dest="dst", required=True, help="new aspiration id")
    ap.add_argument("--sibling", action="append", default=[],
                    help="other aspiration stores that must not already hold --to")
    ap.add_argument("--scan", action="append", default=[],
                    help="files to scan (report only) for references to the old ids")
    ap.add_argument("--drop-stray-goal-records", action="store_true",
                    help="drop top-level goal records of the family that duplicate an inner goal")
    ap.add_argument("--apply", action="store_true", help="write the renumbered store")
    args = ap.parse_args(argv)

    for label, value in (("--from", args.src), ("--to", args.dst)):
        if not _ASP_ID.match(value):
            print(json.dumps({"error": f"{label} must look like asp-NNN (got {value!r})"}))
            return 2
    if args.src == args.dst:
        print(json.dumps({"error": "--from and --to are the same id"}))
        return 2

    path = Path(args.file)
    items = read_jsonl_with_recovery(path)
    if not items:
        print(json.dumps({"error": f"{path}: no records"}))
        return 1
    matches = [i for i, rec in enumerate(items) if rec.get("id") == args.src]
    if len(matches) != 1:
        print(json.dumps({"error": f"{path}: {len(matches)} records carry id {args.src}"}))
        return 1
    if any(rec.get("id") == args.dst for rec in items):
        print(json.dumps({"error": f"{path}: {args.dst} already exists here"}))
        return 1
    for sibling in args.sibling:
        if any(rec.get("id") == args.dst for rec in read_jsonl_with_recovery(Path(sibling))):
            print(json.dumps({"error": f"{sibling}: {args.dst} already exists there"}))
            return 1

    patterns = _patterns(args.src, args.dst)
    changed: dict[str, int] = {}
    goal_ids: dict[str, str] = {}
    idx = matches[0]
    new_record = _rewrite(items[idx], patterns, "", changed, goal_ids)

    # Stray top-level goal records of this family: a duplicate of an inner goal is dropped
    # (its `.history` snapshot is the archive); a stray with no inner twin is kept + reported.
    inner_ids = {
        (g.get("id") if isinstance(g, dict) else g) for g in items[idx].get("goals", []) or []
    }
    family = re.compile(rf"^g-{re.escape(args.src[len('asp-'):])}-\d+$")
    dropped: list[str] = []
    kept_strays: list[str] = []
    keep_index: list[int] = []
    for i, rec in enumerate(items):
        stray = (
            i != idx
            and family.match(str(rec.get("id") or "")) is not None
            and rec.get("aspiration", args.src) == args.src
        )
        if stray and args.drop_stray_goal_records and rec.get("id") in inner_ids:
            dropped.append(str(rec.get("id")))
            continue
        if stray:
            kept_strays.append(str(rec.get("id")))
        keep_index.append(i)

    other_refs = {
        str(rec.get("id")): n
        for i, rec in enumerate(items)
        if i != idx and i in keep_index and (n := _count_refs(rec, patterns))
    }
    scan_hits = {s: hits for s in args.scan if (hits := _scan_file(Path(s), patterns))}

    report = {
        "file": str(path),
        "from": args.src,
        "to": args.dst,
        "applied": False,
        "changed_fields": changed,
        "goal_ids": goal_ids,
        "dropped_stray_goal_records": dropped,
        "kept_stray_goal_records": kept_strays,
        "other_record_refs": other_refs,
        "scan_hits": scan_hits,
    }
    if args.apply:
        items[idx] = new_record
        locked_write_jsonl(path, [items[i] for i in keep_index])
        report["applied"] = True
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
