#!/usr/bin/env python3
"""History operations: list, restore, diff, prune.

Manages the .history/ directory that stores timestamped snapshots of files
before they are overwritten. Each snapshot filename is:
    <timestamp>_<agent>.<ext>
"""

import argparse
import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Ensure stdout/stderr handle unicode on all platforms
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import WORLD_DIR, META_DIR
from _fileops import resolve_base_dir as _resolve_base_dir


def resolve_base_dir(file_path):
    """Determine which base_dir a file belongs to. Exits on failure (CLI tool)."""
    base = _resolve_base_dir(file_path)
    if base is None:
        print(f"Error: {Path(file_path).resolve()} is not under WORLD_DIR or META_DIR", file=sys.stderr)
        sys.exit(1)
    return base


def get_history_dir(file_path):
    """Get the .history/ directory for a given file."""
    file_path = Path(file_path).resolve()
    base = resolve_base_dir(file_path)
    rel = file_path.relative_to(base)
    return base / ".history" / str(rel)


def parse_snapshot_name(name):
    """Parse timestamp and agent from snapshot filename like '2026-03-26T14-30-00_alpha.md'."""
    stem = Path(name).stem
    # Handle double extensions (.jsonl.meta)
    if "_" in stem:
        parts = stem.rsplit("_", 1)
        timestamp_str = parts[0]
        agent = parts[1] if len(parts) > 1 else "unknown"
    else:
        timestamp_str = stem
        agent = "unknown"
    try:
        ts = datetime.strptime(timestamp_str, "%Y-%m-%dT%H-%M-%S")
    except ValueError:
        ts = None
    return ts, agent


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_list(args):
    """List all versions of a file."""
    history_dir = get_history_dir(args.file)
    if not history_dir.exists():
        print(f"No history for {args.file}")
        return

    snapshots = sorted(history_dir.iterdir(), reverse=True)
    # Filter out .meta sidecar files
    snapshots = [s for s in snapshots if not s.name.endswith(".meta")]

    if not snapshots:
        print(f"No history for {args.file}")
        return

    print(f"History for {args.file} ({len(snapshots)} versions):")
    print()
    for snap in snapshots:
        ts, agent = parse_snapshot_name(snap.name)
        size = snap.stat().st_size
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S") if ts else "unknown"

        # Check for summary sidecar
        meta_file = snap.with_suffix(snap.suffix + ".meta")
        summary = ""
        if meta_file.exists():
            summary = f" — {meta_file.read_text(encoding='utf-8').strip()}"

        print(f"  {snap.name}  [{ts_str}] by {agent}  ({size:,} bytes){summary}")


def cmd_restore(args):
    """Restore a specific version of a file."""
    history_dir = get_history_dir(args.file)
    if not history_dir.exists():
        print(f"Error: No history for {args.file}", file=sys.stderr)
        sys.exit(1)

    version_path = history_dir / args.version
    if not version_path.exists():
        print(f"Error: Version '{args.version}' not found", file=sys.stderr)
        print(f"Available versions:", file=sys.stderr)
        for snap in sorted(history_dir.iterdir()):
            if not snap.name.endswith(".meta"):
                print(f"  {snap.name}", file=sys.stderr)
        sys.exit(1)

    target = Path(args.file)
    from _fileops import acquire_lock, release_lock, save_history, append_changelog

    # Lock the target file during restore to prevent concurrent writes
    lock_path = target.with_suffix(".lock")
    acquire_lock(lock_path)
    try:
        # Save current version to history before restoring (if it exists)
        if target.exists():
            agent = os.environ.get("AYOAI_AGENT", "restore")
            base = resolve_base_dir(args.file)
            save_history(target, base, agent, summary=f"Before restore from {args.version}")

        shutil.copy2(str(version_path), str(target))

        # Log the restore in changelog
        base = resolve_base_dir(args.file)
        agent = os.environ.get("AYOAI_AGENT", "restore")
        append_changelog(base, agent, target, "restore",
                         summary=f"Restored from {args.version}")
    finally:
        release_lock(lock_path)

    print(f"Restored {args.file} from {args.version}")


