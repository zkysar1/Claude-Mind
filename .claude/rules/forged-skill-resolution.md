# Forged Skill Resolution

## Principle

Core pseudocode uses natural language to describe actions (e.g., "notify the user",
"deploy the build", "run the processor"). These are intentionally generic — the core
is domain-agnostic. The domain-specific implementation lives in forged skills.

## Rules

1. **When pseudocode describes an action in natural language**, check
   `world/forged-skills.yaml` for a forged skill whose `triggers` match the phrase.
   If found, invoke that skill instead of attempting the action inline.

2. **Never reason about whether a skill "should" exist.** Check the registry.
   If `world/forged-skills.yaml` lists a skill with matching triggers, invoke it.
   If no match, proceed with the best available approach.
