---
name: review-hypotheses
description: "Resolve hypotheses, learn from outcomes, calculate accuracy, and generate reports"
user-invocable: false
triggers:
  - "/review-hypotheses"
parameters:
  - name: mode
    description: "--resolve, --learn, --accuracy-report, --full-cycle, --category-comparison"
    required: false
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
  known_pitfalls: []
  reconsolidation_trigger: "After 10 invocations with declining success rate, trigger skill review"
conventions: [pipeline, aspirations, tree-retrieval, reasoning-guardrails, pattern-signatures]
minimum_mode: assistant
---

# /review-hypotheses — Hypothesis Review & Resolution Engine

Two-phase design: **resolve** (detect outcomes, move records, record results) and **learn** (reflect on outcomes, extract patterns). These phases are separated so that `/boot` can resolve without triggering learning, and `/aspirations` goals can learn from freshly resolved data without re-checking resolution sources.

## Parameters

- `--resolve` — Check resolution status, move active→resolved, record outcomes. **Does NOT call /reflect.** Sets `reflected: false` on each record.
- `--learn` — Find resolved records with `reflected: false`, call `/reflect --on-hypothesis` for each, set `reflected: true`.
- `--accuracy-report` — Generate accuracy statistics across all resolved hypotheses
- `--full-cycle` — `--resolve` + `--learn` + `--accuracy-report` + `/reflect --full-cycle` + spark check
- `--category-comparison <cat1> <cat2>` — Compare accuracy between two categories
- `--hypothesis <id>` — Check a specific hypothesis
  - When called as a goal's skill (from aspirations loop), returns one of:
    - `CONFIRMED` — hypothesis confirmed (move pipeline file to resolved/)
    - `CORRECTED` — hypothesis disconfirmed (move pipeline file to resolved/)
    - `PENDING` — not enough evidence yet (goal stays pending for retry)
    - `EXPIRED` — past resolves_by deadline (move pipeline file to archived/)
- No args → default to `--resolve`

---

## Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

---

## Mode 1: Resolve (`--resolve`)

Detects which active hypotheses have resolved, records outcomes, moves records, and updates the memory tree. **Does NOT trigger reflection or learning** — that is `--learn`'s job.

### Step 1: Load Hypotheses to Check

```
# Primary source: all active hypotheses
Bash: pipeline-read.sh --stage active

# Defensive check: catch hypotheses that weren't moved to active/
evaluating_records = Bash: pipeline-read.sh --stage evaluating
Filter evaluating records to ONLY those with stage: active or a recorded hypothesis

If any evaluating records should be active:
    Log: "NOTE: {N} hypotheses found in evaluating stage — should be in active"
    Add them to the candidate list (they will be resolved and moved to resolved)

# Horizon filter: micro-hypotheses never enter this pipeline.
# Session-horizon hypotheses use self-check verification (Step 2 handles this).
# Filter OUT any records with horizon: micro (shouldn't exist in pipeline, but defensive).
Filter out records where horizon == "micro"

Combine into single candidate list
Sort by resolves_no_earlier_than (soonest first), then end_date for legacy records
```

### Step 1.5: Load Domain Context

Before checking resolution, load background knowledge for each category represented.

```
Collect unique categories from candidate hypotheses
For each unique category:
  Bash: retrieve.sh --category {batch_category} --depth medium
  # Returns unified JSON with all data stores. Retrieval counters already incremented.

  Cache result — reuse for all hypotheses in same category
```

Use retrieved context to:
- Inform resolution interpretation (understand domain before judging outcome)
- Populate context_consulted manifest on each hypothesis record during Step 2
- Record: tree_nodes_read, pattern_signatures_checked, articles_read
- Leave context_gaps_identified empty (populated later by /reflect)

### Step 2: Check Each Hypothesis

For each active hypothesis:

