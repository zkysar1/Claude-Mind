---
name: reflect-hypothesis
description: "Single hypothesis reflection — full pipeline: horizon gate, ABC chain, differentiated extraction, contrastive extraction, encoding score, textual reflection, violation tracking, source tracking, journal, accuracy, pattern signatures, entities, beliefs, contradictions, context gaps, strategy tracking, spark check, knowledge reconciliation, tree growth"
user-invocable: false
parent-skill: reflect
triggers:
  - "/reflect-hypothesis"
  - "/reflect --on-hypothesis"
conventions: [pipeline, experience, tree-retrieval, reasoning-guardrails, pattern-signatures, spark-questions, aspirations]
---

# /reflect-hypothesis — Single Hypothesis Reflection

This sub-skill implements Mode 1 of `/reflect`. It is invoked by the parent `/reflect` router when `--on-hypothesis <hypothesis-id>` is specified, or during `--full-cycle` for each newly resolved hypothesis. It runs the full reflection pipeline: horizon gate, ABC chain analysis, differentiated and contrastive extraction, encoding score, textual reflection, violation tracking, source tracking, journal entry, accuracy reflection, pattern signatures, entity extraction, belief registry, contradiction detection, process-outcome classification, context gap analysis, strategy tracking, experiential index updates, spark check, knowledge reconciliation, and tree growth trigger.

## Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Step 0.5: Horizon Gate

```
Read the resolved record and check its horizon field (default: "short" if missing).

IF horizon == "micro":
    ERROR: Micro-hypotheses should never reach --on-hypothesis.
    They are batch-reflected via --batch-micro. Abort.

IF horizon == "session":
    Run LIGHTWEIGHT reflection path:
      - Generate ABC chain (Step 2) — abbreviated: skip skill_chain and research_depth
      - Skip Step 2.5 differentiated extraction UNLESS surprise >= 7
      - Skip Step 2.6 contrastive extraction
      - Generate encoding score (Step 2.7) — normal
      - Generate textual reflection (Step 3) — abbreviated (3-5 sentences max)
      - Skip Step 3.5 domain-specific reflection
      - Track violation (Step 4) — if corrected, normal
      - Update source tracking (Step 5) — only if external sources used
      - Append to journal (Step 6) — abbreviated entry
      - Accuracy reflection (Step 7) — normal
      - Skip Steps 7.5, 7.5b, 7.6, 7.6b (pattern sigs, entities, beliefs, contradictions)
      - Skip Step 7.6c process-outcome dual score
      - Skip Step 7.7 context gap analysis
      - Run spark check (Step 8) — normal (short hypotheses can still spark insights)
    RETURN after lightweight path — do not continue to full Step 1.

IF horizon == "short" OR horizon == "long" OR horizon is missing:
    Continue to full reflection below.
```

## Step 1: Load Hypothesis

```
Bash: pipeline-read.sh --id {hypothesis-id}  (loads the resolved pipeline record)
Read the original evaluation (scoring, reasoning, confidence)
Read any related knowledge articles that informed the hypothesis

IF hypothesis record has `experience_ref` field:
    Bash: experience-read.sh --id {experience_ref}
    Read the content .md file at the returned record's content_path
    Use this full-fidelity context to reconstruct exact information state at hypothesis formation time
    This replaces reliance on context_manifest paths alone — we now have what the content actually said
```

## Step 2: Generate ABC Chain

Model the hypothesis as an Antecedent-Behavior-Consequence chain:

```yaml
abc_chain:
  antecedents:
    - conditions: "Description of conditions when hypothesis was formed"
    - news_context: "What news/events were relevant"
    - data_signals: "What data points informed the hypothesis"
    - source_signals:
        - source: "source-id"
          signal: "What this source indicated"
          was_correct: true/false
  behavior:
    hypothesis: "YES/NO"
    confidence: 0.75
    reasoning: "Summary of our reasoning"
    skill_chain: ["/research-topic"]  # skills actually used during evaluation
    research_depth: "quick | moderate | deep"
  consequence:
    actual_outcome: "YES/NO"
    confirmed: true/false
    surprise_level: 1-10  # poignancy rating
    confidence_at_hypothesis: 0.65
    time_to_resolution: "N days"
```

```
IF experience record was loaded (from Step 1 experience_ref):
    Use verbatim_anchors from the experience for precise ABC chain construction:
    - Anchor "error-msg" → exact error text for Antecedent
    - Anchor "api-response" → exact response for Behavior context
    - This prevents hallucination in chain construction — anchors are ground truth
```

## Step 2.5: Differentiated Extraction (with Structured Triggers + Dual Classification Modulation)

