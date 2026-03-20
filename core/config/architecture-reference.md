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
  ├── /reflect (ROUTER — ~140 lines, dispatches to mode sub-skills)
  │     ├── /reflect-hypothesis → full single hypothesis reflection (Steps 0.5-9)
  │     │     └── calls /reflect-tree-update for tree propagation
  │     ├── /reflect-batch-micro → batch micro-hypothesis processing (Steps 1-7)
  │     ├── /reflect-extract-patterns → pattern synthesis + strategy extraction (Steps 1-5)
  │     │     └── calls /reflect-tree-update for tree propagation
  │     ├── /reflect-calibration → confidence calibration check (Steps 1-4)
  │     ├── /reflect-curate-memory → memory curation + active forgetting
  │     ├── /reflect-tree-update → shared tree update protocol (Steps 1-4)
  │     ├── updates → mind/pattern-signatures.jsonl (DG/CA3 learning, via script)
  │     ├── updates → mind/knowledge/beliefs.yaml (belief confidence)
  │     ├── updates → mind/knowledge/transitions.yaml (contradictions)
  │     ├── updates → mind/reasoning-bank.jsonl + mind/guardrails.jsonl (via script)
  │     ├── updates → mind/knowledge/meta/step-attribution.yaml (step labels)
  │     └── feeds → /aspirations evolve (strategy changes)
  ├── /replay → compressed review, reconsolidation, domain transfer
  │     ├── updates → mind/pattern-signatures.jsonl (outcome stats, via script)
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
- **Capability gap detection**: recurring gaps logged to `mind/skill-gaps.yaml` → `/forge-skill` creates new skills when threshold met (times_encountered >= 2, value >= medium, type-dependent gate: CALIBRATE+ for utility gaps, EXPLOIT+ for analytical gaps)
- **Performance-based evolution triggers**: Responsive triggers — accuracy drops, consecutive failures, pattern divergence, capability unlocks (`core/config/evolution-triggers.yaml`)
- **Adaptive spark questions**: Track question productivity (yield rate) and retire low-yield questions, promote candidates (`core/config/spark-questions.yaml`)
- **Strategy archive**: When strategies change, old versions are preserved with performance data in `mind/strategy-archive.yaml`
- **Step-level attribution**: Label evaluation steps as GOOD/BAD/NEUTRAL during reflection to identify chronically weak steps (`mind/knowledge/meta/step-attribution.yaml`)
- **Preventive guardrails**: Failure-extracted "check this before acting" rules loaded during evaluation (`mind/guardrails.jsonl`, script-accessed via `guardrails-read.sh`)
- **Belief registry**: Formally separates beliefs from facts, tracks confidence trajectories, detects contradictions (`mind/knowledge/beliefs.yaml`, `mind/knowledge/transitions.yaml`)
- **Horizon-gated hypotheses**: `micro` (seconds, in working memory), `session` (hours, lightweight pipeline), `short` (days, standard), `long` (weeks+, full suite). Overhead scales with time horizon.
- **Self-regulated effort**: Per-goal metacognitive assessment determines effort_level (`full`, `standard`, `skip`). Controls execution thoroughness and spark depth, not retrieval (retrieval is always intelligent and full). Optional user focus directive (`mind/profile.yaml` `focus` field) steers value assessment.

The aspiration engine's evolution log (`mind/evolution-log.jsonl`) tracks when and why strategies change.

## Hippocampal Learning Framework

Biologically-inspired learning mechanisms layered on top of the Memory Tree. Based on hippocampal memory mechanics and Piaget's cognitive development theory.

### Memory Pipeline (encoding stages)
Observations flow through: **sensory buffer** (raw, ephemeral) → **working memory** (10 typed slots, session-scoped, including micro-hypothesis batch) → **encoding gate** (filters by novelty/surprise/impact/goal relevance, threshold 0.40) → **consolidation** (session-end replay, dynamic budget (5-15 items, scaled by violations/domains/surprise) compressed to leaf node articles) → **long-term tree** (dynamic random tree, K=4 MAX, D_max=6). Micro-hypotheses are batch-processed at session-end: only surprises (>= 7) enter the encoding gate. Config: `core/config/memory-pipeline.yaml`. Session state: `mind/session/working-memory.yaml`.

