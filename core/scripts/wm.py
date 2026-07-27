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
import os
import re
import sys
from datetime import datetime
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

from _paths import AGENT_DIR, CONFIG_DIR, WORLD_DIR, assert_agent_dir
from _fileops import acquire_lock, release_lock

# : fail loud at import time if MIND_AGENT unset; replaces the
# opaque `None / "session"` TypeError class the next line would otherwise raise.
assert_agent_dir("wm")

def wm_path():
    """Effective working-memory path for this process (Phase 1A Mind/Body routing, ).

    BODY_WM_PATH (injected by bash-agent-inject.py when the bound session has a
    body-manifest) routes WM ops to the per-Body file. Unset -> the agent-wide
    WM at agents/<agent>/session/working-memory.yaml (today's behavior).
    Backward-compatible: with no body-manifest (one Body) the routing collapses
    to the agent-wide default. Resolved per-call (not cached at import) so the
    env the bash hook injects is always honored.
    """
    body = os.environ.get("BODY_WM_PATH", "").strip()
    if body:
        return Path(body)
    return AGENT_DIR / "session" / "working-memory.yaml"


def wm_lock_path():
    """Advisory-lock sibling of the effective WM path (per-Body aware)."""
    return wm_path().with_suffix(".lock")


CONFIG_PATH = CONFIG_DIR / "memory-pipeline.yaml"


# Backward-compat: WM_PATH / WM_LOCK_PATH were module constants for years and
# several importers reference them (compact-restore-slots, precompact-checkpoint,
# goal-selector). PEP 562 module __getattr__ keeps those names working — each
# resolves through the per-Body-aware functions above at access time, so both
# `from wm import WM_PATH` (bound after bash-agent-inject sets the env) and
# `wm.WM_PATH` honor BODY_WM_PATH.
def __getattr__(name):
    if name == "WM_PATH":
        return wm_path()
    if name == "WM_LOCK_PATH":
        return wm_lock_path()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# Cross-writer advisory lock for working-memory.yaml read-modify-write
# cycles. See  / rb-508 / . wm.py's commands and the
# direct-writer paths (e.g. tree-encoding-drift-gate.py) MUST acquire
# this lock around any sequence that reads WM, mutates state in memory,
# then writes back — without it, two writers can race and one overwrites
# the other's update with stale data, and a reader can observe a slot
# mid-rewrite (which is what the productivity-gate "Expecting value..."
# noise pointed at). The lock file is the SAME logical resource as the
# .lock used by `_fileops.locked_modify_yaml`; so wm.py-mediated writes
# and any direct writer that uses locked_modify_yaml will mutually
# exclude as long as both target the same `<file>.lock` path.
import contextlib

@contextlib.contextmanager
def wm_lock():
    """Hold the WM advisory lock across a read-modify-write cycle.

    stale_seconds=10: WM RMW is sub-100ms; 30s default would block 6+ aspiration
    iterations on a crashed mid-RMW writer. 10s is still 100x cycle time.
    """
    lock = wm_lock_path()
    acquire_lock(lock, stale_seconds=10)
    try:
        yield
    finally:
        release_lock(lock)

# Top-level keys (not inside slots:)
TOP_LEVEL_KEYS = {
    "encoding_queue", "session_id", "session_start",
    "goals_completed_this_session", "aspiration_touched_last",
    "last_goal_category",
}

# Session-identity fields: survive `wm reset` (which runs mid-session at
# autocompact via consolidate Step 5), cleared only by `wm clear-identity`
# (which runs post-consolidate from /stop's graceful-stop D4.5). Add a field
# here only when its semantic is "describes the current session" — not
# "holds ephemeral slot state." Wrong classification either loses data
# across autocompact (identity→slot) or leaks stale state across sessions
# (slot→identity).
SESSION_IDENTITY_FIELDS = {"session_start"}

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

# Structured-dict slots: top-level writes must be a dict or None (clear).
# Non-JSON stdin that falls through cmd_set's int/float scalar fallbacks
# (e.g. a Python traceback piped via `echo "$(py -3 ... 2>&1)"`) would
# otherwise land as a raw string in the slot, breaking downstream consumers
# (loop-state-bump-counters.py, productivity-gate.sh, compact-restore-slots.sh).
#  traced the exact pattern;  is the structural refusal.
STRUCTURED_DICT_SLOTS = {"loop_state"}

