#!/usr/bin/env python3
"""tree-body-presence-audit — authoritative-store body-presence auditor for the knowledge tree.

For every ``_tree.yaml`` node carrying a ``file:`` field, compare the ``.md``
body's presence on the LOCAL mirror (``os.path.exists``) against the
AUTHORITATIVE remote store (``backend.stat`` -> ``None`` means the remote
returns not-found). Four buckets:

  synced      local T + remote T   OK — body present both places
  local_only  local T + remote F   NEVER-PUSHED AT-RISK — body only on THIS
                                    box's mirror, never reached the authoritative
                                    store; permanent-loss risk if this box dies.
                                    The holding box CAN re-push (body in hand).
  cache_miss  local F + remote T   harmless read-through cache miss (remote-synced
                                    store; body simply not pulled to this box yet)
  desync      local F + remote F   INDEX-BODY DESYNC — the index entry synced but
                                    the body reached NEITHER this box's mirror NOR
                                    the authoritative store; a registered node with
                                    no retrievable body.

Why this exists: ``tree-read.sh --validate`` checks LOCAL ``os.path.exists`` only,
so it is BLIND to index->remote-absent (both the desync AND the never-pushed-
at-risk classes). This audit is the authoritative-store-aware complement — the
``backend.stat is None`` signal is exactly what a local existence check
structurally cannot produce.

Backend scope: performs remote HEADs ONLY on a remote-synced backend. On the
local backend it is a clean no-op (the local mirror IS authoritative — there is
no separate store to compare against), so the tool is safe to invoke anywhere.

Single-box caveat (rb-4089): local_only=0 on THIS box does NOT clear the fleet.
The never-pushed-at-risk class is visible ONLY from the box holding the body, so
full at-risk coverage requires EACH box to run this audit. The desync class
(absent everywhere) is detectable from any box.

Read-only: performs remote HEADs and local existence checks; never mutates the
mirror or the store.

Exit codes: 0 clean run (no desync, no at-risk) or local no-op; 1 error;
3 findings present (desync or at-risk > 0) — only when ``--exit-on-findings``.
"""
import argparse
import json
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import WORLD_DIR, assert_world_dir  # noqa: E402
from storage_backend import get_backend  # noqa: E402

# core/scripts/<this>.py -> parents[2] == PROJECT_ROOT (matches _paths' own
# derivation). Used only for the rare non-"world/"-prefixed file field.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _resolve(file_field, world_dir, project_root):
    p = str(file_field).replace("\\", "/")
    if p.startswith("world/"):
        return Path(world_dir) / p[len("world/"):]
    return Path(project_root) / p


def _is_local_backend(b):
    """True when the backend is the plain local mirror (no separate remote store)."""
    btype = type(b).__name__
    bname = getattr(b, "name", "") or ""
    return btype == "LocalBackend" or (isinstance(bname, str) and bname.lower().startswith("local"))


def scan(world_dir, project_root, backend=None, quiet=False):
    """Audit every file-bearing tree node. Returns a summary dict.

    On a local backend: returns {local_noop: True, ...} without any remote HEAD.
    On a remote-synced backend: returns the 4-way classification with full
    records for the two actionable buckets (local_only, desync).
    """
    b = backend if backend is not None else get_backend()
    bname = getattr(b, "name", "") or type(b).__name__
    if _is_local_backend(b):
        return {
            "backend": bname,
            "local_noop": True,
            "note": ("local backend — the local mirror IS authoritative; there is "
                     "no separate remote store to audit against"),
        }

    tree_path = Path(world_dir) / "knowledge" / "tree" / "_tree.yaml"
    with open(tree_path) as f:
        tree = yaml.safe_load(f) or {}
    nodes = tree.get("nodes", {}) or {}

    buckets = {"synced": [], "local_only": [], "cache_miss": [], "desync": [], "probe_error": []}
    no_file = 0
    keys = sorted(nodes.keys())
    for i, key in enumerate(keys):
        node = nodes[key] or {}
        ff = node.get("file")
        if not ff:
            no_file += 1
            continue
        abs_local = _resolve(ff, world_dir, project_root)
        local_present = abs_local.exists()
        try:
            st = b.stat(abs_local)
            remote_present = st is not None
            err = None
        except Exception as ex:  # noqa: BLE001 - probe is best-effort
            remote_present = None
            err = f"{ex.__class__.__name__}: {ex}"
        rec = {"key": key, "file": ff,
               "last_updated": node.get("last_updated"),
               "parent": node.get("parent")}
        if remote_present is None:
            buckets["probe_error"].append({**rec, "err": err})
        elif local_present and remote_present:
            buckets["synced"].append(rec)
        elif local_present and not remote_present:
            buckets["local_only"].append(rec)
        elif (not local_present) and remote_present:
            buckets["cache_miss"].append(rec)
        else:
            buckets["desync"].append(rec)
        if not quiet and (i + 1) % 200 == 0:
            sys.stderr.write(f"...{i + 1}/{len(keys)} scanned\n")
            sys.stderr.flush()

    return {
        "backend": bname,
        "local_noop": False,
        "total_with_file": sum(len(v) for v in buckets.values()),
        "no_file_nodes": no_file,
        "counts": {k: len(v) for k, v in buckets.items()},
        "local_only": buckets["local_only"],   # NEVER-PUSHED AT-RISK (full records)
        "desync": buckets["desync"],            # INDEX-BODY DESYNC (full records)
        "probe_error": buckets["probe_error"],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Authoritative-store body-presence audit for the knowledge tree")
    ap.add_argument("--exit-on-findings", action="store_true",
                    help="exit 3 when desync+at-risk > 0 (for cadence alerting)")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress per-N progress to stderr")
    args = ap.parse_args(argv)
    try:
        assert_world_dir("tree-body-presence-audit")
        result = scan(str(WORLD_DIR), str(PROJECT_ROOT), quiet=args.quiet)
    except Exception as ex:  # noqa: BLE001
        print(json.dumps({"error": f"{ex.__class__.__name__}: {ex}"}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    if result.get("local_noop"):
        return 0
    findings = len(result.get("desync", [])) + len(result.get("local_only", []))
    if args.exit_on_findings and findings > 0:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
