#!/usr/bin/env python3
"""Skill relation graph operations.

Manages a graph of relations between skills: similar_to, compose_with,
belong_to, depend_on. Base relations live in core/config/skill-relations.yaml
(immutable). Forged relations and co-invocation logs live in
world/skill-relations.yaml (mutable, shared across agents).

Subcommands:
  read      — Read and filter skill relations
  add       — Add a forged relation (JSON from stdin)
  co-invoke — Log skill co-invocation for a goal
  discover  — Propose new compose_with relations from co-invocation patterns
"""

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from itertools import combinations

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

from _paths import CONFIG_DIR, WORLD_DIR

BASE_RELATIONS_PATH = CONFIG_DIR / "skill-relations.yaml"
WORLD_RELATIONS_PATH = WORLD_DIR / "skill-relations.yaml"

VALID_TYPES = {"similar_to", "compose_with", "belong_to", "depend_on"}


def _load_config():
    """Load config section from core/config/skill-relations.yaml (cached)."""
    if not hasattr(_load_config, "_cache"):
        data = {}
        if BASE_RELATIONS_PATH.exists():
            with open(BASE_RELATIONS_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        _load_config._cache = data.get("config", {})
    return _load_config._cache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_yaml(path):
    """Read a YAML file, return parsed dict. Returns {} if missing."""
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if data is not None else {}


def write_yaml(path, data):
    """Atomically write data as YAML."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    tmp.replace(path)


def load_all_relations():
    """Load and merge base + forged relations into a combined list.

    Base relations come from core/config/skill-relations.yaml under 'relations'.
    Forged relations come from world/skill-relations.yaml under 'forged_relations'.
    Returns a list of relation dicts.
    """
    base = read_yaml(BASE_RELATIONS_PATH)
    world_data = read_yaml(WORLD_RELATIONS_PATH)

    base_relations = base.get("relations", [])
    if not isinstance(base_relations, list):
        base_relations = []

    forged_relations = world_data.get("forged_relations", [])
    if not isinstance(forged_relations, list):
        forged_relations = []

    return base_relations + forged_relations


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_read(args):
    """Read relations with optional filters."""
    relations = load_all_relations()

    # Apply filters
    if args.skill:
        relations = [r for r in relations
                     if r.get("source") == args.skill or r.get("target") == args.skill]

    if args.type:
        relations = [r for r in relations if r.get("type") == args.type]

    if args.composable:
        relations = [r for r in relations
                     if r.get("type") == "compose_with" and r.get("source") == args.composable]

    if args.similar:
        relations = [r for r in relations
                     if r.get("type") == "similar_to"
                     and (r.get("source") == args.similar or r.get("target") == args.similar)]

    print(json.dumps(relations, indent=2, ensure_ascii=False))


def cmd_add(args):
    """Add a forged relation from stdin JSON."""
    if sys.stdin.isatty():
        print("Error: expected JSON on stdin (not a terminal)", file=sys.stderr)
        sys.exit(1)

    raw = sys.stdin.read().strip()
    if not raw:
        print("Error: empty input on stdin", file=sys.stderr)
        sys.exit(1)

    try:
        entry = json.loads(raw)
    except json.JSONDecodeError as e:
        print("Error: invalid JSON on stdin: {}".format(e), file=sys.stderr)
        sys.exit(1)

    # Validate required fields
    for field in ("source", "target", "type"):
        if field not in entry:
            print("Error: missing required field '{}'".format(field), file=sys.stderr)
            sys.exit(1)

    rel_type = entry["type"]
    if rel_type not in VALID_TYPES:
        print("Error: invalid type '{}'. Must be one of: {}".format(
            rel_type, ", ".join(sorted(VALID_TYPES))), file=sys.stderr)
        sys.exit(1)

    source = entry["source"]
    target = entry["target"]

    # Load existing world relations
    world_data = read_yaml(WORLD_RELATIONS_PATH)
    forged = world_data.get("forged_relations", [])
    if not isinstance(forged, list):
        forged = []

    # Check for duplicates (same source + target + type)
    for existing in forged:
        if (existing.get("source") == source
                and existing.get("target") == target
                and existing.get("type") == rel_type):
            print("Error: duplicate relation already exists: {} --{}--> {}".format(
                source, rel_type, target), file=sys.stderr)
            sys.exit(1)

    # Build the relation record
    relation = {
        "source": source,
        "target": target,
        "type": rel_type,
    }
    if "confidence" in entry:
        relation["confidence"] = entry["confidence"]
    if "evidence" in entry:
        relation["evidence"] = entry["evidence"]

    forged.append(relation)
    world_data["forged_relations"] = forged
    write_yaml(WORLD_RELATIONS_PATH, world_data)

    print("Added relation: {} --{}--> {}".format(source, rel_type, target))


def cmd_co_invoke(args):
    """Log skill co-invocation for a goal."""
    goal_id = args.goal
    skills = [s.strip() for s in args.skills.split(",") if s.strip()]

    if len(skills) < 2:
        print("Error: co-invoke requires at least 2 skills", file=sys.stderr)
        sys.exit(1)

    world_data = read_yaml(WORLD_RELATIONS_PATH)
    log = world_data.get("co_invocation_log", [])
    if not isinstance(log, list):
        log = []

    entry = {
        "goal_id": goal_id,
        "skills": skills,
        "date": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }
    log.append(entry)

    # Single source of truth: core/config/skill-relations.yaml config.co_invocation_log_cap
    cap = _load_config().get("co_invocation_log_cap", 200)
    if len(log) > cap:
        log = log[-cap:]

    world_data["co_invocation_log"] = log
    write_yaml(WORLD_RELATIONS_PATH, world_data)

    print("Logged co-invocation: {} skills for goal {}".format(len(skills), goal_id))


def cmd_discover(args):
    """Analyze co-invocation log and propose new compose_with relations."""
    world_data = read_yaml(WORLD_RELATIONS_PATH)
    log = world_data.get("co_invocation_log", [])
    if not isinstance(log, list):
        log = []

    if not log:
        print(json.dumps([], indent=2, ensure_ascii=False))
        return

    # Count pair co-occurrences
    pair_counts = Counter()
    for entry in log:
        skills = entry.get("skills", [])
        if not isinstance(skills, list) or len(skills) < 2:
            continue
        # Sort each pair for consistent counting (A,B == B,A)
        for pair in combinations(sorted(set(skills)), 2):
            pair_counts[pair] += 1

    total_invocations = len(log)

    # Load existing relations to check for duplicates
    all_relations = load_all_relations()
    existing_compose = set()
    for r in all_relations:
        if r.get("type") == "compose_with":
            # Normalize: store both directions
            s, t = r.get("source", ""), r.get("target", "")
            existing_compose.add((min(s, t), max(s, t)))

    # Single source of truth: core/config/skill-relations.yaml config.discover_min_co_occurrences
    min_co = _load_config().get("discover_min_co_occurrences", 3)
    proposed = []
    for (skill_a, skill_b), count in pair_counts.most_common():
        if count < min_co:
            continue
        normalized = (min(skill_a, skill_b), max(skill_a, skill_b))
        if normalized in existing_compose:
            continue
        confidence = round(count / total_invocations, 3) if total_invocations > 0 else 0
        proposed.append({
            "source": skill_a,
            "target": skill_b,
            "type": "compose_with",
            "confidence": confidence,
            "co_invocation_count": count,
            "evidence": "Co-invoked {} times across {} logged invocations".format(
                count, total_invocations),
        })

    print(json.dumps(proposed, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Skill relation graph operations")
    sub = parser.add_subparsers(dest="command", required=True)

    p_read = sub.add_parser("read", help="Read skill relations")
    p_read.add_argument("--skill", help="Filter by skill name (source or target)")
    p_read.add_argument("--type", help="Filter by relation type")
    p_read.add_argument("--composable", help="Skills composable with given skill")
    p_read.add_argument("--similar", help="Skills similar to given skill")

    sub.add_parser("add", help="Add forged relation from stdin JSON")

    p_co = sub.add_parser("co-invoke", help="Log skill co-invocation")
    p_co.add_argument("--goal", required=True, help="Goal ID")
    p_co.add_argument("--skills", required=True, help="Comma-separated skill names")

    sub.add_parser("discover", help="Propose new relations from co-invocation patterns")

    args = parser.parse_args()
    cmds = {"read": cmd_read, "add": cmd_add, "co-invoke": cmd_co_invoke, "discover": cmd_discover}
    cmds[args.command](args)


if __name__ == "__main__":
    main()
