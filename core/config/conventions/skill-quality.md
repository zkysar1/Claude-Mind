# Skill Quality Convention

SkillNet-inspired five-dimension evaluation for skill execution quality.
Every goal execution produces a quality assessment that accumulates in
`meta/skill-quality.yaml` — a rolling window of the last 20 evaluations per skill.

## Five-Dimension Evaluation

Three-level grading: **good** (1.0) / **average** (0.5) / **poor** (0.0).

| Dimension | Definition | Signals |
|-----------|-----------|---------|
| Safety | Did execution avoid harmful side effects? | No guardrail violations, no data corruption, no unauthorized mutations |
| Completeness | Did execution produce all expected outputs? | All `verification.outcomes` met, no partial results |
| Executability | Could the skill run without errors? | No retries needed, no episode chaining, clean exit |
| Maintainability | Is the skill's procedure clear and reproducible? | Steps unambiguous, no hardcoded values, companion scripts work |
| Cost-awareness | Was execution efficient with context/tokens? | Retrieval proportional to need, no redundant reads, reasonable step count |

## Dimension Weights

Aggregate quality = weighted average of dimensions.
Weights stored in `meta/skill-quality-strategy.yaml` (tunable via meta-strategy protocol).

Default weights:
```
safety:          0.30   # Safety is paramount
completeness:    0.25   # Must produce expected outputs
executability:   0.20   # Clean execution matters
maintainability: 0.15   # Reproducibility
cost_awareness:  0.10   # Efficiency is nice-to-have
```

## Quality Score Storage

### Schema (`meta/skill-quality.yaml`)

```yaml
last_updated: "2026-03-25T14:30:00"
skills:
  aspirations-execute:
    total_evaluations: 15
    rolling_window: 20
    aggregate:
      safety: 0.93
      completeness: 0.87
      executability: 0.80
      maintainability: 0.90
      cost_awareness: 0.73
      overall: 0.85
    evaluations:        # last 20 executions (FIFO)
      - goal_id: "g-001-03"
        date: "2026-03-25T14:30:00"
        safety: 1.0
        completeness: 1.0
        executability: 0.5
        maintainability: 1.0
        cost_awareness: 0.5
        overall: 0.80
        notes: "Episode chain triggered once"
```

## Script API

| Command | Purpose | Output |
|---------|---------|--------|
| `skill-evaluate.sh score --skill NAME --goal ID --safety G --completeness G --executability G --maintainability G --cost-awareness G` | Record quality score | Confirmation text |
| `skill-evaluate.sh read --skill NAME` | Read aggregate + recent evaluations for one skill | JSON |
| `skill-evaluate.sh read --all --summary` | Summary table across all skills | JSON array |
| `skill-evaluate.sh report` | Full quality report across all skills | JSON |
| `skill-evaluate.sh underperforming [--threshold N]` | Skills below threshold on any dimension | JSON array |

Grade values for `--safety`, `--completeness`, etc.: `good`, `average`, or `poor`.

## Quality Thresholds

Defined in `core/config/skill-gaps.yaml` under `quality_thresholds:`:

| Threshold | Default | Meaning |
|-----------|---------|---------|
| `retirement_floor` | 0.30 | Overall below → retirement candidate |
| `review_floor` | 0.50 | Overall below → review needed |
| `dimension_floor` | 0.20 | Any dimension below → alert |
| `min_evaluations` | 5 | Min evaluations before quality-based actions |

## Integration with Aspirations Loop

### Phase 8.76 (Quality Scoring)

After Step 8.75 (Execution Reflection) in `aspirations-state-update/SKILL.md`:

```
8.76. Skill Quality Assessment (skip for routine outcomes):
  Map execution signals to five dimensions:
    safety     = good if no guardrail violations, else average (caught) or poor (uncaught)
    completeness = good if all verification.outcomes met, else average (partial) or poor
    executability = good if no retries, else average (1 retry) or poor (2+)
    maintainability = good (default for base skills; assessed during forge for forged)
    cost_awareness = assess from retrieval manifest (items loaded vs items used)
  Bash: skill-evaluate.sh score --skill {skill} --goal {goal.id} \
      --safety {safety} --completeness {completeness} --executability {executability} \
      --maintainability {maintainability} --cost-awareness {cost_awareness}
```

### Consolidation Step 8 (Skill Health Report)

Read `skill-evaluate.sh report` and include dimension scores in health summary.
Flag skills with any dimension below `dimension_floor`.

### Evolution Step 9.5 (Skill Curation)

Read `skill-evaluate.sh underperforming` to identify retirement/improvement candidates.

## Meta-Strategy Integration

Dimension weights are a meta-strategy (`meta/skill-quality-strategy.yaml`).
Tunable during `/aspirations-evolve` Step 0.7 via `meta-set.sh`.
Bounded by `core/config/meta.yaml` strategy_schemas.skill_quality.
