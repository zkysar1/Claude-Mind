#!/usr/bin/env python3
"""Backfill origin_signal on pre-existing goals — one-shot migration.

Stamps origin_signal on every goal whose current value is missing or fails
the origin-signal-gate validator. Rules (applied in order on the goal title):

  1. "Unblock: ..."     → "unblock:{asp.id}-{goal.id}"
  2. "Investigate: ..." → "investigate:{asp.id}-{goal.id}"
  3. "Idea: ..."        → "idea:{asp.id}-{goal.id}"
  4. "Maintain: ..."    → "maintain:{asp.id}-{goal.id}"
  5. Anything else      → "parent_aspiration:{asp.id}"

Rule 5 is honest provenance, not fabricated: every goal literally belongs to
a parent aspiration, and the aspiration IS the goal's source context. Rules
1-4 enrich that with primitive type when the title declares it.

Files processed:
  - world/aspirations.jsonl
  - <agent>/aspirations.jsonl for each agent directory at project root

Usage:
  py -3 core/scripts/backfill-origin-signal.py             # dry-run
  py -3 core/scripts/backfill-origin-signal.py --apply     # write changes
  py -3 core/scripts/backfill-origin-signal.py --verify    # invariant check only

--apply runs --verify afterward and exits 1 if any gap remains. The migration
is successful iff post-apply verify is clean — i.e., every goal on disk now
carries a valid origin_signal, making the gate's invariant universal.
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from _paths import WORLD_DIR, PROJECT_ROOT, agents_root as _agents_root  # noqa: E402
from _fileops import acquire_lock, release_lock  # noqa: E402
from _prefix_registry import SIGNAL_KIND_PRIMITIVES  # noqa: E402

# Import the gate's canonical validator so backfill + gate share one source
# of truth for "valid". Gate filename has a hyphen so direct import doesn't
# work — importlib handles it. If the gate moves or is renamed, this is the
# only place in the backfill that needs to track it.
_gate_spec = importlib.util.spec_from_file_location(
    "origin_signal_gate",
    SCRIPTS_DIR / "origin-signal-gate.py",
)
_gate_mod = importlib.util.module_from_spec(_gate_spec)
_gate_spec.loader.exec_module(_gate_mod)
is_valid = _gate_mod.is_valid


def infer_signal(goal, asp_id):
    title = (goal.get("title") or "").strip()
    gid = goal.get("id") or ""
    low = title.lower()
    for prefix, kind in SIGNAL_KIND_PRIMITIVES:
        if low.startswith(prefix):
            suffix = f"{asp_id}-{gid}" if gid else asp_id
            return f"{kind}:{suffix}"
    return f"parent_aspiration:{asp_id}"


def _read_jsonl(path):
    items = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(
                    f"[backfill-origin-signal._read_jsonl] WARN: skipped corrupt line {line_no} in {path}: {e}",
                    file=sys.stderr,
                )
                continue
    return items


def _transform_items(items):
    """Pure transform — returns (entries, scanned, ok, changed). No I/O."""
    entries = []
    scanned = 0
    ok = 0
    changed = False
    for asp in items:
        aid = asp.get("id") or ""
        for g in asp.get("goals", []) or []:
            scanned += 1
            sig = g.get("origin_signal")
            if is_valid(sig):
                ok += 1
                continue
            new_sig = infer_signal(g, aid)
            entries.append({
                "aspiration": aid,
                "goal": g.get("id"),
                "title": (g.get("title") or "")[:60],
                "old": sig,
                "new": new_sig,
            })
            g["origin_signal"] = new_sig
            changed = True
    return entries, scanned, ok, changed


def process_file(path, apply_changes):
    if not path.exists():
        return {"scanned": 0, "ok": 0, "backfilled": 0, "entries": []}
    # CRITICAL: read + transform + write MUST be inside a single lock scope
    # when applying. aspirations.py acquires this same .lock file for
    # cmd_add / cmd_update. Reading outside the lock and writing inside
    # would open a TOCTOU window where a concurrent agent write is silently
    # dropped by our overwrite. Dry-run skips the lock entirely (read-only).
    if apply_changes:
        lock_path = path.with_suffix(".lock")
        try:
            acquire_lock(lock_path)
            items = _read_jsonl(path)
            entries, scanned, ok, changed = _transform_items(items)
            if changed:
                tmp = path.with_suffix(path.suffix + ".tmp")
                with tmp.open("w", encoding="utf-8") as f:
                    for asp in items:
                        f.write(json.dumps(asp, ensure_ascii=False) + "\n")
                tmp.replace(path)
        finally:
            release_lock(lock_path)
    else:
        items = _read_jsonl(path)
        entries, scanned, ok, _ = _transform_items(items)
    return {"scanned": scanned, "ok": ok, "backfilled": len(entries),
            "entries": entries}


def find_agent_jsonls():
    """Every <agent>/aspirations.jsonl at project root.

    Agent directories are identified structurally: they contain
    local-paths.conf (the per-agent config file that /start writes). This
    avoids hardcoding a framework blocklist and stays correct as new
    agents are created.
    """
    results = []
    for entry in _agents_root().iterdir():
        if not entry.is_dir():
            continue
        if not (entry / "local-paths.conf").exists():
            continue
        asp_file = entry / "aspirations.jsonl"
        if asp_file.exists():
            results.append(asp_file)
    return results


def verify(files):
    gaps = []
    for path in files:
        if not path.exists():
            continue
        for asp in _read_jsonl(path):
            for g in asp.get("goals", []) or []:
                if not is_valid(g.get("origin_signal")):
                    gaps.append({
                        "file": str(path),
                        "aspiration": asp.get("id"),
                        "goal": g.get("id"),
                        "signal": g.get("origin_signal"),
                    })
    return gaps


def _collect_files():
    files = [WORLD_DIR / "aspirations.jsonl"] + find_agent_jsonls()
    return [f for f in files if f.exists()]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="Write changes. Default is dry-run.")
    ap.add_argument("--verify", action="store_true",
                    help="Invariant check only. Exits 1 if any goal "
                         "missing/invalid origin_signal.")
    args = ap.parse_args()

    files = _collect_files()
    if not files:
        print("No aspirations.jsonl files found.", file=sys.stderr)
        sys.exit(0)

    if args.verify:
        gaps = verify(files)
        if gaps:
            print(f"VERIFY FAILED: {len(gaps)} goal(s) without valid origin_signal:")
            for gap in gaps[:20]:
                print(f"  {gap['file']}: {gap['aspiration']} / "
                      f"{gap['goal']} / {gap['signal']!r}")
            if len(gaps) > 20:
                print(f"  ... and {len(gaps) - 20} more")
            sys.exit(1)
        print(f"VERIFY OK: {len(files)} file(s), every goal has valid origin_signal.")
        sys.exit(0)

    total = {"scanned": 0, "ok": 0, "backfilled": 0}
    all_entries = []
    for path in files:
        res = process_file(path, args.apply)
        total["scanned"] += res["scanned"]
        total["ok"] += res["ok"]
        total["backfilled"] += res["backfilled"]
        all_entries.extend((str(path), e) for e in res["entries"])
        tag = "APPLIED" if args.apply else "DRY-RUN"
        print(f"[{tag}] {path}: {res['scanned']} scanned, "
              f"{res['ok']} already-signed, {res['backfilled']} backfilled")

    verb = "backfilled" if args.apply else "would be backfilled"
    print(f"\nTotal: {total['scanned']} scanned, {total['ok']} already-signed, "
          f"{total['backfilled']} {verb}")

    if all_entries and not args.apply:
        print("\nFirst 10 planned changes:")
        for fpath, e in all_entries[:10]:
            print(f"  {fpath}")
            print(f"    {e['aspiration']} / {e['goal']}: {e['title']}")
            print(f"      {e['old']!r} -> {e['new']!r}")

    if args.apply:
        gaps = verify(files)
        if gaps:
            print(f"\nPOST-APPLY VERIFY FAILED: {len(gaps)} goal(s) still invalid.",
                  file=sys.stderr)
            for gap in gaps[:10]:
                print(f"  {gap['file']}: {gap['aspiration']} / "
                      f"{gap['goal']} / {gap['signal']!r}", file=sys.stderr)
            sys.exit(1)
        print("\nPOST-APPLY VERIFY: all goals carry valid origin_signal.")


if __name__ == "__main__":
    main()
