---
name: reflect-on-outcome
description: "Outcome reflection — hypothesis ABC chains, execution pattern signatures, batch micro-hypothesis processing"
user-invocable: false
parent-skill: reflect
triggers:
  - "/reflect-on-outcome"
  - "/reflect --on-hypothesis"
  - "/reflect --on-execution"
  - "/reflect --batch-micro"
conventions: [pipeline, experience, tree-retrieval, reasoning-guardrails, pattern-signatures, spark-questions, aspirations, handoff-working-memory]
minimum_mode: autonomous
---

# /reflect-on-outcome — Outcome-Based Reflection

This sub-skill implements all outcome-based reflection modes for `/reflect`. It is invoked
by the parent router for three modes:

- **Hypothesis mode** (`--on-hypothesis <id>`): Full single-hypothesis reflection pipeline
- **Execution mode** (`--on-execution`): Lightweight execution outcome reflection
- **Batch micro mode** (`--batch-micro`): Batch micro-hypothesis processing

Each mode section below is self-contained with its own step numbering.

## Mode: Hypothesis Reflection (--on-hypothesis <id>)

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

    # Meta-strategy category depth override
    Bash: meta-read.sh reflection-strategy.yaml
    IF hypothesis.category in category_depth_overrides:
        override = category_depth_overrides[hypothesis.category]
        IF override == "full" AND horizon == "session":
            Log: "META OVERRIDE: session horizon → full pipeline for category {hypothesis.category}"
            CONTINUE to full reflection pipeline (skip lightweight return)

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
Write <agent>/experience/{experience_id}.md with:
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
    content_path: "<agent>/experience/{experience_id}.md"
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
    # See core/config/conventions/precision-encoding.md for full extraction heuristics.
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

## Step 2.8: Divergent Alternatives (Creative Lens)

Before the convergent textual reflection, generate alternative explanations.
This step is the antidote to premature convergence — the pipeline produces
a single ABC chain (Step 2) which becomes THE explanation. But there may be
other explanations that are equally or more valid.

```
# Only fire for hypotheses with surprise >= 3 OR corrected outcomes
# (routine confirmed outcomes with low surprise are unlikely to have
# hidden alternative explanations worth generating)
IF abc_chain.consequence.surprise_level >= 3 OR NOT abc_chain.consequence.confirmed:

    Read core/config/reflection-templates.yaml → creative_lens.questions

    alternatives = []
    FOR EACH question in creative_lens.questions:
        Apply {question} to the ABC chain and outcome:
        IF the question generates a substantive alternative (not a restatement
           of the existing chain):
            alternatives.append({
                lens: question,
                alternative_explanation: the generated alternative,
                plausibility: 0.0-1.0,
                testable: true/false,
                test_description: "how to test" if testable
            })

    # Filter to top 3 most plausible alternatives
    alternatives = sorted(alternatives, key=plausibility, reverse=true)[:3]

    IF len(alternatives) > 0:
        Log: "DIVERGENT ALTERNATIVES: {len(alternatives)} alternative explanations generated"

        # Check: does any alternative have plausibility > 0.4?
        # If so, it deserves its own hypothesis for future verification.
        FOR EACH alt in alternatives:
            IF alt.plausibility >= 0.4 AND alt.testable:
                echo '{"claim":"{alt.alternative_explanation}","confidence":{alt.plausibility},"source_hypothesis":"{hypothesis.id}","source_step":"divergent_alternatives","horizon":"session","test_description":"{alt.test_description}"}' | Bash: wm-append.sh micro_hypotheses
                Log: "DIVERGENT -> HYPOTHESIS: '{alt.alternative_explanation}' (plausibility {alt.plausibility})"

        # Store alternatives for use in Step 3 (enriches the textual reflection)
        divergent_context = alternatives
    ELSE:
        divergent_context = []
        Log: "▸ Divergent alternatives: none generated (all lenses returned restatements)"

ELSE:
    divergent_context = []
    Log: "▸ Divergent alternatives: skipped (confirmed, surprise < 3)"
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
  divergent_alternatives:  # from Step 2.8, empty if skipped
    - lens: "What is the OPPOSITE..."
      explanation: "..."
      plausibility: 0.6
      testable: true
  creative_lens_finding: "One-sentence highest-value finding from creative lens questions"
```

