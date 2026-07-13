#!/usr/bin/env python3
"""Tree _tree.yaml `last_updated` index / front-matter drift check + backfill.

g-115-1683 (promotes the g-115-1612 read-only audit prototype — a zeta
session temp scratch script, gitignored — to a framework script). The node .md
front-matter `last_updated` is the single source of truth (g-001-67). The
_tree.yaml per-node index `last_updated` can drift from it in two directions:

  - index-AHEAD (index newer than node fm): over-reports freshness -- the
    DANGEROUS direction (a staleness-reconciliation sweep trusting the index
    skips a node whose content is actually stale). Historical root cause was the
    cmd_set / batch-set metadata auto-bump, REMOVED in g-115-1683 (it stamped
    node last_updated=today on any non-date field, bypassing the .md fm).
  - index-STALE (node fm newer than index): under-reports (node looks stale when
    fresh). A one-time 2026-05-10 backfill-tree-node-fields.py artifact
    (anchored 2026-01-01, never read the node fm).

Modes:
  --audit (default): READ-ONLY. Classify synced / index_ahead / index_stale and
    print a JSON summary. `--exit-on-ahead` makes it exit 2 when index_ahead > 0
    so an asp-115 Layer-D recurring goal can detect ongoing drift and file an
    Investigate. `--full` additionally emits the COMPLETE index_ahead[]/
    index_stale[] lists (every {key, idx, fm}) instead of only the capped
    samples, so a hypothesis resolution_method can evaluate every entry
    against the full set, not a sample (g-115-1820).
  --apply: BACKFILL. For every desynced node set the index last_updated = that
    node's .md fm last_updated, atomically via locked_modify_yaml(TREE_PATH)
    (guard-366 / g-115-417 -- a single locked read-modify-write, NOT a
    read_tree/write_tree split). The .md fm reads happen INSIDE the modifier so
    the whole operation is consistent under the lock.
"""
import argparse
import json
import re
import sys
from pathlib import Path

from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

from _paths import WORLD_DIR
from _fileops import locked_modify_yaml

TREE_PATH = str(WORLD_DIR / "knowledge" / "tree" / "_tree.yaml")

# Front matter is the leading `---\n ... \n---` block of a node .md file.
FM_RE = re.compile(r"\A---\s*\n(.*?)\n---", re.DOTALL)


def _fm_last_updated(md_path):
    """Return (YYYY-MM-DD str or None, error-or-None) from a node .md fm."""
    try:
        raw = md_path.read_bytes().decode("utf-8")
    except Exception:
        return None, "read_error"
    m = FM_RE.match(raw.replace("\r\n", "\n"))
    if not m:
        return None, "no_front_matter"
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception:
        return None, "fm_parse_error"
    lu = fm.get("last_updated")
    if lu is None:
        return None, "no_last_updated_field"
    return str(lu)[:10], None


def _md_path_for(node_file):
    """Resolve a node's virtual `file` (world/knowledge/...) to an abs path."""
    rel = node_file[len("world/"):] if node_file.startswith("world/") else node_file
    return WORLD_DIR / rel


def _classify(nodes):
    """Pure classification over an in-memory nodes dict.

    ISO YYYY-MM-DD strings compare lexicographically == chronologically, so
    `idx > fm` is a valid date comparison after the [:10] normalization.
    """
    index_ahead, index_stale, synced, nullfile = [], [], 0, 0
    errs = {}
    for key, node in nodes.items():
        if not isinstance(node, dict):
            continue
        vf = node.get("file")
        if not vf:
            nullfile += 1
            continue
        idx = node.get("last_updated")
        idx = str(idx)[:10] if idx is not None else None
        fm, err = _fm_last_updated(_md_path_for(vf))
        if err:
            errs[err] = errs.get(err, 0) + 1
            continue
        if idx is None:
            errs["index_no_last_updated"] = errs.get("index_no_last_updated", 0) + 1
            continue
        if idx == fm:
            synced += 1
        elif idx > fm:
            index_ahead.append({"key": key, "idx": idx, "fm": fm})
        else:
            index_stale.append({"key": key, "idx": idx, "fm": fm})
    return {
        "total_nodes": len(nodes),
        "null_file": nullfile,
        "synced": synced,
        "index_ahead": index_ahead,
        "index_stale": index_stale,
        "errors": errs,
    }


