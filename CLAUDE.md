# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

Domain-agnostic continual learning base agent. The system forms hypotheses, tracks their outcomes, builds memory of what worked and what failed, and self-evolves its reasoning capabilities over time. It serves as a reusable foundation for any domain where an autonomous agent needs to learn, reflect, and improve through experience.

## Architecture

This is a **Claude-native data repository** — no traditional source code or build tools. Configuration and state live in YAML, JSONL, and Markdown files that Claude reads, reasons over, and updates autonomously.

### Framework vs State Split (4-Tier Architecture)

- **`core/config/`** — Framework definitions and parameter bounds (immutable). Contains templates, thresholds, pipeline configs, `initial_state:` sections, and convention reference files in `core/config/conventions/`.
- **`core/scripts/`** — Framework infrastructure scripts. All JSONL stores accessed exclusively via these scripts — the LLM never reads/edits JSONL files directly.
- **`meta/`** — Agent-editable meta-strategies and domain-agnostic data (independent of domain data). Metacognitive self-modification layer inspired by HyperAgents. Also includes: `spark-questions.jsonl`, `skill-quality.yaml`, `skill-gaps.yaml`, `evolution-log.jsonl`, `reflection-templates.yaml`, `strategy-archive.yaml`, `config-overrides.yaml`, `config-changes.yaml`, `step-attribution.yaml`, `meta-knowledge/`.
- **`world/`** — Collective domain state shared across agents within a domain. Lives at an **external user-supplied path** (shared drive, NAS, etc.), configured in `<agent>/local-paths.conf`. Contains the knowledge tree, aspirations, pipeline, reasoning bank, guardrails, pattern signatures, message board, file history, changelog, conventions, sources, `program.md` (The Program — shared purpose), etc.
- **`<agent-name>/`** — Per-agent private state (e.g., `alpha/`). Contains session state, journal, experience traces, `self.md` (agent identity), curriculum, developmental stage, profile, infra health, and the agent's local aspiration queue.

**External paths**: `world/` and `meta/` live at user-supplied external paths configured per-agent in `<agent>/local-paths.conf` (gitignored). The local repo only contains `core/`, `.claude/`, and `<agent>/` directories. Each agent can point to different locations. See `core/config/conventions/external-paths.md` for details.

**Removing data**: Delete the relevant directory — `<agent>/` for one agent, the world directory for domain data, or the meta directory for improvement strategies. Each agent's `<agent>/local-paths.conf` stores its external world/ and meta/ paths. See `core/config/conventions/external-paths.md` for details.

**Project Structure**:
```
core/                # Shareable cognitive framework (copy to any project)
  config/            # Framework definitions (immutable)
    conventions/     # On-demand convention reference files
  scripts/           # Utility scripts (framework infrastructure)
meta/                # Agent-editable meta-strategies (independent of domain data)
  goal-selection-strategy.yaml, reflection-strategy.yaml  # Strategy files
  evolution-strategy.yaml, aspiration-generation-strategy.yaml
  encoding-strategy.yaml, improvement-instructions.md
  improvement-velocity.yaml                               # imp@k metrics
  meta-log.jsonl                                          # Strategy change audit (script-only)
  spark-questions.jsonl, skill-quality.yaml, skill-gaps.yaml
  evolution-log.jsonl, reflection-templates.yaml, strategy-archive.yaml
  config-overrides.yaml, config-changes.yaml, step-attribution.yaml
  meta-knowledge/    # Meta-knowledge index + entries
  experiments/       # A/B experiment tracking
  transfer/          # Cross-domain transfer bundles
world/               # Collective domain state (shared across agents, external path)
  program.md         # The Program — shared purpose
  aspirations.jsonl  # Central task list (world-level goals)
  pipeline.jsonl     # Shared hypothesis registry
  knowledge/tree/    # Collective knowledge tree
  reasoning-bank.jsonl, guardrails.jsonl, pattern-signatures.jsonl
  board/             # Message board channels (general, findings, coordination, decisions)
  .history/          # Self-contained file version history (copy-on-write snapshots)
  changelog.jsonl    # Auto-appended audit trail of all writes
  conventions/       # Domain-specific conventions
  forged-skills.yaml # Forged skills registry (shared across agents)
  skill-relations.yaml # Skill relationship graph (shared across agents)
  scripts/           # Domain-specific scripts (shared across agents)
<agent-name>/        # Per-agent private state (e.g., alpha/)
  self.md            # Agent identity and specialization
  aspirations.jsonl  # Agent's local work queue
  experience.jsonl   # Agent's raw interaction traces
  journal.jsonl      # Agent's activity log
  session/           # Ephemeral session state (working memory, handoff, signal files)
  curriculum.yaml    # Agent's progression
.claude/skills/      # Skill definitions
.claude/rules/       # Rule definitions
```

