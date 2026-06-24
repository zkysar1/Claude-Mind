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
import random
import sys
import time
from datetime import date, datetime
from pathlib import Path

# : force utf-8 on stdin/stdout/stderr (covers Windows cp1252 fallback
# when callers bypass the _platform.sh PYTHONIOENCODING=utf-8 shim).
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

from _paths import PROJECT_ROOT, WORLD_DIR, META_DIR, CONFIG_DIR, resolve_file_path

REPO_ROOT = str(PROJECT_ROOT)
TREE_PATH = str(WORLD_DIR / "knowledge" / "tree" / "_tree.yaml")
TREE_CONFIG = str(CONFIG_DIR / "tree.yaml")


# Override key convention: "<config-file-stem>.<in-file-dotted-path>".
# For tree.yaml's config.decompose_threshold, the override key is
# `tree.config.decompose_threshold`. Each config file's reader filters for
# its own prefix; all others are silently ignored. See
# world/conventions/capability-routing.md and rb-302.
_OVERRIDE_FILE_PREFIX = "tree."


def _merged_config():
    """Read core/config/tree.yaml and overlay any `tree.*`-prefixed entries
    from meta/config-overrides.yaml. Resolution rule:
        "Skills resolve: meta/config-overrides.yaml[<file>.<path>] ?? core/config/<file>[<path>]"
    Entries for other configs (e.g. `aspirations.*`) are skipped. Entry values
    may be raw scalars OR dicts with a `value` field (the shape meta-set.sh
    writes — value/previous/changed_date/reason).
    """
    with open(TREE_CONFIG, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    overrides_path = META_DIR / "config-overrides.yaml"
    if not overrides_path.exists():
        return cfg
    with open(overrides_path, "r", encoding="utf-8") as f:
        override_doc = yaml.safe_load(f)
    overrides = override_doc.get("overrides", {})
    for dotted_key, entry in overrides.items():
        # File-prefix filter: only keys targeting tree.yaml apply here.
        # This is the file-scope enforcement — other configs' entries are
        # each filtered by their own reader, never by this one.
        if not dotted_key.startswith(_OVERRIDE_FILE_PREFIX):
            continue
        in_file_path = dotted_key[len(_OVERRIDE_FILE_PREFIX):]
        # Dict entries MUST have `value` — matches meta-set.sh schema
        # (value/previous/changed_date/reason). Missing `value` on a dict
        # entry is a corrupt write; raise at source, don't silently propagate.
        val = entry["value"] if isinstance(entry, dict) else entry
        parts = in_file_path.split(".")
        target = cfg
        for p in parts[:-1]:
            if not isinstance(target, dict) or p not in target:
                target = None
                break
            target = target[p]
        # CRITICAL: the `parts[-1] in target` check stops typos like
        # `tree.config.decompose_treshold` from silently creating bogus
        # parameters in the merged cfg. Do not relax it.
        if target is not None and isinstance(target, dict) and parts[-1] in target:
            target[parts[-1]] = val
    return cfg


def _config_threshold():
    """Read decompose_threshold. Source: tree.yaml + meta/config-overrides.yaml overlay.

    Missing file or key still raises — a drifted fallback (50 when config says 80)
    would silently change tree decomposition behavior. rb-215, rb-275.
    """
    return _merged_config()["config"]["decompose_threshold"]


def _config_d_max():
    """Read D_max. Source: tree.yaml + meta/config-overrides.yaml overlay."""
    return _merged_config()["config"]["D_max"]


def _config_k_max():
    """Read K_max — max children per node (the Zhong K=4 fan-out cap).

    Source: tree.yaml + meta/config-overrides.yaml overlay. Raises on missing
    key (rb-215/rb-275 anti-drift — a silent fallback would change the
    locality contract without anyone noticing)."""
    return _merged_config()["config"]["K_max"]


def _config_d_retrieval():
    """Read D_retrieval — max depth BELOW a retrieval root (the Zhong D=4 cap).

    Distinct from D_max (the absolute structural ceiling, ~20). The K=D=4
    retrieval-locality contract (g-306-13) constrains depth *below a retrieval
    entry point*, not absolute tree depth. Source: tree.yaml +
    meta/config-overrides.yaml overlay."""
    return _merged_config()["config"]["D_retrieval"]


def _config_leaf_cap():
    """Per-retrieval-subtree leaf cap, derived from K_max and D_retrieval.

    Zhong et al. (PRL 134:237402, 2025): random-tree recall saturates at
    K^(D-1) leaves under a retrieval entry point. With K_max=4, D_retrieval=4
    this is 4^3 = 64. DERIVED rather than a 4th config knob so it can never
    drift from K_max / D_retrieval (single source of truth — rb-215). An
    explicit `config.leaf_cap` overrides the derivation when present (escape
    hatch for non-default tunings)."""
    cfg = _merged_config()["config"]
    explicit = cfg.get("leaf_cap")
    if explicit is not None:
        return explicit
    return cfg["K_max"] ** (cfg["D_retrieval"] - 1)


# DO NOT INLINE THIS FORMULA. It must match utilization-feedback.py exactly.
# Inferred hits count half — manual feedback keeps authoritative weight.
# Changing this in one place without the other silently decouples the signal.
_UTILITY_RATIO_FIELDS = (
    "times_helpful",
    "times_inferred_helpful",
    "retrieval_count",
    "times_noise",
)


def _recompute_utility_ratio(node):
    rc = node.get("retrieval_count", 0)
    th = node.get("times_helpful", 0)
    tih = node.get("times_inferred_helpful", 0)
    node["utility_ratio"] = round((th + 0.5 * tih) / max(rc, 1), 4)
    # C.1 parallel field — see utilization-stats.py module docstring for the
    # canonical formula. Tree nodes don't currently track times_active / cited
    # (those are rb/guardrail-specific signals), so those terms degrade to 0
    # and v2 differs from v1 only in the +1 in the denominator (smoother for
    # newly-seeded nodes). Field stays present for consumer compatibility.
    ta = node.get("times_active", 0)
    tc = node.get("times_cited", 0)
    node["utility_ratio_v2"] = round(
        (th + 0.5 * tih + 0.25 * ta + 1.0 * tc) / max(rc + 1, 1), 4
    )


# ---------------------------------------------------------------------------
# Dedup integration (pre-create duplicate gate)
# ---------------------------------------------------------------------------

_DEDUP_MODULE = None

def _get_dedup_module():
    # Hyphenated filename (tree-dedup-check.py) blocks normal import; must use importlib.
    global _DEDUP_MODULE
    if _DEDUP_MODULE is None:
        import importlib.util
        dedup_path = Path(__file__).parent / "tree-dedup-check.py"
        spec = importlib.util.spec_from_file_location("tree_dedup_check_dyn", dedup_path)
        _DEDUP_MODULE = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_DEDUP_MODULE)
    return _DEDUP_MODULE


def _load_child_limits():
    """Read child_limits: section from core/config/tree.yaml. Cached per process."""
    global _CHILD_LIMITS_CACHE
    try:
        return _CHILD_LIMITS_CACHE
    except NameError:
        pass
    # Default flipped from "warn" to "block" in  — cap violations now
    # hard-fail unless the caller provides --accept-overflow "<justification>",
    # which writes a tree-debt entry and allows the write. Set mode: "warn" in
    # tree.yaml to restore the pre- observability-only behavior.
    cfg = {"mode": "block", "EXPLORE": 2, "CALIBRATE": 4, "EXPLOIT": 8, "MASTER": 8}
    try:
        with open(TREE_CONFIG, "r", encoding="utf-8") as f:
            file_cfg = yaml.safe_load(f) or {}
        cfg.update(file_cfg.get("child_limits", {}) or {})
    except (OSError, yaml.YAMLError):
        pass
    globals()["_CHILD_LIMITS_CACHE"] = cfg
    return cfg


def _write_tree_debt_entry(entry):
    """Append a tree-debt record to world/tree-debt.jsonl. Fail-soft: if the
    world path can't be resolved or the write fails, log and continue so a
    broken debt log never blocks a --accept-overflow acknowledgement. See
    g-115-30 for the introduction of this store; reflect-maintain will consume
    it once its tree-debt loader lands (see spawned follow-up goal)."""
    try:
        if WORLD_DIR is None:
            print("[tree-debt] WORLD_DIR unset — skipping debt entry write",
                  file=sys.stderr)
            return
        debt_path = os.path.join(str(WORLD_DIR), "tree-debt.jsonl")
        with open(debt_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        print(f"[tree-debt] debt write failed ({e}) — accept-overflow honored anyway",
              file=sys.stderr)


def _enforce_child_limit(parent_key, parent_node, no_limit=False,
                         accept_overflow=None, context="add-child"):
    """Phase 3 of cognitive-core curation plan. Cap children per parent by the
    parent's capability_level so early-stage parents are forced to deepen
    existing children before widening. As of g-115-30 the default mode is
    `block` — cap violations exit 6 unless the caller provides
    accept_overflow="<justification>", which writes a tree-debt entry and
    allows the write. Set mode: "warn" in tree.yaml to restore the
    pre-g-115-30 observability-only behavior.

    Bypass with no_limit=True for trusted batch ops / maintenance (skips the
    gate entirely — no warning, no debt entry)."""
    if no_limit:
        return
    cfg = _load_child_limits()
    mode = str(cfg.get("mode", "block")).lower()
    if mode == "off":
        return
    level = (parent_node.get("capability_level") or "CALIBRATE").upper()
    limit = cfg.get(level)
    if not isinstance(limit, int) or limit <= 0:
        return
    current = parent_node.get("child_count")
    if not isinstance(current, int):
        current = len(parent_node.get("children", []) or [])
    if current < limit:
        return
    msg = {
        "context": context,
        "parent": parent_key,
        "capability_level": level,
        "limit": limit,
        "current": current,
        "recommendation": "MERGE or DISTILL an existing child first, or update-in-place",
    }
    # Justified overflow: write debt entry, log, and allow the write.
    if accept_overflow:
        debt_entry = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "parent": parent_key,
            "capability_level": level,
            "limit": limit,
            "current": current,
            "context": context,
            "justification": str(accept_overflow),
            "source_agent": os.environ.get("MIND_AGENT", "") or "",
        }
        _write_tree_debt_entry(debt_entry)
        print(f"[child-limit] ACCEPT-OVERFLOW: parent '{parent_key}' at cap "
              f"({level}: {current}/{limit}) — debt entry written; "
              f"justification: {accept_overflow}",
              file=sys.stderr)
        return
    if mode == "block":
        print(json.dumps({"child_limit_reject": msg}, indent=2, ensure_ascii=False), file=sys.stderr)
        sys.exit(6)
    # warn mode (legacy): log + allow.
    print(f"[child-limit] WARN: parent '{parent_key}' at cap "
          f"({level}: {current}/{limit}) — {msg['recommendation']}",
          file=sys.stderr)


def _enforce_dedup(parent_key, child_key, summary, tree_dict, no_dedup=False, context="add-child"):
    """Run dedup gate; exits process with dedup's exit_code on reject.

    Fail-open: a dedup crash must never block the write path. The gate is a
    curation helper, not a correctness check — the tree's bigger problem is
    breadth-without-depth, so a broken gate defaults to the pre-gate behaviour
    (accept) with a logged warning so the regression is visible.
    Honors the --no-dedup bypass.
    """
    if no_dedup:
        return
    try:
        result = _get_dedup_module().check_dedup(parent_key, child_key, summary or "", tree=tree_dict)
    except Exception as e:
        print(f"[dedup] disabled this call ({e})", file=sys.stderr)
        return
    action = result.get("action")
    if action == "reject":
        print(json.dumps({"dedup_reject": result, "context": context}, indent=2, ensure_ascii=False), file=sys.stderr)
        sys.exit(result.get("exit_code", 3))
    if action == "error":
        print(f"[dedup] internal error, accepting: {result}", file=sys.stderr)


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


def safe_read_tree():
    """Library variant of read_tree: returns None on missing/invalid tree
    instead of sys.exit. For fail-open analyzers (l1-skew-check,
    l1-emergence-detector) where missing tree is "no signal," not a CLI
    error. CLI commands keep using read_tree() for the loud-fail behavior."""
    try:
        if not os.path.exists(TREE_PATH):
            return None
        with open(TREE_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict) or "nodes" not in data:
            return None
        return data
    except (OSError, yaml.YAMLError):
        return None


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
        max_retries = 5
        for attempt in range(max_retries):
            try:
                tmp = str(path) + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    yaml.dump(data, f, default_flow_style=None, sort_keys=False,
                              allow_unicode=True, width=200)
                os.replace(tmp, TREE_PATH)
                break
            except (PermissionError, OSError) as e:
                if attempt == max_retries - 1:
                    raise
                wait = 0.05 * (2 ** attempt) + random.uniform(0, 0.1)
                print("write_tree retry {}/{}: {} (waiting {:.2f}s)".format(
                    attempt + 1, max_retries, e, wait), file=sys.stderr)
                time.sleep(wait)
        if base_dir:
            append_changelog(base_dir, agent, path, "edit")
    finally:
        release_lock(lock_path)