# Cadence-tracker slot patterns — stale by design (fire every N goals or N hours,
# often much longer than evict_threshold_minutes). wm-prune must not evict these
# or the cadence memory is destroyed and the next cadence-check duplicate-fires.
# Discovered  (2026-04-21): wm-prune evicted last_fresh_eyes_review at
# 132 min age; cadence-check then read last=0 and would have fired a duplicate
# briefing. Patterns match slot names like last_fresh_eyes_review,
# last_evolution_at_time, last_strategic_scan, last_strategic_scan_tick, etc.
CADENCE_TRACKER_PATTERNS = (
    re.compile(r"^last_.*_review$"),
    re.compile(r"^last_.*_at$"),
    re.compile(r"^last_.*_at_time$"),
    re.compile(r"^last_.*_tick$"),
    re.compile(r"^last_.*_check$"),
    re.compile(r"^last_.*_checkin$"),
    re.compile(r"^last_.*_scan$"),
    re.compile(r"^last_.*_fire$"),
)

# Slots that survive cmd_reset BY NAME: their writer and reader sit on opposite
# sides of the aspirations-consolidate Step-5 wm-reset boundary (Step 0.65
# writes journal_cluster_summaries pre-reset; Step 9 consumes it one-shot
# post-reset for handoff key_outcomes, then clears it). Content payloads, not
# timestamps — CADENCE_TRACKER_PATTERNS cannot cover them. Staleness
# self-heals: the next consolidation's Step 0.65 overwrites the slot, and
# cmd_maintain's evict path cleans a crashed-consolidation leftover.
# MIRRORED in mind_api/src/endpoints/wm_write.py (the LIVE runtime path —
# wm-reset.sh routes to POST /v1/wm/reset, not to cmd_reset). Keep both in
# sync; parity asserted by test_wm_reset_cadence.py. ()
RESET_SURVIVING_SLOTS = {"journal_cluster_summaries"}

def _is_cadence_tracker(slot_name):
    """True if slot name matches a cadence-tracker pattern — do not evict."""
    return any(p.match(slot_name) for p in CADENCE_TRACKER_PATTERNS)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso():
    """Local ISO timestamp."""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

def read_yaml(path):
    """Read a YAML file, return parsed dict. Returns {} if missing.

    Detective layer (g-001-44): a non-empty file whose bytes are all 0x00 is
    the NTFS metadata-journaled-but-data-not-flushed crash signature — return
    empty with a stderr WARN instead of feeding null bytes to the YAML parser.
    """
    if not path.exists():
        return {}
    raw = path.read_bytes()
    if raw and not raw.strip(b"\x00"):
        print(f"[wm] WARN: {path} is all-null content ({len(raw)} bytes) — "
              f"post-crash corruption signature (g-001-44); treating as empty. "
              f"Rebuild via wm-init.sh if slots are expected.", file=sys.stderr)
        return {}
    data = yaml.safe_load(raw.decode("utf-8", errors="replace"))
    return data if data is not None else {}

