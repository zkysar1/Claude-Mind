#!/usr/bin/env python3
"""Memory tree engine for _tree.yaml mechanical operations.

All shell scripts are thin wrappers around this. Subcommands managed via argparse.
The LLM keeps semantic reasoning (which node best matches a category?), while
this script handles mechanical YAML parsing (get node metadata, walk ancestors,
compute paths, increment counters).
"""

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

# Ensure stdout/stderr handle unicode on all platforms (Windows cp1252 fix)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

from _paths import PROJECT_ROOT, WORLD_DIR, CONFIG_DIR

REPO_ROOT = str(PROJECT_ROOT)
TREE_PATH = str(WORLD_DIR / "knowledge" / "tree" / "_tree.yaml")


# ---------------------------------------------------------------------------
# Helpers: file I/O
# ---------------------------------------------------------------------------

def read_tree():
    """Parse _tree.yaml and return the full dict."""
    if not os.path.exists(TREE_PATH):
        print("Tree file not found: " + TREE_PATH, file=sys.stderr)
        sys.exit(1)
    with open(TREE_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "nodes" not in data:
        print("Invalid tree file: missing 'nodes' key", file=sys.stderr)
        sys.exit(1)
    return data


def write_tree(data):
    """Atomic write with locking, history, and sort_keys=False to preserve key order."""
    from _fileops import acquire_lock, release_lock, save_history, append_changelog, resolve_base_dir, _agent_name
    data["last_updated"] = date.today().isoformat()
    path = Path(TREE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    base_dir = resolve_base_dir(path)
    lock_path = path.with_suffix(".lock")
    acquire_lock(lock_path)
    try:
        agent = _agent_name()
        if base_dir:
            save_history(path, base_dir, agent)
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=None, sort_keys=False,
                      allow_unicode=True, width=200)
        os.replace(tmp, TREE_PATH)
        if base_dir:
            append_changelog(base_dir, agent, path, "edit")
    finally:
        release_lock(lock_path)


# ---------------------------------------------------------------------------
# Helpers: node operations
# ---------------------------------------------------------------------------

def apply_defaults(node):
    """Fill missing fields with defaults. Returns a new dict (does not mutate)."""
    out = dict(node)
    if "article_count" not in out:
        out["article_count"] = 0
    if "growth_state" not in out:
        out["growth_state"] = "stable"
    children = out.get("children", [])
    if "node_type" not in out:
        out["node_type"] = "interior" if children else "leaf"
    # Utility tracking fields
    if "retrieval_count" not in out:
        out["retrieval_count"] = 0
    if "times_helpful" not in out:
        out["times_helpful"] = 0
    if "times_noise" not in out:
        out["times_noise"] = 0
    if "utility_ratio" not in out:
        out["utility_ratio"] = 0.0
    return out


def get_node(tree, key):
    """Get a node by key or exit with error."""
    nodes = tree.get("nodes", {})
    if key not in nodes:
        print("Node not found: " + key, file=sys.stderr)
        sys.exit(1)
    return nodes[key]


def walk_ancestors(tree, key):
    """Follow parent pointers from key up to root. Returns list of dicts
    [{key, file, depth}, ...] from the starting node to root.
    Detects cycles."""
    nodes = tree.get("nodes", {})
    if key not in nodes:
        print("Node not found: " + key, file=sys.stderr)
        sys.exit(1)

    result = []
    visited = set()
    current = key
    while current is not None:
        if current in visited:
            print("Cycle detected at node: " + current, file=sys.stderr)
            sys.exit(1)
        visited.add(current)
        if current not in nodes:
            print("Broken parent chain: node '" + current + "' not found", file=sys.stderr)
            sys.exit(1)
        node = nodes[current]
        result.append({
            "key": current,
            "file": node.get("file", ""),
            "depth": node.get("depth", 0),
        })
        current = node.get("parent")
    return result


def compute_child_path(parent_file, child_slug):
    """Compute the file path for a new child node.
    Strip .md from parent, use as directory, append {slug}.md.
    Uses string manipulation to preserve forward slashes."""
    if parent_file.endswith(".md"):
        parent_dir = parent_file[:-3]
    else:
        parent_dir = parent_file
    return parent_dir + "/" + child_slug + ".md"


def get_all_leaves(tree):
    """Return all leaf nodes (empty children list)."""
    nodes = tree.get("nodes", {})
    leaves = []
    for key, node in nodes.items():
        children = node.get("children", [])
        if not children:
            out = apply_defaults(node)
            out["key"] = key
            leaves.append(out)
    return leaves


def get_leaves_under(tree, key):
    """DFS from key, return all leaf descendants (including key itself if leaf)."""
    nodes = tree.get("nodes", {})
    if key not in nodes:
        print("Node not found: " + key, file=sys.stderr)
        sys.exit(1)

    result = []
    stack = [key]
    visited = set()
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        if current not in nodes:
            continue
        node = nodes[current]
        children = node.get("children", [])
        if not children:
            out = apply_defaults(node)
            out["key"] = current
            result.append(out)
        else:
            for child in reversed(children):
                stack.append(child)
    return result


def get_active_content(tree, key):
    """Return only ## Decision Rules and ## Verified Values sections from node .md file."""
    node = get_node(tree, key)
    file_path = node.get("file", "")
    if not file_path:
        return {"key": key, "active_content": None, "sections_found": []}
    abs_path = os.path.join(REPO_ROOT, file_path)
    if not os.path.exists(abs_path):
        return {"key": key, "active_content": None, "sections_found": []}

    with open(abs_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    active_prefixes = ("## Decision Rules", "## Verified Values")
    result_lines = []
    sections_found = []
    in_active = False
    # Front matter: exactly two --- delimiters at the start. After the second,
    # stop checking for --- entirely so body horizontal rules don't misfire.
    fm_count = 0
    past_front_matter = False

    for line in lines:
        stripped = line.rstrip()
        if not past_front_matter and stripped == "---":
            fm_count += 1
            if fm_count >= 2:
                past_front_matter = True
            continue
        if not past_front_matter:
            continue
        if stripped.startswith("## "):
            if any(stripped.startswith(p) for p in active_prefixes):
                in_active = True
                sections_found.append(stripped)
                result_lines.append(line)
            else:
                in_active = False
        elif in_active:
            result_lines.append(line)

    content = "".join(result_lines).strip() if result_lines else None
    return {"key": key, "active_content": content, "sections_found": sections_found}


def get_distill_candidates(tree):
    """Return leaf nodes eligible for DISTILL based on utility thresholds.
    Reads thresholds from core/config/tree.yaml pruning section."""
    config_path = str(CONFIG_DIR / "tree.yaml")
    pruning = {}
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        pruning = cfg.get("pruning", {})
    threshold = pruning.get("distill_utility_threshold", 0.3)
    min_ret = pruning.get("distill_min_retrievals", 5)
    line_threshold = pruning.get("distill_line_threshold", 50)
    line_util_threshold = pruning.get("distill_line_utility_threshold", 0.5)

    nodes = tree.get("nodes", {})
    candidates = []
    for key, node in nodes.items():
        if node.get("children"):
            continue  # skip interior nodes
        rc = node.get("retrieval_count", 0)
        ur = node.get("utility_ratio", 0.0)
        # Criterion 1: low utility after sufficient retrievals
        crit1 = rc >= min_ret and ur < threshold
        # Criterion 2: large node with mediocre payoff
        file_path = node.get("file", "")
        abs_path = os.path.join(REPO_ROOT, file_path) if file_path else ""
        line_count = 0
        if abs_path and os.path.exists(abs_path):
            with open(abs_path, "r", encoding="utf-8") as f:
                line_count = sum(1 for _ in f)
        crit2 = line_count > line_threshold and ur < line_util_threshold and rc >= min_ret
        if crit1 or crit2:
            candidates.append({
                "key": key,
                "utility_ratio": ur,
                "retrieval_count": rc,
                "times_helpful": node.get("times_helpful", 0),
                "times_noise": node.get("times_noise", 0),
                "line_count": line_count,
                "file": file_path,
                "trigger": "low_utility" if crit1 else "large_mediocre",
            })
    return sorted(candidates, key=lambda x: x["utility_ratio"])


def get_children(tree, key):
    """Return immediate children of a node as JSON array with defaults."""
    nodes = tree.get("nodes", {})
    if key not in nodes:
        print("Node not found: " + key, file=sys.stderr)
        sys.exit(1)

    node = nodes[key]
    children_keys = node.get("children", [])
    result = []
    for ck in children_keys:
        if ck in nodes:
            child = apply_defaults(nodes[ck])
            child["key"] = ck
            result.append(child)
    return result


def compute_stats(tree):
    """Compute tree statistics."""
    nodes = tree.get("nodes", {})
    by_depth = {}
    interior_count = 0
    leaf_count = 0

    for key, node in nodes.items():
        depth = node.get("depth", 0)
        depth_str = str(depth)
        by_depth[depth_str] = by_depth.get(depth_str, 0) + 1

        children = node.get("children", [])
        if children:
            interior_count += 1
        else:
            leaf_count += 1

    return {
        "total_nodes": len(nodes),
        "by_depth": by_depth,
        "interior_count": interior_count,
        "leaf_count": leaf_count,
    }


def get_decompose_candidates(tree, threshold=80):
    """Return leaf nodes whose .md file exceeds threshold lines and depth < D_max."""
    D_MAX = 6
    leaves = get_all_leaves(tree)
    candidates = []
    for leaf in leaves:
        file_path = leaf.get("file", "")
        depth = leaf.get("depth", 0)
        if not file_path or depth >= D_MAX:
            continue
        abs_path = os.path.join(REPO_ROOT, file_path)
        if not os.path.exists(abs_path):
            continue
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                line_count = sum(1 for _ in f)
        except (OSError, UnicodeDecodeError):
            continue
        if line_count > threshold:
            candidates.append({
                "key": leaf["key"],
                "file": file_path,
                "line_count": line_count,
                "depth": depth,
                "growth_state": leaf.get("growth_state", "stable"),
            })
    candidates.sort(key=lambda c: c["line_count"], reverse=True)
    return candidates


def get_redistribute_candidates(tree, threshold=80):
    """Return interior nodes whose .md body exceeds threshold lines and depth < D_max.

    These nodes have children but retain large body content that should be
    redistributed into those children or into new children."""
    D_MAX = 6
    nodes = tree.get("nodes", {})
    candidates = []
    for key, node in nodes.items():
        children = node.get("children", [])
        if not children:
            continue  # leaves handled by get_decompose_candidates
        file_path = node.get("file", "")
        depth = node.get("depth", 0)
        if not file_path or depth >= D_MAX:
            continue
        abs_path = os.path.join(REPO_ROOT, file_path)
        if not os.path.exists(abs_path):
            continue
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                line_count = sum(1 for _ in f)
        except (OSError, UnicodeDecodeError):
            continue
        if line_count > threshold:
            candidates.append({
                "key": key,
                "file": file_path,
                "line_count": line_count,
                "depth": depth,
                "child_count": len(children),
                "children": children,
                "growth_state": node.get("growth_state", "stable"),
            })
    candidates.sort(key=lambda c: c["line_count"], reverse=True)
    return candidates


def validate_tree(tree):
    """Check parent-child consistency and field completeness.
    Returns {valid: bool, errors: [...], warnings: [...]}."""
    nodes = tree.get("nodes", {})
    errors = []
    warnings = []

    for key, node in nodes.items():
        # Check parent reference
        parent = node.get("parent")
        if parent is not None:
            if parent not in nodes:
                errors.append("Node '{}' references non-existent parent '{}'".format(key, parent))
            else:
                parent_children = nodes[parent].get("children", [])
                if key not in parent_children:
                    errors.append("Node '{}' claims parent '{}', but parent doesn't list it as child".format(key, parent))

        # Check children references
        children = node.get("children", [])
        for child in children:
            if child not in nodes:
                errors.append("Node '{}' lists non-existent child '{}'".format(key, child))
            else:
                child_parent = nodes[child].get("parent")
                if child_parent != key:
                    errors.append("Node '{}' lists child '{}', but child's parent is '{}'".format(key, child, child_parent))

        # Check child_count consistency
        child_count = node.get("child_count")
        if child_count is not None and child_count != len(children):
            errors.append("Node '{}' has child_count={} but {} children listed".format(key, child_count, len(children)))

        # Check depth consistency
        if parent is not None and parent in nodes:
            parent_depth = nodes[parent].get("depth", 0)
            node_depth = node.get("depth", 0)
            if node_depth != parent_depth + 1:
                errors.append("Node '{}' depth={} but parent '{}' depth={} (expected {})".format(
                    key, node_depth, parent, parent_depth, parent_depth + 1))

        # Check missing default fields
        if "article_count" not in node:
            warnings.append("Node '{}' missing 'article_count' (default: 0)".format(key))
        if "growth_state" not in node:
            warnings.append("Node '{}' missing 'growth_state' (default: stable)".format(key))
        if "node_type" not in node:
            expected = "interior" if children else "leaf"
            warnings.append("Node '{}' missing 'node_type' (default: {})".format(key, expected))

    return {"valid": len(errors) == 0, "errors": errors, "warnings": warnings}


def parse_value(value_str):
    """Parse a string value into the appropriate Python type."""
    if value_str == "true":
        return True
    if value_str == "false":
        return False
    if value_str == "null" or value_str == "None":
        return None
    if value_str == "[]":
        return []
    # Try JSON parse for complex values
    if value_str.startswith("{") or value_str.startswith("["):
        try:
            return json.loads(value_str)
        except json.JSONDecodeError:
            pass
    # Try int
    try:
        return int(value_str)
    except ValueError:
        pass
    # Try float
    try:
        return float(value_str)
    except ValueError:
        pass
    return value_str


# ---------------------------------------------------------------------------
# Read subcommands
# ---------------------------------------------------------------------------

def cmd_read(args):
    tree = read_tree()

    if args.node:
        node = get_node(tree, args.node)
        out = apply_defaults(node)
        out["key"] = args.node
        print(json.dumps(out, indent=2, ensure_ascii=False))

    elif args.path:
        node = get_node(tree, args.path)
        print(node.get("file", ""))

    elif args.ancestors:
        chain = walk_ancestors(tree, args.ancestors)
        print(json.dumps(chain, indent=2, ensure_ascii=False))

    elif args.children:
        result = get_children(tree, args.children)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.leaves:
        result = get_all_leaves(tree)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.leaves_under:
        result = get_leaves_under(tree, args.leaves_under)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.stats:
        result = compute_stats(tree)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.child_path:
        if len(args.child_path) != 2:
            print("--child-path requires exactly 2 args: <parent-key> <slug>", file=sys.stderr)
            sys.exit(1)
        parent_key, slug = args.child_path
        parent_node = get_node(tree, parent_key)
        path = compute_child_path(parent_node.get("file", ""), slug)
        print(path)

    elif args.validate:
        result = validate_tree(tree)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.decompose_candidates:
        threshold = args.threshold if args.threshold is not None else 80
        result = get_decompose_candidates(tree, threshold)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.redistribute_candidates:
        threshold = args.threshold if args.threshold is not None else 80
        result = get_redistribute_candidates(tree, threshold)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.find:
        from tree_match import find_nodes
        entity_index = tree.get("entity_index", {})
        result = find_nodes(args.find, tree["nodes"], entity_index,
                            top=args.top, leaf_only=args.leaf_only)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.active_content:
        result = get_active_content(tree, args.active_content)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.distill_candidates:
        result = get_distill_candidates(tree)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.summary:
        nodes = tree.get("nodes", {})
        compact = {}
        for key, node in nodes.items():
            compact[key] = {
                "file": node.get("file", ""),
                "summary": node.get("summary", ""),
                "depth": node.get("depth", 0),
                "capability_level": node.get("capability_level", ""),
                "confidence": node.get("confidence"),
                "children": node.get("children", []),
            }
        print(json.dumps({"nodes": compact, "total": len(compact)},
                          indent=2, ensure_ascii=False))

    else:
        print("Specify a read subcommand. Use --help for options.", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Write subcommands
# ---------------------------------------------------------------------------

def cmd_update(args):
    if args.set:
        cmd_set(args)
    elif args.add_child:
        cmd_add_child(args)
    elif args.remove_child:
        cmd_remove_child(args)
    elif args.increment:
        cmd_increment(args)
    elif args.batch:
        cmd_batch(args)
    elif args.propagate:
        cmd_propagate(args)
    else:
        print("Specify an update subcommand. Use --help for options.", file=sys.stderr)
        sys.exit(1)


def cmd_set(args):
    if len(args.set) != 3:
        print("--set requires exactly 3 args: <key> <field> <value>", file=sys.stderr)
        sys.exit(1)
    key, field, value_str = args.set
    value = parse_value(value_str)

    tree = read_tree()
    node = get_node(tree, key)
    node[field] = value
    tree["nodes"][key] = node
    write_tree(tree)

    out = apply_defaults(node)
    out["key"] = key
    print(json.dumps(out, indent=2, ensure_ascii=False))


def cmd_add_child(args):
    parent_key = args.add_child

    if sys.stdin.isatty():
        print("Error: expected JSON on stdin (not a terminal)", file=sys.stderr)
        sys.exit(1)
    raw = sys.stdin.read().strip()
    if not raw:
        print("No input provided on stdin", file=sys.stderr)
        sys.exit(1)
    try:
        child_data = json.loads(raw)
    except json.JSONDecodeError as e:
        print("Invalid JSON: " + str(e), file=sys.stderr)
        sys.exit(1)

    if "key" not in child_data:
        print("Child JSON must include 'key' field", file=sys.stderr)
        sys.exit(1)

    child_key = child_data["key"]

    tree = read_tree()
    nodes = tree.get("nodes", {})

    # Validate parent exists
    if parent_key not in nodes:
        print("Parent node not found: " + parent_key, file=sys.stderr)
        sys.exit(1)

    # Validate key is unique
    if child_key in nodes:
        print("Node key already exists: " + child_key, file=sys.stderr)
        sys.exit(1)

    parent = nodes[parent_key]
    parent_depth = parent.get("depth", 0)

    # Build child node
    child_node = {}
    # Compute file path if not provided
    if "file" not in child_data:
        child_node["file"] = compute_child_path(parent.get("file", ""), child_key)
    else:
        child_node["file"] = child_data["file"]

    child_node["depth"] = parent_depth + 1
    child_node["parent"] = parent_key
    child_node["children"] = child_data.get("children", [])
    child_node["child_count"] = len(child_node["children"])

    # Copy optional fields from input
    for field in ("summary", "domain_confidence", "capability_level", "confidence",
                  "article_count", "growth_state", "node_type"):
        if field in child_data:
            child_node[field] = child_data[field]

    # Persist defaults so raw YAML always has article_count, growth_state, node_type
    child_node = apply_defaults(child_node)

    # Add child to tree
    nodes[child_key] = child_node

    # Update parent's children list and child_count
    if child_key not in parent.get("children", []):
        if "children" not in parent:
            parent["children"] = []
        parent["children"].append(child_key)
        parent["child_count"] = len(parent["children"])

    tree["nodes"] = nodes
    write_tree(tree)

    out = apply_defaults(child_node)  # copy — do not add "key" to child_node directly
    out["key"] = child_key
    print(json.dumps(out, indent=2, ensure_ascii=False))


def cmd_remove_child(args):
    if len(args.remove_child) != 2:
        print("--remove-child requires exactly 2 args: <parent-key> <child-key>", file=sys.stderr)
        sys.exit(1)
    parent_key, child_key = args.remove_child

    tree = read_tree()
    nodes = tree.get("nodes", {})

    if parent_key not in nodes:
        print("Parent node not found: " + parent_key, file=sys.stderr)
        sys.exit(1)

    parent = nodes[parent_key]
    children = parent.get("children", [])
    if child_key not in children:
        print("Child '{}' not in parent '{}' children list".format(child_key, parent_key), file=sys.stderr)
        sys.exit(1)

    # Remove from parent's children
    children.remove(child_key)
    parent["children"] = children
    parent["child_count"] = len(children)

    # Remove the child node itself
    if child_key in nodes:
        del nodes[child_key]

    tree["nodes"] = nodes
    write_tree(tree)

    print(json.dumps({"removed": child_key, "parent": parent_key}, ensure_ascii=False))


def cmd_increment(args):
    if len(args.increment) != 2:
        print("--increment requires exactly 2 args: <key> <field>", file=sys.stderr)
        sys.exit(1)
    key, field = args.increment

    tree = read_tree()
    node = get_node(tree, key)

    current = node.get(field, 0)
    if not isinstance(current, (int, float)):
        current = 0
    node[field] = current + 1
    # utility_ratio is ONLY recomputed here (and in cmd_batch). Never set it manually.
    if field in ("times_helpful", "retrieval_count", "times_noise"):
        rc = node.get("retrieval_count", 0)
        th = node.get("times_helpful", 0)
        node["utility_ratio"] = round(th / max(rc, 1), 4)
    tree["nodes"][key] = node
    write_tree(tree)

    out = apply_defaults(node)
    out["key"] = key
    print(json.dumps(out, indent=2, ensure_ascii=False))


def cmd_batch(args):
    """Batch update: read JSON operations from stdin and apply atomically."""
    if sys.stdin.isatty():
        print("Error: expected JSON on stdin (not a terminal)", file=sys.stderr)
        sys.exit(1)
    raw = sys.stdin.read().strip()
    if not raw:
        print("No input provided on stdin", file=sys.stderr)
        sys.exit(1)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print("Invalid JSON: " + str(e), file=sys.stderr)
        sys.exit(1)

    operations = data.get("operations", [])
    if not operations:
        print("No operations in batch", file=sys.stderr)
        sys.exit(1)

    tree = read_tree()
    nodes = tree.get("nodes", {})

    # Validate all keys exist before any mutation
    for i, op in enumerate(operations):
        op_type = op.get("op")
        key = op.get("key")
        field = op.get("field")
        if not op_type or not key or not field:
            print("Operation {} missing required fields (op, key, field)".format(i), file=sys.stderr)
            sys.exit(1)
        if op_type not in ("set", "increment"):
            print("Operation {} has invalid op '{}' (must be 'set' or 'increment')".format(i, op_type), file=sys.stderr)
            sys.exit(1)
        if key not in nodes:
            print("Operation {} references non-existent node '{}'".format(i, key), file=sys.stderr)
            sys.exit(1)

    # Apply all operations
    updated_keys = set()
    for op in operations:
        key = op["key"]
        field = op["field"]
        node = nodes[key]

        if op["op"] == "set":
            value = op.get("value")
            if isinstance(value, str):
                value = parse_value(value)
            node[field] = value
        elif op["op"] == "increment":
            current = node.get(field, 0)
            if not isinstance(current, (int, float)):
                current = 0
            node[field] = current + 1
            # utility_ratio is ONLY recomputed here (and in cmd_batch). Never set it manually.
            if field in ("times_helpful", "retrieval_count", "times_noise"):
                rc = node.get("retrieval_count", 0)
                th = node.get("times_helpful", 0)
                node["utility_ratio"] = round(th / max(rc, 1), 4)

        nodes[key] = node
        updated_keys.add(key)

    tree["nodes"] = nodes
    write_tree(tree)

    # Return updated nodes
    result = []
    for key in updated_keys:
        out = apply_defaults(nodes[key])
        out["key"] = key
        result.append(out)

    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_propagate(args):
    """Propagate confidence up parent chain from a node.

    Walks ancestors from node to root (skipping self). For each ancestor,
    computes avg confidence from its children, updates confidence +
    domain_confidence, and detects capability_level threshold crossings.
    Stops if confidence is unchanged at current level.
    """
    key = args.propagate

    tree = read_tree()
    nodes = tree.get("nodes", {})

    # Read competence mapping from config
    config_path = str(CONFIG_DIR / "tree.yaml")
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        competence = config.get("domain_health", {}).get("competence_mapping", {})
    else:
        competence = {"EXPLORE": 0.25, "CALIBRATE": 0.50, "EXPLOIT": 0.75, "MASTER": 1.00}

    # Sort levels by threshold ascending for capability determination
    levels_sorted = sorted(competence.items(), key=lambda x: x[1])

    # Walk ancestors (skip self)
    ancestors = walk_ancestors(tree, key)
    if len(ancestors) < 2:
        # Node is root or has no parent — nothing to propagate
        print(json.dumps({
            "source_node": key,
            "ancestors_updated": [],
            "capability_changes": []
        }, indent=2, ensure_ascii=False))
        return

    ancestors_updated = []
    capability_changes = []

    for anc in ancestors[1:]:  # skip self (index 0)
        anc_key = anc["key"]
        anc_node = nodes.get(anc_key)
        if not anc_node:
            break

        children_keys = anc_node.get("children", [])
        if not children_keys:
            continue

        # Compute average confidence from children
        child_confidences = []
        for ck in children_keys:
            if ck in nodes:
                c = nodes[ck].get("confidence")
                if c is not None and isinstance(c, (int, float)):
                    child_confidences.append(c)

        if not child_confidences:
            continue

        new_confidence = round(sum(child_confidences) / len(child_confidences), 4)
        old_confidence = anc_node.get("confidence")
        if old_confidence is not None:
            old_confidence = round(float(old_confidence), 4)

        # Determine new capability level
        old_level = anc_node.get("capability_level", "EXPLORE")
        new_level = "EXPLORE"
        for level_name, threshold in levels_sorted:
            if new_confidence >= threshold:
                new_level = level_name

        capability_changed = old_level != new_level

        # Update node
        anc_node["confidence"] = new_confidence
        anc_node["domain_confidence"] = new_confidence
        if capability_changed:
            anc_node["capability_level"] = new_level
        nodes[anc_key] = anc_node

        ancestors_updated.append({
            "key": anc_key,
            "old_confidence": old_confidence,
            "new_confidence": new_confidence,
            "capability_changed": capability_changed,
        })

        if capability_changed:
            capability_changes.append({
                "key": anc_key,
                "old_level": old_level,
                "new_level": new_level,
            })

        # Stop if confidence didn't change
        if old_confidence is not None and old_confidence == new_confidence:
            break

    tree["nodes"] = nodes
    write_tree(tree)

    result = {
        "source_node": key,
        "ancestors_updated": ancestors_updated,
        "capability_changes": capability_changes,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Memory tree engine for _tree.yaml")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # read
    p_read = subparsers.add_parser("read", help="Read tree data")
    read_group = p_read.add_mutually_exclusive_group(required=True)
    read_group.add_argument("--node", type=str, help="Full node JSON with defaults applied")
    read_group.add_argument("--path", type=str, help="File path string for a node")
    read_group.add_argument("--ancestors", type=str, help="Parent chain array (node -> root)")
    read_group.add_argument("--children", type=str, help="Immediate children as JSON array")
    read_group.add_argument("--leaves", action="store_true", help="All leaf nodes")
    read_group.add_argument("--leaves-under", type=str, help="Leaf descendants of a subtree")
    read_group.add_argument("--stats", action="store_true", help="Node counts by depth")
    read_group.add_argument("--child-path", nargs=2, metavar=("PARENT", "SLUG"),
                            help="Compute file path for new child")
    read_group.add_argument("--validate", action="store_true", help="Check parent-child consistency")
    read_group.add_argument("--decompose-candidates", action="store_true",
                            help="Leaf nodes exceeding decompose_threshold lines")
    read_group.add_argument("--redistribute-candidates", action="store_true",
                            help="Interior nodes with large bodies exceeding decompose_threshold lines")
    read_group.add_argument("--find", type=str,
                            help="Find best-matching node(s) for text query")
    read_group.add_argument("--active-content", type=str, metavar="KEY",
                            help="Return only ## Decision Rules and ## Verified Values sections from node .md")
    read_group.add_argument("--distill-candidates", action="store_true",
                            help="Leaf nodes eligible for DISTILL based on utility thresholds")
    read_group.add_argument("--summary", action="store_true",
                            help="Compact tree overview: keys, summaries, depth, capability, confidence, children keys")
    p_read.add_argument("--threshold", type=int, default=None,
                        help="Line count threshold for --decompose-candidates / --redistribute-candidates (default: 80)")
    p_read.add_argument("--top", type=int, default=3,
                        help="Max results for --find (default: 3)")
    p_read.add_argument("--leaf-only", action="store_true",
                        help="Restrict --find to leaf nodes only")

    # update
    p_update = subparsers.add_parser("update", help="Update tree data")
    update_group = p_update.add_mutually_exclusive_group(required=True)
    update_group.add_argument("--set", nargs=3, metavar=("KEY", "FIELD", "VALUE"),
                              help="Update a single node field")
    update_group.add_argument("--add-child", type=str, metavar="PARENT_KEY",
                              help="Register child node (stdin JSON)")
    update_group.add_argument("--remove-child", nargs=2, metavar=("PARENT", "CHILD"),
                              help="Deregister child and remove node")
    update_group.add_argument("--increment", nargs=2, metavar=("KEY", "FIELD"),
                              help="Atomic increment of numeric field")
    update_group.add_argument("--batch", action="store_true",
                              help="Batch update: read JSON operations from stdin")
    update_group.add_argument("--propagate", type=str, metavar="KEY",
                              help="Propagate confidence up parent chain from node")

    args = parser.parse_args()

    dispatch = {
        "read": cmd_read,
        "update": cmd_update,
    }

    try:
        dispatch[args.command](args)
    except SystemExit:
        raise
    except Exception as e:
        print("Error: " + str(e), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
