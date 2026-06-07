#!/usr/bin/env python3
"""Domain-class portfolio steering gate.

Origin: rb-616 + g-244-20 + g-001-196. Replaces LLM-discretionary "advisory"
pseudocode in create-aspiration/SKILL.md Phase A (lines 332-344) with a
bash-enforced gate. Pattern: rb-428 family (LLM-step-lost when bash safety
net is absent).

Reads `meta/aspiration-generation-strategy.yaml` `domain_class_targets`
plus `core/scripts/learning-ratio.sh --scope goals --json` output. For a
given candidate-class, returns structured JSON indicating whether the
candidate triggers `warn_above` (over max) or `bias_below` (under min)
enforcement.

Subcommands:
  check --candidate-class <class>
    Returns JSON describing whether the candidate triggers an action.
  log --payload <json>
    Appends payload to meta/config-changes.yaml under domain_class_override
    events. Caller writes the payload returned by `check`.

JSON output shape (check):
  {
    "fired": bool,                          # action != "none"
    "candidate_class": str,                 # echo of input
    "current_ratio": float,                 # 0.0-1.0
    "threshold": float | null,              # max for warn, min for bias
    "action": "warn" | "bias" | "none",
    "warn_above_class": str | null,         # set when action=warn
    "bias_below_classes": list[str],        # all classes currently under min
    "log_payload": dict | null              # set when action=warn
  }

Fail-open: missing strategy file, broken learning-ratio.sh, or YAML parse
errors all return action="none", fired=false, log_payload=null. The gate's
job is to catch portfolio drift, not to block aspiration generation when
its own dependencies break.
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
try:
    from _paths import META_DIR, PROJECT_ROOT  # type: ignore
except Exception as e:
    print(f"[domain-class-gate] WARN: failed to import _paths: {e}", file=sys.stderr)
    sys.exit(0)

try:
    import yaml  # type: ignore
except Exception as e:
    print(f"[domain-class-gate] WARN: PyYAML unavailable: {e}", file=sys.stderr)
    sys.exit(0)


STRATEGY_PATH = Path(META_DIR) / "aspiration-generation-strategy.yaml"
CONFIG_CHANGES_PATH = Path(META_DIR) / "config-changes.yaml"
LEARNING_RATIO_SH = Path(PROJECT_ROOT) / "core" / "scripts" / "learning-ratio.sh"


def _fail_open(candidate_class: str, reason: str) -> dict:
    return {
        "fired": False,
        "candidate_class": candidate_class,
        "current_ratio": 0.0,
        "threshold": None,
        "action": "none",
        "warn_above_class": None,
        "bias_below_classes": [],
        "log_payload": None,
        "fail_open_reason": reason,
    }


def _load_targets():
    try:
        with STRATEGY_PATH.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError:
        return None, "strategy file missing"
    except (yaml.YAMLError, OSError) as e:
        return None, f"strategy load failed: {e}"
    if not isinstance(data, dict):
        return None, "strategy file not a mapping"
    targets = data.get("domain_class_targets")
    if not isinstance(targets, dict):
        return None, "domain_class_targets missing or not a mapping"
    return targets, None


def _load_ratios():
    # rb-198: Windows + bash invocation requires MIND_SHELL env var (set by hook)
    # plus posix-style path. Bare "bash" resolves to a different binary than
    # the shim path; backslash paths confuse bash's argument parser.
    from _runtime_bash import BASH as bash  # rb-1472: bin-first, honors MIND_SHELL, clean-PATH-safe
    try:
        result = subprocess.run(
            [bash, LEARNING_RATIO_SH.as_posix(), "--scope", "goals", "--json"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"learning-ratio invocation failed: {e}"
    if result.returncode != 0:
        return None, f"learning-ratio exit {result.returncode}"
    # learning-ratio.sh prints a summary line + JSON. Find the JSON object.
    stdout = result.stdout or ""
    brace_idx = stdout.find("{")
    if brace_idx == -1:
        return None, "no JSON in learning-ratio stdout"
    try:
        payload = json.loads(stdout[brace_idx:])
    except json.JSONDecodeError as e:
        return None, f"learning-ratio JSON parse: {e}"
    pct = payload.get("pct")
    if not isinstance(pct, dict):
        return None, "learning-ratio missing 'pct' field"
    return pct, None


def cmd_check(args) -> int:
    candidate_class = args.candidate_class
    targets, err = _load_targets()
    if targets is None:
        print(json.dumps(_fail_open(candidate_class, err)))
        return 0
    pcts, err = _load_ratios()
    if pcts is None:
        print(json.dumps(_fail_open(candidate_class, err)))
        return 0

    # learning-ratio.sh emits pct as integer percentages (17, 43, etc.).
    # Convert to 0.0-1.0 ratios for comparison with thresholds.
    current_ratio_int = pcts.get(candidate_class)
    if current_ratio_int is None:
        # Class not in pct map (rare — only the 4 declared classes appear).
        # Treat as zero ratio.
        current_ratio = 0.0
    else:
        current_ratio = float(current_ratio_int) / 100.0

    target = targets.get(candidate_class) or {}
    enforcement = target.get("enforcement", "none")
    max_threshold = target.get("max")
    min_threshold = target.get("min")

    # Compute action for THIS candidate's class.
    action = "none"
    warn_above_class = None
    threshold = None
    log_payload = None

    if enforcement == "warn_above" and isinstance(max_threshold, (int, float)):
        if current_ratio >= float(max_threshold):
            action = "warn"
            warn_above_class = candidate_class
            threshold = float(max_threshold)
            log_payload = {
                "date": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "event": "domain_class_override",
                "class": candidate_class,
                "current_ratio": round(current_ratio, 4),
                "max": float(max_threshold),
                "reason": None,  # caller fills with justification text
            }
    elif enforcement == "bias_below" and isinstance(min_threshold, (int, float)):
        if current_ratio < float(min_threshold):
            action = "bias"
            threshold = float(min_threshold)

    # Aggregate bias_below classes across the whole portfolio (informational —
    # caller may use this to add prompt-level steering for OTHER classes too).
    bias_below_classes = []
    for cls, t in targets.items():
        if not isinstance(t, dict):
            continue
        if t.get("enforcement") != "bias_below":
            continue
        cls_min = t.get("min")
        if not isinstance(cls_min, (int, float)):
            continue
        cls_pct = pcts.get(cls)
        cls_ratio = float(cls_pct) / 100.0 if cls_pct is not None else 0.0
        if cls_ratio < float(cls_min):
            bias_below_classes.append(cls)

    print(json.dumps({
        "fired": action != "none",
        "candidate_class": candidate_class,
        "current_ratio": round(current_ratio, 4),
        "threshold": threshold,
        "action": action,
        "warn_above_class": warn_above_class,
        "bias_below_classes": bias_below_classes,
        "log_payload": log_payload,
    }))
    return 0


def cmd_log(args) -> int:
    """Append a payload to meta/config-changes.yaml under domain_class_override.

    Caller invokes this after `check` returned action=warn AND the agent has
    composed the justification text. Caller is expected to fill log_payload.reason
    before passing here.
    """
    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as e:
        print(f"[domain-class-gate] payload JSON parse: {e}", file=sys.stderr)
        return 1
    if not isinstance(payload, dict):
        print("[domain-class-gate] payload must be a JSON object", file=sys.stderr)
        return 1
    if payload.get("event") != "domain_class_override":
        print("[domain-class-gate] payload.event must be 'domain_class_override'", file=sys.stderr)
        return 1

    # Read existing config-changes.yaml (create if missing).
    existing = {}
    if CONFIG_CHANGES_PATH.exists():
        try:
            with CONFIG_CHANGES_PATH.open("r", encoding="utf-8") as fh:
                existing = yaml.safe_load(fh) or {}
        except (yaml.YAMLError, OSError) as e:
            print(f"[domain-class-gate] WARN: config-changes load: {e}", file=sys.stderr)
            existing = {}
    if not isinstance(existing, dict):
        existing = {}

    events = existing.setdefault("domain_class_override", [])
    if not isinstance(events, list):
        events = []
        existing["domain_class_override"] = events
    events.append(payload)

    try:
        with CONFIG_CHANGES_PATH.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(existing, fh, default_flow_style=False, sort_keys=False)
    except OSError as e:
        print(f"[domain-class-gate] write failed: {e}", file=sys.stderr)
        return 1

    print(json.dumps({"appended": True, "path": str(CONFIG_CHANGES_PATH)}))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp_check = sub.add_parser("check", help="Check whether a candidate-class triggers warn/bias")
    sp_check.add_argument("--candidate-class", required=True,
                          help="Domain class of the candidate aspiration (e.g., framework-meta)")
    sp_check.set_defaults(func=cmd_check)

    sp_log = sub.add_parser("log", help="Append a domain_class_override event to config-changes.yaml")
    sp_log.add_argument("--payload", required=True,
                        help="JSON string with the log payload from check + reason filled in")
    sp_log.set_defaults(func=cmd_log)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