def write_yaml(path, data):
    """Atomically write data as YAML (fsync before rename — )."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)

def read_wm():
    """Read working memory file."""
    return read_yaml(wm_path())

def write_wm(data):
    """Write working memory file atomically."""
    write_yaml(wm_path(), data)

def read_config():
    """Read memory pipeline config."""
    return read_yaml(CONFIG_PATH)


def _default_wm_data():
    """Build a fresh working-memory dict from the memory-pipeline config.

    Single source of truth for the canonical empty-WM shape — consumed by
    cmd_init, cmd_reset, and cmd_set (g-115-748 self-heal path). Extracting
    this helper closes the cmd_read/cmd_set asymmetry from g-115-737: a
    missing or empty working-memory.yaml previously caused cmd_set to exit 1
    with "Working memory not initialized" while cmd_read returned {} exit 0.
    The asymmetry made fresh-eyes-cadence-check seed-stagger (g-270-02)
    fail-open and fire all 4 sibling rituals on the same iteration.
    Single-writer rule: any future change to the empty-WM shape edits this
    helper (and its consumers automatically inherit it).
    """
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

    return {
        "encoding_queue": [],
        "session_id": None,
        "session_start": None,
        "goals_completed_this_session": [],
        "aspiration_touched_last": "",
        "last_goal_category": "",
        "slots": slots,
        "slot_meta": slot_meta,
    }

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
# Schema gates
# ---------------------------------------------------------------------------

def _validate_knowledge_debt_entry(item):
    """Reject knowledge_debt entries with unresolvable node_keys.

    Valid forms:
      A) node_key resolves to a real _tree.yaml entry (string key)
      B) priority == "housekeeping" (debt is framework-scoped, not node-scoped)
      C) node_key is explicitly null AND reason is present

    See rb-248 and g-115-59. Rejects loudly rather than silently tagging —
    silent coercion would violate rb-215 single-source-of-truth.
    """
    priority = item.get("priority")
    node_key = item.get("node_key")
    reason = item.get("reason")

    if priority == "housekeeping":
        if not reason:
            print("ERROR: knowledge_debt housekeeping entry requires 'reason'", file=sys.stderr)
            sys.exit(1)
        return  # valid — housekeeping form

    if node_key is None:
        if not reason:
            print("ERROR: knowledge_debt entry with node_key=null requires 'reason'", file=sys.stderr)
            sys.exit(1)
        return  # valid — explicit null with reason

    if not isinstance(node_key, str) or not node_key.strip():
        print(f"ERROR: knowledge_debt node_key must be non-empty string or null, got {node_key!r}", file=sys.stderr)
        sys.exit(1)

    # Must resolve against _tree.yaml
    tree_path = WORLD_DIR / "knowledge" / "tree" / "_tree.yaml"
    try:
        with open(tree_path, encoding="utf-8") as f:
            tree = yaml.safe_load(f) or {}
    except OSError as e:
        print(f"ERROR: cannot read {tree_path} for knowledge_debt validation: {e}", file=sys.stderr)
        sys.exit(1)

    nodes = tree.get("nodes", {})
    if node_key not in nodes:
        print(
            f"ERROR: knowledge_debt node_key '{node_key}' does not resolve to a tree node.\n"
            f"       Valid forms: (A) real node_key from _tree.yaml, (B) priority='housekeeping' + reason,\n"
            f"       (C) node_key=null + reason. See core/config/conventions/working-memory.md.",
            file=sys.stderr,
        )
        sys.exit(1)

# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_set(args):
    """Set a slot value from stdin (JSON).

    Reads stdin OUTSIDE the WM lock (no I/O on WM yet), then acquires the
    advisory lock for the read-modify-write cycle on working-memory.yaml.
    g-115-206 — closes the race that surfaced as productivity-gate noise.

    rb-715 subdict-clobber gate (g-275-02): when slot == 'loop_state' and the
    incoming value is a dict, run loop_state_merge_gate.check() against the
    on-disk loop_state.signals subkeys. If incoming.signals would clobber
    bash-written subkeys (quiescence, goals_since_last_tree_update, etc.),
    refuse the write unless --override-merge-gate <justification> is passed.
    """
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

    # : refuse non-dict-or-null writes to structured-dict slots.
    # Without this, a Python traceback piped via `echo "$(py -3 ... 2>&1)"`
    # falls through the scalar fallbacks above and lands as a 700+ char
    # string in loop_state, breaking productivity-gate / bump-counters /
    # compact-restore. The merge-gate at line 410 catches subdict-clobber
    # (rb-715) but pass-throughs non-dict, so this writer-side check is
    # the only structural defense against type-transition.
    if args.slot in STRUCTURED_DICT_SLOTS and value is not None and not isinstance(value, dict):
        shape = type(value).__name__
        preview = (raw[:80] + "...") if len(raw) > 80 else raw
        print(
            f"BLOCKED: structured-dict slot '{args.slot}' refuses non-dict-or-null "
            f"write (got {shape}, len={len(raw)}, prefix={preview!r})",
            file=sys.stderr,
        )
        sys.exit(1)

    with wm_lock():
        data = read_wm()
        if not data:
            #  self-heal: a missing or empty working-memory.yaml is
            # not a writer's failure mode — it is a fresh agent dir, a wiped
            # session, or a runner that called wm-set BEFORE wm-init had a
            # chance to fire. Seed the canonical empty-WM shape and proceed
            # with the requested set. The cmd_read counterpart already
            # returns {} exit 0 on empty WM; cmd_set previously diverged
            # by exit 1 (rb-748 / ), which made
            # fresh-eyes-cadence-check seed-stagger () fail-open
            # and fire all 4 sibling rituals simultaneously on a fresh dir.
            # cmd_append (~line 473) keeps the exit-1 behavior — array
            # writes against a missing WM are ambiguous (append-to-what?)
            # and the goal description scopes the self-heal to cmd_set only.
            data = _default_wm_data()

        parent, key, is_top = resolve_slot(data, args.slot)
        if parent is None:
            print(f"ERROR: Cannot resolve path '{args.slot}'", file=sys.stderr)
            sys.exit(1)

        # rb-715 subdict-clobber gate: only fires for top-level loop_state writes.
        # Subfield writes (e.g. loop_state.signals.quiescence) are bash-side
        # surgical updates by design — those are exactly what the gate protects.
        if args.slot == "loop_state":
            on_disk_loop_state = parent.get(key)
            override = getattr(args, "override_merge_gate", None)
            try:
                # Import via importlib because the module filename uses
                # hyphens (kebab-case is the project convention for *.py
                # under core/scripts/), which prevents `import loop_state_merge_gate`.
                import importlib.util
                gate_path = Path(__file__).resolve().parent / "loop-state-merge-gate.py"
                spec = importlib.util.spec_from_file_location("loop_state_merge_gate", gate_path)
                gate_mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(gate_mod)
                gate_result = gate_mod.check(value, on_disk_loop_state, override=override)
            except (ImportError, OSError, AttributeError) as e:
                # Fail-open on gate-load failure (don't block the loop on a broken gate)
                print(f"[loop-state-merge-gate] WARN: gate load failed ({e}) — fail-open", file=sys.stderr)
                gate_result = {"would_block": False, "reason": "gate load failed", "missing_subkeys": []}

            if gate_result.get("would_block"):
                print(f"BLOCKED: {gate_result['reason']}", file=sys.stderr)
                sys.exit(1)
            if gate_result.get("override_applied"):
                # Echo override to stderr for audit trail
                print(f"[loop-state-merge-gate] {gate_result['reason']}", file=sys.stderr)
            # : monotonic-counter preservation. When the gate floored
            # stale-lower top-level counters (goals_completed/productive_goals)
            # or unioned a subset counted_goals_this_session against the on-disk
            # committed state, write the PRESERVED value, not the raw incoming —
            # the Mechanism C backstop for the non-CAS collateral writers.
            if gate_result.get("counters_preserved"):
                value = gate_result.get("preserved_value", value)
                print(
                    f"[loop-state-merge-gate] preserved monotonic counters "
                    f"(g-115-1418): {gate_result.get('preserved_counters')}",
                    file=sys.stderr,
                )

        parent[key] = value

        if not is_top:
            update_modified(data, args.slot)

        write_wm(data)

def cmd_append(args):
    """Append an item to an array slot from stdin (JSON).

    Read-modify-write protected by wm_lock — see g-115-206.
    """
    raw = sys.stdin.read().strip()
    if not raw:
        print("ERROR: No input on stdin", file=sys.stderr)
        sys.exit(1)

    item = json.loads(raw)

    # Auto-add _item_ts
    if isinstance(item, dict):
        item["_item_ts"] = now_iso()

    # knowledge_debt schema gate (, rb-248): node_key must either resolve
    # to a real tree node OR the entry must tag itself as housekeeping. Brittle
    # placeholders like "multiple" or "tree_maintenance" produce false positives
    # in debt-closure matchers that use simple string containment.
    root_slot_for_validation = args.slot.split(".")[0]
    if root_slot_for_validation == "knowledge_debt" and isinstance(item, dict):
        _validate_knowledge_debt_entry(item)

    with wm_lock():
        data = read_wm()
        if not data:
            print("ERROR: Working memory not initialized. Run wm-init.sh first.", file=sys.stderr)
            sys.exit(1)

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
    """Clear (null out) a slot. RMW protected by wm_lock — see ."""
    with wm_lock():
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
    """Mid-session pruning based on config thresholds.

    RMW protected by wm_lock — see g-115-206. Prune touches every slot, so
    a concurrent wm-set from any source could clobber pruning output (or
    vice versa) without the lock.
    """
    with wm_lock():
        _do_prune(args)

def _do_prune(args):
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

        # Evict stale scalar slots (non-protected, non-array, non-map,
        # non-cadence-tracker). Cadence trackers are stale BY DESIGN — fire
        # every N goals or N hours; eviction destroys cadence memory and
        # causes duplicate firings. See CADENCE_TRACKER_PATTERNS above.
        if (slot_name not in protected
                and slot_name not in ARRAY_SLOTS
                and slot_name not in MAP_SLOTS
                and not _is_cadence_tracker(slot_name)
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
    """Initialize working memory from template.

    Lock-protected (g-115-206): pure write, but a concurrent wm-set could
    otherwise clobber init or vice versa during the rare window where init
    fires while the loop is starting another writer.
    """
    data = _default_wm_data()
    slot_count = len(data.get("slots", {}))

    with wm_lock():
        write_wm(data)
    print(f"Working memory initialized with {slot_count} slots.")

def cmd_reset(args):
    """Reset working memory to template state. Preserves SESSION_IDENTITY_FIELDS.

    RMW protected by wm_lock — see g-115-206. The read of `existing` for
    identity-field preservation must happen INSIDE the same lock as the
    write, otherwise a concurrent writer's update to those fields could
    be observed-then-clobbered.
    """
    data = _default_wm_data()
    slot_types = data.get("slots", {}).keys()

    with wm_lock():
        existing = read_wm()
        preserved = []
        for k in SESSION_IDENTITY_FIELDS:
            v = existing.get(k)
            if v is not None:
                data[k] = v
                preserved.append(k)

        # Preserve cadence-tracker slots — they hold "last X fired at"
        # timestamps that drive iteration cadences (last_felt_sense_checkin,
        # last_strategic_scan, etc). Eviction at reset causes duplicate
        # firings the next time their gate evaluates. Mirrors the cadence-
        # tracker exemption in maintain prune (see _is_cadence_tracker and
        # the _is_cadence_tracker check in cmd_maintain). Iterates existing
        # slots (not slot_types) because cadence-trackers are typically
        # added dynamically and may not appear in slot_types config.
        # RESET_SURVIVING_SLOTS members survive the same way — their reader
        # runs AFTER this reset by design ().
        existing_slots = existing.get("slots", {})
        existing_meta = existing.get("slot_meta", {})
        cadence_preserved = []
        surviving_preserved = []
        for slot_name, slot_val in existing_slots.items():
            is_cadence = _is_cadence_tracker(slot_name)
            if (is_cadence or slot_name in RESET_SURVIVING_SLOTS) and slot_val is not None:
                data["slots"][slot_name] = slot_val
                if slot_name in existing_meta:
                    data["slot_meta"][slot_name] = existing_meta[slot_name]
                (cadence_preserved if is_cadence else surviving_preserved).append(slot_name)

        write_wm(data)
    status_parts = []
    if preserved:
        status_parts.append(", ".join(sorted(preserved)))
    if cadence_preserved:
        status_parts.append(f"{len(cadence_preserved)} cadence trackers")
    if surviving_preserved:
        status_parts.append("reset-surviving: " + ", ".join(sorted(surviving_preserved)))
    if status_parts:
        print(f"Working memory reset to template state ({len(slot_types)} slots; preserved: {'; '.join(status_parts)}).")
    else:
        print(f"Working memory reset to template state ({len(slot_types)} slots).")

def cmd_clear_identity(args):
    """Clear SESSION_IDENTITY_FIELDS. Authorized caller: /stop graceful-stop D4.5.

    RMW protected by wm_lock — see g-115-206.
    """
    with wm_lock():
        data = read_wm()
        if not data:
            print("Working memory not initialized; nothing to clear.")
            return
        cleared = []
        for k in SESSION_IDENTITY_FIELDS:
            if data.get(k) is not None:
                data[k] = None
                cleared.append(k)
        if not cleared:
            print("Session-identity fields already clear.")
            return
        write_wm(data)
    print(f"Cleared session-identity fields: {', '.join(sorted(cleared))}.")

# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description="Working memory access layer")
    sub = parser.add_subparsers(dest="command", required=True)

    # --- set ---
    p_set = sub.add_parser("set", help="Set a slot value (JSON from stdin)")
    p_set.add_argument("slot", help="Slot path (e.g. 'active_context', 'active_context.retrieval_manifest')")
    p_set.add_argument(
        "--override-merge-gate",
        default=None,
        help="rb-715 subdict-clobber gate override. Pass a justification string "
             "(e.g. 'session boundary signal reset') to bypass the merge gate "
             "when intentionally clearing on-disk loop_state.signals subkeys. "
             "Each override is appended to world/loop-state-merge-overrides.jsonl.",
    )

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
    sub.add_parser("reset", help="Reset working memory to template state (preserves session-identity)")

    # --- clear-identity ---
    sub.add_parser("clear-identity", help="Clear SESSION_IDENTITY_FIELDS (session_id, session_start). Called by /stop post-consolidate.")

    return parser

DISPATCH = {
    "set": cmd_set,
    "append": cmd_append,
    "clear": cmd_clear,
    "ages": cmd_ages,
    "prune": cmd_prune,
    "init": cmd_init,
    "reset": cmd_reset,
    "clear-identity": cmd_clear_identity,
}

def main():
    parser = build_parser()
    args = parser.parse_args()
    DISPATCH[args.command](args)

if __name__ == "__main__":
    main()
