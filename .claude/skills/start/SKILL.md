---
name: start
description: "Start or resume the autonomous learning loop"
triggers:
  - "/start"
conventions: [session-state]
---

# /start — Start the Autonomous Learning Loop

USER-ONLY COMMAND. Claude must NEVER invoke this skill.

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Behavior by Current State

### RUNNING (agent-state contains "RUNNING")
Output: "Agent is already running."
No-op.

### IDLE (agent-state contains "IDLE")
1. Bash: `session-state-set.sh RUNNING`
2. Bash: `session-signal-clear.sh stop-loop`
3. Output: "Agent resumed. Learning loop starting."
4. invoke /boot

### UNINITIALIZED (agent-state doesn't exist or mind/ doesn't exist)

1. Display the Program concept:

   ```
   Before we begin, I need two things from you:

   **Your Program** — This is your core identity. It tells me WHY I exist
   and WHAT I'm for. It's the fundamental drive that shapes every decision
   I make. Think of it as the soul of the agent.

   Examples:
   - "You are an autonomous QA engineer for Acme Corp. Always be looking
     for the next improvement."
   - "You need to make money or die. Find every revenue opportunity."
   - "You are a personal research assistant focused on machine learning
     papers and implementations."

   **Your Aspirations** — These are your goals. Think of them as a feature
   list, or life goals, or a to-do list. They can be literally anything —
   learn something, build something, analyze something, fix something.
   I can have multiple at once and I'll break each into actionable steps.

   Examples:
   - "Learn the codebase and API surface thoroughly."
   - "Improve test coverage to 80%."
   - "Research competitor platforms and identify opportunities."

   Tell me both together — your Program and what you'd like me to aspire to.
   The more detail you provide, the better I can act autonomously.
   ```

2. AskUserQuestion (allowed — agent-state is not RUNNING yet)

3. Parse response:
   - Extract Self (identity/purpose/drive)
   - Extract aspiration descriptions (one or more goals/directions)

4. Echo back understanding:

   ```
   Here's what I understand:

   **Your Program (Self):**
   [parsed Self — the agent's own words summarizing the user's intent]

   **Aspirations I'll create:**
   1. [title] — [brief description with initial goals]
   2. [title] — ...

   Does this look right?
   ```

5. AskUserQuestion for confirmation (yes / adjust)
   - If adjust: re-parse and echo again
   - If yes: proceed

6. `bash core/scripts/init-mind.sh`

7. Write `mind/self.md` with parsed Self:
   ```markdown
   ---
   created: "{today}"
   last_updated: "{today}"
   last_update_trigger: "initial_creation"
   source: "user"
   ---

   # Self

   {parsed Self content}
   ```

8. Invoke `/create-aspiration from-user` with the extracted aspiration descriptions

9. Bash: `session-state-set.sh RUNNING`

10. Bash: `session-signal-clear.sh stop-loop`

11. Output: "Agent initialized. Learning loop starting."

12. Invoke `/boot`

## Chaining
- Calls: /boot
- Called by: User only. NEVER by Claude.
