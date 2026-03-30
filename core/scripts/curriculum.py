#!/usr/bin/env python3
"""Curriculum gate evaluator — deterministic stage promotion engine.

Evaluates graduation gates for the current curriculum stage, promotes
the agent when all gates pass, and checks capability contracts.

All reads go through agent state files. Promotions are append-only
to <agent>/curriculum-promotions.jsonl.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Ensure stdout/stderr handle unicode on all platforms (Windows cp1252 fix)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import AGENT_DIR, CONFIG_DIR, CORE_ROOT, PROJECT_ROOT

CURRICULUM_PATH = AGENT_DIR / "curriculum.yaml"
PROMOTIONS_PATH = AGENT_DIR / "curriculum-promotions.jsonl"
FRAMEWORK_PATH = CONFIG_DIR / "curriculum.yaml"

# PyYAML import — required for reading YAML state files
try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required. Install: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Helpers: file I/O
# ---------------------------------------------------------------------------

def read_yaml(path):
    """Read a YAML file and return a dict. Returns {} if missing/empty."""
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def write_yaml(path, data):
    """Atomically write a dict as YAML."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(p) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    os.replace(str(tmp), str(p))


def read_jsonl(path):
    """Read a JSONL file and return a list of dicts. Returns [] if missing/empty."""
    p = Path(path)
    if not p.exists():
        return []
    items = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                items.append(json.loads(stripped))
    return items


