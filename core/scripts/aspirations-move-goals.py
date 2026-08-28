#!/usr/bin/env python3
"""Move goals from one aspiration record to another — across stores — renumbering them
into the target's goal-id family.

Why this exists (measured 2026-08-28, a served deployment): while the agent-local store and
the world store both held an ``asp-002``, every ``aspirations-add-goal.sh asp-002`` resolved
the WORLD record first, so twelve of the agent's domain goals were filed under the world's
unrelated "Operating Rhythm" aspiration — polluting its progress, its completion pressure in
the selector, and every "which aspiration is closest to done" answer. The allocator is fixed
(``_sibling_aspiration_stores``) and the agent record was renumbered
(``aspirations-renumber.py``); the goals already misfiled need MOVING, and a move by hand is
the store-bypass the framework forbids — so it is a script.

What it does:
  - pops each ``--goal`` from the source record (``--from-file`` / ``--from-asp``);
  - renumbers it into the target family — ``g-<to>-NN`` continuing after the target's
    highest NN — and rewrites every reference among the moved goals (``dependencies``,
    ``blocked_by``, free text) through the old→new map; ``aspiration`` becomes the target id
    where the field is present;
  - appends the goals to the target record (``--to-file`` / ``--to-asp``), in the order given;
  - recomputes ``progress`` on both records with the daemon's own formula
    (``aspirations.recompute_progress``);
  - writes the TARGET first, then the SOURCE, each as a locked read-modify-write
    (``_fileops.locked_modify_jsonl``): a failure between the two leaves a visible duplicate,
    never a loss, and a concurrent daemon write is never clobbered. The source removal
    re-verifies each goal is byte-identical to the copy that was moved; a goal that changed
    meanwhile is left in place and reported (``source_changed``) for a reader to resolve.

Safety:
  - Dry-run by default: prints the JSON report (the old→new id map, both records' progress
    before/after) and touches nothing. ``--apply`` writes.
  - ``--scan`` files are searched (report only) for references to the OLD ids.

Usage:
  python3 core/scripts/aspirations-move-goals.py \\
      --from-file "$WORLD_PATH/aspirations.jsonl" --from-asp asp-002 \\
      --to-file agents/<a>/aspirations.jsonl --to-asp asp-004 \\
      --goal g-002-03 --goal g-002-04 [--scan <file> ...] [--apply]
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
from _fileops import locked_modify_jsonl, read_jsonl_with_recovery  # noqa: E402
from aspirations import recompute_progress  # noqa: E402

_GOAL_ID = re.compile(r"^g-(\d{3,})-(\d{2,})$")


def _asp_num(asp_id: str) -> str:
    return asp_id[len("asp-"):]


def _goal_id(goal: Any) -> str:
    return str(goal.get("id") or "") if isinstance(goal, dict) else str(goal)


def _find(items: list[dict], asp_id: str) -> tuple[int, dict] | None:
    for i, rec in enumerate(items):
        if rec.get("id") == asp_id:
            return i, rec
    return None


def _next_seq(target: dict, to_num: str) -> int:
    top = 0
    for g in target.get("goals", []) or []:
        m = _GOAL_ID.match(_goal_id(g))
        if m and m.group(1) == to_num:
            top = max(top, int(m.group(2)))
    return top + 1


def _rewrite(value: Any, mapping: dict[str, str], patterns) -> Any:
    if isinstance(value, str):
        out = value
        for regex, new in patterns:
            out = regex.sub(new, out)
        return out
    if isinstance(value, list):
        return [_rewrite(v, mapping, patterns) for v in value]
    if isinstance(value, dict):
        return {k: _rewrite(v, mapping, patterns) for k, v in value.items()}
    return value


def _scan_file(path: Path, old_ids: list[str]) -> list[int]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [i for i, line in enumerate(lines, 1) if any(o in line for o in old_ids)]


def _frozen(goal: Any) -> str:
    return json.dumps(goal, sort_keys=True, ensure_ascii=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from-file", required=True)
    ap.add_argument("--from-asp", required=True)
    ap.add_argument("--to-file", required=True)
    ap.add_argument("--to-asp", required=True)
    ap.add_argument("--goal", action="append", default=[], help="goal id to move (repeatable)")
    ap.add_argument("--scan", action="append", default=[],
                    help="files to scan (report only) for references to the old ids")
    ap.add_argument("--apply", action="store_true", help="write both stores")
    args = ap.parse_args(argv)

    if not args.goal:
        print(json.dumps({"error": "no --goal given"}))
        return 2
    if not re.match(r"^asp-\d{3,}$", args.to_asp) or not re.match(r"^asp-\d{3,}$", args.from_asp):
        print(json.dumps({"error": "--from-asp/--to-asp must look like asp-NNN"}))
        return 2
    src_path, dst_path = Path(args.from_file), Path(args.to_file)
    same_file = src_path.resolve() == dst_path.resolve()

    src_items = read_jsonl_with_recovery(src_path)
    dst_items = src_items if same_file else read_jsonl_with_recovery(dst_path)
    src = _find(src_items, args.from_asp)
    dst = _find(dst_items, args.to_asp)
    if src is None:
        print(json.dumps({"error": f"{src_path}: no record {args.from_asp}"}))
        return 1
    if dst is None:
        print(json.dumps({"error": f"{dst_path}: no record {args.to_asp}"}))
        return 1
    if same_file and args.from_asp == args.to_asp:
        print(json.dumps({"error": "source and target are the same record"}))
        return 1
    _, src_rec = src
    _, dst_rec = dst

    by_id = {_goal_id(g): g for g in src_rec.get("goals", []) or []}
    missing = [g for g in args.goal if g not in by_id]
    if missing:
        print(json.dumps({"error": f"{args.from_asp} does not hold: {', '.join(missing)}"}))
        return 1

    to_num = _asp_num(args.to_asp)
    seq = _next_seq(dst_rec, to_num)
    mapping: dict[str, str] = {}
    for old in args.goal:
        mapping[old] = f"g-{to_num}-{seq:02d}"
        seq += 1
    patterns = [
        (re.compile(rf"(?<![A-Za-z0-9-]){re.escape(old)}(?![0-9])"), new)
        for old, new in mapping.items()
    ]
    frozen = {old: _frozen(by_id[old]) for old in args.goal}
    moved: list[Any] = []
    for old in args.goal:
        goal = _rewrite(by_id[old], mapping, patterns)
        if isinstance(goal, dict):
            if "aspiration" in goal:
                goal["aspiration"] = args.to_asp
            if "asp_id" in goal:
                goal["asp_id"] = args.to_asp
        moved.append(goal)

    def _progress(rec: dict) -> dict:
        rec = json.loads(json.dumps(rec))
        recompute_progress(rec)
        return rec.get("progress") or {}

    src_after = json.loads(json.dumps(src_rec))
    src_after["goals"] = [g for g in src_after.get("goals", []) if _goal_id(g) not in mapping]
    dst_after = json.loads(json.dumps(dst_rec))
    dst_after["goals"] = list(dst_after.get("goals", []) or []) + moved

    report: dict[str, Any] = {
        "from": {"file": str(src_path), "aspiration": args.from_asp,
                 "progress_before": src_rec.get("progress"), "progress_after": _progress(src_after)},
        "to": {"file": str(dst_path), "aspiration": args.to_asp,
               "progress_before": dst_rec.get("progress"), "progress_after": _progress(dst_after)},
        "id_map": mapping,
        "applied": False,
        "source_changed": [],
        "scan_hits": {s: hits for s in args.scan if (hits := _scan_file(Path(s), args.goal))},
    }
    if not args.apply:
        print(json.dumps(report, indent=2))
        return 0

    # TARGET first (a failure between the two writes leaves a duplicate, never a loss).
    def _append(items: list[dict]) -> list[dict]:
        found = _find(items, args.to_asp)
        if found is None:
            raise RuntimeError(f"{dst_path}: {args.to_asp} vanished before the write")
        _, rec = found
        held = {_goal_id(g) for g in rec.get("goals", []) or []}
        clash = [_goal_id(g) for g in moved if _goal_id(g) in held]
        if clash:
            raise RuntimeError(f"{args.to_asp} already holds {', '.join(clash)}")
        rec["goals"] = list(rec.get("goals", []) or []) + moved
        recompute_progress(rec)
        return items

    def _remove(items: list[dict]) -> list[dict]:
        found = _find(items, args.from_asp)
        if found is None:
            raise RuntimeError(f"{src_path}: {args.from_asp} vanished before the write")
        _, rec = found
        keep = []
        for g in rec.get("goals", []) or []:
            gid = _goal_id(g)
            if gid in mapping:
                if _frozen(g) != frozen[gid]:
                    report["source_changed"].append(gid)
                    keep.append(g)
                continue
            keep.append(g)
        rec["goals"] = keep
        recompute_progress(rec)
        return items

    if same_file:
        def _both(items: list[dict]) -> list[dict]:
            return _remove(_append(items))
        locked_modify_jsonl(src_path, _both)
    else:
        locked_modify_jsonl(dst_path, _append)
        locked_modify_jsonl(src_path, _remove)
    report["applied"] = True
    print(json.dumps(report, indent=2))
    return 0 if not report["source_changed"] else 3


if __name__ == "__main__":
    sys.exit(main())