## Step 3.5: Domain-Specific Reflection (with Creative Lens Supplement)

1. Read `core/config/reflection-templates.yaml`
2. Look up the hypothesis's category in `domain_templates`
3. Use category-specific questions instead of default questions
4. If category not found in `domain_templates`, use the `default` template
5. The template's `focus` field guides the overall reflection emphasis
6. **Creative Lens**: Only if Step 2.8 did NOT already run (i.e., `divergent_context` is empty):
   Read `creative_lens` template from the same file.
   Ask EACH creative_lens question about this outcome.
   For each creative_lens answer that generates a non-trivial insight:
   - Append to the reflection's `creative_lens_findings` list
   - If the insight suggests a testable prediction: add to micro_hypotheses in working memory
   Log: "Creative lens: {N}/{total} questions produced findings"
   IF Step 2.8 already ran: Log: "Creative lens: skipped (already ran in Step 2.8)"

## Step 4: Track Violation (if hypothesis was wrong)

If the hypothesis was corrected, append to `world/knowledge/patterns/violations.md`:

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
  Bash: world-cat.sh sources.yaml
  Find or create source entry
  Increment total_signals
  If correct: increment correct_signals
  Recalculate reliability: correct_signals / total_signals
  Write updated sources.yaml via world path resolution
```

**Source Agreement path** (source_validation exists — single source of truth, replaces legacy):
```
For each source in source_validation.sources:
  Find or create source entry in world/sources.yaml
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

Write reflection to `<agent>/journal/YYYY/MM/YYYY-MM-DD.md`:

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
   - If root cause is "model-error" or "overconfidence":
     Apply first-principles analysis to the hypothesis category:
     1. What assumptions did we inherit about this category?
     2. Which are verifiable vs inherited?
     3. Rebuild the category mental model from verified facts only
     4. Log what changed — this becomes the guardrail/reasoning bank content
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

Bash: world-cat.sh knowledge/tree/_tree.yaml  # entity_index (create section if missing)

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

1. Bash: world-cat.sh knowledge/beliefs.yaml
2. Identify beliefs relevant to this hypothesis (match by category, entities)
3. For each relevant belief, classify the outcome's impact:
   - `reinforce`: outcome confirms belief → increase confidence by 0.05-0.15
   - `weaken`: outcome mildly contradicts → decrease confidence by 0.10-0.20
   - `contradict`: strong evidence against → decrease by 0.20-0.30, set status to "contradicted"
   - `neutral`: no impact
4. Add trajectory entry: { session, confidence (new), evidence (brief), classification }
5. Update `last_updated` timestamp
6. If status changes to "contradicted": log transition in `world/knowledge/transitions.yaml`

## Step 7.6b: Contradiction Detection

1. Check `interference_with` field in any articles updated during this reflection
2. If interference detected:
   a. Read the interfering article
   b. Determine if this is a genuine contradiction or compatible knowledge
   c. If contradiction: create entry in `world/knowledge/transitions.yaml`
      - entity, belief_id (if applicable), from, to, evidence, impact
   d. If compatible: update both articles to clarify relationship
3. Cross-reference with `world/knowledge/beliefs.yaml` -- if belief weakened below 0.20, mark as "contradicted"

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

Bash: world-cat.sh knowledge/tree/_tree.yaml  # find nodes matching hypothesis category
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
For each strategy from world/knowledge/strategies/extracted-strategies.md that was loaded:
  If deliberation marks this strategy as ACTIVE:
    Increment strategy's times_applied by 1
    Set strategy's last_applied to today's date
    Write updated strategy back to extracted-strategies.md
