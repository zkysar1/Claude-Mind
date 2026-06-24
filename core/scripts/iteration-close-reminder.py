#!/usr/bin/env python3
"""PostToolUse[Bash] hook — iteration-close loop-continuity reminder.

Fires AFTER iteration-close.sh --phase productivity-check OR recurring-close.sh
completes, injecting a system-reminder into the model's context that commands
Skill(aspirations) args='loop' as the next tool call.

Background (session 58 alpha-stopping investigation, 2026-04-24):
The Stop hook does not fire reliably when Claude Code ends a turn with a text
message (no tool calls pending). Alpha's runner SID dec15d77 had zero BLOCK
entries in .stop-hook-log despite the loop repeatedly dying with text-terminal
turns. Relying on Stop to enforce the Skill(aspirations) re-entry contract
fails in practice. PostToolUse fires deterministically on every Bash tool call,
so injecting a reminder here guarantees the model sees the iteration-close
contract at the exact moment it's most likely to drift.

SAFETY (matches bash-agent-inject.py): fail open on ANY error. sys.exit(0)
with empty stdout = "no additional context injected" per Claude Code's
PostToolUse hook contract. A broken hook must never kill the iteration.

Output contract: the injected reminder goes into hookSpecificOutput.
additionalContext per Claude Code's PostToolUse format (same channel used by
path-resolution-hook.py for permissionDecision and bash-agent-inject.py for
updatedInput).
"""

import os
import sys
import json
import re

#  / : force utf-8 on stdin/stdout/stderr (covers Windows
# cp1252 fallback when callers bypass the _platform.sh PYTHONIOENCODING=utf-8
# shim). Closes acceptance (4) of  — stdin-ingest sweep.
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()


REMINDER_TEXT_GENERIC = (
    "<system-reminder>\n"
    "An iteration just closed (iteration-close.sh --phase productivity-check OR "
    "recurring-close.sh completed). Your VERY NEXT tool call MUST be "
    "Skill(aspirations) with args='loop'. Do NOT emit a text summary, do NOT "
    "end with a Bash echo, do NOT write any other tool call first. A terminal "
    "text paragraph or Bash echo here ends the turn without re-entering the "
    "loop and kills the autonomous session. See .claude/rules/return-protocol.md "
    "for the two-case terminal-action rule (sub-skill = Bash; orchestrator at "
    "iteration close = Skill(aspirations)).\n"
    "</system-reminder>"
)

# 8 (zeta investigation 1): on DEEP recurring closes,
# Phase 6 (aspirations-spark) must fire BEFORE Phase 8 state-update. The
# generic reminder above directs Skill(aspirations) only, silently overriding
# recurring-close.sh's outcome-aware stdout imperative (system-reminder wins
# over plain stdout in LLM priority). This second reminder restores the
# spark-first sequence when the OUTCOME=deep marker is present in
# tool_response.stdout. See agents/zeta/reports/
# 1-posttool-hook-overrides-recurring-imperative-analysis.md.
REMINDER_TEXT_DEEP_RECURRING = (
    "<system-reminder>\n"
    "A deep recurring close just completed (recurring-close.sh OUTCOME=deep). "
    "Your VERY NEXT tool call MUST be Skill(aspirations-spark) — Phase 6 spark "
    "fires on deep closes and is NOT wrapped by recurring-close.sh. AFTER "
    "Phase 6 completes, call Skill(aspirations) with args='loop' to re-enter "
    "the loop. Do NOT emit text or a Bash echo before Skill(aspirations-spark). "
    "See .claude/skills/aspirations/SKILL.md \"Recurring-goal shortcut\" section "
    "and .claude/rules/return-protocol.md.\n"
    "</system-reminder>"
)

