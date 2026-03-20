---
name: stop
description: "Stop the autonomous learning loop"
triggers:
  - "/stop"
conventions: [session-state, handoff-working-memory]
---

# /stop — Stop the Autonomous Learning Loop

USER-ONLY COMMAND. Claude must NEVER invoke this skill.

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Behavior by Current State

### RUNNING
1. Bash: `session-state-set.sh IDLE`
2. Bash: `session-signal-set.sh stop-loop`
3. Reset in-progress goals to pending (they didn't complete):
   Bash: `load-aspirations-compact.sh` → IF path returned: Read it
   (compact data has IDs, titles, statuses — no descriptions/verification)
   For each in-progress goal: Bash: `aspirations-update-goal.sh <goal-id> status pending`
4. Run session-end consolidation:
   invoke /aspirations-consolidate with: stop_mode = true
   This runs the full consolidation pipeline (encoding queue flush, micro-hypothesis
   sweep, knowledge debt sweep, experience archive maintenance, journal, working memory
   archive + reset, aspiration archive sweep, continuation handoff) while skipping
   non-essential steps (tree rebalancing, skill reports, user recap, restart).
5. Output:
   "Agent stopped. Session consolidated — encoding, journal, and handoff saved.
   You can now chat with me normally — I have full access to
   all accumulated knowledge and configuration. Ask me anything, or instruct
   me to restructure the agent. Type /start to resume."

### IDLE
Output: "Agent is already stopped. Type /start to resume."

### UNINITIALIZED
Output: "Agent has not been started yet. Type /start to begin."

## Chaining
- Calls: /aspirations-consolidate (with stop_mode=true)
- Called by: User only. NEVER by Claude.