# ---------------------------------------------------------------------------
# Helpers: node operations
# ---------------------------------------------------------------------------

def apply_defaults(node):
    """Fill missing fields with defaults. Returns a new dict (does not mutate).

    Read-safe: never sets time-dependent fields (`last_updated`,
    `last_retrieved`). Those are persisted only by write paths so reading a
    legacy node twice doesn't return two different "today" values. Backfill
    of those fields is a separate one-shot concern (see
    backfill-tree-node-fields.py).

    `capability_level` defaults to "EXPLORE" — the lowest level, contributes
    zero capability bonus to scoring. Safe presentation-layer default;
    legacy nodes stay missing on disk.
    """
    out = dict(node)
    if "article_count" not in out:
        out["article_count"] = 0
    if "growth_state" not in out:
        out["growth_state"] = "stable"
    children = out.get("children", [])
    if "node_type" not in out:
        out["node_type"] = "interior" if children else "leaf"
    if "capability_level" not in out:
        out["capability_level"] = "EXPLORE"
    # Utility tracking fields
    if "retrieval_count" not in out:
        out["retrieval_count"] = 0
    if "times_helpful" not in out:
        out["times_helpful"] = 0
    if "times_noise" not in out:
        out["times_noise"] = 0
    if "utility_ratio" not in out:
        out["utility_ratio"] = 0.0
    # poignancy (, BRD Gap 1a): optional 1-10 importance rating, null
    # when unset. Null-safe — retrieve scoring treats None as neutral. Read-safe
    # default (no disk write): legacy nodes present poignancy=None in-memory
    # without rewriting the file, matching capability_level's defaulting above.
    if "poignancy" not in out:
        out["poignancy"] = None
    # last_relevant_at (, D2 cluster-archival): optional ISO date marking
    # demonstrated relevance, null when unset. Read-safe default (no disk write):
    # null is treated as `last_updated` by the archival sweep's effective_relevance
    # (back-filled to a concrete date only by the write path — tree-archive.sh
    # scan/apply or `tree-update.sh --set last_relevant_at`). Single-writer: only
    # the tree-archival lane writes it. See d2-cluster-archival-design.md §2/§4,
    # guard-155/rb-254 (single-writer tree-maintenance state).
    if "last_relevant_at" not in out:
        out["last_relevant_at"] = None
    # valid_from / valid_to (, BRD Gap 5): optional bi-temporal validity
    # interval on a tree-node front matter. Read-safe null defaults (no disk
    # write), matching poignancy / last_relevant_at above — legacy nodes present
    # valid_from=valid_to=None in-memory without rewriting the file. The
    # falsification / migration write paths set concrete values. Mirror of the
    # daemon _apply_defaults in mind_api/src/world/tree_write.py (byte-compat
    # parity test asserts identical ordered keys).
    if "valid_from" not in out:
        out["valid_from"] = None
    if "valid_to" not in out:
        out["valid_to"] = None
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


def normalize_virtual_path(raw_path):
    """Normalize a file path to canonical virtual form (world/knowledge/tree/...md).

    Handles backslashes, double slashes, and absolute paths. Ensures all paths
    stored in _tree.yaml use the virtual world/ or meta/ prefix with forward slashes.
    """
    if not raw_path:
        return raw_path
    # Forward slashes only
    path = raw_path.replace("\\", "/")
    # Collapse repeated slashes
    while "//" in path:
        path = path.replace("//", "/")
    # Strip trailing slash
    path = path.rstrip("/")
    # Strip absolute WORLD_DIR prefix → world/
    world_str = str(WORLD_DIR).replace("\\", "/").rstrip("/")
    if path.startswith(world_str + "/"):
        path = "world/" + path[len(world_str) + 1:]
    elif path.lower().startswith(world_str.lower() + "/"):
        # Case-insensitive match (Windows drive letters)
        path = "world/" + path[len(world_str) + 1:]
    # Strip absolute META_DIR prefix → meta/
    meta_str = str(META_DIR).replace("\\", "/").rstrip("/")
    if path.startswith(meta_str + "/"):
        path = "meta/" + path[len(meta_str) + 1:]
    elif path.lower().startswith(meta_str.lower() + "/"):
        path = "meta/" + path[len(meta_str) + 1:]
    # Bare knowledge/ path without world/ prefix
    if path.startswith("knowledge/"):
        path = "world/" + path
    # Bare tree-subcategory path (e.g. "intelligence/foo.md") — prepend
    # world/knowledge/tree/. This is THE canonicalization point for tree
    # node paths; resolve_file_path in _paths.py has no symmetric fallback
    # and will route any non-prefixed path through PROJECT_ROOT.
    if path.endswith(".md") and not (
        path.startswith("world/") or path.startswith("meta/")
    ):
        path = "world/knowledge/tree/" + path
    return path


def compute_child_path(parent_file, child_slug):
    """Compute the file path for a new child node.
    Strip .md from parent, use as directory, append {slug}.md.
    Uses string manipulation to preserve forward slashes."""
    if parent_file.endswith(".md"):
        parent_dir = parent_file[:-3]
    else:
        parent_dir = parent_file
    return normalize_virtual_path(parent_dir + "/" + child_slug + ".md")


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
    abs_path = str(resolve_file_path(file_path))
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


def _analyze_node_body(text):
    """Return (line_count, est_tokens, refresh_sections) for a node .md body.

    est_tokens uses the ~4-chars/token markdown heuristic that underlies the
    Read tool's ~25k-token cap. refresh_sections counts the dated append-grown
    sweep headings (markdown headings whose text contains "refresh" or
    "verified values", case-insensitive) that are the structural signature of a
    recurring-sweep node — the shape the rb-2085 distill procedure targets.
    Pure (string in, tuple out) so crit3 in get_distill_candidates is
    unit-testable without filesystem / path-resolution fixtures. (g-115-1570)
    """
    line_count = 0
    char_count = 0
    refresh_sections = 0
    for line in text.splitlines(keepends=True):
        line_count += 1
        char_count += len(line)
        s = line.lstrip()
        if s.startswith("#"):
            low = s.lower()
            if "refresh" in low or "verified values" in low:
                refresh_sections += 1
    return line_count, char_count // 4, refresh_sections


def get_distill_candidates(tree, include_skipped=False):
    """Return leaf nodes eligible for DISTILL based on utility thresholds.
    Reads thresholds from core/config/tree.yaml pruning section.

    When include_skipped=True, returns {candidates, skipped} where skipped is a
    list of {node_key, skip_reason}. Skip-reason enum:
      has_children, insufficient_retrievals, utility_above_threshold.
    (file_not_found / read_error are NOT skip reasons here — a missing file
    just yields line_count=0, which feeds into crit2.)
    """
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
    # crit3 (0): proactive oversized append-grown sweep node.
    token_cap = pruning.get("distill_token_cap", 25000)            # Read tool ~25k-token cap
    token_ratio = pruning.get("distill_token_ratio", 0.8)          # fire at this fraction of the cap
    token_trigger = token_ratio * token_cap
    refresh_min = pruning.get("distill_refresh_min_sections", 3)   # append-grown = 3+ dated refresh sections

    nodes = tree.get("nodes", {})
    candidates = []
    skipped = []
    for key, node in nodes.items():
        if node.get("children"):
            if include_skipped:
                skipped.append({"node_key": key, "skip_reason": "has_children"})
            continue  # skip interior nodes
        rc = node.get("retrieval_count", 0)
        ur = node.get("utility_ratio", 0.0)
        th = node.get("times_helpful", 0)
        tn = node.get("times_noise", 0)
        has_feedback = (th + tn) >= 1
        # Criterion 1: low utility after sufficient retrievals — but only when
        # we have at least one helpful/noise feedback signal. Before ,
        # crit1 was rc-only; utilization-feedback wasn't firing on tree
        # retrievals (--goal absent), so 108/109 candidates had ur=0.0 and th=tn=0
        # purely from the default. Require real feedback to avoid false positives.
        crit1 = rc >= min_ret and has_feedback and ur < threshold
        # Criterion 2: large node with mediocre payoff
        file_path = node.get("file", "")
        abs_path = str(resolve_file_path(file_path)) if file_path else ""
        line_count = 0
        est_tokens = 0
        refresh_sections = 0
        if abs_path and os.path.exists(abs_path):
            with open(abs_path, "r", encoding="utf-8") as f:
                line_count, est_tokens, refresh_sections = _analyze_node_body(f.read())
        # crit2 requires feedback too — same root cause as crit1 (zero-feedback
        # nodes have ur=0.0 by default and mis-fire as "mediocre"). .
        crit2 = line_count > line_threshold and ur < line_util_threshold and rc >= min_ret and has_feedback
        # Criterion 3 (0): proactive oversized append-grown sweep node.
        # Fires BEFORE the node trips the Read ~25k-token cap that forces
        # read-before-edit to re-read it on every recurring sweep (rb-2085's
        # reactive-distill trigger). STRUCTURAL — independent of
        # utility_ratio/feedback (crit1/crit2): the problem is the node is too
        # large to Read at all, not that its payoff is low (these sweep nodes
        # are typically HIGH-utility, so crit2's ur<0.5 gate never catches them).
        # Both conditions required (near-cap size AND append-grown shape) so the
        # false-positive rate stays near zero. Action reuses the rb-2085 distill
        # procedure (archive verbatim + keep newest-N + dated rollup).
        crit3 = est_tokens >= token_trigger and refresh_sections >= refresh_min
        if crit1 or crit2 or crit3:
            trigger = ("oversized_append_grown" if crit3
                       else "low_utility" if crit1 else "large_mediocre")
            candidates.append({
                "key": key,
                "utility_ratio": ur,
                "retrieval_count": rc,
                "times_helpful": th,
                "times_noise": tn,
                "line_count": line_count,
                "est_tokens": est_tokens,
                "refresh_sections": refresh_sections,
                "file": file_path,
                "trigger": trigger,
            })
        elif include_skipped:
            # Attribute the skip to the most specific gate that failed.
            if rc < min_ret:
                skipped.append({"node_key": key, "skip_reason": "insufficient_retrievals"})
            elif not has_feedback:
                skipped.append({"node_key": key, "skip_reason": "no_feedback"})
            else:
                skipped.append({"node_key": key, "skip_reason": "utility_above_threshold"})
    # Surface proactive oversized-append-grown candidates FIRST: they block Read
    # entirely (rb-2085), so they must win the max_distill_per_invocation budget
    # over low-utility / large-mediocre leaves. Within each tier, lowest utility
    # first (preserves the prior ordering for crit1/crit2 candidates). (0)
    candidates.sort(key=lambda x: (0 if x["trigger"] == "oversized_append_grown" else 1, x["utility_ratio"]))
    if include_skipped:
        return {"candidates": candidates, "skipped": skipped}
    return candidates


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


def compute_stats(tree, by_l1=False):
    """Compute tree statistics.

    by_l1 (S1): when True, includes a `by_l1` field with per-L1 structural
    mass, retrieval volume, utility aggregates, mean confidence, and
    capability-level distribution. Off by default to keep the existing
    stats payload stable for legacy callers.
    """
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

    result = {
        "total_nodes": len(nodes),
        "by_depth": by_depth,
        "interior_count": interior_count,
        "leaf_count": leaf_count,
    }
    if by_l1:
        result["by_l1"] = _compute_by_l1_stats(nodes)
    return result


_CANONICAL_END_SECTIONS = {"verified values", "decision rules"}


def _qualifies_for_decomposition(abs_path):
    """Semantic-structure gate: is this file actually decompose-shaped?

    Tightened heuristic (g-240-35): 75% of line-count-only candidates were
    false positives (flat prose with 0 sections, priority-bucket roadmaps,
    nodes where half the sections are canonical end-sections). A decomposable
    node is one with multiple substantive sub-concepts, each weighty enough
    to become its own child node.

    Requires ALL of:
      1. >=4 non-end ## sections (not ### or #)
      2. Average non-end-section length > 10 lines

    Returns (qualifies, skip_reason | None).
    Skip reasons: insufficient_sections, short_sections_avg.
    """
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        # Caller already handled read errors above; if we reach here with
        # one, fail-open (qualifies) so the candidate is still surfaced.
        return True, None
    # Strip YAML front matter (---\n...\n---)
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            body = text[end + 4:]
    lines = body.splitlines()
    # Collect ## sections with their line offsets (not ### — level 2 only).
    section_starts = [
        (i, ln.lstrip("# ").strip())
        for i, ln in enumerate(lines)
        if ln.startswith("## ") and not ln.startswith("### ")
    ]
    # Filter canonical end-sections (Verified Values / Decision Rules) — these
    # are reference appendices, not decomposable concepts.
    non_end_sections = [
        (i, title) for (i, title) in section_starts
        if title.lower() not in _CANONICAL_END_SECTIONS
    ]
    if len(non_end_sections) < 4:
        return False, "insufficient_sections"
    # Compute average non-end-section length (line span from start to next ##).
    non_end_indices = {i for (i, _) in non_end_sections}
    all_starts = [i for (i, _) in section_starts]
    spans = []
    for i, _ in non_end_sections:
        next_starts = [s for s in all_starts if s > i]
        end_idx = next_starts[0] if next_starts else len(lines)
        spans.append(end_idx - i)
    avg_span = sum(spans) / len(spans) if spans else 0
    if avg_span <= 10:
        return False, "short_sections_avg"
    return True, None


