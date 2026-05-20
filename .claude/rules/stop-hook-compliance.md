# Stop Hook Compliance

## The Recovery Protocol

When a stop hook fires with a recovery instruction (e.g., "invoke /aspirations loop"),
the agent MUST follow that instruction. The stop hook exists because context compression
(autocompact) is a normal part of long-running sessions. Losing context is expected, not
a signal to stop.

## Rules

1. **Follow the hook instruction** — If the hook says "invoke /aspirations loop", do exactly that.
   Do not rationalize. Do not write a handoff. Do not consolidate. Just re-enter the loop.

2. **Never manually change state** — The agent MUST NOT call any of these directly:
   - `session-state-set.sh` — only /start, /stop, and the Phase -1.4 Graceful Stop Handler may change agent state
   - `session-signal-set.sh stop-loop` — only /stop and Phase -1.4 may set stop-loop
   - `session-signal-set.sh stop-requested` — only /stop may set stop-requested
   The agent MUST NOT create or modify `agents/<agent>/session/stop-loop`, `agents/<agent>/session/stop-requested`,
   or `agents/<agent>/session/agent-state` by any means (touch, Write, echo, python).

   <!-- exception added 2026-04-18 for productivity-stop-gate -->
   **Exception**: `core/scripts/productivity-stop-gate.sh` (invoked only by
   `iteration-close.sh --phase productivity-check` at the end of every iteration) is
   authorized to set `stop-requested` when the composite productivity score falls
   below the configured floor AND the session has run at least `min_iterations` goals.
   The gate is script-gated — not LLM-discretionary — so the agent cannot bypass the
   threshold math. The script MUST write `agents/<agent>/session/stop-target-mode`
   ("assistant") BEFORE setting the signal, preserving the /stop invariant that
   Phase -1.4 reads the target mode without a fallback.
   Parameters: `core/config/aspirations.yaml` → `productivity_gate`
   (`min_iterations`, `stop_threshold`). This is the ONLY authorized
   caller of `session-signal-set.sh stop-requested` outside `/stop`.

   <!-- exception added 2026-04-19 for recovery-gate (cross-agent visibility plan) -->
   **Exception**: `core/scripts/recovery-gate.sh` (invoked only by the
   SessionStart hook in `.claude/settings.json`) is authorized to call
   `session-state-set.sh IDLE` (RUNNING → IDLE only) AND
   `session-manifest-clear.sh` under a script-gated 6-condition AND-gate.
   The LLM MUST NOT invoke `recovery-gate.sh` directly — only the
   SessionStart hook may. The 6-condition spec (state=RUNNING + heartbeat
   stale + no recent stop-hook BLOCK + execution-diary stale + no
   stop-requested + no pending background job) is cataloged in the
   `recovery-gate` convention (see `core/config/conventions/recovery-gate.md`).
   This is the second authorized caller of `session-state-set.sh` outside
   `/start` and `/stop` — `productivity-stop-gate.sh` is the first
   (productivity-stop-gate stays in RUNNING and only sets the stop signal;
   recovery-gate moves RUNNING → IDLE).

3. **Context compression is normal** — "The session has been running for a long time" is NOT
   a reason to stop. Autocompact compresses context to free space. The loop is designed to
   run indefinitely. Re-enter it.

4. **Long sessions are not failures** — The loop runs until the user says /stop. A session
   being long, context being compressed, or the agent feeling "done" are not stop conditions.
   The Stop Conditions list in aspirations/SKILL.md is exhaustive.

5. **Do not rationalize around the hook** — If the hook blocks your stop attempt, it is
   doing its job. Do not look for ways around it. Follow the instruction it gives you.

6. **Graceful stop is the normal path** — When `stop-requested` is detected at Phase -1.4,
   the loop completes in-flight obligations (verify, state-update) before stopping. This is
   expected behavior, not an error. Do not skip obligations to speed up the stop.
