# Stop Hook Compliance

## The Recovery Protocol

When a stop hook fires with a recovery instruction (e.g., "invoke /aspirations loop"),
the agent MUST follow that instruction. The stop hook exists because context compression
(autocompact) is a normal part of long-running sessions. Losing context is expected, not
a signal to stop.

## Rules

1. **Follow the hook instruction** — If the hook says "invoke /aspirations loop", do exactly that.
   Do not rationalize. Do not write a handoff. Do not consolidate. Just re-enter the loop.

2. **Never manually set stop-loop** — Only `/stop` (user command, sets IDLE first) and
   `/recover` (Tier 4, after 3 failed re-entries) may create the stop-loop signal.
   The agent MUST NOT call `session-signal-set.sh stop-loop` directly.
   The agent MUST NOT create `mind/session/stop-loop` by any means (touch, Write, echo, python).

3. **Context compression is normal** — "The session has been running for a long time" is NOT
   a reason to stop. Autocompact compresses context to free space. The loop is designed to
   run indefinitely. Re-enter it.

4. **Long sessions are not failures** — The loop runs until the user says /stop. A session
   being long, context being compressed, or the agent feeling "done" are not stop conditions.
   The Stop Conditions list in aspirations/SKILL.md is exhaustive.

5. **Do not rationalize around the hook** — If the hook blocks your stop attempt, it is
   doing its job. Do not look for ways around it. Follow the instruction it gives you.