def _subtree_leaf_counts(nodes):
    """Map every node key -> number of leaf descendants in its subtree.

    A leaf (no children) counts as 1 (itself); an interior node's count is the
    sum of its children's counts. Memoized post-order over the node dict.
    Cycle- and dangling-child-safe: a child key absent from `nodes` contributes
    0, and a back-edge in a malformed cycle resolves to 0 without crashing
    (validate_tree owns flagging structural corruption — this helper must never
    raise on a bad tree). Used by the K=D=4 retrieval-locality checks
    (g-306-13): a node whose subtree leaf count exceeds the leaf cap is a
    retrieval entry point that violates the Zhong <=64-leaves locality bound.
    """
    counts = {}
    visiting = set()

    def _count(key):
        if key in counts:
            return counts[key]
        node = nodes.get(key)
        if node is None:
            return 0
        children = node.get("children", [])
        if not children:
            counts[key] = 1
            return 1
        if key in visiting:
            return 0  # cycle back-edge — contribute 0, let the outer frame finish
        visiting.add(key)
        total = 0
        for child in children:
            total += _count(child)
        visiting.discard(key)
        counts[key] = total
        return total

    for k in nodes:
        _count(k)
    return counts


def get_decompose_candidates(tree, include_skipped=False):
    """Return non-root nodes whose retrieval subtree exceeds the leaf cap.

    STRUCTURAL trigger (g-306-13): a node is a decompose candidate when the
    number of leaves under it exceeds K_max^(D_retrieval-1) (the Zhong <=64
    retrieval-locality bound) — NOT when its .md file exceeds a line count. The
    line-count trigger (decompose_threshold) is retired here per g-306-13
    outcome 3: existing tree depth is line-count-decompose residue (board
    decision msg-20260619-075228-bravo-086). Root (depth 0) is excluded — it
    holds every leaf and is the routing index, not a retrieval entry point.

    Recommended action: decompose — add intermediate grouping so any retrieval
    entry point sees <=leaf_cap leaves. Existing depth-5..8 nodes are
    GRANDFATHERED; the maintain path regroups opportunistically, never
    force-migrates (see .claude/skills/tree/SKILL.md maintain section).

    When include_skipped=True, returns {candidates, skipped} where skipped is a
    list of {node_key, skip_reason}. Skip-reason enum: is_root, within_leaf_cap.
    """
    leaf_cap = _config_leaf_cap()
    nodes = tree.get("nodes", {})
    leaf_counts = _subtree_leaf_counts(nodes)
    candidates = []
    skipped = []
    for key, node in nodes.items():
        depth = node.get("depth", 0)
        if depth < 1:
            if include_skipped:
                skipped.append({"node_key": key, "skip_reason": "is_root"})
            continue
        subtree_leaves = leaf_counts.get(key, 0)
        if subtree_leaves > leaf_cap:
            candidates.append({
                "key": key,
                "file": node.get("file", ""),
                "depth": depth,
                "child_count": len(node.get("children", [])),
                "subtree_leaves": subtree_leaves,
                "leaf_cap": leaf_cap,
                "reason": "leaf_overflow",
                "recommended_action": "decompose",
                "growth_state": node.get("growth_state", "stable"),
            })
        elif include_skipped:
            skipped.append({"node_key": key, "skip_reason": "within_leaf_cap"})
    candidates.sort(key=lambda c: c["subtree_leaves"], reverse=True)
    if include_skipped:
        return {"candidates": candidates, "skipped": skipped}
    return candidates


def get_redistribute_candidates(tree, include_skipped=False):
    """Return interior nodes with more than K_max children (regroup candidates).

    STRUCTURAL trigger (g-306-13): a node is a regroup candidate when it has
    more than K_max children (the Zhong K=4 fan-out cap) — NOT when its .md
    body exceeds a line count. The line-count body trigger is retired here per
    g-306-13 outcome 3.

    Recommended action: regroup — group the >K_max children under <=K_max
    intermediate category nodes (balanced regrouping on overflow). Existing
    depth-5..8 nodes are GRANDFATHERED; the maintain path regroups
    opportunistically (see .claude/skills/tree/SKILL.md maintain section).

    When include_skipped=True, returns {candidates, skipped} where skipped is a
    list of {node_key, skip_reason}. Skip-reason enum: no_children, within_k_max.
    """
    k_max = _config_k_max()
    nodes = tree.get("nodes", {})
    candidates = []
    skipped = []
    for key, node in nodes.items():
        children = node.get("children", [])
        if not children:
            if include_skipped:
                skipped.append({"node_key": key, "skip_reason": "no_children"})
            continue  # leaves handled by get_decompose_candidates
        if len(children) > k_max:
            candidates.append({
                "key": key,
                "file": node.get("file", ""),
                "depth": node.get("depth", 0),
                "child_count": len(children),
                "children": children,
                "k_max": k_max,
                "reason": "k_overflow",
                "recommended_action": "regroup",
                "growth_state": node.get("growth_state", "stable"),
            })
        elif include_skipped:
            skipped.append({"node_key": key, "skip_reason": "within_k_max"})
    candidates.sort(key=lambda c: c["child_count"], reverse=True)
    if include_skipped:
        return {"candidates": candidates, "skipped": skipped}
    return candidates


def _path_exists_longsafe(abs_path):
    """os.path.exists that survives Windows MAX_PATH (260-char limit).

    On Windows, paths >= 260 chars return False from os.path.exists even when
    the file exists. The \\\\?\\ prefix bypasses the legacy limit. We try the
    plain path first (fast path), then the long-path form on Windows.
    """
    if os.path.exists(abs_path):
        return True
    if os.name == "nt" and not abs_path.startswith("\\\\?\\"):
        return os.path.exists("\\\\?\\" + abs_path)
    return False


