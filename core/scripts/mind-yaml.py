#!/usr/bin/env python3
"""Generic YAML store for agent state files.

All shell scripts are thin wrappers around this. Subcommands managed via argparse.

Provides field-level read/write/increment/append for any YAML file in the agent directory.
File paths are relative to the agent directory and validated to prevent traversal.
"""

import argparse
import json
import sys
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

from _paths import AGENT_DIR


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def resolve_path(rel_path):
    """Resolve a path relative to the agent directory, rejecting traversal outside it."""
    target = (AGENT_DIR / rel_path).resolve()
    if not target.is_relative_to(AGENT_DIR.resolve()):
        print(f"ERROR: Path '{rel_path}' resolves outside {AGENT_DIR.name}/", file=sys.stderr)
        sys.exit(1)
    return target


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


def navigate(data, dotpath):
    """Navigate a nested dict/list by dot-separated path.

    Returns (parent, key) where parent[key] is the target value.
    Numeric path segments index into lists.
    Creates intermediate dicts as needed for set operations.
    """
    parts = dotpath.split(".")
    current = data
    for i, part in enumerate(parts[:-1]):
        if isinstance(current, list):
            idx = int(part)  # crashes on non-int — that's correct (no fallback)
            current = current[idx]
        elif isinstance(current, dict):
            if part not in current:
                # Auto-create intermediate dicts for set operations
                current[part] = {}
            current = current[part]
        else:
            print(f"ERROR: Cannot navigate into {type(current).__name__} at '{'.'.join(parts[:i+1])}'", file=sys.stderr)
            sys.exit(1)

    final_key = parts[-1]
    if isinstance(current, list):
        final_key = int(final_key)
    return current, final_key


def parse_value(raw):
    """Auto-detect type from string: int, float, bool, null, or string."""
    if raw == "null":
        return None
    if raw == "true":
        return True
    if raw == "false":
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_read(args):
    """Read a YAML file or a specific field."""
    path = resolve_path(args.file)
    data = read_yaml(path)

    if args.field:
        parent, key = navigate(data, args.field)
        try:
            value = parent[key]
        except (KeyError, IndexError):
            print(f"ERROR: Field '{args.field}' not found", file=sys.stderr)
            sys.exit(1)
        if args.json:
            print(json.dumps(value, ensure_ascii=False, default=str))
        else:
            if isinstance(value, (dict, list)):
                yaml.dump(value, sys.stdout, default_flow_style=False, allow_unicode=True, sort_keys=False)
            else:
                print(value)
    else:
        if args.json:
            print(json.dumps(data, ensure_ascii=False, default=str))
        else:
            yaml.dump(data, sys.stdout, default_flow_style=False, allow_unicode=True, sort_keys=False)


def cmd_set(args):
    """Set a scalar field value."""
    path = resolve_path(args.file)
    data = read_yaml(path)
    value = args.value if args.string else parse_value(args.value)

    parent, key = navigate(data, args.dotpath)
    parent[key] = value
    write_yaml(path, data)


def cmd_increment(args):
    """Increment a numeric field."""
    path = resolve_path(args.file)
    data = read_yaml(path)

    parent, key = navigate(data, args.dotpath)
    current = parent.get(key, 0) if isinstance(parent, dict) else parent[key]
    # No fallback — non-numeric crashes here (fail-open upstream)
    parent[key] = current + 1
    write_yaml(path, data)
    print(parent[key])


def cmd_append(args):
    """Append a JSON object from stdin to an array field."""
    if sys.stdin.isatty():
        print("Error: expected JSON on stdin (not a terminal)", file=sys.stderr)
        sys.exit(1)
    path = resolve_path(args.file)
    data = read_yaml(path)

    raw = sys.stdin.read().strip()
    item = json.loads(raw)

    parent, key = navigate(data, args.dotpath)
    arr = parent.get(key) if isinstance(parent, dict) else parent[key]
    if arr is None:
        parent[key] = []
        arr = parent[key]
    if not isinstance(arr, list):
        print(f"ERROR: Field '{args.dotpath}' is {type(arr).__name__}, not a list", file=sys.stderr)
        sys.exit(1)
    arr.append(item)
    write_yaml(path, data)


def cmd_write(args):
    """Full file replacement from stdin (JSON or YAML)."""
    if sys.stdin.isatty():
        print("Error: expected JSON on stdin (not a terminal)", file=sys.stderr)
        sys.exit(1)
    path = resolve_path(args.file)
    raw = sys.stdin.read()

    # Try JSON first, then YAML
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = yaml.safe_load(raw)
        if data is None:
            data = {}

    write_yaml(path, data)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description="Generic YAML store for agent state files")
    sub = parser.add_subparsers(dest="command", required=True)

    # --- read ---
    p_read = sub.add_parser("read", help="Read file or field")
    p_read.add_argument("file", help="File path relative to agent directory")
    p_read.add_argument("--field", help="Dot-notation path to a specific field")
    p_read.add_argument("--json", action="store_true", help="Output as JSON instead of YAML")

    # --- set ---
    p_set = sub.add_parser("set", help="Set a scalar field")
    p_set.add_argument("file", help="File path relative to agent directory")
    p_set.add_argument("dotpath", help="Dot-notation path to field")
    p_set.add_argument("value", help="Value to set (auto-detects type)")
    p_set.add_argument("--string", action="store_true", help="Force value as string (no type detection)")

    # --- increment ---
    p_inc = sub.add_parser("increment", help="Increment a numeric field")
    p_inc.add_argument("file", help="File path relative to agent directory")
    p_inc.add_argument("dotpath", help="Dot-notation path to numeric field")

    # --- append ---
    p_app = sub.add_parser("append", help="Append JSON from stdin to an array field")
    p_app.add_argument("file", help="File path relative to agent directory")
    p_app.add_argument("dotpath", help="Dot-notation path to array field")

    # --- write ---
    p_write = sub.add_parser("write", help="Full file replacement from stdin")
    p_write.add_argument("file", help="File path relative to agent directory")

    return parser


DISPATCH = {
    "read": cmd_read,
    "set": cmd_set,
    "increment": cmd_increment,
    "append": cmd_append,
    "write": cmd_write,
}


def main():
    parser = build_parser()
    args = parser.parse_args()
    DISPATCH[args.command](args)


if __name__ == "__main__":
    main()
