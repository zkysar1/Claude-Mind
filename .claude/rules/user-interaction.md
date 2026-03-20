# User Interaction Protocol

## Immutable Constraints (all states)

- Claude MUST NOT invoke /start, /stop, /reset, /escapePersona, /enterPersona, /open-questions
- Claude MUST NOT modify `mind/session/agent-state` or `mind/session/persona-active` (all access via session-*.sh scripts)
- `/escapePersona` and `/enterPersona` ALWAYS work — persona behavior never blocks the escape hatch
- User directive processing ALWAYS works regardless of persona state

## Response Header (RUNNING state)

When responding to a user message during RUNNING state, always begin output with:
```
═══ RESPONSE ══════════════════════════════════
```

## Priority

Respond to user FIRST, then resume autonomous work.

## State Reporting

When asked about state: `State: {IDLE/RUNNING} | Persona: {ON/OFF} | Loop: {ACTIVE/INACTIVE}`

Bash: `session-state-get.sh`, `session-persona-get.sh`, and `session-signal-exists.sh loop-active`.

## Knowledge Tree Retrieval (MANDATORY)

When persona is active (`session-persona-get.sh` returns `true` or `unset`) and the user asks ANY question that could relate to learned knowledge:

1. Read `mind/knowledge/tree/_tree.yaml` — scan node summaries for relevance
2. For each relevant node: read its `.md` file for content
3. Use retrieved knowledge to inform your answer
4. NEVER say "I don't have context" or "Could you clarify?" without FIRST checking the tree
5. If the tree has nothing relevant: say so explicitly — "I checked my knowledge tree but don't have anything on that topic yet."

This applies in ALL states (RUNNING, IDLE, UNINITIALIZED) as long as persona is active and `mind/knowledge/tree/_tree.yaml` exists. Full retrieval via `retrieve.sh --category {topic} --depth medium` runs in Step 4 of `/respond`, but the tree check above is the **minimum** — it must always happen.

## Routing

When the user sends a message: Read and follow `.claude/skills/respond/SKILL.md`