```

## Step 7.7f: Update Experiential Index + Utilization Scores

Runs after Step 7.7d so the context quality rating is always finalized before aggregation.

```
# Part 1: Experiential Index
Read <agent>/experiential-index.yaml (if exists, else skip)
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
4. Is there a new trap type to watch for? → Add to `world/knowledge/patterns/traps.md`
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
  Bash: meta-read.sh skill-gaps.yaml
  If gap already exists → increment times_encountered, append to encounter_log
  If gap is new → create new entry with id: gap-{next}, status: registered
  Write updated skill-gaps.yaml via meta-set.sh

  # --- Forge criteria check: turn ready gaps into goals ---
  # GUARD: skip already-forged gaps (Phase 9.2 also checks this)
  IF gap.status == "forged": skip forge criteria check

  Read core/config/skill-gaps.yaml → forge_threshold (default: 2)
  Read <agent>/developmental-stage.yaml → current stage
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
Read <agent>/experiential-index.yaml (if exists, else skip)
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
IF hypothesis was CORRECTED or had surprise >= 5 or encoding_score >= 0.6:
    Bash: world-cat.sh knowledge/tree/_tree.yaml
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
Bash: world-cat.sh knowledge/tree/_tree.yaml
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

## Mode: Execution Reflection (--on-execution)

Lightweight reflection on goal execution outcomes. Complements Phase 6.5 (immediate
learning) which handles guardrails and reasoning bank entries during spark checks.
This sub-skill handles what Phase 6.5 does NOT:

1. **Pattern signature** matching/creation from execution patterns
2. **Contradiction detection** against existing knowledge tree nodes
3. **Investigation goal creation** for findings that need follow-up
4. **Experience archival** of execution learning events

**Does NOT create guardrails or reasoning bank entries** — Phase 6.5 already owns those.
Clear separation: Phase 6.5 = create new artifacts (guardrails + rb),
this skill = cross-reference against institutional knowledge + schedule follow-up work.

## Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Step 0.5: Notability Gate (Structural)

Four structural checks on the execution outcome. If NONE fire, RETURN immediately
with zero learning overhead. This gate keeps the step nearly free for routine goals.

```
signals = []

# 1. MISTAKE: execution had errors, retries, workarounds, or failed verification
IF execution had errors, required retries, used workarounds, or verification failed:
    signals.append("mistake")

# 2. SURPRISE: outcome differed from what the agent expected before executing
#    Compare: goal.verification.outcomes (expected) vs actual result
IF execution outcome materially differs from expected outcomes:
    signals.append("surprise")

# 3. RECURRING PATTERN: same procedure/condition seen in a different goal category
#    Check working memory + recent journal for similar outcomes across categories
IF execution involved a procedure or condition encountered in a DIFFERENT goal category:
    signals.append("recurring_pattern")

# 4. VERIFICATION_GAP: Phase 5 escalation flagged a missing negative check,
#    or goal involved code changes with no test execution.
#    Catches "subtly wrong" outcomes invisible to signals 1-3.
IF goal completed AND goal.verification.checks is empty:
    Bash: wm-read.sh sensory_buffer --json
    IF sensory_buffer contains verification_gap entry for this goal.id:
        signals.append("verification_gap")
    ELIF goal execution involved code edits (Edit/Write to source files) but no test command was run:
        signals.append("verification_gap")

IF len(signals) == 0:
    Output: "▸ Exec reflection: No notable execution signals — skipped"
    RETURN
```

## Step 1: Pattern Signature Check

Match execution against existing pattern signatures. Record outcomes for matched
signatures, create new signatures for recurring patterns.

