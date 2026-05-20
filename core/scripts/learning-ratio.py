#!/usr/bin/env python3
# domain-leak-exempt: _DEFAULT_NPC_MIN_RATIO names a category-balance threshold
# in domain terms because the dashboard's whole purpose is reporting domain-vs-
# framework learning distribution. Renaming away from "npc" would obscure the
# config's intent. Sister convention: domain-balance.md.
"""Learning-ratio dashboard — reports framework-meta vs domain learning distribution.

Reads guardrails.jsonl + reasoning-bank.jsonl, resolves each entry's category to a
domain_class via _tree.yaml L1/L2 nodes, and emits a one-line ratio suitable for
embedding in /boot or /backlog-report output.

Non-tree categories (e.g. framework-maintenance, npc-cognition) are resolved via
prefix heuristics as a fallback, since not every category matches a tree node.

Plan: domain-rebalance 2026-04-18.
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SELF_DIR = Path(__file__).resolve().parent
_rb_spec = importlib.util.spec_from_file_location("rb", SELF_DIR / "reasoning-bank.py")
_rb = importlib.util.module_from_spec(_rb_spec); _rb_spec.loader.exec_module(_rb)

import yaml  # noqa: E402

from _paths import AGENT_DIR, CORE_ROOT, WORLD_DIR  # noqa: E402

TREE_PATH = WORLD_DIR / "knowledge" / "tree" / "_tree.yaml"
ASPIRATIONS_CONFIG = CORE_ROOT / "config" / "aspirations.yaml"

# Defaults used when aspirations.yaml is missing/unparseable. Kept in sync
# with modifiable defaults in aspirations.yaml — the user-editable knobs are
# the source of truth; these are the fallback so the dashboard stays
# functional without the config file (fail-open).
_DEFAULT_FRAMEWORK_MAX_RATIO = 0.25
_DEFAULT_DOMAIN_MIN_RATIO = 0.30


def load_targets():
    """Load domain_class_targets from aspirations.yaml:modifiable.
    Returns (framework_max_pct, domain_min_pct) as integer percentages.
    Fail-open to defaults on any read/parse error.

    Tolerates the legacy key `domain_class_targets.npc_min_ratio` for one
    cycle after the 2026-05-18 rename to `domain_min_ratio` (packaging
    plan Phase 2.7). Either key is accepted; new key wins if both present."""
    fw = _DEFAULT_FRAMEWORK_MAX_RATIO
    npc = _DEFAULT_DOMAIN_MIN_RATIO
    try:
        with open(ASPIRATIONS_CONFIG, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        mod = cfg.get("modifiable") or {}
        fw_entry = mod.get("domain_class_targets.framework_max_ratio") or {}
        # New key (preferred); fall back to legacy npc_min_ratio for one
        # cycle so any cached config / external override doesn't break.
        npc_entry = (mod.get("domain_class_targets.domain_min_ratio")
                     or mod.get("domain_class_targets.npc_min_ratio")
                     or {})
        if isinstance(fw_entry, dict) and "default" in fw_entry:
            fw = float(fw_entry["default"])
        if isinstance(npc_entry, dict) and "default" in npc_entry:
            npc = float(npc_entry["default"])
    except (OSError, yaml.YAMLError, TypeError, ValueError):
        pass
    return round(fw * 100), round(npc * 100)

PREFIX_FALLBACK = [
    ("framework-", "framework-meta"),
    ("session", "framework-meta"),
    ("hook-", "framework-meta"),
    ("agent-capab", "framework-meta"),
    ("agent-method", "framework-meta"),
    ("agent-health", "framework-meta"),
    ("agent-performance", "framework-meta"),
    ("cognitive-core", "framework-meta"),
    ("knowledge-encoding", "framework-meta"),
    ("skill-authoring", "framework-meta"),
    ("hypothesis-accuracy", "framework-meta"),
    ("self-improvement", "framework-meta"),
    ("npc-", "npc-domain"),
    ("processor-", "npc-domain"),
    ("dave-mark", "npc-domain"),
    ("research-analyst", "npc-domain"),
    ("nvidia-ace", "npc-domain"),
    ("roblox-npc", "npc-domain"),
    ("game-ai-monet", "npc-domain"),
    ("game-quality", "npc-domain"),
    ("composition-failure", "npc-domain"),
    ("audit-and-scaling", "npc-domain"),
    ("cell-archive", "npc-domain"),
    ("player-behavior", "npc-domain"),
    ("ayoai-", "ayoai-product"),
    ("lambda-", "ayoai-product"),
    ("roblox-", "ayoai-product"),   # after roblox-npc catch above
    ("infrastructure", "ayoai-product"),
    ("code", "ayoai-product"),
    ("data-", "ayoai-product"),
    ("reasoning-discipline", "other"),
    ("cross-platform", "other"),
    ("system-behavior", "other"),
    ("visionary-", "other"),
    ("indie-", "other"),
]


def load_tree_classes():
    """Read _tree.yaml and build {node_key: domain_class} for tagged nodes."""
    if not TREE_PATH.exists():
        return {}
    with open(TREE_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    out = {}
    for key, node in (data or {}).get("nodes", {}).items():
        dc = node.get("domain_class")
        if dc:
            out[key] = dc
    return out


def classify(category, tree_classes):
    if not category:
        return "other"
    if category in tree_classes:
        cls = tree_classes[category]
        return cls if cls != "mixed" else "other"
    for prefix, cls in PREFIX_FALLBACK:
        if category.startswith(prefix):
            return cls
    return "other"


def count_store(path, tree_classes):
    counts = {"framework-meta": 0, "ayoai-product": 0, "npc-domain": 0, "other": 0}
    for rec in _rb.read_jsonl(path):
        if rec.get("status") != "active":
            continue
        counts[classify(rec.get("category", ""), tree_classes)] += 1
    return counts


def count_goals(tree_classes):
    """Count active + in-progress goals across world + agent aspirations.jsonl,
    classified by domain_class. Returns {counts_dict, per_source_dict}."""
    counts = {"framework-meta": 0, "ayoai-product": 0, "npc-domain": 0, "other": 0}
    per_source = {}
    active_statuses = {"pending", "in-progress"}
    sources = []
    if WORLD_DIR:
        sources.append(("world", Path(WORLD_DIR) / "aspirations.jsonl"))
    if AGENT_DIR:
        sources.append(("agent", Path(AGENT_DIR) / "aspirations.jsonl"))
    for label, path in sources:
        if not path.exists():
            per_source[label] = {k: 0 for k in counts}
            continue
        src_counts = {"framework-meta": 0, "ayoai-product": 0, "npc-domain": 0, "other": 0}
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        asp = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if asp.get("status") not in ("active", None):
                        continue
                    for g in asp.get("goals", []):
                        if g.get("status") not in active_statuses:
                            continue
                        cat = g.get("category", "") or ""
                        src_counts[classify(cat, tree_classes)] += 1
        except OSError:
            pass
        per_source[label] = src_counts
        for k, v in src_counts.items():
            counts[k] += v
    return counts, per_source


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Learning-ratio dashboard — framework-meta vs domain distribution.")
    ap.add_argument("--scope", choices=["artifacts", "goals"], default="artifacts",
                    help="artifacts: active guardrails+reasoning-bank (default); "
                         "goals: pending+in-progress goals across world+agent aspirations")
    ap.add_argument("--json", action="store_true",
                    help="emit JSON detail after the one-line ratio")
    args = ap.parse_args(argv)

    tree_classes = load_tree_classes()
    fw_max, npc_min = load_targets()

    if args.scope == "artifacts":
        totals = {"framework-meta": 0, "ayoai-product": 0, "npc-domain": 0, "other": 0}
        per_store = {}
        for label, path in [("guardrails", _rb.GUARD_PATH),
                            ("reasoning_bank", _rb.RB_PATH)]:
            c = count_store(path, tree_classes)
            per_store[label] = c
            for k, v in c.items():
                totals[k] += v
        scope_label = "Learning ratio (artifacts)"
        detail = {"totals": totals, "per_store": per_store}
    else:  # goals
        totals, per_source = count_goals(tree_classes)
        scope_label = "Learning ratio (goals)"
        detail = {"totals": totals, "per_source": per_source}

    total = sum(totals.values()) or 1
    pct = {k: round(100 * v / total) for k, v in totals.items()}
    print(f"{scope_label}: framework-meta {pct['framework-meta']}% · "
          f"ayoai-product {pct['ayoai-product']}% · "
          f"npc-domain {pct['npc-domain']}% · "
          f"other {pct['other']}%  "
          f"(target: framework ≤ {fw_max}%, npc ≥ {npc_min}%)")
    if args.json:
        detail["pct"] = pct
        print(json.dumps(detail, indent=2))


if __name__ == "__main__":
    main()