def cmd_audit(exit_on_ahead, full=False):
    data = yaml.safe_load(Path(TREE_PATH).read_text(encoding="utf-8")) or {}
    nodes = data.get("nodes") or {}
    r = _classify(nodes)
    ahead_n, stale_n = len(r["index_ahead"]), len(r["index_stale"])
    summary = {
        "mode": "audit",
        "tree_path": TREE_PATH,
        "total_nodes": r["total_nodes"],
        "null_file": r["null_file"],
        "synced": r["synced"],
        "desynced": ahead_n + stale_n,
        "index_ahead": ahead_n,
        "index_stale": stale_n,
        "errors": r["errors"],
        "sample_index_ahead": sorted(r["index_ahead"], key=lambda x: x["fm"])[:8],
        "sample_index_stale": sorted(r["index_stale"], key=lambda x: x["idx"])[:8],
    }
    if full:
        # 0: emit the COMPLETE lists (not just the capped [:8] samples)
        # so a hypothesis resolution_method can evaluate every entry against the
        # full set. The full sets are already computed by _classify; this only
        # changes what is serialized at output.
        summary["index_ahead_full"] = sorted(r["index_ahead"], key=lambda x: x["fm"])
        summary["index_stale_full"] = sorted(r["index_stale"], key=lambda x: x["idx"])
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    # index_ahead is the dangerous direction (over-reports freshness); a
    # recurring Layer-D goal greps this exit code to file an Investigate.
    if exit_on_ahead and ahead_n > 0:
        return 2
    return 0


def cmd_apply():
    captured = {"backfilled": 0, "details": [], "skipped_errors": {}}

    def _backfill(data):
        if not isinstance(data, dict) or "nodes" not in data:
            print("Invalid tree file: missing 'nodes' key", file=sys.stderr)
            sys.exit(1)
        nodes = data["nodes"]
        for key, node in nodes.items():
            if not isinstance(node, dict):
                continue
            vf = node.get("file")
            if not vf:
                continue
            idx = node.get("last_updated")
            idx = str(idx)[:10] if idx is not None else None
            fm, err = _fm_last_updated(_md_path_for(vf))
            if err or fm is None:
                if err:
                    captured["skipped_errors"][err] = \
                        captured["skipped_errors"].get(err, 0) + 1
                continue
            if idx != fm:
                # node .md fm is the single source of truth (): set the
                # index to it for BOTH index_ahead (spurious-bump) and
                # index_stale (2026-01-01 placeholder) nodes.
                node["last_updated"] = fm
                captured["backfilled"] += 1
                if len(captured["details"]) < 12:
                    captured["details"].append({"key": key, "from": idx, "to": fm})
        # Intentionally do NOT touch data["last_updated"] (the tree-file write
        # timestamp): this backfill corrects per-node fields only (scope).
        return data

    # guard-366 / : single locked read-modify-write, no split.
    locked_modify_yaml(TREE_PATH, _backfill)
    print(json.dumps({
        "mode": "apply",
        "tree_path": TREE_PATH,
        "backfilled": captured["backfilled"],
        "skipped_errors": captured["skipped_errors"],
        "sample": captured["details"],
    }, indent=2, ensure_ascii=False))
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Tree _tree.yaml last_updated index/front-matter drift "
                    "check (read-only) + backfill (--apply).")
    ap.add_argument("--apply", action="store_true",
                    help="Backfill index last_updated from node .md fm "
                         "(atomic, locked). Default is read-only audit.")
    ap.add_argument("--exit-on-ahead", action="store_true",
                    help="(audit mode) exit 2 when index_ahead > 0, for "
                         "recurring-goal drift detection.")
    ap.add_argument("--full", action="store_true",
                    help="(audit mode) emit the COMPLETE index_ahead[]/"
                         "index_stale[] lists (every {key, idx, fm}), not just "
                         "the capped samples -- to evaluate a hypothesis against "
                         "the full set (g-115-1820).")
    args = ap.parse_args()
    if args.apply:
        sys.exit(cmd_apply())
    sys.exit(cmd_audit(args.exit_on_ahead, args.full))


if __name__ == "__main__":
    main()