After ABC chain analysis, apply extraction based on outcome. First compute `dual_classification` to modulate extraction priority:

```
Read hypothesis process_score (if populated by /review-hypotheses Step 4.1):
  If process_score.dual_classification exists:
    Use it to modulate extraction below
  If not yet computed (process_score.process_quality is null):
    Defer modulation — extract normally (computed later in Step 7.6c)
```

**For CONFIRMED (confirmed hypotheses):**
- If `dual_classification == lucky_confirmed`: LOW PRIORITY extraction — the reasoning was flawed despite confirmed outcome. Create reasoning bank entry but tag `confidence: low`, add note: "Lucky confirmed — do not reinforce this reasoning pattern"
- Otherwise (earned_confirmed or unclassified): Strategy Validation step:
  1. Identify the key reasoning that led to the confirmed hypothesis
  2. Extract transferable reasoning chain (what steps were decisive?)
  3. Create a reasoning bank entry (type: success) via script:
     ```bash
     echo '<JSON>' | bash core/scripts/reasoning-bank-add.sh
     ```
     JSON fields: id (rb-NNN), title, description, content (reasoning summary), type ("success"),
     source_hypothesis, outcome, category, tags, status ("active"),
     when_to_use (conditions, category, confidence_range), utilization (initialized to zeros)
  4. Include `when_to_use` field — structured trigger conditions:
     ```yaml
     when_to_use:
       conditions: ["condition extracted from ABC antecedents"]
       category: "{hypothesis.category}"
       confidence_range: [0.0, 1.0]  # narrow based on evidence strength
     ```
  5. Utilization tracking is initialized automatically by the script:
     ```yaml
     utilization:
       retrieval_count: 0
       last_retrieved: null
       times_helpful: 0
       times_noise: 0
       times_active: 0
       times_skipped: 0
       utilization_score: 0.0
     ```

**For CORRECTED (corrected hypotheses):**
- If `dual_classification == unlucky_corrected`: SKIP guardrail extraction — the process was sound, outcome was variance. Log: "Unlucky corrected — process was sound, no guardrail needed." Still create reasoning bank entry (type: failure) for the record but mark `guardrail_skipped: true`.
- Otherwise (deserved_corrected or unclassified): Preventive Guardrail step:
  1. Identify what went wrong — which evaluation step failed?
  2. Extract a preventive guardrail: "Next time, check [X] before hypothesizing [Y]"
  3. Add guardrail via script:
     ```bash
     echo '<JSON>' | bash core/scripts/guardrails-add.sh
     ```
     JSON fields: id (guard-NNN), title, description, category, status ("active"),
     when_to_use (conditions, category), utilization (initialized to zeros)
     Include `when_to_use` field on the guardrail:
     ```yaml
     when_to_use:
       conditions: ["trigger condition from failure analysis"]
       category: "{hypothesis.category}"
     ```
     Utilization tracking is initialized automatically by the script
  4. Create a reasoning bank entry (type: failure) with failure_lesson and preventive_guardrail fields via script:
     ```bash
     echo '<JSON>' | bash core/scripts/reasoning-bank-add.sh
     ```

## Step 2.6: Contrastive Extraction (CONFIRMED/CORRECTED Pairing)

After differentiated extraction, check if a contrastive pair exists — a CONFIRMED and CORRECTED in the same category that can be compared to extract what distinguished success from failure.

```
Bash: pipeline-read.sh --stage resolved
Find the most recent CONFIRMED in the same category as this hypothesis
Find the most recent CORRECTED in the same category as this hypothesis

If both a CONFIRMED and CORRECTED exist in this category AND they haven't been paired before:
  Compare their ABC chains side by side:
    - What antecedent conditions differed?
    - What behavioral choices (reasoning, sources, confidence) differed?
    - What consequences differed?

  Extract contrastive insight:
    confirmed_id: "{confirmed hypothesis id}"
    corrected_id: "{corrected hypothesis id}"
    distinguishing_factors:
      - factor: "{what was different}"
        present_in_confirmed: true/false
        present_in_corrected: true/false
    contrastive_lesson: "In {category}, success correlates with {X} while failure correlates with {Y}"

  Create reasoning bank entry (type: contrastive) via script:
    echo '<JSON>' | bash core/scripts/reasoning-bank-add.sh
    JSON fields: id (rb-NNN), title, description, content (contrastive analysis),
            type ("contrastive"), confirmed_source, corrected_source, category, tags,
            when_to_use (derived from confirmed conditions),
            utilization (initialized to zeros), status ("active")

  Log: "CONTRASTIVE EXTRACTION: {category} — CONFIRMED {confirmed_id} vs CORRECTED {corrected_id}"

If no pair available (all confirmed or all corrected in category): skip contrastive extraction
If pair already extracted (check reasoning-bank-read.sh --active for existing contrastive with same sources): skip
```