def append_jsonl(path, item):
    """Append one JSON line to a JSONL file, creating it if needed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=True) + "\n")


# ---------------------------------------------------------------------------
# Gate evaluation logic
# ---------------------------------------------------------------------------

def navigate_dotpath(data, dotpath):
    """Navigate a dotpath like 'current_assessment.average_competence' into a dict.

    Returns the value at the path, or None if any segment is missing.
    """
    parts = dotpath.split(".")
    current = data
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def resolve_metric(metric_path):
    """Resolve a metric dotpath relative to the agent directory.

    The first segment is the YAML filename (without .yaml extension).
    The remaining segments are navigated within that file.
    Example: 'developmental-stage.current_assessment.average_competence'
      -> reads <agent>/developmental-stage.yaml, navigates current_assessment.average_competence
    """
    parts = metric_path.split(".", 1)
    if len(parts) < 2:
        return None
    filename = parts[0] + ".yaml"
    yaml_path = parts[1]
    data = read_yaml(AGENT_DIR / filename)
    if not data:
        return None
    return navigate_dotpath(data, yaml_path)


def count_matching_jsonl(file_rel, field, value):
    """Count JSONL entries (or nested items) matching a field/value filter.

    Supports array flattening via goals[*].status syntax:
    - field="goals[*].status", value="completed" counts all goals
      with status=completed across all aspiration records.
    - field="status", value="active" counts top-level records.
    """
    path = AGENT_DIR / file_rel
    records = read_jsonl(path)
    count = 0

    # Parse array flatten pattern: "goals[*].status"
    if "[*]." in field:
        array_field, sub_field = field.split("[*].", 1)
        for record in records:
            arr = record.get(array_field, [])
            if isinstance(arr, list):
                for item in arr:
                    if isinstance(item, dict):
                        item_val = navigate_dotpath(item, sub_field)
                        if value == "*" or str(item_val) == str(value):
                            count += 1
    else:
        for record in records:
            record_val = navigate_dotpath(record, field)
            if value == "*" or str(record_val) == str(value):
                count += 1

    return count


def compare(actual, operator, threshold):
    """Compare a numeric value against a threshold using the given operator."""
    if actual is None:
        return False
    try:
        actual = float(actual)
        threshold = float(threshold)
    except (ValueError, TypeError):
        return False

    if operator == ">=":
        return actual >= threshold
    elif operator == ">":
        return actual > threshold
    elif operator == "<=":
        return actual <= threshold
    elif operator == "==":
        return actual == threshold
    return False


def evaluate_gate(gate):
    """Evaluate a single graduation gate. Returns (passed, current_value)."""
    gate_type = gate.get("type", "")

    if gate_type == "metric_threshold":
        metric = gate.get("metric", "")
        operator = gate.get("operator", ">=")
        threshold = gate.get("threshold", 0)
        current_value = resolve_metric(metric)
        passed = compare(current_value, operator, threshold)
        return passed, current_value

    elif gate_type == "count_check":
        file_rel = gate.get("file", "")
        field = gate.get("field", "")
        value = gate.get("value", "*")
        operator = gate.get("operator", ">=")
        threshold = gate.get("threshold", 0)
        current_value = count_matching_jsonl(file_rel, field, value)
        passed = compare(current_value, operator, threshold)
        return passed, current_value

    elif gate_type == "log_scan":
        log_file = gate.get("log_file", "")
        match_field = gate.get("match_field", "")
        match_value = gate.get("match_value", "")
        min_count = gate.get("min_count", 1)
        records = read_jsonl(AGENT_DIR / log_file)
        count = sum(
            1 for r in records
            if str(r.get(match_field, "")) == str(match_value)
        )
        passed = count >= min_count
        return passed, count

    elif gate_type == "command_check":
        command = gate.get("command", "")
        # Safety: only allow commands starting with "bash core/scripts/"
        if not command.startswith("bash core/scripts/"):
            return False, "blocked: unsafe command"
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                timeout=30,
            )
            passed = result.returncode == 0
            return passed, result.returncode
        except (subprocess.TimeoutExpired, OSError) as e:
            return False, str(e)

    return False, "unknown gate type"


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_status(args):
    """Print current curriculum status as JSON."""
    state = read_yaml(CURRICULUM_PATH)
    if not state or not state.get("current_stage"):
        print(json.dumps({
            "configured": False,
            "current_stage": None,
            "stage_name": None,
            "unlocks": {},
            "gates": [],
            "next_stage": None,
        }, indent=2))
        return

    current_id = state["current_stage"]
    stages = state.get("stages", [])
    current = None
    next_stage = None
    found_current = False

    for s in stages:
        if found_current and next_stage is None:
            next_stage = s.get("id")
        if s.get("id") == current_id:
            current = s
            found_current = True

    if not current:
        print(json.dumps({"error": f"Current stage '{current_id}' not found in stages"}))
        return

    # Build gate status from stored gate_status if available
    gate_report = []
    gate_statuses = current.get("gate_status", [])
    for i, gate in enumerate(current.get("graduation_gates", [])):
        gs = gate_statuses[i] if i < len(gate_statuses) else {}
        gate_report.append({
            "index": i,
            "id": gate.get("id", f"gate_{i}"),
            "type": gate.get("type", ""),
            "description": gate.get("description", ""),
            "passed": gs.get("passed", False),
            "current_value": gs.get("current_value"),
            "last_checked": gs.get("last_checked"),
        })

    framework = read_yaml(FRAMEWORK_PATH)
    unlock_defaults = {}
    for cap, info in framework.get("unlock_capabilities", {}).items():
        if isinstance(info, dict):
            unlock_defaults[cap] = info.get("default", False)

    unlocks = dict(unlock_defaults)
    unlocks.update(current.get("unlocks", {}))

    print(json.dumps({
        "configured": True,
        "current_stage": current_id,
        "stage_name": current.get("name", ""),
        "stage_description": current.get("description", ""),
        "unlocks": unlocks,
        "gates": gate_report,
        "gates_total": len(gate_report),
        "gates_passed": sum(1 for g in gate_report if g["passed"]),
        "next_stage": next_stage,
    }, indent=2))


def cmd_evaluate(args):
    """Evaluate all gates for the current stage. Updates gate_status in state file."""
    state = read_yaml(CURRICULUM_PATH)
    if not state or not state.get("current_stage"):
        print(json.dumps({"configured": False, "all_passed": False, "gates": []}))
        return

    current_id = state["current_stage"]
    stages = state.get("stages", [])
    current_idx = None

    for i, s in enumerate(stages):
        if s.get("id") == current_id:
            current_idx = i
            break

    if current_idx is None:
        print(json.dumps({"error": f"Stage '{current_id}' not found"}))
        return

    current = stages[current_idx]
    gates = current.get("graduation_gates", [])

    if not gates:
        # Terminal stage — no gates means always "passed"
        print(json.dumps({
            "configured": True,
            "current_stage": current_id,
            "all_passed": True,
            "terminal_stage": True,
            "gates": [],
        }))
        return

    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    gate_results = []
    all_passed = True

    for i, gate in enumerate(gates):
        passed, current_value = evaluate_gate(gate)
        gate_results.append({
            "gate_index": i,
            "id": gate.get("id", f"gate_{i}"),
            "type": gate.get("type", ""),
            "description": gate.get("description", ""),
            "passed": passed,
            "current_value": current_value,
            "last_checked": now,
        })
        if not passed:
            all_passed = False

    # Update gate_status in state
    current["gate_status"] = gate_results
    state["stages"][current_idx] = current
    write_yaml(CURRICULUM_PATH, state)

    print(json.dumps({
        "configured": True,
        "current_stage": current_id,
        "stage_name": current.get("name", ""),
        "all_passed": all_passed,
        "gates_total": len(gate_results),
        "gates_passed_count": sum(1 for g in gate_results if g["passed"]),
        "gates": gate_results,
    }, indent=2))


def cmd_promote(args):
    """Advance to next stage if all gates pass. Appends to promotion log."""
    state = read_yaml(CURRICULUM_PATH)
    if not state or not state.get("current_stage"):
        print(json.dumps({"promoted": False, "reason": "curriculum not configured"}))
        return

    current_id = state["current_stage"]
    stages = state.get("stages", [])

    # Find current and next stage
    current_idx = None
    for i, s in enumerate(stages):
        if s.get("id") == current_id:
            current_idx = i
            break

    if current_idx is None:
        print(json.dumps({"promoted": False, "reason": f"stage '{current_id}' not found"}))
        return

    current = stages[current_idx]
    gates = current.get("graduation_gates", [])

    # Evaluate gates
    if gates:
        all_passed = True
        gate_values = []
        for i, gate in enumerate(gates):
            passed, value = evaluate_gate(gate)
            gate_values.append({"gate_index": i, "value": value, "passed": passed})
            if not passed:
                all_passed = False

        if not all_passed:
            print(json.dumps({
                "promoted": False,
                "reason": "not all gates passed",
                "current_stage": current_id,
                "gates": gate_values,
            }, indent=2))
            return
    else:
        gate_values = []

    # Find next stage
    next_idx = current_idx + 1
    if next_idx >= len(stages):
        print(json.dumps({
            "promoted": False,
            "reason": "already at terminal stage",
            "current_stage": current_id,
        }))
        return

    next_stage = stages[next_idx]
    next_id = next_stage.get("id")
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    # Update state: advance current_stage, update stage_history
    state["current_stage"] = next_id

    history = state.get("stage_history", [])
    # Close current stage entry
    for entry in history:
        if entry.get("stage_id") == current_id and entry.get("exited") is None:
            entry["exited"] = now
    # Add new stage entry
    history.append({"stage_id": next_id, "entered": now, "exited": None})
    state["stage_history"] = history

    # Initialize gate_status for new stage
    new_gates = next_stage.get("graduation_gates", [])
    next_stage["gate_status"] = [
        {"gate_index": i, "passed": False, "last_checked": None, "current_value": None}
        for i in range(len(new_gates))
    ]
    state["stages"][next_idx] = next_stage

    write_yaml(CURRICULUM_PATH, state)

    # Append promotion log
    promotion_entry = {
        "date": now,
        "from_stage": current_id,
        "to_stage": next_id,
        "gates_passed": gate_values,
        "actor": "curriculum.py",
    }
    append_jsonl(PROMOTIONS_PATH, promotion_entry)

    print(json.dumps({
        "promoted": True,
        "from_stage": current_id,
        "from_name": current.get("name", ""),
        "to_stage": next_id,
        "to_name": next_stage.get("name", ""),
        "unlocks": next_stage.get("unlocks", {}),
        "date": now,
    }, indent=2))


def cmd_contract_check(args):
    """Check if an action is permitted by the current curriculum stage."""
    action = args.action
    state = read_yaml(CURRICULUM_PATH)

    # Graceful degradation: no curriculum = all permitted
    if not state or not state.get("current_stage"):
        print(json.dumps({"action": action, "permitted": True, "reason": "no curriculum configured"}))
        sys.exit(0)

    current_id = state["current_stage"]
    stages = state.get("stages", [])
    current = None
    for s in stages:
        if s.get("id") == current_id:
            current = s
            break

    if not current:
        print(json.dumps({"action": action, "permitted": True, "reason": f"stage '{current_id}' not found"}))
        sys.exit(0)

    # Read unlock defaults from framework
    framework = read_yaml(FRAMEWORK_PATH)
    unlock_defaults = {}
    for cap, info in framework.get("unlock_capabilities", {}).items():
        if isinstance(info, dict):
            unlock_defaults[cap] = info.get("default", False)

    # Merge: stage unlocks override defaults
    unlocks = dict(unlock_defaults)
    unlocks.update(current.get("unlocks", {}))

    permitted = unlocks.get(action, True)  # Unknown actions default to permitted

    # Find which stage unlocks this action (for informative messages)
    unlocking_stage = None
    if not permitted:
        for s in stages:
            s_unlocks = dict(unlock_defaults)
            s_unlocks.update(s.get("unlocks", {}))
            if s_unlocks.get(action, False):
                unlocking_stage = s.get("name", s.get("id"))
                break

    result = {
        "action": action,
        "permitted": permitted,
        "current_stage": current_id,
        "stage_name": current.get("name", ""),
    }
    if unlocking_stage:
        result["unlocks_at"] = unlocking_stage

    print(json.dumps(result))
    sys.exit(0 if permitted else 1)


def cmd_audit(args):
    """Verify curriculum state consistency."""
    state = read_yaml(CURRICULUM_PATH)
    issues = []

    if not state or not state.get("current_stage"):
        print(json.dumps({"status": "unconfigured", "issues": []}))
        return

    current_id = state["current_stage"]
    stages = state.get("stages", [])
    stage_ids = [s.get("id") for s in stages]

    # Check current_stage exists in stages list
    if current_id not in stage_ids:
        issues.append(f"current_stage '{current_id}' not found in stages list")

    # Check stage_history consistency
    history = state.get("stage_history", [])
    for entry in history:
        if entry.get("stage_id") not in stage_ids:
            issues.append(f"stage_history references unknown stage '{entry.get('stage_id')}'")

    # Check only one history entry has exited=null
    open_entries = [e for e in history if e.get("exited") is None]
    if len(open_entries) > 1:
        issues.append(f"Multiple open stage_history entries: {[e.get('stage_id') for e in open_entries]}")
    elif len(open_entries) == 1 and open_entries[0].get("stage_id") != current_id:
        issues.append(
            f"Open history entry '{open_entries[0].get('stage_id')}' doesn't match current_stage '{current_id}'"
        )

    # Check promotions log matches history transitions
    promotions = read_jsonl(PROMOTIONS_PATH)
    for promo in promotions:
        from_s = promo.get("from_stage")
        to_s = promo.get("to_stage")
        if from_s not in stage_ids:
            issues.append(f"Promotion log references unknown from_stage '{from_s}'")
        if to_s not in stage_ids:
            issues.append(f"Promotion log references unknown to_stage '{to_s}'")

    # Check stage IDs follow cur-NN pattern
    cur_id_re = re.compile(r"^cur-\d{2}$")
    for sid in stage_ids:
        if not cur_id_re.match(str(sid)):
            issues.append(f"Stage ID '{sid}' doesn't match cur-NN pattern")

    print(json.dumps({
        "status": "ok" if not issues else "issues_found",
        "current_stage": current_id,
        "total_stages": len(stages),
        "promotions_count": len(promotions),
        "issues": issues,
    }, indent=2))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Curriculum gate evaluator — deterministic stage promotion"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="Print current curriculum status")
    subparsers.add_parser("evaluate", help="Evaluate all gates for current stage")
    subparsers.add_parser("promote", help="Advance stage if all gates pass")

    cc_parser = subparsers.add_parser("contract-check", help="Check if action is permitted")
    cc_parser.add_argument(
        "--action",
        required=True,
        help="Capability to check (e.g., allow_self_edits, allow_forge_skill)",
    )

    subparsers.add_parser("audit", help="Verify state consistency")

    args = parser.parse_args()

    commands = {
        "status": cmd_status,
        "evaluate": cmd_evaluate,
        "promote": cmd_promote,
        "contract-check": cmd_contract_check,
        "audit": cmd_audit,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