```
0. Resolution time filter (token-saving skip):
   current_time = current date and time (ET)

   # Horizon-aware resolution timing:
   # - session: resolves_by may be "session_end" — always check if current session
   # - short/long: use resolves_no_earlier_than or end_date as before
   # - micro: should never be here (filtered in Step 1)

   IF hypothesis.horizon == "session":
       IF hypothesis.resolves_by == "session_end":
           threshold = null  # always eligible for resolution check
       ELSE:
           threshold = hypothesis.resolves_by (if timestamp) or null

   ELSE:  # short, long, or unset (default to short behavior)
       Determine resolution threshold:
         IF hypothesis.resolves_no_earlier_than exists:
             threshold = resolves_no_earlier_than
         ELSE IF hypothesis.end_date OR hypothesis.source_data.end_date exists:
             threshold = end_date + 12 hours (legacy fallback)
         ELSE:
             threshold = null (no date info — proceed with resolution check)

   IF threshold is not null AND threshold > current_time:
       SKIP this hypothesis — do NOT check resolution status
       Log: "Skipped {slug} — resolves no earlier than {threshold}"
       Add to resolve_result.skipped_not_due
       Continue to next hypothesis

   Note: --hypothesis <id> flag bypasses this filter (always checks)

1. Check resolution date:
   - Past due? → Likely resolved, check outcome
   - Within 48 hours? → Flag as "resolving soon"
   - Session horizon with resolves_by: session_end? → Always check

2. Determine resolution status using horizon-appropriate methods:

   # Session-horizon: self-check verification (lightweight)
   IF hypothesis.horizon == "session":
       IF hypothesis.verification == "self_check":
           Evaluate the hypothesis outcome using available local state:
             - File existence checks
             - State file inspection
             - Result of the action that was taken
             - Inline reasoning about the outcome
           If outcome determinable: record it and proceed to Step 3
           If outcome NOT determinable: skip (will retry next session)
       ELSE:
           Fall through to standard methods below

   # Short/long horizon: full external verification (in priority order)
   a. Domain-specific sub-skill (if available):
      Invoke the relevant resolution sub-skill for this hypothesis's category
      Sub-skills handle API calls, data fetching, and status interpretation

   b. WebSearch (general resolution check):
      WebSearch: "{hypothesis question} outcome result"
      WebSearch: "{hypothesis question} resolved decided"
      Look for authoritative sources confirming the outcome

   c. WebFetch (direct source check):
      If the hypothesis record includes a resolution_url or source_url,
      fetch it directly and check for outcome indicators

   d. Resolution timestamp:
      If resolves_no_earlier_than has passed and the hypothesis has a
      known binary outcome trigger (e.g., scheduled event), check if
      the triggering event has occurred

3. If resolution is confirmed:
   Record outcome:
     actual_outcome: "YES" | "NO" (or the relevant outcome value)
     resolution_date_actual: "YYYY-MM-DD"
     resolution_source: "How we verified (method + source)"

4. Compare hypothesis to outcome:
   our_hypothesis: "YES"
   our_confidence: 0.72
   actual_outcome: "NO"
   outcome: CORRECTED
   surprise_level: 7  # calculated from confidence gap
```

### Step 2.5: Source Agreement Check

When resolving a hypothesis using evidence from multiple sources:

```
sources = []  # collected during resolution research above

For each source consulted during Steps 1-2:
  sources.append({
    source_id: "{domain or identifier}",
    source_type: "{web_search | web_fetch | memory | user_input}",
    verdict: "{YES | NO | UNCLEAR}",  # does this source support the hypothesis?
    snippet: "{relevant excerpt, max 200 chars}"
  })

IF len(sources) == 0:
  source_validation = null  # nothing to validate

ELIF len(sources) == 1:
  source_validation = {
    sources_consulted_count: 1,
    agreement: "single_source",
    agreement_note: "",
    sources: sources
  }

ELSE:  # 2+ sources
  verdicts = [s.verdict for s in sources if s.verdict != "UNCLEAR"]
  IF len(verdicts) > 0 AND all verdicts same: agreement = "unanimous"
  ELSE:
    agreement = "contested"
    agreement_note = "Sources disagree: {summarize disagreement}"
    → Append to outcome_detail: "[SOURCE DISAGREEMENT: {agreement_note}]"

  source_validation = {
    sources_consulted_count: len(sources),
    agreement: agreement,
    agreement_note: agreement_note or "",
    sources: sources
  }
```

