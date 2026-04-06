# Meta-Strategies Convention

Meta-strategies live in `meta/` (top-level, alongside `core/`, `world/`, and `<agent>/`).
The agent reads these to guide improvement procedures and writes to them during evolution and reflection.

## File Layout

| File | Purpose |
|------|---------|
| `meta/meta.yaml` | Master meta-state: imp@k averages, evaluation count, session tracking |
| `meta/goal-selection-strategy.yaml` | Goal scoring weights + selection heuristics |
| `meta/reflection-strategy.yaml` | Reflection triggers, depth allocation, ROI tracking, quality tracking, adaptive depth |
| `meta/evolution-strategy.yaml` | Meta-meta: how to evaluate and modify strategies |
| `meta/aspiration-generation-strategy.yaml` | Aspiration generation heuristics and scope preferences |
| `meta/encoding-strategy.yaml` | Memory encoding priority rules and compression preferences |
| `meta/skill-quality-strategy.yaml` | Skill quality dimension weights and evaluation thresholds |
| `meta/improvement-instructions.md` | Agent's self-authored improvement procedure document |
| `meta/improvement-velocity.yaml` | imp@k entries and rolling averages |
| `meta/meta-log.jsonl` | Append-only strategy change audit log (script-access only) |
| `meta/experiments/active-experiments.yaml` | Currently running A/B experiments |
| `meta/experiments/completed-experiments.yaml` | Historical experiment results |
| `meta/transfer-profile.yaml` | Exportable meta-knowledge for cross-domain transfer |
| `meta/transfer/_index.yaml` | Registry of exported transfer bundles |
| `meta/backpressure.yaml` | Backpressure gate: active monitors + rollback history (AutoContext) |
| `meta/dead-ends.jsonl` | Dead end registry: proven-to-fail approaches (script-access only, AutoContext) |
| `meta/credit-assignment.yaml` | Credit assignment: meta-change attribution scores (AutoContext) |
| `meta/strategy-generations.yaml` | Strategy generations: parameter config → performance mapping (AutoContext) |

## Script-Based Access

| Script | Purpose | Output |
|--------|---------|--------|
| `meta-read.sh <file> [--field <dotpath>] [--json]` | Read a meta-strategy file or field | YAML or JSON |
| `meta-set.sh <file> <dotpath> <value>` | Set a field (bounds-validated, auto-logged) | Confirmation |
| `meta-log-append.sh` | Append JSON from stdin to meta-log.jsonl | Confirmation |
| `meta-impk.sh compute --window <k> --metric <name>` | Compute improvement velocity | JSON |
| `meta-impk.sh snapshot --goal-id <id> --learning-value <v>` | Record learning value for a goal | Confirmation |
| `meta-experiment.sh create --strategy <file> --field <dotpath> --baseline <v> --variant <v>` | Create A/B experiment | JSON |
| `meta-experiment.sh status [--id <exp-id>]` | Check experiment status | JSON |
| `meta-experiment.sh resolve --id <exp-id>` | Resolve experiment (adopt/revert) | JSON |
| `meta-transfer.sh export --output <path>` | Export transferable strategies | YAML bundle |
| `meta-transfer.sh import --input <path> [--dry-run]` | Import strategies from bundle | JSON |
| `meta-backpressure.sh monitor --change-id <id> --file <f> --field <dp> --old <v> --new <v> --baseline <k>` | Create backpressure monitor | JSON |
| `meta-backpressure.sh check --learning-value <v>` | Check monitors, return rollback actions | JSON |
| `meta-backpressure.sh graduate --change-id <id>` | Graduate a monitor (sustained improvement) | JSON |
| `meta-backpressure.sh status` | List active monitors + rollback history | JSON |
| `meta-backpressure.sh cooldown-check [--window <N>]` | Check fields in cooldown (rolled back recently) | JSON |
| `meta-dead-ends.sh add` | Register dead end (JSON from stdin) | JSON |
| `meta-dead-ends.sh check --file <f> --field <dp> --value <v>` | Check if value hits a dead end | JSON |
| `meta-dead-ends.sh read [--active] [--category <cat>]` | List dead ends | JSON |
| `meta-dead-ends.sh increment <id>` | Bump prevention counter | JSON |
| `meta-generations.sh open` | Start new strategy generation | JSON |
| `meta-generations.sh close [--metrics '<json>']` | Close current generation | JSON |
| `meta-generations.sh update --learning-value <v>` | Update generation metrics | JSON |
| `meta-generations.sh status` | Current generation + peak comparison | JSON |
| `meta-generations.sh history [--top N]` | Generations sorted by performance | JSON |

## Modification Protocol

### When the agent may modify meta-strategies:
1. During `/aspirations-evolve` Step 0.7 (Meta-Strategy Evaluation)
2. During `/reflect-on-self` Patterns mode, Step 5 (Meta-Strategy Synthesis)
3. During experiment resolution (after min_duration_goals data collected)
4. During `/aspirations-spark` sq-c06 handler (meta-level signal capture to meta-log)

