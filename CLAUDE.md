# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

Domain-agnostic continual learning base agent. The system forms hypotheses, tracks their outcomes, builds memory of what worked and what failed, and self-evolves its reasoning capabilities over time. It serves as a reusable foundation for any domain where an autonomous agent needs to learn, reflect, and improve through experience.

## Architecture

This is a **Claude-native data repository** — no traditional source code or build tools. Configuration and state live in YAML, JSONL, and Markdown files that Claude reads, reasons over, and updates autonomously.

### Framework vs State Split

- **`core/config/`** — Framework definitions and parameter bounds (immutable). Contains templates, thresholds, pipeline configs, `initial_state:` sections, and convention reference files in `core/config/conventions/`.
- **`core/scripts/`** — Framework infrastructure scripts. All JSONL stores accessed exclusively via these scripts — the LLM never reads/edits JSONL files directly.
- **`mind/`** — All mutable agent data. Everything the agent creates, updates, or accumulates lives here.

**Factory reset**: `/reset` or `rm -rf mind/` — deletes all learned state. Also cleans forged skill directories via `mind/forged-skills.yaml`.

**Project Structure**:
```
core/                # Shareable cognitive framework (copy to any project)
  config/            # Framework definitions (immutable)
    conventions/     # On-demand convention reference files (15 topics)
  scripts/           # Utility scripts (framework infrastructure)
mind/                # All mutable agent state (rm -rf to reset)
  aspirations.jsonl, pipeline.jsonl, experience.jsonl  # JSONL stores (script-accessed only)
  reasoning-bank.jsonl, guardrails.jsonl, pattern-signatures.jsonl
  spark-questions.jsonl, journal.jsonl, experiential-index.yaml
  conventions/       # Domain-specific procedural conventions (loaded on demand)
  knowledge/         # Knowledge base, tree, patterns, beliefs
  experience/        # Full-fidelity interaction trace content files
  journal/           # Activity log content files
  session/           # Ephemeral session state (working memory, handoff, signal files)
  forged-skills.yaml # Registry of forged skill directories
.claude/skills/      # Skill definitions
.claude/rules/       # Rule definitions
```

### Core Design Principle: No Terminal State

The system is a perpetual loop. Completion of one thing seeds the next. `/aspirations loop` is the heartbeat — it never exits, it always has work to create.

### Cognitive Primitives

Three goal types the agent can create anytime via `aspirations-add-goal.sh`:
- **Unblock** (`"Unblock: ..."`, HIGH) — created by CREATE_BLOCKER protocol when a problem can't be fixed inline
- **Investigate** (`"Investigate: ..."`, MEDIUM) — diagnostic, something seems off
- **Idea** (`"Idea: ..."`, MEDIUM) — creative insight, improvement opportunity

Not mutually exclusive. A single event can spawn all three. See `aspirations-execute/SKILL.md` Cognitive Primitives section.

### Core Systems

| System | Key Files |
|--------|-----------|
| Self (The Program) | `mind/self.md`, `.claude/rules/self.md`  |
| Aspirations engine | `mind/aspirations.jsonl`, `core/config/aspirations.yaml` |
| Hypothesis pipeline | `mind/pipeline.jsonl` |
| Experience archive | `mind/experience.jsonl`, `mind/experience/` |
| Memory/Knowledge tree | `mind/knowledge/tree/_tree.yaml` |
| Pattern signatures | `mind/pattern-signatures.jsonl` |
| Reasoning bank | `mind/reasoning-bank.jsonl` |
| Guardrails | `mind/guardrails.jsonl` |
| Spark questions | `mind/spark-questions.jsonl` |
| Journal | `mind/journal.jsonl`, `mind/journal/` |
| Working memory | `mind/session/working-memory.yaml`, `core/scripts/wm-*.sh` |
| Session state | `mind/session/` |
| Secrets store | `.env.example`, `.env.local` |
| Memory pipeline | `core/config/memory-pipeline.yaml` |
| Reflection engine | `/reflect` skill |
| Experiential index | `mind/experiential-index.yaml` |
| Domain conventions | `mind/conventions/*.md` |

## Convention Index