### Core Design Principle: No Terminal State

The system is a perpetual loop. Completion of one thing seeds the next. `/aspirations loop` is the heartbeat — it never exits, it always has work to create.

*(Full rules in `core/config/modes/autonomous.md`)*

### Core Design Principle: Consolidate Before Expand

Depth over breadth. Completion of existing work takes priority over starting new.
An aspiration 90% complete has more gravitational pull than a brand-new aspiration.
New directions require healthy existing completion rates (>25% average) or explicit
justification (user directive, critical blocker, all existing work blocked).

*(Full rules in `.claude/rules/consolidate-before-expand.md`)*

### Mode System

The framework has three operational modes. Mode is the single user-facing control — state and persona are derived automatically.

| Mode | State | Persona | Capabilities |
|------|-------|---------|-------------|
| `reader` (baseline) | IDLE | ON (light) | Read knowledge, prime, answer questions. No writes. Safe default. |
| `assistant` | IDLE | ON (full) | Reader + write to tree, remember things, research when asked, accept directives. No loop. |
| `autonomous` | RUNNING | ON (full) | Everything. Self-directed perpetual learning loop. |

Reader is the baseline — the safe floor. `/stop` always returns to reader. You must explicitly `/start --mode` to upgrade.

Mode-specific behavioral rules live in `core/config/modes/{mode}.md` — loaded on demand at session start.
Mode signal file: `<agent>/session/agent-mode` (plain text: reader, assistant, autonomous).
Scripts: `session-mode-get.sh`, `session-mode-set.sh` (only /start and /stop may write).

### Cognitive Primitives

Three goal types the agent can create anytime via `aspirations-add-goal.sh`:
- **Unblock** (`"Unblock: ..."`, HIGH) — created by CREATE_BLOCKER protocol when a problem can't be fixed inline
- **Investigate** (`"Investigate: ..."`, MEDIUM) — diagnostic, something seems off
- **Idea** (`"Idea: ..."`, MEDIUM) — creative insight, improvement opportunity

Not mutually exclusive. A single event can spawn all three. See `aspirations-execute/SKILL.md` Cognitive Primitives section.

### Core Systems