```
Bash: pattern-signatures-read.sh --active → load active signatures

# 1a. MATCH: Does this execution match an existing pattern signature?
matched = false
FOR EACH signature:
    IF execution conditions align with signature.conditions
       AND signature.category matches or is related to goal.category:

        IF execution succeeded (goal completed, verification passed):
            bash core/scripts/pattern-signatures-record-outcome.sh {sig-id} CONFIRMED
        ELSE:
            bash core/scripts/pattern-signatures-record-outcome.sh {sig-id} CORRECTED

        IF "surprise" in signals:
            # Add separation marker — what made this case different from expectation
            separation_marker = extract distinguishing factor from execution context
            bash core/scripts/pattern-signatures-update-field.sh {sig-id} separation_markers '<appended JSON>'
            Log: "EXEC PATTERN MATCH: {sig-id} — new separation marker: {marker}"

        matched = true
        BREAK

# 1b. NEW: Create a new pattern signature for recurring execution patterns
IF NOT matched AND "recurring_pattern" in signals:
    # Check if same procedure/mistake has occurred 2+ times
    # (check working memory recent_outcomes + journal for similar execution patterns)
    Bash: wm-read.sh active_context --json  # recent outcomes are part of active_context
    IF similar_execution_count >= 2:
        sig_id = next sig-NNN (check existing via pattern-signatures-read.sh --summary)
        echo '<JSON>' | bash core/scripts/pattern-signatures-add.sh
        # JSON: {
        #   id: sig_id, name: descriptive name,
        #   description: the recurring pattern,
        #   conditions: [conditions from execution context],
        #   expected_outcome: what typically happens,
        #   category: goal.category, status: "active",
        #   source: "execution-reflection",
        #   source_goals: [goal.id, prior similar goal IDs]
        # }
        Log: "NEW EXEC PATTERN: {name} — discovered from {goal.id}"
```

## Step 2: Contradiction Detection

Compare execution outcome against the target tree node's content. Fix contradictions
inline when possible, flag for investigation when not.

```
IF "surprise" in signals OR "mistake" in signals:
    # Load the tree node for this goal's category
    node=$(bash core/scripts/tree-find-node.sh --text "{goal.category}" --leaf-only --top 1)
    IF node found:
        Read node.file

        # Compare execution outcome against node's "Key Insights" section
        IF execution outcome CONTRADICTS a specific insight in the node:
            IF contradiction is simple (can be corrected in 1-2 sentences):
                # Fix inline — Edit the contradicted insight
                Edit node.file: replace or annotate the contradicted insight
                # last_update_trigger lives in .md front matter, last_updated in _tree.yaml
                Edit node.file front matter: last_update_trigger: "execution-contradiction"
                bash core/scripts/tree-update.sh --set <node.key> last_updated "$(date +%Y-%m-%dT%H:%M:%S)"
                Log: "EXEC CONTRADICTION FIXED: {node.key} — insight updated after {goal.id}"
            ELSE:
                # Too complex for inline fix — flag for Step 3 investigation goal
                contradiction_for_investigation = {
                    node_key: node.key,
                    old_insight: the contradicted text,
                    new_evidence: execution outcome,
                    reason: why it can't be fixed inline
                }

            # Log transition if fundamental (not minor refinement)
            IF contradiction is fundamental:
                Bash: world-cat.sh knowledge/transitions.yaml
                Append transition entry:
                    entity: node.key
                    from: "old insight summary"
                    to: "corrected insight summary"
                    evidence: "goal {goal.id} execution outcome"
                    status: "detected"
                    date: today

        ELIF execution outcome REFINES understanding (not contradiction):
            # Extract precision from refinement before compressing
            # See core/config/conventions/precision-encoding.md for extraction heuristics
            IF refinement contains exact values (numbers, thresholds, code refs, formulas):
                Append to node "## Verified Values" section (create if missing):
                  - **{label}**: `{value}` {unit} — {context}
            Edit node.file: append 1-2 sentence qualitative refinement to Key Insights
            # last_update_trigger lives in .md front matter, last_updated in _tree.yaml
            Edit node.file front matter: last_update_trigger: "execution-refinement"
            bash core/scripts/tree-update.sh --set <node.key> last_updated "$(date +%Y-%m-%dT%H:%M:%S)"
            Log: "EXEC REFINEMENT: {node.key} — insight refined after {goal.id}"
```

## Step 3: Investigation Goal Creation

When a finding can't be fully resolved now, create a goal for later follow-up
using the three cognitive primitives. This turns in-the-moment observations into
scheduled work.

