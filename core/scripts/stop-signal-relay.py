#!/usr/bin/env python3
"""stop-signal-relay.py — deliver a pending stop-requested at the TOOL-CALL boundary.

PostToolUse[*] hook companion. Reads the hook JSON payload from stdin
({tool_name, session_id, ...}), resolves the bound agent from the session
binding, and — when an UNCONSUMED `stop-requested` is sitting on disk for a
RUNNING agent — injects one short imperative into the model's context via
hookSpecificOutput.additionalContext.

WHY THIS EXISTS (g-373-94, measured 2026-09-16T00:15-00:21Z on a live capped
vessel; root cause located by bravo/cc-05 2026-09-22).

The loop has exactly ONE consumption point for `stop-requested`:
`.claude/skills/aspirations/SKILL.md` Phase -1.4, at the TOP of an iteration.
(`aspirations-execute` Phase 4.5 also READS it, but that read is a cooperative
optimisation — it skips reconciliation and proceeds; it does not consume.)

A goal EXECUTION spans arbitrarily many LLM turns and tool calls inside ONE
iteration, so an iteration boundary is NOT a turn boundary and is nowhere near
a tool-call boundary. A stop raised mid-execution therefore waits for the
RESIDUAL GOAL, whose length is unbounded — not for the residual turn. On the
measured vessel the mind was demonstrably alive throughout (serve.log grew; a
NEW completion opened 325 s after the raise) and still never reached Phase
-1.4: the 350 s shutdown grace expired, the run ended on
"framework stop overran its window - interrupting", `stop-requested` sat 0 B
unconsumed, `agent-state` was still RUNNING, and handoff.yaml /
session-summary.yaml were never written. Eight raises on that vessel; one ever
reached the graceful-stop skill; zero ever signed off.

Every prior remedy attempt sized the GRACE against turn length. That is the
wrong denominator — the binding quantity is the residual GOAL — which is why
350 s (already > the 184.195 s max measured turn) still overran, and why no
reserve value can fix it. The fix has to make the signal reachable at a
boundary finer than the iteration. The tool call is the finest boundary this
harness offers, and PostToolUse[*] already fires there.

WHAT THIS ASSERTS, AND WHAT IT DOES NOT (guard-1806).
  asserts : the model is TOLD about an unconsumed stop within ONE tool call.
  does NOT: force the model to act on it, and does not end the run by itself.
It removes the structural impossibility (the loop was never told until the top
of the next iteration). Compliance is still the model's, and the end-to-end
property — a capped vessel run that ends WITH handoff.yaml and
session-summary.yaml and WITHOUT the overrun line — is only provable on a live
capped run.

SAFETY: fail-silent on ALL paths (returns 0 everywhere). Emitting nothing is
the correct degraded behaviour for a PostToolUse hook (guard-141) and is also
the safe direction here: a spurious imperative would push an observer session
into a stop sequence it does not own.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))


def _read_stdin_with_timeout(timeout_s=None):
    """Bounded stdin read — guard-664 / rb-1568 daemon-thread+join pattern.

    Identical rationale to presence-tick.py, which shares this PostToolUse[*]
    chain: an inherited stdin pipe that never reaches EOF orphans this process
    forever (measured 120-129h py.exe orphans, g-115-1578). select()/SIGALRM do
    not work on Windows pipes; a daemon reader thread does.
    """
    if sys.stdin is None or sys.stdin.isatty():
        return ""
    if timeout_s is None:
        timeout_s = float(os.environ.get("STOP_SIGNAL_RELAY_STDIN_TIMEOUT_S", "10"))
    box = {"data": "", "done": False}

    def _reader():
        try:
            box["data"] = sys.stdin.read()
        finally:
            box["done"] = True

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    t.join(timeout_s)
    return box["data"] if box["done"] else ""


def _resolve_agent(project_root, session_id):
    """Agent from the session binding — Phase 2.6 first, then legacy.

    Same resolution order as presence-tick.py. Returns "" when the sid is
    unusable or no binding is found; the caller then emits nothing.
    """
    if not session_id:
        return ""
    if any(c in session_id for c in ("/", "\\", "\n", "\r", " ")) or ".." in session_id:
        return ""
    agents_parent = Path(project_root) / "agents"
    if agents_parent.is_dir():
        try:
            for child in agents_parent.iterdir():
                if not child.is_dir():
                    continue
                if (child / "sessions" / session_id / "binding.yaml").is_file():
                    return child.name
        except OSError:
            pass
    legacy = Path(project_root) / (".active-agent-" + session_id)
    try:
        if legacy.is_file():
            return legacy.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        pass
    return ""


def _imperative(agent, age_s):
    age = "unknown" if age_s is None else ("%.0f" % age_s)
    return (
        "⛔ STOP REQUESTED AND STILL UNCONSUMED — "
        "agents/" + agent + "/session/stop-requested has been set for " + age + "s.\n"
        "The loop consumes this signal ONLY at Phase -1.4, the TOP of the NEXT "
        "iteration. One goal execution spans arbitrarily many tool calls inside "
        "ONE iteration, so continuing to execute can burn the entire shutdown "
        "grace and end the run on an overrun interrupt with no handoff and no "
        "session summary (g-373-94, measured).\n"
        "DO NOT start new work and do not begin another goal phase. Finish or "
        "abandon the tool call in hand, then make Skill(aspirations-graceful-stop) "
        "your NEXT tool call. That skill is built for this entry: its Phase GS-1 "
        "recovers the in-flight iteration checkpoint and completes the pending "
        "verify / state-update obligations, so nothing in flight is dropped."
    )


def main() -> int:
    raw = _read_stdin_with_timeout()
    if not raw:
        return 0
    try:
        payload = json.loads(raw)
    except Exception:
        return 0
    session_id = payload.get("session_id", "")

    try:
        from _paths import PROJECT_ROOT
    except Exception:
        return 0
    if not PROJECT_ROOT:
        return 0

    agent = _resolve_agent(PROJECT_ROOT, session_id)
    if not agent:
        return 0

    sess = Path(PROJECT_ROOT) / "agents" / agent / "session"
    stop_requested = sess / "stop-requested"
    try:
        if not stop_requested.exists():
            return 0
        # stop-loop present => /stop reached D2; the stop sequence is already
        # running and the relay would be pure noise.
        if (sess / "stop-loop").exists():
            return 0
        state = (sess / "agent-state").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return 0
    if state != "RUNNING":
        return 0

    # Speak only to the session that owns the loop. A DEFINITE mismatch is the
    # only skip (same fail-open shape as runner-identity-check.sh): an absent or
    # unreadable running-session-id still relays, because a vessel whose mind is
    # mid-execution needs the signal more than the relay needs certainty.
    try:
        runner = (sess / "running-session-id").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        runner = ""
    if runner and session_id and runner != session_id:
        return 0

    try:
        age_s = time.time() - stop_requested.stat().st_mtime
    except OSError:
        age_s = None

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": _imperative(agent, age_s),
        }
    }))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