| System | Key Files |
|--------|-----------|
| The Program (shared purpose) | `world/program.md` |
| Self (agent identity) | `<agent>/self.md`, `.claude/rules/self.md`  |
| Aspirations engine | `world/aspirations.jsonl`, `<agent>/aspirations.jsonl`, `core/config/aspirations.yaml` |
| Hypothesis pipeline | `world/pipeline.jsonl` |
| Experience archive | `<agent>/experience.jsonl`, `<agent>/experience/` |
| Memory/Knowledge tree | `world/knowledge/tree/_tree.yaml` |
| Pattern signatures | `world/pattern-signatures.jsonl` |
| Reasoning bank | `world/reasoning-bank.jsonl` |
| Guardrails | `world/guardrails.jsonl` |
| Spark questions | `meta/spark-questions.jsonl` |
| Journal | `<agent>/journal.jsonl`, `<agent>/journal/` |
| Working memory | `<agent>/session/working-memory.yaml`, `core/scripts/wm-*.sh` |
| Session state | `<agent>/session/` |
| Agent mode | `<agent>/session/agent-mode`, `core/config/modes/` |
| Secrets store | `.env.example`, `.env.local` |
| Memory pipeline | `core/config/memory-pipeline.yaml` |
| Reflection engine | `/reflect` skill |
| Experiential index | `<agent>/experiential-index.yaml` |
| Curriculum | `<agent>/curriculum.yaml`, `core/config/curriculum.yaml` |
| Domain conventions | `world/conventions/*.md` |
| Meta-strategies | `meta/*.yaml`, `core/config/meta.yaml` |
| Skill relations | `core/config/skill-relations.yaml`, `world/skill-relations.yaml` |
| Skill quality | `meta/skill-quality.yaml`, `meta/skill-quality-strategy.yaml` |
| Message board | `world/board/*.jsonl`, `core/scripts/board.py` |
| File history | `world/.history/`, `meta/.history/`, `core/scripts/history.py` |
| Changelog | `world/changelog.jsonl`, `core/scripts/changelog.py` |
| Background jobs | `<agent>/session/background-jobs.yaml`, `core/scripts/background-jobs.sh` |
| External paths | `<agent>/local-paths.conf`, `core/scripts/_paths.sh`, `core/scripts/_paths.py` |
| File operations | `core/scripts/_fileops.py` (locking, history, changelog) |
| Team state | `world/team-state.yaml`, `core/scripts/team-state.py`, `team-state-update.sh`, `team-state-read.sh` |
| Execution diary | `<agent>/session/execution-diary.jsonl`, `core/scripts/execution-diary.sh` |
| Reasoning snapshot | `<agent>/session/reasoning-snapshot.yaml`, `core/scripts/reasoning-snapshot.sh` |
| Compact recovery | `<agent>/session/compact-checkpoint.yaml`, `core/scripts/compact-restore-slots.sh` |

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
| `session-state.md` | Agent state machine, session scripts, generic YAML store, background jobs tracker |
| `infrastructure.md` | Error response protocol, infra health, verify-before-assuming details, knowledge reconciliation details |
| `secrets.md` | Credentials convention, env-read.sh, security rules |
| `working-memory.md` | Working memory schema, wm-*.sh script API, slot_meta, pruning rules |
| `curriculum.md` | Curriculum YAML schema, script API, gate types, contract checks |
| `handoff-working-memory.md` | Handoff schema, working memory integration, blocker tracking, reasoning trajectory |
| `compact-recovery.md` | Full-fidelity compact recovery protocol, slot restoration, execution diary, reasoning snapshot |
| `meta-strategies.md` | Meta-strategy schemas, modification protocol, experiments, imp@k, transfer |
| `skill-quality.md` | Skill quality five-dimension evaluation, skill-evaluate.sh API, quality thresholds |
| `board.md` | Message board JSONL schema, script API, agent integration points, directive payload, execution feedback, insight triggers |
| `history.md` | File versioning `.history/` schema, script API, changelog, pruning |
| `external-paths.md` | External path configuration, `local-paths.conf` format, `/start` flow |
| `precision-encoding.md` | Precision manifest schema, extraction heuristics, Verified Values format |
| `agent-spawning.md` | Agent spawning context injection, build-agent-context.sh API, repo safety tiers, anti-patterns |
| `retrieval-escalation.md` | 3-tier retrieval escalation: tree → codebase → web search |
| `exhaustive-search-before-negation.md` | Exhaustive knowledge search protocol before negative conclusions |
| `coordination.md` | Multi-agent coordination: claim protocol, board types/tags, circuit breaker, review gate, dependency chains, self-abstention, directive protocol, team state protocol |
| `constitutional-rings.md` | Three-ring governance model: Ring 1 (immutable mission), Ring 2 (standards), Ring 3 (autonomous protocols) |

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
Everything in `world/` is collective domain state (shared across agents). Everything in `<agent>/` is per-agent private state. Everything in `meta/` is domain-agnostic improvement strategy. Everything in `core/` and `.claude/` is immutable framework.
The cognitive core (base skills, rules, `core/`) describes INTENT, never domain-specific
implementation. Domain knowledge lives in `world/`: conventions (`world/conventions/*.md`),
guardrails, reasoning bank, knowledge tree, forged skills (`world/forged-skills.yaml`). Agent-specific state lives in `<agent>/`: experience, journal, session.

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

### Self File Format and The Program

The shared purpose lives in `world/program.md` (The Program). Each agent's identity lives in `<agent>/self.md` (YAML front matter + markdown body). Schema and maintenance: `.claude/rules/self.md`.

### Skill Invocation Rules
- **Control skills** (/start, /stop, /open-questions): user-invocable only — Claude MUST NOT invoke these
- **Mode control**: `/start --mode <mode>` to enter a mode, `/stop` to return to reader baseline
- **Hybrid skills** (/agent-completion-report, /backlog-report, /priority-review, /verify-learning): user-invocable AND agent-callable
- **Internal skills**: `user-invocable: false` — invoked by agent during RUNNING state
- **No blocking on user input in RUNNING state** — skills must never wait for, request, or depend on user input during autonomous execution

