---
name: enterPersona
description: "Re-enable agent persona and knowledge tree usage"
user-invocable: true
triggers:
  - "/enterPersona"
conventions: []
---

# /enterPersona — Re-Enable Agent Persona

User-only control command. Claude MUST NOT invoke this skill.

## Steps

1. Bash: `session-persona-get.sh` → read output
   - If output is `unset` and `mind/session/` doesn't exist: output "Agent not initialized. Run /start first." and STOP
2. Bash: `session-persona-set.sh true`
3. Invoke `/prime` to load domain knowledge into active context
4. Output: "Persona re-enabled and context primed."
