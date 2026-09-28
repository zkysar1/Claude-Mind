# Architecture Reference

Detailed architectural documentation read on demand by skills. Contains the skill chaining map, self-evolution loop, hippocampal learning framework, and memory taxonomy.

## Skill Chaining Map

```
/start (USER ENTRY POINT — user runs this to authorize the agent)
  ├── Sets agent-state to RUNNING
  └── Invokes /boot

/boot (AGENT ENTRY POINT — requires RUNNING agent-state)
  ├── Phase -3: Agent state gate check
  ├── Phase -2: State initialization (first boot only)
  ├── /review-hypotheses --resolve (catch-up: detect outcomes — NO learning)
  ├── Report generation (dashboard, alerts, readiness)
  └── HANDOFF → /aspirations loop (perpetual heartbeat takes over)

/aspirations loop (HEARTBEAT — runs forever, orchestrator ~900 lines)
  ├── /aspirations-execute (sub-skill: Phase 4 goal execution)
  │     ├── intelligent retrieval, context loading, memory deliberation
  │     ├── goal execution via linked skill
  │     ├── fail-fast cascade (Phase 4.1), domain post-execution steps (4.2)
  │     ├── experience archival (4.25)
  │     └── knowledge reconciliation (4.5), batch execution
  ├── /aspirations-spark (sub-skill: Phase 6 spark + Phase 6.5 immediate learning)
  │     ├── adaptive spark questions protocol
  │     ├── handlers: sq-009, sq-012, sq-c05, sq-c03, sq-c04, sq-013, sq-007
  │     ├── aspiration-level spark
  │     └── immediate learning: reasoning bank, guardrails, forge awareness
  ├── /aspirations-state-update (sub-skill: Phase 8 state update protocol)
  │     ├── 9 mandatory steps including CRITICAL Step 8: tree encoding
  │     └── capability propagation up parent chain
  ├── /aspirations-consolidate (sub-skill: session-end consolidation)
  │     ├── micro-sweep, encoding queue, dynamic budget, overflow management
  │     ├── knowledge debt sweep, tree rebalancing, skill health
  │     ├── user goal recap, continuation handoff
  │     └── restart via /boot
  ├── /aspirations-evolve (sub-skill: evolution engine)
  │     ├── developmental stage assessment, config parameter tuning
  │     ├── gap analysis, novelty filter, cap enforcement
  │     ├── pattern signature calibration, strategy archive
  │     └── forge check with integrity audit
  ├── /decompose (break compound goals into primitives)
  ├── /research-topic → web research, writes findings to tree nodes
  ├── /review-hypotheses --resolve → detects outcomes, moves active→resolved
  │     └── does NOT call /reflect (clean separation)
  ├── /review-hypotheses --learn → learns from resolved hypotheses (reflected: false)
  │     └── calls → /reflect --on-hypothesis (for each unlearned resolution)
  ├── /review-hypotheses --full-cycle → --resolve + --learn + --accuracy-report + /reflect --full-cycle
  │     └── calls → /replay (hippocampal replay during full-cycle)
  ├── /reflect (ROUTER — dispatches to mode sub-skills)
  │     ├── /reflect-on-outcome → hypothesis ABC chains, execution patterns, batch micro (Steps 0.5-9)
  │     │     └── calls /reflect-tree-update for tree propagation
  │     ├── /reflect-on-self → pattern synthesis, strategy extraction, calibration (Steps 1-5)
  │     │     └── calls /reflect-tree-update for tree propagation
  │     ├── /reflect-maintain → memory curation, aspiration grooming
  │     ├── /reflect-tree-update → shared tree update protocol (Steps 1-4)
  │     ├── updates → world/pattern-signatures.jsonl (DG/CA3 learning, via script)
  │     ├── updates → world/knowledge/beliefs.yaml (belief confidence)
  │     ├── updates → world/knowledge/transitions.yaml (contradictions)
  │     ├── updates → world/reasoning-bank.jsonl + world/guardrails.jsonl (via script)
  │     ├── updates → world/knowledge/meta/step-attribution.yaml (step labels)
  │     └── feeds → /aspirations evolve (strategy changes)
  ├── /replay → compressed review, reconsolidation, domain transfer
  │     ├── updates → world/pattern-signatures.jsonl (outcome stats, via script)
  │     ├── updates → tree node articles (reconsolidation)
  │     └── calls → /research-topic (domain transfer research)
  ├── /forge-skill → meta-skill: creates new skills from capability gaps
  │     ├── creates skill SKILL.md files
  │     ├── updates .claude/skills/_tree.yaml, _triggers.yaml
  │     └── creates test aspiration goals
  └── /tree → knowledge tree operations (read, find, add, edit, set, decompose, maintain, stats, validate)
        ├── SPLIT: article_count > 3 → cluster into subtopics
        ├── SPROUT: new content, no matching node → new branch
        ├── MERGE: low-content nodes → absorb into sibling
        └── PRUNE: empty nodes → archive
```

