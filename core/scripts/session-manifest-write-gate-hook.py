#!/usr/bin/env python3
"""PreToolUse[Write|Edit|MultiEdit] hook adapter for session-manifest-write-gate.

Bridges the Claude Code PreToolUse hook contract (stdin JSON in, structured
deny JSON on stdout) to session-manifest-write-gate.py's positional argv +
--output json contract.

Origin: g-254-05 (asp-254). Prerequisite for g-254-03 settings.json wiring.

Contract:
  Stdin (from Claude Code): {"tool_name": "...", "tool_input": {...}, "session_id": "..."}
  Stdout (to Claude Code):
    - empty + exit 0          → allow with no mutation
    - hookSpecificOutput JSON → deny with reason (also exit 0)

Decision flow:
  (1) tool_name not in (Write, Edit, MultiEdit) → silent allow
  (2) tool_input.file_path missing or non-absolute → silent allow (out of scope)
  (3) shell out to session-manifest-write-gate.py <path> --output json
  (4) parse JSON: allowed=true → silent allow; allowed=false → emit deny JSON

SAFETY: fail open on ANY error. The catch-all at the bottom swallows every
uncaught exception. A broken hook never accidentally emits a partial deny —
empty stdout + exit 0 means "approve, no mutation" per Claude Code's contract.

Pattern source: core/scripts/path-resolution-hook.py:113-148 (extract_file_path,
main flow, approve/deny helpers, catch-all fail-open).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

# Shared helpers (): approve_no_mutation, emit_deny,
# extract_file_path, is_absolute_path, stdin_json_or_approve.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from hook_helpers import (  # noqa: E402
    approve_no_mutation,
    emit_deny,
    extract_file_path,
    is_absolute_path,
    stdin_json_or_approve,
)


def main():
    data = stdin_json_or_approve()

    tool_name = data.get("tool_name", "") or ""
    if tool_name not in ("Write", "Edit", "MultiEdit"):
        approve_no_mutation()

    tool_input = data.get("tool_input") or {}
    file_path = extract_file_path(tool_input)
    if not file_path:
        approve_no_mutation()

    if not is_absolute_path(file_path):
        approve_no_mutation()

    project_root = os.environ.get("PROJECT_ROOT", "")
    if not project_root:
        approve_no_mutation()

    gate_script = os.path.join(
        project_root, "core", "scripts", "session-manifest-write-gate.py"
    )
    if not os.path.isfile(gate_script):
        approve_no_mutation()

    # Shell out to the gate. capture_output=True so stdout (JSON) is available
    # regardless of exit code — the gate emits JSON to stdout in --output json
    # mode AND exits non-zero on block. We act on `allowed`, not the exit code.
    try:
        result = subprocess.run(
            [sys.executable, gate_script, file_path, "--output", "json"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except Exception:
        approve_no_mutation()

    stdout = (result.stdout or "").strip()
    if not stdout:
        approve_no_mutation()

    try:
        gate_response = json.loads(stdout)
    except Exception:
        approve_no_mutation()

    if not isinstance(gate_response, dict):
        approve_no_mutation()

    allowed = gate_response.get("allowed")
    if allowed is True or allowed is None:
        approve_no_mutation()

    # allowed == False — translate to structured deny.
    reason = (
        gate_response.get("reason")
        or f"session-manifest-write-gate refused write to {file_path}"
    )
    severity = gate_response.get("severity", "block")
    mode = gate_response.get("mode", "unknown")
    deny_text = (
        f"session-manifest-write-gate ({severity}, mode={mode}): {reason}\n"
        f"Register the file in core/config/session-manifest.yaml or write "
        f"to a different location."
    )
    emit_deny(deny_text)


try:
    main()
except Exception:
    pass

sys.exit(0)
