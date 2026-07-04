#!/usr/bin/env python3
"""tree-adjudication-scan.py -- emit the knowledge-tree nodes that carry an
earned-low calibration-adjudication marker.

THE MARKER: a node whose .md body contains a level-2 heading beginning
`## Confidence Rationale` has been deliberately adjudicated as "earned-low" --
its low REGISTRY confidence is CORRECT (subject resolvability / distilled
pointer-kernel / preliminary underpowered experiment / aging point-in-time
snapshot / registry-vs-content split), NOT a content gap. The section body
explains why. Convention owners: rb-1756, g-115-1422 (zeta), g-115-1710 (bravo).

WHY THIS EXISTS (g-115-1711): g-115-400's recurring tree-confidence calibration
sweep populates an "under-encoded" bucket (retrieval_count>=20 AND confidence<0.5)
and files Idea/Investigate goals to encode those nodes. Without a machine-readable
adjudication signal, the sweep RE-FLAGGED already-adjudicated earned-low nodes
every ~112h cycle (g-115-1422 -> g-115-1710 -> ...) because the adjudication lived
only in node BODIES the sweep could not read. This script IS that machine-readable
signal: the sweep calls it and EXCLUDES the returned keys from the under-encoded
bucket BEFORE filing goals.

No false-exclusion risk: the exclusion only applies to nodes already inside the
low-confidence under-encoded bucket (conf<0.5); a low-confidence node carrying a
`## Confidence Rationale` section IS an earned-low adjudication by definition.

Output (JSON, default): {"adjudicated": [{"key":..., "file":...}], "count": N}
  --keys : print one node key per line (shell-friendly for set-membership exclusion)
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import WORLD_DIR, assert_world_dir  # noqa: E402

# The level-2 heading is the convention marker. \s+ tolerates 1+ spaces after ##.
MARKER_RE = re.compile(r'^##\s+Confidence Rationale', re.MULTILINE)
# Registry key from front matter when present; filename stem is the fallback
# (tree node files are named by their key, so stem == registry key in practice).
KEY_RE = re.compile(r'''^key:\s*["']?([A-Za-z0-9_./-]+)["']?\s*$''', re.MULTILINE)


def scan(tree_root=None):
    """Return [{key, file}] for every tree node carrying the marker.

    tree_root: optional override (test seam). When None, resolves to
    WORLD_DIR/knowledge/tree, guarded by assert_world_dir.
    """
    if tree_root is None:
        assert_world_dir("tree-adjudication-scan")
        tree_root = WORLD_DIR / "knowledge" / "tree"
    else:
        tree_root = Path(tree_root)
    rows = []
    if not tree_root.exists():
        return rows
    for md in sorted(tree_root.rglob("*.md")):
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not MARKER_RE.search(text):
            continue
        m = KEY_RE.search(text)
        key = m.group(1) if m else md.stem
        rows.append({"key": key, "file": str(md)})
    return rows


def main(argv):
    keys_only = "--keys" in argv
    rows = scan()
    if keys_only:
        for r in rows:
            print(r["key"])
    else:
        print(json.dumps({"adjudicated": rows, "count": len(rows)}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
