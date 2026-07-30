#!/usr/bin/env python3
"""Generic YAML store for meta/ strategy files.

All shell scripts are thin wrappers around this. Subcommands managed via argparse.

Provides field-level read/write for any YAML file in meta/.
File paths are relative to meta/ and validated to prevent traversal.
Set operations auto-append to meta/meta-log.jsonl for audit trail.
Bounds validation reads core/config/meta.yaml strategy_schemas.
"""

import argparse
import json
import re
import sys
from datetime import datetime

# : force utf-8 on stdin/stdout/stderr (covers Windows cp1252 fallback
# when callers bypass the _platform.sh PYTHONIOENCODING=utf-8 shim).
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

from _paths import META_DIR, CONFIG_DIR


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def resolve_path(rel_path):
    """Resolve a path relative to meta/, rejecting traversal outside meta/.

    Enforces a .yaml suffix on the final basename to close the ghost-file
    trap (g-115-122 / rb-335): a call like `meta-set.sh tree decompose_threshold 100`
    previously created `meta/tree` (no extension) that no reader ever loaded.
    Fail loud instead of silently creating a dead file. Callers that want
    to write a subdir index file pass e.g. `meta-knowledge/index.yaml`;
    the suffix check is on the basename, not the whole path.
    """
    target = (META_DIR / rel_path).resolve()
    if not target.is_relative_to(META_DIR.resolve()):
        print(f"ERROR: Path '{rel_path}' resolves outside meta/", file=sys.stderr)
        sys.exit(1)
    if target.suffix != ".yaml":
        print(
            f"ERROR: meta/ targets must end in .yaml — got '{rel_path}' "
            f"(resolved basename: {target.name}). Add the .yaml suffix.",
            file=sys.stderr,
        )
        sys.exit(1)
    return target


def read_yaml(path):
    """Read a YAML file, return parsed dict. Returns {} if missing."""
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    # OneDrive sync-corruption guard (): a transiently un-synced replica of a
    # shared meta hot-file reads as NUL bytes -- full zero-fill, or (on a partial block
    # sync) a real-YAML prefix + NUL suffix. The mechanism is a sync-write race on a
    # frequently rewritten file, NOT Files-On-Demand dehydration: all meta files are
    # pinned ("always keep on this device"), census-verified 0 dehydration-prone of 40,237
    # () -- so do not chase a dehydration fix, it is already maxed out. Both
    # forms make yaml.safe_load raise
    # a cryptic "#x0000 ReaderError". Translate to an actionable message so the next reader
    # does not burn an investigation. Behavior is UNCHANGED -- the read still raises, so any
    # read-modify-write path (cmd_set) aborts and the file is NOT overwritten (no clobber);
    # only the message is clearer and names the recovery path.
    if chr(0) in raw:
        raise ValueError(
            f"{path.name}: {raw.count(chr(0))} NUL byte(s) detected -- transient OneDrive "
            f"sync corruption of a shared meta hot-file. NOT overwriting (read aborts, no "
            f"clobber). Usually transient: re-read after OneDrive re-syncs, or restore a "
            f"known-good version via `py -3 core/scripts/history.py restore '{path}' <version>` "
            f"(list versions with `history.py list '{path}'`). (g-115-1271)"
        )
    data = yaml.safe_load(raw)
    return data if data is not None else {}


def write_yaml(path, data):
    """Atomically write YAML with locking and history."""
    from _fileops import locked_write_yaml
    locked_write_yaml(path, data)


def navigate(data, dotpath):
    """Navigate a nested dict/list by dot-separated path.

    Returns (parent, key) where parent[key] is the target value.
    Numeric path segments index into lists.
    Creates intermediate dicts as needed for set operations.

    Bracket-index notation (``gaps[6]``) is normalized to dotted form
    (``gaps.6``) so list indices route through the numeric-segment path
    below. Without this, ``gaps[6]`` was treated as a literal dict key,
    silently creating an orphan sibling key instead of indexing the list —
    the orphan-key corruption class that rotted skill-gaps.yaml (g-115-1263).
    Invalid / out-of-range list indices fail loud (clean error + exit 1)
    instead of raising a raw traceback.
    """
    dotpath = re.sub(r"\[(\d+)\]", r".\1", dotpath).lstrip(".")
    parts = dotpath.split(".")
    current = data
    for i, part in enumerate(parts[:-1]):
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except ValueError:
                print(f"ERROR: list index '{part}' is not an integer at "
                      f"'{'.'.join(parts[:i+1])}'", file=sys.stderr)
                sys.exit(1)
            except IndexError:
                print(f"ERROR: list index {part} out of range at "
                      f"'{'.'.join(parts[:i+1])}' (list len {len(current)})",
                      file=sys.stderr)
                sys.exit(1)
        elif isinstance(current, dict):
            if part not in current:
                current[part] = {}
            current = current[part]
        else:
            print(f"ERROR: Cannot navigate into {type(current).__name__} at '{'.'.join(parts[:i+1])}'", file=sys.stderr)
            sys.exit(1)

    final_key = parts[-1]
    if isinstance(current, list):
        try:
            final_key = int(final_key)
        except ValueError:
            print(f"ERROR: list index '{final_key}' is not an integer "
                  f"(target list at '{'.'.join(parts[:-1]) or '<root>'}')",
                  file=sys.stderr)
            sys.exit(1)
    return current, final_key