## Step 2.6b: Archive Reflection Insight as Experience

```
# Archive reflection insight as experience
experience_id = "exp-reflect-{hypothesis_id}"
Write mind/experience/{experience_id}.md with:
    - ABC chain analysis
    - Extracted strategy or guardrail
    - Contrastive analysis (if applicable)
    - What was learned and why it matters
echo '<experience-json>' | bash core/scripts/experience-add.sh
Experience JSON:
    id: "{experience_id}"
    type: "reflection"
    created: "{ISO timestamp}"
    category: "{hypothesis category}"
    summary: "Reflection on {hypothesis_id}: {key insight}"
    hypothesis_id: "{hypothesis_id}"
    tree_nodes_related: [nodes updated during reflection]
    verbatim_anchors: [key quotes from ABC chain, exact strategy text]
    content_path: "mind/experience/{experience_id}.md"
```

## Step 2.7: Memory Encoding Score (Hippocampal Gate)

Calculate encoding priority for this reflection — determines whether this observation
gets committed to long-term memory or discarded. Based on hippocampal encoding filters.

```
Read core/config/memory-pipeline.yaml → encoding_gate thresholds

Calculate component scores:
  novelty:          0-1 — how different is this outcome from past patterns?
                    1.0 if first hypothesis in this category, 0.1 if routine confirmation
  outcome_impact:   0-1 — impact magnitude based on outcome significance
                    1.0 if high-stakes or major consequence, 0.5 if moderate, 0.1 if routine
  surprise:         surprise_level / 10 (from ABC chain)
  goal_relevance:   1.0 if related to active aspiration goal, 0.5 otherwise
  repetition:       0.1 * times this cause_category has occurred (from violations.md)

encoding_score = (novelty * 0.30) + (outcome_impact * 0.25) + (surprise * 0.20) +
                 (goal_relevance * 0.15) + (repetition * 0.10)

If encoding_score >= 0.40 (encode_threshold):
    # PRECISION EXTRACTION: Before compressing, extract exact values from ABC chain
    # Build precision_manifest from: data_signals, consequence actual_outcome,
    # experience verbatim_anchors if loaded. Include ALL numbers, code refs,
    # error codes, thresholds, formulas, config values, commit hashes, line numbers.
    # See mind/conventions/precision-encoding.md for full extraction heuristics.
    # Each item: {type, label, value (VERBATIM), unit, context}
    # Empty list [] only if genuinely no precise values in this hypothesis.

    # Precision density bonus: precision-rich findings get encoding priority
    precision_item_count = len(precision_manifest)
    IF precision_item_count >= 3: encoding_score += 0.10
    ELIF precision_item_count >= 1: encoding_score += 0.05
    encoding_score = min(1.0, encoding_score)

    echo '<json>' | wm-append.sh encoding_queue  # item fields:
      observation: {compressed ABC summary}
      precision_manifest:    # MANDATORY — structured exact values from this hypothesis
        - type: "{threshold|formula|constant|reference|measurement|config_value}"
          label: "{descriptive name}"
          value: "{exact value — VERBATIM}"
          unit: "{if applicable}"
          context: "{where this applies}"
      source_experience: "{experience_id if available — for full-fidelity retrieval}"
      encoding_score: {score}
      target_article: {best matching leaf node for this insight}
      priority_class: {violations|high_surprise|high_outcome_impact|routine}
      timestamp: now
    Tag: "HIGH-PRIORITY ENCODING"

If encoding_score < 0.15 (skip_threshold):
    Log: "LOW-ENCODING: routine outcome — {record-slug} ({score})"
    Still track in violations.md if wrong, but mark priority: low

If encoding_score 0.15-0.40 (review_range):
    Flag for end-of-session review
    echo '<json>' | wm-append.sh sensory_buffer  # (not encoding_queue)
```

## Step 3: Generate Textual Reflection

Write a structured reflection (this becomes episodic memory):