### Code Change Verification (MANDATORY)
After ANY code change: read the project's CLAUDE.md, run tests, fix errors. Never declare ready until build passes.

### Knowledge Reconciliation
After any action that changes the world, check if knowledge tree nodes need updating. Detail: `core/config/conventions/infrastructure.md`.

### Tool Usage + Write Permissions

- Use `Write` only for NEW files. Use `Edit` for existing files.
- All JSONL stores accessed exclusively via scripts. See convention files for APIs.
- Working memory (`<agent>/session/working-memory.yaml`) accessed exclusively via `wm-*.sh` scripts. See `core/config/conventions/working-memory.md`.

| Path | Permission | Purpose |
|------|-----------|---------|
| `world/**` | Create, write, edit, delete | Collective domain state |
| `<agent>/**` | Create, write, edit, delete | Per-agent private state |
| `meta/**`          | Create, write, edit    | Agent-editable meta-strategies    |
| `.claude/skills/{new-name}/` | Create directory + SKILL.md | Forged skills via /forge-skill |

Everything else is **read-only**. Only the user may modify framework files.

**Mode-based capability gating**: Each skill has a `minimum_mode` front matter field (reader, assistant, autonomous). Skills check mode at entry and refuse if current mode is insufficient. See `core/config/modes/` for per-mode capabilities.

## Session Start Protocol

1. Bash: `session-state-get.sh` → read state
2. Branch on state (check state BEFORE loading mode — avoids contradictions):
   - **If NO_AGENT**: No agent bound. Suggest: `/start <agent-name>` to create/resume. DONE.
   - **If UNINITIALIZED**: Follow `.claude/rules/user-interaction.md` UNINITIALIZED protocol. DONE.
   - **If RUNNING**: Agent is in autonomous mode (another window or crashed session). If this is a new session (not an autocompact resume), suggest `/start <agent> --mode reader` for read-only access or `/start <agent> --mode assistant` for user-directed access. DONE — do not invoke boot or auto-resume.
   - **If IDLE**: Bash: `session-mode-get.sh` → read mode (default: `reader`). Read `core/config/modes/{mode}.md`. Invoke `/prime`, then ready for user.

### Agent-Session Binding

Each Claude Code session is bound to one agent via `AYOAI_AGENT` env var:
- `AYOAI_AGENT` — the ONLY mechanism for agent resolution in scripts
- `.active-agent-<session_id>` — maps session to agent name (used by hooks to set `AYOAI_AGENT`)
- `/start <name>` writes the binding file and sets the env var
- The LLM prefixes all Bash calls with `AYOAI_AGENT=<name>`
- Multiple terminals work independently — no shared state files.

## Knowledge Retrieval (All States)

When persona is active, the agent MUST consult its knowledge before answering domain questions.
Follow the retrieval escalation convention (`core/config/conventions/retrieval-escalation.md`):

1. **Tier 1 — Knowledge Tree**: `retrieve.sh --category {category} --depth medium` or intelligent retrieval protocol
2. **Tier 2 — Codebase Exploration**: Grep/Glob/Read on the primary workspace (from `<agent>/self.md`)
3. **Tier 3 — Web Search**: WebSearch/WebFetch (assistant/autonomous mode only)

Stop at the first tier that provides sufficient knowledge. Never say "I don't have context"
without attempting all eligible tiers.

## User Control Commands

| Command | Effect | Valid From |
|---------|--------|-----------|
| `/start <name>` | Create/resume agent in autonomous mode (default) | UNINITIALIZED, IDLE |
| `/start <name> --mode reader` | Create/resume agent in reader mode (read-only) | UNINITIALIZED, IDLE, RUNNING* |
| `/start <name> --mode assistant` | Create/resume agent in assistant mode (user-directed learning) | UNINITIALIZED, IDLE, RUNNING* |
| `/stop [agent-name]` | Consolidate → drop to reader mode → IDLE | RUNNING, IDLE |
| `/verify-learning` | Post-test verification | ANY |
| `/open-questions` | Show open questions | ANY |
| `/agent-completion-report` | Show what changed *(also agent-callable)* | ANY |
| `/backlog-report` | Sprint planning backlog *(also agent-callable)* | ANY |
| `/priority-review` | Priority dashboard — reorder aspirations *(also agent-callable)* | ANY |

