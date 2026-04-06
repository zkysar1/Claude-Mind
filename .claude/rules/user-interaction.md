# User Interaction Protocol

## Immutable Constraints (all states)

- Claude MUST NOT invoke /start, /stop, /open-questions
- Claude MUST NOT modify `<agent>/session/agent-state`, `<agent>/session/agent-mode`, or `<agent>/session/persona-active` — see Script-Level Restrictions below
- User directive processing works in assistant and autonomous modes (not reader mode)

## Script-Level Restrictions

The following scripts perform user-only state changes. Claude MUST NOT call them
directly via Bash — they may only be executed as part of user-invoked skills:

- `session-state-set.sh` — only /start and /stop may change agent state
- `session-mode-set.sh` — only /start and /stop may change agent mode
- `init-mind.sh`, `init-world.sh`, `init-agent.sh`, `init-meta.sh` — only /start and /boot may initialize

The following are restricted to specific callers:

- `session-persona-set.sh false` — only /stop
- `session-persona-set.sh true` — /start, /boot
- `session-signal-set.sh stop-loop` — only /stop (existing rule, see stop-hook-compliance.md)

The agent retains full access to all **read-only** session scripts:
`session-state-get.sh`, `session-mode-get.sh`, `session-persona-get.sh`, `session-signal-exists.sh`

And to these **write** scripts for legitimate loop operations:
`session-signal-set.sh loop-active`, `session-signal-clear.sh *`

Claude MUST NOT write to session state files directly (via Write, Edit, echo, cat, python, etc.):
`agent-state`, `agent-mode`, `persona-active`, `stop-loop`, `.active-agent-*`

## Response Header (RUNNING state)

When responding to a user message during RUNNING state, always begin output with:
```
═══ RESPONSE ══════════════════════════════════
```

## Priority

Respond to user FIRST, then resume autonomous work.

## State Reporting

When asked about state: `State: {IDLE/RUNNING} | Mode: {reader/assistant/autonomous} | Persona: {ON/OFF} | Loop: {ACTIVE/INACTIVE}`

Bash: `session-state-get.sh`, `session-mode-get.sh`, `session-persona-get.sh`, and `session-signal-exists.sh loop-active`.

## Knowledge Retrieval (MANDATORY)

When persona is active (`session-persona-get.sh` returns `true` or `unset`) and the user asks ANY question that could relate to learned knowledge, follow the retrieval escalation convention (`core/config/conventions/retrieval-escalation.md`):

1. **Tier 1**: Knowledge tree — `retrieve.sh` or read `_tree.yaml` + relevant node `.md` files
2. **Tier 2**: Codebase exploration — Grep/Glob/Read on the primary workspace (all modes)
3. **Tier 3**: Web search — WebSearch/WebFetch (assistant/autonomous only)

Stop at the first tier that provides sufficient knowledge. NEVER say "I don't have context" without attempting all eligible tiers. Full escalated retrieval runs in Step 4 of `/respond`.

## Routing

When the user sends a message: Read and follow `.claude/skills/respond/SKILL.md`
