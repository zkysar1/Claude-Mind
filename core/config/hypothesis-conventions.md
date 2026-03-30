# Hypothesis Conventions

Reference documentation for hypothesis record schemas, horizons, context manifests, and process scores. Read on demand by `/review-hypotheses`, `/reflect`, and `/aspirations`.

## Goal Participants

Goals have a `participants` field (default: `[agent]`) that declares who is involved:
- `[agent]` — agent handles autonomously (scores +2 in agent_executable)
- `[user]` — requires user action (agent skips, logs reminder, scores +0)
- `[agent, user]` — collaborative (agent does its part, may need user input, scores +1)

Participants affect goal selection priority but NOT execution routing. Execution is always determined by the `skill` field.

## Hypothesis Goals

Goals that test predictions. They are normal goals with extra metadata fields and `skill: /review-hypotheses --hypothesis <id>`.

### Additional Fields (hypothesis goals only)

Standard goal fields plus:
```yaml
hypothesis_id: "{YYYY-MM-DD}_{slug}"  # links to pipeline JSONL record
horizon: session | short | long        # micro stays in working memory, NOT as goals
resolves_no_earlier_than: "YYYY-MM-DDTHH:MM:SS"  # time-gate (skip in COLLECT before this)
resolves_by: "YYYY-MM-DDTHH:MM:SS" | "session_end"  # expiration deadline
skill: "/review-hypotheses --hypothesis {hypothesis_id}"
```

### Default Resolution Windows

| Horizon | resolves_no_earlier_than | resolves_by |
|---------|------------------------|-------------|
| `session` | null (immediate) | `session_end` |
| `short` | formed_date + 12h | formed_date + 7d |
| `long` | formed_date + 24h | formed_date + 90d |

### Hypothesis-Goal Lifecycle

```
pending → [time-gate not reached: stays pending, skipped in COLLECT]
        → [time-gate passed] → in-progress → skill invokes /review-hypotheses
           → CONFIRMED/CORRECTED → completed (pipeline-move.sh <id> resolved)
           → UNRESOLVABLE → completed (pipeline-move.sh <id> archived)
           → NOT YET → pending (try again next cycle)
        → [past resolves_by] → expired (pipeline-move.sh <id> archived, reason: expired)
```

## Hypothesis Horizon (Time-Scale Tiers)

Every hypothesis has a `horizon` field that controls its overhead level. Defaults to `short` for backwards compatibility.

| Horizon | Resolution Time | Storage | Verification | Reflection |
|---------|----------------|---------|--------------|------------|
| `micro` | Same session (seconds-minutes) | Batch array in working memory | Self-check inline | Batch summary at session-end |
| `session` | This or next session (hours) | Lightweight pipeline file | Self-check or simple fetch | Episode-level only |
| `short` | 1-7 days | Standard pipeline record | WebSearch/fetch | Full reflection |
| `long` | 7+ days | Standard pipeline record | Full verification suite | Full + pattern extraction |

### Micro-Hypothesis Format (working memory batch)
Micro-hypotheses live in `<agent>/session/working-memory.yaml` under the `micro_hypotheses` slot — no pipeline records, no stage moves.
```yaml
micro_hypotheses:
  - claim: "Short natural-language prediction"
    confidence: 0.0-1.0
    formed: "HH:MM:SS"         # time only (session-scoped)
    outcome: confirmed | corrected | null
    surprise: 0-10             # computed: high confidence + wrong = high surprise
    category: "kebab-case-category"
```
- **5-6 fields only.** Form → act → record outcome → done.
- No `context_consulted`, `process_score`, or `replay_metadata`.
- At session-end consolidation: batch stats computed, surprises (>= 7) promoted to encoding gate.
- Micro-hypotheses count toward `resolved_hypotheses` diagnostic total (no longer gates stage progression).

### Session-Horizon Lightweight Records
Session-horizon hypotheses get a pipeline JSONL record but with reduced metadata:
```yaml
# Required fields (session horizon)
id, title, stage, horizon, category, type, confidence, position, formed_date,
resolves_by, verification, outcome, reflected, reflected_date
# Optional (session horizon) — omit unless relevant
context_consulted, process_score, replay_metadata
```
- `resolves_by: session_end` — special value meaning "check at end of this or next session"
- `verification: self_check` — agent verifies outcome inline (file check, state check, result inspection)

### Horizon-Dependent Behavior
- **Resolution**: `micro` resolves inline (never enters `/review-hypotheses`). `session` uses self-check. `short`/`long` use full external verification.
- **Reflection**: `micro` gets batch reflection only. `session` gets episode-level reflection (no pattern extraction unless surprise >= 7). `short`/`long` get full reflection.
- **Accuracy reporting**: All horizons feed into accuracy stats. `by_time_horizon` bins: `micro`, `session`, `short_term_7d`, `medium_term_30d`, `long_term_90d`.

