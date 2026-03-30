#!/usr/bin/env python3
"""Working memory script — dedicated access layer for <agent>/session/working-memory.yaml.

All shell scripts (wm-*.sh) are thin wrappers around this. Subcommands managed via argparse.

Provides slot-level read/write/append/clear with automatic timestamp tracking via slot_meta,
mid-session pruning, and template initialization/reset.

Slot addressing:
  - Slots live under 'slots:' key: wm.py read active_context → data["slots"]["active_context"]
  - Top-level keys (encoding_queue, session_id, etc.) addressed directly: wm.py read encoding_queue
  - Dot-path subfields: wm.py read active_context.retrieval_manifest → navigates into slot
"""

import argparse
import json
import sys
from datetime import datetime
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

from _paths import AGENT_DIR, CONFIG_DIR

WM_PATH = AGENT_DIR / "session" / "working-memory.yaml"
CONFIG_PATH = CONFIG_DIR / "memory-pipeline.yaml"

# Top-level keys (not inside slots:)
TOP_LEVEL_KEYS = {
    "encoding_queue", "session_id", "session_start",
    "goals_completed_this_session", "aspiration_touched_last",
    "last_goal_category",
}

# Default slot types — used by init/reset when config is unavailable
DEFAULT_SLOT_TYPES = [
    "active_constraints", "active_context", "active_hypothesis", "active_strategy",
    "archived_context", "cross_domain_transfer", "domain_data",
    "ephemeral_observation", "knowledge_debt", "known_blockers",
    "micro_hypotheses", "pending_resolutions", "recent_violations",
    "sensory_buffer", "session_goal", "conclusions",
]

# Slots that are arrays (not scalars or maps)
ARRAY_SLOTS = {
    "knowledge_debt", "known_blockers", "micro_hypotheses",
    "recent_violations", "sensory_buffer", "conclusions",
}