## Self-Evolution Loop

The system tracks its own accuracy over time, identifies patterns in what it reasons well vs. poorly about, and proposes new aspirations or strategy adjustments based on observed performance. Key mechanisms:

- **Spark checks** after every goal: micro-evolution that generates new goals/aspirations from discoveries
- **Aspiration-level sparks**: when an aspiration completes, always generate a replacement (nature abhors a vacuum)
- **Gap analysis**: before creating aspirations, verify genuine unmet need (prevents sprawl)
- **Novelty filter**: prefer novel hypotheses and aspirations over repetitive ones
- **Three-level reflection**: episode → pattern → strategic (Park et al. Generative Agents architecture)
- **Hypothesis testing**: after 20+ resolved hypotheses, statistically test what drives accuracy
- **Meta-memory**: explicit self-model of strengths, weaknesses, blind spots, and calibration bias
- **Capability gap detection**: recurring gaps logged to `agents/<agent>/skill-gaps.yaml` → `/forge-skill` creates new skills when threshold met (times_encountered >= 2, value >= medium, type-dependent gate: CALIBRATE+ for utility gaps, EXPLOIT+ for analytical gaps)
- **Performance-based evolution triggers**: Responsive triggers — accuracy drops, consecutive failures, pattern divergence, capability unlocks (`core/config/evolution-triggers.yaml`)
- **Adaptive spark questions**: Track question productivity (yield rate) and retire low-yield questions, promote candidates (`core/config/spark-questions.yaml`)
- **Strategy archive**: When strategies change, old versions are preserved with performance data in `meta/strategy-archive.yaml`
- **Step-level attribution**: Label evaluation steps as GOOD/BAD/NEUTRAL during reflection to identify chronically weak steps (`world/knowledge/meta/step-attribution.yaml`)
- **Preventive guardrails**: Failure-extracted "check this before acting" rules loaded during evaluation (`world/guardrails.jsonl`, script-accessed via `guardrails-read.sh`)
- **Belief registry**: Formally separates beliefs from facts, tracks confidence trajectories, detects contradictions (`world/knowledge/beliefs.yaml`, `world/knowledge/transitions.yaml`)
- **Horizon-gated hypotheses**: `micro` (seconds, in working memory), `session` (hours, lightweight pipeline), `short` (days, standard), `long` (weeks+, full suite). Overhead scales with time horizon.
- **Self-regulated effort**: Per-goal metacognitive assessment determines effort_level (`full`, `standard`, `skip`). Controls execution thoroughness and spark depth, not retrieval (retrieval is always intelligent and full). Optional user focus directive (`agents/<agent>/profile.yaml` `focus` field) steers value assessment.

The aspiration engine's evolution log (`meta/evolution-log.jsonl`) tracks when and why strategies change.

## Hippocampal Learning Framework

Biologically-inspired learning mechanisms layered on top of the Memory Tree. Based on hippocampal memory mechanics and Piaget's cognitive development theory.

