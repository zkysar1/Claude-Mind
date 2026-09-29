#!/usr/bin/env python3
"""forge-gate-check -- /forge-skill's readiness gate as ONE command ().

Until this script the type/capability half of the gate existed only as SKILL.md
prose: the model read the gap's type, looked up that type's forge_gate, read
the related category's capability, and compared floats it had also read from
prose. A frontier model manages that; a 27B model got it wrong in both
directions (2026-09-05). aspirations-evolve Step 9 carried a SECOND, different
copy that gated on the agent's developmental stage instead. guard-399: an "LLM
must do X at step N" instruction needs the gate that catches the skip. Both
call sites now run this script -- one script, one truth.

    bash core/scripts/forge-gate-check.sh <gap-id> [--category <tree-node-key>] [--json]
    bash core/scripts/forge-gate-check.sh --all [--json]

One verdict line per gap, printing every input it decided on:
    PASS    every criterion is met
    WAIVED  requested_by: user -- the curriculum contract and the capability
            gate are waived (both measure whether the agent may forge ON ITS
            OWN, and a user request answers that); every other criterion applies
    BLOCK   a criterion failed or could not be shown; reasons= names each one

Every threshold is READ at run time from its source, never restated here:
    status             not suppressing per core/config/skill-gaps.yaml
                       gap_statuses (referenced, never copied -- guard-426)
    times_encountered  >= core/config/skill-gaps.yaml config.forge_threshold
    estimated_value    its leading word must rank medium or high; prose that
                       does not start with low|medium|high cannot be ranked
                       and BLOCKs (g-318-157)
    type               the gap's type, utility when absent (g-115-3131);
                       forge_gate = core/config/skill-gaps.yaml gap_types[type]
    capability         --category KEY: the node's STORED capability_level
                       (guard-1195) on core/config/tree.yaml competence_mapping,
                       via backfill-tree-node-fields.py's loader; a missing or
                       unmapped level is derived from the node's confidence
                       with that file's _graduate_from_confidence.
                       Without --category: the agent's developmental-stage.yaml
                       current_assessment.tree_maturity, on the scale that
                       produced it (_competence.COMPETENCE_MAPPING). Gaps carry
                       no category field, so evolve always takes this path.
    contract           curriculum-contract-check.sh --action allow_forge_skill;
                       a result that cannot be read BLOCKs (fail closed: a
                       skipped forge re-qualifies next pass, a wrong one ships)

Exit: 0 = PASS or WAIVED (with --all: at least one gap is forge-ready)
      1 = BLOCK (with --all: no gap is)
      2 = could not evaluate: usage, an unreadable config or gap store, an
          unknown gap id, an unknown category key (read only when the
          capability axis applies, so never for a WAIVED gap), or an
          internal error -- a crash never exits 1, the BLOCK code
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import traceback
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import yaml  # noqa: E402

from _competence import COMPETENCE_MAPPING  # noqa: E402
from _paths import AGENT_DIR, CONFIG_DIR, META_DIR  # noqa: E402
from _runtime_bash import bash_cmd  # noqa: E402

# The Forge Criteria compare estimated_value against "medium"; the store
# declares no value vocabulary, so these three words are the whole scale.
VALUE_LEVELS = ("low", "medium", "high")
VALUE_FLOOR = "medium"
_VALUE_RE = re.compile(r"\s*(low|medium|high)\b", re.IGNORECASE)

# : a gap with no `type` is a utility gap. The decision is recorded
# as a comment under gap_types in core/config/skill-gaps.yaml, not as a key.
TYPELESS_DEFAULT = "utility"

SUBPROCESS_TIMEOUT = 120

# BLOCK tally labels for --all, one per reason code.
REASON_LABELS = {
    "status": "status suppresses forging or is undeclared",
    "times": "times_encountered below forge_threshold",
    "value": "estimated_value below medium",
    "value-unrankable": "estimated_value has no leading low|medium|high",
    "type": "type has no forge_gate",
    "capability": "capability below forge_gate",
    "capability-unreadable": "capability could not be read",
    "contract-blocked": "curriculum contract blocks allow_forge_skill",
    "contract-unreadable": "curriculum contract could not be read",
}

_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class GateError(Exception):
    """The gate could not evaluate at all (exit 2)."""


def _ensure_local(path: Path) -> None:
    # own-cloud read-path idiom (tree_match.parse_front_matter): materialize a
    # backend-only file before the local read. Report, never raise.
    try:
        from storage_backend import get_backend
        get_backend().ensure_local(path)
    except Exception as e:
        try:
            from storage_backend import note_swallowed_backend_error
            note_swallowed_backend_error("ensure_local", path, e)
        except Exception:
            pass


def _load_yaml(path: Path, what: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.load(f, Loader=_YAML_LOADER)
    except (OSError, yaml.YAMLError) as e:
        raise GateError(f"cannot read {what} {path}: {e}")
    if not isinstance(data, dict):
        raise GateError(f"{what} {path} is not a mapping")
    return data


def _load_resolver():
    """backfill-tree-node-fields.py owns the tree.yaml competence loader and
    the confidence->level resolver the Forge Criteria name. Load them; never
    copy them."""
    spec = importlib.util.spec_from_file_location(
        "backfill_tree_node_fields", SCRIPT_DIR / "backfill-tree-node-fields.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._load_competence_thresholds, mod._graduate_from_confidence


def load_config() -> dict:
    path = CONFIG_DIR / "skill-gaps.yaml"
    sg = _load_yaml(path, "skill-gap config")
    try:
        forge_threshold = sg["config"]["forge_threshold"]
        gates = {t: v["forge_gate"] for t, v in sg["gap_types"].items()}
        statuses = sg["gap_statuses"]
    except (KeyError, TypeError, AttributeError) as e:
        raise GateError(f"{path} lacks {e}")
    load_thresholds, graduate = _load_resolver()
    tree_scale = load_thresholds()
    if not tree_scale:
        raise GateError("core/config/tree.yaml domain_health.competence_mapping is missing or empty")
    for gtype, gate in gates.items():
        if gate not in tree_scale or gate not in COMPETENCE_MAPPING:
            raise GateError(f"gap_types.{gtype}.forge_gate {gate!r} is not a competence level")
    return {
        "forge_threshold": forge_threshold,
        "gates": gates,
        "statuses": set(statuses),
        "suppressing": {s for s, v in statuses.items()
                        if isinstance(v, dict) and v.get("suppresses_forge")},
        "tree_scale": tree_scale,
        "graduate": graduate,
    }


def load_gaps() -> list:
    if META_DIR is None:
        raise GateError("META_DIR unresolved (bind an agent or set MIND_META)")
    path = META_DIR / "skill-gaps.yaml"
    _ensure_local(path)
    gaps = _load_yaml(path, "gap store").get("gaps")
    if not isinstance(gaps, list):
        raise GateError(f"{path} has no gaps list")
    return gaps


def find_gap(gaps: list, gap_id: str) -> dict:
    hits = [g for g in gaps if isinstance(g, dict) and g.get("id") == gap_id]
    if not hits:
        raise GateError(f"no gap {gap_id!r} in the gap store ({len(gaps)} gaps)")
    if len(hits) > 1:
        raise GateError(f"gap id {gap_id!r} appears {len(hits)} times in the gap store")
    return hits[0]


def _run(script: str, *args) -> subprocess.CompletedProcess:
    """Run a sibling .sh wrapper via bash_cmd (guard-580: never a bare "bash")."""
    try:
        return subprocess.run(bash_cmd(SCRIPT_DIR / script, *args), capture_output=True,
                              text=True, timeout=SUBPROCESS_TIMEOUT)
    except ValueError as e:  # bash_cmd: an argument win32 would corrupt in transit
        return subprocess.CompletedProcess([script, *args], 2, "", str(e))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            [script, *args], 124, "", f"timed out after {SUBPROCESS_TIMEOUT}s")


def category_capability(key: str, cfg: dict) -> dict:
    proc = _run("tree-read.sh", "--node", key)
    if proc.returncode != 0:
        detail = (proc.stdout.strip() or proc.stderr.strip())[:300]
        raise GateError(f"category node {key!r}: tree-read.sh --node rc={proc.returncode}: {detail}")
    try:
        node = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise GateError(f"tree-read.sh --node {key} printed non-JSON ({len(proc.stdout)} bytes): {e}")
    if not isinstance(node, dict):
        raise GateError(f"tree-read.sh --node {key} printed a {type(node).__name__}, not a node")
    scale = cfg["tree_scale"]
    level, confidence = node.get("capability_level"), node.get("confidence")
    cap = {"source": f"category:{key}", "scale_name": "tree.yaml",
           "scale": scale, "confidence": confidence}
    if level in scale:
        cap.update(level=level, level_source="stored")
    elif isinstance(confidence, (int, float)):
        cap.update(level=cfg["graduate"](confidence, scale), level_source="derived")
    else:
        cap["error"] = (f"node {key} has neither a capability_level on the "
                        f"tree.yaml scale (stored: {level!r}) nor a numeric confidence")
        return cap
    cap["value"] = scale[cap["level"]]
    return cap


def agent_capability(cfg: dict) -> dict:
    cap = {"source": "agent-stage", "scale_name": "_competence",
           "scale": COMPETENCE_MAPPING, "confidence": None}
    if AGENT_DIR is None:
        cap["error"] = "no agent bound (AGENT_DIR unresolved), so no developmental stage to read"
        return cap
    path = AGENT_DIR / "developmental-stage.yaml"
    _ensure_local(path)
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_YAML_LOADER)
    except (OSError, yaml.YAMLError) as e:
        cap["error"] = f"cannot read {path}: {e}"
        return cap
    assessment = data.get("current_assessment") if isinstance(data, dict) else None
    maturity = assessment.get("tree_maturity") if isinstance(assessment, dict) else None
    if not isinstance(maturity, (int, float)):
        cap["error"] = f"{path} has no numeric current_assessment.tree_maturity"
        return cap
    cap.update(confidence=maturity, value=maturity, level_source="derived",
               level=cfg["graduate"](maturity, COMPETENCE_MAPPING))
    return cap


def contract_check() -> dict:
    proc = _run("curriculum-contract-check.sh", "--action", "allow_forge_skill")
    try:
        body = json.loads(proc.stdout)
    except json.JSONDecodeError:
        body = None
    permitted = body.get("permitted") if isinstance(body, dict) else None
    # The wrapper's own contract: exit 0 = permitted, exit 1 = not permitted
    # OR the daemon could not answer. Only a body saying permitted=false is a
    # real curriculum block; any other exit 1 is an unreadable result.
    if proc.returncode == 0:
        result = "permitted"
    elif proc.returncode == 1 and permitted is False:
        result = "blocked"
    else:
        result = "unreadable"
    out = {"result": result, "rc": proc.returncode}
    if isinstance(body, dict):
        for k in ("current_stage", "stage_name", "unlocks_at", "reason"):
            if body.get(k):
                out[k] = body[k]
    if result == "unreadable":
        out["detail"] = (proc.stderr.strip() or proc.stdout.strip())[:300]
    return out


def evaluate(gap: dict, cfg: dict, capability, contract) -> dict:
    reasons = []  # (code, text)
    # A malformed record BLOCKs on its own line; it must never crash the run
    # (with --all, one crash would hide every other gap behind exit 1).
    status = gap.get("status")
    if not isinstance(status, str) or status not in cfg["statuses"]:
        reasons.append(("status", f"status {status!r} is not declared in gap_statuses"))
    elif status in cfg["suppressing"]:
        reasons.append(("status", f"status {status} suppresses forging"))

    times, threshold = gap.get("times_encountered"), cfg["forge_threshold"]
    if not isinstance(times, int) or isinstance(times, bool):
        reasons.append(("times", f"times_encountered {times!r} is not an integer"))
    elif times < threshold:
        reasons.append(("times", f"times_encountered {times} < forge_threshold {threshold}"))

    raw_value = gap.get("estimated_value")
    match = _VALUE_RE.match(raw_value) if isinstance(raw_value, str) else None
    value = match.group(1).lower() if match else None
    if value is None:
        reasons.append(("value-unrankable", "estimated_value does not start with "
                        "low|medium|high, so it cannot be ranked (g-318-157)"))
    elif VALUE_LEVELS.index(value) < VALUE_LEVELS.index(VALUE_FLOOR):
        reasons.append(("value", f"estimated_value {value} < {VALUE_FLOOR}"))

    gtype = gap.get("type") or TYPELESS_DEFAULT
    gate = cfg["gates"].get(gtype) if isinstance(gtype, str) else None
    if gate is None:
        reasons.append(("type", f"type {gtype!r} has no forge_gate in gap_types"))

    waived = gap.get("requested_by") == "user"
    cap_out, contract_out = {"result": "waived"}, {"result": "waived"}
    if not waived:
        cap_out = {k: v for k, v in capability.items() if k != "scale"}
        if capability.get("error"):
            reasons.append(("capability-unreadable", capability["error"]))
        elif gate is not None:
            cap_out["threshold"] = capability["scale"][gate]
            cap_out["met"] = capability["value"] >= cap_out["threshold"]
            if not cap_out["met"]:
                reasons.append(("capability", (
                    f"capability {capability['level']} ({capability['value']}) < "
                    f"forge_gate {gate} ({cap_out['threshold']}) on the "
                    f"{capability['scale_name']} scale")))
        contract_out = contract
        if contract["result"] == "blocked":
            where = contract.get("stage_name") or contract.get("current_stage")
            unlocks = f", unlocks at {contract['unlocks_at']}" if contract.get("unlocks_at") else ""
            reasons.append(("contract-blocked", f"curriculum blocks allow_forge_skill at {where}{unlocks}"))
        elif contract["result"] == "unreadable":
            reasons.append(("contract-unreadable", (
                f"curriculum-contract-check.sh rc={contract['rc']}: {contract.get('detail')}")))

    return {
        "gap_id": gap.get("id"),
        "verdict": "BLOCK" if reasons else ("WAIVED" if waived else "PASS"),
        "reasons": [text for _, text in reasons],
        "reason_codes": [code for code, _ in reasons],
        "type": gtype,
        "type_source": "stored" if gap.get("type") else "default",
        "forge_gate": gate,
        "capability": cap_out,
        "times_encountered": times,
        "forge_threshold": threshold,
        "estimated_value": value,
        "estimated_value_raw": raw_value,
        "contract": contract_out,
        "status": status,
        "requested_by": gap.get("requested_by"),
    }


def _num(x) -> str:
    return f"{x:g}" if isinstance(x, (int, float)) and not isinstance(x, bool) else _tok(x)


def _tok(x) -> str:
    """A token value with no whitespace, so the line always splits on spaces
    into key=value pairs -- even for a malformed record's list-valued field."""
    s = json.dumps(x, separators=(",", ":"), default=str) if isinstance(x, (list, dict)) else str(x)
    return "-".join(s.split())