```yaml
reflection:
  hypothesis_id: "2026-03-15_record-slug"
  date: "2026-03-20"
  level: 0  # episode-level
  expected: "YES at 0.75 confidence"
  actual: "NO"
  confirmed: false
  surprise_level: 8
  what_i_hypothesized_and_why: "..."
  what_actually_happened: "..."
  what_i_missed: "..."
  what_i_would_do_differently: "..."
  what_i_got_right: "..."
  confidence_calibration_note: "I was 75% confident but wrong — overconfident"
  skill_chain_assessment: "Research was insufficient — should have used deep evaluation"
  source_assessment:
    - source: "source-id"
      reliable_this_time: true/false
  pattern_identified: "Description of any pattern this fits"
  category: "politics/crypto/sports/..."
  poignancy: 8  # 1-10, higher = more significant for learning
```

## Step 3.5: Domain-Specific Reflection

1. Read `core/config/reflection-templates.yaml`
2. Look up the hypothesis's category
3. Use category-specific questions instead of default questions
4. If category not found in templates, use the `default` template
5. The template's `focus` field guides the overall reflection emphasis

## Step 4: Track Violation (if hypothesis was wrong)

If the hypothesis was corrected, append to `mind/knowledge/patterns/violations.md`:

```markdown
### YYYY-MM-DD: record-slug
- **Hypothesis**: YES at 75% confidence
- **Actual**: NO
- **Surprise level**: 8/10
- **What I expected and why**: ...
- **What actually happened**: ...
- **Root cause category**: [see categories below]
- **Lesson**: ...
- **Times this cause category has occurred**: N (increment)
- **Confidence in lesson**: 0.4 (low for first occurrence, grows with reinforcement)
```

Root cause categories:
- `polling-error` — Source data was wrong
- `resolution-ambiguity` — Outcome resolved differently than expected
- `black-swan` — Unpredictable event occurred
- `information-lag` — We had stale information
- `overconfidence` — We were too sure about insufficient evidence
- `model-error` — Our reasoning framework was flawed
- `crowd-was-right` — We were contrarian and wrong
- `timing-error` — Right direction, wrong timeframe

## Step 5: Update Source Tracking

If the resolved hypothesis has `source_validation` in its record, use the structured path below (skip the legacy fallback). Otherwise, use the legacy path for older hypotheses without source_validation.

**Legacy path** (no source_validation on record):
```
For each information source used in the hypothesis:
  Read mind/sources.yaml
  Find or create source entry
  Increment total_signals
  If correct: increment correct_signals
  Recalculate reliability: correct_signals / total_signals
  Write updated sources.yaml
```

**Source Agreement path** (source_validation exists — single source of truth, replaces legacy):
```
For each source in source_validation.sources:
  Find or create source entry in mind/sources.yaml
  Increment total_signals
  Update last_seen to today
  If source.verdict matches actual outcome: increment correct_signals
  Update reliability = correct_signals / total_signals
  Update agreement_record:
    If source_validation.agreement == "unanimous": increment times_in_unanimous
    If source_validation.agreement == "contested": increment times_in_contested
    If source verdict != actual outcome AND agreement == "contested": increment times_was_minority
  Add hypothesis category to source's categories list (deduplicate)
```

## Step 6: Append to Journal

Write reflection to `mind/journal/YYYY/MM/YYYY-MM-DD.md`:

```markdown
## Reflection: {record-slug}

**Result**: {Confirmed/Corrected} — hypothesized {YES/NO} at {confidence}%, actual {outcome}
**Surprise**: {N}/10
**Key lesson**: {one-liner lesson}
**Pattern**: {pattern identified or "none yet"}
**Sources used**: {list with reliability assessment}
**Would change**: {what we'd do differently}
```

## Step 7: Accuracy Reflection

Assess whether confidence was calibrated correctly relative to the outcome:

```
Read the hypothesis's reasoning_summary, edge_basis, key_assumption

Accuracy dimensions to assess:
1. Confidence calibration — was our stated confidence appropriate?
   - If CONFIRMED at low confidence (<60%): underconfident — raise confidence for similar cases
   - If CORRECTED at high confidence (>80%): overconfident — lower confidence or add checks
   - If CONFIRMED at high confidence: well-calibrated — reinforce approach
   - If CORRECTED at low confidence: expected variance — no adjustment needed

2. Category accuracy — is this category performing well or poorly?
   - Track accuracy per category over time
   - Flag categories with accuracy < 40% as underperforming
   - Flag categories with accuracy > 70% as strengths

3. Reasoning → Outcome link (CRITICAL for learning):
   - Read reasoning_summary: WHY did we hypothesize this way?
   - Read edge_basis: WHAT created our information edge?
   - Read key_assumption: What could go wrong?
   - If CONFIRMED: Was our reasoning actually right, or did we get lucky?
     - Did the edge_basis hold?
     - Was the key_assumption validated?
   - If CORRECTED: WHY were we wrong?
     - Did the key_assumption fail? → Log as "assumption_failure"
     - Was the edge_basis invalid? → Log as "edge_basis_failure"
     - Was it random/unpredictable? → Log as "variance"
   - Track edge_basis success rates over time
   - This feeds directly into strategy evolution:
     - If an edge_basis has < 40% success rate → STOP relying on it
     - If an edge_basis has > 70% success rate → INCREASE confidence when using it

Write accuracy reflection to journal alongside hypothesis reflection.
Include: "Reasoning: {reasoning_summary} | Edge: {edge_basis} | Result: {outcome} | Lesson: {lesson}"
```