# Deadman-aware variants (default since Stage 5, 2026-06-23). When the agent
# is deadman-ON (no deadman-disabled opt-out flag), the terminal is the PAIR
# [ScheduleWakeup THEN Skill], not Skill alone. The Skill-only reminders above
# are emitted ONLY when the agent has opted out. Keeping the hook aligned with
# the close-script stdout pair imperative removes the contradiction that
# produced the arm-without-Skill / split-orphan messiness (the system-reminder
# outranks stdout, so a Skill-only reminder here actively suppressed the arm).
REMINDER_TEXT_GENERIC_DEADMAN = (
    "<system-reminder>\n"
    "An iteration just closed (iteration-close.sh --phase productivity-check OR "
    "recurring-close.sh OUTCOME=routine completed). The deadman-switch is ON "
    "(default), so your terminal response MUST be EXACTLY these TWO batched tool "
    "calls, IN THIS ORDER, in this ONE response:\n"
    "  (1) ScheduleWakeup(prompt=\"<<autonomous-loop-dynamic>>\", delaySeconds=600) "
    "— the self-resurrection net; MANDATORY, do NOT omit it; THEN\n"
    "  (2) Skill(aspirations) with args='loop' — the primary re-entry and the "
    "LAST call, which continues the loop NOW.\n"
    "Emit BOTH in this single response with NO text before or between them. Do "
    "NOT emit a text summary and do NOT end with a Bash echo — a terminal text "
    "paragraph ends the turn without re-entering the loop and kills the session. "
    "Skill(aspirations) ALONE keeps THIS iteration alive but leaves the NEXT one "
    "unprotected against a silent text-death, so arm the net EVERY iteration. "
    "See .claude/rules/return-protocol.md (deadman terminal-pair) and "
    "schedule-wakeup-correctness.md.\n"
    "</system-reminder>"
)

REMINDER_TEXT_DEEP_RECURRING_DEADMAN = (
    "<system-reminder>\n"
    "A deep recurring close just completed (recurring-close.sh OUTCOME=deep, "
    "deadman-switch ON). Your VERY NEXT tool call MUST be Skill(aspirations-spark) "
    "— Phase 6 spark fires on deep closes and is NOT wrapped by recurring-close.sh. "
    "AFTER Phase 6 completes, re-enter the loop with the deadman PAIR, in this "
    "order and in one response: ScheduleWakeup(prompt=\"<<autonomous-loop-dynamic>>\", "
    "delaySeconds=600) THEN Skill(aspirations) with args='loop'. Do NOT emit text "
    "or a Bash echo before Skill(aspirations-spark). See "
    ".claude/skills/aspirations/SKILL.md \"Recurring-goal shortcut\" section and "
    ".claude/rules/return-protocol.md.\n"
    "</system-reminder>"
)

# Marker emitted by recurring-close.sh:692-694 when OUTCOME=deep. The literal
# uses an em-dash (U+2014) between "OUTCOME=deep" and "NEXT ACTION REQUIRED"
# — NOT a double-hyphen. The pattern below matches by anchoring on the two
# stable phrases and accepting any separator between them, so future drift
# in the dash style (em-dash → double-hyphen → en-dash, etc.) does not
# silently degrade the deep-recurring branch back to generic. /verify-
# learning sq-018 still pins the literal on the producer side so a
# producer-side typo is caught at verification time.
_DEEP_RECURRING_RE = re.compile(
    r"\[recurring-close\]\s+OUTCOME=deep\s*[—\-]+\s*NEXT ACTION REQUIRED"
)


def _is_deep_recurring_close(tool_response) -> bool:
    """True iff tool_response.stdout contains the recurring-close.sh OUTCOME=deep marker.

    Fail-open: any unexpected shape (None, non-dict, missing stdout, non-str
    stdout) yields False. The generic reminder fires instead — never crashes.
    """
    if not isinstance(tool_response, dict):
        return False
    stdout = tool_response.get("stdout")
    if not isinstance(stdout, str):
        return False
    return _DEEP_RECURRING_RE.search(stdout) is not None


# Terminal-script match is two-layered:
#
#   Layer 1 — segment split on `[;&|]+`. PreToolUse.Bash (bash-agent-inject.py)
#   rewrites every command to prepend `export PATH="..."; export MIND_AGENT=...;
#   export MIND_SID=...; ` — so the actual invocation sits at segment-N, not
#   at start-of-command. Without the split, an anchored regex over the whole
#   string would never match in production.
#
#   Layer 2 — anchored per-segment regex. Each segment must start (after
#   optional whitespace + zero-or-more env-var assignments + optional `bash`)
#   with the target script. Anchoring rejects substrings embedded in `echo`
#   arguments, `grep` patterns, `cat` targets, etc.
#
# CRITICAL — DO NOT "simplify" to a single un-anchored `re.search` over the
# full command. That would re-introduce two incidents at once:
#   (1) false positives on echo/grep/cat arguments that happen to contain the
#       script name (observed 2026-04-24 during this hook's own test run);
#   (2) un-anchored match of the runtime `export X=Y; ... bash script.sh` form
#       would succeed for the wrong reason — it'd match "bash script.sh"
#       deep inside the command rather than proving that segment IS the
#       invocation.
# Both were considered and rejected. Keep the segment-split + anchored match.
_CMD_PREFIX = r"^\s*(?:[A-Z_][A-Z0-9_]*=\S+\s+)*(?:bash\s+)?(?:\S*/)?"