### Memory Pipeline (encoding stages)
Observations flow through: **sensory buffer** (raw, ephemeral) → **working memory** (10 typed slots, session-scoped, including micro-hypothesis batch) → **encoding gate** (filters by novelty/surprise/impact/goal relevance, threshold 0.40) → **consolidation** (session-end replay, dynamic budget (5-15 items, scaled by violations/domains/surprise) compressed to leaf node articles) → **long-term tree** (dynamic random tree, K=4 MAX, D_max=20). Micro-hypotheses are batch-processed at session-end: only surprises (>= 7) enter the encoding gate. Config: `core/config/memory-pipeline.yaml`. Session state: `agents/<agent>/session/working-memory.yaml`.

### Pattern Separation & Completion (dentate gyrus + CA3)
Pattern signatures (`world/pattern-signatures.jsonl`, script-accessed via `pattern-signatures-read.sh`) enable two operations: **separation** ("this looks like pattern X but key feature differs so it's actually pattern Y") and **completion** ("partial cue matches → retrieve full strategy context"). Used during hypothesis evaluation to route between System 1 (fast/intuitive) and System 2 (slow/deliberate) reasoning.

### Hippocampal Replay & Reconsolidation
`/replay` skill runs compressed (3-line) reviews of resolved hypotheses, prioritizing violations, surprises, and high-impact outcomes. During replay, recalled strategies enter a **reconsolidation window** — they become temporarily updatable based on new evidence (reinforced, flagged for revision, or extended with new conditions). Includes domain transfer: patterns from strong domains (MASTER level) are abstracted and tested for applicability in weak domains (CALIBRATE level).

### Developmental Stages (Competence-Based)
System maturity tracked in `agents/<agent>/developmental-stage.yaml` (framework: `core/config/developmental-stage.yaml`). Stage labels derived from average domain competence: **exploring** (avg < 0.30) → **developing** (0.30-0.55) → **applying** (0.55-0.80) → **mastering** (> 0.80). Exploration budget computed dynamically: `max(0.15, min(0.85, 1.0 - average_domain_competence))`. Per-domain capability_level gates hypothesis types and System 1/2 routing. `resolved_hypotheses` count is tracked as a diagnostic metric but does not gate progression. Schema operations (assimilation vs accommodation) are logged explicitly.

### Active Forgetting
Enhanced decay model: `retention = e^(-days / (lambda * importance * type_decay))`. Retrieval strengthens memories (resets decay timer). Interference between contradictory articles is detected and prioritized for resolution. Active pruning during `/aspirations evolve` archives validated but stale knowledge.

## Memory Taxonomy

Maps the filesystem structure to memory types (inspired by "Everything is Context" framework):

| Memory Type | Location | Access Pattern | Update Trigger |
|---|---|---|---|
| Scratchpad | `agents/<agent>/session/working-memory.yaml` | Session-scoped R/W, consolidated at end | Every goal execution |
| Micro-predictions | `agents/<agent>/session/working-memory.yaml` → `micro_hypotheses` | Inline R/W within session, batch-reflected at end | Inline during goal execution |
| Episodic | `agents/<agent>/journal/` + `agents/<agent>/journal.jsonl` | Append-only, indexed by session/date/tags via `journal-read.sh` | State Update Protocol Step 7 |
| Fact | `world/knowledge/tree/` node articles (any depth) | Tree navigation via `_tree.yaml`, write via consolidation | Reflection, research, consolidation |
| Experiential | `pipeline-read.sh --stage resolved` AND `--stage archived` (BOTH — `resolved` alone is a survivorship filter holding ~10% of the store; g-115-4866) + `agents/<agent>/experiential-index.yaml` | Pattern matching by category/violation cause | `/reflect` |
| Procedural | `.claude/skills/` + `agents/<agent>/skill-gaps.yaml` | Read at invocation, forged via `/forge-skill` | Capability gap detection |
| User | `core/config/profile.yaml`, `CLAUDE.md` | Read at session start (auto-loaded) | Manual or evolution |
| Historical | `agents/<agent>/journal/` + `meta/evolution-log.jsonl` + `world/pipeline.jsonl` | Immutable audit trail | Append-only |
| Signatures | `world/pattern-signatures.jsonl` | DG separation + CA3 completion via `pattern-signatures-read.sh` | `/reflect`, `/replay` |
| Reasoning | `world/reasoning-bank.jsonl` | Category + tag matching via `reasoning-bank-read.sh` | `/reflect` differentiated extraction |
| Guardrails | `world/guardrails.jsonl` | Category filter via `guardrails-read.sh` | `/reflect` failure extraction |
| Beliefs | `world/knowledge/beliefs.yaml` | Entity + category matching | `/reflect` reinforce/weaken/contradict |
| Transitions | `world/knowledge/transitions.yaml` | Contradiction detection | `/reflect` interference detection |
| Attribution | `world/knowledge/meta/step-attribution.yaml` | Step name lookup | `/reflect` step labeling |