def parse_value(raw):
    """Auto-detect type from string: int, float, bool, null, JSON list/dict, or string.

    A value whose stripped form starts with ``[`` or ``{`` is parsed as JSON so
    that list/dict values round-trip instead of being stored as a literal
    string. This closes the gaps-as-string corruption class (g-115-1263): a
    caller passing a JSON array to ``meta-set.sh`` previously stored the whole
    array as one string. Invalid JSON falls through to the string return, and
    the ``--string`` flag still forces a literal string when a value that
    looks like JSON must be kept verbatim.
    """
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
    stripped = raw.strip()
    if stripped[:1] in ("[", "{"):
        try:
            return json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            pass
    return raw


def assert_type_preserved(prev, value, string_flag, dotpath):
    """Refuse a silent structured -> scalar-string whole-key replacement.

    CLI twin of ``mind_api/src/meta/meta_yaml.py::_assert_type_preserved`` — keep
    the two in sync (meta-set.sh is daemon-routed, so the daemon copy is the LIVE
    path; this one covers direct CLI use). Full rationale, incident trace and the
    rb-5242 refuse-vs-degrade reconciliation live in the daemon docstring.

    Short version (g-115-3462, live-reproduced g-115-3433): ``parse_value``
    attempts json.loads ONLY for values starting with ``[``/``{`` and otherwise
    falls through to the raw string, so a computed JSON payload with a stray line
    of stdout prepended silently replaced 33 gap dicts with one 50,359-char
    string at exit code 0. Narrow by design: ONLY list/dict -> str, ONLY without
    --string; null/int/float/bool stay allowed (unambiguous deliberate scalars,
    and no corrupted payload can parse into one).
    """
    if string_flag:
        return
    if isinstance(prev, (list, dict)) and isinstance(value, str):
        shape = f"{type(prev).__name__} of {len(prev)} item(s)"
        print(
            f"ERROR: refusing to replace '{dotpath}' ({shape}) with a "
            f"{len(value)}-char plain string — this would destroy the whole key. "
            f"The value did not parse as JSON (a JSON payload must start with "
            f"'[' or '{{'; got {value[:40]!r}). Common cause: a stray line of "
            f"stdout prepended to a computed payload. Fix the payload, or pass "
            f"--string if replacing the structure with a literal string is "
            f"genuinely intended. (g-115-3462)",
            file=sys.stderr,
        )
        sys.exit(1)