ITER_CLOSE_RE = re.compile(
    _CMD_PREFIX + r"iteration-close\.sh\s+(?:[^|&;]*?\s+)?--phase\s+productivity-check\b"
)
RECURRING_CLOSE_RE = re.compile(
    _CMD_PREFIX + r"recurring-close\.sh(?:\s|$)"
)
_SEGMENT_SEP_RE = re.compile(r"[;&|]+")


def _command_invokes_iteration_close(command: str) -> bool:
    """True iff any command segment invokes iteration-close.sh productivity-check
    or recurring-close.sh as a standalone command. Segments are split on shell
    command separators (`;`, `&&`, `||`, `|`) — not a full shell parser, but
    sufficient for the invocation patterns iteration-close scripts appear in."""
    for segment in _SEGMENT_SEP_RE.split(command):
        if ITER_CLOSE_RE.match(segment) or RECURRING_CLOSE_RE.match(segment):
            return True
    return False


def _read_stdin_with_timeout(timeout_s=None):
    """Read the hook event JSON from stdin without blocking forever (guard-664).

    This is a PostToolUse[Bash] hook: Claude Code pipes the event JSON and
    normally closes the write-end, so EOF arrives in << timeout_s and the read
    completes immediately. But if the hook is orphaned with the stdin pipe's
    write-end held open by an inherited handle (a detached parent or a
    long-lived sibling process), a bare `json.load(sys.stdin)` blocks
    INDEFINITELY -- the mechanism behind the 343h-alive pid-28404 orphan
    (g-115-1382): launched 2026-05-26T17:17:50, flagged by stale-jobs-scan at
    343h, never terminated. guard-664's daemon-thread + join() deadline
    degrades that hang to "no event read -> exit 0 (no injection)" instead of a
    multi-day orphan. select()/signal.alarm do NOT work on Windows pipes; a
    daemon thread does. Mirrors experience.py _read_optional_stdin.

    Returns "" on a tty or on timeout. Tunable via
    ITERATION_CLOSE_REMINDER_STDIN_TIMEOUT_S (default 10s) -- far above any real
    piped delivery (EOF in ms), far below the orphan hang.
    """
    if sys.stdin.isatty():
        return ""
    if timeout_s is None:
        try:
            timeout_s = float(
                os.environ.get("ITERATION_CLOSE_REMINDER_STDIN_TIMEOUT_S", "10")
            )
        except (ValueError, TypeError):
            timeout_s = 10.0
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
        # stdin never reached EOF -- orphaned/inherited pipe. Fail open: no
        # reminder injected this turn (Stop hook + return-protocol discipline
        # are the backstops), but the process EXITS instead of becoming a
        # multi-day orphan (2).
        return ""
    return box["data"]


