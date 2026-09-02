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
        judge_model: "claude-opus-5"   # who graded; "unknown" when unresolvable
        harness: "claude-code"         # claude-code | zakcode | unknown
        notes: "Episode chain triggered once"   # ILLUSTRATIVE ONLY — see below
```

`notes` appears in this example for historical reasons but is written by
NEITHER writer and has no CLI flag (measured 2026-09-01, g-306-394). Do not
expect it on a real record.

### Judge provenance (`judge_model`, `harness`) — g-306-394

Grades are judge-based measurements, and the judge population is heterogeneous:
different boxes run different models, models roll forward over time, and some
deployments route Mind turns to non-Claude local models. All of it lands in one
rolling window of 20. Without provenance, aggregate drift across a model upgrade
is indistinguishable from real skill-quality change, and the retirement/review
floors fire on a mixture nobody can reconstruct.

- **Resolved CALLER-SIDE and forwarded in the request body — g-306-400.** Both
  values are resolved by the WRAPPER, in the judge's own session, via
  `rt_judge_provenance` (`core/scripts/_runtime.sh`), and travel to the writer
  as `judge_model` / `harness` body keys. The writer normalizes what it is
  given and reads NO environment. This is not a style choice: under daemon-only
  architecture the writer is the long-lived daemon, which inherits the
  environment of whichever session spawned it and holds it for its whole
  lifetime, so an environment read there reports one arbitrary session's values
  for every agent's request (guard-2480). g-306-394 shipped exactly that, with
  15 green tests — measured 2026-09-01, daemon pid 505894: `MIND_JUDGE_MODEL`
  absent, so `judge_model` was `"unknown"` on every record, and `CLAUDECODE`
  present, so `harness` was stamped `"claude-code"` on every evaluation forever.
  Absent would have been honest; that is confidently WRONG.
  **BOTH entry points into the writer must forward.** There are two:
  `skill-evaluate.sh` → `/v1/skill-evaluate/score`, and `skill-quality-score.sh`
  → `/v1/skill-quality/score` → `skill_evaluate._score_write`. The second is the
  Phase 8.76 per-goal call site and therefore the dominant producer of
  evaluations, so fixing only the first leaves the defect on the path that
  actually runs (guard-3448).
- **`judge_model`** — the caller resolves it ONLY from an explicit
  `MIND_JUDGE_MODEL`; otherwise `"unknown"`. It is deliberately NOT inferred
  from `CLAUDE_CODE_SUBAGENT_MODEL`: that names the SUBAGENT model while scoring
  runs on the MAIN loop, and the two genuinely differ (measured 2026-09-01 on
  cc-04 — subagent env read `claude-opus-4-6` while the session ran
  `claude-opus-5`). A confidently wrong judge is worse than an absent one,
  because it corrupts the exact comparison the field exists to enable
  (guard-1925).
- **`harness`** — the caller resolves `claude-code` when `CLAUDECODE` is set,
  `zakcode` when a `ZAKCODE_*` marker is, else empty. The writer maps empty —
  and any value outside the closed set `claude-code | zakcode | unknown` — to
  `"unknown"`: the body is caller-supplied over HTTP, so it is untrusted input,
  and aggregate consumers group by this field.
- **Legacy-absent semantics** — records written before this change carry neither
  key. Absent reads as `"unknown"`; no backfill is required or wanted. The
  merge handler unions whole evaluation dicts, so old records survive untouched
  and new fields propagate across boxes without a handler change.
- **Surfacing** — `read --skill` and bare `read` dump whole records, so the
  fields appear directly. `read --all --summary` and `report` add a `judges`
  list of `{judge_model, harness, n}` giving the composition behind each
  aggregate: one entry means judge-homogeneous and safe to compare over time;
  more than one means drift may be a judge change rather than a skill change.
  Un-provenanced records are counted as `unknown` rather than dropped — hiding
  them would hide the very mixture the list exists to expose.

Both writers are byte-compat twins (`core/scripts/skill-evaluate.py` and
`mind_api/src/meta/skill_evaluate.py`) dumping with `sort_keys=False`, so
insertion order is the on-disk byte order; the new keys go LAST so existing
records' prefix is unchanged. `core/BOUNDARY.md` forbids the daemon importing
the CLI module, so the duplication is deliberate and
`core/scripts/tests/test_skill_evaluate_judge_provenance.py` is what keeps the
two aligned.

Because the CLI twin runs as a fresh subprocess of the judging session, its
environment IS the judge's, so it resolves via its own `_judge_from_env` and
feeds the same normalizer. The daemon twin has no counterpart to that function
by design. `core/scripts/tests/test_skill_evaluate_judge_wiring.py` pins the
WIRING rather than the function — an exported `MIND_JUDGE_MODEL` reaching the
recorded evaluation through each real wrapper, and the writer recording
`"unknown"` while judge variables are set in its own process. That second
assertion is the regression pin: it is what a function-level test cannot make
(guard-1943 — a green suite certifies the FUNCTION, never the WIRING).

### Can these grades FAIL? — measured miss rate (g-306-403)

Provenance above records WHO graded. This records whether the grading can
return a bad verdict at all. Six canaries with objectively-planted flaws, run
through `skill-quality-score.sh derive` (read-only, no store written):
**2 of 6 missed, 2026-09-01.** Both misses grade toward the TOP.

- `completeness` returns `good` when `outcomes_total` is 0 — an execution that
  verified nothing is byte-identical to one that met every outcome.
- `maintainability` is a constant: base skills default to `good`, and 0 of 82
  forged registry entries carry `quality_at_forge`, so all 133 canonical skills
  score `good` on it forever while it holds 0.15 of the aggregate weight.

Also worth carrying when reading any aggregate here: **four of the five
dimensions are deterministic derivation from integer arguments, not judgment.**
Only `cost_awareness` is LLM-supplied — and it arrives as a caller argument, so
it is the one dimension a self-administered canary cannot test.

Canary set, re-run commands, and the rotation note (why a static set decays):
`core/config/judge-canary-skill-quality.md`.

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
