---
description: "When the stop hook says re-enter the loop, do that; never touch agent-state, stop-loop or stop-requested; a long session is no stop reason."
---

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

   **Authorized signal writers** (the LLM MUST NOT invoke any of these directly):
   - `productivity-stop-gate.sh` — sets `stop-requested` (script-gated, from `iteration-close.sh`)
   - `reducer-self-fence.sh` — sets `stop-requested` (script-gated, from `heartbeat-tick.sh`)
   - `loop-exhaustion-fence.sh` — sets `stop-requested` (script-gated, from `stop-hook.sh`)
   - vessel sidecar (`zakcode`) — sets `stop-requested` (addressed, from served-run end)
   - `recovery-gate.sh` — sets `agent-state` RUNNING->IDLE (script-gated, from SessionStart hook)
   - `recovery-yank-reverse.sh` — sets `agent-state` IDLE->RUNNING (script-gated, from `stop-hook.sh` Gate 1-pre)
   - vessel recipe (`bootstrap.sh` 3.5/3.5b, out-of-repo) — sets `agent-state` UNINITIALIZED->IDLE at landing; heals RUNNING->IDLE each boot, removing `stop-requested`/`stop-loop` (liveness-gated, g-377-37)

   All `stop-requested` writers MUST write `stop-target-mode` ("assistant")
   BEFORE setting the signal. Full catalog with invariants, trigger conditions,
   and incident traces: `core/config/conventions/stop-signal-writers.md`
   (`load-conventions.sh stop-signal-writers`).

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
