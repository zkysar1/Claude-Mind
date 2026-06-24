#!/usr/bin/env python3
"""tree_archive.py — D2 LLM-reviewed tree-node cluster archival engine ().

Implements the D2 design (agents/bravo/temp/d2-cluster-archival-design.md):

  - compute_cluster(leaf_key): cluster of leaves sharing ANY of signal
    A (shared parent) / B (shared source_aspiration) / C (tags Jaccard >= floor).
    MAX_CLUSTER_SIZE guard: if a node's cluster blows past the cap, signal C is
    too loose for it — fall back to A UNION B only (stops a ubiquitous tag like
    `framework` from collapsing half the tree into one cluster).
  - scan: emit stale LEAF candidates (effective-relevance staleness > threshold)
    + each candidate's cluster + whether the cluster is refresh-eligible (a member
    retrieved within the lookback window => refresh the whole cluster, skip archive).
  - apply <key> <verdict>: keep | refresh | archive | decompose-first.
      * interior re-check (refuses child_count > 0 — §5 layer 2)
      * reversibility: archive is a registry mark (node_type=archived) + detach
        from parent.children; write_tree snapshots .history + git tracks bytes.
  - restore <key>: undo an archive (re-attach to parent, clear archived markers).

INVARIANTS (design §5 / verify-before-assuming-applied-to-deletion):
  - LEAVES ONLY. Interior nodes (child_count > 0) are NEVER archival candidates
    and NEVER cluster members.
  - DEFAULT-TO-KEEP. No relevance signal => not eligible (not archived).
  - SINGLE-WRITER for last_relevant_at + node_type=archived (guard-155/rb-254):
    only this engine (and the generic `tree-update.sh --set`) writes them.
  - NATURAL-GATED DORMANT (guard-348): `archives_per_pass: 0` in the config block
    means the autonomous lane scans + reviews + applies keep/refresh but applies
    ZERO archive verdicts. Raise it above 0 to activate destructive archival.
    No `enabled:` boolean — the cap-of-0 IS the gate.

Pure helpers (_jaccard, _compute_cluster_pure, effective_relevance,
staleness_days, parse_frontmatter) import NOTHING from tree.py, so the unit
tests import this module without a configured WORLD_DIR. Disk-backed functions
(scan/apply/restore/compute_cluster) import tree.py + _paths lazily.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

import yaml

# Defaults mirror the d2-cluster-archival-design.md §1/§2/§7 values. The live
# values come from core/config/aspirations.yaml -> tree_archival (loaded by
# _load_config); these are the fallback when the block is absent.
_DEFAULTS = {
    "archive_threshold_days": 180,
    "refresh_lookback_days": 60,
    "tag_jaccard_floor": 0.5,
    "max_cluster_size": 40,
    "archives_per_pass": 0,            # natural gate: 0 == dormant
    "compounding_gate_enabled": False,  # D3 cross-link, flag-behind until the field ships
}


# ---------------------------------------------------------------------------
# Pure helpers — NO tree.py / _paths import (unit-testable without WORLD_DIR).
# ---------------------------------------------------------------------------

def _jaccard(a, b):
    """Jaccard similarity of two sets. 0.0 when both empty (no shared meaning)."""
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 0.0
    union = sa | sb
    if not union:
        return 0.0
    return len(sa & sb) / len(union)


def _parse_date(value):
    """ISO date/datetime string -> date, or None. Tolerant of trailing time."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (ValueError, TypeError):
        return None


def effective_relevance(node):
    """max(last_retrieved, last_relevant_at-or-last_updated).

    last_relevant_at defaults (semantically) to last_updated — the §2
    "default = last_updated" rule realized lazily here so apply_defaults can
    stay read-safe (null on disk until the write path back-fills a concrete
    date). Returns None when the node carries no date signal at all
    (default-to-keep: a node with no relevance clock is never eligible)."""
    lr = _parse_date(node.get("last_retrieved"))
    lra = _parse_date(node.get("last_relevant_at")) or _parse_date(node.get("last_updated"))
    candidates = [d for d in (lr, lra) if d is not None]
    if not candidates:
        return None
    return max(candidates)


def staleness_days(node, today):
    """Days since effective_relevance, or None when no relevance signal."""
    er = effective_relevance(node)
    if er is None:
        return None
    return (today - er).days


def _is_leaf(node):
    """A leaf has zero children (child_count == 0). Robust to either the
    child_count int or the children list being the source of truth."""
    cc = node.get("child_count")
    if cc is not None:
        return cc == 0
    return len(node.get("children") or []) == 0