When you need schema, script API, or protocol details for a subsystem, read the relevant file from `core/config/conventions/`:

| File | Topics |
|------|--------|
| `aspirations.md` | Aspiration JSONL schema, script API, archival rules |
| `pipeline.md` | Pipeline JSONL schema, script API, atomic resolve |
| `experience.md` | Experience archive JSONL schema, script API |
| `reasoning-guardrails.md` | Reasoning bank + guardrails JSONL, guardrail-check script |
| `pattern-signatures.md` | Pattern signatures JSONL schema, script API |
| `spark-questions.md` | Spark questions JSONL schema, script API |
| `journal.md` | Journal index JSONL schema, script API |
| `tree-retrieval.md` | Unified retrieval, tree scripts, category suggestion |
| `goal-schemas.md` | Goal verification, recurring/deferred fields, goal scoring |
| `goal-selection.md` | Mandatory goal-selector.sh, post-compaction fabrication guard |
| `session-state.md` | Agent state machine, session scripts, generic YAML store |
| `infrastructure.md` | Error response protocol, infra health, verify-before-assuming details, knowledge reconciliation details |
| `secrets.md` | Credentials convention, env-read.sh, security rules |
| `working-memory.md` | Working memory schema, wm-*.sh script API, slot_meta, pruning rules |
| `handoff-working-memory.md` | Handoff schema, working memory integration, blocker tracking |

Additional on-demand specs (not convention files):
- `core/config/hypothesis-conventions.md` — Hypothesis record schemas, horizons, context manifests
- `core/config/knowledge-conventions.md` — Knowledge articles, memory tree, entity cross-links
- `core/config/architecture-reference.md` — Skill chaining map, self-evolution loop
- `core/config/verification-checklist.md` — Post-test verification checklist (framework)
- `core/config/verification-checklist-domain-specific.md` — Foundational domain verification checks (read directly by /verify-learning)
- `core/config/status-output.md` — Status line formats for RUNNING state

## Universal Conventions

### File Formats
- **YAML** (`.yaml`) for structured data: config, indexes
- **JSONL** (`.jsonl`) for lifecycle records: aspirations, pipeline, experiences, reasoning bank, guardrails, pattern signatures, spark questions, journal index
- **JSON** (`.json`) for metadata: aspirations-meta, pipeline-meta, experience-meta
- **Markdown** (`.md`) with YAML front matter for knowledge articles and journal entries

### Domain-Free Cognitive Core
Everything in `mind/` is domain-specific. Everything outside `mind/` is domain-agnostic.
The cognitive core (base skills, rules, `core/`) describes INTENT, never domain-specific
implementation. Domain knowledge lives exclusively in `mind/`: conventions (`mind/conventions/*.md`),
guardrails, reasoning bank, knowledge tree, and forged skills (registered in `mind/forged-skills.yaml`).

### Naming Rules
- All filenames: **lowercase, kebab-case** (hyphens, no spaces, no underscores except pipeline/experience record IDs)
- ISO 8601 dates everywhere. Timestamps: ALWAYS local system time (never UTC). Use `$(date +%Y-%m-%dT%H:%M:%S)`.

### ID Formats
- Aspirations: `asp-NNN` | Goals: `g-NNN-NN` | Prep tasks: `pt-NNN`
- Guardrails: `guard-NNN` | Reasoning bank: `rb-NNN` | Beliefs: `bel-NNN`
- Transitions: `trans-NNN` | Spark questions: `sq-NNN`, candidates: `sq-cNN`
- Pattern signatures: `sig-NNN` | Strategy archive: `sa-NNN`
- Experiences: `exp-{source-id-or-slug}` | Pipeline: `YYYY-MM-DD_slug`

### Priority Values
- `HIGH`, `MEDIUM`, `LOW` (uppercase)

### Status Values

Goals: `pending`, `in-progress`, `completed`, `blocked`, `skipped`, `expired` | Pipeline: `discovered`, `evaluating`, `active`, `resolved`, `archived` | Aspirations: `active`, `completed`, `paused`, `retired`. Full per-entity status lists: see convention files.

