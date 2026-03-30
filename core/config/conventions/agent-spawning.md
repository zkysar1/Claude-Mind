# Agent Spawning Convention

When spawning background or team agents, the host MUST inject primed context
directly into the agent's prompt using `build-agent-context.sh`. Spawned agents
receive context as data — they do NOT invoke `/prime` or any other skill.

## Script API

| Script | Purpose | Stdin |
|--------|---------|-------|
| `build-agent-context.sh --category <cat>` | Build context block for agent prompt | — |
| `build-agent-context.sh --category <cat> --repo <path>` | Include repo CLAUDE.md safety context | — |
| `build-agent-context.sh --category <cat> --role executor` | Include operation-tagged guardrails | — |
| `build-agent-context.sh --category <cat> --max-tokens <N>` | Limit output size (default 4000) | — |

**Arguments:**

| Argument | Required | Default | Purpose |
|----------|----------|---------|---------|
| `--category <cat>` | Yes | — | Target category for filtering. Comma-separated for multi-category. |
| `--repo <path>` | No | — | Path to target repo. Reads `<repo>/CLAUDE.md` for safety tier, test commands. |
| `--role <role>` | No | `researcher` | `researcher` = read-only. `executor` = can write/commit/push. |
| `--max-tokens <N>` | No | `4000` | Approximate output token budget. Truncates least-important sections first. |

**Output:** Plain text to stdout. Self-contained context block with section headers.
Read-only — no retrieval counter increments, no state file modifications.

## Mandatory Spawning Pattern

All agent spawning MUST follow this 3-step pattern:

```
# 1. Build context (host-side, before spawning)
Bash: agent_context=$(build-agent-context.sh --category "{cat}" [--repo "{path}"] --role {role})

# 2. Register agent (crash-safe: before dispatch so staleness timeout cleans up if dispatch fails)
Bash: pending-agents.sh register --id "{id}" --team "{team}" --goal "{goal_id}" --purpose "{desc}" --timeout 10

# 3. Spawn with context as prompt data
Agent(team_name="{team}", name="{id}",
      prompt="{agent_context}

              YOUR TASK:
              {task_description}

              TIME LIMIT: You have a maximum of 10 minutes. Wrap up and
              report your findings before then — do not start new work
              after 8 minutes of elapsed time.

              CONSTRAINTS:
              {role-specific constraints}",
      run_in_background=true)
```

## Role Constraints

### Researcher (default)
- READ-ONLY: Cannot write files, invoke skills, or call state-mutating scripts
- Receives category-filtered guardrails and reasoning bank entries
- Does NOT receive operation-tagged guardrails (commit, push, etc.)
- Reports findings via structured summary message

### Executor
- Can write, commit, and push
- Receives category-filtered guardrails PLUS all operation-tagged guardrails
  (tags: `commit`, `push`, `git`, `ci-cd`, `deployment`, `staging`)
- When `--repo` is provided and safety tier >= 4: receives explicit
  "Do NOT add CI/CD workflows" constraint
- MUST receive `--repo` when working on a specific external repo

## When `--repo` is Required

- Any goal that creates, modifies, or deletes files in an external repo
- Any goal involving commit, push, CI/CD, or deployment
- NOT required for pure knowledge/research tasks or framework-only work

## Anti-Patterns

- **NEVER** include `"invoke /prime"` or `"First, invoke /prime"` in agent prompts.
  `/prime` is a heavyweight multi-phase protocol designed for the host agent's
  full session context window, not for sub-agents.
- **NEVER** spawn executor agents without `--repo` when the task targets a specific repo.
  The CI-on-Tier-4 incident happened because the agent had zero repo context.
- **NEVER** spawn agents without context. Bare `Agent()` calls without
  `build-agent-context.sh` start with zero domain knowledge and zero guardrails.
- **NEVER** use generic prompts like "fix and push" or "ship changes."
  Always include repo constraints and role-specific guardrails.

## Token Budget Guidance

| Role | Recommended Budget | Rationale |
|------|-------------------|-----------|
| Researcher | 3000 | Orientation context, room for research findings |
| Executor | 4000 (default) | Needs full constraints + repo context |
| Multi-category | 5000 | Multiple domains increase context needs |

Truncation priority (least-important cut first):
1. Knowledge tree nodes (cut from bottom)
2. Reasoning bank entries (cut from bottom)
3. Guardrails (never cut — safety-critical)
4. Repo context (never cut — safety-critical)
5. Identity and Program (never cut)