### Mandatory logging:
Every strategy change MUST be logged to `meta/meta-log.jsonl` via `meta-log-append.sh`.
Each log entry receives a sequential `meta_change_id` (`mc-NNN`) automatically assigned by `meta-set.sh`:
```json
{"date": "<ISO8601>", "meta_change_id": "mc-001", "strategy_file": "<file>", "field": "<dotpath>", "old_value": "<old>", "new_value": "<new>", "reason": "<justification>", "evidence": ["<experience/hypothesis IDs>"], "imp_k_at_change": <snapshot>}
```

### Bounds enforcement:
- Goal selection weights: [0.0, 3.0] per `core/config/meta.yaml` strategy_schemas
- Trigger sensitivity: [0.1, 5.0]
- Reflection depth_allocation: must sum to 1.0
- Custom criteria: max 5 items
- All bounds enforced by `meta-set.sh` (reads `core/config/meta.yaml` for validation)

## Curriculum Gating

| Action | Gate | Stage |
|--------|------|-------|
| Read meta-strategies | No gate | Always |
| Append to meta-log.jsonl (signal capture) | No gate | Always |
| Append to improvement-velocity.yaml (data) | No gate | Always |
| Append to reflection-strategy.yaml roi_history (data) | No gate | Always |
| Edit meta-strategy files | `allow_meta_edits` | cur-02 (Growth) |
| Create A/B experiments | `allow_meta_edits` | cur-02 (Growth) |

Enforcement: `curriculum-contract-check.sh --action allow_meta_edits`

## Improvement Velocity (imp@k)

Formula: `imp@k = (metric_after_k_goals - metric_at_strategy_change) / k`

Tracked metrics (from world/ and <agent>/ stores):
- `pipeline_accuracy` — from `world/pipeline-meta.json` accuracy.accuracy_pct
- `goal_completion_rate` — completed / attempted from aspirations-meta
- `encoding_efficiency` — encoded items later retrieved (from tree retrieval stats)
- `reflection_yield` — actionable findings per reflection (from journal)

Learning value per goal (computed in Step 8.8):
`learning_value = (tree_updated × 0.3) + (min(1, artifacts × 0.2) × 0.3) + (encoding_score × 0.2) + (min(1, findings × 0.25) × 0.2)`

## A/B Experiment Lifecycle

1. **Create**: Agent proposes variant during evolution Step 0.7
2. **Track**: Phase rotation (baseline → variant) every `goals_per_phase` goals
3. **Resolve**: After `min_duration_goals`, compare imp@k: adopt if > threshold, revert if < -threshold
4. **Archive**: Move to `completed-experiments.yaml` with outcome

## Transfer Protocol

Export: `meta-transfer.sh export` extracts validated strategies (adopted after A/B testing) as a YAML bundle with provenance (where learned, evidence count, imp@k).

Import: `meta-transfer.sh import` merges into existing meta-strategies. Weights are bounded. Heuristics are appended with `source: "transfer from {domain}"` tag.

## Integration Points

| Phase | Reads | Writes |
|-------|-------|--------|
| Phase 2 (Goal selection) | goal-selection-strategy.yaml | — |
| Phase 2.5 (Metacognitive) | improvement-instructions.md | — |
| Phase 6 (Spark) | — | meta-log.jsonl (sq-c06) |
| Step 8.8 (State update) | encoding-strategy.yaml | improvement-velocity.yaml |
| Phase 9.5a (Signal) | — | meta-log.jsonl |
| Step 0.7 (Evolve) | all strategies + velocity + log | strategy files + experiments |
| Step 0.3 (Reflect) | reflection-strategy.yaml (incl. effectiveness_by_type, adaptive_depth) | — |
| Step 5 (Pattern extract) | — | strategy files (incl. enabling strategies) |
| Step 5.7 (Full cycle) | — | reflection-strategy.yaml roi_history |
| Step 5.8 (Full cycle) | reflection_quality_log | reflection-strategy.yaml effectiveness_by_type |
| Phase 4.26 (Utilization) | source_reflection_id on items | reflection-strategy.yaml quality_log |
| Step 8.65 (Consolidate) | velocity + meta.yaml | meta.yaml (rolling averages) |
| Boot Step 2 | meta.yaml + velocity | — |
| Step 8.85 (State update) | backpressure.yaml | backpressure.yaml, dead-ends.jsonl, strategy-generations.yaml |
| Step 0.7 (Evolve) | backpressure, dead-ends, credit-assignment, strategy-generations, weakness-report | — |
| Step 5.55 (Reflect full) | pattern-sigs, guardrails, experiences, backpressure | <agent>/weakness-report.yaml |
| Step 8.65 (Consolidate) | credit-assignment.yaml, strategy-generations.yaml | credit-assignment.yaml |

