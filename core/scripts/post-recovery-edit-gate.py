#!/usr/bin/env python3
"""Post-recovery edit gate ().

Refuses Edit/Write/MultiEdit on framework files when the bound agent is in
the (state=IDLE, mode=autonomous) tuple. That combination is unambiguously
"loop crashed or auto-recovered, no goal in flight" — any framework edit
attempted there is an orphan by construction (canonical incident: charlie
session d600a945, 2026-05-20, which landed a verify-learning check against
a confabulated g-115-1014 referenced only by a stale pre-compaction summary).

Exempt tuples:
  - (IDLE, assistant)     — user-directed work
  - (IDLE, reader)        — already lacks edit capability via permissions
  - (RUNNING, autonomous) — loop owns the edit
  - UNINITIALIZED         — first-boot work
  - NO_AGENT / NO_SID     — fail open, no binding to gate against

In-scope paths (relative to PROJECT_ROOT):
  - .claude/skills/**
  - .claude/rules/**
  - core/scripts/**
  - core/config/**
  - CLAUDE.md

Override: include `POST_RECOVERY_EDIT_OVERRIDE="<one-line justification>"`
anywhere in the proposed edit content. The override is recorded to the
ledger at world/post-recovery-edits.jsonl AND surfaced to stderr; the edit
is then approved.

Invoked by .claude/settings.json PreToolUse[Edit|Write|MultiEdit].

Hook contract: exit 0 with empty stdout = approve. Structured JSON on
stdout (via hook_helpers.emit_deny) + exit 0 = deny. Any exit code != 0
is treated by Claude Code as a hook ERROR (fail-open) — NOT as a deny.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from hook_helpers import (
        approve_no_mutation,
        emit_deny,
        extract_file_path,
        stdin_json_or_approve,
    )
    from _resolve_agent_from_sid import resolve as resolve_agent
except Exception:
    sys.exit(0)


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

IN_SCOPE_PATTERNS = [
    r"^\.claude/skills/.+",
    r"^\.claude/rules/.+",
    r"^core/scripts/.+",
    r"^core/config/.+",
    r"^CLAUDE\.md$",
]

OVERRIDE_TOKEN = "POST_RECOVERY_EDIT_OVERRIDE="
LEDGER_REL_PATH = "post-recovery-edits.jsonl"


def in_scope(rel_path: str) -> bool:
    rel = rel_path.replace("\\", "/")
    return any(re.match(p, rel) for p in IN_SCOPE_PATTERNS)


def repo_relative(abs_path: str) -> "str | None":
    try:
        return str(Path(abs_path).resolve().relative_to(PROJECT_ROOT)).replace("\\", "/")
    except (ValueError, OSError):
        return None


def has_override(content: str) -> "str | None":
    m = re.search(re.escape(OVERRIDE_TOKEN) + r'"([^"]+)"', content)
    if m:
        return m.group(1)
    return None


def _extract_proposed_content(tool_name: str, tool_input: dict) -> str:
    if not isinstance(tool_input, dict):
        return ""
    if tool_name == "Write":
        return tool_input.get("content", "") or ""
    if tool_name == "Edit":
        return tool_input.get("new_string", "") or ""
    if tool_name == "MultiEdit":
        edits = tool_input.get("edits", []) or []
        return "\n".join(
            (e.get("new_string", "") or "") for e in edits if isinstance(e, dict)
        )
    return ""


def _read_state(agent_dir: Path) -> "str | None":
    path = agent_dir / "session" / "agent-state"
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None


def _read_mode(agent_dir: Path) -> "str | None":
    path = agent_dir / "session" / "agent-mode"
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None


def _world_dir(agent_dir: Path) -> "Path | None":
    """Resolve the agent's WORLD_PATH from local-paths.conf."""
    conf = agent_dir / "local-paths.conf"
    try:
        for line in conf.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("WORLD_PATH="):
                return Path(line.split("=", 1)[1].strip())
    except (OSError, ValueError):
        return None
    return None


def _audit_override(agent: str, rel_path: str, reason: str, tool_name: str) -> None:
    """Append to world/post-recovery-edits.jsonl. Fail-open on any error."""
    try:
        # Find this agent's world dir from local-paths.conf
        agent_dir = PROJECT_ROOT / "agents" / agent
        world = _world_dir(agent_dir)
        if world is None:
            return
        ledger = world / LEDGER_REL_PATH
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "agent": agent,
            "tool": tool_name,
            "path": rel_path,
            "reason": reason,
        }
        with open(ledger, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def main():
    try:
        data = stdin_json_or_approve()
        if not isinstance(data, dict):
            approve_no_mutation()

        tool_name = data.get("tool_name", "")
        if tool_name not in ("Write", "Edit", "MultiEdit"):
            approve_no_mutation()

        tool_input = data.get("tool_input", {})
        file_path = extract_file_path(tool_input)
        if not file_path:
            approve_no_mutation()

        rel = repo_relative(file_path)
        if rel is None or not in_scope(rel):
            approve_no_mutation()

        # Resolve bound agent from SID
        sid = data.get("session_id", "")
        if not sid:
            approve_no_mutation()
        agent = resolve_agent(sid, PROJECT_ROOT)
        if not agent:
            approve_no_mutation()

        agent_dir = PROJECT_ROOT / "agents" / agent
        if not agent_dir.is_dir():
            approve_no_mutation()

        state = _read_state(agent_dir)
        mode = _read_mode(agent_dir)
        # Only the (IDLE, autonomous) tuple is in scope for this gate.
        if state != "IDLE" or mode != "autonomous":
            approve_no_mutation()

        # Override path — log + approve, surface to stderr.
        proposed = _extract_proposed_content(tool_name, tool_input)
        reason = has_override(proposed) if proposed else None
        if reason:
            _audit_override(agent, rel, reason, tool_name)
            sys.stderr.write(
                f"[post-recovery-edit-gate] OVERRIDE accepted for {rel}: {reason}\n"
            )
            approve_no_mutation()

        deny_reason = (
            f"REFUSED: post-recovery-edit gate.\n\n"
            f"The {tool_name} to `{rel}` is blocked.\n\n"
            f"Agent `{agent}` is in (state=IDLE, mode=autonomous) — that tuple\n"
            "means the autonomous loop crashed or was auto-recovered from a hung\n"
            "autocompact. There is no goal in flight, so framework-file edits\n"
            "attempted from here are orphans by construction (canonical incident:\n"
            "g-001-15 / charlie session d600a945, 2026-05-20).\n\n"
            "To proceed, fix the state first:\n"
            "  /start " + agent + " --mode autonomous   (resume the loop properly)\n"
            "  /start " + agent + " --mode assistant    (user-directed mode, edits OK)\n\n"
            "Bypass (rare — recovery-flow edits before /start can be invoked):\n"
            "include POST_RECOVERY_EDIT_OVERRIDE=\"<one-line justification>\" in\n"
            "the edit content. Logged to world/post-recovery-edits.jsonl."
        )
        emit_deny(deny_reason)
    except Exception:
        try:
            sys.exit(0)
        except Exception:
            pass


if __name__ == "__main__":
    main()