**Context traceability**: Every `short`/`long` horizon hypothesis record includes a `context_consulted` manifest listing which tree nodes, pattern signatures, data sources, and articles were loaded during evaluation. `/reflect` compares this against available resources to detect context gaps. `session` horizon records may optionally include this. `micro` horizon predictions do not use context manifests.

## Session End Protocol

In addition to standard working memory consolidation (see `/aspirations` Session-End Consolidation Pass):

0. **Micro-Hypothesis Sweep**: Invoke `/reflect --batch-micro` to process any micro-predictions from working memory.
1. **Aspiration Archive Sweep**: Run `aspirations-archive.sh` to move completed/retired aspirations from live to archive JSONL.
2. **Tree Rebalancing**: Invoke `/tree maintain` to check for DECOMPOSE, REDISTRIBUTE, SPLIT, SPROUT, MERGE, PRUNE opportunities.
3. **Skill Gap Review**: Read `agents/<agent>/skill-gaps.yaml` and evaluate if any gaps meet forge criteria.
4. **Skill Health Report**: Review active/forged/underperforming skills and flag any for retirement (3+ underperformance events).
5. **Write Continuation Handoff**: Write `agents/<agent>/session/handoff.yaml` with session state snapshot.
6. **Preserve critical control signals**: Session-end consolidation MUST NOT modify:
   - `agents/<agent>/session/agent-state` (only /start and /stop may change it)
   - `agents/<agent>/session/persona-active` (only /start and /stop may change it)

## Focus Directive

- `focus` in `agents/<agent>/profile.yaml`: `null` (self-regulate) or natural language string
- User sets via conversation: "focus on coding", "explore everything", "go back to normal"
- Agent uses focus as context for per-goal metacognitive effort assessment
- Agent MUST NOT set focus — it is a user preference
- When null: agent self-regulates based on goal metadata alone
- When set: agent biases effort assessment toward focus-aligned goals

## Config Override Conventions

- Config files define parameter bounds in `modifiable:` sections (immutable)
- Active overrides live in `meta/config-overrides.yaml`
- Skills resolve: `meta/config-overrides.yaml[param] ?? core/config/[file][param]`
- All overrides must fall within `[min, max]` from modifiable section
- Every change logged to `meta/config-changes.yaml` (append-only audit)
- Override format: `{param}: {value: N, previous: N, changed_date: "YYYY-MM-DD"}`
- Change log format: `{param, config_file, old_value, new_value, reason, date, session, triggered_by}`
- Overrides can be reverted by removing the entry from config-overrides.yaml

## Skill Forging Conventions

### Gap Registry
- Capability gaps tracked in `agents/<agent>/skill-gaps.yaml` (forge criteria in `core/config/skill-gaps.yaml`)
- Gap IDs: `gap-NNN` (zero-padded 3-digit)
- Gap statuses: `registered`, `under-review`, `forging`, `forged`, `dismissed`
- Forge criteria: times_encountered >= 2, value >= medium, distinct, type-dependent gate (utility: CALIBRATE+, analytical: EXPLOIT+)
- Gap types: `utility` (well-defined procedures, CALIBRATE+ gate), `analytical` (domain judgment, EXPLOIT+ gate)

### Forged Skill Lifecycle
- Created by `/forge-skill skill <gap-id>`
- Registered in `world/forged-skills.yaml`, `.claude/skills/_tree.yaml`, and `.claude/skills/_triggers.yaml`
- Companion scripts live in `world/scripts/`
- Parent skill updated to reference new child
- Test goal created: 3 real invocations to validate
- Retire after 3+ underperformance events