### Pattern Separation & Completion (dentate gyrus + CA3)
Pattern signatures (`mind/pattern-signatures.jsonl`, script-accessed via `pattern-signatures-read.sh`) enable two operations: **separation** ("this looks like pattern X but key feature differs so it's actually pattern Y") and **completion** ("partial cue matches → retrieve full strategy context"). Used during hypothesis evaluation to route between System 1 (fast/intuitive) and System 2 (slow/deliberate) reasoning.

### Hippocampal Replay & Reconsolidation
`/replay` skill runs compressed (3-line) reviews of resolved hypotheses, prioritizing violations, surprises, and high-impact outcomes. During replay, recalled strategies enter a **reconsolidation window** — they become temporarily updatable based on new evidence (reinforced, flagged for revision, or extended with new conditions). Includes domain transfer: patterns from strong domains (MASTER level) are abstracted and tested for applicability in weak domains (CALIBRATE level).

### Developmental Stages (Competence-Based)
System maturity tracked in `mind/developmental-stage.yaml` (framework: `core/config/developmental-stage.yaml`). Stage labels derived from average domain competence: **exploring** (avg < 0.30) → **developing** (0.30-0.55) → **applying** (0.55-0.80) → **mastering** (> 0.80). Exploration budget computed dynamically: `max(0.15, min(0.85, 1.0 - average_domain_competence))`. Per-domain capability_level gates hypothesis types and System 1/2 routing. `resolved_hypotheses` count is tracked as a diagnostic metric but does not gate progression. Schema operations (assimilation vs accommodation) are logged explicitly.

### Active Forgetting
Enhanced decay model: `retention = e^(-days / (lambda * importance * type_decay))`. Retrieval strengthens memories (resets decay timer). Interference between contradictory articles is detected and prioritized for resolution. Active pruning during `/aspirations evolve` archives validated but stale knowledge.

## Memory Taxonomy

Maps the filesystem structure to memory types (inspired by "Everything is Context" framework):

| Memory Type | Location | Access Pattern | Update Trigger |
|---|---|---|---|
| Scratchpad | `mind/session/working-memory.yaml` | Session-scoped R/W, consolidated at end | Every goal execution |
| Micro-predictions | `mind/session/working-memory.yaml` → `micro_hypotheses` | Inline R/W within session, batch-reflected at end | Inline during goal execution |
| Episodic | `mind/journal/` + `mind/journal.jsonl` | Append-only, indexed by session/date/tags via `journal-read.sh` | State Update Protocol Step 7 |
| Fact | `mind/knowledge/tree/` node articles (any depth) | Tree navigation via `_tree.yaml`, write via consolidation | Reflection, research, consolidation |
| Experiential | `pipeline-read.sh --stage resolved` + `mind/experiential-index.yaml` | Pattern matching by category/violation cause | `/reflect` |
| Procedural | `.claude/skills/` + `mind/skill-gaps.yaml` | Read at invocation, forged via `/forge-skill` | Capability gap detection |
| User | `core/config/profile.yaml`, `CLAUDE.md` | Read at session start (auto-loaded) | Manual or evolution |
| Historical | `mind/journal/` + `mind/evolution-log.jsonl` + `mind/pipeline.jsonl` | Immutable audit trail | Append-only |
| Signatures | `mind/pattern-signatures.jsonl` | DG separation + CA3 completion via `pattern-signatures-read.sh` | `/reflect`, `/replay` |
| Reasoning | `mind/reasoning-bank.jsonl` | Category + tag matching via `reasoning-bank-read.sh` | `/reflect` differentiated extraction |
| Guardrails | `mind/guardrails.jsonl` | Category filter via `guardrails-read.sh` | `/reflect` failure extraction |
| Beliefs | `mind/knowledge/beliefs.yaml` | Entity + category matching | `/reflect` reinforce/weaken/contradict |
| Transitions | `mind/knowledge/transitions.yaml` | Contradiction detection | `/reflect` interference detection |
| Attribution | `mind/knowledge/meta/step-attribution.yaml` | Step name lookup | `/reflect` step labeling |