def _compute_cluster_pure(leaf_key, leaves, fm_map,
                          tag_jaccard_floor=0.5, max_cluster_size=40):
    """Cluster(n) = { m leaf : A(n,m) OR B(n,m) OR C(n,m) }, n in Cluster(n).

    leaves   : dict leaf_key -> node-record (LEAVES ONLY; callers pre-filter so
               interior nodes are excluded from membership entirely — §1 edge iii).
    fm_map   : dict leaf_key -> {"tags": set[str], "asp": str|None} (front matter).
    Returns a set of leaf keys. Reflexive. Applies the MAX_CLUSTER_SIZE scope
    guard (fall back to A UNION B when C makes the cluster too loose — §1 edge ii
    safety + the mega-cluster guard).
    """
    if leaf_key not in leaves:
        # Not a leaf (or unknown) — a cluster is a set of leaves; reflexive only.
        return {leaf_key}

    n_node = leaves[leaf_key]
    n_parent = n_node.get("parent")
    n_fm = fm_map.get(leaf_key, {})
    n_tags = set(n_fm.get("tags") or [])
    n_asp = n_fm.get("asp")

    def _members(use_c):
        out = {leaf_key}
        for m_key, m_node in leaves.items():
            if m_key == leaf_key:
                continue
            m_fm = fm_map.get(m_key, {})
            # Signal A — shared parent (both non-null).
            a = n_parent is not None and m_node.get("parent") == n_parent
            # Signal B — shared source aspiration (both non-null).
            b = n_asp is not None and m_fm.get("asp") == n_asp
            # Signal C — tags Jaccard >= floor (both non-empty). NOT mere
            # non-empty intersection (regression guard, §1 edge ii).
            c = False
            if use_c:
                m_tags = set(m_fm.get("tags") or [])
                c = bool(n_tags) and bool(m_tags) and \
                    _jaccard(n_tags, m_tags) >= tag_jaccard_floor
            if a or b or c:
                out.add(m_key)
        return out

    cluster = _members(use_c=True)
    if len(cluster) > max_cluster_size:
        # Signal C too loose for this node — fall back to A UNION B only.
        cluster = _members(use_c=False)
    return cluster