This does NOT block resolution. Contested sources are noted but the agent still makes a judgment call on the outcome.

### Step 3: Record Outcome

```
For each resolved hypothesis:
    1. Determine outcome:
       - Compare our_hypothesis against actual_outcome
       - outcome: "CONFIRMED" if hypothesis matches outcome, "CORRECTED" if not

    2. Calculate surprise level:
       # Single source of truth — same formula as reflect --batch-micro Step 3.
       - if CORRECTED: surprise_level = round(our_confidence * 10)
       - if CONFIRMED: surprise_level = round((1 - our_confidence) * 10)
       - High surprise = high confidence + wrong, or low confidence + right

    3. Update the pipeline record with:
       - outcome: CONFIRMED | CORRECTED
       - our_confidence: (preserved from hypothesis)
       - surprise_level: (calculated above)
       - resolution_summary: "CONFIRMED — predicted {X} with {confidence}% confidence"
         or "CORRECTED — predicted {X}, actual was {Y}"

    4. Include outcome in resolution output:
       "Result: CONFIRMED — predicted YES with 72% confidence"
       or "Result: CORRECTED — predicted YES with 72% confidence, actual was NO"
```

### Step 4: Move Resolved Hypotheses (ATOMIC — complete ALL steps for each hypothesis)

For each resolved hypothesis, execute this checklist IN ORDER.
`{source}` = the directory where the record currently lives (active/ or evaluating/).

```
□ 4.0  CHECK EXPIRATION (hypothesis goals only):
       IF the record has resolves_by AND resolves_by has passed AND outcome is still undetermined:
         - echo '{"outcome":"EXPIRED"}' | bash core/scripts/pipeline-move.sh <id> archived
         - Return "EXPIRED" to calling skill
         - SKIP remaining steps 4.1-4.2 for this record

□ 4.1  BUILD merge JSON with:
       - Outcome data (outcome: CONFIRMED/CORRECTED, surprise, outcome_date)
       - outcome_detail: resolution summary text
       - horizon: (preserve from original — defaults to "short" if missing)

       # Horizon-dependent metadata:
       # short/long horizon — full metadata:
       - replay_metadata:
           last_replayed: null
           replay_count: 0
           encoding_score: null
           reconsolidation_updates: []
       - Preserve context_consulted from original record (populated during evaluation):
           tree_nodes_read, pattern_signatures_checked, data_sources_used,
           articles_read, experiential_matches, context_gaps_identified
       - Initialize context_quality:
           usefulness: pending
           most_valuable_source: null
           least_valuable_source: null
           chain_note: null
       # Compute process_score inline (not deferred to reflect)
       IF outcome == "CONFIRMED" AND confidence >= 0.60:
           dual_classification = "earned_confirmed"
       ELIF outcome == "CONFIRMED" AND confidence < 0.60:
           dual_classification = "lucky_confirmed"
       ELIF outcome == "CORRECTED" AND confidence >= 0.60:
           dual_classification = "unlucky_corrected"
       ELIF outcome == "CORRECTED" AND confidence < 0.60:
           dual_classification = "deserved_corrected"
       process_quality = confidence if CONFIRMED else (1.0 - confidence)

       # session horizon — reduced metadata:
       # SKIP replay_metadata, context_quality, process_score
       # SKIP context_consulted unless already present on the record

       - Source cross-validation (ALL horizons):
           source_validation: {source_validation object from Step 2.5}

       - Learning status (ALL horizons):
           reflected: false
           reflected_date: null

□ 4.2  ATOMIC MOVE+UPDATE (single script call):
       # The merge JSON MUST include ALL fields from Step 4.1 above.
       # For short/long horizon, this means: outcome, outcome_detail, outcome_date,
       # surprise, replay_metadata, context_quality, process_score, reflected: false.
       # DO NOT skip fields — pipeline-move.sh merges them atomically.
       # Missing fields here = missing fields forever (reflect can't backfill structure).
       echo '<merge-json-with-ALL-4.1-fields>' | bash core/scripts/pipeline-move.sh <id> resolved
       This atomically: updates all fields, sets stage to resolved, recounts meta.

GATE: Do NOT proceed to the next hypothesis until the move completes successfully.
If the script exits non-zero, STOP and report the error — do not partially resolve.
```

