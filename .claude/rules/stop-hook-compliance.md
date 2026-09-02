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
   (`min_iterations`, `stop_threshold`). This was the ONLY authorized caller of
   `session-signal-set.sh stop-requested` outside `/stop` until 2026-08-05;
   `reducer-self-fence.sh` (below) is now the second. Whoever adds a fourth must
   correct this sentence in the same change — an authoritative-sounding count
   that has silently gone stale is worse than no count at all.

   <!-- exception added 2026-08-05 for reducer-self-fence (g-306-225) -->
   **Exception**: `core/scripts/reducer-self-fence.sh` (invoked only by
   `heartbeat-tick.sh`, on every tick, whatever the backend — g-115-8200)
   is authorized to set `stop-requested` when the cross-machine runner lease says
   this box is no longer the reducer. The runner claim is a LEASE, and a lease
   needs `T_stepdown < T_takeover`: the holder must stop acting as leader before a
   peer may legally seize the claim, or both act as reducer at once. `T_stepdown`
   was effectively INFINITY until this gate existed — measured 2026-08-05, cc-04
   lost its claim at 14:38 and kept executing goals as reducer for 2.5+ hours
   while two other bodies acquired it.
   Like productivity-stop-gate, the script MUST write
   `agents/<agent>/session/stop-target-mode` ("assistant") BEFORE setting the
   signal, preserving the /stop invariant that Phase -1.4 reads the target mode
   without a fallback. The LLM MUST NOT invoke it directly — only
   `heartbeat-tick.sh` may.
   The decision is script-gated in `core/scripts/reducer_self_fence.py::decide`
   (pure, fully branch-tested) and fires on exactly TWO triggers, both of which
   must be read against the signal asymmetry that governs this whole gate: a
   FAILED RENEWAL is ambiguous (a broken writer and a dead agent look identical
   from here), while a claim held by a DIFFERENT MACHINE is unambiguous.
   - `different-holder` — the live claim names another machine. Decisive alone.
   - `sustained-renewal-gap` — renewal has failed CONTINUOUSLY for
     `runner_heartbeat.stepdown_seconds` (1950s = half of T_takeover, deliberately
     equal to the escalation threshold heartbeat-tick.sh already warns at).
   Every other signal HOLDS, and that is the load-bearing half: a transient
   daemon blip, an unreadable holder id, an unrecognised rc, and `rc=4`
   (ABSENT | NOT-RUNNING | STALE | REFUSE) all keep the loop running. Stopping a
   healthy loop on a plumbing fault is worse than the disease (guard-1562). Note
   `rc=4` is DECISIVE in the sibling `worker_reducer_liveness.py` and INERT here —
   the two modules are deliberate mirrors with opposite fail-safe directions, and
   `test_reducer_self_fence.py` pins that divergence against the real worker
   module so a future fusion of the two fails loudly.

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

   <!-- exception added 2026-09-01 for recovery-yank-reverse (g-357-51) -->
   **Exception**: `core/scripts/recovery-yank-reverse.sh` (invoked only by
   `stop-hook.sh` Gate 1-pre, when the turn-ending session finds agent-state
   IDLE beside a `session/recovery-log.jsonl`) is authorized to call
   `session-state-set.sh RUNNING` (IDLE → RUNNING only) for the ONE sid the
   recovery gate demoted: a process executing its own stop hook is alive by
   construction, so that demotion was false (the 2026-09-01 rate-limited-alive
   kill). Script-gated by `recovery_yank.py preconditions` — same sid, bound
   autonomous BEFORE the yank, inside the reversal window (default 6h), no
   user-stop artifact after the yank, no peer holding the runner claim — and
   every miss is a no-op. The LLM MUST NOT invoke it directly. Third authorized
   caller outside `/start` and `/stop`; the only one that moves IDLE → RUNNING.

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