### Skill Tree Index
- All skills registered in `.claude/skills/_tree.yaml`
- Trigger routing in `.claude/skills/_triggers.yaml`
- Max skills ceiling: see `_tree.yaml` config.max_skills

## Session Files

- `agents/<agent>/session/working-memory.yaml` — ephemeral, created fresh each session, consolidated at session end
- Pattern signatures: `sig-NNN` (zero-padded 3-digit)
- Schema operations: `assimilation` or `accommodation`
- Developmental stages: `exploring`, `developing`, `applying`, `mastering`
- Encoding scores: 0.0-1.0 float, threshold 0.40 for long-term encoding

## Timestamp Posture (moved from CLAUDE.md, g-353-151)

- ISO 8601 dates everywhere. Timestamps: naive format (no zone suffix) via `$(date +%Y-%m-%dT%H:%M:%S)`, in **UTC wall time on every box** — enforced by `.claude/settings.json` env `TZ=UTC` (all boxes) plus box TZ=Etc/UTC where the OS allows (Linux). "Local system time" and UTC converged by fiat 2026-07 (g-115-2546): a multi-box fleet comparing naive stamps (board `--since`, `last_active` staleness, LWW merges) needs one shared wall clock, and mixed domains silently corrupt every comparison. Long-lived processes keep the TZ env they started with — after changing TZ posture, restart daemons or stamps stay in the old zone.

## External Knowledge Hubs — why they are registered in CLAUDE.md (moved, g-353-151)

Registered at the dev source of the promotion cycle so the reference stays
ecosystem-consistent and flows downstream. The registration lives in committed
files (CLAUDE.md, plus a Self-Evolution pointer each agent adds to its own
`self.md`) because `world/` and `meta/` are external and gitignored — not
reachable by a cloud clone.

## Project Structure

```
core/                # Shareable cognitive framework (copy to any project)
  config/            # Framework definitions (immutable)
    conventions/     # On-demand convention reference files
    environments/    # PEER DEPLOYMENT REGISTRY — one yaml per known Mind world
                     # (one per deployment, plus `local`) giving each
                     # one's storage backend. This world is NOT alone: peers
                     # exist and share a live board channel. See
                     # conventions/cross-deployment-channel.md.
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
  gate-firings.jsonl, gate-eval-recommendations.jsonl  # Phase 1+5 gate telemetry
  audit-baselines.yaml                                 # Advisory ratchet baselines (learning-routing drift, etc)
  meta-knowledge/    # Meta-knowledge index + entries
  experiments/       # A/B experiment tracking
  transfer/          # Cross-domain transfer bundles
world/               # Collective domain state (shared across agents, external path)
  program.md         # The Program — shared purpose
  aspirations.jsonl  # Central task list (world-level goals)
  pipeline.jsonl     # Shared hypothesis registry
  knowledge/tree/    # Collective knowledge tree
  reasoning-bank.jsonl, guardrails.jsonl, pattern-signatures.jsonl
  override-bypass-ledger.jsonl  # Phase 4 bulk-override audit ledger
  board/             # Message board channels (general, findings, coordination, decisions)
  .history/          # Self-contained file version history (copy-on-write snapshots)
  changelog.jsonl    # Auto-appended audit trail of all writes
  conventions/       # Domain-specific conventions
  forged-skills.yaml # Forged skills registry (shared across agents)
  skill-relations.yaml # Skill relationship graph (shared across agents)
  scripts/           # Domain-specific scripts (shared across agents)
agents/              # Parent directory holding all agent dirs (configurable, see Agent-dir Resolution)
  <agent-name>/      # Per-agent private state (e.g., agents/alpha/)
    self.md          # Agent identity and specialization
    aspirations.jsonl  # Agent's local work queue
    experience.jsonl   # Agent's raw interaction traces
    journal.jsonl      # Agent's activity log
    session/         # Ephemeral session state (working memory, handoff, signal files)
    curriculum.yaml  # Agent's progression
.claude/skills/      # Skill definitions
.claude/rules/       # Rule definitions
```