# Slots that are maps with specific structure (not scalars)
MAP_SLOTS = {
    "active_context": {"summary": None, "experience_refs": [], "retrieval_manifest": None},
    "archived_context": {"summary": None, "experience_refs": []},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso():
    """Local ISO timestamp."""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


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


def read_wm():
    """Read working memory file."""
    return read_yaml(WM_PATH)


def write_wm(data):
    """Write working memory file atomically."""
    write_yaml(WM_PATH, data)


def read_config():
    """Read memory pipeline config."""
    return read_yaml(CONFIG_PATH)


def resolve_slot(data, slot_path):
    """Resolve a slot path to (parent_dict, final_key, is_top_level).

    'known_blockers' → data["slots"]["known_blockers"]
    'encoding_queue' → data["encoding_queue"]
    'active_context.retrieval_manifest' → data["slots"]["active_context"]["retrieval_manifest"]
    """
    parts = slot_path.split(".")
    root_key = parts[0]

    if root_key in TOP_LEVEL_KEYS:
        # Top-level key
        current = data
        for part in parts[:-1]:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None, None, True
        return current, parts[-1], True
    else:
        # Slot key — lives under slots:
        slots = data.get("slots", {})
        if len(parts) == 1:
            return slots, root_key, False
        # Navigate deeper
        current = slots
        for part in parts[:-1]:
            if isinstance(current, dict) and part in current:
                current = current[part]
            elif isinstance(current, dict):
                current[part] = {}
                current = current[part]
            else:
                return None, None, False
        return current, parts[-1], False


def get_slot_meta(data, slot_name):
    """Get or create slot_meta entry for a slot."""
    meta = data.setdefault("slot_meta", {})
    root = slot_name.split(".")[0]
    if root not in meta:
        meta[root] = {"updated_at": None, "accessed_at": None, "update_count": 0}
    return meta[root]


def update_accessed(data, slot_name):
    """Mark a slot as accessed."""
    m = get_slot_meta(data, slot_name)
    m["accessed_at"] = now_iso()


def update_modified(data, slot_name):
    """Mark a slot as modified."""
    m = get_slot_meta(data, slot_name)
    m["updated_at"] = now_iso()
    m["update_count"] = m.get("update_count", 0) + 1


def get_pruning_config(config):
    """Get pruning configuration with defaults."""
    defaults = {
        "stale_threshold_minutes": 30,
        "evict_threshold_minutes": 120,
        "array_limits": {
            "encoding_queue": 20,
            "sensory_buffer": 20,
            "micro_hypotheses": 30,
            "knowledge_debt": 15,
            "known_blockers": 10,
            "recent_violations": 5,
        },
        "item_stale_minutes": {
            "micro_hypotheses": 180,
            "sensory_buffer": 60,
            "ephemeral_observation": 60,
        },
        "protected_slots": ["known_blockers", "knowledge_debt"],
    }
    return config.get("working_memory_pruning", defaults)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_read(args):
    """Read a slot or the entire working memory."""
    data = read_wm()
    if not data:
        if args.json:
            print("{}")
        return

    if not args.slot:
        # Full dump — no accessed_at tracking for full reads
        if args.json:
            print(json.dumps(data, ensure_ascii=False, default=str))
        else:
            yaml.dump(data, sys.stdout, default_flow_style=False, allow_unicode=True, sort_keys=False)
        return

    parent, key, is_top = resolve_slot(data, args.slot)
    if parent is None or key not in parent:
        print("null")
        return

    value = parent[key]

    # Output first — reads must succeed even if tracking write fails
    if args.json:
        print(json.dumps(value, ensure_ascii=False, default=str))
    else:
        if isinstance(value, (dict, list)):
            yaml.dump(value, sys.stdout, default_flow_style=False, allow_unicode=True, sort_keys=False)
        else:
            print(value if value is not None else "null")

    # Track access (after output — this is a side effect, not the primary operation)
    if not is_top:
        update_accessed(data, args.slot)
        write_wm(data)


def cmd_set(args):
    """Set a slot value from stdin (JSON)."""
    data = read_wm()
    if not data:
        print("ERROR: Working memory not initialized. Run wm-init.sh first.", file=sys.stderr)
        sys.exit(1)

    raw = sys.stdin.read().strip()
    if not raw:
        print("ERROR: No input on stdin", file=sys.stderr)
        sys.exit(1)

    # Parse value — try JSON, fall back to scalar
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        # Treat as scalar string
        if raw == "null":
            value = None
        elif raw == "true":
            value = True
        elif raw == "false":
            value = False
        else:
            try:
                value = int(raw)
            except ValueError:
                try:
                    value = float(raw)
                except ValueError:
                    value = raw

    parent, key, is_top = resolve_slot(data, args.slot)
    if parent is None:
        print(f"ERROR: Cannot resolve path '{args.slot}'", file=sys.stderr)
        sys.exit(1)

    parent[key] = value

    if not is_top:
        update_modified(data, args.slot)

    write_wm(data)


def cmd_append(args):
    """Append an item to an array slot from stdin (JSON)."""
    data = read_wm()
    if not data:
        print("ERROR: Working memory not initialized. Run wm-init.sh first.", file=sys.stderr)
        sys.exit(1)

    raw = sys.stdin.read().strip()
    if not raw:
        print("ERROR: No input on stdin", file=sys.stderr)
        sys.exit(1)

    item = json.loads(raw)

    # Auto-add _item_ts
    if isinstance(item, dict):
        item["_item_ts"] = now_iso()

    parent, key, is_top = resolve_slot(data, args.slot)
    if parent is None:
        print(f"ERROR: Cannot resolve path '{args.slot}'", file=sys.stderr)
        sys.exit(1)

    arr = parent.get(key)
    if arr is None:
        parent[key] = []
        arr = parent[key]
    if not isinstance(arr, list):
        print(f"ERROR: '{args.slot}' is {type(arr).__name__}, not a list", file=sys.stderr)
        sys.exit(1)

    arr.append(item)

    # Enforce array limits
    config = read_config()
    pruning = get_pruning_config(config)
    limits = pruning.get("array_limits", {})
    root_slot = args.slot.split(".")[0]
    limit = limits.get(root_slot)
    if limit and len(arr) > limit:
        # Remove oldest items (those without _item_ts first, then by _item_ts)
        arr.sort(key=lambda x: x.get("_item_ts", "0000") if isinstance(x, dict) else "0000")
        while len(arr) > limit:
            arr.pop(0)

    if not is_top:
        update_modified(data, args.slot)

    write_wm(data)


def cmd_clear(args):
    """Clear (null out) a slot."""
    data = read_wm()
    if not data:
        print("ERROR: Working memory not initialized.", file=sys.stderr)
        sys.exit(1)

    parent, key, is_top = resolve_slot(data, args.slot)
    if parent is None:
        print(f"ERROR: Cannot resolve path '{args.slot}'", file=sys.stderr)
        sys.exit(1)

    # Clear to [] if currently a list, None otherwise — handles both
    # slot arrays (known_blockers) and top-level arrays (encoding_queue)
    current_val = parent.get(key) if isinstance(parent, dict) else None
    root_slot = args.slot.split(".")[0]
    if isinstance(current_val, list) or root_slot in ARRAY_SLOTS:
        parent[key] = []
    else:
        parent[key] = None

    if not is_top:
        update_modified(data, args.slot)

    write_wm(data)


def cmd_ages(args):
    """Report slot ages (minutes since last update/access)."""
    data = read_wm()
    if not data:
        if args.json:
            print("{}")
        else:
            print("Working memory not initialized.")
        return

    now = datetime.now()
    meta = data.get("slot_meta", {})
    slots = data.get("slots", {})
    result = {}

    for slot_name in slots:
        m = meta.get(slot_name, {})
        updated = m.get("updated_at")
        accessed = m.get("accessed_at")
        update_count = m.get("update_count", 0)

        mins_since_update = None
        mins_since_access = None
        if updated:
            try:
                dt = datetime.fromisoformat(updated)
                mins_since_update = int((now - dt).total_seconds() / 60)
            except (ValueError, TypeError):
                pass
        if accessed:
            try:
                dt = datetime.fromisoformat(accessed)
                mins_since_access = int((now - dt).total_seconds() / 60)
            except (ValueError, TypeError):
                pass

        # Count items for array slots
        slot_val = slots.get(slot_name)
        item_count = len(slot_val) if isinstance(slot_val, list) else None

        result[slot_name] = {
            "minutes_since_update": mins_since_update,
            "minutes_since_access": mins_since_access,
            "update_count": update_count,
            "item_count": item_count,
        }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, default=str))
    else:
        for name, info in result.items():
            upd = f"{info['minutes_since_update']}m" if info['minutes_since_update'] is not None else "never"
            acc = f"{info['minutes_since_access']}m" if info['minutes_since_access'] is not None else "never"
            items = f", {info['item_count']} items" if info['item_count'] is not None else ""
            print(f"  {name}: updated {upd} ago, accessed {acc} ago, {info['update_count']} writes{items}")


