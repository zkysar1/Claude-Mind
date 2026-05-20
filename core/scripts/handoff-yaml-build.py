#!/usr/bin/env python3
"""handoff-yaml-build.py — Tier 2 utility extraction.

Replaces aspirations-consolidate/SKILL.md Step 9 handoff YAML assembly.
The LLM provides the judgment fields (prose next_focus, key_outcomes, reasons);
the script assembles the final handoff.yaml with schema validation + atomic
write via _fileops.

Plan: ~/.claude/plans/i-had-one-agent-luminous-reddy.md (Tier 2 #1).

Input: JSON on stdin OR --payload FILE.
Output: JSON stdout with written_path + summary. Side effect: writes
  <agent>/session/handoff.yaml via locked_write_yaml.

Exit codes: 0=ok, 1=validation failed, 2=input error, 3=write failed.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# : force utf-8 on stdin/stdout/stderr (covers Windows cp1252 fallback
# when callers bypass the _platform.sh PYTHONIOENCODING=utf-8 shim).
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

from _paths import AGENT_DIR  # type: ignore
from _fileops import log_script_decision  # type: ignore

try:
    import yaml  # type: ignore
except ImportError:
    print("ERROR: PyYAML not installed", file=sys.stderr)
    sys.exit(2)

try:
    from _fileops import locked_write_yaml  # type: ignore
except ImportError as e:
    print(f"ERROR: _fileops not importable: {e}", file=sys.stderr)
    sys.exit(2)


REQUIRED_TOP = [
    "session_number",
    "next_focus",
    "first_action",
    "session_summary",
]

FIRST_ACTION_REQUIRED = ["goal_id", "reason"]
SESSION_SUMMARY_REQUIRED = ["goals_completed", "goals_failed"]


def _now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _validate(payload):
    errs = []
    for k in REQUIRED_TOP:
        if k not in payload:
            errs.append(f"missing top-level field: {k}")
    fa = payload.get("first_action") or {}
    for k in FIRST_ACTION_REQUIRED:
        if k not in fa:
            errs.append(f"missing first_action.{k}")
    ss = payload.get("session_summary") or {}
    for k in SESSION_SUMMARY_REQUIRED:
        if k not in ss:
            errs.append(f"missing session_summary.{k}")
    if not isinstance(payload.get("session_number"), int):
        errs.append("session_number must be integer")
    return errs


def _assemble(payload):
    """Build the canonical handoff structure with all optional fields
    defaulted. Absent LLM-provided fields become null/empty rather than
    being left out — keeps boot's handoff parser stable across sessions."""
    return {
        "session_number": payload["session_number"],
        "timestamp": payload.get("timestamp") or _now_iso(),
        "last_goal_completed": payload.get("last_goal_completed"),
        "goals_in_progress": payload.get("goals_in_progress") or [],
        "hypotheses_pending": payload.get("hypotheses_pending", 0),
        "next_focus": payload["next_focus"],
        "first_action": payload["first_action"],
        "decisions_locked": payload.get("decisions_locked") or [],
        "session_summary": payload["session_summary"],
        "known_blockers_active": payload.get("known_blockers_active") or [],
        "critical_path": payload.get("critical_path") or {},
        "knowledge_debts_pending": payload.get("knowledge_debts_pending") or [],
        "user_goals_pending": payload.get("user_goals_pending") or {"count": 0, "goals": []},
        "meta_state": payload.get("meta_state") or {},
        "consolidation_meta": payload.get("consolidation_meta") or {},
        "phase_cost_report": payload.get("phase_cost_report") or
            {"path": "none", "summary": "skipped (no markers)"},
        "curriculum_stage": payload.get("curriculum_stage"),
    }


def main():
    ap = argparse.ArgumentParser(description="Build handoff.yaml from structured JSON")
    ap.add_argument("--payload", type=str, default=None,
                    help="Path to JSON file (else read from stdin)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Validate + assemble but do not write")
    ap.add_argument("--output-path", type=str, default=None,
                    help="Override output path (default: <agent>/session/handoff.yaml)")
    args = ap.parse_args()

    if AGENT_DIR is None:
        print(json.dumps({"error": "no agent bound", "flags": ["no_agent"]}))
        sys.exit(2)

    if args.payload:
        try:
            with open(args.payload, "r", encoding="utf-8") as f:
                raw = f.read()
        except OSError as e:
            print(json.dumps({"error": f"cannot read payload file: {e}"}))
            sys.exit(2)
    else:
        raw = sys.stdin.read()

    if not raw.strip():
        print(json.dumps({"error": "empty payload", "flags": ["input_error"]}))
        sys.exit(2)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"invalid JSON: {e}", "flags": ["input_error"]}))
        sys.exit(2)

    errs = _validate(payload)
    if errs:
        print(json.dumps({
            "error": "validation failed",
            "validation_errors": errs,
            "flags": ["validation_failed"],
            "summary": f"{len(errs)} validation error(s)",
        }, ensure_ascii=False, default=str))
        sys.exit(1)

    handoff = _assemble(payload)

    if args.dry_run:
        print(json.dumps({
            "dry_run": True,
            "summary": f"handoff.yaml validated; {len(handoff)} fields assembled",
            "flags": ["dry_run"],
            "handoff_preview": handoff,
        }, ensure_ascii=False, default=str))
        sys.exit(0)

    out_path = Path(args.output_path) if args.output_path else (
        Path(AGENT_DIR) / "session" / "handoff.yaml")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        locked_write_yaml(out_path, handoff)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({
            "error": f"locked_write_yaml failed: {type(e).__name__}: {e}",
            "flags": ["write_failed"],
        }))
        sys.exit(3)

    log_script_decision("handoff-yaml-build", {
        "written_path": str(out_path),
        "session_number": handoff["session_number"],
        "fields_written": len(handoff),
    })
    print(json.dumps({
        "written_path": str(out_path),
        "session_number": handoff["session_number"],
        "timestamp": handoff["timestamp"],
        "fields_written": len(handoff),
        "summary": f"handoff.yaml written for session {handoff['session_number']}",
        "flags": [],
    }, ensure_ascii=False, default=str))
    sys.exit(0)


if __name__ == "__main__":
    main()