def main():
    raw = _read_stdin_with_timeout()
    if not raw:
        # tty, empty stdin, or stdin never reached EOF (orphaned pipe) -> fail
        # open with no injection rather than hang forever (2).
        sys.exit(0)
    try:
        data = json.loads(raw)
    except Exception:
        sys.exit(0)

    if data.get("tool_name") != "Bash":
        sys.exit(0)

    tool_input = data.get("tool_input") or {}
    command = tool_input.get("command") or ""
    if not command:
        sys.exit(0)

    if not _command_invokes_iteration_close(command):
        sys.exit(0)

    # Mode gate: the Skill(aspirations) imperative only applies to the runner
    # session of an agent in RUNNING + autonomous. Three cuts, in order:
    #   (a) session_id + PROJECT_ROOT present,
    #   (b) .active-agent-<sid> binding exists and resolves to an agent name,
    #   (c) agent-state=RUNNING, agent-mode=autonomous,
    #   (d) running-session-id matches THIS session_id (runner, not observer).
    # CRITICAL — (d) is NOT optional. agent-state/agent-mode reflect the
    # RUNNER's values; an observer session (reader/assistant started while
    # the agent is RUNNING per CLAUDE.md "observer session") would pass (c)
    # but MUST NOT be told to call Skill(aspirations) — that violates the
    # control-command boundary (user-interaction.md: "Claude MUST NOT invoke
    # boot or start the aspirations loop without RUNNING state and autonomous
    # mode"). Omitting (d) would make observer terminals get hectored with
    # Skill(aspirations) reminders every time the user manually probes
    # iteration-close.sh.
    session_id = data.get("session_id", "") or ""
    project_root = os.environ.get("PROJECT_ROOT", "")
    if not session_id or not project_root:
        sys.exit(0)

    # Path-traversal defense on session_id before joining it into a filesystem
    # path. Same defense posture as stop-hook.sh and path-resolution-hook.py.
    if any(c in session_id for c in ("/", "\\", "\n", "\r", " ")) or ".." in session_id:
        sys.exit(0)

    # Phase 2.6 binding (preferred): agents/<name>/sessions/<SID>/binding.yaml.
    # The directory parent IS the agent name. Glob via os.listdir to avoid
    # PyYAML dependency in this hot-path hook. Without this check, /start
    # --retire-legacy retires the .active-agent-<SID> file and this hook
    # silently exits — leaving the Skill(aspirations) Return Protocol nudge
    # un-fired for every Phase 2.6 session. Matches stop-hook.sh fix
    # (ff6e71c6) + recovery-gate.sh / stop-failure-hook.sh fix (938ca231).
    agent = ""
    agents_parent = os.path.join(project_root, "agents")
    if os.path.isdir(agents_parent):
        try:
            for candidate_agent in os.listdir(agents_parent):
                binding_p26 = os.path.join(
                    agents_parent, candidate_agent, "sessions", session_id, "binding.yaml"
                )
                if os.path.isfile(binding_p26):
                    agent = candidate_agent
                    break
        except OSError:
            pass
    # Legacy fallback: pre-Phase-2.6 .active-agent-<SID> file at PROJECT_ROOT.
    if not agent:
        binding_path = os.path.join(project_root, f".active-agent-{session_id}")
        try:
            with open(binding_path, "r", encoding="utf-8") as fh:
                agent = fh.read().strip()
        except Exception:
            sys.exit(0)
    if not agent:
        sys.exit(0)

    # Phase 2.5.D: agent dirs live under agents/ parent — was previously
    # os.path.join(project_root, agent, "session") which resolved to a
    # non-existent path on disk → every read below failed → reminder
    # silently suppressed.
    session_dir = os.path.join(project_root, "agents", agent, "session")
    try:
        with open(os.path.join(session_dir, "agent-state"), "r", encoding="utf-8") as fh:
            state = fh.read().strip()
        with open(os.path.join(session_dir, "agent-mode"), "r", encoding="utf-8") as fh:
            mode = fh.read().strip()
        with open(os.path.join(session_dir, "running-session-id"), "r", encoding="utf-8") as fh:
            running_sid = fh.read().strip()
    except Exception:
        sys.exit(0)

    if state != "RUNNING" or mode != "autonomous":
        sys.exit(0)
    if running_sid != session_id:
        # Observer session — not the runner. Skill(aspirations) not applicable.
        sys.exit(0)

    # Pick reminder text by inspecting tool_response.stdout for the deep
    # recurring marker. Generic reminder is the default — applies to
    # iteration-close.sh productivity-check AND to OUTCOME=routine recurring
    # closes AND to any unexpected tool_response shape (fail-open).
    tool_response = data.get("tool_response")
    # Deadman-aware (default-ON since Stage 5): emit the terminal-PAIR reminder
    # unless this agent opted out via a deadman-disabled flag — same flag the
    # close scripts gate on. Without this, the Skill-only reminder (which
    # outranks the close-script stdout pair imperative) suppressed the arm.
    deadman_on = not os.path.isfile(os.path.join(session_dir, "deadman-disabled"))
    if _is_deep_recurring_close(tool_response):
        reminder_text = (REMINDER_TEXT_DEEP_RECURRING_DEADMAN if deadman_on
                         else REMINDER_TEXT_DEEP_RECURRING)
    else:
        reminder_text = (REMINDER_TEXT_GENERIC_DEADMAN if deadman_on
                         else REMINDER_TEXT_GENERIC)

    # Inject the imperative as additionalContext. The model sees this in its
    # next turn's context window alongside the tool output, so ignoring it
    # requires actively overriding an explicit <system-reminder> — which is
    # much rarer than forgetting an implicit contract.
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": reminder_text,
        }
    }
    print(json.dumps(payload))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Catch-all fail-open: any unexpected error -> no injection.
        pass
    sys.exit(0)
