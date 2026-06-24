#!/usr/bin/env python3
"""Cross-domain meta-strategy transfer.

Subcommands:
  export — export transferable strategies as a YAML bundle
  import — import strategies from a bundle into current meta/
"""

import argparse
import json
import sys
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

from _paths import META_DIR, AGENT_DIR


def read_yaml(path):
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if data is not None else {}


def write_yaml(path, data):
    """Atomically write YAML with locking and history."""
    from _fileops import locked_write_yaml
    locked_write_yaml(path, data)


def cmd_export(args):
    """Export transferable strategies as a YAML bundle."""
    goal_sel = read_yaml(META_DIR / "goal-selection-strategy.yaml")
    reflection = read_yaml(META_DIR / "reflection-strategy.yaml")
    encoding = read_yaml(META_DIR / "encoding-strategy.yaml")
    meta_state = read_yaml(META_DIR / "meta.yaml")

    # Read Self for provenance
    self_path = AGENT_DIR / "self.md" if AGENT_DIR else None
    self_name = "unknown"
    if self_path and self_path.exists():
        with open(self_path, "r", encoding="utf-8") as f:
            content = f.read()
        for line in content.split("\n"):
            if line.startswith("name:"):
                self_name = line.split(":", 1)[1].strip().strip('"')
                break

    bundle = {
        "exported": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "source_agent": self_name,
        "total_goals_at_export": meta_state.get("total_meta_changes", 0),
        "strategies": {
            "goal_selection": {
                "weights": goal_sel.get("weights", {}),
                "selection_heuristics": goal_sel.get("selection_heuristics", []),
            },
            "reflection": {
                "depth_allocation": reflection.get("depth_allocation", {}),
                "trigger_overrides": reflection.get("trigger_overrides", []),
            },
            "encoding": {
                "priority_rules": encoding.get("priority_rules", []),
            },
        },
    }

    output_path = args.output
    with open(output_path, "w", encoding="utf-8") as f:
        yaml.dump(bundle, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    # Register in transfer index
    index = read_yaml(META_DIR / "transfer" / "_index.yaml")
    bundles = index.get("bundles", [])
    bundles.append({
        "path": output_path,
        "exported": bundle["exported"],
        "source": self_name,
    })
    index["bundles"] = bundles
    write_yaml(META_DIR / "transfer" / "_index.yaml", index)

    print(json.dumps({"status": "exported", "path": output_path, "strategies": list(bundle["strategies"].keys())}))


def cmd_import(args):
    """Import strategies from a bundle."""
    with open(args.input, "r", encoding="utf-8") as f:
        bundle = yaml.safe_load(f)

    strategies = bundle.get("strategies", {})
    changes = []

    if args.dry_run:
        for strategy_name, data in strategies.items():
            changes.append({"strategy": strategy_name, "fields": list(data.keys()), "action": "would_merge"})
        print(json.dumps({"dry_run": True, "changes": changes}))
        return

    # Merge goal selection weights
    if "goal_selection" in strategies:
        gs = read_yaml(META_DIR / "goal-selection-strategy.yaml")
        imported = strategies["goal_selection"]
        if "weights" in imported:
            for k, v in imported["weights"].items():
                if k in gs.get("weights", {}):
                    gs["weights"][k] = max(0.0, min(3.0, float(v)))
                    changes.append({"field": f"weights.{k}", "value": gs["weights"][k], "source": "transfer"})
        if "selection_heuristics" in imported:
            existing = gs.get("selection_heuristics", [])
            for h in imported["selection_heuristics"]:
                h["source"] = f"transfer from {bundle.get('source_agent', 'unknown')}"
                existing.append(h)
            gs["selection_heuristics"] = existing
        write_yaml(META_DIR / "goal-selection-strategy.yaml", gs)

    # Merge reflection
    if "reflection" in strategies:
        ref = read_yaml(META_DIR / "reflection-strategy.yaml")
        imported = strategies["reflection"]
        if "depth_allocation" in imported:
            ref["depth_allocation"] = imported["depth_allocation"]
            changes.append({"field": "depth_allocation", "value": ref["depth_allocation"], "source": "transfer"})
        if "trigger_overrides" in imported:
            existing = ref.get("trigger_overrides", [])
            for t in imported["trigger_overrides"]:
                t["source"] = f"transfer from {bundle.get('source_agent', 'unknown')}"
                existing.append(t)
            ref["trigger_overrides"] = existing
        write_yaml(META_DIR / "reflection-strategy.yaml", ref)

    # Merge encoding
    if "encoding" in strategies:
        enc = read_yaml(META_DIR / "encoding-strategy.yaml")
        imported = strategies["encoding"]
        if "priority_rules" in imported:
            existing = enc.get("priority_rules", [])
            for r in imported["priority_rules"]:
                r["source"] = f"transfer from {bundle.get('source_agent', 'unknown')}"
                existing.append(r)
            enc["priority_rules"] = existing
        write_yaml(META_DIR / "encoding-strategy.yaml", enc)

    print(json.dumps({"status": "imported", "changes": len(changes), "details": changes}))


def build_parser():
    parser = argparse.ArgumentParser(description="Meta-strategy transfer")
    sub = parser.add_subparsers(dest="command", required=True)

    p_export = sub.add_parser("export", help="Export strategies")
    p_export.add_argument("--output", required=True)

    p_import = sub.add_parser("import", help="Import strategies")
    p_import.add_argument("--input", required=True)
    p_import.add_argument("--dry-run", action="store_true")

    return parser


DISPATCH = {
    "export": cmd_export,
    "import": cmd_import,
}


def main():
    parser = build_parser()
    args = parser.parse_args()
    DISPATCH[args.command](args)


if __name__ == "__main__":
    main()