### Step 4.5: Rate Context Quality (Retrieval Protocol Phase 5)

For each hypothesis just resolved in Step 4, rate the context loaded during Step 1.5:

```
For each resolved hypothesis:
    Read its context_consulted section

    Rate usefulness:
      - Did loaded tree nodes, patterns, guardrails, or beliefs help interpret the outcome?
      - Correctly predicted + context supported reasoning → "helpful"
      - Context present but didn't influence resolution interpretation → "neutral"
      - Context pointed toward wrong interpretation → "misleading"
      - Context had no bearing on this hypothesis → "irrelevant"

    Identify most_valuable_source:
      Which loaded item (tree node, pattern, guardrail, belief, experiential record)
      was most useful? Format: "{layer}:{id}"

    Identify least_valuable_source:
      Which loaded item added least value or was noise? Format: "{layer}:{id}"

    Write to resolved record's context_consulted.context_quality:
      usefulness: {rating}
      most_valuable_source: {layer:id}
      least_valuable_source: {layer:id}
      chain_note: "{one-sentence explanation}"

    If no context was loaded (empty context_consulted): set usefulness: "irrelevant"
    If unable to judge: leave usefulness: "pending" (finalized by /reflect)
```

### Step 5: Check for Triggered Reviews

Check if any auto-review triggers are met:

| Trigger | Condition | Action |
|---|---|---|
| Streak break | 3+ consecutive corrected | Flag for priority learning |
| High-confidence miss | Confidence > 0.80 AND corrected | Flag for priority violation analysis |
| Low-confidence hit | Confidence < 0.50 AND confirmed | Flag for underconfidence review |
| New category success | First confirmed in a category | Flag as potential strength |
| Stale hypothesis | Active > 90 days, no resolution | Check if hypothesis is still trackable |
| Category drift | 5+ hypotheses in one category, 0 in others | Flag for exploration |

If triggered: write triggered review flags to the resolve_result (consumed by callers for alerting).

### Step 6: Return Resolve Result

Return a structured summary (consumed by `/boot` for reporting, and by `--full-cycle` for chaining):

```yaml
resolve_result:
  newly_resolved: N
  already_resolved: M
  still_active: K
  skipped_not_due: J          # hypotheses whose resolves_no_earlier_than hasn't arrived
  resolving_soon: L           # within 48 hours of resolving
  triggered_reviews: [...]   # any flags from Step 5
  resolved_hypotheses:
    - id: "2026-03-15_record-slug"
      question: "Will X happen?"
      predicted: "YES"
      confidence: 0.72
      actual: "NO"
      outcome: CORRECTED
      surprise_level: 7
      reflected: false
```

### Tree Update Protocol (after resolution)

After resolving any hypothesis, update the memory tree:

#### Tree Step 1: Recalculate Category Accuracy
```
category = resolved_hypothesis.category  # e.g., "crypto", "sports-nhl", "politics"
Recount: confirmed / total resolved in this category
```

#### Tree Step 2: Update Affected Node (Dynamic Lookup)
```
node=$(bash core/scripts/tree-find-node.sh --text "{category}" --leaf-only --top 1)
# Returns: {key, score, file, depth, summary, node_type}

Read node.file
Update _tree.yaml via batch (accuracy, sample_size, confidence, capability_level):
echo '{"operations": [
  {"op": "set", "key": "<node.key>", "field": "accuracy", "value": <new-value>},
  {"op": "set", "key": "<node.key>", "field": "sample_size", "value": <new-value>},
  {"op": "set", "key": "<node.key>", "field": "confidence", "value": <new-value>},
  {"op": "set", "key": "<node.key>", "field": "capability_level", "value": "<new-value>"}
]}' | bash core/scripts/tree-update.sh --batch
```

#### Tree Step 3: Update Performance Tracking Node (Dynamic Lookup)
```
# Use dynamic lookup — never hardcode paths at any depth.
perf_node=$(bash core/scripts/tree-find-node.sh --text "performance-tracking" --top 1)
If perf_node exists:
    Read perf_node.file
    Update outcome record, category outcome breakdown
```

