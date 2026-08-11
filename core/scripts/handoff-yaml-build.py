#!/usr/bin/env python3
"""handoff-yaml-build.py — Tier 2 utility extraction.

Replaces aspirations-consolidate/SKILL.md Step 9 handoff YAML assembly.
The LLM provides the judgment fields (prose next_focus, reasons, and
session_summary.key_outcomes); the script assembles the final handoff.yaml
with schema validation + atomic write via _fileops.

FIELD-PATH NOTE (g-115-3385): `key_outcomes` is NESTED under `session_summary`,
NOT a top-level field. The canonical schema in
core/config/conventions/handoff-working-memory.md nests it, and boot/SKILL.md
(the only consumer) reads `session_summary.key_outcomes`. `session_summary`
passes through _assemble() whole, so the nested form round-trips. An earlier
reading of this docstring — which listed "key_outcomes" beside the genuinely
top-level "next_focus" — led a consolidation to emit it at TOP level, where
_assemble()'s fixed allowlist silently discarded it. Emit it nested. Any
top-level payload key _assemble() does not carry is now reported in the
`dropped_keys` output field and a stderr WARN rather than vanishing (rb-538:
allowlist parsers that silently drop unknown keys hide contract breaks).

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
    ap.add_argument("--schema", action="store_true",
                    help="Print the accepted JSON payload schema and exit (no agent needed)")
    args = ap.parse_args()

    # --schema: emit the accepted-field schema, derived from the live
    # validator constants (REQUIRED_TOP / *_REQUIRED) + _assemble() so it
    # never drifts from the real schema. No agent binding required.
    if args.schema:
        _stub = {"session_number": 0, "next_focus": "", "first_action": {},
                 "session_summary": {}}
        optional_fields = [k for k in _assemble(_stub).keys()
                           if k not in REQUIRED_TOP]
        print(json.dumps({
            "writer": "handoff-yaml-build.py",
            "input": "JSON on stdin OR --payload FILE",
            "required_top": REQUIRED_TOP,
            "session_number_type": "int",
            "first_action_required": FIRST_ACTION_REQUIRED,
            "session_summary_required": SESSION_SUMMARY_REQUIRED,
            "optional_fields": optional_fields,
            "doc": "core/config/conventions/handoff-working-memory.md",
        }, indent=2))
        sys.exit(0)

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

    # Dropped-key detection (). _assemble() is a fixed allowlist, so a
    # top-level payload key it does not carry is discarded with no error — the
    # failure mode that hid the key_outcomes contract break for an entire
    # release (a payload carrying top-level key_outcomes validated cleanly,
    # wrote 17 fields, reported flags:[], and the field was simply gone).
    # Report rather than reject: unknown keys are not necessarily wrong (a
    # caller may pass provenance the schema does not persist), but they must
    # never be SILENT. rb-538 / guard-527.
    # str(k), not k: a non-string top-level payload key makes `sorted()` raise
    # on mixed types and `", ".join()` raise on ints, so the reporting path
    # would abort the very write it exists to annotate. Found by fresh-eyes on
    # a COPY of this line () and fixed here in the same sweep rather
    # than leaving the reference broken behind its corrected copies (guard-3088).
    dropped_keys = sorted(str(k) for k in payload if k not in handoff)
    if dropped_keys:
        print(
            "WARN: handoff-yaml-build dropped %d unrecognized top-level "
            "payload key(s): %s. _assemble() carries a fixed allowlist; these "
            "were NOT written to handoff.yaml. If one is meant to persist, add "
            "it to _assemble() or nest it under an allowlisted field (e.g. "
            "key_outcomes belongs at session_summary.key_outcomes). "
            "See core/config/conventions/handoff-working-memory.md."
            % (len(dropped_keys), ", ".join(dropped_keys)),
            file=sys.stderr,
        )

    if args.dry_run:
        print(json.dumps({
            "dry_run": True,
            "summary": f"handoff.yaml validated; {len(handoff)} fields assembled",
            "flags": ["dry_run"] + (["dropped_keys"] if dropped_keys else []),
            "dropped_keys": dropped_keys,
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
        "dropped_keys": dropped_keys,
        "summary": f"handoff.yaml written for session {handoff['session_number']}",
        "flags": ["dropped_keys"] if dropped_keys else [],
    }, ensure_ascii=False, default=str))
    sys.exit(0)


if __name__ == "__main__":
    main()
