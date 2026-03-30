---
name: stop
description: "Stop the autonomous learning loop"
triggers:
  - "/stop"
conventions: [session-state, handoff-working-memory]
minimum_mode: any
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
   # MODE ORDER: consolidation has minimum_mode: autonomous. Mode must still be
   # autonomous here. Step 5 (reader mode) MUST come AFTER this step, not before.
   invoke /aspirations-consolidate with: stop_mode = true
   This runs the full consolidation pipeline (encoding queue flush, micro-hypothesis
   sweep, knowledge debt sweep, experience archive maintenance, journal, working memory
   archive + reset, aspiration archive sweep, continuation handoff) while skipping
   non-essential steps (tree rebalancing, skill reports, user recap, restart).
5. Reset to reader mode (safe baseline):
   Bash: `session-mode-set.sh reader`
6. Output:
   "Agent stopped. Session consolidated — encoding, journal, and handoff saved.
   Mode set to reader (read-only). You can now chat with me — I have full access to
   all accumulated knowledge. Ask me anything.
   Type `/start` to resume autonomous mode, or `/start --mode assistant` for user-directed learning."

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
- Calls: /aspirations-consolidate (with stop_mode=true)
- Called by: User only. NEVER by Claude.