#### Tree Step 4: Update Accuracy Tracking Node (Dynamic Lookup)
```
# Use dynamic lookup — never hardcode paths at any depth.
acc_node=$(bash core/scripts/tree-find-node.sh --text "hypothesis-accuracy" --top 1)
If acc_node exists:
    Read acc_node.file
    Update overall accuracy, category breakdown, batch data
```

#### Tree Step 5: Propagate Capability Changes
```
If any node's capability_level changed:
  result=$(bash core/scripts/tree-propagate.sh <node.key>)
  # Returns: {source_node, ancestors_updated: [...], capability_changes: [...]}
  IF result.capability_changes is non-empty:
    For each changed ancestor: Read ancestor.file (.md)
    - Update parent node's capability map and topic summary
  If root-level domain summary affected:
    bash core/scripts/tree-update.sh --set root summary "<updated>"
  Log: "CAPABILITY UNLOCK: {topic} → {new_level}"
  Announce in output: "Category {X} unlocked {LEVEL} level!"
```

---

## Mode 2: Learn (`--learn`)

Processes resolved hypotheses that have NOT yet been reflected on. This is the learning phase — it calls `/reflect` to generate ABC chains, violation tracking, and pattern updates.

### Step 1: Load Unreflected Hypotheses

```
Bash: pipeline-read.sh --unreflected
Sort by surprise descending (learn from surprises first)
If no unreflected records: return { hypotheses_learned: 0 } and exit
```

### Step 2: Reflect on Each Hypothesis

For each unreflected hypothesis:

```
1. Bash: pipeline-read.sh --id {id}  (loads the full resolved record)
2. invoke /reflect --on-hypothesis {hypothesis-id}
   This generates:
   - ABC chain (Antecedent-Behavior-Consequence)
   - Textual reflection
   - Violation tracking (if wrong)
   - Source assessment
   - Encoding score
   - Pattern signature updates
3. After /reflect completes successfully:
   - Bash: pipeline-update-field.sh {id} reflected true
     (auto-sets reflected_date to today)
```

### Step 2.5: Differentiated Extraction Handoff

When calling /reflect for each unlearned resolution:
- Pass the hypothesis outcome (CONFIRMED/CORRECTED) to /reflect
- /reflect will use differentiated extraction (confirmed → strategy validation, corrected → preventive guardrails)
- After /reflect completes, verify that:
  - A reasoning bank entry was created via `reasoning-bank-read.sh --id rb-NNN`
  - For corrected outcomes: a guardrail was added via `guardrails-read.sh --id guard-NNN`
- If /reflect did not create expected entries, create them directly:
  - Reasoning bank entry: pipe JSON to `reasoning-bank-add.sh` (stdin)
  - Guardrail entry: pipe JSON to `guardrails-add.sh` (stdin)

### Step 3: Return Learn Result

```yaml
learn_result:
  hypotheses_learned: N
  hypotheses_already_learned: M
  violations_recorded: K
  patterns_updated: J
  learned_hypotheses:
    - id: "2026-03-15_record-slug"
      outcome: CORRECTED
      violation_type: "high-confidence miss"
      encoding_score: 0.72
```

---

## Mode 3: Accuracy Report (`--accuracy-report`)

### Step 1: Load All Resolved Hypotheses

```
Bash: pipeline-read.sh --stage resolved  (all resolved records)
Bash: meta-read.sh meta-knowledge/_index.yaml  # existing meta-data

# Include micro-hypothesis batch stats from pipeline metadata.
# Micro stats are stored in pipeline-meta.json under micro_hypothesis_stats.
# These aggregate across sessions — each session-end consolidation appends batch totals.
Bash: pipeline-read.sh --meta → micro_hypothesis_stats (if exists)
```

### Step 2: Calculate Metrics