## Backpressure Gate (AutoContext-inspired)

Monitors every `meta-set.sh` change and auto-reverts if performance regresses.

**State**: `meta/backpressure.yaml` — `active_monitors[]` (status: monitoring|rolled_back|graduated) + `rollback_history[]`.

**Flow**:
1. `meta-set.sh` creates a backpressure monitor with current imp@k as baseline
2. Step 8.85 calls `meta-backpressure.sh check` after every goal
3. If `consecutive_below_baseline >= regression_window` (default 5): auto-rollback
4. If `consecutive_above_baseline >= graduation_window` (default 15): graduate (stop monitoring)
5. Repeated rollbacks on same field (2+) auto-register dead ends

**Config** (in `core/config/meta.yaml` strategy_schemas.backpressure):
- `regression_window`: 5 (modifiable: 3-10)
- `graduation_window`: 15 (modifiable: 10-30)
- `baseline_tolerance`: -0.10 (modifiable: -0.20 to -0.02)
- `max_active_monitors`: 5

**Cooldown**: Step 0.7 calls `meta-backpressure.sh cooldown-check --window 20`. Fields rolled back within 20 goals are skipped.

## Dead End Registry (AutoContext-inspired)

Tracks meta-strategy approaches proven to fail. Prevents retrying known-bad configurations.

**Store**: `meta/dead-ends.jsonl` (script-access only, JSONL — lifecycle records grow over time).

**Schema**:
```json
{"id":"de-001","strategy_file":"...","field":"...","value_range":[lo,hi],"value_pattern":"...","evidence":["mc-003"],"failure_pattern":"why this fails","registered":"ISO8601","times_matched":0,"category":"meta_weight","status":"active"}
```

**Categories**: `meta_weight | meta_heuristic | meta_experiment | encoding_rule | domain_approach`

**Registration sources**:
- Auto: backpressure rollback of same field 2+ times (Step 8.85)
- Manual: agent can register via `echo '<json>' | meta-dead-ends.sh add`

**Enforcement**: Step 0.7 calls `meta-dead-ends.sh check` before proposing any change. Matches block the change and increment `times_matched`.

## Credit Assignment (AutoContext-inspired)

Attributes performance improvements to specific meta-strategy changes.

**State**: `meta/credit-assignment.yaml` — `assignments[]` with isolation flags and attribution scores.

**Schema per assignment**:
- `meta_change_id`: mc-NNN reference
- `strategy_file`, `field`, `old_value`, `new_value`, `applied_date`
- `goals_measured`: how many goals since change
- `avg_learning_value_before`, `avg_learning_value_after`, `delta`
- `isolated`: true if sole active change during measurement
- `confidence`: 0.8 if isolated, 1/N if co-occurring with N other changes
- `attribution_score`: delta * confidence

**Flow**:
1. Step 8.8 tags imp@k snapshots with `active_meta_changes` (mc-NNN IDs from backpressure monitors)
2. Step 8.65 (consolidation) computes credit for changes with 10+ goals measured
3. Step 0.7 reads credit assignments: preserve high-attribution, prioritize modifying low-attribution

## Strategy Generations (AutoContext-inspired)

Maps parameter configurations to performance. Each "generation" = a frozen set of meta-strategy values.

**State**: `meta/strategy-generations.yaml` — `generations[]` with parameter snapshots and metrics.

**Flow**:
1. `meta-set.sh` closes current generation and opens a new one on every strategy parameter change
2. Step 8.85 calls `meta-generations.sh update --learning-value` to update running metrics
3. Step 0.7 reads generation history: "Generation N was peak (avg X.XX)"
4. Step 8.65 updates `peak_generation` if current exceeds all-time best

## Weakness Report

Aggregated failure pattern detection from multiple signal sources.

**State**: `<agent>/weakness-report.yaml` (domain-specific, wiped when agent is removed).

**Schema per weakness**:
- `id`: wk-NNN
- `type`: regression | stagnation | dead_end | systematic_bias
- `severity`: HIGH | MEDIUM | LOW
- `evidence`: {pattern_signatures, guardrail_triggers, experience_ids, meta_log_entries}
- `status`: active | addressed | monitoring
- `remediation`: {proposed, applied, goal_id}

**Flow**: Step 5.55 (reflect --full-cycle) scans pattern sigs, guardrails, experiences, and backpressure rollbacks. HIGH-severity weaknesses auto-create investigation goals.

## Curator Quality Gate

Knowledge encoding quality evaluation (see `core/config/memory-pipeline.yaml` curator_gate section).

**Dimensions**: coverage (0.40), specificity (0.35), actionability (0.25).
**Threshold**: 0.45 (modifiable: 0.25-0.70).
**Location**: Step 8c.5 in aspirations-state-update, between WRITE NARRATIVE and PRECISION AUDIT.
**On fail**: demote to overflow queue (working memory). Second chance during consolidation.