def _canonical_path_key(p):
    """Return a canonical comparison key for a filesystem path.

    Strips the Windows `\\\\?\\` extended-length prefix and lowercases on
    Windows (where NTFS is case-insensitive by default). Used to compare paths
    from different sources — e.g. `_tree.yaml` node files (which may be
    `_longpath_safe`-wrapped above 260 chars) vs `os.walk` output (which is
    not). Without this, comparison fails at exactly MAX_PATH even though both
    sides reference the same file. See validate_tree orphan-check.

    NOTE: the lowercase assumes NTFS default (case-insensitive). If the tree
    ever lands on a case-sensitive Windows directory (per-dir flag enabled) or
    a case-sensitive bind mount, remove the lowercase branch — otherwise
    Foo.md and foo.md will incorrectly compare equal and mask real orphans.
    """
    s = os.path.normpath(str(p))
    if os.name == "nt" and s.startswith("\\\\?\\"):
        s = s[4:]
    if os.name == "nt":
        s = s.lower()
    return s


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

        # Check node_type consistency (6): a stored node_type must
        # match the canonical derivation (interior if children else leaf).
        # apply_defaults DERIVES node_type when ABSENT but does NOT mask a
        # PRESENT-but-wrong value on reads, so validate is the right surface
        # for the present-but-wrong case. Symmetric to the child_count ERROR
        # above — both assert that a stored field matches the actual node
        # structure. Legacy 'branch'/'fact' values (24 normalized 2026-06-16)
        # surface here as wrong-value errors if they ever reappear.
        node_type = node.get("node_type")
        if node_type is not None:
            expected_type = "interior" if children else "leaf"
            if node_type != expected_type:
                errors.append("Node '{}' has node_type='{}' but {} children listed (expected '{}')".format(
                    key, node_type, len(children), expected_type))

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

    # --- Physical-logical sync checks ---

    # Check physical file existence for each node
    for key, node in nodes.items():
        file_path = node.get("file")
        if not file_path:
            continue  # root node has file: null
        abs_path = str(resolve_file_path(file_path))
        # Tolerate legacy storage where 'file' lacks the 'world/' prefix: also
        # try WORLD_DIR-rooted variants before reporting missing. This prevents
        # spurious warnings on files that exist but have stale path prefixes in
        # the YAML. The canonical fix is to repair the YAML entry, but the
        # validator should not double-report during the transition window.
        if not _path_exists_longsafe(abs_path):
            if not file_path.startswith(("world/", "meta/")):
                alt1 = str(WORLD_DIR / file_path)
                alt2 = str(WORLD_DIR / "knowledge" / "tree" / file_path)
                if _path_exists_longsafe(alt1) or _path_exists_longsafe(alt2):
                    continue  # found under a prefix — don't warn
            warnings.append("Node '{}' references file '{}' which does not exist on disk".format(key, file_path))

    # --- Node-body dangling cross-reference check (9) ---
    # The physical-file check above validates each node's OWN `file` field, but
    # not the cross-references embedded in node .md BODIES. When a node is
    # relocated (its `file` moves to a new path), every OTHER node whose body
    # referenced the old path is left with a dangling inbound ref pointing at a
    # path no longer on disk. Those never surfaced here because no check read
    # node bodies — root cause of the  / 7 dangling-ref
    # incidents (a single relocation left 6 inbound refs across 5 files). This
    # block scans each node body for backtick-quoted tree-node references
    # (paths ending in .md whose first segment is a live L1 tree directory) and
    # warns when the target does not exist on disk. rb-8: treat every
    # cross-reference as a positive-state assertion; probe target existence
    # before trusting. Directory-style refs (ending in '/') and non-tree refs
    # (core/, .claude/, world/conventions/, ...) are intentionally out of scope
    # to keep false positives low.
    import re  # local: keeps the whole 9 feature one contiguous block
    tree_root = WORLD_DIR / "knowledge" / "tree"
    # Derive the live set of L1 tree directories from node files (domain-agnostic
    # — never hardcode category names; the tree owns its own taxonomy).
    known_l1 = set()
    for _n in nodes.values():
        _fp = _n.get("file")
        if not _fp:
            continue
        _rel = _fp
        if _rel.startswith("world/knowledge/tree/"):
            _rel = _rel[len("world/knowledge/tree/"):]
        elif _rel.startswith(("world/", "meta/")):
            continue  # non-tree world/meta file
        _seg = _rel.split("/", 1)[0]
        if _seg.endswith(".md"):
            _seg = _seg[:-3]
        if _seg:
            known_l1.add(_seg)
    _md_ref_re = re.compile(r"`([^`\n]+?\.md)`")
    for key, node in nodes.items():
        file_path = node.get("file")
        if not file_path:
            continue
        body_abs = str(resolve_file_path(file_path))
        if not _path_exists_longsafe(body_abs):
            # Mirror the physical-file check's prefix tolerance before giving up.
            if not file_path.startswith(("world/", "meta/")):
                for _alt in (str(WORLD_DIR / file_path),
                             str(WORLD_DIR / "knowledge" / "tree" / file_path)):
                    if _path_exists_longsafe(_alt):
                        body_abs = _alt
                        break
            if not _path_exists_longsafe(body_abs):
                continue  # missing body already warned above
        try:
            with open(body_abs, "r", encoding="utf-8") as _bf:
                body = _bf.read()
        except (OSError, UnicodeDecodeError):
            continue  # unreadable body — fail-open, never abort validation
        seen_refs = set()
        for _m in _md_ref_re.finditer(body):
            ref = _m.group(1).strip()
            if ref in seen_refs:
                continue
            seen_refs.add(ref)
            # Skip illustrative/abbreviated paths: a literal '...' ellipsis (or
            # its unicode form, U+2026) is a prose placeholder for omitted path
            # segments, not a real target (9 live-validation finding).
            if "..." in ref or "…" in ref:
                continue
            if ref.startswith("world/knowledge/tree/"):
                rel = ref[len("world/knowledge/tree/"):]
            elif ref.split("/", 1)[0] in known_l1:
                rel = ref
            else:
                continue  # not a tree-node reference — out of scope
            if not _path_exists_longsafe(str(tree_root / rel)):
                warnings.append(
                    "Node '{}' body references tree node '{}' which does not exist on disk".format(key, ref))

    # Check for orphan .md files (exist on disk but have no _tree.yaml entry)
    tree_dir = str(WORLD_DIR / "knowledge" / "tree")
    if os.path.isdir(tree_dir):
        # Build the reference set using _canonical_path_key so Windows paths
        # at or beyond MAX_PATH (260 chars) — where resolve_file_path returns
        # a `\\\\?\\`-prefixed Path from _longpath_safe, but os.walk returns
        # the raw form — compare equal. See _canonical_path_key.
        referenced_files = set()
        for key, node in nodes.items():
            fp = node.get("file")
            if not fp:
                continue
            referenced_files.add(_canonical_path_key(resolve_file_path(fp)))
            if not fp.startswith(("world/", "meta/")):
                referenced_files.add(_canonical_path_key(WORLD_DIR / fp))
                referenced_files.add(_canonical_path_key(WORLD_DIR / "knowledge" / "tree" / fp))
        for dirpath, dirnames, filenames in os.walk(tree_dir):
            # Skip .archive/ directories — intentionally retained historical snapshots
            # that should not count as orphans. Mutate dirnames in-place to prevent
            # os.walk from descending into them.
            dirnames[:] = [d for d in dirnames if d != ".archive"]
            for fname in filenames:
                if not fname.endswith(".md") or fname.startswith("_"):
                    continue
                full_path = os.path.normpath(os.path.join(dirpath, fname))
                if _canonical_path_key(full_path) not in referenced_files:
                    rel = os.path.relpath(full_path, str(WORLD_DIR)).replace("\\", "/")
                    warnings.append("Orphan file 'world/{}' exists on disk but has no _tree.yaml entry".format(rel))

    # Check directory structure for interior nodes
    for key, node in nodes.items():
        if not node.get("children"):
            continue  # leaf, no directory expected
        file_path = node.get("file")
        if not file_path:
            continue
        abs_path = str(resolve_file_path(file_path))
        expected_dir = abs_path[:-3] if abs_path.endswith(".md") else abs_path
        if not os.path.isdir(expected_dir):
            virtual_dir = file_path[:-3] if file_path.endswith(".md") else file_path
            warnings.append("Interior node '{}' expected directory '{}/' but it does not exist".format(key, virtual_dir))

    # --- K=D=4 retrieval-locality advisory () ---
    # Per Zhong et al. (PRL 134:237402, 2025): random-tree recall is bounded by
    # K=4 children, D=4 retrieval depth, saturating at K^(D-1)=64 leaves under
    # any retrieval entry point. This block is ADVISORY (report-mode-first per
    # the  HYBRID decision, board msg-20260619-075228-bravo-086): it
    # emits violation counts + a decompose/regroup SIGNAL in a `locality` key
    # and appends only SUMMARY warnings (never errors) — so `valid` stays true
    # and `tree --validate` exits 0 (never blocks writes). Existing depth-5..8
    # nodes are GRANDFATHERED: the D count is reported for visibility, not as an
    # action item; only NEW depth/fan-out growth is constrained, at maintain
    # time via the structural candidate functions.
    try:
        k_max = _config_k_max()
        d_retrieval = _config_d_retrieval()
        leaf_cap = _config_leaf_cap()
    except (KeyError, TypeError):
        # Config missing the locality keys — skip the advisory rather than
        # break structural validation (fail-open; the rest of validate stands).
        k_max = d_retrieval = leaf_cap = None

    locality = None
    if k_max is not None:
        leaf_counts = _subtree_leaf_counts(nodes)
        k_viol = []      # nodes with > k_max children (regroup)
        d_count = 0      # nodes deeper than d_retrieval (grandfathered)
        d_by_depth = {}
        max_depth_seen = 0
        leaf_viol = []   # non-root nodes whose subtree exceeds leaf_cap (decompose)
        for key, node in nodes.items():
            nchild = len(node.get("children", []))
            if nchild > k_max:
                k_viol.append({"key": key, "children": nchild})
            depth = node.get("depth", 0)
            if depth > max_depth_seen:
                max_depth_seen = depth
            if depth > d_retrieval:
                d_count += 1
                d_by_depth[depth] = d_by_depth.get(depth, 0) + 1
            # Root (depth 0) holds every leaf and is the routing index, not a
            # retrieval entry point — exclude it from the leaf-cap signal.
            if depth >= 1 and leaf_counts.get(key, 0) > leaf_cap:
                leaf_viol.append({"key": key, "leaves": leaf_counts[key]})

        k_viol.sort(key=lambda v: v["children"], reverse=True)
        leaf_viol.sort(key=lambda v: v["leaves"], reverse=True)

        if k_viol and leaf_viol:
            signal = "decompose+regroup"
        elif leaf_viol:
            signal = "decompose"
        elif k_viol:
            signal = "regroup"
        else:
            signal = "ok"

        locality = {
            "k_max": k_max,
            "d_retrieval": d_retrieval,
            "leaf_cap": leaf_cap,
            "max_depth": max_depth_seen,
            "k_violations": {"count": len(k_viol), "worst": k_viol[:10]},
            "d_violations": {"count": d_count,
                             "by_depth": {str(k): v for k, v in sorted(d_by_depth.items())},
                             "grandfathered": True},
            "leaf_violations": {"count": len(leaf_viol), "worst": leaf_viol[:10]},
            "signal": signal,
        }
        # Summary warnings (advisory — never errors). One line per violation
        # class that fired, naming the worst offender so the signal is
        # actionable without parsing the full `locality` dict.
        if k_viol:
            warnings.append(
                "K=D=4 locality: {} node(s) exceed {} children (worst: '{}' with {}) — regroup under <={} intermediates".format(
                    len(k_viol), k_max, k_viol[0]["key"], k_viol[0]["children"], k_max))
        if leaf_viol:
            warnings.append(
                "K=D=4 locality: {} retrieval-subtree(s) exceed {} leaves (worst: '{}' with {}) — decompose to restore retrieval locality".format(
                    len(leaf_viol), leaf_cap, leaf_viol[0]["key"], leaf_viol[0]["leaves"]))
        if d_count:
            # Grandfathered — single informational line, no per-node spam.
            warnings.append(
                "K=D=4 locality: {} node(s) deeper than D_retrieval={} (max depth {}) — GRANDFATHERED (advisory; new depth growth constrained at maintain time)".format(
                    d_count, d_retrieval, max_depth_seen))

    return {"valid": len(errors) == 0, "errors": errors, "warnings": warnings, "locality": locality}


# ---------------------------------------------------------------------------
# L1 pick logging (S9) — captures the implicit-taxonomy decision every tree
# write makes. Append-only JSONL at meta/l1-pick-log.jsonl. Consumed by
# l1-skew-check.py and /fresh-eyes-tree. Fail-open: a logging error MUST NOT
# block the tree write that just succeeded.
# ---------------------------------------------------------------------------

def _get_l1_for_node(nodes, key):
    """Walk parent chain to find the L1 (depth==1) ancestor.

    Returns the L1 key. For a node that IS L1, returns itself. Returns None
    if the chain is malformed or breaks before reaching depth==1.
    """
    visited = set()
    current = key
    while current is not None and current not in visited:
        visited.add(current)
        node = nodes.get(current)
        if not node:
            return None
        depth = node.get("depth")
        if depth == 1:
            return current
        if depth == 0:
            return None
        current = node.get("parent")
    return None


def _append_l1_pick_log(target_node, l1, decision_type, source, reason):
    """Append one L1 pick log entry. Fail-open."""
    try:
        log_path = META_DIR / "l1-pick-log.jsonl"
        agent = os.environ.get("MIND_AGENT", "").strip() or None
        sid = os.environ.get("MIND_SID", "").strip() or None
        entry = {
            "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "agent": agent,
            "session_id": sid,
            "target_node": target_node,
            "l1": l1,
            "decision_type": decision_type,
            "source": source,
            "reason": reason,
        }
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(log_path), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print("[l1-pick-log] WARN: " + str(e), file=sys.stderr)


def _log_l1_pick_for_key(key, decision_type, source=None, reason=None):
    """Resolve L1 for a freshly-written node and append a pick log entry.

    Reads the tree fresh (outside any lock). Fail-open — never raises.
    Used by cmd_add_child, cmd_batch (per add-child op), and cmd_reparent.
    """
    try:
        tree = read_tree()
        nodes = tree.get("nodes", {}) if isinstance(tree, dict) else {}
        l1 = _get_l1_for_node(nodes, key)
        if l1 is None:
            l1 = "_orphan"
        _append_l1_pick_log(key, l1, decision_type, source, reason)
    except Exception as e:
        print("[l1-pick-log] WARN: " + str(e), file=sys.stderr)


def _compute_by_l1_stats(nodes):
    """Per-L1 distribution breakdown (S1).

    Each L1 = a depth==1 node. Every descendant (including the L1 itself)
    contributes to its L1's bucket. Root (depth==0) is excluded. Nodes whose
    ancestor walk doesn't reach an L1 are bucketed under `_orphan`.

    Per-L1 metrics:
      total_nodes, leaf_count, interior_count: structural mass
      total_retrieval_count: aggregate retrieval volume from `retrieval_count`
      total_times_helpful, total_times_noise: utility signal aggregates
      mean_confidence: mean of nodes with numeric confidence (None if zero)
      capability_mass: dict of capability_level → node count (with "unset")
    """
    l1_keys = [k for k, n in nodes.items() if n.get("depth") == 1]

    def _new_bucket():
        return {
            "total_nodes": 0,
            "leaf_count": 0,
            "interior_count": 0,
            "total_retrieval_count": 0,
            "total_times_helpful": 0,
            "total_times_noise": 0,
            "confidence_sum": 0.0,
            "confidence_count": 0,
            "capability_mass": {
                "EXPLORE": 0, "CALIBRATE": 0, "EXPLOIT": 0,
                "MASTER": 0, "unset": 0,
            },
        }

    buckets = {l1: _new_bucket() for l1 in l1_keys}
    orphan_bucket = _new_bucket()
    saw_orphans = False

    for key, node in nodes.items():
        if node.get("depth") == 0:
            continue
        l1 = _get_l1_for_node(nodes, key)
        if l1 and l1 in buckets:
            bucket = buckets[l1]
        else:
            bucket = orphan_bucket
            saw_orphans = True

        bucket["total_nodes"] += 1
        if node.get("children", []):
            bucket["interior_count"] += 1
        else:
            bucket["leaf_count"] += 1

        rc = node.get("retrieval_count", 0)
        if isinstance(rc, (int, float)):
            bucket["total_retrieval_count"] += int(rc)
        th = node.get("times_helpful", 0)
        if isinstance(th, (int, float)):
            bucket["total_times_helpful"] += int(th)
        tn = node.get("times_noise", 0)
        if isinstance(tn, (int, float)):
            bucket["total_times_noise"] += int(tn)

        conf = node.get("confidence")
        if isinstance(conf, (int, float)):
            bucket["confidence_sum"] += float(conf)
            bucket["confidence_count"] += 1

        cap = node.get("capability_level") or "unset"
        if cap not in bucket["capability_mass"]:
            cap = "unset"
        bucket["capability_mass"][cap] += 1

    def _finalize(b):
        if b["confidence_count"]:
            b["mean_confidence"] = round(
                b["confidence_sum"] / b["confidence_count"], 4)
        else:
            b["mean_confidence"] = None
        b.pop("confidence_sum")
        b.pop("confidence_count")
        return b

    out = {l1: _finalize(b) for l1, b in buckets.items()}
    if saw_orphans:
        out["_orphan"] = _finalize(orphan_bucket)
    return out