\*When started from RUNNING state, reader/assistant create an **observer session** that coexists
with the autonomous loop. Observer sessions do not write to agent-state, agent-mode, or
persona-active. See `/start` RUNNING branch and `core/config/conventions/session-state.md`.

### Enforcement Rules

1. Claude MUST NOT invoke /start, /stop, or /open-questions.
2. Claude MUST NOT invoke boot or start the aspirations loop without RUNNING state and autonomous mode.
3. In reader mode: read-only assistant. May read state but MUST NOT execute write operations or workflow skills.
4. In assistant mode: user-directed assistant. May read and write when asked but MUST NOT self-initiate or run the loop.
5. In autonomous mode (RUNNING state): autonomous via aspirations loop.
6. Auto-resume after autocompact is handled by the stop hook (unconditional BLOCK + LOOP_CONTINUE), NOT by the Session Start Protocol. A new session that finds RUNNING state must show the error (or start an observer session if `--mode reader|assistant` is requested), not auto-resume.

### Autonomous Loop Rules

See `core/config/modes/autonomous.md` (loaded on demand in autonomous mode).

## Auto-Session Continuation

Session-keyed agent binding (project root):

| File | Purpose | Set By |
|------|---------|--------|
| `.active-agent-<session_id>` | Binds a Claude Code session to an agent | /start |

Signal files (all in `<agent>/session/`):

| File | Purpose | Set By |
|------|---------|--------|
| `agent-state` | "RUNNING" or "IDLE" | /start, /stop only |
| `agent-mode` | "reader", "assistant", or "autonomous" | /start, /stop |
| `persona-active` | "true" or "false" | /start, /stop, /boot |
| `stop-loop` | Allow exit (set after obligations complete) | /stop Phase -1.4 |
| `stop-requested` | Graceful stop signal (set immediately by /stop) | /stop |
| `iteration-checkpoint.json` | In-flight obligation tracker for graceful stop recovery | aspirations loop |
| `handoff.yaml` | Cross-session state | aspirations consolidation |
| `pending-agents.yaml` | Background agent tracking (stop hook Gate 2.5) | aspirations-execute Phase 4 |
| `background-jobs.yaml` | Long-running external process tracking | forged skills with background tasks |

Other session signals (`loop-active`, `compact-checkpoint.yaml`, `context-reads.txt`, `pending-questions.yaml`, `aspirations-compact.json`): see `core/config/conventions/session-state.md`.

### Compact Checkpoint Protocol

PreCompact/SessionStart hooks manage encoding state across autocompact. Detail: `core/config/conventions/session-state.md`.

### Context Read Deduplication

Hooks prevent redundant file reads AND skill invocations between compaction cycles.
`PreToolUse[Read]` gates file re-reads; `PreToolUse[Skill]` gates skill re-invocations
(combined gate+record since `PostToolUse` does not fire for the Skill tool).
Detail: `core/config/conventions/session-state.md`.

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
| Curriculum Gates | Evaluate graduation gates and promote curriculum stages |
| Respond | Handle user messages — persona, knowledge search, directive routing |
| Review Hypotheses | Resolve hypotheses, learn from outcomes, accuracy stats |
| Reflect | ABC chains, violations, hierarchical reflection, strategy extraction |
| *Reflect On Outcome* | *Hypothesis ABC chains, execution pattern signatures, batch micro-hypothesis processing* |
| *Reflect On Self* | *Pattern synthesis, strategy extraction, confidence calibration* |
| *Reflect Maintain* | *Memory curation, active forgetting, aspiration grooming* |
| *Reflect Tree Update* | *Shared tree update protocol (propagate upward)* |
| Replay | Compressed review, reconsolidation, domain transfer |
| Research Topic | Build knowledge base via web research |
| Decompose | Break compound goals into primitives |
| Forge Skill | Create new skills from capability gaps |
| Tree | Knowledge tree operations: read, find, add, edit, set, decompose, maintain, stats, validate |

*(Forged skills created via /forge-skill appear here after creation — see world/forged-skills.yaml)*