```
goals_to_create = []

# 3a. Contradiction too deep to fix inline (from Step 2)
IF contradiction_for_investigation exists:
    goals_to_create.append({
        title: "Investigate: {node.key} contradicts {goal.id} execution outcome",
        priority: "MEDIUM",
        type: "investigate",
        description: "Tree node {node.key} states: '{old_insight}'\n\n"
                     "But goal {goal.id} execution showed: '{new_evidence}'\n\n"
                     "Needs deeper analysis to determine which is correct and why."
    })

# 3b. Recurring pattern needs root cause analysis
IF "recurring_pattern" in signals AND similar_execution_count >= 3:
    # Pattern has been seen 3+ times — warrants investigation beyond just a signature
    goals_to_create.append({
        title: "Investigate: why {pattern_description} keeps recurring",
        priority: "MEDIUM",
        type: "investigate",
        description: "Pattern seen {count} times across goals: {goal_ids}.\n\n"
                     "Root cause analysis needed — is this a systemic issue?"
    })

# 3c. Mistake reveals broader systemic issue
IF "mistake" in signals:
    # Check if the mistake class has occurred before (search journal + experience)
    IF same_mistake_class_count >= 2:
        goals_to_create.append({
            title: "Unblock: systemic {mistake_class} across {category}",
            priority: "HIGH",
            type: "unblock",
            description: "Same mistake class occurred {count} times.\n\n"
                         "Latest: {goal.id}. Previous: {prior_goal_ids}.\n\n"
                         "Systemic fix needed — guardrail alone is insufficient."
        })

# 3d. Execution reveals improvement opportunity
IF execution revealed a technique, shortcut, or optimization that could benefit
   other goals or categories:
    goals_to_create.append({
        title: "Idea: {improvement_description}",
        priority: "MEDIUM",
        type: "idea",
        description: "During {goal.id}, discovered: {improvement_details}.\n\n"
                     "Could benefit: {benefiting_categories_or_goals}."
    })

# ── Create goals with dedup guard ─────────────────────────────────
IF len(goals_to_create) > 0:
    # Dedup: scan goal titles to avoid duplicates (same pattern as Step 8.5)
    Bash: load-aspirations-compact.sh → IF path returned: Read it
    (compact data has IDs, titles, statuses — no descriptions/verification)
    active_titles = extract goal titles with status pending/in-progress from ALL aspirations
    # Also check completed siblings — a finished goal in the same aspiration may have already addressed this
    parent_asp = goal's parent aspiration
    sibling_completed_titles = extract goal titles with status completed from parent_asp ONLY
    dedup_titles = active_titles + sibling_completed_titles

    FOR EACH new_goal in goals_to_create:
        IF similar title already exists in dedup_titles:
            Output: "▸ Exec reflection: {new_goal.type} goal already exists — skipped"
            continue

        goal_json = {
            title: new_goal.title,
            status: "pending",
            priority: new_goal.priority,
            skill: null,
            participants: ["agent"],
            category: goal.category,
            description: new_goal.description + "\n\nDiscovered by: Step 8.75 Execution Reflection",
            verification: {
                outcomes: ["Investigation complete — finding resolved or documented with reasoning"],
                checks: []
            },
            discovered_by: goal.id,
            discovery_type: new_goal.type
        }
        target_asp = parent_asp  # Route to same aspiration as source goal
        echo '<goal_json>' | bash core/scripts/aspirations-add-goal.sh <target_asp>
        Output: "▸ Exec reflection: Created '{new_goal.title}' in {target_asp}"

    goals_created_count = number of goals actually created (not skipped)
```

## Step 4: Experience Archival

If any learning occurred in Steps 1-3, archive as an experience record.

```
IF matched OR new signature created OR contradiction handled OR goals_created_count > 0:
    experience_id = "exp-exec-{goal.id}"
    Write <agent>/experience/{experience_id}.md with:
        ---
        type: execution_reflection
        goal_id: {goal.id}
        category: {goal.category}
        signals: {signals}
        date: {today}
        ---
        # Execution Reflection: {goal.title}

        ## Signals
        {list of signals detected and why}

        ## Pattern Signature
        {matched/created/none — details}

        ## Contradiction Detection
        {found/none — details, node affected}

        ## Goals Created
        {list of investigation/unblock/idea goals, or "none"}

        ## Key Takeaway
        {one-liner summary of what was learned}

    echo '<experience-json>' | bash core/scripts/experience-add.sh
    # JSON: {
    #   id: experience_id, type: "execution_reflection",
    #   created: today, category: goal.category,
    #   summary: "Exec reflection on {goal.title}: {signals} → {outcomes}",
    #   goal_id: goal.id,
    #   tree_nodes_related: [node.key if any],
    #   content_path: "<agent>/experience/{experience_id}.md"
    # }
```