## Step 7.5: Update Pattern Signatures (Dentate Gyrus Learning)

After each reflection, update the pattern signature library so the system's
pattern separation and completion improve over time.

```
Bash: pattern-signatures-read.sh --active   (load all active pattern signatures)

1. MATCH CHECK: Does this resolved hypothesis match an existing signature?
   Compare conditions from the ABC chain against each signature's conditions

   If MATCH found:
     Record outcome via script:
       bash core/scripts/pattern-signatures-record-outcome.sh {sig-id} CONFIRMED   # or CORRECTED
     This atomically increments outcome_stats.total, outcome_stats.confirmed (if CONFIRMED),
     recalculates accuracy, and updates last_matched.

     If outcome was surprising (surprise >= 7):
       Extract what made this case different
       Add new separation_marker to the signature
       Log: "DG LEARNING: {signature.name} — new separation marker: {marker}"

     If outcome contradicted signature's expected behavior:
       Decrease signature confidence by 0.05
       Add entry to confused_with if identifiable pattern

     Evaluate pattern signature status:
       hit_rate = outcome_stats.confirmed / outcome_stats.total
       If hit_rate < 0.30 AND outcome_stats.total >= 10: set status: contradicted

2. NEW PATTERN CHECK: Is this a new recognizable pattern?
   If the ABC chain reveals conditions NOT matching any signature:
     AND the same condition has occurred 2+ times (check violations.md for repeats):
       Create new signature entry via script:
         echo '<JSON>' | bash core/scripts/pattern-signatures-add.sh
         JSON fields: id (sig-{next number}), name ({descriptive kebab-case name}),
         conditions ({from hypothesis_context}), outcome_stats ({total: 1, confirmed: 0 or 1}),
         retrieval_cues ({from antecedents}), separation_markers ({what makes this different}),
         status ("active"), created_session ({current session_count from aspirations-read.sh --meta})
       Log: "NEW PATTERN SIGNATURE: {name} — discovered from {hypothesis-id}"

3. CONFUSION CHECK: Was a pattern wrongly matched during evaluation?
   If the hypothesis's evaluation record shows a pattern_match
   AND the outcome contradicts the matched signature's expected behavior:
     Add this case to confused_with for the matched signature:
       sig_id: {correct signature}
       name: {correct pattern name}
       distinguishing_feature: {what was different}
     Log: "PATTERN SEPARATION UPDATE: {matched} ≠ {actual} — {distinguishing feature}"
```

## Step 7.5b: Entity Extraction and Cross-Linking

Extract entities mentioned in the ABC chain and update the entity index for cross-link retrieval.

```
From the ABC chain (Step 2), extract named entities:
  - People, organizations, concepts, metrics, events mentioned in antecedents
  - Key terms from behavior.reasoning
  - Normalize to lowercase-kebab-case (e.g., "Federal Reserve" → "federal-reserve")

Read mind/knowledge/tree/_tree.yaml → entity_index (create section if missing)

For each extracted entity:
  If entity already in index:
    - Add this hypothesis's resolved record path to articles list (if not already present)
    - Add relevant tree node IDs to tree_nodes list (from Step 0 context)
    - Increment mention_count
  If entity is new:
    - Read core/config/tree.yaml → entity_index.max_entities
    - If total_entities < max_entities:
        Create entry: {articles: [record_path], tree_nodes: [node_ids], mention_count: 1}
        Increment total_entities
    - Else: skip (index full)

Write updated entity_index back to _tree.yaml
Log: "ENTITY INDEX: updated {N} entities from {hypothesis-id}"
```

## Step 7.6: Belief Registry Update

1. Read `mind/knowledge/beliefs.yaml`
2. Identify beliefs relevant to this hypothesis (match by category, entities)
3. For each relevant belief, classify the outcome's impact:
   - `reinforce`: outcome confirms belief → increase confidence by 0.05-0.15
   - `weaken`: outcome mildly contradicts → decrease confidence by 0.10-0.20
   - `contradict`: strong evidence against → decrease by 0.20-0.30, set status to "contradicted"
   - `neutral`: no impact
