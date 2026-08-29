---
description: "When pseudocode names an action in natural language, check world/forged-skills.yaml triggers and invoke the matching skill, never improvise."
---

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

## Preferred Phrasing for Notification Calls

When a core or base skill needs to notify, alert, or reach out to the user,
use this canonical phrasing block so the forged-skill resolver fires
reliably:

```
Notify the user about <event>.
(Check world/forged-skills.yaml for a skill whose triggers match
"notify the user" and invoke it with a short subject and message. If no
matching skill is registered, fall back to a `participants: [agent, user]`
goal via aspirations-add-goal.sh. Never block on notification failure.)
```

Why the canonical form:

- The phrase `"notify the user"` is a registered trigger in forged skills
  that provide notification transport — zero ambiguity for rule 1 above.
- The parenthetical makes forged-skill resolution explicit at every call
  site rather than relying on the LLM to remember to consult the registry.
- Domain-agnostic: no transport name (email, Slack, webhook, etc.) or
  skill name (`/notify-user`) is hardcoded, so the base skill stays
  portable across domains with different notification forged skills.
- Preserves the Tier-3 fallback when no forged skill is registered,
  keeping the base skill usable in fresh worlds.

**Anti-pattern:** hardcoding a specific forged skill name like
`Invoke /notify-user with category: X` in a base skill. That couples the
core to a domain-specific skill and violates rule 1 of this file plus
`.claude/rules/domain-free-examples.md`.