def load_bounds():
    """Load strategy_schemas from core/config/meta.yaml for bounds validation."""
    meta_config = CONFIG_DIR / "meta.yaml"
    if not meta_config.exists():
        return {}
    with open(meta_config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return config.get("strategy_schemas", {})


def validate_weight_bounds(file_rel, dotpath, value, bounds):
    """Validate a weight value is within bounds."""
    # Check if this is a goal_selection weight
    if "goal-selection-strategy" in file_rel and dotpath.startswith("weights."):
        schema = bounds.get("goal_selection", {})
        wb = schema.get("weight_bounds", {})
        lo, hi = wb.get("min", 0.0), wb.get("max", 3.0)
        if isinstance(value, (int, float)):
            clamped = max(lo, min(hi, float(value)))
            if clamped != float(value):
                print(f"BOUNDS: clamped {value} to {clamped} (range [{lo}, {hi}])", file=sys.stderr)
                return clamped
    # Check trigger sensitivity bounds
    if "evolution-strategy" in file_rel and "trigger_sensitivity" in dotpath:
        schema = bounds.get("evolution", {})
        tb = schema.get("trigger_sensitivity_bounds", {})
        lo, hi = tb.get("min", 0.1), tb.get("max", 5.0)
        if isinstance(value, (int, float)):
            clamped = max(lo, min(hi, float(value)))
            if clamped != float(value):
                print(f"BOUNDS: clamped {value} to {clamped} (range [{lo}, {hi}])", file=sys.stderr)
                return clamped
    return value


def next_meta_change_id():
    """Generate sequential mc-NNN ID from meta-log.jsonl."""
    log_path = META_DIR / "meta-log.jsonl"
    max_num = 0
    if log_path.exists():
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    mc_id = rec.get("meta_change_id", "")
                    if mc_id.startswith("mc-"):
                        num = int(mc_id[3:])
                        max_num = max(max_num, num)
                except (json.JSONDecodeError, ValueError):
                    pass
    return f"mc-{max_num + 1:03d}"


def append_log(file_rel, dotpath, old_value, new_value, reason=""):
    """Append a change record to meta/meta-log.jsonl with mc-NNN ID."""
    log_path = META_DIR / "meta-log.jsonl"
    mc_id = next_meta_change_id()
    record = {
        "date": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "meta_change_id": mc_id,
        "strategy_file": file_rel,
        "field": dotpath,
        "old_value": old_value,
        "new_value": new_value,
        "reason": reason,
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return mc_id


def _create_backpressure_monitor(mc_id, file_rel, dotpath, old_value, new_value):
    """Create a backpressure monitor for a meta-strategy change.

    Reads current imp@k as baseline. Prints to stderr on failure (non-fatal).
    """
    try:
        bp_path = META_DIR / "backpressure.yaml"
        vel_path = META_DIR / "improvement-velocity.yaml"

        # Get current imp@k baseline
        baseline = 0.0
        if vel_path.exists():
            vel = read_yaml(vel_path)
            entries = vel.get("entries", [])
            if entries:
                recent = entries[-min(10, len(entries)):]
                baseline = sum(e.get("learning_value", 0) for e in recent) / len(recent)

        # Ensure backpressure.yaml exists
        bp = read_yaml(bp_path)
        if "version" not in bp:
            bp = {"version": 1, "active_monitors": [], "rollback_history": []}

        config = load_bounds().get("backpressure", {})
        max_monitors = config.get("max_active_monitors", 5)
        monitors = bp.get("active_monitors", [])

        if len(monitors) >= max_monitors:
            monitors.pop(0)  # Evict oldest

        monitor = {
            "meta_change_id": mc_id,
            "strategy_file": file_rel,
            "field": dotpath,
            "old_value": old_value,
            "new_value": new_value,
            "baseline_imp_k": round(baseline, 4),
            "goals_since_change": 0,
            "imp_k_samples": [],
            "consecutive_below_baseline": 0,
            "consecutive_above_baseline": 0,
            "status": "monitoring",
            "created": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }
        monitors.append(monitor)
        bp["active_monitors"] = monitors
        write_yaml(bp_path, bp)
    except Exception as e:
        print(f"BACKPRESSURE: monitor creation failed: {e}", file=sys.stderr)


def _trigger_generation_transition():
    """Close current strategy generation and open a new one.

    Returns early if generation tracking isn't initialized. Prints to stderr on failure (non-fatal).
    """
    try:
        gen_path = META_DIR / "strategy-generations.yaml"
        gen = read_yaml(gen_path)
        if "version" not in gen:
            return  # Not initialized yet

        generations = gen.get("generations", [])

        # Close current if open
        if generations and generations[-1].get("ended") is None:
            generations[-1]["ended"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        # Open new generation
        new_num = gen.get("current_generation", 0) + 1

        # Capture snapshot (inline, lightweight version)
        # Keep in sync with meta-generations.py STRATEGY_FILES
        snapshot = {}
        strategy_files = [
            "goal-selection-strategy.yaml", "reflection-strategy.yaml",
            "evolution-strategy.yaml", "aspiration-generation-strategy.yaml",
            "encoding-strategy.yaml", "skill-quality-strategy.yaml",
        ]
        for sf in strategy_files:
            sf_path = META_DIR / sf
            if sf_path.exists():
                sf_data = read_yaml(sf_path)
                prefix = sf.replace(".yaml", "").replace("-", "_")
                if isinstance(sf_data, dict):
                    for k, v in sf_data.items():
                        if isinstance(v, (int, float, str, bool, type(None))):
                            snapshot[f"{prefix}.{k}"] = v
                        elif isinstance(v, dict):
                            for k2, v2 in v.items():
                                if isinstance(v2, (int, float, str, bool, type(None))):
                                    snapshot[f"{prefix}.{k}.{k2}"] = v2

        new_gen = {
            "generation": new_num,
            "started": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "ended": None,
            "goals_completed": 0,
            "parameter_snapshot": snapshot,
            "metrics": {
                "avg_learning_value": 0.0,
                "total_learning_value": 0.0,
            },
            "best_score": 0.0,
            "worst_score": 1.0,
        }
        generations.append(new_gen)
        gen["generations"] = generations
        gen["current_generation"] = new_num
        write_yaml(gen_path, gen)
    except Exception as e:
        print(f"GENERATIONS: transition failed: {e}", file=sys.stderr)


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
    """Set a scalar field value with bounds validation, auto-logging,
    backpressure monitoring, and generation tracking."""
    path = resolve_path(args.file)
    data = read_yaml(path)
    value = args.value if args.string else parse_value(args.value)

    # Bounds validation
    bounds = load_bounds()
    value = validate_weight_bounds(args.file, args.dotpath, value, bounds)

    parent, key = navigate(data, args.dotpath)
    try:
        old_value = parent[key]
    except (KeyError, IndexError):
        old_value = None
    # Refuse BEFORE any mutation: write_yaml is below, so the file is left
    # untouched and the caller gets a non-zero exit ().
    assert_type_preserved(old_value, value, args.string, args.dotpath)
    # List-index append: when the target is a list and the index equals the
    # current length, append rather than raising IndexError. Lets
    # `meta-set.sh <file> 'gaps[N]' '<json>'` extend a list (the real
    # list-overlay behavior the orphan-key bug faked), .
    if isinstance(parent, list) and isinstance(key, int) and key == len(parent):
        parent.append(value)
    else:
        parent[key] = value
    write_yaml(path, data)

    # Auto-log the change (now returns mc-NNN ID)
    mc_id = append_log(args.file, args.dotpath, old_value, value, args.reason or "")

    # Skip backpressure/generations for non-strategy files or backpressure rollbacks
    is_strategy_file = any(s in args.file for s in [
        "goal-selection-strategy", "reflection-strategy", "evolution-strategy",
        "aspiration-generation-strategy", "encoding-strategy", "skill-quality-strategy",
    ])
    is_rollback = "BACKPRESSURE ROLLBACK" in (args.reason or "")

    if is_strategy_file and not is_rollback:
        # Create backpressure monitor for this change
        _create_backpressure_monitor(mc_id, args.file, args.dotpath, old_value, value)
        # Trigger generation transition
        _trigger_generation_transition()


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


def cmd_log(args):
    """Append a JSON record from stdin to meta-log.jsonl."""
    if sys.stdin.isatty():
        print("Error: expected JSON on stdin (not a terminal)", file=sys.stderr)
        sys.exit(1)
    log_path = META_DIR / "meta-log.jsonl"
    raw = sys.stdin.read().strip()
    # Validate JSON
    json.loads(raw)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(raw + "\n")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description="Generic YAML store for meta/ strategy files")
    sub = parser.add_subparsers(dest="command", required=True)

    # --- read ---
    p_read = sub.add_parser("read", help="Read file or field")
    p_read.add_argument("file", help="File path relative to meta/")
    p_read.add_argument("--field", help="Dot-notation path to a specific field")
    p_read.add_argument("--json", action="store_true", help="Output as JSON instead of YAML")

    # --- set ---
    p_set = sub.add_parser("set", help="Set a scalar field (bounds-validated, auto-logged)")
    p_set.add_argument("file", help="File path relative to meta/")
    p_set.add_argument("dotpath", help="Dot-notation path to field")
    p_set.add_argument("value", help="Value to set (auto-detects type)")
    p_set.add_argument("--string", action="store_true", help="Force value as string")
    p_set.add_argument("--reason", default="", help="Reason for change (logged)")

    # --- append ---
    p_app = sub.add_parser("append", help="Append JSON from stdin to an array field")
    p_app.add_argument("file", help="File path relative to meta/")
    p_app.add_argument("dotpath", help="Dot-notation path to array field")

    # --- log ---
    sub.add_parser("log", help="Append JSON from stdin to meta-log.jsonl")

    return parser


DISPATCH = {
    "read": cmd_read,
    "set": cmd_set,
    "append": cmd_append,
    "log": cmd_log,
}


def main():
    parser = build_parser()
    args = parser.parse_args()
    DISPATCH[args.command](args)


if __name__ == "__main__":
    main()