## Step 5: Journal Entry

Append execution reflection summary to the session journal.

```
Append to <agent>/journal/YYYY/MM/YYYY-MM-DD.md:

    ## {timestamp} — Execution Reflection: {goal.title}
    Signals: {signals joined by ", "}
    Pattern: {matched sig-id / created sig-id / none}
    Contradiction: {fixed node.key / flagged for investigation / none}
    Goals created: {count} ({goal titles})
    Experience: {experience_id or "none — no learning occurred"}

Update journal index via scripts (same pattern as state-update Step 7):
    IF session entry exists: pipe update JSON to `bash core/scripts/journal-merge.sh <session-num>`
    IF session entry does not exist: pipe new entry JSON to `bash core/scripts/journal-add.sh`
```

## Mode: Batch Micro Reflection (--batch-micro)

This sub-skill implements Mode 1b of `/reflect`. It is invoked by the parent `/reflect` router when `--batch-micro` is specified, or during `--full-cycle` as the first step. It processes the entire micro_hypotheses array from working memory as a single batch — never creates individual pipeline records. Called during session-end consolidation.

## Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Step 1: Load Micro-Hypotheses

```
Bash: wm-read.sh micro_hypotheses --json
If slot is empty or null: return { micro_reflected: 0 } and exit
```

## Step 2: Compute Batch Statistics

```
total = count of micro-hypotheses
confirmed = count where outcome == "confirmed"
corrected = count where outcome == "corrected"
unresolved = count where outcome == null
accuracy_pct = confirmed / (confirmed + corrected) if (confirmed + corrected) > 0 else null

# Category breakdown
by_category:
  For each unique category in micro-hypotheses:
    {category}: {total, confirmed, corrected, accuracy_pct}

# Calibration check
overconfident_misses = count where confidence >= 0.80 AND outcome == "corrected"
underconfident_hits = count where confidence <= 0.40 AND outcome == "confirmed"
```

## Step 3: Identify Surprises for Promotion

```
surprises = []
For each micro-hypothesis:
  # Calculate surprise: high confidence + wrong = high surprise
  if outcome == "corrected":
    surprise = round(confidence * 10)  # 0.90 confidence wrong → surprise 9
  elif outcome == "confirmed":
    surprise = round((1 - confidence) * 10)  # 0.20 confidence right → surprise 8
  else:
    surprise = 0

  Write surprise back to the micro-hypothesis entry

  # Promotion check (from core/config/memory-pipeline.yaml micro_hypothesis_consolidation)
  if surprise >= 7:
    Add to surprises list → promote to encoding gate
  elif confidence >= 0.90 AND outcome == "corrected":
    Add to surprises list → promote as violation
  elif confidence <= 0.30 AND outcome == "confirmed":
    Add to surprises list → promote as underconfidence

For each promoted micro-hypothesis:
  echo '<json>' | wm-append.sh encoding_queue  # item fields:
    observation: "MICRO: {claim} — predicted {confidence*100}% → {outcome}"
    encoding_score: 0.50 + (surprise / 20)  # surprise boosts encoding priority
    target_article: {best matching leaf node for this category, or null}
    priority_class: "micro_surprise"
    source_horizon: "micro"
    timestamp: now
```

## Step 4: Update Aggregate Stats

```
# Append batch stats to pipeline metadata for accuracy reporting
Bash: pipeline-read.sh --meta  → get current micro_hypothesis_stats
Update micro_hypothesis_stats:
  total_all_time: += total
  confirmed_all_time: += confirmed
  corrected_all_time: += corrected
  accuracy_all_time: confirmed_all_time / (confirmed_all_time + corrected_all_time)
  sessions_with_micros: += 1
  last_session_stats:
    date: today
    total: {total}
    confirmed: {confirmed}
    accuracy_pct: {accuracy_pct}
    promoted_to_encoding: {count of surprises}
    by_category: {category breakdown}
Bash: pipeline-meta-update.sh micro_hypothesis_stats '<JSON>'

# Count toward developmental stage resolved_hypotheses total
Read <agent>/developmental-stage.yaml
Update: resolved_hypotheses += (confirmed + corrected)  # only resolved micros count
Write <agent>/developmental-stage.yaml
```

