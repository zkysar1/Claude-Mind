#!/usr/bin/env python3
"""presence-tick.py — Record tool call to world/presence/<agent>.jsonl.

PostToolUse hook companion. Reads JSON payload from stdin
({tool_name, session_id, ...}), resolves agent from session binding, and
appends one ~140-byte record to world/presence/<agent>.jsonl via
locked_append_jsonl.

Visibility-only: fail-silent on ALL errors (sys.exit 0 every path).
Visibility hook MUST NEVER block tool execution.

g-115-411 — Magic-wand framework improvement #1 (cross-agent presence).
Polish layer over per-iteration team-state.last_active heartbeat: per-tool-call
visibility so bravo can see alpha is mid-execution on a 10-minute goal without
waiting for the next heartbeat tick. Lock contention: zero cross-agent (each
agent owns its own file).

Schema: {ts, agent, tool, goal_id, phase, seq, session_id}

Activation: LIVE -- bound by .claude/settings.json PostToolUse hook with
matcher='*' (settings.json ~L360-367; fires on every tool call). The
"deny-list-blocked / dormant" note from g-115-411 is STALE: the hook is
active on this install (g-115-1578, 2026-06-20 -- confirmed by live orphan
evidence in core/logs/stale-scanner-report.jsonl + the settings.json
matcher). Because it is live AND reads stdin, the read MUST be bounded (see
_read_stdin_with_timeout) -- an orphaned pipe otherwise hangs this py.exe
process forever (observed 120-129h before this guard).
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import threading
from pathlib import Path

#  / : force utf-8 on stdin/stdout/stderr (covers Windows
# cp1252 fallback when callers bypass the _platform.sh PYTHONIOENCODING=utf-8
# shim). Closes acceptance (4) of  — stdin-ingest sweep.
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# Force UTF-8 to match sibling scripts; prevents cp1252 fallback on Windows.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _read_stdin_with_timeout(timeout_s=None):
    """Bounded stdin read -- guard-664 / rb-1568 daemon-thread+join pattern.

    A PostToolUse hook can be handed an inherited stdin pipe that never
    reaches EOF (parent session dies, payload write interrupted). An
    unbounded read of sys.stdin then blocks forever, orphaning this
    py.exe child (g-115-1578: pid-18456 ran 129h, pid-23168 ran 120h).
    select()/signal.alarm do NOT work on Windows pipes; a daemon reader
    thread does -- when main() returns the interpreter exits and the
    still-blocked daemon thread is killed with it. Fail open (return "")
    on timeout so this visibility hook never blocks tool execution.
    Canonical reference: experience.py::_read_optional_stdin.
    """
    if sys.stdin is None or sys.stdin.isatty():
        return ""
    if timeout_s is None:
        timeout_s = float(os.environ.get("PRESENCE_TICK_STDIN_TIMEOUT_S", "10"))
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


def _read_last_line(path, encoding="utf-8", tail_bytes=8192):
    """Last non-empty line of a file without loading the whole file ().

    presence-tick fires on EVERY tool call (PostToolUse matcher='*') and Step 5
    previously did f.read().splitlines() of the fully-unbounded
    execution-diary.jsonl just to grab the last record's phase -- O(filesize)
    per tool call. This seeks the final tail_bytes block instead (each diary
    record is ~140 bytes, so the last complete line is always within it) and
    returns the last non-empty line. A truncated first line in the block is
    discarded (only the last line is used); errors='replace' tolerates a tail
    cut mid-multibyte-char. Bounded O(tail_bytes) regardless of file size.
    Fail-open is the caller's job (json.loads wrapped in try/except).
    """
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - tail_bytes))
        chunk = f.read()
    lines = [ln for ln in chunk.decode(encoding, errors="replace").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def main() -> int:
    # Step 1: Parse hook payload from stdin (bounded read -- guard-664/rb-1568;
    # an unbounded json.load here orphaned this hook 120-129h, ).
    raw = _read_stdin_with_timeout()
    if not raw:
        return 0
    try:
        payload = json.loads(raw)
    except Exception:
        return 0

    tool_name = payload.get("tool_name", "")
    session_id = payload.get("session_id", "")
    if not tool_name:
        return 0

    # Step 2: Resolve framework paths
    try:
        from _paths import WORLD_DIR, PROJECT_ROOT, AGENT_DIR
        from _fileops import locked_append_jsonl
    except Exception:
        return 0

    if WORLD_DIR is None or PROJECT_ROOT is None:
        return 0

    # Step 3: Resolve agent from session binding.
    # Tries Phase 2.6 (agents/<name>/sessions/<SID>/binding.yaml) first,
    # then legacy (.active-agent-<SID>). Without Phase 2.6, presence
    # logging silently degrades for non-Bash hook tool calls
    # (Write/Edit/MultiEdit don't get MIND_AGENT env-injected).
    agent = ""
    if session_id and not any(c in session_id for c in ("/", "\\", "\n", "\r", " ")) and ".." not in session_id:
        agents_parent = Path(PROJECT_ROOT) / "agents"
        if agents_parent.is_dir():
            try:
                for child in agents_parent.iterdir():
                    if not child.is_dir():
                        continue
                    binding_p26 = child / "sessions" / session_id / "binding.yaml"
                    if binding_p26.is_file():
                        agent = child.name
                        break
            except OSError:
                pass
        if not agent:
            binding = Path(PROJECT_ROOT) / f".active-agent-{session_id}"
            if binding.exists():
                try:
                    agent = binding.read_text(encoding="utf-8").strip()
                except Exception:
                    pass
    if not agent:
        # Final fallback: MIND_AGENT env var (available for PreToolUse[Bash]
        # but NOT for Write/Edit/MultiEdit hooks).
        agent = os.environ.get("MIND_AGENT", "").strip()
    if not agent:
        return 0

    # Step 4: Optional goal_id from the agent's team-state row (
    # sharding: row file first, core-file residual fallback).
    goal_id = ""
    try:
        from _team_state import read_agent_row
        status = read_agent_row(Path(WORLD_DIR), agent,
                                core_path=Path(WORLD_DIR) / "team-state.yaml") or {}
        goal_id = (status.get("in_flight") or {}).get("goal_id", "") or ""
    except Exception:
        pass

    # Step 5: Optional phase from execution-diary tail
    phase = ""
    if AGENT_DIR is not None:
        diary = Path(AGENT_DIR) / "session" / "execution-diary.jsonl"
        if diary.exists():
            try:
                last_line = _read_last_line(diary)
                if last_line:
                    last = json.loads(last_line)
                    phase = last.get("phase", "") or ""
            except Exception:
                pass

    # Step 6: Per-agent monotonic seq (best-effort; reset on file removal)
    presence_dir = Path(WORLD_DIR) / "presence"
    try:
        presence_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return 0

    seq_file = presence_dir / f".seq-{agent}"
    seq = 0
    try:
        if seq_file.exists():
            seq = int(seq_file.read_text().strip() or "0")
    except Exception:
        seq = 0
    seq += 1
    try:
        seq_file.write_text(str(seq))
    except Exception:
        pass

    # Step 7: Build + append record
    record = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "agent": agent,
        "tool": tool_name,
        "goal_id": goal_id,
        "phase": phase,
        "seq": seq,
        "session_id": session_id,
    }

    try:
        locked_append_jsonl(presence_dir / f"{agent}.jsonl", record)
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