### Pipeline Rules
- **Never delete** pipeline records — move via `pipeline-move.sh`
- Journal entries are **append-only**
- Hypothesis horizons: `micro`, `session`, `short`, `long`
- Hypothesis types: `high-conviction`, `calibration`, `exploration`, `contrarian`

### Self File Format

The agent's core purpose lives in `mind/self.md` (YAML front matter + markdown body). Schema and maintenance: `.claude/rules/self.md`.

### Skill Invocation Rules
- **Control skills** (/start, /stop, /reset, /escapePersona, /enterPersona, /verify-learning, /open-questions): user-invocable only — Claude MUST NOT invoke these
- **Hybrid skills** (/completion-report, /backlog-report): user-invocable AND agent-callable
- **Internal skills**: `user-invocable: false` — invoked by agent during RUNNING state
- **No blocking on user input in RUNNING state** — skills must never wait for, request, or depend on user input during autonomous execution

### Code Change Verification (MANDATORY)
After ANY code change: read the project's CLAUDE.md, run tests, fix errors. Never declare ready until build passes.

### Knowledge Reconciliation
After any action that changes the world, check if knowledge tree nodes need updating. Detail: `core/config/conventions/infrastructure.md`.

### Tool Usage + Write Permissions

- Use `Write` only for NEW files. Use `Edit` for existing files.
- All JSONL stores accessed exclusively via scripts. See convention files for APIs.
- Working memory (`mind/session/working-memory.yaml`) accessed exclusively via `wm-*.sh` scripts. See `core/config/conventions/working-memory.md`.

| Path | Permission | Purpose |
|------|-----------|---------|
| `mind/**` | Create, write, edit, delete | All mutable agent state |
| `.claude/skills/{new-name}/` | Create directory + SKILL.md | Forged skills via /forge-skill |

Everything else is **read-only**. Only the user may modify framework files.

## Session Start Protocol

1. Bash: `session-state-get.sh` → read output
2. **If RUNNING**: Invoke the boot skill.
3. **If IDLE**: Follow `.claude/rules/user-interaction.md` IDLE protocol.
4. **If UNINITIALIZED**: Follow `.claude/rules/user-interaction.md` UNINITIALIZED protocol.

## Knowledge Tree Retrieval (All States)

When persona is active, the agent MUST consult its knowledge tree (`mind/knowledge/tree/_tree.yaml`) before answering domain questions.

Minimum retrieval: read `_tree.yaml`, identify relevant nodes, read their `.md` files.
Full retrieval: `Bash: retrieve.sh --category {category} --depth medium`.

Never say "I don't have context" without first checking the tree.

## User Control Commands

Seven user-only commands plus `/completion-report` and `/backlog-report` (also agent-callable). Claude MUST NEVER invoke the seven user-only commands:

| Command | Effect | Valid From |
|---------|--------|-----------|
| `/start` | Begin or resume autonomous loop | UNINITIALIZED, IDLE |
| `/stop` | Gracefully stop the loop | RUNNING |
| `/reset` | Wipe all state | ANY |
| `/escapePersona` | Disable agent persona | ANY |
| `/enterPersona` | Re-enable agent persona | ANY |
| `/verify-learning` | Post-test verification | ANY |
| `/open-questions` | Show open questions | ANY |
| `/completion-report` | Show what changed *(also agent-callable)* | ANY |
| `/backlog-report` | Sprint planning backlog as markdown *(also agent-callable)* | ANY |

### Enforcement Rules

1. Claude MUST NOT invoke /start, /stop, /reset, /escapePersona, /enterPersona, /verify-learning, or /open-questions.
2. Claude MUST NOT invoke boot or start the aspirations loop without RUNNING state.
3. In IDLE state: normal assistant. May read state but MUST NOT execute workflow skills.
4. In RUNNING state: autonomous via aspirations loop.
5. Auto-resume: If session starts and agent-state is RUNNING, Claude auto-resumes.

### Autonomous Loop Rules