4. Add trajectory entry: { session, confidence (new), evidence (brief), classification }
5. Update `last_updated` timestamp
6. If status changes to "contradicted": log transition in `mind/knowledge/transitions.yaml`

## Step 7.6b: Contradiction Detection

1. Check `interference_with` field in any articles updated during this reflection
2. If interference detected:
   a. Read the interfering article
   b. Determine if this is a genuine contradiction or compatible knowledge
   c. If contradiction: create entry in `mind/knowledge/transitions.yaml`
      - entity, belief_id (if applicable), from, to, evidence, impact
   d. If compatible: update both articles to clarify relationship
3. Cross-reference with `mind/knowledge/beliefs.yaml` -- if belief weakened below 0.20, mark as "contradicted"

## Step 7.6c: Process-Outcome Dual Classification

For each resolved hypothesis in this reflection batch, compute dual classification
from outcome + original confidence (no step-attribution dependency):

    IF outcome == "CONFIRMED" AND confidence >= 0.60:
        dual_classification = "earned_confirmed"
    ELIF outcome == "CONFIRMED" AND confidence < 0.60:
        dual_classification = "lucky_confirmed"
    ELIF outcome == "CORRECTED" AND confidence >= 0.60:
        dual_classification = "unlucky_corrected"
    ELIF outcome == "CORRECTED" AND confidence < 0.60:
        dual_classification = "deserved_corrected"

    process_quality = confidence if CONFIRMED else (1.0 - confidence)

    Bash: pipeline-update-field.sh {id} process_score.dual_classification {dual_classification}
    Bash: pipeline-update-field.sh {id} process_score.process_quality {process_quality}

## Step 7.7: Context Gap Analysis (Context Manifest Review)

If the hypothesis record has a `context_consulted` section, analyze whether the right context was loaded:

```
Read hypothesis.context_consulted (if exists — older hypotheses may not have this)

# Step A: Check for MISSED context
# Were there relevant tree nodes, pattern signatures, or articles that EXISTED
# at hypothesis time but were NOT consulted?

Read mind/knowledge/tree/_tree.yaml → find all nodes matching this hypothesis's category
Compare tree_nodes_read against available nodes:
  If a relevant node existed but wasn't in tree_nodes_read:
    Append to context_consulted.context_gaps_identified:
      - type: "missed_tree_node"
        node_id: "{node}"
        relevance: "Would have provided {insight}"

Bash: pattern-signatures-read.sh --active
Compare pattern_signatures_checked against all signatures matching this category:
  If a relevant signature existed but wasn't checked:
    Append to context_consulted.context_gaps_identified:
      - type: "missed_pattern"
        signature_id: "{sig-NNN}"
        relevance: "Pattern {name} would have flagged {issue}"

# Step B: Assess context quality → outcome correlation
If hypothesis was CORRECTED AND context_gaps were found:
    Log: "CONTEXT GAP CONTRIBUTED TO CORRECTED HYPOTHESIS: {gap description}"
    Increase encoding_score by 0.15 (this is a high-learning-value observation)

If hypothesis was CONFIRMED AND full context was loaded:
    Log: "Full context load correlated with confirmed hypothesis"

# Step C: Write gaps back to resolved record
Bash: pipeline-update-field.sh {hypothesis-id} context_gaps_identified '<JSON array>'
```

## Step 7.7d: Finalize Context Quality Rating (Retrieval Protocol Phase 5)

If the resolved record's `context_quality.usefulness` is `pending` or missing, rate it now with full reflection context:

```
Read hypothesis.context_consulted.context_quality

If usefulness is "pending" or missing:
    Rate using ABC chain and violation analysis:
      - CONFIRMED + context supported reasoning → "helpful"
      - CONFIRMED + context irrelevant to why we were right → "neutral"
      - CORRECTED + context contributed to the error → "misleading"
      - CORRECTED + context had no bearing → "irrelevant"
      - CONFIRMED + no context loaded → "neutral"
      - CORRECTED + no context loaded → "irrelevant"

    Identify most_valuable_source:
      Which loaded item's information appears in the ABC antecedents or behavior?
      Format: "{layer}:{id}" — or "none" if nothing was useful

    Identify least_valuable_source:
      Which loaded item added zero value to the reasoning chain?
      Format: "{layer}:{id}" — or "none" if all were useful

    Write chain_note: one-sentence linking context quality to outcome
    Update resolved record with finalized context_quality

If usefulness already rated (not "pending"):
    Only override if reflection reveals the rating was wrong
    If corrected: append " (corrected by /reflect)" to chain_note
```