def cmd_prune(args):
    """Mid-session pruning based on config thresholds."""
    data = read_wm()
    if not data:
        print("Working memory not initialized.", file=sys.stderr)
        sys.exit(1)

    config = read_config()
    pruning = get_pruning_config(config)
    now = datetime.now()
    meta = data.get("slot_meta", {})
    slots = data.get("slots", {})
    protected = set(pruning.get("protected_slots", []))
    report = {"pruned_items": [], "stale_slots": [], "evicted_slots": []}

    stale_mins = pruning.get("stale_threshold_minutes", 30)
    evict_mins = pruning.get("evict_threshold_minutes", 120)
    item_stale = pruning.get("item_stale_minutes", {})
    limits = pruning.get("array_limits", {})

    for slot_name, slot_val in list(slots.items()):
        m = meta.get(slot_name, {})
        updated_str = m.get("updated_at")

        mins_since = None
        if updated_str:
            try:
                dt = datetime.fromisoformat(updated_str)
                mins_since = (now - dt).total_seconds() / 60
            except (ValueError, TypeError):
                pass

        # Flag stale slots
        if mins_since is not None and mins_since > stale_mins:
            report["stale_slots"].append({
                "slot": slot_name,
                "minutes_stale": int(mins_since),
            })

        # Evict stale scalar slots (non-protected, non-array, non-map)
        if (slot_name not in protected
                and slot_name not in ARRAY_SLOTS
                and slot_name not in MAP_SLOTS
                and slot_val is not None
                and mins_since is not None
                and mins_since > evict_mins):
            if not args.dry_run:
                slots[slot_name] = None
                update_modified(data, slot_name)
            report["evicted_slots"].append({
                "slot": slot_name,
                "minutes_stale": int(mins_since),
            })

        # Array item pruning
        if isinstance(slot_val, list) and slot_name in item_stale:
            max_age = item_stale[slot_name]
            to_remove = []
            for i, item in enumerate(slot_val):
                if not isinstance(item, dict):
                    continue
                ts = item.get("_item_ts")
                if not ts:
                    continue
                try:
                    dt = datetime.fromisoformat(ts)
                    age_mins = (now - dt).total_seconds() / 60
                except (ValueError, TypeError):
                    continue
                if age_mins > max_age:
                    # Protected slots: only prune resolved items
                    if slot_name in protected:
                        if slot_name == "known_blockers" and item.get("resolution") is not None:
                            to_remove.append(i)
                        elif slot_name == "knowledge_debt" and item.get("resolved"):
                            to_remove.append(i)
                    else:
                        # For micro_hypotheses: only prune resolved ones by age
                        if slot_name == "micro_hypotheses" and item.get("outcome") is not None:
                            to_remove.append(i)
                        elif slot_name != "micro_hypotheses":
                            to_remove.append(i)

            for i in reversed(to_remove):
                removed = slot_val.pop(i)
                report["pruned_items"].append({
                    "slot": slot_name,
                    "item_summary": str(removed.get("claim", removed.get("reason", removed.get("observation", "?"))))[:80],
                    "reason": "item_stale",
                })

            if to_remove and not args.dry_run:
                update_modified(data, slot_name)

        # Array size cap enforcement
        if isinstance(slot_val, list) and slot_name in limits:
            limit = limits[slot_name]
            if len(slot_val) > limit:
                # Sort by _item_ts, remove oldest
                slot_val.sort(key=lambda x: x.get("_item_ts", "0000") if isinstance(x, dict) else "0000")
                while len(slot_val) > limit:
                    removed = slot_val.pop(0)
                    report["pruned_items"].append({
                        "slot": slot_name,
                        "item_summary": str(removed.get("claim", removed.get("reason", "?")))[:80] if isinstance(removed, dict) else "?",
                        "reason": "array_limit",
                    })
                if not args.dry_run:
                    update_modified(data, slot_name)

    # Also check encoding_queue (top-level)
    eq = data.get("encoding_queue", [])
    eq_limit = limits.get("encoding_queue", 20)
    if isinstance(eq, list) and len(eq) > eq_limit:
        eq.sort(key=lambda x: x.get("_item_ts", "0000") if isinstance(x, dict) else "0000")
        while len(eq) > eq_limit:
            removed = eq.pop(0)
            report["pruned_items"].append({
                "slot": "encoding_queue",
                "item_summary": str(removed.get("observation", "?"))[:80] if isinstance(removed, dict) else "?",
                "reason": "array_limit",
            })

    if not args.dry_run:
        write_wm(data)

    print(json.dumps(report, ensure_ascii=False, default=str))


