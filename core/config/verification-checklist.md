# Post-Test Verification Checklist

> **Note:** This file is a comprehensive reference catalog. It is too large to load
> in a single LLM context window (~50K tokens). Actively-evaluated checks live in
> `.claude/skills/verify-learning/SKILL.md` Step 3. When adding checks for new
> features, add them there. Use this file for targeted per-section deep dives.

Domain-specific checks: `core/config/verification-checklist-domain-specific.md` (foundational) + `world/verification-checklist.md` (agent-discovered). Both read by /verify-learning.

Use this after running a fresh `/start` → `/stop` test cycle. Read the state files and verify each item below. For each item, report PASS, FAIL, or N/A (if the agent didn't reach that stage).

---

## A. Core Agent Lifecycle

1. State machine: `<agent>/session/agent-state` contains "IDLE" (stopped correctly)
2. Journal entries: `<agent>/journal/` has session logs with dated entries
3. Aspirations progress: `world/aspirations.jsonl` — goals attempted, sparks fired
4. Aspirations meta: `world/aspirations-meta.json` — session_count, readiness_gates
5. Aspirations archive: `world/aspirations-archive.jsonl` — completed aspirations moved here
6. Evolution log: `meta/evolution-log.jsonl` — evolution events logged
7. Developmental stage: `<agent>/developmental-stage.yaml` — should be `exploring` (first run)
8. Working memory: `Bash: wm-read.sh --json` — should be reset (all slots null/empty) after consolidation. Has `slot_meta` section.
9. Pipeline activity: `pipeline-read.sh --counts` — did it discover, evaluate, or activate any hypotheses?
10. Knowledge tree index: `world/knowledge/tree/_tree.yaml` — nodes registered with `article_count`, `growth_state`, `confidence`, `capability_level`, interior/leaf distinction via `node_type`. All scoring/structural metadata lives exclusively in `_tree.yaml` (split-by-nature schema).
11. No `world/knowledge/research-queue.yaml` exists (queue eliminated — aspirations handles topic selection)
12. No `world/knowledge/_index.yaml` exists (index consolidated into `_tree.yaml`)

---

## B. ALMA Improvements (Context Quality Feedback Loop)

1. `retrieve.sh --category <cat> --depth medium` returns JSON with tree_nodes, reasoning_bank, guardrails, pattern_signatures, experiences, beliefs, experiential_index
2. `core/config/hypothesis-conventions.md` / `core/config/knowledge-conventions.md` context manifest has `context_quality` sub-section with 4 fields (usefulness, most_valuable_source, least_valuable_source, chain_note)
3. `review-hypotheses` Step 4.1 initializes `context_quality: pending` on resolved records
4. `review-hypotheses` Step 4.5 rates context quality after resolution
5. `reflect-hypothesis` Step 7.7d finalizes pending context quality ratings
6. `reflect-hypothesis` Step 7.7e aggregates quality data into experiential index (runs AFTER 7.7d)
7. `core/config/evolution-triggers.yaml` has 6th trigger `context_retrieval_ineffectiveness`
8. `world/evolution-triggers.yaml` has matching initial state entry for trigger #6
9. `aspirations` Phase 9 comments list trigger #6 with `gap_frequency` + `usefulness_rate` checks
10. `core/config/evolution-triggers.yaml` has 7th trigger `evolution_cadence` (sessions_without_evolution: 7, cooldown_sessions: 5)
11. `world/evolution-triggers.yaml` has matching initial state entry for `evolution_cadence`
12. `core/config/evolution-triggers.yaml` `modifiable:` has `evolution_cadence_sessions` with bounds {min: 3, max: 15, default: 7}
13. `aspirations-evolve/SKILL.md` Step 6 calls `aspirations-meta-update.sh last_evolution` after logging
14. `core/config/evolution-triggers.yaml` has 8th trigger `evolution_goal_cadence` (goals_without_evolution: 15, cooldown_sessions: 0)
15. `world/evolution-triggers.yaml` has matching initial state entry for `evolution_goal_cadence`
16. `core/config/evolution-triggers.yaml` `modifiable:` has `evolution_goal_cadence_goals` with bounds {min: 8, max: 30, default: 15}
17. `aspirations/SKILL.md` Phase 9 Part A.1 checks `goals_since_last_evolution >= evolution_goal_cadence.goals_without_evolution`
18. `aspirations/SKILL.md` Phase -0.5 initializes `productive_goals_this_session`, `last_evolution_goal_count`, and `session_signals`
19. `aspirations/SKILL.md` `productive_goals_this_session` increment is AFTER all reclassification (per-goal + global anti-drift)
20. `aspirations/SKILL.md` global anti-drift fires at threshold 8 (higher than per-goal threshold 5)
21. `aspirations-learning-gate/SKILL.md` Phase 9.8 reads threshold from `meta/reflection-strategy.yaml` → `mode_preferences.full_cycle_cadence_goals`
22. `aspirations-learning-gate/SKILL.md` Phase 9.8 has team-aware deferral (checks coordination board for orchestrator)
23. `meta/reflection-strategy.yaml` has `mode_preferences.full_cycle_cadence_goals: 15`
24. `aspirations-learning-gate/SKILL.md` inputs include `productive_goals_this_session` (separate from `goals_completed_this_session`)
25. **Runtime**: After 15+ productive goals, journal should contain "OBLIGATION: full-cycle reflection" entry
26. **Runtime**: After 15+ goals without evolution, evolution log should contain `evolution_goal_cadence` trigger entry

---

## C. HindSight Improvements (Knowledge Retrieval)

1. `retrieve.py` depth limits: all levels return up to 50 tree nodes and 25 experiences (no longer differentiated by depth)
2. `core/config/hypothesis-conventions.md` / `core/config/knowledge-conventions.md` context manifest tracks `retrieval_channels` list
6. `core/config/hypothesis-conventions.md` / `core/config/knowledge-conventions.md` has Entity Cross-Links section with format, types, resolution rules
7. `core/config/tree.yaml` has `entity_index` config (max_entities: 200) and `initial_state` entry
8. `research-topic` Step 3 extracts entities from tree node content, respects max_entities cap
9. `reflect` Step 7.5b extracts entities from ABC chains, respects max_entities cap
10. **Runtime**: If any tree nodes were created or updated, check `world/knowledge/tree/_tree.yaml` for `entity_index` entries (including at L3+ nodes from DECOMPOSE)

---

## D. ReasoningBank Improvements (k-Bounded Retrieval + Contrastive)

1. `retrieve.py` loads ALL active reasoning bank entries and increments retrieval_count on each
2. `retrieve.py` loads ALL active guardrails and increments retrieval_count on each
3. `retrieve.py` increments retrieval counters atomically (writes back to source JSONL files)
4. `core/config/hypothesis-conventions.md` / `core/config/knowledge-conventions.md` / `core/config/conventions/reasoning-guardrails.md` has Reasoning Bank Retrieval Rules section (k-bounds, ranking, when_to_use format)
7. `core/config/hypothesis-conventions.md` / `core/config/knowledge-conventions.md` / `core/config/conventions/reasoning-guardrails.md` has Utilization Tracking section with full schema (retrieval_count, times_helpful, times_noise, times_active, times_skipped, utilization_score)
8. `core/config/hypothesis-conventions.md` / `core/config/knowledge-conventions.md` / `core/config/conventions/reasoning-guardrails.md` reasoning bank entry types include `contrastive`
9. `reflect` Step 2.6 contrasts CONFIRMED/CORRECTED pairs in same category to create contrastive rb entries
10. `reflect` Step 7.7e Part 2 updates utilization scores based on context_quality and deliberation
11. **Runtime**: If any rb entries were created, check via `reasoning-bank-read.sh --id <id>` that they have `when_to_use` and `utilization` fields

---

## E. AgentEvolver Improvements (Process-Outcome Dual Score)

1. Step-attribution data loaded from session snapshot (boot Step 2.5), not retrieve.sh
2. `core/config/hypothesis-conventions.md` / `core/config/knowledge-conventions.md` documents `process_score` and `dual_classification` on resolved records
5. `core/config/hypothesis-conventions.md` / `core/config/knowledge-conventions.md` process_quality formula comment says `confidence if CONFIRMED else (1.0 - confidence)`
6. `reflect` Step 2.5 extracts `when_to_use` from ABC antecedents for both wins and losses
7. `reflect` Step 2.5 has dual_classification modulation logic (defers if not yet computed)
8. `reflect` Step 7.6c computes `process_quality = confidence if CONFIRMED else (1.0 - confidence)` and `dual_classification` via confidence >= 0.60 threshold
9. `reflect` Step 7.6c computes `dual_classification` (earned_confirmed/lucky_confirmed/unlucky_corrected/deserved_corrected)
10. `review-hypotheses` Step 4.1 initializes `process_score` with all null fields including `dual_classification`
11. **Runtime**: If any hypotheses were resolved and reflected, check resolved records for `process_score` section

---

## F. Cross-Cutting Integration

1. All skills use `retrieve.sh --category --depth` for context loading (reflect, review-hypotheses, research-topic, replay, prime)
2. `CLAUDE.md` references `retrieve.sh` in Memory Tree and Context Loading sections
3. `core/config/hypothesis-conventions.md` / `core/config/knowledge-conventions.md` context_consulted section says "populated when retrieve.sh loads context"
4. Knowledge tree: `world/knowledge/tree/_tree.yaml` — L2+ nodes created if research occurred, L3+ from DECOMPOSE if /tree maintain ran
5. No orphan state: `pipeline-read.sh --counts` stage counts match `pipeline-recompute-meta.sh` recount
6. No stale signals: `<agent>/session/loop-active` exists if the loop ran; `<agent>/session/stop-loop` exists if stopped

---

## G. Known Design Limitations (verify these are NOT bugs)

1. Step 2.5 `dual_classification` modulation always defers on first reflection (process_score is null until Step 7.6c runs later) — this is expected, not a bug
2. Entity types in config are `[person, organization, concept, metric, event]` — these are informational, not enforced
3. Temporal retrieval uses preset windows (recent/month/quarter/all), not arbitrary date ranges — by design

---

## H. Recursive Tree (Depth Growth)

1. `core/config/tree.yaml` has `_tree_entry_node` generic template with `article_count` and `growth_state`
2. `core/config/tree.yaml` has `decompose_threshold` in config and modifiable sections
3. `tree/SKILL.md` has DECOMPOSE operation (triggers on monolithic leaves > decompose_threshold lines)
4. `tree/SKILL.md` has REDISTRIBUTE operation (triggers on interior nodes with body > decompose_threshold — fixes content left behind when SPROUT creates children)
5. `aspirations-consolidate/SKILL.md` step 6 lists all 8 ops: DECOMPOSE, REDISTRIBUTE, DISTILL, SPLIT, SPROUT, MERGE, PRUNE, RETIRE (omitting DISTILL/RETIRE prevents utility-based tree curation)
6. `tree/SKILL.md` SPLIT computes paths from `_tree.yaml` parent, not hardcoded domain
7. `tree/SKILL.md` SPLIT has depth guard (`parent.depth + 1 > D_max` → abort)
8. `reflect/SKILL.md` Tree Update Protocol creates child articles at leaf nodes (not append to L2)
9. `reflect/SKILL.md` Tree Update Protocol propagates recursively up parent chain
10. `core/config/knowledge-conventions.md` documents interior vs leaf invariant
11. `retrieve.py` depth names (shallow/medium/deep) map to numeric limits (3/7/12), not L1/L2/L3
12. **Runtime**: `_tree.yaml` has `article_count` and `growth_state` on all nodes
13. **Runtime**: L3+ nodes exist (directories created under L2 nodes that were decomposed)
14. **Runtime**: No hardcoded depth references remain: `grep -r "L2 topic\|L3 subtopic\|parent L1\|L2/L3\|depth: medium (L" .claude/skills/ core/config/` returns 0
15. **Runtime**: No leaf nodes exceed 2x decompose_threshold (160 lines) without `growth_state: ready_to_decompose`
16. **Runtime**: Tree depth stats show L3+ nodes after research sessions (`tree-read.sh --stats` → `by_depth.3` > 0)
17. **Runtime**: `tree-read.sh --decompose-candidates` returns empty (all leaves under threshold), OR `tree_growth_log` includes DECOMPOSE entries for oversized leaves
18. **Runtime**: `tree-read.sh --redistribute-candidates` returns empty (all interior bodies under threshold), OR `tree_growth_log` includes REDISTRIBUTE entries for oversized interiors
19. `tree/SKILL.md` has "Completion Criteria" section with 5 success conditions and 3 failure states (structural enforcement against identify-without-executing)
20. `core/config/tree.yaml` has `max_decompose_per_invocation: 7` in config and modifiable sections
21. `core/config/tree.yaml` has `max_redistribute_per_invocation: 5` in config and modifiable sections

---

## I. Tree Script Access Layer

1. `core/scripts/tree.py` exists with `read` and `update` subcommands
2. `core/scripts/tree-read.sh` and `core/scripts/tree-update.sh` are thin bash wrappers
3. `tree-read.sh --node root` returns JSON with defaults (`article_count`, `growth_state`, `node_type`)
4. `tree-read.sh --path intelligence` returns plain string path (not JSON)
5. `tree-read.sh --ancestors <any-L2-key>` returns array from node up to root
6. `tree-read.sh --children <L1-node>` returns child nodes matching `_tree.yaml` as JSON array
7. `tree-read.sh --leaves` returns only nodes with empty children lists
8. `tree-read.sh --stats` returns `total_nodes`, `by_depth`, `interior_count`, `leaf_count`
9. `tree-read.sh --child-path intelligence new-topic` returns correct computed path
10. `tree-read.sh --validate` returns `{"valid": true}` on consistent tree
11. `tree-update.sh --set root summary "test"` writes atomically and preserves YAML structure
12. `tree-update.sh --increment <key> article_count` handles missing field (default 0 → 1)
13. `core/config/conventions/tree-retrieval.md` has "Memory Tree Script Access" section
14. `tree-read.sh --validate` returns JSON with `warnings` array alongside `valid` and `errors` (field completeness warnings separate from structural errors)
15. **Runtime**: `tree-update.sh --add-child` with minimal JSON (just key + summary) persists `article_count`, `growth_state`, and `node_type` on the new node (write-time defaults prevent H17/H18 regression)
16. `tree-find-node.sh --text "environment processor" --top 3` returns JSON array with `key`, `score`, `file`, `depth`, `summary`, `node_type`
17. `tree-find-node.sh --text "deployment" --leaf-only --top 1` returns only leaf nodes (no interior)
18. `tree-update.sh --batch` accepts `{"operations":[...]}` on stdin with `set` and `increment` ops
19. `tree-update.sh --batch` validates ALL keys exist before any mutation (atomic: all-or-nothing)
20. `tree-propagate.sh <node-key>` returns JSON with `source_node`, `ancestors_updated`, `capability_changes`
21. `tree-propagate.sh root` returns empty `ancestors_updated` (root has no parent)
22. `tree-read.sh --summary` returns compact JSON with `file`, `summary`, `depth`, `capability_level`, `confidence`, `children` per node (no counters, growth_state)
23. `tree-read.sh --summary` total count matches `tree-read.sh --stats` total_nodes
24. `core/scripts/tree_match.py` exists — shared matching module imported by both `tree.py` and `retrieve.py`
25. `tree_match.py` `_compute_match_score` handles `confidence: null` (YAML null → Python None) without crashing
26. `core/config/conventions/tree-retrieval.md` documents Batch Update, Propagate, and Find Node APIs with usage examples
27. All 6 skill files reference `tree-find-node.sh` instead of `Read _tree.yaml` + `find_best_node()` for node lookup
28. All 4 skill files with propagation loops reference `tree-propagate.sh` instead of manual ancestor walk
29. `category-suggest.py` imports `build_concept_index` from `tree_match.py` (single source of truth — no duplicate matching logic)

---

## J. Pipeline Script Access Layer

1. `core/scripts/pipeline.py` exists with `read`, `add`, `update`, `update-field`, `move`, `archive-sweep`, `recompute-meta`, `meta-update` subcommands
2. 8 shell wrappers exist: `pipeline-read.sh`, `pipeline-add.sh`, `pipeline-update.sh`, `pipeline-update-field.sh`, `pipeline-move.sh`, `pipeline-archive.sh`, `pipeline-recompute-meta.sh`, `pipeline-meta-update.sh`
3. `pipeline-read.sh --counts` returns JSON with 5 stage keys
4. `pipeline-read.sh --accuracy` returns JSON with `total_resolved`, `confirmed`, `corrected`, `accuracy_pct`
5. `pipeline-read.sh --id <known-id>` returns complete record (searches live then archive)
6. `pipeline-read.sh --unreflected` returns only `reflected: false` resolved records
7. `pipeline-add.sh` rejects missing required fields (exit non-zero)
8. `pipeline-add.sh` rejects duplicate IDs (exit non-zero)
9. `echo '{"outcome":"CONFIRMED"}' | pipeline-move.sh <id> resolved` atomically merges + moves
10. `pipeline-update-field.sh <id> reflected true` auto-sets `reflected_date`
10b. `pipeline-update-field.sh <id> process_score.dual_classification earned_confirmed` creates nested object (not flat key) — dot-notation parity with experience.py
11. `pipeline-recompute-meta.sh` recomputes counts + accuracy from data (preserves `micro_hypothesis_stats`)
12. `core/config/conventions/pipeline.md` has "Pipeline JSONL Format" section with script API table
13. `CLAUDE.md` references pipeline JSONL files (not `world/pipeline/` directories)
14. No `world/pipeline/` directory references remain in skills: `grep -r "world/pipeline/" .claude/skills/` returns 0
15. **Runtime**: `world/pipeline.jsonl` exists (not `world/pipeline/` directory)
16. **Runtime**: `pipeline-read.sh --counts` stage counts sum matches `wc -l world/pipeline.jsonl` + archive
17. **Runtime**: `pipeline-read.sh --accuracy` `accuracy_pct` matches `confirmed / (confirmed + corrected) * 100`

---

## K. GSD-Inspired Optimizations (Deterministic Scoring, Handoff, Verification, Caching)

### K1. Goal Scoring with Exploration Noise
1. `core/scripts/goal-selector.py` exists with `select` and `blocked` subcommands
2. `core/scripts/goal-selector.sh` is thin bash wrapper that routes subcommands (defaults to `select`)
3. `goal-selector.sh` reads `world/aspirations.jsonl` via JSONL infrastructure
4. `goal-selector.sh` returns JSON array of ranked goals with score breakdowns
5. `goal-selector.sh` implements all 11 deterministic scoring criteria from aspirations/SKILL.md: priority, deadline_urgency, agent_executable, variety_bonus, streak_momentum, novelty_bonus, recurring_urgency, reward_history, evidence_backing, deferred_readiness, context_coherence
5b. `goal-selector.py` reads epsilon from `<agent>/developmental-stage.yaml` and noise_scale from `core/config/developmental-stage.yaml`
5c. `goal-selector.py` output includes `exploration_noise` in breakdown/raw, and `exploration_params` with epsilon/noise_scale/noise_weight
5d. At epsilon=0.19 (mastering), max noise contribution < 0.6
5e. At epsilon=0.85 (exploring), max noise contribution ~2.5
5f. `goal-selector.py` output includes `recurring` boolean field per goal (true if goal.recurring is set)
6. `aspirations/SKILL.md` Phase 2 calls `goal-selector.sh` instead of LLM-computing scores
6b. `aspirations/SKILL.md` Phase 2 has creative boredom check: if all candidates are recurring, invokes `/create-aspiration from-self` before proceeding with top recurring goal
7. **Runtime**: `goal-selector.sh` scores vary between invocations (exploration noise); deterministic criteria stable

### K2. Enhanced Handoff
8. `aspirations/SKILL.md` Session-End Consolidation Step 9 writes `first_action`, `decisions_locked`, and `session_summary` to handoff.yaml
9. `boot/SKILL.md` Step 0.5 reads `first_action` from handoff.yaml
10. `aspirations/SKILL.md` Phase 2 skips scoring on first iteration when `first_action` is present
11. `decisions_locked` entries have `decision`, `made_session`, `reason` fields
12. `decisions_locked` entries expire after 3 sessions (cleanup logic in consolidation)
13. `core/config/conventions/handoff-working-memory.md` documents handoff schema additions
14. **Runtime**: `<agent>/session/handoff.yaml` contains `first_action` after session-end consolidation
15. **Runtime**: First goal in new session matches `first_action.goal_id` (check journal)

### K3. Unified Verification Field
16. `core/config/aspirations.yaml` goal templates include `verification` field with `outcomes`, `checks`, `preconditions` sub-fields
17. `core/config/world-aspirations-initial.jsonl` and `core/config/agent-aspirations-initial.jsonl` bootstrap goals use `verification` field
18. `core/scripts/aspirations.py` validates `verification` field structure
19. `core/scripts/aspirations.py` accepts legacy `desiredEndState` + `completion_check` (backward compat)
20. `aspirations/SKILL.md` Phase 5 reads `verification.checks` (falls back to `completion_check`)
21. `aspirations/SKILL.md` Phase 2 COLLECT checks `verification.preconditions` alongside `blocked_by`
22. `decompose/SKILL.md` generates `verification` field instead of separate `desiredEndState` + `completion_check`
23. `core/config/conventions/goal-schemas.md` documents `verification` schema
24. **Runtime**: New goals have `verification` field (check aspirations-read.sh --active)
25. **Runtime**: Legacy goals with `completion_check` still pass Phase 5 verification

### K4. Intelligent Retrieval Protocol
26. `aspirations-execute/SKILL.md` Phase 4 reads `_tree.yaml` directly and reasons about relevant nodes
27. `aspirations-execute/SKILL.md` Phase 4 calls `retrieve.sh --supplementary-only` for non-tree stores
28. `aspirations-execute/SKILL.md` Phase 4 evaluates context sufficiency and reads secondary nodes if needed
29. `retrieve.py` supports `--supplementary-only` flag (skips tree node matching, returns empty `tree_nodes`)
29b. `aspirations/SKILL.md` Phase 4 uses `load-execute-protocol.sh` to load protocol digest (not full SKILL.md read)
29c. `core/config/execute-protocol-digest.md` contains complete retrieval Steps 1-5c (the safety-critical content)

### K5. Compatible Goal Batching (Context-Aware)
30. `aspirations/SKILL.md` Phase 2 reads `<agent>/session/context-budget.json` for zone
31. Fresh zone (<40%): batch up to 3 same-category goals (any aspiration), break on first category mismatch
32. Normal zone (40-65%): batch up to 2 same-category + same-aspiration goals
33. Tight zone (>65%): original strict criteria (same category + aspiration + skill + minimal effort)
34. If batch detected: single retrieval, sequential execution, individual Phase 5-8 per goal
35. **Runtime**: After decompose produces sibling goals, journal shows single retrieval for batched goals

### K6. Context Budget Estimator
36. `core/scripts/context-budget-status.py` reads status line JSON from stdin, writes `<agent>/session/context-budget.json`
37. `core/scripts/context-budget-status.sh` is thin bash wrapper
38. `.claude/settings.local.json` has `statusLine` object with `type: "command"` pointing to context-budget-status.sh
39. Budget file has: `used_pct`, `remaining_pct`, `window_size`, `input_tokens`, `zone`, `updated_at`
40. Zone thresholds: fresh (<40%), normal (40-65%), tight (>=65%)
41. `core/scripts/goal-selector.py` WEIGHTS dict includes `context_coherence: 1.0`
42. `core/scripts/goal-selector.py` reads budget file via `read_context_budget()`, defaults to zone "normal" if missing
43. `core/scripts/goal-selector.py` output includes `context_coherence` in breakdown and raw for each goal
44. `core/scripts/wm.py` TOP_LEVEL_KEYS includes `last_goal_category`; cmd_init and cmd_reset include `last_goal_category: ""`
45. `aspirations-state-update/SKILL.md` Step 3 writes `last_goal_category` via wm-set.sh
46. `core/scripts/precompact-checkpoint.py` saves `last_goal_category` in checkpoint
47. **Runtime**: With `last_goal_category` set, same-category goals show context_coherence > 0 in breakdown
48. **Runtime**: Status line output shows `CTX: {pct}% [{zone}]` after each assistant message

---

## L. Context Priming

Context is loaded at boot via `/prime`, which reads all domain-agnostic stores (guardrails,
reasoning bank, beliefs) and category-specific knowledge directly from source scripts.
No intermediate snapshot file is used — `/prime` and `retrieve.sh` read source stores on demand.

1. `aspirations/SKILL.md` Phase 4 uses intelligent retrieval protocol (reads _tree.yaml, selects nodes, calls --supplementary-only)
2. Intelligent retrieval protocol in Phase 4 reads _tree.yaml directly (no separate cache layer needed)

---

## M. Knowledge Freshness System

1. `.claude/rules/knowledge-freshness.md` exists
2. `core/config/memory-pipeline.yaml` has `knowledge_debt` slot type
3. `aspirations/SKILL.md` has Phase 4.5 post-execution reconciliation check
4. `aspirations/SKILL.md` Session-End has Step 2.25 knowledge debt sweep
5. `aspirations/SKILL.md` Handoff schema includes `knowledge_debts_pending`
6. `boot/SKILL.md` loads carried debts in Step 0.5
7. `respond/SKILL.md` has Step 6 user correction encoding
8. `reflect/SKILL.md` has Step 8.25 post-reflection reconciliation
9. `replay/SKILL.md` Step 4 (Reconsolidation) has source freshness check (item 5)
10. `core/config/conventions/infrastructure.md` documents `last_update_trigger` types
11. **Runtime**: When an aspiration is completed with skipped goals, all "TBD" entries in referenced knowledge tree nodes are resolved (not left as placeholders)
12. **Runtime**: When an aspiration is completed, knowledge tree nodes referenced by the aspiration have no stale defaults or contradicted status values (e.g., model marked "BLOCKED" when evaluation concluded "NO-GO")

---

## N. Self System

### N1. Self File & Rule
1. `<agent>/self.md` exists with YAML front matter (created, last_updated, last_update_trigger, source)
2. `<agent>/self.md` has non-empty body content (the agent identity description)
3. `.claude/rules/self.md` exists — contains directive to consult <agent>/self.md

### N2. Boot Integration
5. Boot dashboard displays "SELF" section with contents of <agent>/self.md
6. Boot continuation mode (Step 0.5) reads <agent>/self.md in abbreviated flow
8. **Runtime**: Journal boot entry mentions Self

### N3. /create-aspiration Skill
9. `.claude/skills/create-aspiration/SKILL.md` exists
10. `_tree.yaml` has create-aspiration registered under aspirations
11. `_triggers.yaml` has create-aspiration trigger entry
12. **Runtime**: After first boot, aspirations were created via /create-aspiration (not bootstrap copy)

### N4. Self-Driven Aspiration Generation
13. `/aspirations evolve` reads <agent>/self.md before generating aspirations
14. Aspirations loop no-goals fallback invokes /create-aspiration (not inline gap analysis)
15. Aspiration completion triggers /create-aspiration from-self
16. Phase 0 has aspiration health check (active_count < 2 triggers creation)
17. **Runtime**: At least one aspiration's motivation references the Self

### N5. Spark Questions
18. `core/config/spark-questions.yaml` has sq-012 (self_evolution category)
19. `core/config/spark-questions.yaml` has sq-c05 candidate (data_acquisition category)
20. `spark-questions-read.sh --id sq-012` returns sq-012 as active with counters (from `meta/spark-questions.jsonl`)
21. `spark-questions-read.sh --candidates` includes sq-c05 (from `meta/spark-questions.jsonl`)

### N6. Data Acquisition Awareness
22. /create-aspiration from-self Phase B scans tree nodes for unaccessed data sources
23. Entity index consulted for external system references (SSH, cloud services, APIs)
24. **Runtime**: After evolution, check if aspirations exist for known but unaccessed data sources

### N7. Self Maintenance
25. `/respond` directive table includes "Self update" type
26. sq-012 spark action writes Self evolution proposals to pending-questions.yaml
27. <agent>/self.md last_update_trigger tracks all update sources
28. Self survives IDLE/RUNNING transitions; wiped when agent directory is deleted

---

## O. Memex Experience Archive

### O1. Experience Archive Infrastructure
1. `core/scripts/experience.py` exists with `read`, `add`, `update-field`, `archive-sweep`, `meta-update` subcommands
2. 5 shell wrappers exist: `experience-read.sh`, `experience-add.sh`, `experience-update-field.sh`, `experience-archive.sh`, `experience-meta-update.sh`
3. `core/scripts/init-agent.sh` creates `<agent>/experience/` directory and empty JSONL/JSON files
4. `core/config/knowledge-conventions.md` documents experience archive format (JSONL schema, content files, verbatim anchors, retrieval_stats)
5. `core/config/memory-pipeline.yaml` has experience archival step, `archived_context` slot type, adaptive weight bounds
6. `core/config/conventions/experience.md` has Experience Archive section with ID format, script API, file layout
7. `CLAUDE.md` lists experience files in project structure and Core Systems

### O2. Experience Script Validation
8. `experience-add.sh` rejects missing required fields (exit non-zero)
9. `experience-add.sh` rejects duplicate IDs (exit non-zero)
10. `experience-read.sh --category <cat>` returns records filtered by category
11. `experience-read.sh --most-retrieved 5` returns top 5 by retrieval_count
12. `experience-update-field.sh <id> retrieval_stats.retrieval_count 1` updates atomically

### O3. Experience-Backed Hypotheses
13. `core/config/hypothesis-conventions.md` has `experience_ref` as optional string field
14. `core/scripts/pipeline.py` accepts `experience_ref` field without validation error
15. `aspirations/SKILL.md` Hypothesis Formation Spark Handler (step 2.5, within Phase 6 spark processing) archives hypothesis context via `experience-add.sh`
16. `aspirations/SKILL.md` Hypothesis Formation Spark Handler sets `experience_ref` on pipeline record
17. `reflect/SKILL.md` Step 1 dereferences `experience_ref` via `experience-read.sh --id`
18. **Runtime**: After hypothesis formation, experience `.md` file exists in `<agent>/experience/`
19. **Runtime**: `pipeline-read.sh --id <id>` shows `experience_ref` field on new records

### O4. Goal Execution Traces
20. `aspirations/SKILL.md` Phase 4.25 archives goal trace via `experience-add.sh`
21. **Runtime**: After goal execution, `<agent>/experience/` has corresponding `.md` file

### O5. Research Archive with Verbatim Anchors
22. `research-topic/SKILL.md` archives full results via `experience-add.sh` with verbatim anchors
23. `research-topic/SKILL.md` adds `experience_refs` to tree node front matter
24. `core/config/knowledge-conventions.md` documents verbatim anchor format
25. **Runtime**: After research, experience record has `verbatim_anchors` with at least 1 anchor

### O6. Experience Retrieval Integration
26. `retrieve.py` loads experiences by category, depth-limited (shallow=3, medium=5, deep=10)
27. `retrieve.py` increments experience retrieval_stats.retrieval_count on each load
29. `aspirations/SKILL.md` Phase 2 COLLECT checks prior experiences via `experience-read.sh --category`
30. `research-topic/SKILL.md` Step 1 checks prior research experiences
31. `reflect/SKILL.md` Step 2 uses verbatim anchors for ABC chain precision
32. `replay/SKILL.md` dereferences experience content during replay
33. **Runtime**: After retrieval, experience records show updated `retrieval_count`

### O7. Indexed Working Memory
34. `core/config/memory-pipeline.yaml` working memory slot schema includes `experience_refs` field
35. `core/config/memory-pipeline.yaml` has `archived_context` slot type
36. `core/config/conventions/handoff-working-memory.md` documents `experience_refs` and `archived_context` in slot schema
37. `aspirations/SKILL.md` writes `experience_refs` on slots when archiving context
38. **Runtime**: Working memory slots reference experience entries via `experience_refs`

### O8. Compression Quality Feedback + Freshness/Curation Signals
39. `reflect/SKILL.md` Step 7.7f updates experience retrieval_stats
40. `replay/SKILL.md` updates experience retrieval_stats
41. `aspirations/SKILL.md` session-end Step 2.6 adjusts encoding weights from utility data
42. `aspirations/SKILL.md` reconciliation checks `experience-read.sh --most-retrieved` for freshness prioritization
43. `reflect/SKILL.md` curate-memory checks `experience-read.sh --least-retrieved` for curation candidates
44. **Runtime**: After multiple sessions, experience records show varied retrieval_counts
45. **Runtime**: High-retrieval + aging tree nodes get priority reconciliation (knowledge_debt: HIGH)

### O9. Experience Staleness & Archival
46. `core/config/memory-pipeline.yaml` has `experience_staleness` section with `archive_unused_after_days`, `archive_low_utility_after_days`, `protect_threshold`
47. `experience-archive.sh` sweeps records matching staleness criteria to `<agent>/experience-archive.jsonl`
48. `experience-archive.sh` preserves content `.md` files (only moves JSONL record, not content)
49. `experience-read.sh --id <id>` searches archive after live (archived experiences still dereferenceable)
50. `experience-read.sh --category` excludes archived records (only returns live)
51. `reflect/SKILL.md` curate-memory calls `experience-archive.sh` alongside existing curation
52. `aspirations/SKILL.md` session-end consolidation calls `experience-archive.sh`
53. **Runtime**: Retrieval updates `last_retrieved` on experience records (retrieval strengthening)
54. **Runtime**: After 30+ days, unretrieved experiences are moved to archive JSONL

### O10. Cross-System Integration
55. `verify-learning/SKILL.md` Step 2 references "sections A through V"
56. `respond/SKILL.md` creates `user_correction` experience on user corrections
57. No orphan experiences: every JSONL record has corresponding `.md` file at `content_path`
58. Experience refs valid: every `experience_ref` in pipeline records points to existing experience
59. Retrieval data does NOT drive SPLIT/SPROUT — tree structure operations remain governed by article_count/body size criteria only

---

## P. Credentials & User Goals

### P1. Environment Infrastructure
1. `.env.example` exists at repo root with key entries
2. `core/scripts/env-read.sh` and `core/scripts/env.py` exist and are executable
3. `env-read.sh status` returns valid JSON array (one entry per registered key)
4. `env-read.sh missing` returns JSON array (empty if all credentials present)

### P2. Credential Detection During Boot
5. Boot Phase -0.5 runs during full boot (first `/start`)
6. Boot Step 0.5 step 1d runs during every auto-continuation
7. For each key returned by `env-read.sh missing`: a corresponding user goal exists with `participants: [user]`
8. User goals have `verification.checks` with `type: command_check` and `command: "bash core/scripts/env-read.sh has {KEY}"`
9. **Runtime**: After boot with missing credentials, `aspirations-read.sh --active` shows user goals for each missing key

### P3. Auto-Completion
10. Boot Step 0 sub-step 3 (full boot) and Step 0.5 step 1e (auto-continuation) check user credential goals
11. When `env-read.sh has <KEY>` returns exit 0, the corresponding user goal is marked completed
12. **Runtime**: After adding a credential to `.env.local`, next boot auto-completes the goal

### P4. Session-End Recap
13. Aspirations session-end Step 8.7 recaps pending user goals in visible banner
14. Handoff.yaml includes `user_goals_pending` with count and goal list
15. Boot Step 0.5 step 1c reports user goals from handoff on resume

### P5. Security
16. No credential values (from `env-read.sh value`) appear in any file under `world/`, `<agent>/`, or `meta/`
17. No credential values appear in `<agent>/journal/` entries
18. No credential values appear in `<agent>/session/handoff.yaml`

---

## Q. Work Discovery (Goal Sprouting)

### Q1. Spark Question Infrastructure
1. `core/config/spark-questions.yaml` has sq-013 in seed_questions (category: work_discovery)
2. `core/config/spark-questions.yaml` has sq-013 in initial_state with zeroed counters
3. `core/config/spark-questions.yaml` max_active_questions >= 13
4. `spark-questions-read.sh --id sq-013` returns sq-013 with status: active (from `meta/spark-questions.jsonl`)

### Q2. Spark Handlers
5. `aspirations/SKILL.md` has sq-013 handler section ("Work Discovery Spark Handler")
6. `aspirations/SKILL.md` has sq-007 handler section ("Aspiration Generation Spark Handler")
7. sq-013 handler step 2 has three-tier routing: current aspiration → other active → `/create-aspiration`
8. sq-013 handler step 2c skips to step 9 (log + increment) — `/create-aspiration` handles its own goals
9. sq-013 handler adds goals via aspirations-update.sh (not direct JSONL edit)
10. sq-013 handler discovery types include `fix` and `capability_gap` (not just requirement/dependency/follow-up/opportunity)
11. sq-007 handler invokes /create-aspiration from-self
12. Both handlers log via evolution-log-append.sh

### Q3. Runtime
13. **Runtime**: After goal completion, `spark-questions-read.sh --id sq-013` shows incremented times_asked
14. **Runtime**: When sq-013 fires, new goal appears in aspirations-read.sh --active with discovered_by field
15. **Runtime**: sq-013 can route discovered work to a DIFFERENT aspiration (not just current)
16. **Runtime**: Journal entry mentions work discovery spark when it fires

---

## R. Recurring Goals & Maintain Aspiration

### R1. Recurring Goal Infrastructure (Scripts)
1. `core/scripts/goal-selector.py` has `hours_since()` helper handling both `YYYY-MM-DD` and `YYYY-MM-DDTHH:MM:SS`
2. `core/scripts/goal-selector.py` has `get_interval_hours()` helper with `remind_days * 24` fallback
3. `core/scripts/goal-selector.py` `collect_candidates()` uses `interval_hours`/`hours_since` for recurring time gate
4. `core/scripts/goal-selector.py` `score_goal()` `recurring_urgency` uses 2.0 baseline + capped overdue ratio: `min(2.0 + (hours_overdue - interval_hours) / interval_hours, 5.0)`
5. `core/scripts/aspirations.py` `validate_goal()` accepts `interval_hours` (positive int) and `recurring` (bool)

### R2. Recurring Goal Lifecycle (Skill)
6. `aspirations/SKILL.md` Phase 0 recurring check uses `interval_hours` with `remind_days * 24` fallback
7. `aspirations/SKILL.md` Phase 5 writes full ISO timestamp (`YYYY-MM-DDTHH:MM:SS`) to `lastAchievedAt` on recurring goal completion
8. `aspirations/SKILL.md` Phase 5 updates `currentStreak` and `longestStreak` on recurring completion — resets streak to 1 when overdue by > 2x interval
9. `aspirations/SKILL.md` Phase 7 skips "aspiration fully complete" for aspirations where ALL goals are recurring
10. `aspirations/SKILL.md` `complete <goal-id>` documents `--permanent` flag (sets `recurring: false`)
11. `aspirations/SKILL.md` Goal Selection Algorithm COLLECT references `interval_hours`
12. `aspirations/SKILL.md` Goal Selection Algorithm SCORE `recurring_urgency` uses normalized overdue ratio (capped at 5.0)

### R3. Maintain Aspiration (Golden Source)
13. `core/config/aspirations.yaml` has `recurring` goal template with `interval_hours` field
14. `core/config/world-aspirations-initial.jsonl` has asp-001 (Explore and Learn)
15. `core/config/agent-aspirations-initial.jsonl` has asp-001 (Maintain Agent Health) with recurring goals using correct `interval_hours`, `priority`, and `recurring: true` fields
16. Each recurring goal has: `recurring: true`, `interval_hours: N`, `lastAchievedAt: null`, full `verification` block. g-001-01 has precondition requiring prior non-recurring goal execution.

### R4. Bootstrap Seeding
18. `core/scripts/init-world.sh` copies `core/config/world-aspirations-initial.jsonl` to `world/aspirations.jsonl` then runs `recompute-all-progress`
19. `core/scripts/init-agent.sh` copies `core/config/agent-aspirations-initial.jsonl` to `<agent>/aspirations.jsonl` (plus onboarding for subsequent agents)
20. Boot doesn't duplicate-create aspirations that already exist from init scripts

### R5. Schema & Conventions
20. `core/config/conventions/goal-schemas.md` has "Recurring Goal Fields" section documenting `interval_hours`, deprecated `remind_days`
21. `core/config/conventions/goal-schemas.md` documents `lastAchievedAt` uses full ISO 8601 timestamps (`YYYY-MM-DDTHH:MM:SS`)
22. `boot/SKILL.md` recurring goals table shows interval, last done, next due
22a. `core/config/conventions/goal-schemas.md` documents streak reset on missed intervals (2x threshold)

### R6. Runtime
23. **Runtime**: `goal-selector.sh` correctly gates a recurring goal with `interval_hours: 4` (recently completed goal not in candidates)
24. **Runtime**: After completing a recurring goal, `lastAchievedAt` contains a full timestamp (not just date)
25. **Runtime**: `agent-aspirations-read.sh --active` shows asp-001 with recurring goals
26. **Runtime**: Setting `recurring: false` via `aspirations-update-goal.sh` permanently stops the goal
27. **Runtime**: Maintain aspiration never triggers "aspiration fully complete" in Phase 7
28. **Runtime**: After completing a recurring goal that was overdue by > 2x interval, `currentStreak` is 1 (not incremented)
29. **Runtime**: `recurring_urgency` raw score never exceeds 5.0 in goal-selector.sh output
30. **Runtime**: g-001-01 is skipped on first boot iteration when no non-recurring goals have been executed

### R7. Deferred Goal Infrastructure
31. `core/scripts/goal-selector.py` `collect_candidates()` filters goals where `now < deferred_until`
32. `core/scripts/goal-selector.py` `score_goal()` `deferred_readiness` gives +1.5 raw (×0.6 weight) when deferred goal becomes due
33. `core/scripts/aspirations.py` `validate_goal()` accepts `deferred_until` (ISO 8601 timestamp or null) and `defer_reason` (string or null)
34. `core/config/conventions/goal-schemas.md` has "Deferred Goal Fields" section
35. `core/config/aspirations.yaml` `_common_fields` includes `deferred_until: null` and `defer_reason: null`
36. `aspirations/SKILL.md` COLLECT references `deferred_until` time gate
37. `aspirations/SKILL.md` SCORE includes `deferred_readiness` criterion
38. **Runtime**: `goal-selector.sh` correctly filters a goal with `deferred_until` in the future (not in candidates)
39. **Runtime**: `goal-selector.sh` includes a formerly-deferred goal after timestamp passes (with deferred_readiness bonus)
40. **Runtime**: `deferred_until` persists after goal execution (not cleared)
41. `core/scripts/aspirations.py` `cmd_update_goal()` comment warns: use "null" not "" to clear date fields (empty string passes update but fails validate_goal)

### R8. Decision Validity Checking (world_claim differential expiry)
42. `core/config/conventions/handoff-working-memory.md` documents `kind` ("strategy"|"world_claim") and `evidence_strength` ("weak"|"moderate"|"strong") fields
43. `boot/SKILL.md` Step 0.5.3 has world_claim challenge logic: weak expires after 1 session, moderate after 2
44. `aspirations-consolidate/SKILL.md` Step 9 has classification instruction for `kind` and `evidence_strength`
45. `reflect-curate-aspirations/SKILL.md` Step 1 has criterion 1e (orphaned deferral — defer_reason not backed by active decision)
46. `reflect-curate-aspirations/SKILL.md` Step 2 UNBLOCK decision covers orphaned deferrals (clear deferred_until + defer_reason)
47. **Runtime**: Weak world_claim in handoff.yaml expires after 1 session (not 3)
48. **Runtime**: Boot clears goal deferred_until/defer_reason when invalidating the supporting decision
49. All `decisions_locked` entries have `kind` field — no entries without it (no fallback, single source of truth)

---

## S. Phase 4.2 Post-Execution Domain Steps

### S1. Domain Convention Infrastructure
1. `.claude/skills/aspirations-execute/SKILL.md` contains Phase 4.2 section between Phase 4.1 and Phase 4.25
2. Phase 4.2 loads domain convention via `load-conventions.sh post-execution`
3. Phase 4.2 gracefully skips when no domain convention exists (fresh agent)
4. Phase 4.5 references `external_changes` from Phase 4.2 (not assumed)

### S2. Domain-Agnostic Cognitive Core
5. Base skill files contain no domain-specific terms from `<agent>/self.md` — verify by grepping key domain nouns from Self against `.claude/skills/*/SKILL.md`
6. `.claude/rules/*.md` files contain no domain-specific terms — verify by grepping key domain nouns
7. Convention files (`core/config/conventions/*.md`) use generic examples, not domain-specific ones
8. `core/config/verification-checklist-domain-specific.md` is a template (domain checks live in `world/verification-checklist.md`)
9. `core/scripts/infra-health.py` has no hardcoded component names or probe functions — components come from `<agent>/infra-health.yaml`, probes from `<agent>/scripts/probe-{name}.sh`
10. `core/scripts/init-agent.sh` creates `infra-health.yaml` with `components: {}` (empty, not domain-specific)
11. `core/scripts/guardrail-check.py` `INFRASTRUCTURE_KEYWORDS` contains only generic terms (no domain product names)
12. `core/scripts/guardrail-check.py` `extract_action_hint()` uses no auto-prefix magic — paths in guardrail text are used directly
13. `.env.example` has only section headers, no domain-specific credential entries (agent registers keys via `env.py register`)
14. `.gitignore` forged skill entries match `<agent>/forged-skills.yaml` (no orphan entries after reset)
15. `boot/SKILL.md` discovers L1 tree nodes dynamically from `_tree.yaml` (no hardcoded filenames)
16. `CLAUDE.md` Available Skills table has no domain-specific forged skill entries

### S3. Domain Convention Content
_(Domain-specific items live in `world/verification-checklist.md`, seeded from `core/config/verification-checklist-domain-specific.md` on init)_

---

## U. Communication Obligations

### U1. Scoring System
1. `core/scripts/goal-selector.py` recurring_urgency formula has 2.0 baseline when due
2. `core/scripts/goal-selector.py` treats `lastAchievedAt: null` as due (never-completed = needs doing)
3. `core/scripts/goal-selector.py` uses `>=` not `>` for interval comparison
4. `aspirations/SKILL.md` Goal Selection Algorithm documents the 2.0 baseline formula
5. `core/config/agent-aspirations-initial.jsonl` asp-001 includes g-001-04 (Generate progress report) at HIGH priority with interval_hours: 4
5b. Bootstrap g-001-04 `skill` = `/agent-completion-report` (core framework skill)
5c. Domain-specific wrappers (e.g., email delivery) are forged at runtime and override g-001-04 skill field

### U4. Runtime
11. **Runtime**: `goal-selector.sh` output shows g-001-04 with recurring_urgency raw >= 2.0 when due
12. **Runtime**: g-001-04 scores competitively with HIGH domain goals when due (score >= 7.0)
13. **Runtime**: After 4+ hours of operation, g-001-04 has been selected and executed at least once

---

## V. Script-Accessed JSONL Stores (Reasoning Bank, Guardrails, Pattern Signatures, Spark Questions, Journal)

### V1. Reasoning Bank + Guardrails Script Infrastructure
1. `core/scripts/reasoning-bank.py` exists with `rb` and `guard` top-level subcommands
2. 8 shell wrappers exist: `reasoning-bank-read.sh`, `reasoning-bank-add.sh`, `reasoning-bank-update-field.sh`, `reasoning-bank-increment.sh`, `guardrails-read.sh`, `guardrails-add.sh`, `guardrails-update-field.sh`, `guardrails-increment.sh`
3. `reasoning-bank-read.sh --summary` returns one-liner per record
4. `reasoning-bank-read.sh --active` returns only status=active records
5. `reasoning-bank-increment.sh <id> utilization.retrieval_count` increments counter AND recomputes `utilization_score = times_helpful / max(retrieval_count, 1)`
6. `guardrails-read.sh --active` returns only status=active guardrails
7. `guardrails-increment.sh <id> utilization.retrieval_count` increments counter AND recomputes utilization_score
8. Validation: `echo '{"id":"rb-test"}' | reasoning-bank-add.sh` exits non-zero (missing required fields)
9. Validation: duplicate ID rejection on add (exit non-zero)
10. All utilization objects have 7 fields: retrieval_count, last_retrieved, times_helpful, times_noise, times_active, times_skipped, utilization_score
10b. `guardrails-read.sh --summary` returns one-liner per guardrail (not empty output)
11. No `world/knowledge/reasoning-bank/` directory exists (old YAML format deleted)

### V2. Pattern Signatures Script Infrastructure
12. `core/scripts/pattern-signatures.py` exists with `read`, `add`, `update`, `update-field`, `record-outcome`, `set-status`, `migrate-yaml` subcommands
13. 6 shell wrappers exist: `pattern-signatures-read.sh`, `pattern-signatures-add.sh`, `pattern-signatures-update.sh`, `pattern-signatures-update-field.sh`, `pattern-signatures-record-outcome.sh`, `pattern-signatures-set-status.sh`
14. `pattern-signatures-read.sh --summary` returns one-liner per record with accuracy stats
15. `pattern-signatures-record-outcome.sh <id> CONFIRMED` atomically increments total+confirmed, recomputes `accuracy = confirmed / total`
16. `pattern-signatures-record-outcome.sh <id> CORRECTED` increments total only, recomputes accuracy
17. Accuracy is NEVER trusted from input — always recomputed by `normalize_record()`
18. `pattern-signatures-update.sh <id>` rejects stdin JSON with mismatched ID (data integrity guard)
19. No `world/knowledge/patterns/pattern-signatures.yaml` exists (old YAML format deleted)

### V3. Spark Questions Script Infrastructure
20. `core/scripts/spark-questions.py` exists with `read`, `add`, `update-field`, `increment`, `retire`, `promote`, `migrate-yaml` subcommands
21. 6 shell wrappers exist: `spark-questions-read.sh`, `spark-questions-add.sh`, `spark-questions-update-field.sh`, `spark-questions-increment.sh`, `spark-questions-retire.sh`, `spark-questions-promote.sh`
22. `spark-questions-read.sh --active` returns only type=question, status=active records
23. `spark-questions-read.sh --candidates` returns only type=candidate records
24. `spark-questions-increment.sh <id> times_asked` atomically increments AND recomputes `yield_rate = sparks_generated / max(times_asked, 1)`
25. `spark-questions-promote.sh <candidate-id> <new-sq-id>` converts candidate to active question with zeroed counters
26. Yield rate is NEVER trusted from input — always recomputed by `normalize_record()`
27. Both question (sq-NNN) and candidate (sq-cNN) records coexist in same JSONL, distinguished by `type` field
28. No `meta/spark-questions.yaml` exists (old YAML format deleted)

### V4. Journal Script Infrastructure
29. `core/scripts/journal.py` exists with `read`, `add`, `update`, `merge` subcommands
30. 4 shell wrappers exist: `journal-read.sh`, `journal-add.sh`, `journal-update.sh`, `journal-merge.sh`
31. `journal-read.sh --meta` returns computed metadata: total_sessions, last_updated, date_range (always derived from data)
32. `journal-read.sh --latest` returns highest session number record
33. `journal-read.sh --recent 5` returns last 5 sessions sorted by session number descending
34. `echo '{"goals_completed":["g-test"],"key_events":["test"]}' | journal-merge.sh <N>` union-merges goals_completed/tags, always-appends key_events
35. `journal-add.sh` auto-increments session number and auto-sets date if not provided
36. `journal-update.sh <N>` rejects stdin JSON with mismatched session number (data integrity guard)
37. No `<agent>/journal/_index.yaml` exists (old YAML format deleted)

### V5. Experiential Index (experience.py integration)
38. `core/scripts/experience.py` has `recompute-index` subcommand
39. `experience-recompute-index.sh` wrapper exists
40. `experience-recompute-index.sh` reads both live and archive pipeline JSONL, aggregates by category
41. Output: writes `<agent>/experiential-index.yaml` (human-readable YAML) + prints JSON to stdout
42. `<agent>/experiential-index.yaml` lives at top-level <agent>/ (not world/knowledge/patterns/)

### V6. Init Bootstrap
43. `core/scripts/init-world.sh` creates empty JSONL files: `world/reasoning-bank.jsonl`, `world/guardrails.jsonl`; `core/scripts/init-agent.sh` creates `<agent>/journal.jsonl`
44. `core/scripts/init-meta.sh` converts spark-questions to `meta/spark-questions.jsonl`; `core/scripts/init-world.sh` converts pattern-signatures to `world/pattern-signatures.jsonl` from config YAML initial_state via `migrate-yaml` subcommands
45. `core/scripts/init-agent.sh` creates `<agent>/experiential-index.yaml` at top-level (not patterns/ subdirectory)
46. `core/scripts/init-world.sh` does NOT create `world/knowledge/reasoning-bank/` directory (old format)

### V7. Runtime
47. **Runtime**: `world/reasoning-bank.jsonl` has records after `/reflect` OR Phase 6.5 immediate learning
48. **Runtime**: `world/guardrails.jsonl` has records after `/reflect` OR Phase 6.5 immediate learning
49. **Runtime**: `world/pattern-signatures.jsonl` has records with updated accuracy after `/reflect` or `/replay`
50. **Runtime**: `meta/spark-questions.jsonl` shows incrementing `times_asked` counters after goal completions
51. **Runtime**: `<agent>/journal.jsonl` has one record per session with accumulated goals/events/tags
52. **Runtime**: No direct JSONL file reads/edits by the LLM — all access through scripts
53. All 8 JSONL scripts use `ensure_ascii=True` in `write_jsonl`, `append_jsonl`, and `write_json` functions (prevents Windows shell mojibake from creating unreadable surrogates): `grep -c "ensure_ascii=True" core/scripts/aspirations.py core/scripts/pipeline.py core/scripts/experience.py core/scripts/reasoning-bank.py core/scripts/journal.py core/scripts/pattern-signatures.py core/scripts/spark-questions.py core/scripts/retrieve.py` — each file returns >= 2

---

## W. Context Priming (/prime)

### W1. Infrastructure
1. `.claude/skills/prime/SKILL.md` exists with `user-invocable: false` (internal skill)
2. `_tree.yaml` registers prime with `type: system`, `parent: null`, `user_invocable: false`
3. `_triggers.yaml` has boot trigger entry that calls prime
4. Boot SKILL.md has Step 2.7 (full boot: `invoke /prime`) and Step 8.5 (continuation: `invoke /prime --category {goal_category}`)
5. Boot chaining section lists `/prime`
6. CLAUDE.md Internal Skills table lists Prime (called by boot)
7. CLAUDE.md has NO "Utility Commands" section — prime is internal-only

### W2. Invocation
8. **Runtime**: Boot Step 2.7 invokes `/prime` during full boot (after snapshot, before dashboard)
9. **Runtime**: Boot Step 8.5 invokes `/prime --category {goal_category}` during auto-continuation
10. **Runtime**: Prime loads Self, guardrails, reasoning bank, beliefs, and category-specific tree nodes
11. **Runtime**: Prime output includes PRIMED summary banner with domain counts
12. **Runtime**: `/prime` with UNINITIALIZED state (no agent-state file) outputs "Nothing to prime" and stops

---

## X. Immediate Learning & Progress Self-Healing

### X1. Phase 6.5 Immediate Learning
1. `aspirations/SKILL.md` has Phase 6.5 between Phase 6 (spark check) and Phase 7 (aspiration check)
2. Phase 6.5 runs after every goal execution (no effort gate — all goals get immediate learning)
3. Phase 6.5 creates reasoning bank entries via `reasoning-bank-add.sh` (not direct JSONL edit)
4. Phase 6.5 creates guardrails via `guardrails-add.sh` (not direct JSONL edit)
5. Phase 6.5 comment distinguishes execution-time learning from `/reflect`'s hypothesis-resolution learning
6. Phase 6.5 has forge awareness block: detects recurring manual procedures that should be skills
7. Phase 6.5 forge awareness registers/increments gaps in `<agent>/skill-gaps.yaml`, checks forge criteria, creates forge goals via `aspirations-update.sh`
8. Phase 6.5 forge awareness has `gap.status == "forged"` guard (prevents re-forging already-forged skills)

### X2. Batch-Micro Actionable Discoveries
6. `reflect/SKILL.md` batch-micro has Step 6 (Actionable Work Check) before Step 7 (Return Batch Result)
7. Step 6 checks for concentrated surprises (3+ in one category) and overconfident misses (2+ in one category)
8. `batch_micro_result` includes `actionable_discoveries` field (empty list if none)
9. `aspirations/SKILL.md` consolidation Step 0 handles `actionable_discoveries` after batch-micro returns
10. Consolidation routes discoveries using same logic as sq-013 handler step 2

### X3. Progress Self-Healing
11. `core/scripts/aspirations.py` has `recompute_progress()` helper — always derives progress from goals array
12. `recompute_progress()` called in `cmd_add`, `cmd_update`, and `cmd_update_goal` (all three write paths)
13. `cmd_complete` and `cmd_retire` do NOT call it (they don't modify goals arrays — progress already correct)
14. `aspirations-read.sh --summary` progress counts always match actual goal counts

### X4. Runtime
15. **Runtime**: After goal execution, Phase 6.5 MAY create rb/guardrail entries (not guaranteed — only when insight is clear)
16. **Runtime**: After batch-micro with 3+ surprises in one category, consolidation creates goals for the gap
17. **Runtime**: `aspirations-read.sh --summary` total_goals matches `len(goals)` on every aspiration (self-healed on any write)

---

## Y. Single Source of Truth Architecture (No Baseline)

### Y1. No Baseline
1. `baseline/` directory does NOT exist
2. `core/scripts/deploy-baseline.sh` does NOT exist
3. `.claude/rules/golden-copy-sync.md` does NOT exist
4. `.gitignore` does NOT list `.claude/` or `CLAUDE.md` (they are committed to git)
5. No file in the repo contains the string `baseline/` (except historical journal entries in `<agent>/`)
6. `.claude/` and `CLAUDE.md` are tracked by git (not untracked `??`)

### Y2. Static Framework Registries
7. `.claude/skills/_tree.yaml` has NO entries with `forged: true` — base skills only
8. `.claude/skills/_tree.yaml` config section has NO `forged_skills` or `retired_skills` fields
9. `.claude/skills/_triggers.yaml` has NO forged skill trigger entries
10. Both files have comments stating they are static framework — never modified during RUNNING state

### Y3. Forged Skills in <agent>/
11. `<agent>/forged-skills.yaml` exists (created by `init-agent.sh` on first boot)
12. Every `.claude/skills/` directory NOT registered in `_tree.yaml` has a matching entry in `<agent>/forged-skills.yaml`
13. `forge-skill/SKILL.md` writes to `<agent>/forged-skills.yaml` (NOT `_tree.yaml` or `_triggers.yaml`)
14. `forge-skill/SKILL.md` contains "Do NOT touch `_tree.yaml` or `_triggers.yaml`"

### Y4. Framework Protection (settings.json)
16. `.claude/settings.json` has deny rules for every base skill directory (Edit + Write)
17. `.claude/settings.json` has deny rules for `_tree.yaml` and `_triggers.yaml`
18. `.claude/settings.json` has deny rules for `.claude/rules/*`, `core/config/*`, `core/scripts/*`, `CLAUDE.md`
19. Agent CAN create new `.claude/skills/{new-name}/` directories (forging) — not blocked by deny rules
20. **Runtime**: Agent attempting to edit a base skill gets a permission prompt (denied by default)

---

## Y. Forge-Skill Autonomous Wiring

### Y1. Bottom-Up Entry Points (Q6, Phase 6.5, Phase 9.2)
1. `reflect/SKILL.md` Q6 has forge criteria check after gap registration (threshold + value + developmental gate)
2. `reflect/SKILL.md` Q6 has `gap.status == "forged"` guard before forge criteria check
3. `reflect/SKILL.md` Q6 has dedup check: search active goals for gap ID before creating forge goal
4. `reflect/SKILL.md` Q6 routes forge goals using same three-tier routing as sq-013 (current → matching → create-aspiration)
5. `reflect/SKILL.md` Q6 logs forge-ready event via `evolution-log-append.sh` with `trigger_reason: reflect-q6-gap-detection`
6. `aspirations/SKILL.md` Phase 9.2 evolve has explicit per-gap loop: `For EACH gap where status != "forged"`
7. `aspirations/SKILL.md` Phase 9.2 invokes `/forge-skill check` for integrity audit before gap → goal creation
8. `aspirations/SKILL.md` Phase 9.2 has same forge criteria, dedup, and routing as Q6

### Y2. Consistency
9. All three entry points (Q6, Phase 6.5, Phase 9.2) use identical forge criteria: `times_encountered >= forge_threshold AND estimated_value >= "medium" AND developmental stage >= EXPLOIT`
10. All three entry points check `gap.status != "forged"` before creating goals
11. All three entry points check for existing forge goals before creating duplicates
12. All three entry points have distinct `trigger_reason` values for traceability

### Y3. User-Initiated Forge Path (4th Entry Point)
13. `/respond` Step 5 "Skill creation request" directive creates goals with `skill: "/forge-skill"` (top-down user entry into forge pipeline)
14. `/respond` registers new gaps in `meta/skill-gaps.yaml` when no matching gap exists (same inline pattern as Phase 6.5)
15. Generic "make a skill" (no specifics) routes to `/forge-skill list` (informational, no side effects)

### Y4. Misroute Guard (aspirations-execute)
16. `aspirations-execute/SKILL.md` has misroute guard before `result = invoke goal.skill with goal.args`
17. Misroute guard fires ONLY when `goal.skill is null` AND title matches skill-creation pattern
18. Misroute guard re-routes to `/forge-skill list` (safe fallback — no gap-id needed)
19. Misroute guard pattern is narrow: `(forge|create.*skill|make.*skill|skill.*creation)` — avoids false positives on Investigation/Idea/Unblock goals

### Y5. Runtime
20. **Runtime**: After a gap reaches `forge_threshold` encounters, `aspirations-read.sh --active` shows a forge goal with `skill: /forge-skill`
21. **Runtime**: Already-forged gaps (`status: forged` in `<agent>/skill-gaps.yaml`) do NOT generate new forge goals
22. **Runtime**: `evolution-log-append.sh` entries include `forge-ready` events with source entry point identified
23. **Runtime**: User says "make a skill for X" → goal created with `skill: "/forge-skill"` (not `skill: null`)

---

## Z. HTN Goal Decomposition Wiring

### Z1. Phase 3 Compound Detection
1. `aspirations/SKILL.md` Phase 3 has 6 compound detection criteria inline (from `/decompose` Step 3)
2. Phase 3 criteria: "and" in title, requires discovery, depends on findings, vague verbs, 2+ skills, effort > 1 session
3. Phase 3 invokes `/decompose goal.id` when compound criteria match
4. Phase 3 `continue` is CONDITIONAL on `goal.status == "decomposed"` (prevents infinite loop on heuristic false positives)
5. Phase 3 comment explains the else case: heuristic false positive → fall through to Phase 4
6. No undefined function calls: `is_compound()` does NOT appear anywhere in aspirations/SKILL.md

### Z2. /decompose Skill Completeness
7. `decompose/SKILL.md` has 5-point primitiveness test (Step 2) and 6-point compound detection (Step 3)
8. `decompose/SKILL.md` sets parent goal status to `"decomposed"` (Step 6)
9. `decompose/SKILL.md` adds sub-goals to aspiration via `aspirations-update.sh` (Step 6)
10. `core/scripts/goal-selector.py` treats `"decomposed"` as terminal status (same as `"completed"`)
11. `core/scripts/aspirations.py` has `"decomposed"` in `VALID_GOAL_STATUSES`

### Z3. Runtime
12. **Runtime**: Compound goals show status `"decomposed"` after Phase 3 + `/decompose` runs
13. **Runtime**: Sub-goals appear in `aspirations-read.sh --active` with `parent_goal` and `blocked_by` fields
14. **Runtime**: `goal-selector.sh` does NOT return decomposed goals (they are terminal)

---

## AA. Stale Blocker Prevention

### AA1. Script Infrastructure
1. `core/scripts/aspirations.py` has `_clear_stale_blockers(items, archived_goal_ids)` helper
2. `cmd_complete()` calls `_clear_stale_blockers` after `items.pop(idx)` and before `write_jsonl`
3. `cmd_retire()` calls `_clear_stale_blockers` after `items.pop(idx)` and before `write_jsonl`
4. `cmd_archive_sweep()` collects goal IDs from ALL aspirations being archived, calls `_clear_stale_blockers` on remaining

### AA2. Runtime
5. **Runtime**: After `aspirations-complete.sh <asp-id>`, no remaining active goals have `blocked_by` referencing goals from the archived aspiration
6. **Runtime**: Cross-aspiration `blocked_by` references (e.g., g-170-06 blocked by g-168-06) are cleaned when the blocking aspiration is archived

---

## AC. Participant Naming (owner → user)

1. `grep -ri "owner" core/config/ .claude/skills/ .claude/rules/ CLAUDE.md core/scripts/goal-selector.py` returns 0 matches (excluding "authoritative owner" data-controller usage)
2. `core/scripts/goal-selector.py` participant check uses `["user"]` not `["owner"]`
3. `core/config/aspirations.yaml` has `user_action:` goal template (not `owner_action:`)
4. `core/config/profile.yaml` has `surface_user_goals` (not `surface_owner_goals`)
5. User notification skill (if forged) uses `user` naming, not `owner`
6. `<agent>/forged-skills.yaml` entries use `user` naming, not `owner`
7. `aspirations-read.sh --active` contains zero "owner" references
8. `core/config/conventions/handoff-working-memory.md` documents `user_goals_pending` in handoff schema

## AD. Autonomous Loop Discipline

1. `aspirations/SKILL.md` Phase 10b contains "NEVER STOP, NEVER ASK" directive — no `stop_condition_met` pseudocode
2. `aspirations/SKILL.md` Stop Conditions section lists only 2 conditions: agent-state changed, critical error. "Session turn limit" is NOT listed.
3. `aspirations/SKILL.md` Phase 2.5 contains "Token cost and wall-clock time are NOT reasons to defer or skip"
4. `CLAUDE.md` Autonomous Loop Rules contains "NEVER STOP for context concerns"
5. `CLAUDE.md` Autonomous Loop Rules contains "NEVER defer or skip goals because of token cost"
6. `<agent>/session/stop-loop` does NOT exist (stale stop signals must be cleaned up)
6b. `boot/SKILL.md` Step 0.5 clears stop-loop in BOTH paths (handoff AND non-handoff) via `session-signal-clear.sh stop-loop`
7. `grep -c "stop_condition_met" .claude/skills/aspirations/SKILL.md` returns 0
8. `CLAUDE.md` Autonomous Loop Rules prohibits ALL forms of asking — tool, plain text, options, and waiting
9. `aspirations/SKILL.md` Stop Conditions lists "Wanting to ask a question" as NOT a stop condition
10. `aspirations-execute/SKILL.md` contains "Execution Autonomy Rule" section with decision-logging pattern
11. `.claude/rules/self.md` contains "Decision Authority" section — manager framing, act-then-log
12. `CLAUDE.md` Skill Invocation Rules contains "No blocking on user input in RUNNING state" (not just AskUserQuestion tool)
13. `session-signal-set.sh stop-loop` exits non-zero when state=RUNNING and counter < 4
14. `.claude/rules/stop-hook-compliance.md` exists and prohibits manual stop-loop
15. `stop-hook.sh` Tier 1-3 message contains "Do NOT set stop-loop"
16. `.claude/settings.json` has `"Stop"` key in `hooks` object referencing `stop-hook.sh` (global, not skill-scoped — skill-scoped hooks don't fire between loop iterations)

## AE. No-Fallback Philosophy

1. `guardrails-read.sh --id guard-001` rule contains "no graceful degradation"
2. `guardrails-read.sh --id guard-001` trigger_condition covers knowledge authoring (not just code)
3. `<agent>/self.md` contains no-fallback philosophy content _(domain-specific Self content checks moved to `core/config/verification-checklist-domain-specific.md`)_
4. `grep -ri "graceful degradation" world/knowledge/` returns 0 matches that endorse fallbacks (only matches in negation context like "not graceful degradation" are acceptable)
5. `guardrails-read.sh --id guard-013` exists with category "execution-discipline" and rule containing "Execute, don't just report"

## AG. Email Notification System

### AG1. Notification Intent Language
5. `aspirations/SKILL.md` has "Aspiration Update Notification (MANDATORY)" section using generic intent language
6. `create-aspiration/SKILL.md` Step 8.5 notifies user on aspiration creation using generic intent language
7. `decompose/SKILL.md` Step 6 item 8 notifies user on sub-goal addition using generic intent language
8. `forge-skill/SKILL.md` Step 8 notifies user on skill creation using generic intent language

### AG4. Domain Decoupling
25. No core skill contains domain-specific notification script names or email addresses — grep returns zero matches across aspirations, create-aspiration, decompose, forge-skill, aspirations-execute

---

## AH. Team-Based Research Delegation

### AH1. Framework Config
1. `core/config/aspirations.yaml` has `max_concurrent_goals: 3` in framework section
2. `core/config/aspirations.yaml` `modifiable:` section has `max_concurrent_goals: {min: 1, max: 3, default: 3}`
3. Config comment says "Max concurrent goals per iteration (primary + parallel)" — NOT "dispatched to sub-agents"

### AH2. Aspirations Skill Phases
4. `aspirations/SKILL.md` has Phase 2.6 (Pre-Fetch Context for Upcoming Goals) between Phase 2.5b and Phase 3
5. Phase 2.6 says "team agents are research assistants, not executors"
6. Phase 2.6 limits prefetch goals to `max_concurrent_goals - 1`
7. Phase 2.6 checks "g has a research/analysis phase that can run independently"
8. `aspirations-execute/SKILL.md` Phase 4 has `Team-Based Research Delegation` section
9. Phase 4 says "Never use bare sub-agents (Agent tool without team_name)"
10. Phase 4 dispatch uses `TeamCreate` + `Agent(team_name=...)` — NOT bare `Agent(...)`
11. Worker prompt uses `build-agent-context.sh` to inject context as data — NOT "invoke /prime"
12. Worker prompt says "Do NOT write or edit ANY files" and "Do NOT invoke skills"
13. Workers MUST NOT invoke skills, write files, or call state-mutating scripts
14. Host executes goals with enriched context from agent findings
15. No `invoke goal.skill` inside team agent prompts
16. Apply Pre-Fetched Research section is positioned AFTER Phase 9.7, BEFORE Phase 10
17. Results section executes each goal with enriched context (invoke goal.skill with args + findings)
18. Results section runs Phase 4.1 through Phase 9 per goal (full cycle)
19. Results section includes team shutdown after processing
20. Phase 10b has "team agents" discovery note (host stays productive while agents work)

### AH3. Tool Access
21. `CLAUDE.md` Tool Access section lists `TeamCreate, SendMessage` alongside existing tools

### AH4. Consistency
22. `grep -c "sub-agent" .claude/skills/aspirations-execute/SKILL.md` returns exactly 1 match (the "Never use bare sub-agents" warning — this is correct)
23. No bare `Agent(...)` calls (without `team_name`) appear in the delegation sections
24. Delegation is opt-in: Phase 2.6 says "IF host chooses to pre-fetch" — not mandatory

---

## AI. Skill Decomposition (Sub-Skill Architecture)

Verifies the aspirations and reflect skills were correctly decomposed into orchestrator/router + sub-skills.

### AI1. Aspirations Sub-Skills Exist

1. `aspirations/SKILL.md` is the orchestrator (should be ~900 lines, NOT 1800+)
2. `aspirations-execute/SKILL.md` exists with `parent-skill: aspirations` and `user-invocable: false`
3. `aspirations-spark/SKILL.md` exists with `parent-skill: aspirations` and `user-invocable: false`
4. `aspirations-state-update/SKILL.md` exists with `parent-skill: aspirations` and `user-invocable: false`
5. `aspirations-consolidate/SKILL.md` exists with `parent-skill: aspirations` and `user-invocable: false`
6. `aspirations-evolve/SKILL.md` exists with `parent-skill: aspirations` and `user-invocable: false`

### AI2. Reflect Sub-Skills Exist

7. `reflect/SKILL.md` is the router (should be ~140 lines, NOT 1400+)
8. `reflect-hypothesis/SKILL.md` exists with `parent-skill: reflect` and `user-invocable: false`
9. `reflect-batch-micro/SKILL.md` exists with `parent-skill: reflect` and `user-invocable: false`
10. `reflect-extract-patterns/SKILL.md` exists with `parent-skill: reflect` and `user-invocable: false`
11. `reflect-calibration/SKILL.md` exists with `parent-skill: reflect` and `user-invocable: false`
12. `reflect-curate-memory/SKILL.md` exists with `parent-skill: reflect` and `user-invocable: false`
13. `reflect-tree-update/SKILL.md` exists with `parent-skill: reflect` and `user-invocable: false`

### AI3. Invoke Stubs in Orchestrators

14. `aspirations/SKILL.md` contains `aspirations-execute` reference (Phase 4 invoke)
15. `aspirations/SKILL.md` contains `aspirations-spark` reference (Phase 6 invoke)
16. `aspirations/SKILL.md` contains `aspirations-state-update` reference (Phase 8 invoke)
17. `aspirations/SKILL.md` contains `aspirations-consolidate` reference (Session-End invoke)
18. `aspirations/SKILL.md` contains `aspirations-evolve` reference (evolve invoke)
19. `reflect/SKILL.md` contains `reflect-hypothesis` reference (--on-hypothesis dispatch)
20. `reflect/SKILL.md` contains `reflect-batch-micro` reference (--batch-micro dispatch)
21. `reflect/SKILL.md` contains `reflect-extract-patterns` reference (--extract-patterns dispatch)
22. `reflect/SKILL.md` contains `reflect-calibration` reference (--calibration-check dispatch)
23. `reflect/SKILL.md` contains `reflect-curate-memory` reference (--curate-memory dispatch)
24. `reflect/SKILL.md` contains `reflect-tree-update` reference (Integration Points)

### AI4. Settings Deny List

25. `.claude/settings.json` has Edit+Write deny for all 11 sub-skills: aspirations-execute, aspirations-spark, aspirations-consolidate, aspirations-evolve, aspirations-state-update, reflect-hypothesis, reflect-batch-micro, reflect-extract-patterns, reflect-calibration, reflect-curate-memory, reflect-tree-update

### AI5. Keyword Coverage (No Dropped Steps)

26. All phase markers (Phase 0 through Phase 11) appear across aspirations skill files
27. All critical script calls (aspirations-update-goal.sh, aspirations-add-goal.sh, retrieve.sh, experience-add.sh, pipeline-add.sh, tree-update.sh, goal-selector.sh, journal-add.sh, evolution-log-append.sh, spark-questions-read.sh) appear across aspirations skill files
28. All reflect step markers (Step 0.5 through Step 9) appear across reflect skill files
29. All reflect concepts (ABC Chain, Differentiated Extraction, Contrastive Extraction, Pattern Signatures, Belief Registry, Context Gap Analysis, Decay Model, Propagate Upward) appear across reflect skill files

### AI6. Cross-References Updated

30. `CLAUDE.md` Internal Skills table lists all 11 sub-skills as italicized sub-skill entries
31. `core/config/architecture-reference.md` chaining map includes sub-skill hierarchy under `/aspirations loop` and `/reflect`
32. No orphaned "see section below" or "defined below" references in `aspirations/SKILL.md`

## AJ. Error Response Protocol

Verifies the agent confronts infrastructure errors instead of retreating to self-contained tasks.

### AJ1. Phase 4.1 Guardrail Consultation + Error Response Protocol

1. Phase 4.1 consults guardrails after ANY infrastructure goal — success or failure, NOT local/tooling errors
2. Step 4.1-pre uses `guardrail-check.sh --context infrastructure --outcome {flag} --phase post-execution` for deterministic matching
3. Step 4.1-pre is generic — specific checks (e.g., domain error scripts) live in guardrails, not hardcoded in the skill
4. `guardrail_found_issues` initialized BEFORE the `IF involved_infrastructure:` block (not inside it)
5. Protocol fires when: guardrail found issues OR goal failed + infrastructure
6. Step 4.1a skips sleep when guardrail already confirmed error emails exist
7. End-of-protocol has TWO exit paths: failed goals revert to pending + continue; successful goals with guardrail issues fall through to Phase 4.25+ for normal completion
8. Step 4.1b (CASCADE DETECTION) sorts emails by timestamp ascending, identifies root cause
9. Step 4.1c determines severity: confirmed_infrastructure, explicit_failure, or soft_failure
10. Steps 4.1d-e execute when severity is confirmed or explicit: 4.1d TRY FIX INLINE, 4.1e CREATE BLOCKER (only if fix failed, gated by `fixed_inline` flag)

### AJ2. Scripts Exist and Work

7. `core/scripts/aspirations-add-goal.sh` exists and adds a goal to an existing aspiration
8. `core/scripts/aspirations.py` has `add-goal` subcommand that validates goal JSON and auto-assigns ID
11b. `core/scripts/guardrail-check.sh` exists and `core/scripts/guardrail-check.py` implements matching
11c. `guardrail-check.sh --context infrastructure --outcome succeeded --phase post-execution` returns guard-016, guard-017
11d. `guardrail-check.sh --context any --phase pre-selection` returns guard-011, guard-014, guard-017
11e. `guardrail-check.sh --context infrastructure --outcome failed --phase post-execution` returns guard-011, guard-014, guard-015, guard-016, guard-017, guard-018
11f. `guardrail-check.py` imports `reasoning-bank.py` for all JSONL I/O (no duplicated read/write code)

### AJ3. Cognitive Primitive Goal Properties

12. Unblocking goals (from CREATE_BLOCKER) have `priority: HIGH`, title starts with "Unblock:"
13. Investigation goals have `priority: MEDIUM`, title starts with "Investigate:"
14. Idea goals have `priority: MEDIUM`, title starts with "Idea:"
15. All three types have `skill: null` and include relevant context in description
16. Dedup: before creating any goal type, check for existing goals with similar title. Update existing rather than duplicating

### AJ4. Behavioral Verification

17. Agent NEVER says "self-contained design task, no infrastructure needed" when unresolved errors exist
18. `.claude/rules/error-response.md` exists and states the blocker-centric imperative with three cognitive primitives and guardrail-driven enforcement
19. `core/config/conventions/aspirations.md` documents `aspirations-add-goal.sh` in the Aspiration Script-Based Access table
20. `core/config/conventions/infrastructure.md` documents infrastructure check scripts in the relevant sections
21. guard-016 exists with category "error-response" and rule about checking error emails before deferring
21b. guard-017 exists with category "error-response" — post-infrastructure email check (success or failure) + pre-selection sweep
21c. rb-014 exists with category "error-response" — superficial success lesson

### AJ5. Cascade Detection & Deferral Gate

22. When multiple error emails exist, sorted by timestamp ascending (oldest first)
23. Earliest email identified as root cause
24. Unblocking goal (from CREATE_BLOCKER) title references failure reason; optional investigation goal references root cause
25. CREATE_BLOCKER diagnostic_context includes cascade chain; unblocking goal description includes what was tried
26. Owner alert includes blocker info, unblocking goal ID, and cascade chain when detected
27. **Runtime**: Agent NEVER defers a goal due to timeout/hang without first checking for infrastructure errors
28. **Runtime**: Agent NEVER rationalizes a failure without first checking error emails
29. **Runtime**: Agent checks error emails after SUCCESSFUL infrastructure goals, not just failures (guard-017 enforcement)

### AJ6. Pre-Selection Guardrail Check (Phase 0.5a)

30. Phase 0.5a exists in aspirations/SKILL.md between Phase 0.5 and Phase 0.5b
31. Phase 0.5a uses `guardrail-check.sh --context any --phase pre-selection` for deterministic matching
32. Phase 0.5a is generic — no domain-specific content in the skill pseudocode
33. **Runtime**: Error emails are checked before every goal selection (via guard-017 pre-selection trigger)

---

## AK. Retrieval Quality (Multi-Strategy Matching)

Verifies the improved retrieval pipeline returns relevant nodes via multiple matching channels.

### AK1. Retrieval Accuracy

3. All goals in `aspirations.jsonl` have non-null `category` fields (set via `category-suggest.sh`)
4. Multi-category query works: `retrieve.sh --category "<cat1>,<cat2>" --depth shallow` returns nodes from both categories

### AK2. Output Fields

5. Tree nodes in `retrieve.sh` output include `match_channel` (how the node was matched) and `match_score` (numeric relevance)
6. `retrieve.sh` output meta section includes `retrieval_channels` listing which strategies contributed results
7. `core/config/knowledge-conventions.md` documents Strategy 4 (concept matching), sibling inclusion, match-quality scoring, multi-category support, and new output fields

### AK3. Category System

8. `category-suggest.sh --text "<domain-specific query>"` returns a relevant tree node key as top match
9. `goal-selector.py` `_resolve_category` uses goal.category first, falls back to category-suggest subprocess
10. `create-aspiration/SKILL.md` Step 4b includes `category` field with `category-suggest.sh` determination
11. `core/config/aspirations.yaml` `_common_fields` includes `category: null` field

### AK4. Invocation Coverage

12. `aspirations/SKILL.md` Phase 2.5 metacognitive assessment runs without separate pre-fetch (Phase 4 intelligent retrieval handles all context)
13. `decompose/SKILL.md` has Step 3.5 calling `retrieve.sh` for goal category before decomposing
14. `aspirations-spark/SKILL.md` Phase 6.5 checks existing reasoning-bank/guardrails by category before creating new entries
15. `aspirations-spark/SKILL.md` sq-009 calls `retrieve.sh` + pipeline-read before forming hypotheses
16. `aspirations-evolve/SKILL.md` gap analysis calls `retrieve.sh --category intelligence` (NOT `root` — root has no file/content)

## AM. Intelligent Retrieval Protocol

Verifies Phase 4 uses LLM-driven retrieval instead of mechanical depth-based retrieval.

### AM1. Script Changes

1. `retrieve.py` accepts `--supplementary-only` flag (skips tree node matching, returns empty `tree_nodes`)
2. `retrieve.py` `DEPTH_LIMITS` are all 50 (no differentiation between shallow/medium/deep)
3. `retrieve.py` `EXP_LIMITS` are all 25 (no differentiation between shallow/medium/deep)
4. `retrieve.py` always includes `.md` content (no `include_content` conditional)
5. `retrieve.py` `--depth` parameter accepted but deprecated (all levels equivalent)

### AM2. Skill Pseudocode

6. `aspirations-execute/SKILL.md` Phase 4 reads tree via `load-tree-summary.sh` (Step 1), convention-style cached
7. `aspirations-execute/SKILL.md` Phase 4 calls `retrieve.sh --supplementary-only` (Step 4)
8. `aspirations-execute/SKILL.md` Phase 4 increments `retrieval_count` via `tree-update.sh` per node (Step 3)
9. `aspirations-execute/SKILL.md` has no references to `retrieval-cache.yaml`, `cache_hit`, or `depth_map`
10. `aspirations/SKILL.md` Phase 2.25 loads tree summary via `load-tree-summary.sh` for goal selection context (convention-style cached, informs Phase 2.5 familiarity/value assessment)
11. `aspirations/SKILL.md` effort_level values are `full`, `standard`, `skip` only (no `light`)
12. `aspirations/SKILL.md` Phase 6 spark check has no effort_level gate (all goals get sparks)
13. `aspirations-spark/SKILL.md` Phase 6.5 has no `effort_level == "light"` skip condition

### AM3. Cleanup Verification

14. No `.md` files in repo contain `retrieval-cache`, `cache_hit`, `depth_map`, or `"light"` as effort level
15. `boot/SKILL.md` whitelist does not include `retrieval-cache.yaml`
16. `core/config/architecture-reference.md` references `intelligent retrieval` (not `retrieval cache`)

### AM4. Observability

17. `aspirations-execute/SKILL.md` emits `▸ Intelligent retrieval: scanning knowledge tree...` before Step 1
18. `aspirations-execute/SKILL.md` emits `▸ Tree nodes: {keys} ({N} loaded)` after Step 3 listing loaded node keys
19. `aspirations-execute/SKILL.md` emits `▸ Supplementary: {N} reasoning, {N} guardrails, {N} patterns, {N} experiences` after Step 4
20. `core/config/status-output.md` Context Retrieval section matches the intelligent retrieval output format
21. **Runtime**: Phase 4 execution produces visible `▸ Intelligent retrieval:` and `▸ Tree nodes:` lines in agent output

### AM5. Retrieval Manifest & Utilization Enforcement

22. `aspirations-execute/SKILL.md` Step 5b writes durable retrieval manifest to `working-memory.yaml → slots.active_context.retrieval_manifest`
23. Retrieval manifest includes: `goal_id`, `goal_title`, `tree_nodes_loaded`, `supplementary_counts`, `deliberation` (active_items + skipped_items), `utilization_pending: true`
24. `aspirations-execute/SKILL.md` Step 5c outputs `▸ Retrieval influence:` line articulating how retrieved knowledge informs execution
25. `aspirations-execute/SKILL.md` Phase 4.26 reads the durable manifest (not transient LLM state) for utilization feedback
26. `aspirations-execute/SKILL.md` Phase 4.26 unconditionally clears `utilization_pending: false` for ALL outcomes (not just productive)
27. `aspirations-execute/SKILL.md` Phase 4.25 experience archive includes `retrieval_audit` field (manifest_present, nodes_count, active_count, skipped_count, utilization_fired, influence)
28. `aspirations/SKILL.md` Phase 9.5b (Retrieval Gate) runs for ALL outcomes — forces retroactive retrieval or utilization feedback if skipped
29. `core/config/status-output.md` has `▸ Retrieval manifest:`, `▸ Retrieval influence:`, and `▸ Utilization feedback:` output formats
30. **Runtime**: After goal execution, `▸ Retrieval manifest:` and `▸ Utilization feedback:` lines appear in output
31. **Runtime**: After several sessions, `guardrails-read.sh --active` shows `times_helpful > 0` on at least some items

---

## AL. Verify Before Assuming (Infrastructure Health)

Verifies the agent always probes infrastructure before declaring it unavailable.

### AL1. Rule & Script Infrastructure

1. `.claude/rules/verify-before-assuming.md` exists with verification imperative
2. `core/scripts/infra-health.sh` exists as thin bash wrapper
3. `core/scripts/infra-health.py` exists with `check`, `check-all`, `status`, `stale` subcommands
4. `<agent>/infra-health.yaml` created by `init-agent.sh` with all components initialized to null

### AL2. Integration Points

5. Phase 2.5b blocker gate probes infrastructure via `infra-health.sh check` before accepting a blocker
6. Phase 0.5b checks `infra-health.yaml` last_success for success-based blocker clearing
7. Phase 0.5b ACTIVE REPROBING probes every iteration while blocker exists (not once per session — cost of staying blocked >> cost of repeated probes)
8. Domain convention `world/conventions/post-execution.md` Step 1 records infrastructure health after successful infrastructure goals
9. `core/config/conventions/infrastructure.md` documents Infrastructure Health Tracking section with script table and schema

### AL4. Behavioral Verification

13. **Runtime**: Agent never declares a component unreachable without a preceding failed probe command in the same session
14. **Runtime**: Agent never declares "infrastructure unavailable" without `infra-health.sh check` output showing failure
15. **Runtime**: Stale blockers are re-probed before being accepted (Phase 0.5b active reprobe every iteration + Phase 2.5b gate)
17. **Runtime**: Blockers with `last_success=null` are probed every iteration, not carried forward indefinitely
18. **Runtime**: A blocker caused by missing credentials is cleared when credentials are added and probe succeeds

### AL5. Auto-Recovery Probes

21. Auto-recovery does NOT apply to remote infrastructure components (no local fix)

---

## AN. Hybrid Skill Architecture (agent-completion-report + backlog-report)

Verifies the hybrid skill pattern: user-invocable AND agent-callable. Currently two hybrid skills:
`/agent-completion-report` (core) and `/backlog-report` (core). Domain-specific wrappers (e.g., email delivery) are forged at runtime.

### AN1. Hybrid Skill Category

1. `.claude/skills/agent-completion-report/SKILL.md` has `user-invocable: true` in frontmatter
2. `CLAUDE.md` Skill Invocation Rules has "Hybrid skills" bullet listing `/agent-completion-report` AND `/backlog-report`
3. `CLAUDE.md` User Control Commands table includes `/agent-completion-report` and `/backlog-report` with `ANY` valid-from
4. `CLAUDE.md` Enforcement Rule 1 has explicit exception: both ARE agent-callable
5. `CLAUDE.md` Available Skills "User Control Commands" section includes both with "*(also agent-callable)*"
6. `.claude/skills/_triggers.yaml` has comment noting `/agent-completion-report` is agent-callable
7. Neither `/agent-completion-report` nor `/backlog-report` is in the "MUST NOT invoke" enumerated list (enforcement rule 1)
7b. `/backlog-report` is NOT in the Internal Skills table (it's hybrid, not agent-only)
7c. `.claude/skills/backlog-report/SKILL.md` has `user-invocable: true` in frontmatter
7d. `_tree.yaml` has `backlog-report` with `user_invocable: true` and `model_invocable: true`

### AN2. Timer Ownership

8. `/agent-completion-report` Phase 5 always sets `lastAchievedAt` (report window advances)
9. `/agent-completion-report` Phase 5 sets `status completed` ONLY when called by user directly
10. Domain-specific wrappers (if forged) set `status completed` only after successful delivery

### AN3. Domain Decoupling

11. `/agent-completion-report` contains NO domain-specific script calls, credential checks, or delivery references — pure framework scripts only
12. Domain-specific delivery wrappers live in forged registry, deleted on factory reset

### AN4. Blocked Goals Section

13. `/agent-completion-report` Phase 2 Step 9 calls `goal-selector.sh blocked` (single source of truth — no inline chain-walking)
14. `/agent-completion-report` Phase 3 has "Blocked" section between "Active Work" and "Needs Attention" — omitted when 0 blocked
15. `trace_root_bottleneck()` stop conditions cover ALL 7 goal statuses: pending→READY/INFRA/DEFERRED/NEEDS USER, in-progress→IN PROGRESS, blocked→BLOCKED (status), skipped/expired→DEAD END (completed/decomposed in done_ids, never reached)
16. `cmd_blocked()` reads `known_blockers` from working-memory for INFRA classification — empty known_blockers → all roots show as READY (fail-open)

### AN5. Message Board Section

17. `/agent-completion-report` Phase 2 Step 10 reads all 4 channels via `board-read.sh --json`
18. Board section omitted entirely when all channels have zero messages
19. Duration calculation: `ceil((now_epoch - since_epoch) / 3600)` hours, passed to `--since`
20. Lifetime report (since=null) reads all messages (no `--since` flag)
21. Empty/missing board directory handled gracefully (no error)
22. Max 10 messages per channel, with "... and N earlier" overflow note

### AN8. Report Persistence

23. `/agent-completion-report` Phase 4 writes timestamped file to `<agent>/reports/`
24. `/agent-completion-report` Phase 4 writes `<agent>/COMPLETION-REPORT.md` (latest, overwritten)
25. Timestamped filename uses hyphens (not colons) for Windows compatibility: `completion-report-{YYYY-MM-DDTHH-MM-SS}.md`

### AN6. Backlog Report Behavior

26. `/backlog-report` SKILL.md writes to `<agent>/BACKLOG.md` (not repo root)
27. Phase 1 uses only framework scripts + pending-questions.yaml read (no direct JSONL reads)
28. Phase 2 step 4: null `lastAchievedAt` treated as infinitely overdue (never-completed recurring goals)
29. Phase 2 step 5: null/missing `resolves_no_earlier_than` treated as testable (no time gate)
30. Phase 5 terminal summary includes absolute path to `<agent>/BACKLOG.md`
31. `/backlog-report` mutates NO JSONL, NO state files — only writes `<agent>/BACKLOG.md`
32. `/backlog-report` has NO timer mechanism (unlike `/agent-completion-report` which updates g-001-04)

### AN7. Runtime

33. **Runtime**: User runs `/agent-completion-report` → console report displayed, g-001-04 `lastAchievedAt` updated
34. **Runtime**: `/agent-completion-report` shows "Blocked" section with root bottlenecks when dependency-blocked goals exist
35. **Runtime**: After 4h of RUNNING, g-001-04 selected → `/agent-completion-report` fires (or forged delivery wrapper if exists)
36. **Runtime**: If delivery wrapper fails, g-001-04 is NOT marked completed (retried next cycle)
37. **Runtime**: User runs `/backlog-report` → `<agent>/BACKLOG.md` written, terminal summary displayed with full file path
38. **Runtime**: `/backlog-report` shows "Hypotheses Ready to Test" section only for hypotheses past their `resolves_no_earlier_than` date

---

## AO. Blocked Goals Diagnostics (goal-selector.sh blocked + /open-questions)

Verifies the `goal-selector.sh blocked` subcommand and `/open-questions` blocked goals section.

### AO1. Script Infrastructure

1. `core/scripts/goal-selector.py` has `collect_blocked()` function (inverse of `collect_candidates()`)
2. `core/scripts/goal-selector.py` has `cmd_blocked()` subcommand registered in `main()`
3. `core/scripts/goal-selector.sh` defaults to `select` when called with no arguments (backward compat)
4. `goal-selector.sh blocked` returns JSON with `blocked_goals`, `by_reason`, `summary` keys
5. `by_reason` always has all 5 keys: `infrastructure`, `dependency`, `deferred`, `hypothesis_gate`, `explicit_status` — even when empty
6. `by_reason.dependency` always has `head_count` and `downstream_count` fields
7. `collect_blocked()` check order is: explicit_status → infrastructure → dependency → deferred → hypothesis_gate (order matters for chain compression)

### AO2. Mutual Exclusivity

8. No goal appears in both `goal-selector.sh select` and `goal-selector.sh blocked` output
9. Goals excluded from both: user-only (`participants: ["user"]`), recurring not-yet-due, aspiration-cooldown
10. `total_active_goals` counts all non-terminal goals (pending + blocked + in-progress) including user-only

### AO3. Chain Compression & Root Bottleneck Tracing

11. Dependency-blocked goals have `chain_position`: `"head"` or `"downstream"`
12. A goal is `"head"` if none of its unmet deps are themselves dependency-blocked
13. Cross-aspiration dependencies work correctly (goal IDs are globally unique)
14. Infrastructure-blocked goals are NOT in `dep_blocked_ids` — downstream goals depending on them are correctly classified as `"head"`
15. `bottlenecks` array sorted by `downstream_count` descending
16. Each bottleneck entry has: `goal_id`, `title`, `aspiration_id`, `cause`, `downstream_count`, `downstream_ids`, `affected_aspirations`
17. `trace_root_bottleneck()` handles cycles (returns `CYCLE`), missing goals (returns `UNKNOWN`), all 7 statuses
18. Each `blocked_goals` entry has `root_bottleneck` field with `goal_id` and `cause`
19. `/agent-completion-report`, `/open-questions`, `/backlog-report` all use `goal-selector.sh blocked` (single source of truth)

### AO4. Skill Integration

20. `/open-questions/SKILL.md` Phase 3.5 calls `goal-selector.sh blocked`
21. `/open-questions/SKILL.md` Phase 4 groups blocked goals by reason (infrastructure, dependency, deferred, hypothesis_gate, explicit_status)
22. Dependency section shows only HEAD goals with downstream count summary
23. Summary line includes blocked goals count

### AO5. Runtime

24. **Runtime**: `goal-selector.sh blocked` output lists expected blocked goals with correct reasons and `bottlenecks` array
25. **Runtime**: `goal-selector.sh` (no args) still returns scored candidates (backward compat)
26. **Runtime**: User runs `/open-questions` → blocked goals section appears with grouped display

---

## AQ. Blocker-Centric Error Response

### AQ1. CREATE_BLOCKER Protocol
1. aspirations-execute has CREATE_BLOCKER protocol defined (between Phase 4.0 and Phase 4.1)
2. Protocol creates BOTH blocker entry AND unblocking goal atomically
3. Blocker entry has `unblocking_goal` field linking to the goal ID
4. Unblocking goal has `priority: HIGH` and descriptive title starting with "Unblock:"
5. Protocol is invoked by Phase 4.0, Phase 4.1e, AND Phase 0.5a (single source of truth)

### AQ2. Phase 4.0 Fast-Path (with Recovery Attempt)
6. aspirations-execute has Phase 4.0 BEFORE Phase 4.1
7. Phase 4.0 handles INFRASTRUCTURE_UNAVAILABLE and RESOURCE_BLOCKED markers
8. Phase 4.0 attempts ONE recovery via `infra-health.sh check` (which has auto-recovery probes for known components) before blocking
9. Phase 4.0 retries the skill once if recovery succeeds; blocks only if retry also fails or no component mapping exists
9b. Phase 4.0 `retry_attempted` guard prevents infinite retry loops — do not remove

### AQ3. Phase 4.1 Redesign
10. Phase 4.1 no longer has Step 4.1e (CREATE INVESTIGATION GOAL) — eliminated
11. Phase 4.1d is TRY FIX (was 4.1h ASAP, moved before blocker creation)
12. Phase 4.1e is CREATE BLOCKER (only if inline fix failed)
13. Alert and cascade are inside CREATE_BLOCKER, not separate steps

### AQ4. Structured Markers
14. Domain-specific forged skills (if present) use structured markers at SKIP points
16. Skill Failure Return Contract says MUST (not SHOULD)

### AQ5. Blocker Resolution
17. Phase 0.5b checks unblocking_goal completion as PRIMARY resolution path
18. Phase 0.5b still checks: user goal completion, 3-session expiry, infra-health success
19. known_blockers schema has: type, unblocking_goal, diagnostic_context fields

### AQ6. Cognitive Primitives (Three Types)
20. aspirations-execute has "Cognitive Primitives (Always Available)" section before Phase 4
21. Section documents all three types: Unblock (HIGH), Investigate (MEDIUM), Idea (MEDIUM)
22. Unblocking goals created ONLY via CREATE_BLOCKER (never manually)
23. Investigation + Idea goals created ad-hoc via aspirations-add-goal.sh (anytime)
24. Title conventions enforced: "Unblock:", "Investigate:", "Idea:"
25. Dedup instruction: check for similar existing goals before creating
26. Cross-aspiration placement: pick the RIGHT aspiration, not just current

### AQ7. Spark + Evolve Integration
27. Spark question pool has at least one idea-generating spark ("Is there a better way?")
28. /aspirations-evolve gap analysis checks for accumulated idea goals as aspiration signals
29. /respond routes user ideas ("What if...?") to idea goals in relevant aspiration

### AQ8. /respond Discovery Pipeline
30. `/respond` Step 5 directive table includes "Observation / problem report" type
31. Observation directive creates **HIGH**-priority investigation goals (user-reported = urgent, not MEDIUM)
32. Observation directive says "No confirmation needed" (explicit override of Processing Rule #2)
33. Observation directive works in RUNNING and IDLE states; verbal-only in UNINITIALIZED
34. `/respond` has Step 7 "Discovery Check" — agent self-discoveries, RUNNING only, MEDIUM priority
35. Step 7 dedup runs AFTER Step 5 writes (sequential execution prevents duplicates)
36. **Runtime**: User reports problem during RUNNING → HIGH investigation goal created before loop resumes
37. **Runtime**: Agent notices anomaly during response → MEDIUM investigation goal created (Step 7)
38. `/respond` Step 5 directive table includes "Skill creation request" type (placed BEFORE "Idea/suggestion")
39. Skill creation directive creates goal with `skill: "/forge-skill"` — NOT `skill: null`
40. Skill creation directive delegates forge gates to execution time ("do NOT pre-check here")
41. Skill creation directive works in RUNNING and IDLE; verbal-only in UNINITIALIZED

### AQ9. Runtime
39. **Runtime**: Agent fixes error inline → optionally creates investigation goal ("why?") and/or idea goal ("prevent this?")
40. **Runtime**: Agent notices pattern during goal → creates idea or investigation goal → scored by goal-selector
41. **Runtime**: Unblocking goal completes → Phase 0.5b clears blocker → affected goals become selectable
42. **Runtime**: Accumulated ideas in one domain → /aspirations-evolve may create new aspiration

---

## AP. Convention Demand-Loading Architecture

Verifies that cognitive core conventions are demand-loaded via `core/config/conventions/` rather than always-loaded, and that skills correctly declare their dependencies.

### AP1. Convention Files Exist

1. `core/config/conventions/` directory contains exactly 15 `.md` files
2. Required files: `aspirations.md`, `pipeline.md`, `experience.md`, `reasoning-guardrails.md`, `pattern-signatures.md`, `spark-questions.md`, `journal.md`, `tree-retrieval.md`, `goal-schemas.md`, `goal-selection.md`, `session-state.md`, `infrastructure.md`, `secrets.md`, `handoff-working-memory.md`, `working-memory.md`
3. `CLAUDE.md` has "Convention Index" table listing all 15 files with topics

### AP2. Always-Loaded Files Are Slim

4. `.claude/rules/conventions.md` does NOT exist (deleted — content moved to `core/config/conventions/`)
5. `CLAUDE.md` is under 16,000 characters (target: ~15K, was ~24K)
6. `.claude/rules/error-response.md` is under 600 characters (slim imperative + pointer)
7. `.claude/rules/knowledge-freshness.md` is under 600 characters (slim imperative + pointer)
8. `.claude/rules/verify-before-assuming.md` is under 500 characters (slim imperative + pointer)
9. Total always-loaded (CLAUDE.md + all rules/*.md) is under 25,000 characters

### AP3. Skill Front Matter Declarations

10. Every skill SKILL.md has a `conventions:` field in YAML front matter
11. Skills with non-empty `conventions:` lists have a "Step 0: Load Conventions" instruction referencing `load-conventions.sh`
12. Skills with `conventions: []` (reset) do NOT have Step 0
13. `/aspirations` conventions include: aspirations, pipeline, goal-schemas, session-state, handoff-working-memory, infrastructure, reasoning-guardrails, experience
14. `/boot` conventions include: aspirations, pipeline, session-state, handoff-working-memory, secrets, reasoning-guardrails, tree-retrieval, pattern-signatures, journal
15. `/verify-learning` conventions include all 13 convention files (loads everything for checklist)

### AP4. No Stale References

16. No file in the repository contains the string `.claude/rules/conventions.md` (grep returns 0 matches)
17. `core/config/verification-checklist.md` references point to `core/config/conventions/{topic}.md` or `CLAUDE.md`, not to deleted `conventions.md`

### AP5. Content Completeness

18. Each convention file contains the complete script API table for its topic (same row count as the original `conventions.md` section)
19. `core/config/conventions/infrastructure.md` contains the full error response protocol, infra health scripts, verify-before-assuming rules, AND knowledge reconciliation detailed protocol
20. `CLAUDE.md` Universal Conventions section contains: file formats, naming, dates, ID formats, priority values, key status values (Goals, Pipeline, Aspirations) with pointer to convention files for full per-entity lists, pipeline rules, self file format, skill invocation rules, code change verification, knowledge reconciliation imperative, tool usage + write permissions

### AP6. Runtime

21. **Runtime**: When a skill with `conventions: [aspirations, pipeline]` is invoked, it reads `core/config/conventions/aspirations.md` and `core/config/conventions/pipeline.md` before executing its first phase
22. **Runtime**: The agent can successfully run `/start` → boot → aspirations loop with convention files loaded on demand (no failures due to missing schema information)
23. **Runtime**: Skills use `load-conventions.sh` in Step 0 to check which conventions need reading — already-loaded conventions are skipped (context read dedup, section AT)

---

## AR. Compact Checkpoint Protocol (PreCompact/SessionStart Hooks)

Verifies that encoding state is preserved across autocompact cycles and processed in fresh context.

### AR1. Script Infrastructure
1. `core/scripts/precompact-checkpoint.py` exists — reads working memory, writes `<agent>/session/compact-checkpoint.yaml`
2. `core/scripts/precompact-checkpoint.sh` exists — thin bash wrapper, delegates to .py
3. `core/scripts/postcompact-restore.py` exists — reads checkpoint, prints restoration message to stdout
4. `core/scripts/postcompact-restore.sh` exists — thin bash wrapper, delegates to .py
5. `precompact-checkpoint.py` reads `encoding_queue` from TOP-LEVEL of working-memory.yaml (not inside `slots`)
6. `precompact-checkpoint.py` accumulates across multiple compactions via `compact_count` and `prior_encoding_items`
7. `precompact-checkpoint.py` uses `os.replace()` for atomic write (Windows-safe)
8. `postcompact-restore.py` output is ASCII-safe (no Unicode box-drawing characters)
9. `postcompact-restore.py` prints STATE summary before BLOCKER details (not reversed)
9b. `precompact-checkpoint.py` extracts `retrieval_manifest` from `slots.active_context` as top-level checkpoint field
9c. `postcompact-restore.py` prints RETRIEVAL STATE section with goal_id, nodes, deliberation counts, and UTILIZATION FEEDBACK PENDING alarm when `utilization_pending: true`
9d. Both scripts have `sys.stderr.reconfigure(encoding="utf-8")` boilerplate (Windows cp1252 safety)
9e. `precompact-checkpoint.py` prints `[precompact] saved checkpoint #N: ...` to stderr after successful write
9f. `postcompact-restore.py` prints `[postcompact] restored checkpoint #N: ...` to stderr before stdout injection
9g. `postcompact-restore.py` `log()` uses `file=sys.stderr` — stdout is the agent context injection channel

### AR2. Hook Configuration
10. `.claude/settings.json` has `hooks.PreCompact` with `matcher: "auto"` pointing to `precompact-checkpoint.sh`
11. `.claude/settings.json` has `hooks.SessionStart` with `matcher: "compact"` pointing to `postcompact-restore.sh`
12. Both hooks have `timeout: 10` (seconds)
13. Hooks are in settings (session-level), NOT in skill front matter (which is skill-scoped)

### AR3. Aspirations Loop Integration
14. `aspirations/SKILL.md` has Phase -0.5c between Phase -0.5 and Phase -1.5
15. Phase -0.5c checks for `<agent>/session/compact-checkpoint.yaml` existence
16. Phase -0.5c restores encoding queue from checkpoint if working memory queue was lost
17. Phase -0.5c processes encoding queue with `budget = min(5, len(queue))` — smaller than full consolidation
18. Phase -0.5c uses `last_update_trigger: "compact_encoding"` on tree node updates
19. Phase -0.5c deletes checkpoint after consumption (one-shot)
20. Phase -0.5c restores knowledge_debt and micro_hypotheses if lost during compaction
20b. Phase -0.5c step 2.5 restores retrieval manifest if `utilization_pending == true` (bridges Phase 4.26 across compaction)

### AR4. Supporting Integration
21. `boot/SKILL.md` Phase -1.5 whitelist includes `compact-checkpoint.yaml`
22. `core/scripts/stop-hook.sh` tier 1-3 message mentions checkpoint existence when file is present
23. `CLAUDE.md` signal files table includes `compact-checkpoint.yaml` row
24. `CLAUDE.md` has "Compact Checkpoint Protocol" section
25. `core/config/conventions/session-state.md` has "Compact Checkpoint" section

### AR5. Runtime
26. **Runtime**: After autocompact fires, `<agent>/session/compact-checkpoint.yaml` exists with correct `compact_count`
27. **Runtime**: After compaction, context restoration message appears in agent's context (from SessionStart hook stdout)
28. **Runtime**: After loop re-entry, Phase -0.5c processes encoding items and deletes checkpoint
29. **Runtime**: After multiple compactions in one session, `compact_count` increments and `prior_encoding_items` accumulates
30. **Runtime**: g-001-07 (regular encoding flush) and Phase -0.5c (post-compaction encoding) complement each other without conflict
31. **Runtime**: After autocompact, terminal shows `[precompact]` and `[postcompact]` stderr lines (user-visible hook feedback)

---

## ARa. Pending Background Agent Tracking (Stop Hook Gate 2.5)

Verifies that background agents dispatched via `Agent(run_in_background=true)` are tracked
persistently and the stop hook allows graceful idle-waiting instead of forced loop re-entry.

### ARa1. Script Infrastructure
1. `core/scripts/pending-agents.py` exists — manages `<agent>/session/pending-agents.yaml`
2. `core/scripts/pending-agents.sh` exists — thin bash wrapper, delegates to .py
3. `pending-agents.py` supports subcommands: register, deregister, deregister-team, list, has-pending, prune-stale, clear
4. `pending-agents.py` `has-pending` and `list` run `prune_stale` internally before returning — stale entries self-heal
5. `pending-agents.py` uses `os.replace()` for atomic write (Windows-safe)
6. `pending-agents.py` `deregister` and `deregister-team` delete file when list becomes empty (no orphaned empty files)
7. `pending-agents.py` has `sys.stderr.reconfigure(encoding="utf-8")` boilerplate (Windows cp1252 safety)

### ARa2. Stop Hook Integration
8. `stop-hook.sh` header comment lists Gate 2.5 in gate summary
9. `stop-hook.sh` header comment lists Gate 2.5 in counter lifecycle reset sources
10. `stop-hook.sh` Gate 2.5 is between Gate 2 (stop-loop) and counter increment — never reached if earlier gates pass
11. `stop-hook.sh` Gate 2.5 calls `has-pending` with `2>/dev/null` — script failure falls through (fail-open)
12. `stop-hook.sh` Gate 2.5 clears counter before exit 0 — prevents idle-wait episodes from accumulating toward safety valve
13. `stop-hook.sh` Gate 2.5 counter clear has a comment explaining why it must not be removed

### ARa3. Aspirations Loop Integration
14. `aspirations/SKILL.md` has Phase -0.5a between Phase -0.5 and Phase -0.5c
15. Phase -0.5a checks for `<agent>/session/pending-agents.yaml` existence
16. Phase -0.5a calls `pending-agents.sh list --json` to enumerate pending agents
17. Phase -0.5a deregisters agents whose results are available in context
18. Phase -0.5a calls `has-pending` at end to prune stale and clean up
19. `aspirations/SKILL.md` post-Phase 9.7 calls `pending-agents.sh deregister-team` after team shutdown
20. `aspirations-execute/SKILL.md` calls `pending-agents.sh register` BEFORE each `Agent()` dispatch (crash-safe ordering)

### ARa4. Supporting Integration
21. `boot/SKILL.md` Phase -1.5 whitelist includes `pending-agents.yaml`
22. `precompact-checkpoint.py` saves `pending_agents_count` to checkpoint (informational — file persists on disk)
23. `precompact-checkpoint.py` log summary includes agent count when > 0
24. `postcompact-restore.py` prints PENDING AGENTS warning when `pending_agents_count > 0`
25. `CLAUDE.md` signal files table includes `pending-agents.yaml` row
26. `core/config/conventions/session-state.md` has "Pending Background Agents" section

### ARa5. Runtime
27. **Runtime**: `pending-agents.sh register --id X --team Y --goal Z` creates `<agent>/session/pending-agents.yaml`
28. **Runtime**: `pending-agents.sh has-pending` returns exit 0 when non-stale agents exist, exit 1 when empty/all-stale
29. **Runtime**: `pending-agents.sh deregister --id X` removes entry and deletes file when list empty
30. **Runtime**: `pending-agents.sh deregister-team --team Y` removes all team entries
31. **Runtime**: After staleness timeout (default 10 min), `has-pending` and `list` prune expired agents; `has-pending` returns exit 1 if none remain
32. **Runtime**: Stop hook allows stop (exit 0) when `pending-agents.yaml` has non-stale entries

---

## AS. Routine Outcome Fast-Path (outcome_class)

Recurring goals that find nothing to do (empty inbox, all healthy) skip expensive post-execution phases via `outcome_class`. Two values: `"productive"` (default, full pipeline) and `"routine"` (reduced pipeline). Only recurring goals on success can be routine.

### AS1. Classification (aspirations-execute/SKILL.md)
1. Phase 4-post sets `outcome_class = "productive"` as default before any conditional
2. Only `goal.recurring AND goal_succeeded` can produce `outcome_class = "routine"`
3. Non-recurring goals ALWAYS remain `"productive"` (structural constraint)
4. Failed goals ALWAYS remain `"productive"` (safety rule)
5. If uncertain, remains `"productive"` (fail-open)
6. After Phase 4.1 guardrail check: `IF guardrail_found_issues: outcome_class = "productive"` (guardrails override routine)
7. `outcome_class` is listed in the Phase 4 return values comment

### AS2. Pipeline Gating (aspirations-execute/SKILL.md)
8. Phase 4.25 (experience archival) wrapped with `IF outcome_class == "productive":`
9. Phase 4.2 (domain steps) runs unconditionally (NOT gated by outcome_class)
10. Phase 4.26 (context utilization) feedback loop gated by `outcome_class == "productive"`, but `utilization_pending: false` clearing runs unconditionally for ALL outcomes

### AS3. Pipeline Gating (aspirations/SKILL.md)
11. Phase 6 (spark check) wrapped with `IF outcome_class == "productive":`
12. Phase 7 (aspiration-level check) runs unconditionally (NOT gated)
13. Phase 8 passes `outcome_class` to `/aspirations-state-update`
14. Phase 9 Part A (cadence/lifecycle triggers: `evolution_cadence`, `capability_unlock`) runs unconditionally — NOT gated by `outcome_class`
15. Phase 9 Part B (performance triggers: `accuracy_drop`, `consecutive_losses`, `pattern_divergence`, `stale_strategy`, `context_retrieval_ineffectiveness`) wrapped with `IF outcome_class == "productive":`
16. Phase 9 Part B resets `evolution_cadence.last_fired` when performance triggers fire evolution — prevents cadence from ignoring performance-triggered evolutions
17. Phase 9.5 (learning gate) has explicit routine bypass: `IF outcome_class == "routine": # No tree encoding needed`
18. Phase 9.7 (reflection counter) increments unconditionally — routine goals count toward 5-goal checkpoint

### AS4. State Update (aspirations-state-update/SKILL.md)
19. Skill description mentions `outcome_class` parameter (default: `"productive"`)
20. Body text describes productive (all 9 steps) vs routine (Steps 1-4 + abbreviated Step 7) paths
21. Routine early-return block exists between Step 4 and Step 5 with `IF outcome_class == "routine":`
22. Routine path writes abbreviated journal: `"## {timestamp} — Routine: {goal.title}\nNo new items. Streak: {currentStreak}."`
23. Routine path still updates journal index via `journal-merge.sh` or `journal-add.sh`
24. Routine path has `RETURN` before Step 5 — Steps 5, 6, 8, 9 are explicitly skipped

### AS5. Anti-Drift Safeguard (aspirations/SKILL.md)
25. `routine_streaks[goal.id]` counter increments on each routine outcome
26. After 5 consecutive routine outcomes: `outcome_class` overridden to `"productive"`, counter reset to 0
27. Any productive outcome resets the counter for that goal to 0
28. Counter is ephemeral (in-memory) — autocompact reset fails open (more processing, not less)

### AS6. Batched Execution (aspirations-execute/SKILL.md)
29. Batched execution section mentions `outcome_class` classification per batched goal
30. Phase 5 listed as "always runs", Phase 6 listed as "SKIP if routine"

### AS7. Runtime
31. **Runtime**: After a recurring inbox check with no emails, journal shows `"Routine: Check inbox..."` (not full goal entry)
32. **Runtime**: After a recurring inbox check that FINDS an email, full experience trace is archived
33. **Runtime**: After 5 consecutive routine outcomes for a recurring goal, the 6th runs full pipeline (spark, tree, evolution)

---

## AT. Context Read Deduplication (Convention Caching)

Verifies that the hook-based context deduplication system prevents redundant file reads between autocompact cycles, reducing context bloat and tool-call overhead.

### AT1. Script Infrastructure
1. `core/scripts/context-reads.py` exists with subcommands: gate, record, invalidate, check, check-file, clear, status
2. `core/scripts/context-reads-gate.sh` exists — PreToolUse[Read] hook wrapper, extracts file_path from stdin JSON
3. `core/scripts/context-reads-record.sh` exists — PostToolUse[Read] hook wrapper, extracts file_path from stdin JSON
4. `core/scripts/context-reads-invalidate.sh` exists — PostToolUse[Write,Edit] hook wrapper
5. `core/scripts/context-reads-clear.sh` exists — explicit clear wrapper
6. `core/scripts/load-conventions.sh` exists — batch convention check wrapper for skill Step 0
7. Gate and record wrappers detect partial reads (offset/limit/pages) and bypass tracking
8. `context-reads.py` scope filter tracks: `core/config/**`, `.claude/skills/**`, `world/knowledge/tree/**`, `world/conventions/**`
9. `context-reads.py` gate uses exit 2 (block with message) for tracked files, exit 0 (allow) for untracked
10. `context-reads.py` fail-open: any exception exits 0 (never bricks file access)
11. `context-reads.py` `SystemExit` re-raise preserves exit code 2 from gate (removing this line breaks all dedup)
12. `context-reads.py` path normalization uses forward slashes consistently (Windows compatibility)
13. `context-reads.py` `_read_raw_lines()` guards `TRACKER_PATH is None` (no-agent-bound state — not relying on exception handler)
14. `context-reads.py` `append_tracker()` guards `SESSION_DIR is None` (no-agent-bound state)
15. `context-reads.py` `remove_from_tracker()` guards `TRACKER_PATH is None` (no-agent-bound state)

### AT2. Hook Configuration
13. `.claude/settings.json` has `hooks.PreToolUse` with `matcher: "Read"` pointing to `context-reads-gate.sh`
13b. `.claude/settings.json` has `hooks.PreToolUse` with `matcher: "Skill"` pointing to `context-reads-skill-gate.sh`
14. `.claude/settings.json` has `hooks.PostToolUse` with `matcher: "Read"` pointing to `context-reads-record.sh`
15. `.claude/settings.json` has `hooks.PostToolUse` with `matcher: "Write"` pointing to `context-reads-invalidate.sh`
16. `.claude/settings.json` has `hooks.PostToolUse` with `matcher: "Edit"` pointing to `context-reads-invalidate.sh`
16b. No `PostToolUse` with `matcher: "Skill"` — PostToolUse does not fire for the Skill tool
17. All five hooks have `timeout: 5` (seconds)

### AT2b. Skill Invocation Deduplication
17b. `core/scripts/context-reads-skill-gate.sh` exists — PreToolUse[Skill] combined gate+record
17c. Gate script extracts `tool_input.skill` from stdin JSON, constructs `.claude/skills/<name>/SKILL.md` path
17d. Gate script sources `_platform.sh` BEFORE constructing skill_path (MSYS path conversion)
17e. Gate script uses `gate` subcommand with `&& rc=0 || rc=$?` idiom (captures exit code safely under `set -e`)
17f. Gate script outputs block message to stderr (Claude Code reads stderr for hook messages)
17g. **Runtime**: First `/prime` invocation allowed and recorded; second `/prime` blocked with "already in context"
17h. **Runtime**: After autocompact (tracker cleared), skill re-invokes normally

### AT3. Tracker Lifecycle
18. Tracker file: `<agent>/session/context-reads.txt` — plain text, first line is `#session:<id>` header, remaining lines are paths
19. `precompact-checkpoint.py` deletes tracker file before compaction (post-compact context may not retain file content)
20. `context-reads.py clear` deletes tracker file (idempotent)
21. `context-reads.py invalidate` only removes `world/knowledge/tree/**` paths (convention files are immutable)
22b. Gate and record wrappers extract `session_id` from hook stdin JSON and pass via `--session-id` flag
22c. `read_tracker()` detects stale sessions: if stored session ID differs from current, DELETES tracker and returns empty (self-healing)
22d. `read_tracker()` runs BEFORE `is_in_scope()` in both gate and record — ordering is critical so out-of-scope reads (like working-memory.yaml) still clear stale trackers
22e. **Runtime**: Starting a new Claude Code session with stale tracker from previous session → first Read clears it automatically

### AT4. Batch Convention Loading (Step 0)
22. All 35 skills with non-empty `conventions:` have updated Step 0 instruction referencing `load-conventions.sh`
23. `load-conventions.sh` takes convention names as args, returns absolute paths of unloaded files
24. Empty output from `load-conventions.sh` means all conventions already loaded — agent skips reads

### AT5. Tree Summary Caching
25. `core/scripts/load-tree-summary.sh` exists — generates `_summary.json` from `_tree.yaml` if stale, outputs path if not tracked
26. `load-tree-summary.sh` uses temp file + mv pattern (no corrupt cache on `tree-read.sh` failure)
27. `load-tree-summary.sh` calls `invalidate` after regeneration (clears stale tracker entry so agent re-Reads new content)
28. `load-tree-summary.sh` uses `check-file` (not `check`) to test if `_summary.json` is tracked
29. `_summary.json` is under `world/knowledge/tree/` — in TRACKED_PREFIXES scope, hooks handle it automatically
30. `core/config/conventions/tree-retrieval.md` script table includes `load-tree-summary.sh` entry

### AT5b. Execute Protocol Digest Caching
31. `core/config/execute-protocol-digest.md` exists — compact (~136 line) digest of `aspirations-execute/SKILL.md`
32. Digest covers: Intelligent Retrieval (Steps 1-5c), Outcome Classification, Phase 4.0-4.5
33. `core/scripts/load-execute-protocol.sh` exists — sources `_platform.sh` BEFORE deriving paths (Windows/MSYS2 normalization)
34. `load-execute-protocol.sh` uses `check-file` (not `check`) — same pattern as `load-tree-summary.sh`
35. `core/config/execute-protocol-digest.md` is under `core/config/` — already in TRACKED_PREFIXES scope
36. `aspirations/SKILL.md` Phase 4 calls `load-execute-protocol.sh` (not direct `Read .claude/skills/aspirations-execute/SKILL.md`)
37. `aspirations/SKILL.md` Phase 9.5b (Retrieval Gate) references `load-execute-protocol.sh` for retroactive retrieval
38. **Runtime**: `load-execute-protocol.sh` first call outputs digest path; second call (after Read hook records) outputs nothing
39. **Runtime**: After autocompact, tracker clears and digest is re-read (~136 lines instead of ~636)

### AT6. Known Limitation
40. Subagent Read calls share the tracker file — subagent reads may cause the main agent's gate to block files not in the main agent's context. Self-corrects at compaction.

### AT7. Runtime
41. **Runtime**: After reading a convention file, `context-reads.py status` shows it tracked
42. **Runtime**: Second Read of same convention file is blocked with "Already in context" message
43. **Runtime**: Partial read (with offset or limit) is NOT blocked and NOT recorded
44. **Runtime**: After editing a tree node, that node is removed from tracker (re-read allowed)
45. **Runtime**: `load-conventions.sh aspirations pipeline` returns only unloaded convention paths
46. **Runtime**: After autocompact, tracker is cleared and all files can be re-read
47. **Runtime**: `<agent>/session/working-memory.yaml` reads are never tracked (out of scope)
48. **Runtime**: `load-tree-summary.sh` first call outputs path; second call outputs nothing (cached)
49. **Runtime**: After `tree-update.sh --set` modifies `_tree.yaml`, `load-tree-summary.sh` regenerates and outputs path

---

## AF. Actionable Findings Gate (Step 8.5)

### AF1. Gate Structure
1. `aspirations-state-update/SKILL.md` Step 8.5 exists between Step 8 (tree encoding) and the closing block
2. Step 8.5 only runs when `outcome_class == "productive"` (inside routine early-return block)
3. Step 8.5 has instant-skip when Step 8 did NOT write new insight (no `step_8_wrote_insight`)
4. Investigation goals (`"Investigate:"` prefix) get mandatory binary fallback check even without keyword match

### AF2. Signal Detection
5. Gate checks for 4 keyword patterns: `root_cause`, `bug_identified`, `proposed_fix`, `unimplemented_action`
6. Each pattern uses keyword matching on the compressed insight text from Step 8
6a. Each pattern includes negative filters (resolution keywords within 50-char window) to suppress already-resolved findings
7. Investigation goals with zero keyword hits fall through to binary LLM check ("informational or needs action?")

### AF3. Goal Creation
8. `root_cause`, `bug_identified`, `investigation_finding` signals create Unblock goals (HIGH priority)
9. `proposed_fix`, `unimplemented_action` signals create Idea goals (MEDIUM priority)
10. All created goals include `discovered_by` (source goal ID) and `discovery_type` (signal type) fields
11. Goals created via `aspirations-add-goal.sh` (not direct JSONL edit)
12. Dedup check runs against active goal titles (pending/in-progress) PLUS completed sibling goal titles (same aspiration only)

### AF4. Runtime
13. **Runtime**: After completing an Investigation goal with root cause findings, a new Unblock goal appears
14. **Runtime**: Journal entry mentions "Findings gate" when signals detected
15. **Runtime**: Routine outcomes never trigger the gate (early return at Step 4)
16. **Runtime**: Investigation finding root cause already fixed by sibling goal does NOT create duplicate Unblock goal

---

## ACR. Aspiration Completion Review (Phase 7.5)

### ACR1. Gate Structure
1. Phase 7.5 exists in `aspirations/SKILL.md` between `run_aspiration_spark()` and archival
2. `goals_added_to_completing_asp` initialized to 0 at Phase 7.5 entry, incremented during routing
3. Single archival point at bottom of Phase 7, guarded by `goals_added_to_completing_asp == 0`
4. Skipped/expired goals produce `abandoned_goal` findings (HIGH priority)
5. Step 7.5.2b (Motivation Fulfillment Check) exists between findings scan and Step 7.5.3 early exit
6. Step 7.5.2b can increment `goals_added_to_completing_asp` independently of structural findings

### ACR2. Experience Scanning
5. Each non-recurring goal checked via `experience-read.sh --goal <goal-id>`
6. Summary scanned first (cheap); content_path read only when summary triggers signal
7. Goals without experience entries: verification.outcomes checked for partial signals
8. Keyword patterns include negative filters (e.g., "root cause" + NOT "fixed/resolved")

### ACR3. Dedup and Routing
9. Dedup runs against ALL active aspirations' goal titles (pending/in-progress AND completed statuses)
10. Three-tier routing: A) same aspiration, B) other active aspiration, C) new aspiration
11. Route C invokes `/create-aspiration from-self` with follow-up context

### ACR4. Archival Gate
12. If goals added to completing aspiration: archival deferred, aspiration continues
13. If findings routed elsewhere only: archival proceeds normally
14. Step 7.5.3 early exit checks BOTH `outstanding_findings == 0` AND `goals_added_to_completing_asp == 0`
15. Clean aspirations (zero findings AND motivation fulfilled): single-line output, archival proceeds

### ACR4b. Motivation Fulfillment Check
16. Step 7.5.2b reads `asp.motivation` and evaluates whether completed goals actually fulfilled it
17. Unfulfilled motivation with < 10 completed goals: 1-3 follow-up goals generated
18. Fulfilled motivation OR >= 10 completed goals: passes through to archival
19. Motivation check fires BEFORE Step 7.5.3 early exit — can reopen even structurally clean aspirations

### ACR5. Notification
20. User notified when follow-up goals created (via forged notification skill or pending-questions fallback)
21. Journal entry records findings count, dedup count, routing destinations

### ACR6. Runtime
22. **Runtime**: Aspiration with unresolved root cause in experience → follow-up Unblock goal created before archival
23. **Runtime**: Aspiration with all clean experiences AND fulfilled motivation → "clean completion" output, archived normally
24. **Runtime**: Aspiration with skipped goals → abandoned_goal findings created as HIGH priority
25. **Runtime**: Finding already addressed by a completed goal in another aspiration does NOT create duplicate follow-up goal
26. **Runtime**: Aspiration with clean experiences but unfulfilled motivation → follow-up goals added, archival deferred
27. **Runtime**: Aspiration with 10+ completed goals → motivation check passes through regardless (growth guard)

---

## AV. Mandatory Goal Selection (Post-Compaction Fabrication Guard)

Verifies the agent always runs `goal-selector.sh` before claiming goals are blocked.

### AV1. Convention & Wiring

1. `core/config/conventions/goal-selection.md` exists with Single Authority Rule
2. `aspirations/SKILL.md` conventions list includes `goal-selection`
3. `aspirations/SKILL.md` Phase 2 ELSE block has assertion comment referencing `core/config/conventions/goal-selection.md`
4. `core/scripts/postcompact-restore.py` output includes "MANDATORY: Phase 2 requires `goal-selector.sh`"
5. `CLAUDE.md` Convention Index table includes `goal-selection.md` row

### AV2. Knowledge & Memory

6. `world/knowledge/tree/system.md` has "Loop Integrity" section with goal-selection lesson
7. `world/knowledge/tree/_tree.yaml` system node summary mentions "loop integrity"

### AV3. Runtime

8. **Runtime**: Agent never claims "all goals are blocked" without `goal-selector.sh` output in the same iteration
9. **Runtime**: After autocompact, first loop re-entry runs `goal-selector.sh` in Phase 2 (not narrative assessment)
10. **Runtime**: If `goal-selector.sh` returns candidates, agent executes top-scoring one (no ad-hoc override)

---

## AX. Split-by-Nature Tree Metadata (No Duplication)

Verifies scoring/structural metadata lives exclusively in `_tree.yaml`, with `.md` front matter containing only content-provenance fields. Eliminates the dual-source drift bug where interrupted writes left `.md` and `_tree.yaml` inconsistent.

### AX1. Schema Split

1. `.md` front matter contains ONLY: `topic`, `entities`, `temporal_validity`, `sources`, `last_update_trigger` (plus L1 legacy: `domain`, `level`, `topics`)
2. `.md` front matter does NOT contain: `confidence`, `capability_level`, `accuracy`, `sample_size`, `article_count`, `node_type`, `domain_confidence`, `depth`, `parent`, `last_updated`, `children`
3. `_tree.yaml` nodes contain all scoring fields: `confidence`, `capability_level`, `accuracy`, `sample_size`
4. `_tree.yaml` nodes contain all structural fields: `depth`, `parent`, `node_type`, `children`, `child_count`, `article_count`
5. `core/config/knowledge-conventions.md` documents the minimal `.md` front matter schema with explicit "ONLY in `_tree.yaml`" list
6. `core/config/tree.yaml` `node_file_front_matter` template has only: `topic`, `entities`, `temporal_validity`, `sources`, `last_update_trigger`
7. `core/config/tree.yaml` `l1_file_front_matter` template has only: `domain`, `level`, `topics`, `last_update_trigger`
8. `core/config/knowledge-conventions.md` Interior vs Leaf Invariant says `node_type` lives in `_tree.yaml` (not `.md` front matter)

### AX2. Retrieval Independence

9. `retrieve.py` `build_concept_index()` reads ONLY `entities` from `.md` front matter (no scoring field reads)
10. `retrieve.py` has no backfill code reading `.md` front matter into node dicts
11. `retrieve.py` has protective comment above entity reading: "confidence/capability_level are NOT read from .md"
12. `retrieve.py` `_compute_match_score()` reads `confidence` and `capability_level` from node dict (= `_tree.yaml`) only

### AX3. Phase 2 Follow-Up (Skills Stop Dual-Writing)

13. Skills `/tree`, `/reflect-tree-update`, `/review-hypotheses`, `/aspirations-state-update`, `/aspirations-consolidate`, `/research-topic` updated to stop writing migrated fields to `.md` front matter (Phase 2 complete)

### AX4. Runtime

21. **Runtime**: `tree-read.sh --validate` returns `{"valid": true}` after migration
22. **Runtime**: `retrieve.sh --category <any> --depth medium` returns nodes with correct `confidence` and `capability_level` scores
23. **Runtime**: Spot-check 3 `.md` files — front matter has only KEEP fields (topic, entities, temporal_validity, sources, last_update_trigger)
24. **Runtime**: After `/tree maintain` SPROUT creates a new node, `_tree.yaml` has `capability_level` and `confidence` for it

---

## AY. Unified Work Planning Pipeline

Verifies the Self-anchored alignment check replaces binary boredom, `--plan` flag enables web research + deliberation, and aspiration-level spark de-duplication prevents double generation.

### AY1. Alignment Script

1. `core/scripts/work-alignment.py` exists with `check` subcommand
2. `core/scripts/work-alignment.sh` exists as bash wrapper (same pattern as `goal-selector.sh`)
3. `bash core/scripts/work-alignment.sh check` outputs JSON with 7 fields: `self_priorities`, `covered_priorities`, `uncovered_priorities`, `hours_since_novel_goal`, `recurring_ratio`, `active_aspiration_count`, `goal_category_distribution`, plus `config_thresholds`
4. `self_priorities` are extracted from `<agent>/self.md` numbered items and `##` headers
5. `covered_priorities` uses term matching (50% of 3+ char terms) against active aspiration titles, descriptions, and goal titles
6. `hours_since_novel_goal` uses `firstAchievedAt` for recurring goals with any achievedCount (not just achievedCount==1)
7. `recurring_ratio` is null when `--ranked-goals` is not provided, otherwise a 0.0–1.0 float
8. Script outputs raw metrics only — no boolean decisions, no "should plan" field

### AY2. Config

9. `core/config/aspirations.yaml` has `planning:` section with: `check_interval_goals` (default 3), `novelty_drought_hours` (default 48), `maintenance_threshold` (default 0.70)
10. `core/config/aspirations.yaml` `modifiable:` has bounds for all three planning thresholds
11. Thresholds are described as guidelines for LLM interpretation, not hard gates

### AY3. Create-Aspiration Skill

12. `create-aspiration/SKILL.md` documents `--plan` flag in invocation patterns table
13. Step 2.5 (Self-Grounded Web Research) is `--plan` only — reads Self and tree for research queries
14. Step 3.5 (Structured Deliberation) is `--plan` only — re-reads Self, evaluates candidates, journals the planning cycle
15. Context-Aware Routing section documents auto-detection for `follow_up_context`, `discovery_context`, `forge_context`, `batch_context`
16. Chaining section does NOT list `work-alignment.sh` (alignment data is passed in, not called by this skill)
17. Self is read at 3 points during `--plan`: Step 1 (baseline), Step 2.5 (research), Step 3.5 (deliberation)

### AY4. Aspirations Loop Integration

18. Phase -0.5 initializes `goals_since_last_alignment_check = 0`
19. Phase 0.5 (health check, `active_count < 2`) uses `--plan`
20. Phase 2 alignment check is guarded by `IF ranked_goals is non-empty` (prevents double invocation with no-goals path)
21. Phase 2 alignment check increments counter and runs `work-alignment.sh` when counter >= `check_interval_goals` OR all goals are recurring
22. Phase 2 no-goals fallback uses `--plan`
23. Phase 7 archival (aspiration completed) uses `--plan`
24. Old binary boredom check (lines 306-313 "Creative boredom: only maintenance goals available?") is GONE — replaced by alignment check

### AY5. De-Duplication

25. `aspirations-spark/SKILL.md` aspiration-level spark does NOT invoke `/create-aspiration` (removed lines 313-317)
26. Aspiration-level spark STILL has the 3 reflective questions (lines 305-311)
27. Replacement text says "handled by Phase 7 archival" — points to the single generation site
28. Aspiration completion triggers ONE `/create-aspiration` call (Phase 7), not two

### AY6. Evolve Integration

29. `aspirations-evolve/SKILL.md` step 3 (gap analysis) uses `--plan`

### AY7. Runtime

30. **Runtime**: `bash core/scripts/work-alignment.sh check` returns valid JSON with non-empty `self_priorities`
31. **Runtime**: During a session, the alignment check fires after `check_interval_goals` iterations (check journal for "planning_cycle" events)
32. **Runtime**: After an aspiration completes, only ONE `/create-aspiration` invocation appears in output (not two)

---

## IL. Interaction Learning (/respond Step 7.5)

1. User shares domain insight (e.g. "the way X works is Y because Z") → reasoning bank entry created with `tags: ["user-provided"]`
2. User gives behavioral feedback (e.g. "don't do that, instead do X") → guardrail created with `source: "user-interaction"`
3. User makes testable prediction (e.g. "I think X causes Y") → pipeline hypothesis created at stage `discovered`
4. Simple Q&A (e.g. "what's the status?") → no artifacts created, notability assessment returns immediately
5. Duplicate insight → existing rb entry strengthened (`times_helpful` incremented), no new entry
6. Duplicate feedback → existing guardrail strengthened (`times_triggered` incremented), no new entry
7. Experience record created for notable interactions with type `user_interaction`
8. Journal entry appended for interactions where learning occurred
9. Works in IDLE state (no RUNNING requirement)
10. Skipped in UNINITIALIZED state (no <agent>/ directory)
11. Step 6 fact corrections are NOT re-processed by Step 7.5 (ownership boundary respected)
12. `core/scripts/experience.py` VALID_TYPES includes `user_interaction` and `execution_reflection`

---

## PF. Platform Fix (MSYS Path Conversion)

Verifies the cross-platform bash wrapper pattern that prevents Git Bash on Windows from mangling `/skill-name` arguments.

### PF1. Infrastructure

1. `core/scripts/_platform.sh` exists with `cygpath -m` + `MSYS_NO_PATHCONV=1` inside an `MSYSTEM` guard
2. All bash wrappers that `exec python3` have `source "$REPO_ROOT/core/scripts/_platform.sh"` AFTER `REPO_ROOT` is set and BEFORE the `exec` line
3. No bash wrapper has a bare `export MSYS_NO_PATHCONV=1` line (must go through `_platform.sh`)
4. Scripts using `$SCRIPT_DIR` instead of `$REPO_ROOT` must compute `REPO_ROOT` before sourcing `_platform.sh`

### PF2. _paths.sh Safety Under set -u

5. `_paths.sh` line 26 uses `${AYOAI_AGENT:-}` — safe under `set -u` when AYOAI_AGENT is unset
6. All hook scripts source `_paths.sh` with `set -euo pipefail` — if `_paths.sh` crashes, ALL hooks die silently (stop hook, read dedup, compaction checkpoint)
7. `_paths.sh` sets `AGENT_DIR=""` (not unset) when no agent bound — all downstream `[ -n "$AGENT_DIR" ]` checks work

### PF3. Runtime

8. **Runtime**: `bash core/scripts/aspirations-update-goal.sh <id> skill /some-skill` stores `/some-skill` (not `C:/Program Files/Git/some-skill`)
9. **Runtime**: Hook scripts (`context-reads-gate.sh`, `context-reads-invalidate.sh`) run without path errors
10. **Runtime**: `AYOAI_AGENT="" bash core/scripts/stop-hook.sh < /dev/null` exits 0 (not crash) — stop hook survives no-agent state

---

## FS. Forged Skills Gitignore

1. Every entry in `<agent>/forged-skills.yaml` has a matching `.claude/skills/{name}/` line in `.gitignore`
2. Base skills (e.g., `/tree`) are NOT in `.gitignore` — they are git-tracked
3. `forge-skill/SKILL.md` Step 4 includes adding to `.gitignore`
4. `forge-skill/SKILL.md` `/forge-skill check` audit verifies gitignore entries
5. `forged-skills.yaml` entries with `gap_ref` cross-reference to matching `skill-gaps.yaml` entries with `forged_into`

---

## SC. Stop Consolidation (stop_mode)

### SC1. Skill Definitions

1. `stop/SKILL.md` RUNNING section invokes `/aspirations-consolidate with: stop_mode = true` (not inline mini-consolidation)
2. `stop/SKILL.md` resets in-progress goals to pending BEFORE invoking consolidation
3. `stop/SKILL.md` Chaining section lists `/aspirations-consolidate` as a callee
4. `aspirations-consolidate/SKILL.md` has `## Parameters` section documenting `stop_mode`
5. Steps 6, 7, 8, 8.7, 10 each have `(skip in stop_mode)` annotation and `IF stop_mode != true:` gate
6. Step 8.7 "Store user goal count" is INSIDE the `IF stop_mode != true:` block (not dangling outside)
7. Step 10 has early RETURN when `stop_mode == true` (before the `/boot` invocation)
8. `aspirations/SKILL.md` Session-End Consolidation notes mention /stop as a caller with stop_mode
9. `aspirations/SKILL.md` chaining table lists `/stop (stop_mode)` for aspirations-consolidate

### SC2. Runtime (after a /start → /stop cycle)

10. `<agent>/session/handoff.yaml` exists after /stop (Step 9 ran)
11. `<agent>/session/working-memory.yaml` is reset to template state (Steps 4-5 ran)
12. Journal entry has "## Consolidation" section (Step 3 ran)
13. No "Interrupted Session — Sensory Buffer Snapshot" in journal (old mini-consolidation gone)
15. Next `/start` detects handoff.yaml and enters continuation mode

---

## AZ. Aspiration Grooming (reflect-curate-aspirations)

### AG1. Infrastructure
1. `.claude/skills/reflect-curate-aspirations/SKILL.md` exists with `user-invocable: false`, `parent-skill: reflect`
2. `.claude/skills/reflect/SKILL.md` has `--curate-aspirations` mode entry
3. `.claude/skills/reflect/SKILL.md` `--full-cycle` includes step 1.75 curate-aspirations

### AG2. Candidate Detection
4. Grooming detects goals with `started` set but `achievedCount == 0` (stuck)
5. Grooming detects goals with all `blocked_by` dependencies resolved (stale blockers)
6. Grooming detects goals with expired `deferred_until` (operational stale)
7. Grooming skips recurring goals (they reset naturally)

### AG3. Evidence Cross-Reference
8. Grooming reads `experience-read.sh --category` for each candidate
9. Grooming reads `retrieve.sh --category --depth shallow` for each candidate
10. Grooming checks sibling goals in same aspiration for overlap

### AG4. Decisions
11. Grooming decisions are never automatic — agent must cite specific evidence
12. COMPLETE/SKIP decisions trigger knowledge reconciliation (M.11-12 pattern)
13. Aspiration auto-completion fires when all goals are complete/skipped after grooming

### AG5. Runtime
14. **Runtime**: After `--full-cycle` with stuck goals, grooming_result shows candidates processed
15. **Runtime**: Groomed goals have evolution-log entries with event "aspiration_grooming"

---

## BA. Auto-Memory Disabled (Knowledge Tree is Single Source of Truth)

Verifies that Claude Code's built-in auto-memory is disabled. All persistent knowledge uses the agent's own systems: knowledge tree, guardrails, reasoning bank.

### BA1. Settings

1. `.claude/settings.local.json` has `"autoMemoryEnabled": false` — platform-level kill switch
2. `.claude/settings.local.json` deny array has `"Write(*/.claude/projects/*/memory/*)"` and `"Edit(*/.claude/projects/*/memory/*)"` — permission-level block

### BA2. Rule File

3. `.claude/rules/no-auto-memory.md` exists — instruction-level override
4. Rule file lists 4 persistence destinations: guardrails (`guardrails-add.sh`), knowledge tree (`/tree add`), reasoning bank (`reasoning-bank-add.sh`), working memory
5. Rule file explicitly says "NEVER write to the platform auto-memory directory"

### BA3. Respond Skill

6. `/respond` Step 5 "Remember fact/preference" directive routes to knowledge tree via `/tree add`
7. `/respond` Step 5 "Remember fact/preference" directive explicitly says "NEVER use platform auto-memory"

### BA4. Runtime

8. **Runtime**: When user says "remember X", agent uses `/tree add` or working memory — no Write to `~/.claude/projects/*/memory/`
9. **Runtime**: Auto-memory `MEMORY.md` is NOT injected into conversation context (verify: no `Contents of ...memory\MEMORY.md` system-reminder block)
10. **Runtime**: Agent learning from goal execution writes to guardrails/reasoning bank, not auto-memory files

---

## BC. Settings Split (Framework vs User)

Verifies that framework-critical settings (hooks, deny rules) live in the committed `settings.json`, not in the gitignored `settings.local.json`. Cloners must receive framework protections without copying user-specific config.

### BC1. File Placement

1. `.claude/settings.json` exists and is committed (framework hooks + deny rules)
2. `.claude/settings.local.json` is listed in `.gitignore`
3. `.claude/settings.local.json` is NOT tracked by git (`git ls-files --error-unmatch .claude/settings.local.json` returns error)

### BC2. Framework Deny Rules

4. `settings.json` `permissions.deny` includes deny entries for ALL base skill directories (35 total)
5. `settings.json` `permissions.deny` does NOT contain `*/core/scripts/*` pattern — collides with writable `<agent>/scripts/`
6. `settings.json` `permissions.deny` includes `Edit(*/core/config/*)`, `Edit(*/.claude/rules/*)`, `Edit(*/CLAUDE.md)`
7. `settings.json` `permissions.deny` includes `Edit(*/world/knowledge/tree/_tree.yaml)` and `Edit(*/world/*.jsonl)` and `Edit(*/<agent>/*.jsonl)` — forces script access

### BC3. Framework Hooks

8. `settings.json` has all 6 hooks: PreToolUse[Read], PostToolUse[Read], PostToolUse[Write], PostToolUse[Edit], PreCompact[auto], SessionStart[compact]
9. `settings.local.json` has NO hooks section — all hooks live in `settings.json`
10. Hook commands use relative paths (`bash core/scripts/...`) — work from any clone location

### BC4. User-Only in Local

11. `settings.local.json` contains ONLY: user `allow` permissions, user `deny` (auto-memory), `outputStyle`, `env` vars, `autoMemoryEnabled`
12. No framework deny rules (skill protection, config protection) appear in `settings.local.json`

---

## BE. Aspirations Compact Cache (Context Dedup)

Verifies the aspirations compact cache reduces repeated context loading from `aspirations-read.sh --active` (60KB+ per call, 3-5 calls per iteration).

### BE1. Script Infrastructure

1. `core/scripts/aspirations.py` has `COMPACT_GOAL_KEEP` set and `compact_aspiration()` helper
2. `core/scripts/aspirations.py` `--active-compact` CLI flag in read mutually exclusive group
3. `aspirations-read.sh --active-compact` output has `id`, `title`, `status` but NOT `description`, NOT `verification`
4. `core/scripts/load-aspirations-compact.sh` exists, follows `load-tree-summary.sh` pattern (staleness check, atomic write, invalidate, check-file)
5. `core/scripts/load-aspirations-compact.sh` sources `_platform.sh` (MSYS path fix)
6. `core/scripts/context-reads.py` has `TRACKED_FILES` list containing `aspirations-compact.json` path
7. `core/scripts/context-reads.py` `is_in_scope()` checks `TRACKED_FILES` before `TRACKED_PREFIXES`
8. `core/scripts/context-reads.py` `cmd_invalidate()` allows invalidation of tracked files (early return before tree-prefix check)

### BE2. Convention & Documentation

9. `core/config/conventions/aspirations.md` script table includes `load-aspirations-compact.sh` and `--active-compact`
10. `CLAUDE.md` signal files table includes `aspirations-compact.json`

### BE3. Skill File Migration

11. 16+ skill files use `load-aspirations-compact.sh` instead of `aspirations-read.sh --active`
12. `aspirations/SKILL.md` Phase 2.9 calls `aspirations-read.sh --id <asp-id>` after goal selection — provides full detail (description, verification) for Phase 3-5
13. Phase 2.9 comment says "do NOT remove this or execution runs blind"
14. `boot/SKILL.md` dashboard display (Step 2) KEEPS `aspirations-read.sh --active` (full detail needed)
15. `backlog-report/SKILL.md` KEEPS `aspirations-read.sh --active` (full detail user report)
16. `decompose/SKILL.md` KEEPS `aspirations-read.sh --active` (needs description + verification)

### BE4. Runtime

17. **Runtime**: `aspirations-read.sh --active-compact | wc -c` is significantly smaller than `aspirations-read.sh --active | wc -c`
18. **Runtime**: `load-aspirations-compact.sh` first call returns path; after Read, second call returns empty (dedup working)
19. **Runtime**: After `aspirations-update-goal.sh`, next `load-aspirations-compact.sh` regenerates cache (staleness detected)
20. **Runtime**: After autocompact, compact cache file is re-read (tracker cleared by PreCompact hook)
21. **Runtime**: Phase 2.9 `--id` call provides goal with `description` and `verification` fields for execution

## WM. Working Memory Script API

Verifies the dedicated working memory script layer (`wm-*.sh`) with slot_meta timestamps, mid-session pruning, and template init/reset.

### WM1. Script Infrastructure

1. `core/scripts/wm.py` exists with 8 subcommands: read, set, append, clear, ages, prune, init, reset
2. 8 shell wrappers exist: `wm-read.sh`, `wm-set.sh`, `wm-append.sh`, `wm-clear.sh`, `wm-ages.sh`, `wm-prune.sh`, `wm-init.sh`, `wm-reset.sh`
3. `wm-init.sh` creates `<agent>/session/working-memory.yaml` with 15 slots + `slot_meta` section
4. `wm-reset.sh` produces identical template to `wm-init.sh` (with distinct message)
5. `core/config/memory-pipeline.yaml` `slot_types` list has 15 entries including `active_constraints`, `sensory_buffer`, and `known_blockers`
6. `core/config/memory-pipeline.yaml` has `working_memory_pruning` section with `stale_threshold_minutes`, `evict_threshold_minutes`, `array_limits`, `item_stale_minutes`, `protected_slots`

### WM2. Timestamp Tracking

7. `echo '{"summary":"test"}' | wm-set.sh active_context` → `wm-read.sh --json` shows `slot_meta.active_context.updated_at` is non-null ISO timestamp
8. `echo '{"claim":"test"}' | wm-append.sh micro_hypotheses` → appended item has `_item_ts` field with ISO timestamp
9. `wm-read.sh active_context --json` → `slot_meta.active_context.accessed_at` updates (value returned BEFORE tracking write)
10. `wm-ages.sh --json` returns JSON with `minutes_since_update`, `minutes_since_access`, `update_count`, `item_count` per slot

### WM3. Pruning

11. `wm-prune.sh --dry-run` returns JSON with `pruned_items`, `stale_slots`, `evicted_slots` arrays
12. `wm-clear.sh encoding_queue` produces `[]` not `null` (top-level array handling)
13. `wm-clear.sh known_blockers` produces `[]` not `null` (slot array handling)
14. Protected slots (`known_blockers`, `knowledge_debt`) only prune resolved items

### WM4. Skill Migration (No Direct File Access)

15. Grep `.claude/skills/` for `Read <agent>/session/working-memory` returns 0 matches
16. Grep `.claude/skills/` for `Write <agent>/session/working-memory` returns 0 matches
17. Grep `.claude/skills/` for `Edit <agent>/session/working-memory` returns 0 matches
18. Only legitimate `working-memory.yaml` reference: `boot/SKILL.md` cleanup whitelist (filename string, not access)

### WM5. Convention & Documentation

19. `core/config/conventions/working-memory.md` exists with schema, script API, pruning rules
20. `core/config/conventions/session-state.md` has Working Memory Scripts section with `wm-*.sh` table
21. `CLAUDE.md` Convention Index has `working-memory.md` entry
22. `CLAUDE.md` Tool Usage section mentions `wm-*.sh` for working memory access

### WM6. Integration (Existing Scripts)

23. `core/scripts/precompact-checkpoint.py` imports `read_wm` from `wm` module (not raw `yaml.safe_load`)
24. `core/scripts/goal-selector.py` imports `read_wm` from `wm` module
25. Both scripts still read `slots.known_blockers` correctly (backward-compatible data paths)

### WM7. Runtime

26. **Runtime**: After Phase -1 init, `wm-ages.sh` shows all slots with `update_count: 0` and null timestamps
27. **Runtime**: After goal execution, `wm-ages.sh` shows `active_context` with `update_count > 0`
28. **Runtime**: Phase 11 calls `wm-prune.sh` and reports pruned items (if any)
29. **Runtime**: After consolidation (Step 5), `wm-read.sh --json` shows clean template state
30. **Runtime**: `slot_meta` survives autocompact (checkpoint saves slots, postcompact restores them)

---

## BF. Aspiration Scope System

### BF1. Schema Foundation
1. `core/config/aspirations.yaml` has `aspiration_scopes` with three tiers: sprint, project, initiative
2. `core/config/aspirations.yaml` has `default_scope: project`
3. Each tier has: `description`, `goal_range`, `research_required`, `archival_min_sessions`, `goal_addition_research`
4. `core/scripts/aspirations.py` `VALID_SCOPES = {"sprint", "project", "initiative"}`
5. `core/scripts/aspirations.py` `validate_aspiration()` accepts optional `scope` — rejects invalid values
6. `core/scripts/aspirations.py` `validate_aspiration()` accepts optional `sessions_active` — must be number
7. `core/scripts/aspirations.py` `cmd_complete()` prints MATURITY WARNING when `sessions_active < archival_min_sessions`

### BF2. Create-Aspiration Integration
8. `create-aspiration/SKILL.md` Step 3.7 (Scope Classification) is after Step 3 (Determine Aspirations), before Step 2.5 (Research)
9. Step 2.5 has scope-dependent depth: sprint SKIP, project+ deep research (full page reads)
10. Step 4a.5 (Plan the Plan) for project+ scope: RESEARCH → BUILD → TEST → INTEGRATION → KNOWLEDGE lifecycle
11. Step 4b scope-based goal ranges: sprint 2-5, project 5-15, initiative first 8-12
12. Step 4b CALIBRATION CHECK for project+: at least 1 research goal, 1 test per build, 1 knowledge goal
13. Step 4c MANDATORY verification for project+: every build/change goal MUST have companion test goal

### BF3. Aspirations Loop Integration
14. Phase -0.5 initializes `aspirations_touched_this_session = set()`
15. Phase 7.6 (Maturity Check) adds depth goals when aspiration completes too quickly for scope
16. Phase 2 ambition check counts small aspirations, logs to evolution-log when >= 3
17. Phase 8.1 increments `sessions_active` via read-modify-pipe (once per aspiration per session)
18. `add` sub-command: sprint → /decompose, project+ → `/create-aspiration from-self --plan`

### BF4. Evolution & Spark Integration
19. `aspirations-evolve/SKILL.md` Step 2 merge check: clusters sprint-scope aspirations by category
20. `aspirations-evolve/SKILL.md` Step 3 passes `default_scope: "project"` for gap analysis
21. `aspirations-spark/SKILL.md` sq-013 Step 5.5 quality gate for project+ aspirations (fix/dependency exempt)

### BF5. Runtime
22. **Runtime**: New aspirations have `scope` field set (default: project)
23. **Runtime**: `sessions_active` increments after goal execution
24. **Runtime**: Project-scope aspirations have 5+ goals with research and test companions
25. **Runtime**: Maturity check fires when project-scope aspiration completes in 1 session

---

## BG. Testing Theater Resistance (Verification Quality)

### BG1. Phase 5 Verification Escalation
1. `aspirations/SKILL.md` Phase 5 has "Verification Escalation (empty-checks protocol)" block
2. Block contains three structured questions: Q1 EVIDENCE, Q2 NEGATIVE CHECK, Q3 INTEGRATION SCOPE
3. `all_passed` is set on ALL code paths (no undefined variable): artifact-confirms → true, artifact-fails → false, no-artifact → false
4. Q2 evaluation is gated by `IF all_passed AND Q2 is vague` — already-failed goals skip Q2
5. Verification gap signal writes to `wm-append.sh sensory_buffer` with `verification_gap` key

### BG2. Reflect-Execution Verification Gap Signal
6. `reflect-execution/SKILL.md` Step 0.5 header says "Four structural checks" (not Three)
7. Signal #4 `verification_gap` exists in the notability gate after signal #3
8. Signal #4 reads sensory_buffer for Phase 5 escalation flags
9. Signal #4 also fires independently when code edits occur without test execution

### BG3. Integration Path Spark Question (sq-015)
10. `spark-questions-read.sh --active` returns sq-015 with status "active"
11. `aspirations-spark/SKILL.md` has "Integration Path Coverage Spark Handler" section for sq-015
12. sq-015 handler step 1 skips non-code goals immediately
13. sq-015 handler uses IF/ELIF/ELSE: create investigation goal / create idea goal / no spark
14. `sparks_generated` increments ONLY when step 3 or 4 creates a goal (not unconditionally)

### BG4. Goal Template Verification Hints
15. `core/config/aspirations.yaml` — all templates with `checks: []` have `verification_hint` field
16. Templates with actual checks (research, test_automated, test_regression, documentation, knowledge_encoding, spike) do NOT have `verification_hint`

### BG5. Structural Utilization Feedback
17. `aspirations-execute/SKILL.md` Phase 4.26 does NOT contain "actually helped" text
18. Phase 4.26 guardrail/rb section uses structural check: `item.id appears in retrieval_manifest.influence text`
19. Phase 4.26 tree node section uses same structural pattern (no vague "informed a decision" text)
20. **Runtime**: After 1+ RUNNING session, guardrails show non-zero `times_helpful` or `times_noise`
21. **Runtime**: Phase 5 escalation fires for goals with empty checks and produces structured Q1/Q2/Q3 answers

---

## BH. Tree Utility Tracking

1. `core/config/tree.yaml` `_tree_entry_node` template has `retrieval_count`, `times_helpful`, `times_noise`, `utility_ratio` fields
2. `core/config/tree.yaml` has `pruning` section with `distill_utility_threshold`, `distill_min_retrievals`, `retire_sessions_unused`
3. `core/scripts/tree.py` `apply_defaults()` fills `times_helpful: 0`, `times_noise: 0`, `utility_ratio: 0.0`, `retrieval_count: 0`
4. `core/scripts/tree.py` `cmd_increment()` auto-recomputes `utility_ratio` when `times_helpful`, `retrieval_count`, or `times_noise` change
5. `core/scripts/tree.py` `cmd_batch()` has the same auto-recomputation in its increment branch
6. `tree-read.sh --node <key>` returns utility fields with correct defaults
7. `tree-read.sh --active-content <key>` returns only `## Decision Rules` and `## Verified Values` sections (prefix match)
8. `tree-read.sh --active-content <key>` returns `null` for nodes without those sections
9. `tree-read.sh --distill-candidates` returns leaf nodes with `utility_ratio < threshold AND retrieval_count >= min`
10. `aspirations-execute/SKILL.md` Phase 4.26 has tree node utilization feedback loop (increments `times_helpful` or `times_noise`)
11. `core/config/execute-protocol-digest.md` Step 3 has effort-gated retrieval (`--active-content` for minimal/standard effort)
12. `tree/SKILL.md` has `/tree distill <key>` sub-command
13. `tree/SKILL.md` maintain has operation 1.75 DISTILL (low-utility nodes) and 5.5 RETIRE (never-retrieved nodes)
14. `aspirations-consolidate/SKILL.md` Step 6 lists all 8 ops: DECOMPOSE, REDISTRIBUTE, DISTILL, SPLIT, SPROUT, MERGE, PRUNE, RETIRE
15. `aspirations-state-update/SKILL.md` Step 8 has step 8e Decision Rules extraction
16. `aspirations-consolidate/SKILL.md` Step 2 has step 2d.5 Decision Rules during encoding
17. `reflect-curate-memory/SKILL.md` has Step 2.5 (Decision Rule auto-promotion to guardrails) and Step 2.6 (tree node utility curation)
18. `core/config/conventions/decision-rules.md` documents format, when to write, auto-promotion criteria
19. `world/knowledge/archive/` directory exists for distilled/retired node content
20. `core/scripts/tree.py` front matter parser uses counter (not toggle) — body `---` separators do not misfire
21. **Runtime**: `tree-read.sh --validate` returns `valid: true`
22. **Runtime**: After 3+ sessions, some nodes have `utility_ratio > 0` (Phase 4.26 writing back)
23. **Runtime**: `tree-read.sh --distill-candidates` returns candidates once utility data accumulates

---

## BJ. MR-Search Integration (Episode Chaining, Exploration Mode, Temporal Credit)

### Episode Chaining (Priority 1)
1. `core/config/aspirations.yaml` has `episode_chaining` section with `max_episodes_per_goal`, `chain_on_outcomes`, `context_zone_override`
2. `core/config/aspirations.yaml` `chain_on_outcomes` contains only `"failed"` (NOT `"blocked"` or `"surprise_gt_7"` — those were removed as incorrect targets)
3. `core/config/aspirations.yaml` `context_zone_override.tight` is `0` (disabled, not 1)
4. `core/config/aspirations.yaml` `modifiable` has `max_episodes_per_goal` with bounds
5. `core/config/memory-pipeline.yaml` `slot_types` includes `episode_chain`
6. `core/config/memory-pipeline.yaml` `working_memory_slots` has `episode_chain` template with `goal_id`, `max_episodes`, `current_episode`, `episodes` array
7. `core/config/conventions/goal-schemas.md` has "Episode Chaining Fields" section with `episode_history` schema
8. `aspirations-execute/SKILL.md` has Phase 4-chain between Phase 4-post and Phase 4.0
9. `aspirations-execute/SKILL.md` Phase 4-chain has infrastructure guard: `IF result is INFRASTRUCTURE_UNAVAILABLE or RESOURCE_BLOCKED → chain_trigger = false`
10. `aspirations-execute/SKILL.md` Phase 4-chain clears stale episode chain in ELSE branch (success or no-chain path)
11. `aspirations-state-update/SKILL.md` Step 8 has episode chain encoding block that reads `episode_history` from context (not via nonexistent script)
12. **Runtime**: IF a goal failed during the test AND context_zone was normal or fresh:
    Check: journal mentions "EPISODE CHAIN" with attempt count
    Check: goal has `episode_history` field with at least 2 entries
    Check: working memory `episode_chain` is null after goal completes (cleaned up)
    Check: tree node has episode progression content (approach evolution encoded)

### Exploration Mode (Priority 3)
13. `core/config/aspirations.yaml` has `exploration_mode` section with `auto_designate_below_capability`, `max_exploration_fraction`, `shield_from`, `retain_in`
14. `core/config/aspirations.yaml` `modifiable` has `auto_designate_below_capability` and `max_exploration_fraction` with bounds
15. `core/config/conventions/goal-schemas.md` has "Execution Mode Field" section documenting `execution_mode`: `"standard"` | `"exploration"`
16. `aspirations/SKILL.md` Phase 2.5 has exploration mode auto-designation block that reads `exploration_mode` config
17. `aspirations/SKILL.md` Phase 2.5 auto-designation checks session exploration fraction cap before designating
18. `aspirations-state-update/SKILL.md` Step 5 has exploration mode gate: skips `accuracy_drop` and `consecutive_losses` triggers
19. `aspirations-state-update/SKILL.md` Step 8.8 adds `exploration_mode: true` to imp@k snapshot when applicable
20. **Runtime**: IF any goal was executed in a category with capability_level < 0.30:
    Check: goal has `execution_mode: "exploration"` set
    Check: journal does NOT show evolution triggers firing for that goal's failure
    Check: tree node was still updated despite exploration outcome (knowledge retained)

### Reflection Quality Tracking (Priority 2)
21. `core/config/meta.yaml` `reflection_strategy` initial state has `reflection_quality_log: []`
22. `core/config/meta.yaml` `reflection_quality_log` comment says entries are `{reflection_id, downstream_goal, helpful}` (matches what Phase 4.26 writes)
23. `core/config/meta.yaml` `reflection_strategy` has `reflection_effectiveness_by_type` with execution/hypothesis/spark keys, each with `total`, `effective`, `rate`
24. `aspirations-spark/SKILL.md` Phase 6.5 reasoning bank creation includes `source_reflection_id` field
25. `aspirations-spark/SKILL.md` Phase 6.5 guardrail creation includes `source_reflection_id` field
26. `aspirations-execute/SKILL.md` Phase 4.26 has reflection quality tracking block that reads `source_reflection_id` from items
27. `aspirations-execute/SKILL.md` Phase 4.26 reflection quality write uses read → append → `meta-set.sh` pattern (NOT `--append` flag which doesn't exist)
28. `reflect/SKILL.md` Step 0.3 has reflection quality-driven depth allocation with `total >= 3` guard (prevents penalizing types with no data)
29. `reflect/SKILL.md` Step 5.8 consolidation uses `helpful` field (NOT `led_to_improvement`) and derives type from `reflection_id`
30. `core/config/conventions/meta-strategies.md` integration table includes Step 5.8 and Phase 4.26 rows
31. **Runtime**: IF any reasoning bank entry was created AND later retrieved as helpful:
    Check: `meta/reflection-strategy.yaml` `reflection_quality_log` has at least one entry
    Check: entry has `reflection_id`, `downstream_goal`, `helpful` fields (no other schema)

### Temporal Credit Propagation (Priority 4)
32. `core/config/conventions/experience.md` has "Temporal Credit Fields" section with `enabled_by` and `temporal_credit` schema
33. `core/config/conventions/experience.md` notes that `source_reflection_id` belongs on reasoning bank/guardrail records (NOT experience records)
34. `aspirations-execute/SKILL.md` has Phase 4.27 (Causal Enabler Scan) positioned AFTER Phase 4.26 (not before — helpfulness data must be available)
35. `aspirations-execute/SKILL.md` Phase 4.27 applies structural helpfulness criteria (same test as Phase 4.26, not a separate flag)
36. `aspirations-execute/SKILL.md` Phase 4.25 experience JSON includes `enabled_by: []` and `temporal_credit: 0.0` fields
37. `aspirations-state-update/SKILL.md` Step 8.9 propagates credit with `gamma = 0.9` discount per temporal distance unit
38. `aspirations-state-update/SKILL.md` Step 8.9 has minimum credit threshold (`> 0.01`) to avoid noise
39. `reflect-extract-patterns/SKILL.md` Step 3 has "Enabling Strategy Detection" subsection that filters experiences by `temporal_credit > 0.1`
40. `reflect-extract-patterns/SKILL.md` enabling strategies have `strategy_type: "enabling"` (distinct from direct strategies)
41. **Runtime**: IF a goal succeeded using retrieved items from a prior goal's execution:
    Check: experience record has `enabled_by` with at least one entry
    Check: enabler entry has `experience_id`, `relationship`, `temporal_distance`
    Check: enabler experience record has `temporal_credit > 0`

### Relative Advantage Scoring (Priority 5)
42. `aspirations-state-update/SKILL.md` Step 8.10 reads from `meta-read.sh improvement-velocity.yaml` (NOT from experience records)
43. `aspirations-state-update/SKILL.md` Step 8.10 requires `>= 3` similar snapshots before computing advantage
44. **Runtime**: IF 3+ goals completed in the same category:
    Check: experience records have `relative_advantage` field set

### Adaptive Reflection Depth (Priority 6)
45. `core/config/meta.yaml` `reflection_strategy` has `adaptive_depth` with `scale_on_surprise`, `scale_on_chain_length`, `scale_on_importance`, `max_depth_multiplier`
46. `reflect/SKILL.md` Step 0.3 adaptive depth block has explicit guard: `AND goal context is available` (skipped in `--full-cycle`)
47. `reflect/SKILL.md` Step 0.3 adaptive depth uses `min()` to cap at `max_depth_multiplier` for each scaling factor

### Known Design Limitations (verify these are NOT bugs)
48. `reflection_effectiveness_by_type` only tracks spark reflections (Phase 6.5 tags artifacts). Hypothesis and execution reflection sub-skills do not yet tag their artifacts with `source_reflection_id` — this is a known limitation, not a bug. Step 0.3 guards against this with `total >= 3`.
49. Episode chaining only fires on `"failed"` outcomes. Infrastructure failures (`INFRASTRUCTURE_UNAVAILABLE`, `RESOURCE_BLOCKED`) are explicitly excluded — Phase 4.0's blocker protocol handles those.
50. `max_episodes_per_goal: 3` in config is only the fallback when `context_zone` is undefined. The runtime max is determined by `context_zone_override` values (tight:0, normal:1, fresh:2).

---

## BI. Cognitive Judgment Quality

1. `.claude/rules/verify-before-assuming.md` has 4 rules: multi-signal, cost-proportional, infrastructure-specific, silent failure awareness
2. `core/config/conventions/negative-conclusions.md` documents verification tiers, independent signals, silent failure catalog
3. `core/config/conventions/infrastructure.md` "Verify Before Assuming" section title says "Infrastructure Rules" and cross-references `negative-conclusions.md`
4. `aspirations-execute/SKILL.md` Phase 4.0 has negative conclusion gate before CREATE_BLOCKER with `required_signals = 2`
5. `aspirations-execute/SKILL.md` Phase 4.0 negative conclusion gate has retry guard (`AND NOT retry_attempted`) preventing infinite loop
6. `aspirations-execute/SKILL.md` Phase 4.0 has contradiction path (infra up but skill fails after retry → skip blocker, proceed)
7. `aspirations/SKILL.md` Phase 5 Q2 is a hard gate: if agent names a failure mode but didn't check, must check now
8. `aspirations/SKILL.md` Phase 5 Q2 hard gate can block completion (set `all_passed = false`) if named check fails
9. `aspirations/SKILL.md` Phase 9.7 has Q4 conclusion audit: re-verify stale blocking conclusions, flag weak evidence
10. `core/config/memory-pipeline.yaml` has `conclusions` in `slot_types` list and `max_slots: 16`
11. `core/scripts/wm.py` has `conclusions` in both `ARRAY_SLOTS` and `DEFAULT_SLOT_TYPES`
12. `core/config/conventions/working-memory.md` documents `conclusions` slot
13. `aspirations-consolidate/SKILL.md` Step 2.7 sweeps conclusions for judgment quality stats and encodes lessons inline
14. `aspirations-consolidate/SKILL.md` Step 2.7 encodes wrong-conclusion lessons directly to tree (not queued — would be lost on wm-reset)
15. **Runtime**: Working memory has `conclusions: []` slot after init/reset
16. **Runtime**: After a session with infrastructure interactions, `conclusions` slot contains entries with evidence and weights

---

## BK. First-Principles Thinking

1. `.claude/rules/first-principles.md` exists with "When To Apply" scope limiter and 4 numbered rules
2. `.claude/rules/first-principles.md` has "Anti-patterns" section including "Applying first-principles to trivial/routine goals"
3. `core/config/spark-questions.yaml` `seed_candidates` has `sq-c07` with `category: first_principles`
4. `core/config/spark-questions.yaml` `initial_state.candidates` has matching `sq-c07` with identical text
5. `core/config/meta.yaml` `improvement_instructions` initial state has "## First-Principles Analysis (When Warranted)" section
6. `core/config/meta.yaml` improvement instructions first-principles section has "Apply when System 2 is active" guard
7. `aspirations-execute/SKILL.md` episode chain mini-reflection says "four questions" (not "three")
8. `aspirations-execute/SKILL.md` question 4 mentions "Strip to ground truth and rebuild approach"
9. `reflect-hypothesis/SKILL.md` Step 7 has first-principles escalation gated by `root cause is "model-error" or "overconfidence"`
10. `reflect-hypothesis/SKILL.md` first-principles escalation step 4 says "this becomes the guardrail/reasoning bank content"
11. **Runtime**: IF `meta/improvement-instructions.md` exists (agent has booted):
    Check: file retains "First-Principles Analysis" section (not removed by agent evolution)
12. **Runtime**: IF any hypothesis was reflected with root cause "model-error" or "overconfidence":
    Check: journal or reasoning bank entry references inherited assumptions or first-principles analysis

---

## BL. SkillNet Integration (Relation Graph, Quality Evaluation, Experience Mining)

### BL1. Skill Relation Graph

1. `core/config/skill-relations.yaml` exists with `config.relation_types` defining 4 types: similar_to, compose_with, belong_to, depend_on
2. `core/config/skill-relations.yaml` `config` section has `co_invocation_log_cap` and `discover_min_co_occurrences` (single source of truth for script thresholds)
3. `core/config/skill-relations.yaml` `relations` list has entries for all sub-skill belong_to relations (aspirations-execute→aspirations, reflect-hypothesis→reflect, etc.)
4. `core/config/skill-relations.yaml` `relations` list has compose_with chains for boot→prime→aspirations and aspirations-execute→aspirations-spark→aspirations-state-update
5. `core/config/skill-relations.yaml` has `initial_state` section with `forged_relations: []` and `co_invocation_log: []`
6. `core/scripts/skill-relations.sh` exists and delegates to `skill-relations.py`
7. `core/scripts/skill-relations.py` exists with subcommands: read, add, co-invoke, discover
8. `core/scripts/skill-relations.py` reads `co_invocation_log_cap` and `discover_min_co_occurrences` from config (not hardcoded)
9. Bash: `skill-relations.sh read --composable boot` → returns JSON array containing prime and aspirations
10. Bash: `skill-relations.sh read --similar research-topic` → returns JSON array containing replay
11. Bash: `skill-relations.sh read --similar replay` → returns same relation (symmetric lookup works)
12. `core/scripts/init-agent.sh` extracts `initial_state` from `skill-relations.yaml` to `<agent>/skill-relations.yaml`

### BL2. Five-Dimension Skill Quality Evaluation

13. `core/config/conventions/skill-quality.md` exists with dimension table (Safety, Completeness, Executability, Maintainability, Cost-awareness)
14. `core/config/conventions/skill-quality.md` documents `skill-evaluate.sh` script API (score, read, report, underperforming)
15. `core/scripts/skill-evaluate.sh` exists and delegates to `skill-evaluate.py`
16. `core/scripts/skill-evaluate.py` exists with subcommands: score, read, report, underperforming
17. `core/scripts/skill-evaluate.py` `DIMENSIONS` list has exactly 5 entries matching convention file
18. `core/scripts/skill-evaluate.py` `GRADE_MAP` maps good→1.0, average→0.5, poor→0.0
19. `core/scripts/skill-evaluate.py` reads weights from `meta/skill-quality-strategy.yaml` (not hardcoded)
20. `core/scripts/skill-evaluate.py` evaluation entries use key `"overall"` for weighted aggregate score
21. Bash: `skill-evaluate.sh report` → returns JSON with `skills`, `summary`, `alerts` structure
22. `core/config/meta.yaml` `strategy_schemas` has `skill_quality` entry with `file: "meta/skill-quality-strategy.yaml"`
23. `core/config/meta.yaml` `initial_state` has `skill_quality_strategy` with `dimension_weights` summing to 1.0
24. `core/scripts/meta-init.py` `FILE_MAP` has `skill_quality_strategy` entry
25. `core/scripts/init-meta.sh` creates `meta/skill-quality.yaml` with `{last_updated: null, skills: {}}`
26. CLAUDE.md Convention Index has `skill-quality.md` entry
27. CLAUDE.md Core Systems table has `Skill quality` row

### BL3. Skill Quality Integration in Aspirations Loop

28. `aspirations-state-update/SKILL.md` has Step 8.76 (Skill Quality Assessment) between Steps 8.75 and 8.8
29. `aspirations-state-update/SKILL.md` Step 8.76 calls `skill-evaluate.sh score` with all 5 dimension args
30. `aspirations-state-update/SKILL.md` Step 8.76 skips for routine outcomes and goals with no linked skill
31. `aspirations-state-update/SKILL.md` description includes "Step 8.76 Skill Quality Assessment"

### BL4. Experience-to-Skill Mining

32. `core/config/skill-gaps.yaml` has `experience_mining` section with `min_cluster_size`, `scan_window_days`, `max_gaps_per_scan`
33. `core/config/skill-gaps.yaml` has `quality_thresholds` section with `retirement_floor`, `review_floor`, `dimension_floor`, `min_evaluations`
34. `aspirations-consolidate/SKILL.md` has Step 7.5 (Experience-to-Skill Mining) between Steps 7 and 8
35. `aspirations-consolidate/SKILL.md` Step 7.5 references `experience_mining.max_gaps_per_scan` cap
36. `aspirations-consolidate/SKILL.md` Step 8 (Skill Health Report) calls `skill-evaluate.sh report`, `skill-relations.sh discover`, `skill-analytics.sh recommendations`
37. `aspirations-consolidate/SKILL.md` description includes "experience-to-skill mining"

### BL5. Automated Skill Curation

38. `aspirations-evolve/SKILL.md` has Step 9.5 (Skill Curation) between forge check and Pattern Signature Calibration
39. `aspirations-evolve/SKILL.md` Step 9.5 calls `skill-evaluate.sh underperforming`
40. `aspirations-evolve/SKILL.md` Step 9.5 distinguishes forged skills (retire/improve) from base skills (flag for user)
41. `aspirations-evolve/SKILL.md` description includes "skill curation"

### BL6. Dynamic Skill Routing

42. `core/scripts/goal-selector.py` has `SKILL_QUALITY_PATH` constant pointing to `meta/skill-quality.yaml`
43. `core/scripts/goal-selector.py` has `skill_affinity` criterion (criterion #12) that reads from `meta/skill-quality.yaml`
44. `core/scripts/goal-selector.py` `skill_affinity` defaults to 0 (neutral) when skill has no evaluations
45. `core/config/meta.yaml` `initial_state.goal_selection_strategy.weights` has `skill_affinity: 0.4`
46. Bash: `goal-selector.sh select 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print('OK' if not d or 'skill_affinity' in d[0].get('raw',{}) else 'MISSING')"` → verify skill_affinity appears in raw output
47. `decompose/SKILL.md` has "Skill Inference Refinement (Relation Graph)" section after Skill Inference Table
48. `decompose/SKILL.md` refinement calls `skill-relations.sh read --composable` and `--similar`
49. `decompose/SKILL.md` refinement calls `skill-evaluate.sh read --all --summary` for quality comparison

### BL7. Co-Invocation Logging

50. `aspirations-execute/SKILL.md` has Phase 4.28 (Skill Co-Invocation Logging) between Phases 4.27 and 4.5
51. `aspirations-execute/SKILL.md` Phase 4.28 calls `skill-relations.sh co-invoke` with goal and skills args
52. `aspirations-execute/SKILL.md` Phase 4.28 only logs when 2+ skills were involved

### BL8. Forge-Skill Dedup Enhancement

53. `forge-skill/SKILL.md` Constraints section mentions `skill-relations.sh read --similar` for dedup check
54. `forge-skill/SKILL.md` dedup constraint says to "strengthen that skill or register a compose_with relation instead"

### BL9. Skill Analytics

55. `core/scripts/skill-analytics.sh` exists and delegates to `skill-analytics.py`
56. `core/scripts/skill-analytics.py` exists with subcommands: reuse-report, co-invocation, coverage, recommendations, trend
57. `core/scripts/skill-analytics.py` reads evaluation scores using key `"overall"` (matches skill-evaluate.py writer)
58. Bash: `skill-analytics.sh reuse-report` → returns JSON with `skills` and `summary` structure
59. Bash: `skill-analytics.sh recommendations` → returns JSON with `forge`, `retire`, `improve`, `substitute` arrays

### BL10. Cross-Script Data Consistency

60. `skill-evaluate.py` writes entries with key `"overall"` — `skill-analytics.py` reads `"overall"` — `goal-selector.py` reads `aggregate.overall` — ALL match
61. `skill-relations.py` writes to `<agent>/skill-relations.yaml` under `forged_relations` and `co_invocation_log` — `skill-analytics.py` reads same keys
62. `core/config/meta.yaml` transfer section `exportable_strategies` includes `skill_quality.dimension_weights` and `skill_quality.learned_relations`
63. `core/config/conventions/meta-strategies.md` file layout table includes `meta/skill-quality-strategy.yaml`

### BL11. Runtime

64. **Runtime**: IF agent ran 5+ goals after SkillNet deployment:
    Bash: `skill-evaluate.sh report` → verify `total_skills_evaluated > 0` (Phase 8.76 fired)
    Check: `meta/skill-quality.yaml` has entries under `skills` with `evaluations[]` and `aggregate`
65. **Runtime**: IF consolidation ran:
    Check: `<agent>/skill-relations.yaml` `co_invocation_log` has entries (Phase 4.28 fired)
    Check: journal mentions "Skill Health Report" or "skill-evaluate" (Step 8 used new scripts)
66. **Runtime**: IF evolve Step 9.5 ran:
    Check: journal mentions "SKILL CURATION" or "underperforming" if any skills below threshold
67. **Runtime**: Bash: `goal-selector.sh select 2>/dev/null` → verify `raw.skill_affinity` present on each result
