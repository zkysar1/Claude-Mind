#!/usr/bin/env python3
"""skip-fastpath-eval.py — Tier 2 utility extraction.

Replaces aspirations-execute/SKILL.md Phase 4.0 SKIP fast-path pseudocode.
Takes a SKIP result (INFRASTRUCTURE_UNAVAILABLE | RESOURCE_BLOCKED), maps
skill → component via infra-health.yaml, runs ONE recovery probe, and
recommends the next action: RETRY | PROVISION | CREATE_BLOCKER.

Plan: ~/.claude/plans/i-had-one-agent-luminous-reddy.md (Tier 2 #4).

Input (flags):
  --goal-skill STR            The skill that returned SKIP (e.g., efs-ssh)
  --skip-result STR           INFRASTRUCTURE_UNAVAILABLE | RESOURCE_BLOCKED
  --retry-attempted           Set if retry was already attempted this iteration
  --failure-reason STR        Short reason from original failure (for CREATE_BLOCKER)

Output JSON:
  {
    "next_action": "RETRY" | "PROVISION" | "CREATE_BLOCKER" | "NO_COMPONENT",
    "component": "efs-ssh",
    "probe_status": "ok" | "provisionable" | "failing" | "no_probe",
    "provision_skill": "...",   # if PROVISION
    "signal_count": 2,          # multi-signal check
    "flags": [...],
    "summary": "..."
  }

Exit codes: 0=decision emitted (read next_action to branch), 2=input error.
NO_COMPONENT is a valid decision (→ CREATE_BLOCKER), not an error — exit 0.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import AGENT_DIR, CORE_ROOT  # type: ignore
from _fileops import log_script_decision  # type: ignore

try:
    import yaml  # type: ignore
except ImportError:
    print("ERROR: PyYAML not installed", file=sys.stderr)
    sys.exit(2)


VALID_SKIP_RESULTS = ("INFRASTRUCTURE_UNAVAILABLE", "RESOURCE_BLOCKED")


def _map_skill_to_component(goal_skill):
    """Read <agent>/infra-health.yaml skill_mapping; return component or None."""
    if AGENT_DIR is None:
        return None
    ih = Path(AGENT_DIR) / "infra-health.yaml"
    if not ih.exists():
        return None
    try:
        with open(ih, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001
        return None
    mapping = data.get("skill_mapping") or {}
    # Support both "/access-efs-data" and "access-efs-data" forms.
    # Mapping values may be a string (single component) or list (multiple).
    # When a list: caller is expected to probe all. We return the first for
    # the recovery probe — if a later probe returns different status, it will
    # surface in multi-signal evaluation.
    key = goal_skill.lstrip("/")
    raw = mapping.get(key) or mapping.get(goal_skill)
    if raw is None:
        return None
    if isinstance(raw, list):
        return raw[0] if raw else None
    return raw


def _run_infra_health(component, timeout=15):
    """Invoke infra-health.py check <component> via sys.executable.

    Bypasses bash wrapper to avoid the Windows shell-resolution landmine.
    Returns (status, full_response_dict_or_none, stderr).
    """
    py_path = Path(CORE_ROOT) / "scripts" / "infra-health.py"
    if not py_path.exists():
        return "no_probe", None, "infra-health.py not found"
    try:
        p = subprocess.run(
            [sys.executable, str(py_path), "check", component],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "no_probe", None, f"infra-health timeout after {timeout}s"

    if not p.stdout.strip():
        return "no_probe", None, p.stderr.strip()
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        return "no_probe", None, f"non-JSON output: {p.stdout[:200]}"

    status = data.get("status", "no_probe")
    return status, data, p.stderr.strip()


def main():
    ap = argparse.ArgumentParser(description="SKIP fast-path decision helper (Phase 4.0)")
    ap.add_argument("--goal-skill", required=True)
    ap.add_argument("--skip-result", required=True, choices=VALID_SKIP_RESULTS)
    ap.add_argument("--retry-attempted", action="store_true")
    ap.add_argument("--failure-reason", default="")
    args = ap.parse_args()

    component = _map_skill_to_component(args.goal_skill)
    if not component:
        print(json.dumps({
            "next_action": "NO_COMPONENT",
            "goal_skill": args.goal_skill,
            "component": None,
            "flags": ["no_component_mapping"],
            "summary": (f"no component mapping for skill {args.goal_skill} in "
                        "<agent>/infra-health.yaml — cannot run recovery probe; "
                        "fall through to CREATE_BLOCKER"),
        }, ensure_ascii=False, default=str))
        sys.exit(0)

    # If retry already attempted, skip the probe — we're past the recovery phase
    if args.retry_attempted:
        print(json.dumps({
            "next_action": "CREATE_BLOCKER",
            "goal_skill": args.goal_skill,
            "component": component,
            "probe_status": "skipped_retry_attempted",
            "signal_count": 1,  # original-failure signal only
            "flags": ["retry_exhausted"],
            "summary": "retry already attempted — CREATE_BLOCKER with original failure signal",
            "multi_signal_advice": ("obtain a second independent probe (different tool/endpoint) "
                                    "before filing blocker — see verify-before-assuming rule 1"),
        }, ensure_ascii=False, default=str))
        sys.exit(0)

    status, probe_data, stderr = _run_infra_health(component)

    # Count signals: original-failure (always 1) + probe (1 if not no_probe)
    signal_count = 1 + (0 if status == "no_probe" else 1)

    if status == "ok":
        print(json.dumps({
            "next_action": "RETRY",
            "goal_skill": args.goal_skill,
            "component": component,
            "probe_status": status,
            "probe_data": probe_data,
            "signal_count": signal_count,
            "flags": ["recovery_probe_ok"],
            "summary": f"infra-health check passed for {component} — retry once",
        }, ensure_ascii=False, default=str))
        sys.exit(0)

    if status == "provisionable":
        provision_skill = (probe_data or {}).get("provision_skill")
        if not provision_skill:
            # Treat as CREATE_BLOCKER — we don't know how to provision
            print(json.dumps({
                "next_action": "CREATE_BLOCKER",
                "goal_skill": args.goal_skill,
                "component": component,
                "probe_status": status,
                "probe_data": probe_data,
                "signal_count": signal_count,
                "flags": ["provisionable_no_skill"],
                "summary": f"{component} is provisionable but no provision_skill declared",
            }, ensure_ascii=False, default=str))
            sys.exit(0)

        print(json.dumps({
            "next_action": "PROVISION",
            "goal_skill": args.goal_skill,
            "component": component,
            "probe_status": status,
            "probe_data": probe_data,
            "provision_skill": provision_skill,
            "same_skill_recovery": provision_skill == args.goal_skill,
            "signal_count": signal_count,
            "flags": ["provisionable"],
            "summary": (f"{component} is provisionable via {provision_skill}; "
                        f"same_skill={provision_skill == args.goal_skill}"),
        }, ensure_ascii=False, default=str))
        sys.exit(0)

    # status is "failing", "no_probe", or unknown — recommend CREATE_BLOCKER
    log_script_decision("skip-fastpath-eval", {
        "goal_skill": args.goal_skill,
        "component": component,
        "probe_status": status,
        "signal_count": signal_count,
        "next_action": "CREATE_BLOCKER",
    })
    flags = ["recovery_failed"]
    if signal_count < 2:
        flags.append("single_signal_only")

    print(json.dumps({
        "next_action": "CREATE_BLOCKER",
        "goal_skill": args.goal_skill,
        "component": component,
        "probe_status": status,
        "probe_data": probe_data,
        "probe_stderr": stderr,
        "signal_count": signal_count,
        "flags": flags,
        "summary": (f"{component} status={status} after recovery probe; "
                    f"signal_count={signal_count}; recommend CREATE_BLOCKER"),
        "multi_signal_advice": (
            "need ≥2 signals before filing blocker; add an alternative probe "
            "(different tool/endpoint) if you only have 1"
            if signal_count < 2 else None
        ),
    }, ensure_ascii=False, default=str))
    sys.exit(0)


if __name__ == "__main__":
    main()
