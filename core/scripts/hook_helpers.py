#!/usr/bin/env python3
"""Shared helpers for PreToolUse hook adapters.

Extracted in g-254-07 from the three existing hooks that share the same
shape: stdin JSON -> tool_name dispatch -> file_path extract -> absolute-path
filter -> fail-open at every step + bottom catch-all.

Adapters:
  - bash-agent-inject.py
  - path-resolution-hook.py
  - session-manifest-write-gate-hook.py

Hook contract (Claude Code PreToolUse): exit 0 with empty stdout = approve
with no mutation. Structured JSON on stdout = deny with reason. Any uncaught
exception MUST be swallowed so a broken hook never blocks legitimate work.
Each adapter retains its own bottom catch-all `try/except: pass` block; this
module only standardizes the four-helper surface area.
"""

import json
import sys

#  / : force utf-8 on stdin/stdout/stderr (covers Windows
# cp1252 fallback when callers bypass the _platform.sh PYTHONIOENCODING=utf-8
# shim). Closes acceptance (4) of  — stdin-ingest sweep. Hooks that
# import hook_helpers inherit the reconfigure (read_payload reads stdin).
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()


def approve_no_mutation():
    """Exit 0 with no stdout — Claude Code's "approve with no mutation"."""
    sys.exit(0)


def emit_deny(reason):
    """Emit structured deny response per Claude Code PreToolUse hook contract.
    Exit code 0 + JSON on stdout. Caller controls the reason text."""
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(payload))
    sys.exit(0)


def emit_advisory(message):
    """Emit a NON-BLOCKING advisory per Claude Code PreToolUse contract.

    permissionDecision stays "allow" — the command runs; the message rides
    EVERY non-blocking channel because delivery was measured field-by-field
    (g-115-3511, trailing-echo-exit-gate): allow+reason alone does NOT reach
    the model; the full shape below is the one that delivers. stderr is also
    written for the human terminal. Exits 0 like every helper here.
    """
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": message,
            "additionalContext": message,
        },
        "systemMessage": message,
    }
    print(json.dumps(payload))
    print(message, file=sys.stderr)
    sys.exit(0)


def stdin_json_or_approve():
    """Read stdin JSON. On any parse error, approve_no_mutation (exits 0).
    Returns the parsed dict on success."""
    try:
        return json.load(sys.stdin)
    except Exception:
        approve_no_mutation()


def extract_file_path(tool_input):
    """Pull `file_path` from Write/Edit/MultiEdit tool_input. None when
    absent or wrong type. Caller decides what to do with None."""
    if not isinstance(tool_input, dict):
        return None
    fp = tool_input.get("file_path")
    if isinstance(fp, str) and fp:
        return fp
    return None


def is_absolute_path(p):
    """True if path is absolute across MSYS2/Windows forms. Relative paths
    are out of scope for path gates — caller typically approve_no_mutation
    when this returns False.

    Recognized absolute forms:
      c:/foo or C:/foo (Windows drive letter)
      /foo or /c/foo  (POSIX or MSYS2)
      \\\\... (UNC)
    """
    if not p:
        return False
    return (
        (len(p) >= 2 and p[1] == ":")          # c:/... or C:/...
        or p.startswith("/")                    # /foo or /c/foo
        or p.startswith("\\\\")                 # UNC
    )