def parse_frontmatter(path):
    """Parse YAML front matter (between the first two `---`) of a tree-node .md.
    Returns {} on any error (missing file, no front matter, bad YAML)."""
    if not path:
        return {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    if not text.lstrip().startswith("---"):
        return {}
    body = text.lstrip()
    parts = body.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        fm = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return {}
    return fm if isinstance(fm, dict) else {}


def _fm_signals(fm):
    """Extract the {tags, asp} cluster signals from a node's front matter dict."""
    tags = fm.get("tags") or []
    if not isinstance(tags, (list, tuple, set)):
        tags = []
    asp = fm.get("source_aspiration")
    if not asp:
        src = fm.get("source")
        if isinstance(src, str) and src.startswith("asp-"):
            asp = src
    return {"tags": set(tags), "asp": asp}


# ---------------------------------------------------------------------------
# Config (natural-gated). Lazy _paths import.
# ---------------------------------------------------------------------------

def _load_config():
    """Read core/config/aspirations.yaml -> tree_archival, merged over defaults.
    Fail-open to defaults (dormant) on any error."""
    cfg = dict(_DEFAULTS)
    try:
        from _paths import CONFIG_DIR
        path = Path(CONFIG_DIR) / "aspirations.yaml"
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            block = data.get("tree_archival")
            if isinstance(block, dict):
                cfg.update({k: block[k] for k in block if k in _DEFAULTS})
    except Exception:
        pass
    return cfg


def is_dormant(cfg=None):
    """Natural gate (guard-348): archives_per_pass <= 0 => dormant (no archive
    verdicts auto-applied). Scan/keep/refresh remain available."""
    cfg = cfg or _load_config()
    try:
        return int(cfg.get("archives_per_pass", 0)) <= 0
    except (TypeError, ValueError):
        return True


# ---------------------------------------------------------------------------
# Disk-backed operations. Lazy tree.py import.
# ---------------------------------------------------------------------------

def _abs_node_path(file_field):
    """Resolve a node's virtual `world/...` file field to an absolute path."""
    if not file_field:
        return None
    from _paths import WORLD_DIR
    f = str(file_field).replace("\\", "/")
    if f.startswith("world/"):
        return Path(WORLD_DIR) / f[len("world/"):]
    return Path(WORLD_DIR) / "knowledge" / "tree" / f


def _leaves_and_frontmatter(tree, read_fm=True):
    """Build (leaves, fm_map) from a tree dict. Leaves exclude interior nodes and
    already-archived nodes. fm_map maps leaf_key -> {tags,asp} (front matter)."""
    nodes = tree.get("nodes", {})
    leaves = {
        k: v for k, v in nodes.items()
        if _is_leaf(v) and v.get("node_type") != "archived"
    }
    fm_map = {}
    if read_fm:
        for k, node in leaves.items():
            fm_map[k] = _fm_signals(parse_frontmatter(_abs_node_path(node.get("file"))))
    return leaves, fm_map


def compute_cluster(leaf_key, tree=None, cfg=None):
    """Disk-backed cluster for a live leaf key (reads node .md front matter)."""
    cfg = cfg or _load_config()
    if tree is None:
        from tree import read_tree
        tree = read_tree()
    leaves, fm_map = _leaves_and_frontmatter(tree)
    return _compute_cluster_pure(
        leaf_key, leaves, fm_map,
        tag_jaccard_floor=cfg["tag_jaccard_floor"],
        max_cluster_size=cfg["max_cluster_size"],
    )


def scan(today=None, scope_keys=None, cfg=None):
    """Emit stale leaf candidates + clusters + refresh-eligibility. READ-ONLY
    (writes nothing — back-fill happens lazily in effective_relevance and
    persistently only on apply). Returns a JSON-serialisable dict."""
    from tree import read_tree, apply_defaults
    cfg = cfg or _load_config()
    today = today or date.today()
    tree = read_tree()
    # apply_defaults so last_relevant_at / node_type are present in-memory.
    raw_nodes = tree.get("nodes", {})
    tree = {"nodes": {k: apply_defaults(v) for k, v in raw_nodes.items()}}
    leaves, fm_map = _leaves_and_frontmatter(tree)

    threshold = cfg["archive_threshold_days"]
    lookback = cfg["refresh_lookback_days"]
    scope = set(scope_keys) if scope_keys else None

    candidates = []
    for k, node in leaves.items():
        if scope and k not in scope:
            continue
        sd = staleness_days(node, today)
        if sd is None or sd <= threshold:
            continue
        cluster = _compute_cluster_pure(
            k, leaves, fm_map,
            tag_jaccard_floor=cfg["tag_jaccard_floor"],
            max_cluster_size=cfg["max_cluster_size"],
        )
        refresh_eligible = False
        for m in cluster:
            lr = _parse_date(leaves[m].get("last_retrieved"))
            if lr is not None and (today - lr).days <= lookback:
                refresh_eligible = True
                break
        candidates.append({
            "key": k,
            "summary": node.get("summary", ""),
            "depth": node.get("depth"),
            "parent": node.get("parent"),
            "last_retrieved": node.get("last_retrieved"),
            "last_relevant_at": node.get("last_relevant_at"),
            "last_updated": node.get("last_updated"),
            "retrieval_count": node.get("retrieval_count", 0),
            "staleness_days": sd,
            "cluster": sorted(cluster),
            "cluster_size": len(cluster),
            "refresh_eligible": refresh_eligible,
            "file": node.get("file"),
        })

    candidates.sort(key=lambda c: c["staleness_days"], reverse=True)
    return {
        "today": today.isoformat(),
        "config": cfg,
        "dormant": is_dormant(cfg),
        "leaf_count": len(leaves),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


_VALID_VERDICTS = ("keep", "refresh", "archive", "decompose-first")


def apply(key, verdict, today=None, cfg=None, force=False, reason=""):
    """Apply a per-candidate verdict. Returns a result dict; raises ValueError on
    a bad verdict. Interior re-check refuses child_count > 0 for archive."""
    if verdict not in _VALID_VERDICTS:
        raise ValueError("invalid verdict: %r (expected one of %s)"
                         % (verdict, ", ".join(_VALID_VERDICTS)))
    from tree import read_tree, write_tree, apply_defaults
    cfg = cfg or _load_config()
    today = today or date.today()
    tree = read_tree()
    nodes = tree.get("nodes", {})
    if key not in nodes:
        return {"ok": False, "key": key, "verdict": verdict,
                "error": "node_not_found"}
    node = apply_defaults(nodes[key])
    nodes[key] = node

    if verdict == "decompose-first":
        # Non-destructive routing signal — never archives this sweep.
        return {"ok": True, "key": key, "verdict": verdict,
                "routed_to": "tree maintain (decompose lane)", "reason": reason}

    if verdict == "keep":
        node["last_relevant_at"] = today.isoformat()
        if reason:
            node["last_relevant_reason"] = reason
        write_tree(tree)
        return {"ok": True, "key": key, "verdict": verdict,
                "last_relevant_at": node["last_relevant_at"]}

    if verdict == "refresh":
        leaves, fm_map = _leaves_and_frontmatter(tree)
        cluster = _compute_cluster_pure(
            key, leaves, fm_map,
            tag_jaccard_floor=cfg["tag_jaccard_floor"],
            max_cluster_size=cfg["max_cluster_size"],
        )
        refreshed = []
        for m in cluster:
            if m in nodes and _is_leaf(nodes[m]) and nodes[m].get("node_type") != "archived":
                nodes[m]["last_relevant_at"] = today.isoformat()
                refreshed.append(m)
        write_tree(tree)
        return {"ok": True, "key": key, "verdict": verdict,
                "cluster_refreshed": sorted(refreshed), "count": len(refreshed)}

    # verdict == "archive"
    # §5 layer 2: interior re-check immediately before the move.
    if not _is_leaf(node):
        return {"ok": False, "key": key, "verdict": verdict,
                "error": "interior_node_refused",
                "detail": "child_count=%s > 0; interior nodes are NEVER archived"
                          % node.get("child_count")}
    if node.get("node_type") == "archived":
        return {"ok": False, "key": key, "verdict": verdict,
                "error": "already_archived"}
    # Natural gate: dormant unless forced (manual/test apply) or cap raised.
    if is_dormant(cfg) and not force:
        return {"ok": False, "key": key, "verdict": verdict,
                "error": "dormant",
                "detail": "tree_archival.archives_per_pass=0 (natural-gated "
                          "dormant); pass --force or raise the cap to archive"}

    parent_key = node.get("parent")
    detached_from = None
    parent_emptied = False
    if parent_key and parent_key in nodes:
        parent = nodes[parent_key]
        children = parent.get("children") or []
        if key in children:
            children = [c for c in children if c != key]
            parent["children"] = children
            parent["child_count"] = len(children)
            detached_from = parent_key
            # §5 corollary: archival never cascades upward. An emptied interior
            # parent is flagged, NOT auto-archived.
            parent_emptied = (len(children) == 0)

    node["node_type"] = "archived"
    node["archived_at"] = today.isoformat()
    node["archived_from_parent"] = parent_key  # for --restore re-attach
    if reason:
        node["archived_reason"] = reason
    write_tree(tree)  # snapshots .history + git-tracks bytes (reversible)
    return {"ok": True, "key": key, "verdict": verdict,
            "archived_at": node["archived_at"], "detached_from": detached_from,
            "parent_emptied_flag": parent_emptied}


def restore(key, today=None):
    """Undo an archive: re-attach to parent, clear archived markers."""
    from tree import read_tree, write_tree
    tree = read_tree()
    nodes = tree.get("nodes", {})
    if key not in nodes:
        return {"ok": False, "key": key, "error": "node_not_found"}
    node = nodes[key]
    if node.get("node_type") == "archived":
        node["node_type"] = "leaf"
    parent_key = node.get("archived_from_parent") or node.get("parent")
    reattached_to = None
    if parent_key and parent_key in nodes:
        parent = nodes[parent_key]
        children = parent.get("children") or []
        if key not in children:
            children.append(key)
            parent["children"] = children
            parent["child_count"] = len(children)
            reattached_to = parent_key
    node.pop("archived_at", None)
    node.pop("archived_from_parent", None)
    node.pop("archived_reason", None)
    node["last_relevant_at"] = (today or date.today()).isoformat()
    write_tree(tree)
    return {"ok": True, "key": key, "reattached_to": reattached_to,
            "node_type": node.get("node_type")}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="D2 tree-node cluster archival engine (g-303-29)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="emit stale leaf candidates + clusters")
    p_scan.add_argument("--scope", help="comma-separated leaf keys to limit the scan")
    p_scan.add_argument("--today", help="override today (ISO date) for testing")

    p_apply = sub.add_parser("apply", help="apply a per-candidate verdict")
    p_apply.add_argument("key")
    p_apply.add_argument("verdict", choices=_VALID_VERDICTS)
    p_apply.add_argument("--reason", default="")
    p_apply.add_argument("--force", action="store_true",
                         help="bypass the natural-gate dormancy (manual/test archive)")
    p_apply.add_argument("--today", help="override today (ISO date) for testing")

    p_restore = sub.add_parser("restore", help="undo an archive")
    p_restore.add_argument("key")

    p_cluster = sub.add_parser("cluster", help="print the cluster of a leaf key")
    p_cluster.add_argument("key")

    args = parser.parse_args(argv)
    today = _parse_date(getattr(args, "today", None))

    if args.cmd == "scan":
        scope = args.scope.split(",") if args.scope else None
        out = scan(today=today, scope_keys=scope)
        print(json.dumps(out, indent=2))
        return 0
    if args.cmd == "apply":
        out = apply(args.key, args.verdict, today=today,
                    force=args.force, reason=args.reason)
        print(json.dumps(out, indent=2))
        return 0 if out.get("ok") else 1
    if args.cmd == "restore":
        out = restore(args.key, today=today)
        print(json.dumps(out, indent=2))
        return 0 if out.get("ok") else 1
    if args.cmd == "cluster":
        out = {"key": args.key, "cluster": sorted(compute_cluster(args.key))}
        print(json.dumps(out, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    # Make stdout robust on Windows (mirrors tree.py).
    try:
        from _stdio import reconfigure_stdio
        reconfigure_stdio()
    except Exception:
        pass
    sys.exit(main())