- Claude MUST NEVER ask the user a question during RUNNING state — not by any means.
- If genuinely stuck: write to `mind/session/pending-questions.yaml` with `default_action`, EXECUTE it, continue.
- **Decision authority**: Manager, not intern. Make decisions and continue. Log significant calls to pending-questions as "I decided X because Y — override if you disagree."
- **NEVER STOP for context concerns.** Autocompact handles context. The loop runs until `/stop`.
- **NEVER defer or skip goals because of token cost or perceived expense.**
- **NEVER circumvent the stop hook.**

### Tool Access During RUNNING State

Full access to: Bash, Read, Write, Edit, Glob, Grep, WebSearch, WebFetch, Agent, TeamCreate, SendMessage.

Read anywhere, write only within `mind/` (exception: forged skills in `.claude/skills/`).
MUST NOT modify existing base skill files, `_triggers.yaml`, `.claude/rules/`, `core/`, `CLAUDE.md`.

## Auto-Session Continuation

Signal files (all in `mind/session/`):

| File | Purpose | Set By |
|------|---------|--------|
| `agent-state` | "RUNNING" or "IDLE" | /start, /stop only |
| `persona-active` | "true" or "false" | /escapePersona, /enterPersona, /boot |
| `stop-loop` | Allow exit | /stop, /recover |
| `handoff.yaml` | Cross-session state | aspirations consolidation |
| `pending-agents.yaml` | Background agent tracking (stop hook Gate 2.5) | aspirations-execute Phase 4 |

Other session signals (`loop-active`, `stop-block-count`, `compact-checkpoint.yaml`, `context-reads.txt`, `pending-questions.yaml`, `aspirations-compact.json`): see `core/config/conventions/session-state.md`.

### Compact Checkpoint Protocol

PreCompact/SessionStart hooks manage encoding state across autocompact. Detail: `core/config/conventions/session-state.md`.

### Context Read Deduplication

Hooks prevent redundant file reads between compaction cycles. Detail: `core/config/conventions/session-state.md`.

## Available Skills

User control commands: see User Control Commands table above.

### Internal Skills (agent-only — invoked autonomously during RUNNING state)

| Skill | Purpose |
|-------|---------|
| Boot | Session entry point: status report + prime + handoff to aspirations loop |
| Prime | Context priming — load knowledge, guardrails, reasoning |
| Aspirations | Perpetual goal loop — the heartbeat (orchestrator, includes Phase 7.5 Completion Review) |
| *Aspirations Execute* | *Phase 4 goal execution, retrieval, verification, reconciliation* |
| *Aspirations Spark* | *Spark checks, sq-XXX handlers, immediate learning* |
| *Aspirations State Update* | *State update protocol with tree encoding + Step 8.5 Actionable Findings Gate* |
| *Aspirations Consolidate* | *Session-end consolidation, encoding, handoff* |
| *Aspirations Evolve* | *Evolution engine, developmental stage, config tuning* |
| Create Aspiration | Self-driven aspiration creation |
| Respond | Handle user messages — persona, knowledge search, directive routing |
| Review Hypotheses | Resolve hypotheses, learn from outcomes, accuracy stats |
| Reflect | ABC chains, violations, hierarchical reflection, strategy extraction |
| *Reflect Hypothesis* | *Full single hypothesis reflection pipeline* |
| *Reflect Execution* | *Pattern signatures + contradiction detection + investigation goals from execution outcomes* |
| *Reflect Batch Micro* | *Batch micro-hypothesis reflection* |
| *Reflect Extract Patterns* | *Pattern synthesis and strategy extraction* |
| *Reflect Calibration* | *Confidence calibration check* |
| *Reflect Curate Memory* | *Memory curation and active forgetting* |
| *Reflect Curate Aspirations* | *Aspiration grooming — stuck goal detection, evidence cross-reference* |
| *Reflect Tree Update* | *Shared tree update protocol (propagate upward)* |
| Replay | Compressed review, reconsolidation, domain transfer |
| Research Topic | Build knowledge base via web research |
| Decompose | Break compound goals into primitives |
| Forge Skill | Create new skills from capability gaps |
| Recover | Last-resort recovery |
| Tree | Knowledge tree operations: read, find, add, edit, set, decompose, maintain, stats, validate |

*(Forged skills created via /forge-skill appear here after creation — see mind/forged-skills.yaml)*