def _contract_token(contract: dict) -> str:
    stage = _tok(contract.get("current_stage") or contract.get("reason", ""))
    return f"contract={contract['result']}" + (f":{stage}" if stage else "")


def format_line(v: dict, shared: bool = False) -> str:
    """One verdict line. shared=True (--all) leaves out the capability and
    contract inputs, which are the same for every gap and print once."""
    parts = [v["verdict"], _tok(v["gap_id"]), f"type={_tok(v['type'])}",
             f"type_source={v['type_source']}", f"forge_gate={v['forge_gate']}"]
    cap = v["capability"]
    if cap.get("result") == "waived":
        parts.append("capability=waived")
    elif not shared:
        parts += [f"threshold={_num(cap.get('threshold'))}", f"scale={cap.get('scale_name')}",
                  f"capability_level={_tok(cap.get('level'))}", f"level_source={cap.get('level_source')}",
                  f"confidence={_num(cap.get('confidence'))}", f"capability_source={_tok(cap.get('source'))}"]
    parts += [f"times_encountered={_num(v['times_encountered'])}",
              f"forge_threshold={_num(v['forge_threshold'])}",
              f"estimated_value={v['estimated_value'] or 'unrankable'}"]
    if v["contract"].get("result") == "waived" or not shared:
        parts.append(_contract_token(v["contract"]))
    parts += [f"status={_tok(v['status'])}", f"requested_by={_tok(v['requested_by'] or 'none')}"]
    if v["reasons"]:
        parts.append("reasons=" + "; ".join(v["reasons"]))
    return " ".join(parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="forge-gate-check.sh",
        description="The /forge-skill readiness gate: PASS | BLOCK | WAIVED, with every input.")
    ap.add_argument("gap_id", nargs="?", help="the gap to check, e.g. gap-045")
    ap.add_argument("--all", action="store_true",
                    help="check every gap in the store (aspirations-evolve Step 9)")
    ap.add_argument("--category", metavar="TREE-NODE-KEY",
                    help="gate on this knowledge-tree node's capability; "
                         "without it the agent's developmental stage is used")
    ap.add_argument("--json", action="store_true", help="print the verdict(s) as JSON")
    args = ap.parse_args(argv)
    if bool(args.gap_id) == bool(args.all):
        ap.error("give exactly one of <gap-id> or --all")
    if args.all and args.category:
        ap.error("--category names one gap's category; it cannot apply to --all")
    try:
        return _gate(args)
    except GateError as e:
        print(f"forge-gate-check: cannot evaluate: {e}", file=sys.stderr)
        return 2
    except Exception:
        # Exit 1 is the BLOCK verdict. A crash must never read as one.
        traceback.print_exc()
        print("forge-gate-check: cannot evaluate: internal error (traceback above)", file=sys.stderr)
        return 2


