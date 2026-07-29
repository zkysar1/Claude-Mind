---
name: operator-health-trend
forged: true
forged_by: bravo
forged_date: "2026-04-10"
description: Computes the multi-day operator health trend — task success rate, error counts, and heartbeat continuity over a trailing window — and reports direction (improving / flat / degrading). Use whenever the agent needs the operator service's health TREND rather than a point-in-time snapshot — e.g. "show operator health trend", "operator daily trend", "task success trend", weekly production reviews, or when a monitoring goal asks whether operator health is drifting. Fires when trend direction (not current status) is the question; for point-in-time deep sweeps use check-production-health instead.
user-invocable: false
minimum_mode: assistant
companion_scripts:
  - world/scripts/operator-api.sh
  - world/scripts/aws-exec.sh
---

# /operator-health-trend — Operator Health Trend Analysis

<!-- Re-materialized 2026-07-16 (g-115-2293) from the world/forged-skills.yaml
     registry entry after the original SKILL.md (forged 2026-04-10 by bravo)
     was stranded on bravo's prior box — the confirmed
     2026-07-15_forged-skill-box-local-gaps instance. Reconstructed from the
     registry contract (triggers + companion_scripts + type=analysis), not a
     byte-restore of the original. -->

Answers "which way is operator health MOVING?" over a trailing window
(default 7 days). Complements `check-production-health` (point-in-time deep
sweep): this skill compares across days; that skill inspects one moment.

## Step 1: Collect the trailing window

```
# Operator-side metrics (authoritative for task outcomes). operator-api.sh
# sources credentials + adds the AYOAI-API-KEY header (canonical transport —
# do NOT raw-curl, probe-with-canonical-code-path.md).
Bash: source core/scripts/_paths.sh && bash "$WORLD_DIR/scripts/operator-api.sh" GET /metrics/daily?days=7
# If the endpoint shape differs on this deployment, list endpoints first:
#   bash "$WORLD_DIR/scripts/operator-api.sh" GET /
# and adapt: the goal is per-day task success counts for the window.

# CloudWatch corroboration (2nd signal — verify-before-assuming rule 1 for
# any "degrading" conclusion): TaskSuccessRate / error-count metrics.
Bash: bash "$WORLD_DIR/scripts/aws-exec.sh" cloudwatch get-metric-statistics \
      --namespace <operator-namespace> --metric-name TaskSuccessRate \
      --start-time <now-7d> --end-time <now> --period 86400 \
      --statistics Average --output json
```

## Step 2: Compute the trend

```
For each day in the window: success_rate, error_count, heartbeat gaps.
direction = compare first-half mean vs second-half mean:
  - improving:  second-half success mean > first-half + 2pp
  - degrading:  second-half success mean < first-half - 2pp
  - flat:       within ±2pp
Note single-day spikes separately (a one-day dip is an incident, not a trend).
```

## Step 3: Report

```
Emit a compact trend table (day | success% | errors | notes) + one-line
verdict: "Operator health {improving|flat|degrading} over {window}d
({first-half}% → {second-half}%)".

IF direction == degrading:
    Post a finding to the board (channel findings, tags: operator-health,
    trend) so the monitoring lane and partner agents see it, and consider
    filing an Investigate goal per the Cognitive Primitives if no existing
    goal covers the degradation.
ELSE:
    Report inline only (no board post for flat/improving — signal, not noise).
```

## Return Protocol

See `.claude/rules/return-protocol.md` — this is a sub-skill: terminate with a
Bash tool call handing control back to the caller (e.g. `echo "trend analysis
complete — return to orchestrator"`), never with trailing prose.
