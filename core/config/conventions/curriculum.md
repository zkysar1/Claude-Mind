# Curriculum Convention

Curriculum uses YAML state + JSONL promotion log with script-based access:

## File Layout
- `<agent>/curriculum.yaml` — Mutable curriculum state (current stage, stages, gate status)
- `<agent>/curriculum-promotions.jsonl` — Append-only promotion log
- `core/config/curriculum.yaml` — Framework definition (immutable: stage schema, gate types, defaults)

## Relationship to Developmental Stage

Two complementary systems:
- **Curriculum** = prescribed trajectory (user-defined stages with explicit graduation gates)
- **Developmental Stage** (`<agent>/developmental-stage.yaml`) = emergent competence (computed from tree capability levels)

Curriculum gates can reference developmental-stage metrics (e.g., `metric: "developmental-stage.current_assessment.average_competence"`), bridging both systems.

## Script-Based Access (Exclusive Data Layer)

The LLM NEVER reads or edits curriculum JSONL/YAML files directly during RUNNING state.
All operations go through scripts:

| Script | Purpose | Output |
|--------|---------|--------|
| `curriculum-status.sh` | Current stage, unlocks, gate status | JSON |
| `curriculum-evaluate.sh` | Compute pass/fail for all gates | JSON |
| `curriculum-promote.sh` | Advance stage if all gates pass | JSON |
| `curriculum-contract-check.sh --action <name>` | Is action permitted in current stage? | JSON + exit code |
| `curriculum-audit.sh` | Verify log consistency | JSON |

## State Schema (`<agent>/curriculum.yaml`)

```yaml
current_stage: cur-01              # ID of active stage (null before /start)
stage_history:
  - stage_id: cur-01
    entered: "2026-03-21T10:00:00"
    exited: null                   # null = current stage
stages:
  - id: cur-01
    name: Foundation
    description: "Learn the domain..."
    unlocks:
      allow_self_edits: false
      allow_forge_skill: false
      allow_multi_goal_parallelism: false
    graduation_gates:
      - type: count_check
        id: gate_completed_goals
        description: "Complete at least 10 goals"
        file: "aspirations.jsonl"
        field: "goals[*].status"
        value: "completed"
        operator: ">="
        threshold: 10
    gate_status:                   # Populated by curriculum-evaluate
      - gate_index: 0
        passed: false
        last_checked: null
        current_value: null
```

## Promotion Log Schema (`<agent>/curriculum-promotions.jsonl`)

Each line is one JSON object — append-only:

```json
{"date": "2026-03-21T14:30:00", "from_stage": "cur-01", "to_stage": "cur-02", "gates_passed": [{"gate_index": 0, "value": 12}, {"gate_index": 1, "value": 0.28}], "actor": "curriculum.py"}
```

## Gate Types

### metric_threshold
Read a YAML file in the agent directory, navigate a dotpath, compare against threshold.
- `metric`: dotpath — first segment = filename (without .yaml), rest = YAML path
- `operator`: `>=`, `<=`, `==`, `>`
- `threshold`: number

### count_check
Count JSONL entries (or nested items) matching a filter.
- `file`: path relative to the agent directory (e.g., `aspirations.jsonl`)
- `field`: dotpath into each line; `goals[*].status` flattens arrays
- `value`: value to match
- `operator`: `>=`, `<=`, `==`, `>`
- `threshold`: required count

### log_scan
Count entries in a JSONL log matching field/value.
- `log_file`: path relative to the agent directory
- `match_field`: field name in each JSON line
- `match_value`: expected value
- `min_count`: minimum matches required

### command_check
Run a shell command; gate passes if exit code is 0.
- `command`: must start with `bash core/scripts/` (safety constraint)

## Unlock Capabilities

| Capability | Description | Default |
|-----------|-------------|---------|
| `allow_self_edits` | Agent may edit <agent>/self.md via sq-012 | false |
| `allow_forge_skill` | Agent may invoke /forge-skill | false |
| `allow_multi_goal_parallelism` | Agent may use TeamCreate for parallel goals | false |
| `allow_meta_edits` | Agent may edit meta/ strategy files | false |

## Contract Check Pattern

Enforcement points call `curriculum-contract-check.sh --action <capability>`:
- Exit 0 + `{"permitted": true}` → action allowed
- Exit 1 + `{"permitted": false, "current_stage": "...", "stage_name": "..."}` → action blocked

If `<agent>/curriculum.yaml` is missing (pre-curriculum agent): all actions permitted (graceful degradation).

## Enforcement Points

| Action | Enforcement Location |
|--------|---------------------|
| Self edits (sq-012) | `.claude/skills/aspirations-spark/SKILL.md` step 2.5 |
| Skill forging | `.claude/skills/forge-skill/SKILL.md` Forge Criteria |
| Parallel execution | `.claude/skills/aspirations-execute/SKILL.md` before TeamCreate |
| Meta-strategy edits | `.claude/skills/aspirations-evolve/SKILL.md` Step 0.7 |

## Evaluation Cadence

Curriculum gates are evaluated:
- At session end (`/aspirations-consolidate` Step 8.6)
- During evolution cycle (`/aspirations-evolve` Step 10)
- On demand via `/curriculum-gates` skill
