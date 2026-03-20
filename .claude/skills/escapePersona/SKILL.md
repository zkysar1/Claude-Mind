---
name: escapePersona
description: "Disable agent persona — act as a standard Claude Code assistant"
user-invocable: true
triggers:
  - "/escapePersona"
conventions: []
---

# /escapePersona — Disable Agent Persona

User-only control command. Claude MUST NOT invoke this skill.

## Steps

1. Bash: `session-persona-get.sh` → read output
   - If output is `false`: output "Persona is already disabled." and STOP
   - If output is `unset` and `mind/session/` doesn't exist: output "No agent state found. Persona is not active." and STOP
2. Bash: `session-persona-set.sh false`
3. Output: "Persona disabled. I'll act as a standard Claude Code assistant until you type `/enterPersona`."
