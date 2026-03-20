#!/usr/bin/env python3
"""Secrets & credentials engine for .env.example / .env.local management.

All shell scripts are thin wrappers around this. Subcommands managed via argparse.
"""

import argparse
import json
import re
import sys
from pathlib import Path

# Ensure stdout/stderr handle unicode on all platforms (Windows cp1252 fix)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import PROJECT_ROOT

EXAMPLE_PATH = PROJECT_ROOT / ".env.example"
LOCAL_PATH = PROJECT_ROOT / ".env.local"

# Category header pattern: # --- Category Name ---
CATEGORY_RE = re.compile(r"^#\s*---\s*(.+?)\s*---\s*$")
# Key entry pattern: # KEY=  or # KEY=  # description  or KEY=value
COMMENTED_KEY_RE = re.compile(r"^#\s*([A-Z][A-Z0-9_]*)=\s*(?:#\s*(.*))?$")
ACTIVE_KEY_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


def parse_example():
    """Parse .env.example into a list of {key, description, category}."""
    if not EXAMPLE_PATH.exists():
        return []

    entries = []
    current_category = "Uncategorized"

    with open(EXAMPLE_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\r\n")

            # Category header
            m = CATEGORY_RE.match(line)
            if m:
                current_category = m.group(1).strip()
                continue

            # Commented key entry: # KEY=  or # KEY=  # description
            m = COMMENTED_KEY_RE.match(line)
            if m:
                key = m.group(1)
                desc = (m.group(2) or "").strip()
                entries.append({
                    "key": key,
                    "description": desc,
                    "category": current_category,
                })
                continue

            # Active key entry (uncommented in example — rare but possible)
            m = ACTIVE_KEY_RE.match(line)
            if m:
                key = m.group(1)
                entries.append({
                    "key": key,
                    "description": "",
                    "category": current_category,
                })
                continue

    return entries


def parse_local():
    """Parse .env.local into a dict of {KEY: value}."""
    if not LOCAL_PATH.exists():
        return {}

    values = {}
    with open(LOCAL_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\r\n")

            # Skip comments and blank lines
            if not line or line.startswith("#"):
                continue

            m = ACTIVE_KEY_RE.match(line)
            if m:
                key = m.group(1)
                val = m.group(2).strip()
                # Strip surrounding quotes
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                    val = val[1:-1]
                values[key] = val

    return values


# --- Subcommands ---

def cmd_status(_args):
    """All keys from .env.example with present/missing status."""
    entries = parse_example()
    local = parse_local()

    result = []
    for entry in entries:
        key = entry["key"]
        val = local.get(key, "")
        result.append({
            "key": key,
            "description": entry["description"],
            "category": entry["category"],
            "present": bool(val),
        })

    print(json.dumps(result, indent=2))


def cmd_missing(_args):
    """Keys in example but missing/empty in local."""
    entries = parse_example()
    local = parse_local()

    missing = [e["key"] for e in entries if not local.get(e["key"], "")]
    print(json.dumps(missing))


def cmd_has(args):
    """Check if key exists and is non-empty."""
    local = parse_local()
    val = local.get(args.key, "")
    if val:
        print("true")
        sys.exit(0)
    else:
        print("false")
        sys.exit(1)


def cmd_value(args):
    """Return raw value for a key."""
    local = parse_local()
    val = local.get(args.key, "")
    if val:
        print(val)
        sys.exit(0)
    else:
        print(f"ERROR: Key {args.key} not found or empty in .env.local", file=sys.stderr)
        sys.exit(1)


def cmd_register(args):
    """Add key entry to .env.example (idempotent)."""
    # Check if key already exists
    entries = parse_example()
    for entry in entries:
        if entry["key"] == args.key:
            print(f"Key {args.key} already registered in .env.example")
            sys.exit(0)

    # Append to .env.example under # --- Custom --- section or at end
    desc_comment = f"  # {args.description}" if args.description else ""
    new_line = f"# {args.key}={desc_comment}\n"

    if not EXAMPLE_PATH.exists():
        print("ERROR: .env.example does not exist", file=sys.stderr)
        sys.exit(1)

    content = EXAMPLE_PATH.read_text(encoding="utf-8")

    # Try to insert before the last blank line or at end
    # Look for "# --- Custom ---" section
    custom_marker = "# --- Custom ---"
    if custom_marker in content:
        # Insert after the custom section header and any existing entries there
        lines = content.split("\n")
        insert_idx = None
        for i, line in enumerate(lines):
            if custom_marker in line:
                insert_idx = i + 1
                # Skip any comment lines immediately after the header
                while insert_idx < len(lines) and (
                    lines[insert_idx].startswith("#") and not CATEGORY_RE.match(lines[insert_idx])
                ):
                    insert_idx += 1
                break

        if insert_idx is not None:
            lines.insert(insert_idx, new_line.rstrip("\n"))
            content = "\n".join(lines)
            if not content.endswith("\n"):
                content += "\n"
            EXAMPLE_PATH.write_text(content, encoding="utf-8")
            print(f"Registered {args.key} in .env.example under Custom section")
            sys.exit(0)

    # Fallback: append at end
    with open(EXAMPLE_PATH, "a", encoding="utf-8") as f:
        f.write(new_line)

    print(f"Registered {args.key} in .env.example")


# --- Main ---

def main():
    parser = argparse.ArgumentParser(description="Secrets & credentials engine")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="All keys with present/missing status")
    sub.add_parser("missing", help="Keys missing from .env.local")

    has_p = sub.add_parser("has", help="Check if key exists and is non-empty")
    has_p.add_argument("key", help="Key name (UPPER_SNAKE_CASE)")

    val_p = sub.add_parser("value", help="Return raw value for a key")
    val_p.add_argument("key", help="Key name (UPPER_SNAKE_CASE)")

    reg_p = sub.add_parser("register", help="Add key to .env.example")
    reg_p.add_argument("key", help="Key name (UPPER_SNAKE_CASE)")
    reg_p.add_argument("description", nargs="?", default="", help="Description")

    args = parser.parse_args()

    if args.command == "status":
        cmd_status(args)
    elif args.command == "missing":
        cmd_missing(args)
    elif args.command == "has":
        cmd_has(args)
    elif args.command == "value":
        cmd_value(args)
    elif args.command == "register":
        cmd_register(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