**Context traceability**: Every `short`/`long` horizon hypothesis record includes a `context_consulted` manifest listing which tree nodes, pattern signatures, data sources, and articles were loaded during evaluation. `/reflect` compares this against available resources to detect context gaps. `session` horizon records may optionally include this. `micro` horizon predictions do not use context manifests.

## Session End Protocol

In addition to standard working memory consolidation (see `/aspirations` Session-End Consolidation Pass):

0. **Micro-Hypothesis Sweep**: Invoke `/reflect --batch-micro` to process any micro-predictions from working memory.
1. **Aspiration Archive Sweep**: Run `aspirations-archive.sh` to move completed/retired aspirations from live to archive JSONL.
2. **Tree Rebalancing**: Invoke `/tree maintain` to check for DECOMPOSE, REDISTRIBUTE, SPLIT, SPROUT, MERGE, PRUNE opportunities.
3. **Skill Gap Review**: Read `mind/skill-gaps.yaml` and evaluate if any gaps meet forge criteria.
4. **Skill Health Report**: Review active/forged/underperforming skills and flag any for retirement (3+ underperformance events).
5. **Write Continuation Handoff**: Write `mind/session/handoff.yaml` with session state snapshot.
6. **Preserve critical control signals**: Session-end consolidation MUST NOT modify:
   - `mind/session/agent-state` (only /start and /stop may change it)
   - `mind/session/persona-active` (only /escapePersona and /enterPersona may change it)

## Focus Directive

- `focus` in `mind/profile.yaml`: `null` (self-regulate) or natural language string
- User sets via conversation: "focus on coding", "explore everything", "go back to normal"
- Agent uses focus as context for per-goal metacognitive effort assessment
- Agent MUST NOT set focus — it is a user preference
- When null: agent self-regulates based on goal metadata alone
- When set: agent biases effort assessment toward focus-aligned goals

## Config Override Conventions

- Config files define parameter bounds in `modifiable:` sections (immutable)
- Active overrides live in `mind/config-overrides.yaml`
- Skills resolve: `mind/config-overrides.yaml[param] ?? core/config/[file][param]`
- All overrides must fall within `[min, max]` from modifiable section
- Every change logged to `mind/config-changes.yaml` (append-only audit)
- Override format: `{param}: {value: N, previous: N, changed_date: "YYYY-MM-DD"}`
- Change log format: `{param, config_file, old_value, new_value, reason, date, session, triggered_by}`
- Overrides can be reverted by removing the entry from config-overrides.yaml

## Skill Forging Conventions

### Gap Registry
- Capability gaps tracked in `mind/skill-gaps.yaml` (forge criteria in `core/config/skill-gaps.yaml`)
- Gap IDs: `gap-NNN` (zero-padded 3-digit)
- Gap statuses: `registered`, `under-review`, `forging`, `forged`, `dismissed`
- Forge criteria: times_encountered >= 2, value >= medium, distinct, type-dependent gate (utility: CALIBRATE+, analytical: EXPLOIT+)
- Gap types: `utility` (well-defined procedures, CALIBRATE+ gate), `analytical` (domain judgment, EXPLOIT+ gate)

### Forged Skill Lifecycle
- Created by `/forge-skill skill <gap-id>`
- Registered in `.claude/skills/_tree.yaml` and `.claude/skills/_triggers.yaml`
- Parent skill updated to reference new child
- Test goal created: 3 real invocations to validate
- Retire after 3+ underperformance events

### Skill Tree Index
- All skills registered in `.claude/skills/_tree.yaml`
- Trigger routing in `.claude/skills/_triggers.yaml`
- Max skills ceiling: 15 (prevents sprawl)

## Session Files

- `mind/session/working-memory.yaml` — ephemeral, created fresh each session, consolidated at session end
- Pattern signatures: `sig-NNN` (zero-padded 3-digit)
- Schema operations: `assimilation` or `accommodation`
- Developmental stages: `exploring`, `developing`, `applying`, `mastering`
- Encoding scores: 0.0-1.0 float, threshold 0.40 for long-term encoding
