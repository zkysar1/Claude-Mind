---
name: reflect-calibration
description: "Confidence calibration — bin by confidence level, calculate accuracy, self-consistency check, update calibration data"
user-invocable: false
parent-skill: reflect
triggers:
  - "/reflect-calibration"
  - "/reflect --calibration-check"
conventions: [pipeline, pattern-signatures]
---

# /reflect-calibration — Calibration Check

This sub-skill implements Mode 3 of `/reflect`. It is invoked by the parent `/reflect` router when `--calibration-check` is specified, or during `--full-cycle` when 10+ resolved hypotheses exist. It analyzes confidence calibration across all hypotheses, bins them by confidence level, computes actual accuracy per bin, recommends self-consistency checks, and updates calibration data.

## Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Step 1: Bin Hypotheses by Confidence Level

```
Group all resolved hypotheses into confidence bins:
  50-59%: [hypotheses]
  60-69%: [hypotheses]
  70-79%: [hypotheses]
  80-89%: [hypotheses]
  90-100%: [hypotheses]
```

## Step 2: Calculate Actual Accuracy Per Bin

```
For each bin:
  expected_accuracy = midpoint of bin (e.g., 75% for 70-79%)
  actual_accuracy = confirmed / total in bin
  calibration_error = abs(expected - actual)
```

## Step 3: Multi-Sample Self-Consistency Check

For future hypotheses, recommend using self-consistency:
```
1. Generate 3-5 independent assessments of the same question
2. Measure agreement level
3. High agreement (4/5 or 5/5) = high confidence
4. Moderate agreement (3/5) = moderate confidence
5. Low agreement (2/5 or less) = low confidence or skip
```

## Step 4: Update Calibration Data

Write calibration report to journal and update:
- Aspirations meta confidence_calibration_bias via Bash: `aspirations-meta-update.sh confidence_calibration_bias <value>` (read via `aspirations-read.sh --meta`)
- `mind/knowledge/meta/_index.yaml` category-level calibration data
