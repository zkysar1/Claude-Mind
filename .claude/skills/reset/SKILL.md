---
name: reset
description: "Factory reset — wipe all accumulated state"
triggers:
  - "/reset"
conventions: []
---

# /reset — Factory Reset

USER-ONLY COMMAND. Claude must NEVER invoke this skill.

## Step 1: Confirmation
Output:
"WARNING: This will delete ALL accumulated knowledge, hypotheses, journal
entries, session state, learned patterns, and forged skill directories.
Framework files (rules, base skills, CLAUDE.md, config, scripts) are unchanged.
Type 'confirm reset' to proceed."

Wait for user response. If not exactly "confirm reset", output "Reset cancelled."

## Step 2: Execute
1. Run: bash core/scripts/factory-reset.sh --force
2. Output: "Agent reset to blank slate. Type /start to begin fresh."

## Chaining
- Calls: core/scripts/factory-reset.sh
- Called by: User only. NEVER by Claude.
