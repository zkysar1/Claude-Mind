---
name: stop
description: "Stop the autonomous learning loop"
triggers:
  - "/stop"
conventions: [session-state, handoff-working-memory]
minimum_mode: any
---

# /stop -- Stop the Autonomous Learning Loop

USER-ONLY COMMAND. Claude must NEVER invoke this skill.

## Syntax

```
/stop                  # Stop the currently-bound agent
/stop <agent-name>     # Stop a specific agent (fixes cross-session binding issues)
```

**Step 0: Load Conventions** -- `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded -- proceed to next step.

**Step 0.5: Resolve Target Agent** -- If `<agent-name>` argument provided:

1. Validate agent directory exists: `ls <agent-name>/session/agent-state`
   - If not found: output "Agent '<agent-name>' not found or has no session state." DONE.

2. Set agent prefix for this session:
   All subsequent Bash calls use `AYOAI_AGENT=<agent-name>` prefix.

If no `<agent-name>` provided: use current session binding (existing behavior).

**Step 1: Check State** -- Bash: `session-state-get.sh`
(If agent-name was provided in Step 0.5, the rebinding ensures this reads the target agent's state.)

## Behavior by Current State

### RUNNING

Graceful two-phase stop. The agent finishes its current iteration's obligations
(verify, state-update, learning checks) before stopping. No learning is lost.

**How it works**: This skill sets a `stop-requested` signal but does NOT change state
to IDLE. The stop hook sees RUNNING + no stop-loop → BLOCKs → Claude re-enters the
aspirations loop → the loop detects `stop-requested` at Phase -1.4 → completes any
in-flight obligations from the iteration checkpoint → runs the full stop sequence
(IDLE, consolidation, cleanup, reader mode).

1. Idempotent guard:
   Bash: `session-signal-exists.sh stop-requested`
   IF exit 0 (signal already exists):
       Output: "Stop already requested -- waiting for the loop to finish its current obligations."
       DONE.

2. Set the signal:
   Bash: `session-signal-set.sh stop-requested`

3. Output:
   "Stop requested -- the loop will finish its current obligations and then stop.
   This usually takes under 2 minutes. You'll see progress updates as each step completes."

The actual stop sequence (IDLE, consolidation, cleanup, reader mode) is executed by the
aspirations loop's Phase -1.4 Graceful Stop Handler. See aspirations/SKILL.md.

### IDLE (assistant or reader mode)
1. Check current mode: Bash: `session-mode-get.sh`
2. If mode is not `reader`:
   Bash: `session-mode-set.sh reader`
   Output: "Mode set to reader (read-only). Type `/start --mode assistant` or `/start` to upgrade."
3. If mode is already `reader`:
   Output: "Already in reader mode. Type `/start` to resume."

### UNINITIALIZED
Output: "Agent has not been started yet. Type `/start <name>` to begin."

## Chaining
- Sets: `stop-requested` signal (the aspirations loop handles the actual stop sequence)
- Does NOT call: /aspirations-consolidate (that's now handled by the loop's Phase -1.4)
- Called by: User only. NEVER by Claude.
