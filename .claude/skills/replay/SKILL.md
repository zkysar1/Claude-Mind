---
name: replay
description: "Hippocampal replay — compressed, selective review of resolved hypotheses for reconsolidation and domain transfer"
user-invocable: false
triggers:
  - "/replay"
parameters:
  - name: mode
    description: "--sharp-wave (compressed review), --reverse (recent first), --selective (encoding queue only), --category <cat>, --domain-transfer"
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
conventions: [pipeline, experience, tree-retrieval, reasoning-guardrails, pattern-signatures, handoff-working-memory]
minimum_mode: autonomous
---

# /replay — Hippocampal Replay Engine

Compressed, selective review of resolved hypotheses. Inspired by hippocampal sharp-wave ripples that replay experiences at 20x speed during rest, selectively prioritizing novel, goal-relevant, and high-stakes outcomes.

Based on: Hippocampal sharp-wave ripples (Buzsaki 2015), systems consolidation theory, memory reconsolidation (Nader et al. 2000).

## Quick Links

| Related Skill | Relationship |
|---------------|-------------|
| [/reflect](../reflect/SKILL.md) | Parent — calls /replay during `--full-cycle` |
| [/reflect-on-outcome](../reflect-on-outcome/SKILL.md) | Hypothesis + execution reflection feeds replay candidates |
| [/reflect-on-self](../reflect-on-self/SKILL.md) | Pattern extraction mines replayed hypotheses |
| [/aspirations-consolidate](../aspirations-consolidate/SKILL.md) | Calls /replay during session-end consolidation |

## Parameters

- `--sharp-wave` — Run compressed replay of last N resolved hypotheses (default: 10)
- `--reverse` — Replay in reverse chronological order (recent first)
- `--selective` — Only replay tagged items from working memory encoding_queue (via `wm-read.sh encoding_queue --json`)
- `--category <cat>` — Replay only hypotheses from a specific category
- `--domain-transfer` — Cross-domain replay: find patterns in strong domain applicable to weak domains

Default (no args): equivalent to `--sharp-wave --reverse`

## Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Step 1: Select Replay Candidates

```
Bash: pipeline-read.sh --replay-candidates → resolved hypotheses eligible for replay
Bash: wm-read.sh encoding_queue --json  (if --selective mode)
Read core/config/memory-pipeline.yaml → replay_priority_order, max_replay_items

Priority selection (most learning signal first):
1. Violations: hypotheses where outcome contradicted expectation (surprise >= 5)
2. High-impact outcomes: hypotheses with surprise level >= 7 or significant consequences
3. Pattern signature mismatches: hypotheses where a pattern was matched but outcome differed
4. EXPLORE/CALIBRATE categories: hypotheses in categories where we're still learning
5. Random sample: 2-3 routine hypotheses (prevents overfitting to extremes)

Apply spaced repetition filter:
  For each candidate, check replay_metadata.last_replayed
  Skip if replayed within last 7 days
  Prefer hypotheses never replayed (replay_count == 0)

Select top N candidates (N = max_replay_items from config, default 10)

   # Add experience-backed candidates
   IF <agent>/experience.jsonl exists:
       Bash: experience-read.sh --type goal_execution
       Bash: experience-read.sh --type hypothesis_formation
       Include experiences with high retrieval_count as additional replay candidates
       Experience candidates complement pipeline-based candidates — they provide
       full-fidelity traces that pipeline summaries may have compressed away
```

## Step 1.5: Load Current Strategy State

Before replaying, load current knowledge to compare against replay memories.

```
Collect unique categories from replay candidates
For each unique category:
  Bash: retrieve.sh --category {hypothesis.category} --depth medium
  # Returns unified JSON with all data stores. Retrieval counters already incremented.

  Cache result — reuse for all hypotheses in same category
```

Use retrieved context to:
- Compare replay memories against CURRENT strategy state (detect drift)
- During reconsolidation (Step 4): know what to reinforce vs. revise
- During domain transfer (Step 5): know source domain strategy to abstract

## Step 2: Compressed Replay (20x Compression)

For each selected hypothesis (max 10 per session):

```
Read the full resolved pipeline record
Read the original evaluation record (scoring, reasoning)

Generate 3-line compressed summary:
  CONDITION: {conditions when hypothesized — category, key signals, data recency, context}
  ACTION:    {what we hypothesized, confidence, strategy used, pattern matched}
  OUTCOME:   {actual result, confirmed/corrected, surprise level, key lesson}

Example:
  CONDITION: Category A, strong signal alignment, fresh data (2min old), 3 confirming indicators
  ACTION:    Hypothesized YES at 0.72 confidence via signal-freshness strategy (sig-001 matched)
  OUTCOME:   CONFIRMED — signals held. Lesson: fresh data + strong alignment = high accuracy

Example (violation):
  CONDITION: Category A, 6 consecutive signals in same direction, data 12min old
  ACTION:    Hypothesized continuation at 0.55 via trend-following (sig-001 matched — WRONG MATCH)
  OUTCOME:   CORRECTED — reversal occurred. DG separation should have triggered sig-002.
             Lesson: extended streaks signal exhaustion, NOT continuation. Stale data compounded error.

   # Dereference experience content for full-fidelity replay
   For each replay candidate that has an experience_ref (pipeline record) or is itself an experience:
       Bash: experience-read.sh --id {experience_id}
       Read the content .md file at content_path
       Use verbatim_anchors for precise CONDITION/ACTION/OUTCOME replay:
       - Anchors provide exact text rather than compressed summaries
       - This enables more accurate cross-hypothesis pattern mining (Step 3)
```