```yaml
accuracy:
  overall:
    total: 25
    confirmed: 18
    corrected: 7
    accuracy_pct: 72.0
    trend: "improving"  # compare last 10 vs previous 10

  by_category:
    politics: {total, confirmed, accuracy, confidence_avg, calibration_error}
    crypto: {total, confirmed, accuracy, confidence_avg, calibration_error}

  by_hypothesis_type:
    high-conviction: {total, confirmed, accuracy}
    calibration: {total, confirmed, accuracy}
    exploration: {total, confirmed, accuracy}
    contrarian: {total, confirmed, accuracy}

  by_evaluation_method:
    system-1: {total, confirmed, accuracy}
    system-2: {total, confirmed, accuracy}

  by_research_depth:
    quick: {total, confirmed, accuracy}
    moderate: {total, confirmed, accuracy}
    deep: {total, confirmed, accuracy}

  by_time_horizon:
    micro: {total, confirmed, accuracy}             # same-session, seconds-minutes
    session: {total, confirmed, accuracy}            # this/next session, hours
    short_term_7d: {total, confirmed, accuracy}
    medium_term_30d: {total, confirmed, accuracy}
    long_term_90d: {total, confirmed, accuracy}

  confidence_calibration:
    bins:
      "50-59%": {predicted, actual, count, error}
      "60-69%": {predicted, actual, count, error}
      "70-79%": {predicted, actual, count, error}
      "80-89%": {predicted, actual, count, error}
      "90-100%": {predicted, actual, count, error}

  source_reliability:
    - source: "source-id"
      times_used: 12
      led_to_confirmed: 9
      reliability: 0.75

  streaks:
    current: 3
    longest_confirmed: 5
    longest_corrected: 2
```

### Step 3: Hypothesis Testing

If 20+ resolved hypotheses, test hypotheses:

```
H1: "Deep research outperforms quick evaluations"
H2: "Category X outperforms category Y"
H3: "Hypotheses with source X outperform those without"
H4: "Higher-confidence hypotheses are more predictable"
H5: "Shorter time horizons are more predictable (compare micro→session→short→long)"
H6: "Micro-hypothesis accuracy correlates with self-model accuracy (are we good at predicting our own behavior?)"

For each: compare groups, note significance, write to
world/knowledge/strategies/hypothesis-results.md
```

### Step 4: Update Meta-Memory

```
Bash: meta-set.sh meta-knowledge/_index.yaml  # update with all accuracy figures
Update aspirations meta via Bash: `aspirations-meta-update.sh <field> <value>`
```

### Step 5: Strategy Recommendations

```
1. Categories to focus on (accuracy > 70%)
2. Categories to reduce (accuracy < 40%)
3. Confidence calibration adjustments
4. Evaluation weight adjustments
5. Research depth recommendations
6. Source reliability actions
```

---

## Mode 4: Full Cycle (`--full-cycle`)

The comprehensive weekly review. Chains all modes plus deep reflection and replay.

```
1. invoke /review-hypotheses --resolve       (detect outcomes — idempotent if already done)
2. invoke /review-hypotheses --learn         (reflect on unlearned — idempotent if already done)
3. invoke /review-hypotheses --accuracy-report
4. invoke /reflect --full-cycle               (pattern extraction, calibration, replay)
5. Run spark check for new aspirations/goals
6. Propose strategy adjustments to /aspirations evolve
```

**Idempotency note:** If `/boot` already ran `--resolve` this session, step 1 is a no-op (nothing new to resolve). If aspiration goals already ran `--learn`, step 2 is a no-op (all records already reflected). The full-cycle still adds value via steps 3-6 which operate at the aggregate level.

---

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.

## Chaining

- **Called by**: `/aspirations` (as goal skill for hypothesis goals, `--resolve`/`--learn` for batch ops), `/boot` (`--resolve` only for catch-up)
- **Calls** (by mode):
  - `--resolve`: None (standalone — detects outcomes, records results)
  - `--learn`: `/reflect --on-hypothesis` (for each unreflected resolution)
  - `--full-cycle`: `--resolve`, `--learn`, `--accuracy-report`, `/reflect --full-cycle` (which includes `--batch-micro`)
- **Feeds into**: `/aspirations evolve` (strategy recommendations)
- **Updated by**: `/reflect` (provides patterns and strategies back)