def parse_value(value_str):
    """Parse a string value into the appropriate Python type.

    Booleans and null are case-insensitive. Pre-2026-05-07 only the
    lowercase YAML forms were accepted, so a shell caller passing
    `--set node field True` would silently get the string "True"
    written to the field instead of Python True. The case-insensitive
    match covers `True`, `TRUE`, `False`, `FALSE`, `Null`, `NULL`,
    and `None` / `NONE`.
    """
    lowered = value_str.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null" or lowered == "none":
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
        result = compute_stats(tree, by_l1=getattr(args, "by_l1", False))
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
        # Structural trigger (): subtree-leaves > leaf_cap. The legacy
        # --threshold line-count arg no longer applies to decompose candidates.
        result = get_decompose_candidates(
            tree,
            include_skipped=getattr(args, "include_skipped", False),
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.redistribute_candidates:
        # Structural trigger (): children > K_max. The legacy
        # --threshold line-count arg no longer applies to regroup candidates.
        result = get_redistribute_candidates(
            tree,
            include_skipped=getattr(args, "include_skipped", False),
        )
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
        result = get_distill_candidates(
            tree,
            include_skipped=getattr(args, "include_skipped", False),
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.maintenance:
        # Single-source-of-truth read path for the maintenance cadence block.
        # Callers (aspirations Phase 8.8) depend on this — DO NOT shadow with
        # a working-memory mirror. See 2026-04-18 Option A cleanup.
        result = tree.get("maintenance") or {}
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
                # last_updated + article_count: consumed by strategic-scan S2
                # (knowledge-frontier staleness/thin detection). Both fields
                # exist on every node; omitting them here left S2a/S2b dead.
                # 8.
                "last_updated": node.get("last_updated"),
                "article_count": node.get("article_count", 0),
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
    elif args.reconcile_capabilities:
        cmd_reconcile_capabilities(args)
    elif args.reparent:
        cmd_reparent(args)
    elif args.record_maintenance:
        cmd_record_maintenance(args)
    else:
        print("Specify an update subcommand. Use --help for options.", file=sys.stderr)
        sys.exit(1)


def cmd_record_maintenance(args):
    """Record /tree maintain completion in the top-level maintenance block.

    Invoked at the tail of the /tree maintain pseudocode. Writes:
      maintenance.last_maintain_at       — always (ISO-8601 local timestamp)
      maintenance.last_backlog_mode_at   — when --backlog-mode is set
      maintenance.last_backlog_clear_at  — auto-set when post-run debt
                                            (distill + decompose candidates)
                                            has dropped to or below
                                            tree_debt_check.debt_threshold

    Cleared state is computed here (not passed from the caller) so the
    SKILL.md pseudocode stays platform-neutral — no inline Python parsing
    of candidate JSON required.

    The maintenance block is the only cadence signal the framework has for
    "tree rebalancing actually ran." Without it, skipped consolidation steps
    are invisible until backlog compounds into structural drag.

    With --with-run-record, additionally reads a JSON blob from stdin with
    LLM-reported action-phase counts and appends a full run record to
    world/tree-maintenance-log.jsonl. The JSONL log is the instrumentation
    artifact — _tree.yaml.maintenance remains the single cadence source of
    truth. Python-side pre-filter skip counts are re-computed here (after
    the maintain run), capturing the POST-RUN candidate surface; this is
    the drain-rate signal callers will aggregate against the LLM-reported
    "actioned" counts to diagnose why the tree stays debt-heavy.
    """
    # CRITICAL — DO NOT split this back into read_tree() ... write_tree(tree).
    # Maintenance metadata writes can race against any other tree-modifier
    # that's also producing post-run debt counts; locked_modify_yaml holds
    # the lock so the maintenance block reflects the SAME tree snapshot the
    # debt counts were computed from. See _fileops.py:638.
    now = datetime.now().isoformat(timespec="seconds")

    # Auto-detect cleared state: post-run debt vs threshold.
    # No fallbacks — tree.yaml is framework-owned and must contain
    # tree_debt_check.debt_threshold. A missing config or key means the
    # framework is misconfigured; crash loudly rather than silently using
    # a defensive default (single-source-of-truth directive).
    with open(str(CONFIG_DIR / "tree.yaml"), "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    debt_threshold = cfg["tree_debt_check"]["debt_threshold"]
    with_run_record = getattr(args, "with_run_record", False)

    from _fileops import locked_modify_yaml

    captured = {
        "maintenance": None, "distill_count": 0, "decompose_count": 0,
        "distill_detail": None, "decompose_detail": None, "redistribute_detail": None,
    }

    def _do_record(tree):
        if not isinstance(tree, dict) or "nodes" not in tree:
            print("Invalid tree file: missing 'nodes' key", file=sys.stderr)
            sys.exit(1)
        maintenance = tree.get("maintenance") or {}
        maintenance["last_maintain_at"] = now
        if getattr(args, "backlog_mode", False):
            maintenance["last_backlog_mode_at"] = now
        # Stop-mode cadence marker (Lane D Magic Wand #4 — small budget per /stop
        # so structural debt drains incrementally; see tree.yaml stop_mode_caps).
        # Mirrors the backlog_mode pattern above. The downstream LLM can read
        # last_stop_mode_at to surface "last drained: T" in stop reports.
        if getattr(args, "stop_mode", False):
            maintenance["last_stop_mode_at"] = now

        if with_run_record:
            distill_detail = get_distill_candidates(tree, include_skipped=True)
            decompose_detail = get_decompose_candidates(
                tree, include_skipped=True,
            )
            redistribute_detail = get_redistribute_candidates(
                tree, include_skipped=True,
            )
            distill_count = len(distill_detail["candidates"])
            decompose_count = len(decompose_detail["candidates"])
            captured["distill_detail"] = distill_detail
            captured["decompose_detail"] = decompose_detail
            captured["redistribute_detail"] = redistribute_detail
        else:
            distill_count = len(get_distill_candidates(tree))
            decompose_count = len(get_decompose_candidates(tree))

        post_debt = distill_count + decompose_count
        if post_debt <= debt_threshold:
            maintenance["last_backlog_clear_at"] = now

        tree["maintenance"] = maintenance
        tree["last_updated"] = date.today().isoformat()
        captured["maintenance"] = maintenance
        captured["distill_count"] = distill_count
        captured["decompose_count"] = decompose_count
        return tree

    locked_modify_yaml(TREE_PATH, _do_record)
    maintenance = captured["maintenance"]
    distill_count = captured["distill_count"]
    decompose_count = captured["decompose_count"]
    post_debt = distill_count + decompose_count

    result = {
        "maintenance": maintenance,
        "post_run_debt": {
            "distill": distill_count,
            "decompose": decompose_count,
            "total": post_debt,
            "threshold": debt_threshold,
            "cleared": post_debt <= debt_threshold,
        },
    }

    # --- Run-record append (opt-in via --with-run-record) ---------------
    if with_run_record:
        # Function-local import: matches the idiom at the top of the
        # read-path functions (see `from _fileops import acquire_lock, ...`).
        # _fileops has a valid Python module name — no importlib needed.
        from _fileops import locked_append_jsonl

        if sys.stdin.isatty():
            print(
                "Error: --with-run-record requires a JSON blob on stdin "
                "(mode/started_at/decompose/distill/...).",
                file=sys.stderr,
            )
            sys.exit(1)
        raw = sys.stdin.read().strip()
        if not raw:
            print(
                "Error: --with-run-record got empty stdin (expected JSON blob).",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            llm_reported = json.loads(raw)
        except json.JSONDecodeError as e:
            print("Error: --with-run-record JSON parse failed: " + str(e), file=sys.stderr)
            sys.exit(1)
        if not isinstance(llm_reported, dict):
            print(
                "Error: --with-run-record JSON must be an object (got "
                + type(llm_reported).__name__ + ").",
                file=sys.stderr,
            )
            sys.exit(1)

        def _aggregate_skip_reasons(skipped_items):
            agg = {}
            for item in skipped_items:
                reason = item.get("skip_reason", "unknown")
                agg[reason] = agg.get(reason, 0) + 1
            return agg

        decompose_detail = captured["decompose_detail"]
        distill_detail = captured["distill_detail"]
        redistribute_detail = captured["redistribute_detail"]
        pre_filter = {
            "decompose": {
                "candidates_in": len(decompose_detail["candidates"]),
                "skipped_by_reason": _aggregate_skip_reasons(decompose_detail["skipped"]),
            },
            "distill": {
                "candidates_in": len(distill_detail["candidates"]),
                "skipped_by_reason": _aggregate_skip_reasons(distill_detail["skipped"]),
            },
            "redistribute": {
                "candidates_in": len(redistribute_detail["candidates"]),
                "skipped_by_reason": _aggregate_skip_reasons(redistribute_detail["skipped"]),
            },
        }

        # Run ID: stable per-invocation. If the caller's JSON included
        # started_at, use its value as the anchor; else use now. Falling back
        # to now is safe (each run record is appended once).
        started_at = llm_reported.get("started_at") or now
        agent_from_env = os.environ.get("MIND_AGENT", "") or "unknown"
        run_id = "maint-" + started_at.replace(":", "").replace("-", "") + "-" + agent_from_env

        record = {
            "run_id": run_id,
            "agent": agent_from_env,
            "mode": llm_reported.get("mode", "standard"),
            "started_at": started_at,
            "ended_at": now,
            "candidates_pre_filter": pre_filter,
            "llm_reported": {
                k: v for k, v in llm_reported.items()
                if k not in ("mode", "started_at")
            },
            "post_run_debt": result["post_run_debt"],
        }

        log_path = str(WORLD_DIR / "tree-maintenance-log.jsonl")
        locked_append_jsonl(log_path, record)

        result["run_record"] = {
            "run_id": run_id,
            "log_path": log_path,
            "appended": True,
        }

    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_set(args):
    # CRITICAL — DO NOT split this back into read_tree() ... write_tree(tree).
    # That pattern reads OUTSIDE the write lock, so two concurrent processes
    # both see the same baseline and the second writer clobbers the first.
    # Confidence updates are particularly nasty under that race because
    # _propagate_in_memory walks the parent chain in memory; a clobbered
    # write loses the propagation upward. locked_modify_yaml holds the
    # lock across read, modify, propagate, AND write — atomically. See
    # _fileops.py:638 for the helper.
    if len(args.set) != 3:
        print("--set requires exactly 3 args: <key> <field> <value>", file=sys.stderr)
        sys.exit(1)
    key, field, value_str = args.set
    value = parse_value(value_str)

    from _fileops import locked_modify_yaml

    captured = {"node": None, "ancestors_updated": [], "capability_changes": []}

    def _do_set(data):
        if not isinstance(data, dict) or "nodes" not in data:
            print("Invalid tree file: missing 'nodes' key", file=sys.stderr)
            sys.exit(1)
        nodes = data["nodes"]
        if key not in nodes:
            print("Node not found: " + key, file=sys.stderr)
            sys.exit(1)
        node = nodes[key]
        v = value
        if field == "file" and isinstance(v, str):
            v = normalize_virtual_path(v)
        node[field] = v
        # Auto-bump per-node last_updated when the caller mutated something
        # OTHER than last_updated itself (don't clobber explicit date sets).
        # Replaces the manual `--set <key> last_updated <today>` ceremony
        # the SKILL.md authors had to remember after every content change.
        if field != "last_updated":
            node["last_updated"] = date.today().isoformat()
        nodes[key] = node

        if field == "confidence":
            competence = _load_competence_config()
            ancestors_updated, capability_changes = _propagate_in_memory(
                nodes, key, competence)
            captured["ancestors_updated"] = ancestors_updated
            captured["capability_changes"] = capability_changes

        captured["node"] = dict(node)
        data["nodes"] = nodes
        data["last_updated"] = date.today().isoformat()
        return data

    locked_modify_yaml(TREE_PATH, _do_set)

    out = apply_defaults(captured["node"])
    out["key"] = key
    if field == "confidence":
        out["ancestors_updated"] = captured["ancestors_updated"]
        out["capability_changes"] = captured["capability_changes"]
    print(json.dumps(out, indent=2, ensure_ascii=False))


def _read_in_flight_goal_id():
    """Read the EXECUTING goal id from world/team-state.yaml in_flight (,
    Gate D spillover provenance). Fail-open: any error — no bound agent, missing
    file, parse error, no in_flight — returns None. Mirrors store_registry.py
    _rb_inject_source_goal so a tree-node write records the SAME executing goal an
    rb write does, giving the SPILL-1 analysis a uniform origin signal across both
    encoding stores."""
    try:
        from _fileops import _agent_name
        agent_name = _agent_name()
        if not agent_name or WORLD_DIR is None:
            return None
        path = WORLD_DIR / "team-state.yaml"
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return (data.get("agent_status", {})
                    .get(agent_name, {})
                    .get("in_flight", {})
                    .get("goal_id")) or None
    except Exception:
        return None


def cmd_add_child(args):
    # CRITICAL — DO NOT split this back into read_tree() ... write_tree(tree).
    # The dedup gate (_enforce_dedup) and child-limit gate are TOCTOU-vulnerable
    # without holding the lock across read+write: process A reads "no
    # duplicate sibling", process B adds an identically-named child, process
    # A writes its add — duplicate keys collide and one of the two children
    # is silently overwritten in the YAML map. Same atomicity reason for
    # the parent-existence and unique-key validations. See _fileops.py:638.
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

    from _fileops import locked_modify_yaml

    captured = {"child_node": None}

    def _do_add(tree):
        if not isinstance(tree, dict) or "nodes" not in tree:
            print("Invalid tree file: missing 'nodes' key", file=sys.stderr)
            sys.exit(1)
        nodes = tree["nodes"]

        if parent_key not in nodes:
            print("Parent node not found: " + parent_key, file=sys.stderr)
            sys.exit(1)
        if child_key in nodes:
            print("Node key already exists: " + child_key, file=sys.stderr)
            sys.exit(1)

        # Pre-create duplicate gate (Phase 2 of cognitive-core curation plan).
        # Exits 2 on exact-key sibling, 3 on summary overlap. Bypass with --no-dedup.
        _enforce_dedup(parent_key, child_key, child_data.get("summary", ""),
                       tree, no_dedup=getattr(args, "no_dedup", False),
                       context="add-child")

        parent = nodes[parent_key]
        parent_depth = parent.get("depth", 0)

        # Capability-aware child limit (Phase 3 of cognitive-core curation plan).
        # Default mode `warn` logs but allows; `block` in config hard-enforces.
        _enforce_child_limit(parent_key, parent,
                             no_limit=getattr(args, "no_dedup", False),
                             accept_overflow=getattr(args, "accept_overflow", None),
                             context="add-child")

        child_node = {}
        if "file" not in child_data:
            child_node["file"] = compute_child_path(parent.get("file", ""), child_key)
        else:
            child_node["file"] = normalize_virtual_path(child_data["file"])
        child_node["depth"] = parent_depth + 1
        child_node["parent"] = parent_key
        child_node["children"] = child_data.get("children", [])
        child_node["child_count"] = len(child_node["children"])
        # 3: node_type is derive-always from child-presence (mirror
        # child_count) — set HERE at the create path, not copied (removed from
        # the allowlist below) and not via apply_defaults' fill-if-absent (which
        # also normalizes reads, masking on-disk drift). The parent-flip below
        # keeps a later-childed node correct; this keeps it correct AT creation.
        # Sibling 7.
        child_node["node_type"] = "interior" if child_node["children"] else "leaf"
        # poignancy (, BRD Gap 1a producer): copy an LLM-rated 1-10
        # importance value at creation so the tree-node write path assigns
        # poignancy AT WRITE TIME (the  blend consumes it). Absent →
        # apply_defaults sets None (null-safe). Without this allowlist entry a
        # poignancy in child_data would be silently dropped.
        # node_type intentionally excluded — derive-always at create (3).
        for field in ("summary", "domain_confidence", "capability_level", "confidence",
                      "article_count", "growth_state", "origin_goal_id",
                      "poignancy"):
            if field in child_data:
                child_node[field] = child_data[field]
        # Inherit capability_level from parent when caller didn't specify one,
        # matching _enforce_child_limit's parent-defaulting convention.
        # apply_defaults will fall back to "EXPLORE" only if parent is also
        # missing capability_level (legacy data — backfill handles those).
        if "capability_level" not in child_node:
            parent_cl = parent.get("capability_level")
            if parent_cl:
                child_node["capability_level"] = parent_cl
        child_node = apply_defaults(child_node)
        # Stamp creation date so freshness/recency scoring has a real signal
        # on day 1 rather than waiting for the first content edit to backfill it.
        child_node["last_updated"] = date.today().isoformat()
        # origin_goal_id (): record the EXECUTING goal that created this
        # node, for the Gate D SPILL-1 spillover analysis. Caller-wins (an explicit
        # value copied above is preserved); otherwise auto-inject from team-state
        # in_flight. Absent when no goal is executing (a manual add) — pre-
        # readers ignore the unknown field, so existing consumers parse unchanged.
        if "origin_goal_id" not in child_node:
            _origin = _read_in_flight_goal_id()
            if _origin:
                child_node["origin_goal_id"] = _origin

        nodes[child_key] = child_node
        if child_key not in parent.get("children", []):
            if "children" not in parent:
                parent["children"] = []
            parent["children"].append(child_key)
            parent["child_count"] = len(parent["children"])
            # 7: a freshly-created parent that gains its first
            # child via add-child must flip leaf->interior. add-child
            # updates child_count but historically left node_type stale,
            # mislabeling interior nodes as leaves (misleads retrieval/
            # decompose and trips tree-validate). Mirrors the cmd_reparent
            # new-parent idiom.
            if parent.get("node_type") == "leaf":
                parent["node_type"] = "interior"
        nodes[parent_key] = parent

        captured["child_node"] = dict(child_node)
        tree["nodes"] = nodes
        tree["last_updated"] = date.today().isoformat()
        return tree

    locked_modify_yaml(TREE_PATH, _do_add)

    # S9: log the L1 pick for this add-child (fail-open).
    _log_l1_pick_for_key(
        child_key,
        decision_type="add-child",
        source=getattr(args, "encoding_source", None),
        reason=getattr(args, "encoding_reason", None),
    )

    out = apply_defaults(captured["child_node"])
    out["key"] = child_key
    print(json.dumps(out, indent=2, ensure_ascii=False))


def _check_subtree_orphan(nodes, child_key):
    """Return child_key's IMMEDIATE children list (one hop), or [] if absent
    or leaf. Used by cmd_remove_child + cmd_batch's remove-child as a refusal
    gate — if this returns non-empty, removing child_key would orphan its
    whole subtree (each descendant's `parent` field still points at the
    deleted key, leaving them unreachable from root, invisible to retrieval,
    and flagged as orphans by validate_tree). Immediate-children is a
    sufficient gate: if there are no children, there are no descendants,
    and removal is safe.
    """
    if child_key not in nodes:
        return []
    return list(nodes[child_key].get("children") or [])


def cmd_remove_child(args):
    # CRITICAL — DO NOT split this back into read_tree() ... write_tree(tree).
    # The orphan gate (_check_subtree_orphan) is TOCTOU-vulnerable without
    # holding the lock across read+write: process A reads "no children"
    # → passes the gate, while process B adds a child concurrently
    # → process A writes the removal, leaving B's new child orphaned.
    # locked_modify_yaml holds the lock across read, gate-check, AND
    # write so the gate decision and the removal are atomic. See
    # _fileops.py:638 for the helper.
    if len(args.remove_child) != 2:
        print("--remove-child requires exactly 2 args: <parent-key> <child-key>", file=sys.stderr)
        sys.exit(1)
    parent_key, child_key = args.remove_child

    from _fileops import locked_modify_yaml

    def _do_remove(data):
        if not isinstance(data, dict) or "nodes" not in data:
            print("Invalid tree file: missing 'nodes' key", file=sys.stderr)
            sys.exit(1)
        nodes = data["nodes"]
        if parent_key not in nodes:
            print("Parent node not found: " + parent_key, file=sys.stderr)
            sys.exit(1)
        parent = nodes[parent_key]
        children = parent.get("children", [])
        if child_key not in children:
            print("Child '{}' not in parent '{}' children list".format(child_key, parent_key), file=sys.stderr)
            sys.exit(1)

        descendants = _check_subtree_orphan(nodes, child_key)
        if descendants:
            print(
                "Refused: '{}' has {} descendant(s) ({}). Remove them first "
                "(depth-first) — removing this node now would orphan the "
                "subtree.".format(child_key, len(descendants), ", ".join(descendants)),
                file=sys.stderr,
            )
            sys.exit(2)

        children.remove(child_key)
        parent["children"] = children
        parent["child_count"] = len(children)
        if not children:
            # 5: removing the last child flips parent interior->leaf
            # (mirrors cmd_reparent old-parent path; symmetric to the
            # 7 add-child leaf->interior flip).
            parent["node_type"] = "leaf"
        nodes[parent_key] = parent
        if child_key in nodes:
            del nodes[child_key]
        data["nodes"] = nodes
        data["last_updated"] = date.today().isoformat()
        return data

    locked_modify_yaml(TREE_PATH, _do_remove)

    # : sweep dangling cross-refs in dependent stores (experience,
    # reasoning-bank, guardrails). Without this, deleting a tree node leaves
    # tree_nodes_related / experience_ref pointers that match no node and
    # the learning-routing ratchet drifts from baseline 0. Sweep runs after
    # the lock releases — repair script holds its own per-file locks via
    # tempfile+replace and is idempotent.
    _post_remove_sweep_dangling([child_key])

    print(json.dumps({"removed": child_key, "parent": parent_key}, ensure_ascii=False))


def _post_remove_sweep_dangling(removed_slugs):
    """Null dangling cross-refs in learning stores after tree node removal.

    Invoked by cmd_remove_child and cmd_batch ("remove-child" op) AFTER the
    tree write completes. Delegates to learning-routing-repair.py --apply,
    which (a) scans reasoning-bank, guardrails, and per-agent experience.jsonl
    for refs that no longer resolve, (b) nulls those fields, and (c) writes
    old values to world/.history/learning-routing-repair-YYYY-MM-DD.jsonl.

    Failures here do NOT roll back the tree mutation — the tree write is
    primary, the sweep is cleanup. Any leftover dangling refs surface on the
    next learning-routing-ratchet run, which the agent can resolve via the
    same repair script.

    See g-248-83 (closes the silent-drift class where tree-edit-driven node
    deletion leaked 105 dangling refs across 5 files).
    """
    if not removed_slugs:
        return
    repair_py = Path(__file__).resolve().parent / "learning-routing-repair.py"
    if not repair_py.exists():
        return
    import subprocess
    try:
        # sys.executable bypasses the Windows python3 → Microsoft-Store-stub
        # trap (CLAUDE.md "Python Invocation"). Capture output so the
        # cleanup's stdout doesn't pollute tree.py's JSON output.
        result = subprocess.run(
            [sys.executable, str(repair_py), "--apply"],
            capture_output=True, text=True, timeout=30,
        )
        # Repair exit codes: 0 = clean OR applied. Anything else is a real
        # error worth surfacing; --apply never returns 1 (that's dry-run only).
        if result.returncode != 0:
            print(
                "[tree.py] post-remove sweep failed (exit {}): {}".format(
                    result.returncode, (result.stderr or "").strip()[:200]),
                file=sys.stderr,
            )
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        print("[tree.py] post-remove sweep error: {}".format(exc), file=sys.stderr)


def cmd_increment(args):
    # CRITICAL — DO NOT revert to the older read_tree()+write_tree() pattern.
    # That pattern read the tree OUTSIDE the write lock, which let two
    # concurrent processes both observe counter=N and both write N+1,
    # silently dropping one increment. journal-append.sh's tree-cite scan
    # and retrieve.py's locked retrieval-count bumps all hit this path
    # under multi-agent contention. locked_modify_yaml holds the lock
    # across read AND write, closing the race. See _fileops.py:638.
    if len(args.increment) != 2:
        print("--increment requires exactly 2 args: <key> <field>", file=sys.stderr)
        sys.exit(1)
    key, field = args.increment

    from _fileops import locked_modify_yaml

    captured = {"node": None}

    def _bump(data):
        if not isinstance(data, dict) or "nodes" not in data:
            print("Invalid tree file: missing 'nodes' key", file=sys.stderr)
            sys.exit(1)
        nodes = data["nodes"]
        if key not in nodes:
            print("Node not found: " + key, file=sys.stderr)
            sys.exit(1)
        node = nodes[key]
        current = node.get(field, 0)
        if not isinstance(current, (int, float)):
            current = 0
        node[field] = current + 1
        if field in _UTILITY_RATIO_FIELDS:
            _recompute_utility_ratio(node)
        nodes[key] = node
        data["last_updated"] = date.today().isoformat()
        captured["node"] = dict(node)
        return data

    locked_modify_yaml(TREE_PATH, _bump)

    out = apply_defaults(captured["node"])
    out["key"] = key
    print(json.dumps(out, indent=2, ensure_ascii=False))


def cmd_batch(args):
    """Batch update: read JSON operations from stdin and apply atomically.

    Supports 5 op types: set, increment, add-child, remove-child, propagate.
    All mutations happen in a single read-write cycle. Propagate ops run LAST
    so they see updated children from earlier ops in the same batch.

    CRITICAL — DO NOT split this back into read_tree() ... write_tree(tree).
    All validation, mutation, and propagation happen INSIDE locked_modify_yaml's
    lock. The orphan gate on remove-child is TOCTOU-vulnerable without that
    lock (process A reads "no children" → passes gate → process B adds a
    child → process A writes the removal → orphaned). Same atomicity reason
    applies to add-child's dedup gate, the forward-reference validation,
    and the propagate-walks-children phase. See _fileops.py:638.
    """
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

    valid_ops = ("set", "increment", "add-child", "remove-child", "propagate")

    # Collect child keys that add-child ops will create (for forward-reference
    # validation). Doing this OUTSIDE the lock is safe — it's a pure function
    # of the input operations, not the tree state.
    pending_child_keys = set()
    for op in operations:
        if op.get("op") == "add-child":
            child = op.get("child", {})
            if child.get("key"):
                pending_child_keys.add(child["key"])

    from _fileops import locked_modify_yaml

    captured = {"updated_nodes": [], "propagate_results": []}
    removed_slugs = []  # : collect for post-lock cleanup sweep
    added_child_keys = []  # S9: collect for post-lock L1-pick logging

    def _do_batch(tree):
        if not isinstance(tree, dict) or "nodes" not in tree:
            print("Invalid tree file: missing 'nodes' key", file=sys.stderr)
            sys.exit(1)
        nodes = tree["nodes"]

        # Split into mutation ops and propagate ops (validation phase inside
        # the lock — forward-reference checks against `nodes` need the
        # locked-read snapshot).
        mutation_ops = []
        propagate_ops = []
        for i, op in enumerate(operations):
            op_type = op.get("op")
            key = op.get("key")
            if not op_type or not key:
                print("Operation {} missing required fields (op, key)".format(i), file=sys.stderr)
                sys.exit(1)
            if op_type not in valid_ops:
                print("Operation {} has invalid op '{}' (must be one of: {})".format(
                    i, op_type, ", ".join(valid_ops)), file=sys.stderr)
                sys.exit(1)
            # Key existence check: skip for add-child (creates new nodes) and for keys
            # that will be created by add-child ops earlier in this batch.
            if op_type != "add-child" and key not in nodes and key not in pending_child_keys:
                print("Operation {} references non-existent node '{}'".format(i, key), file=sys.stderr)
                sys.exit(1)
            # set/increment require field
            if op_type in ("set", "increment") and not op.get("field"):
                print("Operation {} ({}) missing required 'field'".format(i, op_type), file=sys.stderr)
                sys.exit(1)
            # add-child requires child dict with key
            if op_type == "add-child":
                child = op.get("child", {})
                if not child.get("key"):
                    print("Operation {} (add-child) missing child.key".format(i), file=sys.stderr)
                    sys.exit(1)
                if key not in nodes:
                    print("Operation {} (add-child) parent '{}' not found".format(i, key), file=sys.stderr)
                    sys.exit(1)
            # remove-child requires child_key
            if op_type == "remove-child" and not op.get("child_key"):
                print("Operation {} (remove-child) missing 'child_key'".format(i), file=sys.stderr)
                sys.exit(1)

            if op_type == "propagate":
                propagate_ops.append(op)
            else:
                mutation_ops.append(op)

        # Phase 1: Apply mutation ops sequentially
        updated_keys = set()
        for op in mutation_ops:
            op_type = op["op"]
            key = op["key"]

            if op_type == "set":
                node = nodes[key]
                field = op["field"]
                value = op.get("value")
                if isinstance(value, str):
                    value = parse_value(value)
                if field == "file" and isinstance(value, str):
                    value = normalize_virtual_path(value)
                node[field] = value
                if field != "last_updated":
                    node["last_updated"] = date.today().isoformat()
                nodes[key] = node
                updated_keys.add(key)

            elif op_type == "increment":
                node = nodes[key]
                field = op["field"]
                current = node.get(field, 0)
                if not isinstance(current, (int, float)):
                    current = 0
                node[field] = current + 1
                if field in _UTILITY_RATIO_FIELDS:
                    _recompute_utility_ratio(node)
                nodes[key] = node
                updated_keys.add(key)

            elif op_type == "add-child":
                parent_key = key
                child_data = op["child"]
                child_key = child_data["key"]
                if child_key in nodes:
                    print("Batch add-child: node key already exists: " + child_key, file=sys.stderr)
                    sys.exit(1)
                # Pre-create dedup gate (same as single-op add-child).
                _enforce_dedup(parent_key, child_key, child_data.get("summary", ""),
                               tree, no_dedup=getattr(args, "no_dedup", False),
                               context="batch add-child")
                parent = nodes[parent_key]
                parent_depth = parent.get("depth", 0)
                _enforce_child_limit(parent_key, parent,
                                     no_limit=getattr(args, "no_dedup", False),
                                     accept_overflow=getattr(args, "accept_overflow", None),
                                     context="batch add-child")
                child_node = {}
                if "file" not in child_data:
                    child_node["file"] = compute_child_path(parent.get("file", ""), child_key)
                else:
                    child_node["file"] = normalize_virtual_path(child_data["file"])
                child_node["depth"] = parent_depth + 1
                child_node["parent"] = parent_key
                child_node["children"] = child_data.get("children", [])
                child_node["child_count"] = len(child_node["children"])
                # 3: node_type is derive-always from child-presence
                # (mirror child_count) — set at the create path, not copied
                # (removed from the allowlist below) and not via apply_defaults'
                # fill-if-absent. Mirrors cmd_add_child. Sibling 7.
                child_node["node_type"] = "interior" if child_node["children"] else "leaf"
                # poignancy () + origin_goal_id ( / 3):
                # batch add-child mirrors cmd_add_child — copy the LLM-rated 1-10
                # poignancy AND the executing-goal provenance at creation. The
                # origin_goal_id allowlist entry landed only in cmd_add_child, so
                # every batch-created node was invisible to the Gate D SPILL-1
                # attribution (duplicated-path divergence, rb-1776). Restored here.
                # node_type intentionally excluded — derive-always at create (3).
                for field in ("summary", "domain_confidence", "capability_level", "confidence",
                              "article_count", "growth_state", "origin_goal_id",
                              "poignancy"):
                    if field in child_data:
                        child_node[field] = child_data[field]
                if "capability_level" not in child_node:
                    parent_cl = parent.get("capability_level")
                    if parent_cl:
                        child_node["capability_level"] = parent_cl
                child_node = apply_defaults(child_node)
                child_node["last_updated"] = date.today().isoformat()
                # origin_goal_id auto-inject ( / 3): mirror
                # cmd_add_child — caller-wins (an explicit value copied above is
                # preserved); otherwise auto-inject from team-state in_flight.
                if "origin_goal_id" not in child_node:
                    _origin = _read_in_flight_goal_id()
                    if _origin:
                        child_node["origin_goal_id"] = _origin
                nodes[child_key] = child_node
                if child_key not in parent.get("children", []):
                    if "children" not in parent:
                        parent["children"] = []
                    parent["children"].append(child_key)
                    parent["child_count"] = len(parent["children"])
                    # 7: flip parent leaf->interior on first child
                    # (mirrors cmd_add_child / cmd_reparent new-parent idiom).
                    if parent.get("node_type") == "leaf":
                        parent["node_type"] = "interior"
                nodes[parent_key] = parent
                updated_keys.add(child_key)
                updated_keys.add(parent_key)
                added_child_keys.append(child_key)

            elif op_type == "remove-child":
                parent_key = key
                child_key = op["child_key"]
                # Same subtree-orphan gate as cmd_remove_child. Refuse in-batch
                # rather than partially apply — write_tree is single-shot so a
                # refused remove leaves the entire batch unwritten (caller can
                # fix and retry, or list the descendants earlier in the same
                # batch so they're gone by the time this op runs).
                descendants = _check_subtree_orphan(nodes, child_key)
                if descendants:
                    print(
                        "Refused remove-child in batch (parent={}, child={}): "
                        "child still has {} descendant(s) ({}). Remove them "
                        "first (depth-first).".format(
                            parent_key, child_key,
                            len(descendants), ", ".join(descendants)),
                        file=sys.stderr,
                    )
                    sys.exit(2)
                parent = nodes[parent_key]
                children = parent.get("children", [])
                if child_key in children:
                    children.remove(child_key)
                    parent["children"] = children
                    parent["child_count"] = len(children)
                    if not children:
                        # 5: last-child removal flips parent
                        # interior->leaf (mirrors cmd_reparent; symmetric to
                        # the 7 add-child path).
                        parent["node_type"] = "leaf"
                if child_key in nodes:
                    del nodes[child_key]
                    removed_slugs.append(child_key)  # : track for sweep
                nodes[parent_key] = parent
                updated_keys.add(parent_key)

        # Phase 2: Apply propagate ops LAST (sees all mutations from Phase 1)
        propagate_results = []
        if propagate_ops:
            competence = _load_competence_config()
            for op in propagate_ops:
                p_key = op["key"]
                ancestors_updated, capability_changes = _propagate_in_memory(nodes, p_key, competence)
                propagate_results.append({
                    "source_node": p_key,
                    "ancestors_updated": ancestors_updated,
                    "capability_changes": capability_changes,
                })
                for anc in ancestors_updated:
                    updated_keys.add(anc["key"])

        # Capture output snapshot INSIDE the lock — once we release, another
        # writer could change `nodes` and the printed JSON would lie about
        # the post-batch state.
        for key in updated_keys:
            if key in nodes:
                out = apply_defaults(nodes[key])
                out["key"] = key
                captured["updated_nodes"].append(out)
        captured["propagate_results"] = propagate_results

        tree["nodes"] = nodes
        tree["last_updated"] = date.today().isoformat()
        return tree

    locked_modify_yaml(TREE_PATH, _do_batch)

    # : post-lock sweep for any remove-child ops in this batch.
    _post_remove_sweep_dangling(removed_slugs)

    # S9: per-op L1 pick logging for each add-child in this batch (fail-open).
    source = getattr(args, "encoding_source", None)
    reason = getattr(args, "encoding_reason", None)
    for ck in added_child_keys:
        _log_l1_pick_for_key(ck, decision_type="batch-add-child",
                              source=source, reason=reason)

    # Backward compat: plain array if no propagate ops
    if not captured["propagate_results"]:
        print(json.dumps(captured["updated_nodes"], indent=2, ensure_ascii=False))
    else:
        print(json.dumps({
            "updated_nodes": captured["updated_nodes"],
            "propagate": captured["propagate_results"],
        }, indent=2, ensure_ascii=False))


def _load_competence_config():
    """Load competence mapping from config, with defaults."""
    config_path = str(CONFIG_DIR / "tree.yaml")
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        return config.get("domain_health", {}).get("competence_mapping", {})
    return {"EXPLORE": 0.25, "CALIBRATE": 0.50, "EXPLOIT": 0.75, "MASTER": 1.00}


def _graduate_node_level(node, competence):
    """Recompute a single node's capability_level from its own confidence
    against the competence_mapping thresholds. Mutates node in place when
    the level changes.

    Returns (old_level, new_level) if changed, else (None, None). Nodes
    without numeric confidence are skipped — structural/placeholder nodes
    keep whatever capability_level they had.
    """
    conf = node.get("confidence")
    if conf is None or not isinstance(conf, (int, float)):
        return None, None
    levels_sorted = sorted(competence.items(), key=lambda x: x[1])
    if not levels_sorted:
        return None, None
    old_level = node.get("capability_level", "") or ""
    new_level = "EXPLORE"
    for level_name, threshold in levels_sorted:
        if conf >= threshold:
            new_level = level_name
    if old_level != new_level:
        node["capability_level"] = new_level
        return old_level, new_level
    return None, None


def _propagate_in_memory(nodes, key, competence):
    """Propagate confidence up parent chain. Mutates nodes in place.
    Returns (ancestors_updated, capability_changes).

    Also re-evaluates the source node's OWN capability_level from its
    current confidence (self-graduation) — without this step, leaf writes
    leave the source's capability_level stale. See rb-228, g-115-37.

    Walks ancestors from key to root. For each ancestor, computes avg
    confidence from its children, updates confidence + domain_confidence,
    and detects capability_level threshold crossings.
    """
    levels_sorted = sorted(competence.items(), key=lambda x: x[1])

    # Walk ancestors using nodes dict directly (no tree wrapper needed)
    if key not in nodes:
        return [], []

    ancestors_updated = []
    capability_changes = []

    # Self-graduation: the ancestor loop below skips the source node
    # (result_chain[1:]), so without this step a leaf whose confidence was
    # just raised above a threshold keeps its old capability_level forever.
    src_old, src_new = _graduate_node_level(nodes[key], competence)
    if src_old is not None:
        capability_changes.append({
            "key": key,
            "old_level": src_old,
            "new_level": src_new,
        })

    result_chain = []
    visited = set()
    current = key
    while current is not None:
        if current in visited or current not in nodes:
            break
        visited.add(current)
        result_chain.append(current)
        current = nodes[current].get("parent")

    if len(result_chain) < 2:
        return ancestors_updated, capability_changes

    for anc_key in result_chain[1:]:  # skip self (index 0) — handled above
        anc_node = nodes.get(anc_key)
        if not anc_node:
            break

        children_keys = anc_node.get("children", [])
        if not children_keys:
            continue

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

        old_level = anc_node.get("capability_level", "EXPLORE")
        new_level = "EXPLORE"
        for level_name, threshold in levels_sorted:
            if new_confidence >= threshold:
                new_level = level_name

        capability_changed = old_level != new_level

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

        if old_confidence is not None and old_confidence == new_confidence:
            break

    return ancestors_updated, capability_changes


def cmd_propagate(args):
    """Propagate confidence up parent chain from a node.

    CRITICAL — DO NOT split this back into read_tree() ... write_tree(tree).
    Propagation walks ancestor children to compute averages; running the
    walk OUTSIDE the lock means a concurrent cmd_set on a sibling can
    invalidate the average between read and write. locked_modify_yaml
    holds the lock across the whole walk. See _fileops.py:638.
    """
    key = args.propagate

    from _fileops import locked_modify_yaml

    captured = {"ancestors_updated": [], "capability_changes": []}

    def _do_propagate(tree):
        if not isinstance(tree, dict) or "nodes" not in tree:
            print("Invalid tree file: missing 'nodes' key", file=sys.stderr)
            sys.exit(1)
        nodes = tree["nodes"]
        competence = _load_competence_config()
        ancestors_updated, capability_changes = _propagate_in_memory(nodes, key, competence)
        captured["ancestors_updated"] = ancestors_updated
        captured["capability_changes"] = capability_changes
        tree["nodes"] = nodes
        tree["last_updated"] = date.today().isoformat()
        return tree

    locked_modify_yaml(TREE_PATH, _do_propagate)

    result = {
        "source_node": key,
        "ancestors_updated": captured["ancestors_updated"],
        "capability_changes": captured["capability_changes"],
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_reconcile_capabilities(args):
    """Batch reconciliation: recompute capability_level for every node from
    its confidence against the competence_mapping thresholds. One-shot
    cleanup for historical debt — nodes whose capability_level was never
    re-evaluated after confidence writes (see rb-228, g-115-37). After the
    self-graduation fix in _propagate_in_memory, this should be idempotent
    and only show changes the first time it runs.

    CRITICAL — DO NOT split this back into read_tree() ... write_tree(tree).
    The whole-tree walk must run inside the lock; otherwise a concurrent
    cmd_set on a node's confidence can land BETWEEN this walk's read and
    write, and the walk's stale snapshot will overwrite the cmd_set value.
    See _fileops.py:638.
    """
    from _fileops import locked_modify_yaml

    captured = {"changes": [], "total_nodes": 0}

    def _do_reconcile(tree):
        if not isinstance(tree, dict) or "nodes" not in tree:
            print("Invalid tree file: missing 'nodes' key", file=sys.stderr)
            sys.exit(1)
        nodes = tree["nodes"]
        competence = _load_competence_config()

        changes = []
        for key, node in nodes.items():
            old_level, new_level = _graduate_node_level(node, competence)
            if old_level is not None:
                changes.append({
                    "key": key,
                    "old_level": old_level,
                    "new_level": new_level,
                    "confidence": node.get("confidence"),
                })

        captured["changes"] = changes
        captured["total_nodes"] = len(nodes)
        tree["nodes"] = nodes
        tree["last_updated"] = date.today().isoformat()
        return tree

    locked_modify_yaml(TREE_PATH, _do_reconcile)

    result = {
        "reconciled": len(captured["changes"]),
        "total_nodes": captured["total_nodes"],
        "changes": captured["changes"],
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_reparent(args):
    """Reparent a node: move it from its current parent to a new parent.

    Atomically updates _tree.yaml: removes from old parent, adds to new parent,
    recomputes file paths and depths for the entire subtree, propagates confidence
    to both old and new parent chains. Physical file moves are NOT performed —
    they are reported in the output for the LLM to execute.

    CRITICAL — DO NOT split this back into read_tree() ... write_tree(tree).
    Reparent has more invariants per write than any other tree mutation
    (circular check, depth check, dual-chain propagation, subtree
    file-path recomputation). Running these checks OUTSIDE the lock means
    any concurrent writer between the validation and the write can
    invalidate a check the validation thought it had nailed down.
    locked_modify_yaml holds the lock across the whole sequence. See
    _fileops.py:638.
    """
    node_key, new_parent_key = args.reparent

    from _fileops import locked_modify_yaml

    captured = {
        "file_moves": [], "old_parent_key": None, "new_depth": None,
        "old_ancestors": [], "old_cap": [], "new_ancestors": [], "new_cap": [],
    }

    def _do_reparent(tree):
        if not isinstance(tree, dict) or "nodes" not in tree:
            print("Invalid tree file: missing 'nodes' key", file=sys.stderr)
            sys.exit(1)
        nodes = tree["nodes"]

        # --- Validation ---
        if node_key not in nodes:
            print("Node not found: " + node_key, file=sys.stderr)
            sys.exit(1)
        if new_parent_key not in nodes:
            print("New parent not found: " + new_parent_key, file=sys.stderr)
            sys.exit(1)
        if node_key == "root":
            print("Cannot reparent root node", file=sys.stderr)
            sys.exit(1)
        if node_key == new_parent_key:
            print("Cannot reparent a node to itself", file=sys.stderr)
            sys.exit(1)

        node = nodes[node_key]
        old_parent_key = node.get("parent")
        if old_parent_key is None:
            print("Node '{}' has no parent (is it root?)".format(node_key), file=sys.stderr)
            sys.exit(1)
        if old_parent_key not in nodes:
            print("Old parent '{}' not found in tree".format(old_parent_key), file=sys.stderr)
            sys.exit(1)
        if new_parent_key == old_parent_key:
            print("Node '{}' is already a child of '{}'".format(node_key, new_parent_key), file=sys.stderr)
            sys.exit(1)

        # Circular check: new parent must not be a descendant of node
        descendants = set()
        stack = [node_key]
        while stack:
            cur = stack.pop()
            for child in nodes.get(cur, {}).get("children", []):
                if child not in descendants:
                    descendants.add(child)
                    stack.append(child)
        if new_parent_key in descendants:
            print("Cannot reparent: '{}' is a descendant of '{}'".format(new_parent_key, node_key), file=sys.stderr)
            sys.exit(1)

        # Depth check
        d_max = _config_d_max()
        new_parent_depth = nodes[new_parent_key].get("depth", 0)

        def max_subtree_depth(key, nodes_dict):
            """Return the max depth offset below key (0 if leaf)."""
            children = nodes_dict.get(key, {}).get("children", [])
            if not children:
                return 0
            return 1 + max(max_subtree_depth(c, nodes_dict) for c in children)

        subtree_height = max_subtree_depth(node_key, nodes)
        new_max_depth = new_parent_depth + 1 + subtree_height
        if new_max_depth > d_max:
            print("Reparent would exceed D_max={}: deepest descendant would be at depth {}".format(
                d_max, new_max_depth), file=sys.stderr)
            sys.exit(1)

        # --- Execute reparent ---
        old_parent = nodes[old_parent_key]
        old_children = old_parent.get("children", [])
        if node_key in old_children:
            old_children.remove(node_key)
        old_parent["children"] = old_children
        old_parent["child_count"] = len(old_children)
        if not old_children:
            old_parent["node_type"] = "leaf"

        new_parent = nodes[new_parent_key]
        if "children" not in new_parent:
            new_parent["children"] = []
        new_parent["children"].append(node_key)
        new_parent["child_count"] = len(new_parent["children"])
        if new_parent.get("node_type") == "leaf":
            new_parent["node_type"] = "interior"

        node["parent"] = new_parent_key

        file_moves = []

        def recompute_subtree(key, parent_file, parent_depth):
            n = nodes[key]
            old_file = n.get("file", "")
            new_depth = parent_depth + 1
            new_file = compute_child_path(parent_file, key)
            if old_file != new_file:
                file_moves.append({"key": key, "old": old_file, "new": new_file})
            n["file"] = new_file
            n["depth"] = new_depth
            for child_key in n.get("children", []):
                recompute_subtree(child_key, new_file, new_depth)

        recompute_subtree(node_key, new_parent.get("file", ""), new_parent_depth)

        competence = _load_competence_config()
        new_ancestors, new_cap = _propagate_in_memory(nodes, node_key, competence)
        old_ancestors, old_cap = _propagate_in_memory(nodes, old_parent_key, competence)

        captured["file_moves"] = file_moves
        captured["old_parent_key"] = old_parent_key
        captured["new_depth"] = nodes[node_key].get("depth")
        captured["old_ancestors"] = old_ancestors
        captured["old_cap"] = old_cap
        captured["new_ancestors"] = new_ancestors
        captured["new_cap"] = new_cap

        tree["nodes"] = nodes
        tree["last_updated"] = date.today().isoformat()
        return tree

    locked_modify_yaml(TREE_PATH, _do_reparent)

    # S9: log the L1 pick for this reparent (fail-open). Crosses-L1
    # reparents are the highest-signal events in the log — they directly
    # change which L1 a subtree contributes to.
    _log_l1_pick_for_key(
        node_key,
        decision_type="reparent",
        source=getattr(args, "encoding_source", None),
        reason=getattr(args, "encoding_reason", None),
    )

    result = {
        "reparented": node_key,
        "old_parent": captured["old_parent_key"],
        "new_parent": new_parent_key,
        "new_depth": captured["new_depth"],
        "file_moves": captured["file_moves"],
        "old_chain_propagation": {
            "ancestors_updated": captured["old_ancestors"],
            "capability_changes": captured["old_cap"],
        },
        "new_chain_propagation": {
            "ancestors_updated": captured["new_ancestors"],
            "capability_changes": captured["new_cap"],
        },
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
    read_group.add_argument("--maintenance", action="store_true",
                            help="Top-level maintenance cadence block from _tree.yaml (single source of truth)")
    read_group.add_argument("--summary", action="store_true",
                            help="Compact tree overview: keys, summaries, depth, capability, confidence, children keys")
    p_read.add_argument("--threshold", type=int, default=None,
                        help="Line count threshold for --decompose-candidates / --redistribute-candidates (default: 80)")
    p_read.add_argument("--top", type=int, default=3,
                        help="Max results for --find (default: 3)")
    p_read.add_argument("--leaf-only", action="store_true",
                        help="Restrict --find to leaf nodes only")
    p_read.add_argument("--include-skipped", action="store_true",
                        dest="include_skipped",
                        help=("Use with --decompose-candidates / --distill-candidates / "
                              "--redistribute-candidates. Returns {candidates, skipped} "
                              "where skipped lists {node_key, skip_reason} for each "
                              "pre-filter rejection. Feeds /tree maintain instrumentation."))
    p_read.add_argument("--by-l1", action="store_true", dest="by_l1",
                        help=("Use with --stats. Adds per-L1 breakdown: structural "
                              "mass, retrieval volume, utility aggregates, mean "
                              "confidence, capability-level distribution. Feeds "
                              "l1-skew-check.py and /fresh-eyes-tree (S1)."))

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
    update_group.add_argument("--reconcile-capabilities", action="store_true",
                              dest="reconcile_capabilities",
                              help="One-shot: recompute capability_level for every node from its confidence vs competence_mapping thresholds")
    update_group.add_argument("--reparent", nargs=2, metavar=("NODE", "NEW_PARENT"),
                              help="Move node to new parent (YAML only; reports file moves)")
    update_group.add_argument("--record-maintenance", action="store_true",
                              dest="record_maintenance",
                              help="Record /tree maintain completion in the top-level maintenance block")
    p_update.add_argument("--backlog-mode", action="store_true",
                          dest="backlog_mode",
                          help="Use with --record-maintenance: also set last_backlog_mode_at (caller indicates backlog mode was used)")
    p_update.add_argument("--stop-mode", action="store_true",
                          dest="stop_mode",
                          help="Use with --record-maintenance: also set last_stop_mode_at (caller indicates stop-mode small-budget caps were used; see tree.yaml stop_mode_caps)")
    p_update.add_argument("--with-run-record", action="store_true",
                          dest="with_run_record",
                          help=("Use with --record-maintenance: read LLM-reported "
                                "action-phase counts from stdin JSON and append "
                                "a full run record to world/tree-maintenance-log.jsonl. "
                                "Expected stdin shape: "
                                "{mode, started_at, decompose:{actioned, skipped_by_reason, ...}, "
                                "distill:{...}, redistribute:{...}, split:{...}, ...}."))
    p_update.add_argument("--no-dedup", action="store_true",
                          help="Bypass the pre-create duplicate gate (use for trusted batch ops only)")
    p_update.add_argument("--accept-overflow", type=str, metavar="JUSTIFICATION",
                          dest="accept_overflow",
                          help="Acknowledge child-cap violation and write a tree-debt entry. Required text is free-form; reflect-maintain will surface the entry as an actionable grooming task.")
    p_update.add_argument("--encoding-source", type=str, default=None,
                          dest="encoding_source",
                          help=("S9: optional attribution string for the L1 pick log "
                                "(e.g., 'aspirations-state-update-step-8', "
                                "'encode-session-lane-1', 'tree-maintain-sprout'). "
                                "Used by --add-child / --batch / --reparent. "
                                "Auto-logged regardless; this enriches the entry."))
    p_update.add_argument("--encoding-reason", type=str, default=None,
                          dest="encoding_reason",
                          help=("S9: optional one-line reason for the L1 pick log "
                                "(e.g., 'new audit-pattern node clusters under system'). "
                                "Used alongside --encoding-source for richer signal."))

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