## Step 3: Cross-Hypothesis Pattern Mining

After individual replays, analyze the batch as a whole:

```
1. SHARED CONDITIONS in corrected hypotheses:
   Group all corrected hypotheses
   Extract common antecedents (conditions, strategy used, timing)
   Flag: "N of M corrected hypotheses shared condition X"

2. STRATEGY PERFORMANCE by pattern signature:
   For each pattern signature matched in this batch:
     Calculate: matches attempted, matches confirmed, accuracy
   Flag any signature where accuracy diverges > 10pp from its historical average

3. TEMPORAL PATTERNS:
   Check: does batch position correlate with accuracy?
   Check: does time-of-day correlate with accuracy?
   Check: does session fatigue (hypotheses late in session) affect accuracy?

4. CATEGORY CROSS-REFERENCE:
   Group replayed hypotheses by category
   Compare accuracy across categories
   Flag categories performing significantly above or below overall
```

## Step 3.5: Convention Pattern Mining

After cross-hypothesis pattern mining, check if shared conditions in corrected
hypotheses map to missing procedural execution steps (convention candidates).

```
# Prerequisite: only runs if Step 3 found shared conditions in corrected hypotheses
IF no shared_condition groups from Step 3 with N >= 2 corrected hypotheses:
    SKIP convention pattern mining

FOR EACH shared_condition group where N >= 2 corrected hypotheses:
    # Does this shared condition map to a missing execution step?
    # Scan OUTCOME fields for procedural gap indicators
    lesson_texts = [h.outcome_lesson for h in shared_condition.hypotheses]

    procedural_gap_indicators = [
        "should have checked", "forgot to", "didn't verify",
        "missed the step", "would have caught", "if we had run",
        "always need to", "next time must", "should always"
    ]

    is_procedural_gap = any(
        any(indicator in lesson.lower() for indicator in procedural_gap_indicators)
        for lesson in lesson_texts
    )

    IF NOT is_procedural_gap:
        CONTINUE  # Not a convention candidate

    # Phase classification
    IF shared_condition relates to setup/prerequisites before execution:
        target = "pre-execution"
    ELIF shared_condition relates to cleanup/verification after execution:
        target = "post-execution"
    ELSE:
        CONTINUE  # Ambiguous — skip

    # Check for existing proposals to reinforce
    Bash: source core/scripts/_paths.sh
    IF file_exists($WORLD_DIR/conventions/convention-changes.jsonl):
        Read convention-changes.jsonl
        similar_proposal = find entry where target matches AND proposed_step is semantically similar
        IF similar_proposal exists AND similar_proposal.status == "pending":
            # Reinforce existing proposal
            Update similar_proposal: reinforcement_count += 1, confidence += 0.15
            Log: "REPLAY CONVENTION: reinforced proposal for {target} — '{similar_proposal.proposed_step.title}' now confidence {new_confidence}, reinforcements {new_count}"
            CONTINUE

    # New proposal from cross-hypothesis pattern
    proposed_step = {
        title: synthesize concise title from shared_condition,
        condition: "IF {shared_condition.common_antecedent}:",
        action: synthesize procedural step from lesson_texts
    }

    hypothesis_ids = [h.id for h in shared_condition.hypotheses]
    echo '{"date":"<today>","type":"add","target":"{target}","proposed_step":<proposed_step JSON>,"source":"replay-pattern-mining","source_hypothesis":"{hypothesis_ids[0]}","source_guardrails":[],"reinforcement_count":1,"confidence":0.5,"status":"pending"}' >> $WORLD_DIR/conventions/convention-changes.jsonl

    Log: "REPLAY CONVENTION: proposed new {target} step from {N} corrected hypotheses sharing condition '{shared_condition.description}'"

# Pass any convention proposals to Step 4 for reconsolidation context
```

## Step 4: Reconsolidation Window

When a strategy is recalled during replay, it enters a reconsolidation window. The strategy becomes temporarily "labile" — updatable based on new evidence.

