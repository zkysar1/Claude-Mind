#!/usr/bin/env python3
"""tree-inbound-ref-fix.py — repair inbound body cross-references after a reparent.

g-115-1830. `tree-update.sh --reparent` updates `_tree.yaml` and reports
`file_moves` (old->new paths) but does NOT rewrite the inbound prose
cross-references in OTHER nodes' bodies that hardcode a moved node's OLD path.
Left unrepaired, every such ref dangles and only surfaces LATER as a
validate warning (the g-115-1419 detection) — the silent-breakage gap that made
the g-115-398 regroup break 8 inbound refs across 7 nodes (all fixed by hand).

This tool consumes a reparent's `file_moves` and — running AFTER the physical
file moves are applied, so every body is readable at its current path — rewrites
each inbound ref old_path -> new_path. The rewrite is backtick-scoped and
form-preserving (`world/knowledge/tree/...` or the L1-first form), so it never
touches prose that merely mentions a path. It reuses `tree._iter_body_md_refs`
(the same iterator that drives validate's g-115-1419 dangling-ref detection), so
detection and repair can never drift apart.

Usage:
    # dry-run (surface only) — print the inbound refs that WOULD be rewritten:
    bash core/scripts/tree-update.sh --reparent A B | py -3 core/scripts/tree-inbound-ref-fix.py
    # apply — rewrite them (run AFTER applying file_moves physically):
    echo '<reparent-output-or-file_moves-json>' | py -3 core/scripts/tree-inbound-ref-fix.py --apply

Input JSON on stdin: either a reparent's full output (a dict carrying a
`file_moves` key) OR a bare list of `{"old": ..., "new": ...}` (extra keys such
as `key` are ignored). Fail-open on every body: an unreadable/unwritable file is
recorded, never aborts the run.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import tree as _tree  # noqa: E402  (read_tree, _iter_body_md_refs)

_TREE_PREFIX = "world/knowledge/tree/"


def _strip_tree_prefix(p):
    return p[len(_TREE_PREFIX):] if p and p.startswith(_TREE_PREFIX) else p


def build_moved_map(file_moves):
    """old_rel -> new_rel for every move whose path actually changed. Both sides
    normalized to the tree-root-relative form so they compare against the `rel`
    that _iter_body_md_refs yields."""
    moved = {}
    for fm in file_moves or []:
        if not isinstance(fm, dict):
            continue
        old = _strip_tree_prefix((fm.get("old") or "").strip())
        new = _strip_tree_prefix((fm.get("new") or "").strip())
        if old and new and old != new:
            moved[old] = new
    return moved


def find_inbound_refs(moved, nodes=None):
    """Scan every node body for backtick-md refs whose target is a moved node's
    OLD path. Returns [{node, file, old_ref, new_ref, body_abs}]. `nodes` may be
    injected for testing; defaults to the live tree."""
    if not moved:
        return []
    if nodes is None:
        nodes = _tree.read_tree().get("nodes", {})
    out = []
    for key, raw_ref, rel, body_abs in _tree._iter_body_md_refs(nodes):
        if rel not in moved:
            continue
        new_rel = moved[rel]
        # Preserve the reference form the author used.
        if raw_ref.startswith(_TREE_PREFIX):
            new_ref = _TREE_PREFIX + new_rel
        else:
            new_ref = new_rel  # L1-first form
        out.append({
            "node": key,
            "file": nodes.get(key, {}).get("file"),
            "old_ref": raw_ref,
            "new_ref": new_ref,
            "body_abs": body_abs,
        })
    return out


def apply_fix(refs):
    """Rewrite each referencing body: literal `old_ref` -> `new_ref` WITHIN its
    backtick delimiters (so only the tracked cross-ref changes — never prose that
    repeats the path outside backticks). Groups refs by body file (one body may
    carry several moved refs) and records history + changelog per file, mirroring
    tree.write_tree. Fail-open per file. Returns the list of refs actually
    rewritten."""
    from _fileops import (save_history, append_changelog, resolve_base_dir,
                          _agent_name)
    by_file = {}
    for r in refs:
        by_file.setdefault(r["body_abs"], []).append(r)
    fixed = []
    agent = _agent_name()
    for body_abs, group in by_file.items():
        try:
            with open(body_abs, "r", encoding="utf-8") as f:
                body = f.read()
        except (OSError, UnicodeDecodeError) as e:
            for r in group:
                r["error"] = "read failed: {}".format(e)
            continue
        # Single-pass replacement ( Finding 1): build ONE regex
        # alternation of every old_tick and re.sub against the ORIGINAL body, so
        # each occurrence resolves EXACTLY once. The prior per-ref sequential
        # str.replace corrupted CHAINED moves — e.g. A->B and B->C in one batch:
        # after A's replace inserted `B`, B's replace then hit BOTH the
        # pre-existing AND the freshly-inserted `B`, over-rewriting the first to
        # `C`. re.sub scans original string positions and never re-scans inserted
        # text, so a callable replacement (no backref processing) is chain-safe.
        mapping = {"`" + r["old_ref"] + "`": "`" + r["new_ref"] + "`" for r in group}
        # applied_here is computed against the ORIGINAL body (pre-sub): a ref whose
        # tick is absent is not reported as fixed. old_ref != new_ref always
        # (build_moved_map drops no-op moves), so any present old_tick guarantees
        # new_body != body.
        applied_here = [r for r in group if ("`" + r["old_ref"] + "`") in body]
        if not applied_here:
            continue  # nothing matched exactly (already fixed / non-backtick-exact)
        # Longest-tick-first alternation so a shorter tick cannot pre-empt a longer
        # one it prefixes (defensive; backtick-delimited forms don't overlap here).
        pattern = re.compile(
            "|".join(re.escape(t) for t in sorted(mapping, key=len, reverse=True)))
        new_body = pattern.sub(lambda m: mapping[m.group(0)], body)
        if new_body == body:
            continue
        path = Path(body_abs)
        try:
            base_dir = resolve_base_dir(path)
            if base_dir:
                save_history(path, base_dir, agent)
            tmp = str(path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(new_body)
            os.replace(tmp, str(path))
        except (OSError, PermissionError) as e:
            for r in applied_here:
                r["error"] = "write failed: {}".format(e)
            continue
        # Body IS on disk now — record success BEFORE the best-effort changelog
        # append ( Finding 2). The prior code put fixed.extend AFTER
        # append_changelog inside the SAME try, so a changelog-only failure (body
        # already written) wrongly marked the refs "write failed" and dropped them
        # from the fixed count. The changelog is audit-only; its failure is non-fatal.
        fixed.extend(applied_here)
        if base_dir:
            try:
                append_changelog(base_dir, agent, path, "edit")
            except (OSError, PermissionError) as e:
                for r in applied_here:
                    r["changelog_error"] = "changelog append failed: {}".format(e)
    return fixed


def main():
    ap = argparse.ArgumentParser(
        description="Repair inbound body cross-refs after a reparent (g-115-1830)")
    ap.add_argument("--apply", action="store_true",
                    help="Rewrite bodies (default: dry-run, surface only)")
    args = ap.parse_args()

    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps({"error": "no input on stdin (expected reparent output or file_moves JSON)"}))
        sys.exit(1)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": "invalid JSON: {}".format(e)}))
        sys.exit(1)

    if isinstance(data, dict):
        file_moves = data.get("file_moves", [])
    elif isinstance(data, list):
        file_moves = data
    else:
        print(json.dumps({"error": "expected a dict with file_moves or a list of moves"}))
        sys.exit(1)

    moved = build_moved_map(file_moves)
    refs = find_inbound_refs(moved)
    result = {
        "moves": len(moved),
        "inbound_refs_found": len(refs),
        "applied": False,
        "refs": [{"node": r["node"], "file": r["file"],
                  "old_ref": r["old_ref"], "new_ref": r["new_ref"]} for r in refs],
    }
    if args.apply and refs:
        fixed = apply_fix(refs)
        result["applied"] = True
        result["fixed"] = len(fixed)
        result["fix_errors"] = [
            {"node": r["node"], "error": r["error"]} for r in refs if r.get("error")]
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
