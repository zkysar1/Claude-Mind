"""GET/POST /v1/curriculum/{status,evaluate,promote,contract-check,audit}.

Daemonises core/scripts/curriculum.py. All 5 commands are AGENT-scoped (read
agents/<agent>/curriculum.yaml) → every one is X-Mind-Agent gated. Two write
(evaluate, promote); three are pure reads (status, contract-check, audit).

  GET  /v1/curriculum/status         (cmd_status)
  POST /v1/curriculum/evaluate       (cmd_evaluate)
  POST /v1/curriculum/promote        (cmd_promote)
  GET  /v1/curriculum/contract-check (cmd_contract_check) ?action=<cap>
  GET  /v1/curriculum/audit          (cmd_audit)

curriculum.py is daemon-import-unsafe (assert_agent_dir at module level), so the
pure helpers are copied VERBATIM and parameterised to take agent_dir /
project_root instead of the module-level _paths constants.

BYTE-COMPAT — TWO surfaces:
  1. WRITTEN FILES. curriculum.yaml is written RAW via the CLI's exact path:
     yaml.dump(data, f, default_flow_style=False, allow_unicode=True,
     sort_keys=False) with the DEFAULT yaml.Dumper (NOT _fileops' CSafeDumper),
     tmp+os.replace. curriculum-promotions.jsonl is json.dumps(item,
     ensure_ascii=True)+"\n" appended in mode "a". NO file lock, NO .history
     snapshot, NO changelog entry — the CLI does none of these and curriculum
     is a high-churn state file where snapshots would be noise. Adding any of
     them would break byte-compat AND hold locks the CLI never holds.
  2. STDOUT. Every handler returns Response.text(json.dumps(obj[, indent=2])
     + "\n") — ensure_ascii=True (the CLI default, NOT the ensure_ascii=False
     used by journal/changelog reads) so the wrapper cut prints byte-identical
     stdout. Indent is per-BRANCH: the CLI mixes indent=2 (main/success paths)
     and no-indent (early-return/error paths). Each branch is replicated.

CORRECTED TRAP (adversarial verify, blocking): FOUR gate types resolve paths
relative to the agent/project root — metric_threshold (curriculum.py:122),
count_check (:136), log_scan (:208) all read AGENT_DIR/<file>, and command_check
(:225) runs subprocess with cwd=PROJECT_ROOT. ALL are routed through
ctx.paths.agent / ctx.paths.project_root here. The original spec only flagged
command_check; the file-reading three are the higher-bug-risk omission.

contract-check's CLI sys.exit(0|1) is a SHELL SIGNAL, not an error: exit(1) on
not-permitted becomes HTTP 200 with permitted=false, never a 4xx.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# Agent-header gate (all 5 commands are agent-scoped)
# ---------------------------------------------------------------------------

def _require_agent_header(ctx):
    from ..server import Response
    agent = (ctx.headers.get("x-ayoai-agent") or "").strip()
    if not agent:
        return Response.error(
            400, "missing_agent_header",
            "X-Mind-Agent header required for curriculum operations (g-115-957).")
    return None


def _json_out(obj, indent):
    """Mirror the CLI's print(json.dumps(obj[, indent=2])).

    ensure_ascii defaults True (the CLI default). print() adds one trailing
    newline, so the body == CLI stdout byte-for-byte.
    """
    from ..server import Response
    s = json.dumps(obj, indent=indent) if indent is not None else json.dumps(obj)
    return Response.text(s + "\n", content_type="application/json")


def _paths(ctx):
    agent_dir = Path(ctx.paths.agent)
    project_root = Path(ctx.paths.project_root)
    curriculum_path = agent_dir / "curriculum.yaml"
    promotions_path = agent_dir / "curriculum-promotions.jsonl"
    framework_path = project_root / "core" / "config" / "curriculum.yaml"
    return agent_dir, project_root, curriculum_path, promotions_path, framework_path


# ---------------------------------------------------------------------------
# File I/O helpers — verbatim from curriculum.py:48-87 (RAW, no lock/history)
# ---------------------------------------------------------------------------

def _read_yaml(path):
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def _write_yaml(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(p) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    os.replace(str(tmp), str(p))


def _read_jsonl(path):
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


def _append_jsonl(path, item):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=True) + "\n")


# ---------------------------------------------------------------------------
# Gate evaluation — verbatim from curriculum.py:94-234, AGENT_DIR / PROJECT_ROOT
# routed through agent_dir / project_root parameters
# ---------------------------------------------------------------------------

def _navigate_dotpath(data, dotpath):
    parts = dotpath.split(".")
    current = data
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _resolve_metric(agent_dir, metric_path):
    parts = metric_path.split(".", 1)
    if len(parts) < 2:
        return None
    filename = parts[0] + ".yaml"
    yaml_path = parts[1]
    data = _read_yaml(agent_dir / filename)  # CORRECTED: AGENT_DIR -> agent_dir
    if not data:
        return None
    return _navigate_dotpath(data, yaml_path)


def _count_matching_jsonl(agent_dir, file_rel, field, value):
    path = agent_dir / file_rel  # CORRECTED: AGENT_DIR -> agent_dir
    records = _read_jsonl(path)
    count = 0
    if "[*]." in field:
        array_field, sub_field = field.split("[*].", 1)
        for record in records:
            arr = record.get(array_field, [])
            if isinstance(arr, list):
                for item in arr:
                    if isinstance(item, dict):
                        item_val = _navigate_dotpath(item, sub_field)
                        if value == "*" or str(item_val) == str(value):
                            count += 1
    else:
        for record in records:
            record_val = _navigate_dotpath(record, field)
            if value == "*" or str(record_val) == str(value):
                count += 1
    return count


def _compare(actual, operator, threshold):
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


def _evaluate_gate(gate, agent_dir, project_root):
    gate_type = gate.get("type", "")

    if gate_type == "metric_threshold":
        metric = gate.get("metric", "")
        operator = gate.get("operator", ">=")
        threshold = gate.get("threshold", 0)
        current_value = _resolve_metric(agent_dir, metric)
        passed = _compare(current_value, operator, threshold)
        return passed, current_value

    elif gate_type == "count_check":
        file_rel = gate.get("file", "")
        field = gate.get("field", "")
        value = gate.get("value", "*")
        operator = gate.get("operator", ">=")
        threshold = gate.get("threshold", 0)
        current_value = _count_matching_jsonl(agent_dir, file_rel, field, value)
        passed = _compare(current_value, operator, threshold)
        return passed, current_value

    elif gate_type == "log_scan":
        log_file = gate.get("log_file", "")
        match_field = gate.get("match_field", "")
        match_value = gate.get("match_value", "")
        min_count = gate.get("min_count", 1)
        records = _read_jsonl(agent_dir / log_file)  # CORRECTED: AGENT_DIR -> agent_dir
        count = sum(
            1 for r in records
            if str(r.get(match_field, "")) == str(match_value)
        )
        passed = count >= min_count
        return passed, count

    elif gate_type == "command_check":
        command = gate.get("command", "")
        if not command.startswith("bash core/scripts/"):
            return False, "blocked: unsafe command"
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=str(project_root),  # CORRECTED: PROJECT_ROOT -> project_root
                capture_output=True,
                timeout=30,
            )
            passed = result.returncode == 0
            return passed, result.returncode
        except (subprocess.TimeoutExpired, OSError) as e:
            return False, str(e)

    return False, "unknown gate type"


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def status(ctx) -> "Response":  # type: ignore[name-defined]
    gate = _require_agent_header(ctx)
    if gate is not None:
        return gate
    _, _, curriculum_path, _, framework_path = _paths(ctx)

    state = _read_yaml(curriculum_path)
    if not state or not state.get("current_stage"):
        return _json_out({
            "configured": False,
            "current_stage": None,
            "stage_name": None,
            "unlocks": {},
            "gates": [],
            "next_stage": None,
        }, indent=2)

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
        return _json_out({"error": f"Current stage '{current_id}' not found in stages"}, indent=None)

    gate_report = []
    gate_statuses = current.get("gate_status", [])
    for i, g in enumerate(current.get("graduation_gates", [])):
        gs = gate_statuses[i] if i < len(gate_statuses) else {}
        gate_report.append({
            "index": i,
            "id": g.get("id", f"gate_{i}"),
            "type": g.get("type", ""),
            "description": g.get("description", ""),
            "passed": gs.get("passed", False),
            "current_value": gs.get("current_value"),
            "last_checked": gs.get("last_checked"),
        })

    framework = _read_yaml(framework_path)
    unlock_defaults = {}
    for cap, info in framework.get("unlock_capabilities", {}).items():
        if isinstance(info, dict):
            unlock_defaults[cap] = info.get("default", False)

    unlocks = dict(unlock_defaults)
    unlocks.update(current.get("unlocks", {}))

    return _json_out({
        "configured": True,
        "current_stage": current_id,
        "stage_name": current.get("name", ""),
        "stage_description": current.get("description", ""),
        "unlocks": unlocks,
        "gates": gate_report,
        "gates_total": len(gate_report),
        "gates_passed": sum(1 for g in gate_report if g["passed"]),
        "next_stage": next_stage,
    }, indent=2)


def evaluate(ctx) -> "Response":  # type: ignore[name-defined]
    gate = _require_agent_header(ctx)
    if gate is not None:
        return gate
    agent_dir, project_root, curriculum_path, _, _ = _paths(ctx)

    state = _read_yaml(curriculum_path)
    if not state or not state.get("current_stage"):
        return _json_out({"configured": False, "all_passed": False, "gates": []}, indent=None)

    current_id = state["current_stage"]
    stages = state.get("stages", [])
    current_idx = None
    for i, s in enumerate(stages):
        if s.get("id") == current_id:
            current_idx = i
            break

    if current_idx is None:
        return _json_out({"error": f"Stage '{current_id}' not found"}, indent=None)

    current = stages[current_idx]
    gates = current.get("graduation_gates", [])

    if not gates:
        return _json_out({
            "configured": True,
            "current_stage": current_id,
            "all_passed": True,
            "terminal_stage": True,
            "gates": [],
        }, indent=None)

    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    gate_results = []
    all_passed = True
    for i, g in enumerate(gates):
        passed, current_value = _evaluate_gate(g, agent_dir, project_root)
        gate_results.append({
            "gate_index": i,
            "id": g.get("id", f"gate_{i}"),
            "type": g.get("type", ""),
            "description": g.get("description", ""),
            "passed": passed,
            "current_value": current_value,
            "last_checked": now,
        })
        if not passed:
            all_passed = False

    current["gate_status"] = gate_results
    state["stages"][current_idx] = current
    _write_yaml(curriculum_path, state)

    return _json_out({
        "configured": True,
        "current_stage": current_id,
        "stage_name": current.get("name", ""),
        "all_passed": all_passed,
        "gates_total": len(gate_results),
        "gates_passed_count": sum(1 for g in gate_results if g["passed"]),
        "gates": gate_results,
    }, indent=2)


def promote(ctx) -> "Response":  # type: ignore[name-defined]
    gate = _require_agent_header(ctx)
    if gate is not None:
        return gate
    agent_dir, project_root, curriculum_path, promotions_path, _ = _paths(ctx)

    state = _read_yaml(curriculum_path)
    if not state or not state.get("current_stage"):
        return _json_out({"promoted": False, "reason": "curriculum not configured"}, indent=None)

    current_id = state["current_stage"]
    stages = state.get("stages", [])
    current_idx = None
    for i, s in enumerate(stages):
        if s.get("id") == current_id:
            current_idx = i
            break

    if current_idx is None:
        return _json_out({"promoted": False, "reason": f"stage '{current_id}' not found"}, indent=None)

    current = stages[current_idx]
    gates = current.get("graduation_gates", [])

    if gates:
        all_passed = True
        gate_values = []
        for i, g in enumerate(gates):
            passed, value = _evaluate_gate(g, agent_dir, project_root)
            gate_values.append({"gate_index": i, "value": value, "passed": passed})
            if not passed:
                all_passed = False

        if not all_passed:
            return _json_out({
                "promoted": False,
                "reason": "not all gates passed",
                "current_stage": current_id,
                "gates": gate_values,
            }, indent=2)
    else:
        gate_values = []

    next_idx = current_idx + 1
    if next_idx >= len(stages):
        return _json_out({
            "promoted": False,
            "reason": "already at terminal stage",
            "current_stage": current_id,
        }, indent=None)

    next_stage = stages[next_idx]
    next_id = next_stage.get("id")
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    state["current_stage"] = next_id

    history = state.get("stage_history", [])
    for entry in history:
        if entry.get("stage_id") == current_id and entry.get("exited") is None:
            entry["exited"] = now
    history.append({"stage_id": next_id, "entered": now, "exited": None})
    state["stage_history"] = history

    new_gates = next_stage.get("graduation_gates", [])
    next_stage["gate_status"] = [
        {"gate_index": i, "passed": False, "last_checked": None, "current_value": None}
        for i in range(len(new_gates))
    ]
    state["stages"][next_idx] = next_stage

    _write_yaml(curriculum_path, state)

    promotion_entry = {
        "date": now,
        "from_stage": current_id,
        "to_stage": next_id,
        "gates_passed": gate_values,
        "actor": "curriculum.py",  # literal — must NOT change (byte-compat)
    }
    _append_jsonl(promotions_path, promotion_entry)

    return _json_out({
        "promoted": True,
        "from_stage": current_id,
        "from_name": current.get("name", ""),
        "to_stage": next_id,
        "to_name": next_stage.get("name", ""),
        "unlocks": next_stage.get("unlocks", {}),
        "date": now,
    }, indent=2)


def contract_check(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    gate = _require_agent_header(ctx)
    if gate is not None:
        return gate

    action = ctx.query.get("action")
    if not action:
        return Response.error(400, "missing_param", "action query param required")

    _, _, curriculum_path, _, framework_path = _paths(ctx)
    state = _read_yaml(curriculum_path)

    # Graceful degradation: no curriculum = all permitted (CLI sys.exit(0)).
    if not state or not state.get("current_stage"):
        return _json_out({"action": action, "permitted": True,
                          "reason": "no curriculum configured"}, indent=None)

    current_id = state["current_stage"]
    stages = state.get("stages", [])
    current = None
    for s in stages:
        if s.get("id") == current_id:
            current = s
            break

    if not current:
        return _json_out({"action": action, "permitted": True,
                          "reason": f"stage '{current_id}' not found"}, indent=None)

    framework = _read_yaml(framework_path)
    unlock_defaults = {}
    for cap, info in framework.get("unlock_capabilities", {}).items():
        if isinstance(info, dict):
            unlock_defaults[cap] = info.get("default", False)

    unlocks = dict(unlock_defaults)
    unlocks.update(current.get("unlocks", {}))

    permitted = unlocks.get(action, True)  # unknown actions default permitted

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

    # CLI sys.exit(0 if permitted else 1) is a SHELL SIGNAL — the daemon returns
    # HTTP 200 with permitted in the body either way, never a 4xx.
    return _json_out(result, indent=None)


def audit(ctx) -> "Response":  # type: ignore[name-defined]
    gate = _require_agent_header(ctx)
    if gate is not None:
        return gate
    _, _, curriculum_path, promotions_path, _ = _paths(ctx)

    state = _read_yaml(curriculum_path)
    issues = []

    if not state or not state.get("current_stage"):
        return _json_out({"status": "unconfigured", "issues": []}, indent=None)

    current_id = state["current_stage"]
    stages = state.get("stages", [])
    stage_ids = [s.get("id") for s in stages]

    if current_id not in stage_ids:
        issues.append(f"current_stage '{current_id}' not found in stages list")

    history = state.get("stage_history", [])
    for entry in history:
        if entry.get("stage_id") not in stage_ids:
            issues.append(f"stage_history references unknown stage '{entry.get('stage_id')}'")

    open_entries = [e for e in history if e.get("exited") is None]
    if len(open_entries) > 1:
        issues.append(f"Multiple open stage_history entries: {[e.get('stage_id') for e in open_entries]}")
    elif len(open_entries) == 1 and open_entries[0].get("stage_id") != current_id:
        issues.append(
            f"Open history entry '{open_entries[0].get('stage_id')}' doesn't match current_stage '{current_id}'"
        )

    promotions = _read_jsonl(promotions_path)
    for promo in promotions:
        from_s = promo.get("from_stage")
        to_s = promo.get("to_stage")
        if from_s not in stage_ids:
            issues.append(f"Promotion log references unknown from_stage '{from_s}'")
        if to_s not in stage_ids:
            issues.append(f"Promotion log references unknown to_stage '{to_s}'")

    cur_id_re = re.compile(r"^cur-\d{2}$")
    for sid in stage_ids:
        if not cur_id_re.match(str(sid)):
            issues.append(f"Stage ID '{sid}' doesn't match cur-NN pattern")

    return _json_out({
        "status": "ok" if not issues else "issues_found",
        "current_stage": current_id,
        "total_stages": len(stages),
        "promotions_count": len(promotions),
        "issues": issues,
    }, indent=2)


def register(routes) -> None:
    routes[("GET", "/v1/curriculum/status")] = status
    routes[("POST", "/v1/curriculum/evaluate")] = evaluate
    routes[("POST", "/v1/curriculum/promote")] = promote
    routes[("GET", "/v1/curriculum/contract-check")] = contract_check
    routes[("GET", "/v1/curriculum/audit")] = audit
