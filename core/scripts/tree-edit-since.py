#!/usr/bin/env python3
"""tree-edit-since.py — detect whether any knowledge-tree .md or _tree.yaml
file was modified since a given ISO timestamp.

Used by iteration-close.sh do_state_update() to auto-set TREE_UPDATED=true
when the LLM forgot to pass --tree-updated explicitly. Filed by g-273-20
after observing alpha session-60 iter-17/19/20 fire force_tree_encoding=true
three times despite g-273-18 actively encoding tree (because --tree-updated
was LLM-residue and got dropped every iteration).

Usage:
    py -3 tree-edit-since.py <ISO-8601-timestamp>

Exit codes:
    0 — at least one file under WORLD_DIR/knowledge/tree was modified after
        the given timestamp (auto-detect should set TREE_UPDATED=true).
    1 — no files modified since (or could not parse timestamp / could not
        access tree dir). Caller treats this as "no tree edit detected" and
        falls through to LLM-passed flag (default false).

Fail-open: any error returns exit 1 with stderr message — never blocks
state-update.
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import WORLD_DIR  # type: ignore


def main():
    if len(sys.argv) != 2:
        print("usage: tree-edit-since.py <ISO-8601-timestamp>", file=sys.stderr)
        sys.exit(1)

    iso = sys.argv[1].strip()
    try:
        # Accepts both "2026-05-04T15:35:00" and "2026-05-04T15:35:00.123"
        cutoff = datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError) as e:
        print(f"tree-edit-since: bad timestamp {iso!r}: {e}", file=sys.stderr)
        sys.exit(1)

    tree_dir = Path(WORLD_DIR) / "knowledge" / "tree"
    if not tree_dir.is_dir():
        print(f"tree-edit-since: no tree dir at {tree_dir}", file=sys.stderr)
        sys.exit(1)

    # Check _tree.yaml first (small, fast)
    tree_yaml = tree_dir / "_tree.yaml"
    if tree_yaml.exists():
        try:
            if tree_yaml.stat().st_mtime > cutoff:
                print(f"tree-edit-since: detected {tree_yaml.name} modified after {iso}")
                sys.exit(0)
        except OSError:
            pass

    # Scan .md files. Short-circuit on first match — no need to enumerate all
    # 900+ files when one hit is sufficient signal.
    for md in tree_dir.rglob("*.md"):
        try:
            if md.stat().st_mtime > cutoff:
                rel = md.relative_to(tree_dir)
                print(f"tree-edit-since: detected {rel} modified after {iso}")
                sys.exit(0)
        except OSError:
            continue

    sys.exit(1)


if __name__ == "__main__":
    main()