def cmd_diff(args):
    """Show diff between current file and a historical version."""
    import difflib

    history_dir = get_history_dir(args.file)
    if not history_dir.exists():
        print(f"Error: No history for {args.file}", file=sys.stderr)
        sys.exit(1)

    version_path = history_dir / args.version
    if not version_path.exists():
        print(f"Error: Version '{args.version}' not found", file=sys.stderr)
        sys.exit(1)

    target = Path(args.file)
    if not target.exists():
        print(f"Error: Current file {args.file} does not exist", file=sys.stderr)
        sys.exit(1)

    old_lines = version_path.read_text(encoding="utf-8").splitlines(keepends=True)
    new_lines = target.read_text(encoding="utf-8").splitlines(keepends=True)

    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"{args.version} (historical)",
        tofile=f"{target.name} (current)",
    )
    output = "".join(diff)
    if output:
        print(output)
    else:
        print("No differences.")


def cmd_prune(args):
    """Prune old history snapshots.

    Retention policy:
      - Keep all versions from the last 7 days
      - Keep one daily snapshot for days 8-30
      - Keep one weekly snapshot for days 31+
    """
    base_dirs = []
    if WORLD_DIR and (WORLD_DIR / ".history").exists():
        base_dirs.append(WORLD_DIR / ".history")
    if META_DIR and (META_DIR / ".history").exists():
        base_dirs.append(META_DIR / ".history")

    if not base_dirs:
        print("No .history/ directories found.")
        return

    now = datetime.now()
    total_removed = 0
    total_kept = 0

    for history_root in base_dirs:
        # Walk all snapshot directories
        for snap_dir in sorted(history_root.rglob("*")):
            if not snap_dir.is_dir():
                continue
            # Only process leaf directories (containing snapshot files)
            snapshots = [f for f in snap_dir.iterdir() if f.is_file() and not f.name.endswith(".meta")]
            if not snapshots:
                continue

            # Group by date
            by_date = {}
            for snap in snapshots:
                ts, _ = parse_snapshot_name(snap.name)
                if ts is None:
                    continue
                date_key = ts.date()
                by_date.setdefault(date_key, []).append((ts, snap))

            # Separate entries into retention tiers
            recent = []    # age <= 7: keep all
            daily = {}     # age 8-30: keep latest per day
            weekly = {}    # age 31+: keep latest per ISO week

            for date_key, entries in by_date.items():
                age = (now.date() - date_key).days
                if age <= 7:
                    recent.extend(entries)
                elif age <= 30:
                    daily.setdefault(date_key, []).extend(entries)
                else:
                    week_key = date_key.isocalendar()[:2]  # (year, week)
                    weekly.setdefault(week_key, []).extend(entries)

            total_kept += len(recent)

            def _prune_group(groups):
                """Keep only the latest entry per group, remove the rest."""
                nonlocal total_kept, total_removed
                for _, group_entries in groups.items():
                    group_entries.sort(key=lambda x: x[0])
                    # Keep the latest, remove the rest
                    for ts, snap in group_entries[:-1]:
                        if args.dry_run:
                            print(f"  [dry-run] Would remove: {snap}")
                        else:
                            snap.unlink()
                            meta = snap.with_suffix(snap.suffix + ".meta")
                            if meta.exists():
                                meta.unlink()
                        total_removed += 1
                    total_kept += 1

            _prune_group(daily)
            _prune_group(weekly)

    action = "Would remove" if args.dry_run else "Removed"
    print(f"{action} {total_removed} snapshots, kept {total_kept}")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description="File history operations")
    sub = parser.add_subparsers(dest="command", required=True)

    # list
    list_p = sub.add_parser("list", help="List all versions of a file")
    list_p.add_argument("file", help="Path to the file")

    # restore
    restore_p = sub.add_parser("restore", help="Restore a historical version")
    restore_p.add_argument("file", help="Path to the file")
    restore_p.add_argument("version", help="Version filename (from list output)")

    # diff
    diff_p = sub.add_parser("diff", help="Diff current vs historical version")
    diff_p.add_argument("file", help="Path to the file")
    diff_p.add_argument("version", help="Version filename (from list output)")

    # prune
    prune_p = sub.add_parser("prune", help="Prune old snapshots")
    prune_p.add_argument("--dry-run", action="store_true", help="Show what would be removed")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "list": cmd_list,
        "restore": cmd_restore,
        "diff": cmd_diff,
        "prune": cmd_prune,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