## Step 7.7e: Strategy Usage Tracking

When a strategy is loaded and marked ACTIVE in deliberation:
- Increment `times_applied` by 1
- Set `last_applied` to today's date

```
Read hypothesis context_consulted.deliberation (if exists)
For each strategy from mind/knowledge/strategies/extracted-strategies.md that was loaded:
  If deliberation marks this strategy as ACTIVE:
    Increment strategy's times_applied by 1
    Set strategy's last_applied to today's date
    Write updated strategy back to extracted-strategies.md
```

## Step 7.7f: Update Experiential Index + Utilization Scores

Runs after Step 7.7d so the context quality rating is always finalized before aggregation.

```
# Part 1: Experiential Index
Read mind/experiential-index.yaml (if exists, else skip)
Update by_context_quality section:
  - Increment total for this category
  - Increment the usefulness bucket (helpful/neutral/misleading/irrelevant)
  - If most_valuable_source set: increment usage count for that layer type
  - If least_valuable_source set: increment noise count for that layer type
  - Recalculate helpful_rate = helpful_count / total for this category

# Part 2: Utilization Score Updates (ReasoningBank tracking)
Read hypothesis context_consulted.deliberation (if exists)
For each reasoning bank entry (rb-NNN) or guardrail (guard-NNN) that was loaded:

  If deliberation marks this item as ACTIVE:
    bash core/scripts/reasoning-bank-increment.sh {id} utilization.times_active   # or guardrails-increment.sh for guard-NNN
  If deliberation marks this item as SKIPPED:
    bash core/scripts/reasoning-bank-increment.sh {id} utilization.times_skipped  # or guardrails-increment.sh for guard-NNN

  If context_quality.most_valuable_source references this item's layer:id:
    bash core/scripts/reasoning-bank-increment.sh {id} utilization.times_helpful  # or guardrails-increment.sh for guard-NNN
  If context_quality.least_valuable_source references this item's layer:id:
    bash core/scripts/reasoning-bank-increment.sh {id} utilization.times_noise    # or guardrails-increment.sh for guard-NNN

  The increment scripts automatically recalculate utilization_score.

  Read back updated record to check retirement candidacy:
  If utilization_score < 0.20 AND retrieval_count >= 5:
    Log: "LOW UTILIZATION: {entry_id} — score {utilization_score} after {retrieval_count} retrievals — candidate for retirement"

# Update experience retrieval stats (if experiences were consulted during this reflection)
For each experience record consulted during this reflection cycle:
    IF the experience was helpful (informed the ABC chain or strategy extraction):
        bash core/scripts/experience-update-field.sh {exp-id} retrieval_stats.times_useful {n+1}
    ELSE:
        bash core/scripts/experience-update-field.sh {exp-id} retrieval_stats.times_noise {n+1}
```

## Step 8: Spark Check

After every reflection, ask:
1. Does this lesson change how we should discover opportunities? → Update discovery filters
2. Does this challenge an existing knowledge article? → Flag for re-research
3. Should we adjust category priorities? → Propose aspiration evolution
4. Is there a new trap type to watch for? → Add to `mind/knowledge/patterns/traps.md`
5. Should a new aspiration be created? → Propose via gap analysis
6. Should confidence thresholds change for any category? → Propose calibration adjustment