## Step 5: Journal Entry

```
Append to <agent>/journal/YYYY/MM/YYYY-MM-DD.md:

## Micro-Hypothesis Batch — Session {N}
Total: {total} | Confirmed: {confirmed}/{confirmed+corrected} ({accuracy_pct}%)
Promoted to encoding: {promoted_count}
Categories: {list of categories with counts}
Notable surprises:
{For each promoted surprise: "- {claim} (confidence {confidence}) → {outcome}"}

If overconfident_misses > 0:
  "Self-model note: {overconfident_misses} high-confidence micro-predictions were wrong"
If underconfident_hits > 0:
  "Self-model note: {underconfident_hits} low-confidence micro-predictions were right"
```

## Step 6: Actionable Work Check

After computing batch stats, assess whether the patterns suggest tracked work:

```
actionable_discoveries = []

IF promoted_to_encoding >= 3 AND 3+ promotions share a single category:
    # Concentrated surprises suggest a systematic knowledge gap
    actionable_discoveries.append({
        category: that_category,
        insight: "Systematic surprises in {category} — {N} high-surprise predictions wrong",
        suggested_work: "Research {category} domain deeper or review assumptions",
        priority: "MEDIUM"
    })

IF overconfident_misses >= 2 in a single category:
    # Repeated high-confidence failures suggest wrong mental model
    actionable_discoveries.append({
        category: that_category,
        insight: "Overconfident failures in {category} — mental model may be wrong",
        suggested_work: "Investigate {category} assumptions and update knowledge",
        priority: "HIGH"
    })

IF any promoted micro-hypothesis directly implies a fix, investigation, or research need:
    # Specific actionable item discoverable from the surprise content
    actionable_discoveries.append({
        category: relevant_category,
        insight: "{what the surprise reveals}",
        suggested_work: "{specific action needed}",
        priority: "MEDIUM"
    })
```

## Step 7: Return Batch Result

```yaml
batch_micro_result:
  total: N
  confirmed: N
  corrected: N
  accuracy_pct: N.N
  promoted_to_encoding: N
  by_category: {category: {total, confirmed, accuracy}}
  self_model_insights:
    - "Overconfident about {category}: {N} high-confidence misses"
    - "Underconfident about {category}: {N} low-confidence hits"
  actionable_discoveries: [...]  # from Step 6, empty list if none
```

---

## Chaining Map (All Modes)

| Direction | Skill/Script | Modes | How |
|-----------|-------------|-------|-----|
| Called by | `/reflect --on-hypothesis` | Hypothesis | Mode routing from parent |
| Called by | `/reflect --on-execution` | Execution | Mode routing from parent |
| Called by | `/reflect --batch-micro` | Batch Micro | Mode routing from parent |
| Called by | `/aspirations-state-update` | Execution | Step 8.75 after productive goals |
| Calls | `/reflect-tree-update` | Hypothesis | Propagate tree changes upward |
| Calls | `pattern-signatures-*.sh` | Hypothesis, Execution | Pattern signature operations |
| Calls | `experience-add.sh` | Hypothesis, Execution | Archive reflections |
| Calls | `reasoning-bank-add.sh` | Hypothesis | Strategy extraction (confirmed) |
| Calls | `guardrails-add.sh` | Hypothesis | Preventive guardrails (corrected) |
| Calls | `aspirations-add-goal.sh` | Execution | Investigation/unblock/idea goals |
| Calls | `pipeline-meta-update.sh` | Batch Micro | Aggregate stats |
| Updates | Tree nodes, beliefs, transitions | Hypothesis | Knowledge encoding |
| Updates | `<agent>/journal/` | All | Reflection entries |
