---
name: reflect-batch-micro
description: "Batch micro-hypothesis reflection — batch stats, surprise promotion, aggregate stats, journal, actionable work check"
user-invocable: false
parent-skill: reflect
triggers:
  - "/reflect-batch-micro"
  - "/reflect --batch-micro"
conventions: [pipeline, handoff-working-memory]
---

# /reflect-batch-micro — Batch Micro-Hypothesis Reflection

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
Read mind/developmental-stage.yaml
Update: resolved_hypotheses += (confirmed + corrected)  # only resolved micros count
Write mind/developmental-stage.yaml
```

## Step 5: Journal Entry

```
Append to mind/journal/YYYY/MM/YYYY-MM-DD.md:

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
