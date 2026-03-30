---
name: recover
description: "Last-resort recovery — diagnose state, report to user, enable clean exit"
user-invocable: false
triggers:
  - "/recover"
conventions: [session-state, handoff-working-memory]
minimum_mode: autonomous
---

# /recover — Last-Resort Recovery

Invoked by the stop hook after 3 failed re-entry attempts. Reads agent state,
outputs a friendly status to the user explaining what happened, and creates
`<agent>/session/stop-loop` so the next stop attempt succeeds.

This skill does NOT resume the loop. It gives control back to the user.

## Constraints

- MUST NOT modify `<agent>/session/agent-state` (only /start and /stop can)
- MUST NOT invoke `/aspirations loop` (that already failed 3 times)
- MUST create `<agent>/session/stop-loop` (enables clean exit)

## Steps

### Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

### Step 1: Read State

```
Bash: `session-state-get.sh` → agent state
Bash: wm-read.sh --json
Bash: aspirations-read.sh --summary (if available)
Bash: `session-counter-get.sh` → stop-block-count (to confirm we're in Tier 4)
```

### Step 2: Output Recovery Status

Output a clear, friendly message to the user:

```
=============== RECOVERY ===============

The autonomous loop lost context and couldn't recover after 3 attempts.

Current state:
  Agent: {RUNNING/IDLE}
  Last goal: {Bash: wm-read.sh session_goal, or "unknown"}
  Aspirations: {summary count, or "unavailable"}

What you can do:
  /stop   — Stop the agent and enter assistant mode
  /start  — Restart the autonomous loop fresh
  (or just chat — the agent will respond normally)

The loop will exit cleanly on the next cycle.
========================================
```

### Step 3: Enable Clean Exit

```
Bash: `session-signal-set.sh stop-loop`
```

This ensures the stop hook allows the next stop attempt through,
breaking the block cycle without modifying agent-state.

### Step 4: Clean Up Counter

```
Bash: `session-counter-clear.sh`
```