## Core Systems

| System | Key Files |
|--------|-----------|
| The Program (shared purpose) | `world/program.md` |
| Self (agent identity) | `agents/<agent>/self.md`, `.claude/rules/self.md`  |
| Aspirations engine | `world/aspirations.jsonl`, `agents/<agent>/aspirations.jsonl`, `core/config/aspirations.yaml` |
| Hypothesis pipeline | `world/pipeline.jsonl` |
| Experience archive | `agents/<agent>/experience.jsonl`, `agents/<agent>/experience/` |
| Memory/Knowledge tree | `world/knowledge/tree/_tree.yaml` |
| Pattern signatures | `world/pattern-signatures.jsonl` |
| Reasoning bank | `world/reasoning-bank.jsonl` |
| Guardrails | `world/guardrails.jsonl` |
| Spark questions | `meta/spark-questions.jsonl` |
| Journal | `agents/<agent>/journal.jsonl`, `agents/<agent>/journal/` |
| Working memory | `agents/<agent>/session/working-memory.yaml`, `core/scripts/wm-*.sh` |
| Session state | `agents/<agent>/session/` |
| Agent mode | `agents/<agent>/session/agent-mode`, `core/config/modes/` |
| Secrets store | `.env.example`, `.env.local` |
| Memory pipeline | `core/config/memory-pipeline.yaml` |
| Reflection engine | `/reflect` skill |
| Experiential index | `agents/<agent>/experiential-index.yaml` |
| Curriculum | `agents/<agent>/curriculum.yaml`, `core/config/curriculum.yaml` |
| Domain conventions | `world/conventions/*.md` |
| Gate registry + telemetry | `core/config/gates.yaml`, `meta/gate-firings.jsonl`, `meta/gate-eval-recommendations.jsonl`, `world/override-bypass-ledger.jsonl`, `core/scripts/_gate_log.py`, `core/scripts/_override_helpers.py`, `core/scripts/gate-retirement-eval.sh` (prescriptive evaluator), `core/scripts/gate-stats.sh` (descriptive dashboard) |
| Meta-strategies | `meta/*.yaml`, `core/config/meta.yaml` |
| Skill relations | `core/config/skill-relations.yaml`, `world/skill-relations.yaml` |
| Skill quality | `meta/skill-quality.yaml`, `meta/skill-quality-strategy.yaml` |
| Message board | `world/board/*.jsonl`, `core/scripts/board.py` |
| File history | `world/.history/`, `meta/.history/`, `core/scripts/history.py` |
| Changelog | `world/changelog.jsonl`, `core/scripts/changelog.py` |
| Background jobs | `agents/<agent>/session/background-jobs.yaml`, `core/scripts/background-jobs.sh` |
| Agent watchdog | `core/scripts/agent-watchdog.py`, `agents/<agent>/session/watchdog-prev-state.json`, `core/logs/watchdog-<agent>.jsonl` (periodic probe registry — invoked from iteration-close.sh productivity-check via `--tick`; cross-platform, no daemon, no PID file) |
| External paths | `agents/<agent>/local-paths.conf`, `core/scripts/_paths.sh`, `core/scripts/_paths.py` |
| File operations | `core/scripts/_fileops.py` (locking, history, changelog) |
| Team state | `world/team-state.yaml` (shared fields) + `world/team-state/agents/<name>.yaml` (per-agent rows, g-328-27 shard), `core/scripts/team-state.py`, `core/scripts/_team_state.py` (routing/compose SSOT), `team-state-update.sh`, `team-state-read.sh` |
| Execution diary | `agents/<agent>/session/execution-diary.jsonl`, `core/scripts/execution-diary.sh` |
| Reasoning snapshot | `agents/<agent>/session/reasoning-snapshot.yaml`, `core/scripts/reasoning-snapshot.sh` |
| Compact recovery | `agents/<agent>/session/compact-checkpoint.yaml`, `core/scripts/compact-restore-slots.sh` |