def cmd_init(args):
    """Initialize working memory from template."""
    config = read_config()
    wm_config = config.get("working_memory", {})
    slot_types = wm_config.get("slot_types", DEFAULT_SLOT_TYPES)

    slots = {}
    slot_meta = {}
    for st in slot_types:
        if st in ARRAY_SLOTS:
            slots[st] = []
        elif st in MAP_SLOTS:
            slots[st] = dict(MAP_SLOTS[st])  # shallow copy
        else:
            slots[st] = None
        slot_meta[st] = {"updated_at": None, "accessed_at": None, "update_count": 0}

    data = {
        "encoding_queue": [],
        "session_id": None,
        "session_start": None,
        "goals_completed_this_session": [],
        "aspiration_touched_last": "",
        "last_goal_category": "",
        "slots": slots,
        "slot_meta": slot_meta,
    }

    write_wm(data)
    print(f"Working memory initialized with {len(slot_types)} slots.")


def cmd_reset(args):
    """Reset working memory to template state (session-end)."""
    config = read_config()
    wm_config = config.get("working_memory", {})
    slot_types = wm_config.get("slot_types", DEFAULT_SLOT_TYPES)

    slots = {}
    slot_meta = {}
    for st in slot_types:
        if st in ARRAY_SLOTS:
            slots[st] = []
        elif st in MAP_SLOTS:
            slots[st] = dict(MAP_SLOTS[st])
        else:
            slots[st] = None
        slot_meta[st] = {"updated_at": None, "accessed_at": None, "update_count": 0}

    data = {
        "encoding_queue": [],
        "session_id": None,
        "session_start": None,
        "goals_completed_this_session": [],
        "aspiration_touched_last": "",
        "last_goal_category": "",
        "slots": slots,
        "slot_meta": slot_meta,
    }

    write_wm(data)
    print(f"Working memory reset to template state ({len(slot_types)} slots).")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description="Working memory access layer")
    sub = parser.add_subparsers(dest="command", required=True)

    # --- read ---
    p_read = sub.add_parser("read", help="Read a slot or full working memory")
    p_read.add_argument("slot", nargs="?", default=None, help="Slot name (e.g. 'active_context', 'encoding_queue', 'active_context.retrieval_manifest')")
    p_read.add_argument("--json", action="store_true", help="Output as JSON")

    # --- set ---
    p_set = sub.add_parser("set", help="Set a slot value (JSON from stdin)")
    p_set.add_argument("slot", help="Slot path (e.g. 'active_context', 'active_context.retrieval_manifest')")

    # --- append ---
    p_app = sub.add_parser("append", help="Append item to array slot (JSON from stdin)")
    p_app.add_argument("slot", help="Slot name (e.g. 'micro_hypotheses', 'encoding_queue')")

    # --- clear ---
    p_clr = sub.add_parser("clear", help="Clear a slot (null for scalars, [] for arrays)")
    p_clr.add_argument("slot", help="Slot name")

    # --- ages ---
    p_ages = sub.add_parser("ages", help="Report slot ages")
    p_ages.add_argument("--json", action="store_true", help="Output as JSON")

    # --- prune ---
    p_prune = sub.add_parser("prune", help="Mid-session pruning")
    p_prune.add_argument("--dry-run", action="store_true", help="Report what would be pruned without modifying")

    # --- init ---
    sub.add_parser("init", help="Initialize working memory from template")

    # --- reset ---
    sub.add_parser("reset", help="Reset working memory to template state")

    return parser


DISPATCH = {
    "read": cmd_read,
    "set": cmd_set,
    "append": cmd_append,
    "clear": cmd_clear,
    "ages": cmd_ages,
    "prune": cmd_prune,
    "init": cmd_init,
    "reset": cmd_reset,
}


def main():
    parser = build_parser()
    args = parser.parse_args()
    DISPATCH[args.command](args)


if __name__ == "__main__":
    main()