```
For each strategy referenced during replay:
  1. Read the strategy's current state from node articles at any tree depth
  2. Tally replay evidence:
     reinforcing_count = hypotheses where strategy worked as expected
     contradicting_count = hypotheses where strategy failed unexpectedly
     extending_count = hypotheses that reveal new conditions for the strategy

  3. Reconsolidation decision:
     If reinforcing_count > contradicting_count * 2:
       REINFORCE — increase strategy confidence by 0.02
       Log: "RECONSOLIDATION: {strategy} reinforced ({reinforcing}/{total})"

     If contradicting_count >= reinforcing_count:
       FLAG FOR REVISION — the strategy may need updating
       Log: "RECONSOLIDATION: {strategy} FLAGGED — contradictions ({contradicting}/{total})"
       Write revision note to the affected node article

     If extending_count > 0:
       EXTEND — add new conditions or rules to the strategy
       Log: "RECONSOLIDATION: {strategy} extended — new conditions discovered"
       Append new conditions to the strategy article

  4. Update pattern signatures:
     For each affected signature, record outcome via script:
       bash core/scripts/pattern-signatures-record-outcome.sh <sig-id> CONFIRMED|CORRECTED
     Read current signatures if needed: bash core/scripts/pattern-signatures-read.sh --active

  5. Source node freshness check:
     For each strategy's source tree node:
       IF node.last_updated is older than the strategy's most recent outcome_date:
           Log: "STALE SOURCE: {node_key} last updated {date}, strategy has
                  newer evidence from {outcome_date}"
           echo '{"node_key":"<key>","reason":"<reason>","source":"replay-staleness"}' | wm-append.sh knowledge_debt

   # Update experience retrieval stats for replayed experiences
   For each experience record consulted during replay:
       bash core/scripts/experience-update-field.sh {exp-id} retrieval_stats.retrieval_count {n+1}
       bash core/scripts/experience-update-field.sh {exp-id} retrieval_stats.last_retrieved "{today}"
       IF experience content contributed to strategy reinforcement or revision:
           bash core/scripts/experience-update-field.sh {exp-id} retrieval_stats.times_useful {n+1}
       ELSE:
           bash core/scripts/experience-update-field.sh {exp-id} retrieval_stats.times_noise {n+1}
```

## Step 5: Domain Transfer Check (--domain-transfer mode)

Find patterns in the strongest domain that could bootstrap weaker domains.

```
leaves_json=$(bash core/scripts/tree-read.sh --leaves)
# Each entry has key, depth, capability_level — extract domain-level capability info
Read <agent>/developmental-stage.yaml → exploration budget allocation

strongest = leaf with highest capability_level (strong domain, EXPLOIT or MASTER level)
weakest = leaf with lowest capability_level (weak domain, EXPLORE or CALIBRATE level)

For each validated pattern/strategy in strongest domain:
  Extract core principle (abstract from domain-specific details):
    "data-freshness signal" → Core: "Fresh data + context detection = high accuracy"
    "dual-filter system" → Core: "Gate hypotheses on context; skip unfavorable conditions"
    "streak exhaustion" → Core: "Long streaks reverse; skip after extended consecutive signals"

  For each weaker domain, ask:
    "Could this abstract principle apply to {weak domain}?"

    The transfer process:
      strong domain "data freshness" → weak domain "equivalent recency signal"
      strong domain "regime/context detection" → weak domain "phase/state detection"
      strong domain "exhaustion detection" → weak domain "mean reversion signals"

  If plausible transfer:
    echo '<transfer-json>' | wm-set.sh cross_domain_transfer
    Log spark for aspirations: "Test {pattern} transfer to {domain}" (aspirations gap analysis picks this up)
    Log: "SCAFFOLDING: {strong domain} → {weak domain}: {hypothesis}"
```

## Step 6: Replay Report

Write structured output and append to journal:

```
## Hippocampal Replay — {date}

### Configuration
Mode: {mode} | Candidates screened: {N} | Replayed: {N}

### Compressed Replays
| # | Hypothesis | Condition | Strategy | Result | Insight |
|---|-----------|-----------|----------|--------|---------|
| 1 | {id} | {3-word condition} | {strategy} | {outcome} | {lesson} |

### Cross-Hypothesis Patterns
- {pattern description, if any found}

### Reconsolidation Updates
- {strategy}: {reinforced/flagged/extended} — {details}

### Domain Transfers Identified
- {from} → {to}: {hypothesis}

### Spaced Repetition Stats
Hypotheses never replayed: {N remaining}
Next replay due: {date based on 7-day interval}
```

## Chaining Map

| Direction | Skill | How |
|-----------|-------|-----|
| Called by | `/reflect --full-cycle` | After pattern extraction (Step 2.5) |
| Called by | `/aspirations loop` | During session-end consolidation pass |
| Calls | `/research-topic` | When domain transfer generates research question |
| Updates | Pattern signatures via `pattern-signatures-record-outcome.sh` | Outcome stats, new separation markers |
| Updates | Knowledge tree node articles | Reconsolidation updates |
| Updates | Working memory (via `wm-set.sh`) | Cross-domain transfer slot, pattern cache |