def _gate(args) -> int:
    cfg = load_config()
    gaps = load_gaps()
    targets = ([g for g in gaps if isinstance(g, dict)] if args.all
               else [find_gap(gaps, args.gap_id)])
    capability = contract = None
    if any(g.get("requested_by") != "user" for g in targets):
        capability = (category_capability(args.category, cfg) if args.category
                      else agent_capability(cfg))
        contract = contract_check()
    verdicts = [evaluate(g, cfg, capability, contract) for g in targets]

    ready = [v for v in verdicts if v["verdict"] in ("PASS", "WAIVED")]
    if not args.all:
        v = verdicts[0]
        print(json.dumps(v, indent=2, default=str) if args.json else format_line(v))
        return 0 if ready else 1

    if args.json:
        print(json.dumps({"evaluated": len(verdicts),
                          "forge_ready": [v["gap_id"] for v in ready],
                          "verdicts": verdicts}, indent=2, default=str))
        return 0 if ready else 1
    blocked = [v for v in verdicts if v["verdict"] == "BLOCK"]
    not_mappings = len(gaps) - len(targets)
    print(f"forge-gate-check --all: {len(verdicts)} gap(s) evaluated, "
          f"{len(ready)} forge-ready, {len(blocked)} BLOCK"
          + (f" ({not_mappings} non-mapping record(s) in the store skipped)" if not_mappings else ""))
    for v in ready:
        print(format_line(v, shared=True))
    tally = {}
    for v in blocked:
        for code in set(v["reason_codes"]):
            tally[code] = tally.get(code, 0) + 1
    if tally:
        print("BLOCK reasons (a gap can fail several): " + "; ".join(
            f"{REASON_LABELS[c]} {n}" for c, n in sorted(tally.items(), key=lambda x: -x[1])))
    if capability is not None:
        if capability.get("error"):
            cap_text = f"capability_error={capability['error']}"
        else:
            cap_text = f"capability_level={capability['level']} confidence={_num(capability['confidence'])} " \
                       f"scale={capability['scale_name']} forge_gate thresholds: " + ", ".join(
                           f"{g}={_num(capability['scale'][g])} "
                           f"{'met' if capability['value'] >= capability['scale'][g] else 'NOT met'}"
                           for g in sorted(set(cfg["gates"].values()), key=capability["scale"].get))
        print(f"shared inputs (every line above): capability_source={capability['source']} "
              f"{cap_text}; {_contract_token(contract)}")
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main())