**Q6: Capability Gap Detection** (feeds /forge-skill):
```
Did we hit a CAPABILITY GAP during this hypothesis cycle?
Gap detection heuristics:
  - "Did I perform a manual multi-step operation that could be a skill?"
  - "Did I need data that no existing skill provides?"
  - "Did I repeat the same procedure I've done before?"
  - "Did I use WebSearch as a workaround for something a structured skill would handle?"
  - "Did I hit a tool failure and have to improvise?"

If YES:
  Read mind/skill-gaps.yaml
  If gap already exists → increment times_encountered, append to encounter_log
  If gap is new → create new entry with id: gap-{next}, status: registered
  Write updated mind/skill-gaps.yaml

  # --- Forge criteria check: turn ready gaps into goals ---
  # GUARD: skip already-forged gaps (Phase 9.2 also checks this)
  IF gap.status == "forged": skip forge criteria check

  Read core/config/skill-gaps.yaml → forge_threshold (default: 2)
  Read mind/developmental-stage.yaml → current stage
  IF gap.times_encountered >= forge_threshold
     AND gap.estimated_value >= "medium"
     AND developmental stage >= EXPLOIT (developing+):

    # Check no pending forge goal already exists for this gap
    Bash: load-aspirations-compact.sh → IF path returned: Read it
    (compact data has IDs, titles, args — no descriptions/verification)
    Search all goals for args containing this gap's ID → if found, skip

    # Route to target aspiration (same as sq-013 step 2):
    #   1. Current aspiration (if it accepts new goals)
    #   2. Another active aspiration with matching category
    #   3. /create-aspiration from-self (last resort)
    Determine target_aspiration_id

    # Build and add the forge goal
    Build goal JSON:
      title: "Forge skill: {gap.procedure_name}"
      skill: "/forge-skill"
      args: "skill {gap.id}"
      priority: "MEDIUM"
      status: "pending"
      discovered_by: current hypothesis ID (if available)
      discovery_type: "capability_gap"
      verification:
        outcomes: ["New skill registered in _tree.yaml with forged: true"]
        checks: [{type: file_check, target: ".claude/skills/{name}/SKILL.md", condition: "File exists"}]

    Add goal to target aspiration via aspirations-update.sh
    Log: echo '{"date":"...","event":"forge-ready","details":"Gap {gap.id} met forge criteria (encountered {N}x), goal created in {asp-id}","trigger_reason":"reflect-q6-gap-detection"}' | bash core/scripts/evolution-log-append.sh
    Log in journal: "Forge-ready: gap {gap.id} → goal added to {asp-id}"
```

**Q7: Update Experiential Memory Index**:
```
Read mind/experiential-index.yaml (if exists, else skip)
Update indexes with this hypothesis's data:
  - by_violation_cause: if corrected, add to relevant cause bucket
  - by_category: update accuracy, confirmed/corrected, update exemplar_confirmed/corrected if notable
  - by_edge_bucket: classify edge size, update accuracy for bucket
  - by_context_quality: update based on Step 7.7 analysis
Write updated experiential-index.yaml
```

**Q8: Skill Underperformance Detection** (feeds /forge-skill):
```
Did an existing skill UNDERPERFORM during this hypothesis cycle?
  - "Did a forged skill fail or produce wrong output?"
  - "Did I have to work around a skill's limitation?"
  - "Did a skill's procedure need manual correction?"

If YES:
  Log underperformance event in journal
  If 3+ underperformance events for same skill → create goal to retire/fix the skill
```

## Step 8.25: Knowledge Reconciliation Sweep

After extracting lessons from a hypothesis outcome, check whether the lesson
invalidates existing knowledge beyond the hypothesis record itself.

```
IF hypothesis was CORRECTED or had surprise >= 7:
    Read mind/knowledge/tree/_tree.yaml
    For each node consulted during this hypothesis lifecycle (from context):
        Read node .md file
        Compare node content against lesson learned:
          - If lesson CONTRADICTS something the node states → update immediately
          - If lesson REFINES the node's understanding → append compressed insight
          - If no bearing → skip
        IF updated:
            Set last_update_trigger: {type: "post-reflection-reconciliation",
                source: hypothesis-id, session: N}
            Log: "KNOWLEDGE RECONCILIATION: {node_key} updated after {hypothesis-id}"
        IF update needed but too complex for inline fix:
            echo '<json>' | wm-append.sh knowledge_debt

# Post-reflection reconciliation extends to experience-informed nodes
Bash: experience-read.sh --most-retrieved 10
For each high-retrieval experience with tree_nodes_related:
    For each related tree node:
        IF node was last_updated BEFORE the reflection that just ran:
            Check if reflection insights should update this frequently-consulted node
            IF yes: update node, set last_update_trigger: {type: "post-reflection-reconciliation"}
            Log as HIGH priority knowledge debt if deferred
```

## Step 8.5: Tree Growth Trigger

After pattern extraction, check if the tree needs to grow:
```
Read mind/knowledge/tree/_tree.yaml
Read core/config/tree.yaml for decompose_threshold, split_threshold
If new category detected without tree node:
  Add to _tree.yaml unmapped_categories
  Invoke /tree maintain (check if SPROUT needed)
For each leaf node updated during this reflection:
  line_count = count lines in node .md body (excluding YAML front matter)
  If line_count > decompose_threshold AND depth < D_max:
    bash core/scripts/tree-update.sh --set <node-key> growth_state ready_to_decompose
  Elif article_count crossed split_threshold:
    bash core/scripts/tree-update.sh --set <node-key> growth_state ready_to_split
If any growth_state changed OR unmapped_categories added:
  Invoke /tree maintain
```

## Tree Update Protocol

When tree nodes are updated during this reflection, invoke `/reflect-tree-update` to propagate changes upward through the memory tree.
