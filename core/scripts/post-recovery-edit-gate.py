#!/usr/bin/env python3
"""Post-recovery edit gate ().

Refuses Edit/Write/MultiEdit on framework files when the bound agent is in
the (state=IDLE, mode=autonomous) tuple AND is not a worker Body. For a
REDUCER that tuple means "loop crashed or auto-recovered, no goal in flight" —
any framework edit attempted there is an orphan by construction (canonical
incident: charlie session d600a945, 2026-05-20, which landed a verify-learning
check against a confabulated g-115-1014 referenced only by a stale
pre-compaction summary).

That tuple was called "unambiguously" crashed until 2026-08-06, and the
Mind/Body split made the word false: a WORKER Body never flips agent-state, so
(IDLE, autonomous) is its normal healthy working condition. The gate fired on
every framework edit a worker made, leaving the override as the only way
through — the state in which a gate has stopped doing its job. See the
worker-body exemption in main().

Exempt tuples:
  - (IDLE, assistant)     — user-directed work
  - (IDLE, reader)        — already lacks edit capability via permissions
  - (RUNNING, autonomous) — loop owns the edit
  - (IDLE, autonomous) + forked per-session WM — a WORKER Body, working normally
  - (IDLE, autonomous) + stop-checkpoint.json  — mid-graceful-stop, see below
  - UNINITIALIZED         — first-boot work
  - NO_AGENT / NO_SID     — fail open, no binding to gate against

The graceful-stop exemption is the same correction as the worker one, on a
second population the "unambiguously crashed" wording also mis-described. The
deferred stop sequence sets IDLE at D1 and the target mode only at D7, so every
step between them — D4 consolidation above all — runs at (IDLE, autonomous) BY
DESIGN. aspirations-graceful-stop/SKILL.md says so in its own Mode invariant:
"the skill retains autonomous capabilities through D7 even though agent-state
changes mid-skill." A framework edit there is not an orphan; the agent is
executing a documented sequence with a deliberate stop in progress.

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
    from _paths import agent_dir as resolve_agent_dir
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


# POST_RECOVERY_EDIT_OVERRIDE="repairing this gate's own mode read under an explicit user
# directive; the gate refused this very edit from a session whose binding.yaml says
# mode: assistant, which is the defect being fixed and its own live reproduction"
def _read_mode(agent_dir: Path, sid: str = "") -> "str | None":
    """Mode for THIS session — the per-session binding first, agent-wide after.

    THIRD POPULATION THE AGENT-WIDE READ MIS-DESCRIBES (2026-09-04), and the
    same defect as the worker and graceful-stop exemptions below, one layer
    down. Those two fixed WHICH TUPLES are exempt while leaving the tuple itself
    read out of `session/agent-mode`, which is AGENT-WIDE. On a box running more
    than one Body that file holds whatever the Body that started LAST wrote, so
    the mode it reports is not a property of the asking session at all.

    MEASURED on DESKTOP-O91DLK2: an assistant session bound 2026-09-02T19:02:14
    carrying `mode: assistant` in its own sessions/<SID>/binding.yaml was
    refused, because a worker Body started on the same box the next day and left
    `autonomous` in the agent-wide file. The gate's own `(IDLE, assistant) —
    user-directed work` exemption was therefore never reached and the override
    became the only way through — precisely the "how a gate stops being a gate"
    failure the worker exemption was written to end. The edit that introduced
    this docstring was itself refused by this function, which is as direct a
    reproduction as the defect admits.

    This NARROWS the gate; it does not weaken the crashed-reducer catch it
    exists for. A crashed or auto-recovered REDUCER was started autonomous, so
    its binding says `autonomous` and it stays in scope — the canonical incident
    (charlie session d600a945, an orphan edit against a confabulated goal id) is
    refused exactly as before.

    `resolve_binding` is the declared resolver for session identity (CLAUDE.md §
    Session Binding), so mode comes from the SSOT rather than a second
    hand-rolled parse of the same file. It already falls back to the legacy
    `.active-agent-<SID>` form, where `mode` is None — there the agent-wide file
    remains the only signal and the old behaviour is preserved exactly.
    Fail-open on every error path: a gate that cannot resolve a mode must not
    invent one.
    """
    if sid:
        try:
            from _session_binding import resolve_binding

            bound = resolve_binding(sid, PROJECT_ROOT)
            bound_mode = getattr(bound, "mode", None)
            if bound_mode and str(bound_mode).strip():
                return str(bound_mode).strip()
        except Exception:
            pass  # fall through to the agent-wide file

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
        agent_dir = resolve_agent_dir(agent)
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

        agent_dir = resolve_agent_dir(agent)
        if not agent_dir.is_dir():
            approve_no_mutation()

        state = _read_state(agent_dir)
        # POST_RECOVERY_EDIT_OVERRIDE="second half of this gate's own mode-read repair under
        # an explicit user directive — passing sid is what makes the fixed _read_mode reachable"
        # `sid` is load-bearing here, not decorative: without it _read_mode falls
        # back to the AGENT-WIDE agent-mode file, which on a multi-Body box
        # reports whichever Body started last rather than this session. See
        # _read_mode's docstring for the measured case.
        mode = _read_mode(agent_dir, sid)
        # Only the (IDLE, autonomous) tuple is in scope for this gate.
        if state != "IDLE" or mode != "autonomous":
            approve_no_mutation()

        # WORKER-BODY EXEMPTION (). The tuple above stopped being
        # "unambiguously crashed" when the Mind/Body split landed: a WORKER is
        # (IDLE, autonomous) BY DESIGN — it never flips agent-state, and that is
        # its normal, healthy, work-in-flight condition. Without this check the
        # gate fires on every framework edit a worker makes and the only way
        # through is the override, which is how a gate stops being a gate. Live
        # evidence in the ledger before the fix: 3 of the last 3 records were a
        # worker, one of them editing worker-loop/SKILL.md itself.
        #
        # This NARROWS the gate by positively identifying a worker; it does not
        # weaken the crashed-reducer catch the gate exists for. A crashed or
        # auto-recovered REDUCER has no forked per-session working memory (it
        # stays on the agent-wide WM), so the canonical incident — charlie
        # session d600a945, an orphan edit against a confabulated goal id — is
        # still refused exactly as before.
        #
        # Predicate: a Body with a forked sessions/<SID>/working-memory.yaml is
        # a WORKER. Same one bash-agent-inject keys BODY_WM_PATH on and
        # worker_reducer_liveness / reducer_self_fence derive. Derived LOCALLY
        # rather than imported, following guard-2445 — so no module can quietly
        # change another's meaning of "which Body is this".
        if (agent_dir / "sessions" / sid / "working-memory.yaml").exists():
            approve_no_mutation()

        # GRACEFUL-STOP EXEMPTION (). Second population the tuple
        # mis-describes, found the same way the worker one was: the deferred
        # stop sequence sets IDLE at D1 (aspirations-graceful-stop/SKILL.md:268)
        # and the target mode only at D7, so D4 consolidation — and every other
        # step in that window — runs at (IDLE, autonomous) BY DESIGN. Without
        # this the gate refuses framework edits for the whole stop and the only
        # way through is the override, whose token must land IN the edit content
        # and therefore becomes permanent cruft in a framework file. That cost is
        # what made alpha abandon the edit outright on 2026-08-05 rather than
        # override — so the ledger shows zero graceful-stop overrides while the
        # false-positive was real. A ledger of ACCEPTED overrides cannot count
        # REFUSALS; do not read its emptiness as evidence the gate never fired.
        #
        # Predicate: stop-checkpoint.json, written at GS-0 (SKILL.md:134) and
        # cleared ONLY at D7.1 (SKILL.md:481), so its presence brackets exactly
        # the stop sequence and nothing else. Derived LOCALLY per guard-2445
        # rather than shelling out to stop-checkpoint.sh — this is a PreToolUse
        # hot path and stop_checkpoint.py owns the same literal at
        # CHECKPOINT_NAME.
        #
        # guard-1562 enumeration of what is NEWLY permitted: framework edits at
        # (IDLE, autonomous), non-worker, WITH a checkpoint present. THREE
        # members — member (c) was found by the fresh-eyes pass on this very
        # change, after this comment had already been written claiming two.
        # (a) a live graceful stop D1..D7.1 — legitimate per the Mode invariant.
        # (b) an INTERRUPTED stop whose checkpoint outlived it. That is not the
        #     canonical incident's state: a checkpoint exists only because a stop
        #     was deliberately requested, and CLAUDE.md's Session Start Protocol
        #     routes that exact tuple (checkpoint present + mode still
        #     autonomous) straight into /aspirations-graceful-stop --resume. The
        #     crashed-mid-goal reducer the gate exists for leaves NO checkpoint,
        #     so the canonical incident (charlie d600a945) is still refused.
        # (c) NOT BOX-LOCAL — a checkpoint written by ANOTHER instance of this
        #     same agent on a DIFFERENT machine. stop-checkpoint.json is absent
        #     from core/config/session-manifest.yaml, so owncloud_sync falls to
        #     its unregistered heuristic and syncs any known data extension:
        #     measured 2026-08-06, _session_file_machine_local(
        #     "stop-checkpoint.json", ...) returns False, where agent-state
        #     returns True. The record carries stop_started_at / target_mode /
        #     last_updated / resume_count and NO machine or SID field, so this
        #     gate has nothing to key box-identity on and cannot narrow (c)
        #     itself. Registering the file machine_local in the manifest is the
        #     fix, and it is NOT scoped here because the same cross-box leak
        #     already misroutes the Session Start Protocol into
        #     `--resume` for a stop that happened on another box — a larger,
        #     PRE-EXISTING surface than this exemption. Tracked by .
        #     Until that lands, member (c) is a KNOWN residual: it narrows the
        #     gate slightly more than intended on a multi-box agent, and it
        #     cannot re-open the canonical incident, which requires no
        #     checkpoint to exist anywhere.
        if (agent_dir / "session" / "stop-checkpoint.json").exists():
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
