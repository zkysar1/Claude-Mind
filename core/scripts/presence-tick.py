#!/usr/bin/env python3
"""presence-tick.py — Record tool call to world/presence/<agent>.jsonl.

PostToolUse hook companion. Reads JSON payload from stdin
({tool_name, session_id, ...}), resolves agent from session binding, and
appends one ~140-byte record to world/presence/<agent>.jsonl via
locked_append_jsonl.

Visibility-only: fail-silent on ALL errors (sys.exit 0 every path).
Visibility hook MUST NEVER block tool execution.

 — Magic-wand framework improvement #1 (cross-agent presence).
Polish layer over per-iteration team-state.last_active heartbeat: per-tool-call
visibility so bravo can see alpha is mid-execution on a 10-minute goal without
waiting for the next heartbeat tick. Lock contention: zero cross-agent (each
agent owns its own file).

Schema: {ts, agent, tool, goal_id, phase, seq, session_id}

Activation: bound by .claude/settings.json PostToolUse hook with matcher='*'
(see g-115-411 description; that hook addition is deny-list-blocked and
requires user-side configuration). Until activated, this script is dormant.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
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


def main() -> int:
    # Step 1: Parse hook payload from stdin
    try:
        payload = json.load(sys.stdin)
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

    # Step 4: Optional goal_id from team-state.in_flight.<agent>
    goal_id = ""
    team_state = Path(WORLD_DIR) / "team-state.yaml"
    if team_state.exists():
        try:
            import yaml
            with open(team_state, encoding="utf-8") as f:
                d = yaml.safe_load(f) or {}
            in_flight = (((d.get("agent_status") or {}).get(agent) or {}).get("in_flight") or {})
            goal_id = in_flight.get("goal_id", "") or ""
        except Exception:
            pass

    # Step 5: Optional phase from execution-diary tail
    phase = ""
    if AGENT_DIR is not None:
        diary = Path(AGENT_DIR) / "session" / "execution-diary.jsonl"
        if diary.exists():
            try:
                with open(diary, encoding="utf-8") as f:
                    lines = [l for l in f.read().splitlines() if l.strip()]
                if lines:
                    last = json.loads(lines[-1])
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
