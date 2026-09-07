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
import os
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


def _read_stdin_bounded(timeout_s=None):
    """Read all of stdin, but never block forever on a pipe that has no EOF.

    A PreToolUse hook is handed its JSON on stdin by the harness, so the naive
    `json.load(sys.stdin)` looks safe -- but `read()` returns at EOF, not when
    the JSON has arrived. If the writer keeps its end of the pipe open (an
    inherited descriptor, a wrapper that never closes), the hook blocks with a
    COMPLETE payload already sitting in the buffer.

    Measured 2026-09-06 (alpha, DESKTOP-O91DLK2): every gate body hung in
    exactly that state, and `timeout 12` did NOT reap it -- the process lived
    until the writer exited (30.2s elapsed against a 12s timeout), because a
    Windows pipe read is not interruptible. With 11 of these firing on EVERY
    Bash call, that is the "bash calls spin forever and the agent gets stuck"
    failure this function exists to stop.

    Remedy is the one guard-664 prescribes and four other CLIs already run
    (experience.py::_read_optional_stdin, loop-state-save.py, presence-tick.py,
    iteration-close-reminder.py): read in a daemon thread with a join()
    deadline. select()/signal.alarm do NOT work on Windows pipes; a thread
    does. Tunable via HOOK_STDIN_TIMEOUT_S.

    ORDERING INVARIANT (guard-1737 step 3 -- state it, then assert it):

        HOOK_STDIN_TIMEOUT_S  <  min(configured hook timeout in settings.json
                                     over every hook that imports this module)

    The bound must be STRICTLY under the smallest caller grant so the hook
    fails OPEN on its own terms -- emitting the WARN below and approving --
    rather than being killed by the harness mid-read, which produces the same
    non-mutation with no diagnostic at all. Measured 2026-09-06: 41 hook
    entries import this module, granted 10s / 15s / 30s, so the binding
    minimum is 10s (schedule-wakeup-gate.sh). Asserted by
    tests/test_hook_stdin_timeout_ordering.py -- re-run it after changing any
    hook timeout in settings.json, not just after changing this constant.

    Default 8s. It was 5s from this function's introduction until 2026-09-06
    and that was too tight ON THIS BOX: total wall time for one
    bash-agent-inject.sh run was measured at 1564-5073ms over 8 runs while
    otherwise idle, so the 5s bound sat INSIDE the observed spread. When it
    trips, the hook approves without injecting MIND_AGENT and every
    downstream script that needs it fails -- observed live, a goal-selector.sh
    run died with "MIND_AGENT not set" while the same command had succeeded
    minutes earlier. The prior docstring justified 5s as "safely under the 15s
    harness hook timeout"; that number was wrong in both directions (the
    minimum grant is 10s, and this module's own hook is granted 30s).

    The sibling constant guard-664 cites for this exact pattern is
    EXPERIENCE_STDIN_TIMEOUT_S at 10s; 8s is that value adapted to the 10s
    grant this hook family runs under, not an independent guess.
    """
    if sys.stdin.isatty():
        return ""
    if timeout_s is None:
        try:
            timeout_s = float(os.environ.get("HOOK_STDIN_TIMEOUT_S", "8"))
        except Exception:
            timeout_s = 8.0
    import threading
    box = {"data": "", "done": False}

    def _reader():
        try:
            box["data"] = sys.stdin.read()
        except Exception:
            pass
        finally:
            box["done"] = True

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    t.join(timeout_s)
    if not box["done"]:
        sys.stderr.write(
            "WARN: hook stdin did not reach EOF within %.0fs -- approving with "
            "no mutation. The payload may have arrived in full but the writer "
            "never closed the pipe (guard-664 class).\n" % timeout_s
        )
        # OBSERVABILITY (2026-09-06). Without this the trip is SILENT: hook
        # stderr does not reach the transcript, and the approve is
        # byte-identical to a healthy no-op. That silence has already cost a
        # live diagnosis -- a goal-selector.sh run failed with "MIND_AGENT not
        # set" and the only breadcrumb, bash-agent-inject's own WARN, points
        # the reader at core/logs/bash-inject-misses.jsonl, which this path
        # never writes (it logs binding-yaml-missing, a different cause). So
        # the reader is sent to a log that is CORRECTLY empty and concludes the
        # hook ran fine. One line here separates the two causes.
        #
        # Best-effort by contract: any failure to log is swallowed. A hook that
        # cannot write a diagnostic must still approve -- never let telemetry
        # turn a fail-open path into a failure.
        try:
            import time
            _root = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
            _log = os.path.join(_root, "core", "logs", "hook-stdin-timeouts.jsonl")
            if os.path.isdir(os.path.dirname(_log)):
                with open(_log, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "timeout_s": timeout_s,
                        "hook": os.environ.get("CLAUDE_HOOK_NAME")
                                or os.path.basename(sys.argv[0] or "?"),
                        "sid": os.environ.get("MIND_SID"),
                        "agent": os.environ.get("MIND_AGENT"),
                        "reason": "stdin_no_eof_within_bound",
                    }) + "\n")
        except Exception:
            pass
        # Returning "" is enough: json.loads("") raises in the caller, which
        # falls through to approve_no_mutation() -- exit 0, empty stdout, the
        # fail-open contract every gate here already declares.
        #
        # Deliberately NOT os._exit(). An abandoned daemon thread parked in
        # sys.stdin.read() does NOT stall interpreter shutdown: measured
        # 2026-09-06 (alpha, DESKTOP-O91DLK2), child lifetime isolated from the
        # producer, a 3s join exits at 3.28s under sys.exit(0) versus 3.17s
        # under os._exit(0). The hard exit buys ~0.1s and costs atexit
        # handlers and stream flushing, so it is not worth it.
        return ""
    return box["data"]


def stdin_json_or_approve():
    """Read stdin JSON. On any parse error, approve_no_mutation (exits 0).
    Returns the parsed dict on success.

    The read is BOUNDED (see _read_stdin_bounded): a stdin that never reaches
    EOF degrades to approve-with-no-mutation instead of hanging the whole Bash
    call. The success path is unchanged -- when the writer closes normally, EOF
    arrives in milliseconds and this parses exactly what it always parsed.
    """
    try:
        return json.loads(_read_stdin_bounded())
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