## Resolution Timing (Hypothesis Records)
- Applies to `short` and `long` horizon hypotheses (micro/session use different mechanisms)
- `resolves_no_earlier_than`: ISO 8601 timestamp (`YYYY-MM-DDTHH:MM:SS`) — the earliest time a hypothesis could resolve. Set conservatively with a buffer to account for data propagation and outcome confirmation. Used by `/review-hypotheses --resolve` to skip premature checks.
- Default buffer: +12h from expected resolution time. Domain-specific skills may set tighter buffers.
- Records without this field: `/review-hypotheses` falls back to `end_date` + 12h default buffer

## Context Manifest (Hypothesis Records)
- Applies to `short` and `long` horizon hypotheses. `session` horizon MAY include if relevant. `micro` never includes.
- Every `short`/`long` hypothesis record SHOULD have a `context_consulted` section
- `tree_nodes_read`, `pattern_signatures_checked`, `articles_read`, `experiential_matches`: populated when `retrieve.sh` loads context for the category
- `data_sources_used`: populated during evaluation (WebSearch/WebFetch URLs)
- `context_gaps_identified`: populated by `/reflect --on-hypothesis` post-hoc (compares manifest against available context)
- `retrieval_channels`: list of channels that contributed results (e.g., `[tree, category, entity, temporal]`) — populated by `retrieve.sh`
- `deliberation`: list of `{item_id, status: ACTIVE|SKIPPED, reason}` — populated by Phase 4 memory deliberation
- `context_quality`: populated by consuming skill after main work
  - `usefulness`: `helpful` | `neutral` | `misleading` | `irrelevant` | `pending`
  - `most_valuable_source`: `{layer}:{id}` format (e.g., `tree:execution`, `pattern:sig-003`)
  - `least_valuable_source`: `{layer}:{id}` format — which loaded source added least value
  - `chain_note`: one-sentence explanation
- `source_validation`: populated during evaluation when multiple sources are consulted
  ```yaml
  source_validation:
    sources_consulted_count: 2
    agreement: "unanimous"        # unanimous | contested | single_source
    agreement_note: ""            # details if contested
    sources:
      - source_id: "reuters.com"
        source_type: web_search   # web_search | web_fetch | memory | user_input
        verdict: "YES"            # YES | NO | UNCLEAR
        snippet: "relevant excerpt"
  ```
- Used for analysis: "Did we miss context?" and "Was loaded context actually useful?"

### Experience Reference (Hypothesis Records)

Every `short`/`long` hypothesis record MAY have an `experience_ref` field pointing to the full-fidelity experience archive record that captured the formation context:

```yaml
experience_ref: "exp-2026-03-10_api-response-latency"  # optional, string
```

- Created by `/aspirations` Phase 6 when hypothesis formation archives its context
- Dereferenced by `/reflect` Step 1 to reconstruct exact information state at hypothesis time
- Enables precise calibration analysis: "Given exactly this evidence, was my confidence right?"
- Content is read via `experience-read.sh --id <experience_ref>` + reading the `.md` file at `content_path`

## Learning Status Tracking (Resolved Records)
- Every resolved hypothesis record MUST have `reflected: true|false` field
- `reflected` is set to `false` by `/review-hypotheses --resolve` when moving active→resolved
- `reflected` is set to `true` by `/review-hypotheses --learn` after `/reflect` completes
- `reflected_date` records when learning occurred (null until reflected)
- `/review-hypotheses --learn` filters on `reflected: false` — idempotent, safe to call multiple times

## Process-Outcome Dual Score (Hypothesis Records)

- Every resolved hypothesis SHOULD have a `process_score` section:
  ```yaml
  process_score:
    process_quality: null   # confidence if CONFIRMED else (1.0 - confidence). 0.0-1.0
    dual_classification: null  # earned_confirmed | lucky_confirmed | unlucky_corrected | deserved_corrected
  ```
- `dual_classification` uses confidence >= 0.60 threshold with outcome:
  - `earned_confirmed`: CONFIRMED + confidence >= 0.60 (sure and right)
  - `lucky_confirmed`: CONFIRMED + confidence < 0.60 (unsure but right)
  - `unlucky_corrected`: CORRECTED + confidence >= 0.60 (sure but wrong)
  - `deserved_corrected`: CORRECTED + confidence < 0.60 (unsure and wrong)
- Modulates learning priority in `/reflect`:
  - `earned_confirmed`: standard strategy extraction
  - `lucky_confirmed`: low priority — don't reinforce reasoning that was flawed
  - `unlucky_corrected`: skip guardrail extraction — the process was sound
  - `deserved_corrected`: high priority — extract guardrail + investigate failure

## Pre-Formation Calibration Gate

Every hypothesis formed via sq-009 MUST pass the calibration gate defined in
`aspirations-spark/SKILL.md` Step 0.5. The gate reads recent accuracy data and
applies a confidence ceiling proportional to demonstrated calibration.

This closes the feedback loop: resolved hypothesis outcomes accumulate →
`aspirations-spark` reads accuracy at formation time → confidence is bounded by track record.

The adversarial pre-mortem (Step 0.7) additionally requires articulating why
the prediction might be wrong before assigning confidence > 0.65.
