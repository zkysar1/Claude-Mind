---
name: check-production-health
forged: true
forged_by: bravo
forged_date: "2026-07-15"
forged_from: gap-001
description: "Runs a one-shot read-only production health sweep across the AyoAI cloud stack — Lambda runtime errors, CloudWatch error/throttle aggregates, DynamoDB throttling, EC2 fleet status, and infra-health component streaks — returning a per-leg PASS/WARN/FAIL JSON rollup. Use whenever the agent needs a production health deep-dive, a production health sweep, a deep production check, asks 'is production healthy', a production-monitoring or health-audit goal fires, or a deployment/incident calls for verifying cloud-stack health beyond a single service. MUST use the companion script world/scripts/production-health-sweep.sh — never a hand-rolled sequence of raw aws CLI calls."
user-invocable: false
triggers: [production-health, health-sweep, production health deep-dive, deep production check, cloudwatch errors, dynamodb throttling, ec2 fleet status, lambda errors, production monitoring]
tools_used: [Bash]
companion_scripts: [world/scripts/production-health-sweep.sh, world/scripts/lambda-health-check.sh, world/scripts/aws-exec.sh]
conventions: [secrets, infrastructure]
minimum_mode: assistant
revision_id: "skill-forge-check-production-health-b7f31a"
previous_revision_id: null
---

# /check-production-health — One-Shot Production Health Sweep

Aggregated read-only health battery over the AyoAI cloud stack. Replaces the
8+ manual AWS CLI calls (gap-001 pattern, encountered in g-001-83, g-222-01,
g-001-91, and the g-335-09 monitoring lane) with one companion-script call
plus interpretation.

## Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from
the `conventions:` front matter. Read only the paths returned. If output is
empty, all conventions already loaded — proceed.

## Restricted Operations

MUST use `world/scripts/production-health-sweep.sh` — never a hand-rolled
sequence of raw `aws` CLI calls. The companion script:

- is a FIXED battery of describe/get/list verbs only (no mutating AWS call
  can be reached through it),
- resolves credentials via `world/scripts/aws-exec.sh` (env-read.sh chain —
  no hardcoded secrets, values never echoed),
- defaults to `--region us-east-2` (the AyoAI production region).

## Procedure

```
# 1. Run the sweep (read-only; ~30-60s; window defaults to 24h)
Bash: source core/scripts/_paths.sh && PROJECT_ROOT="$PROJECT_ROOT" \
      bash "$WORLD_PATH/scripts/production-health-sweep.sh" [--since-minutes N] [--region R]

# 2. Parse the JSON rollup: {overall: PASS|WARN|FAIL, legs: {...}}
#    Exit 0 = sweep COMPLETED (verdicts live in the JSON, not the exit code).
#    Exit 1 = the sweep itself could not run (missing wrapper/credentials) —
#    that is an infrastructure problem, NOT a health verdict.

# 3. Interpret each leg:
#    - lambda_runtime_errors.broken non-empty  -> production Lambda(s) erroring
#    - cloudwatch_lambda_aggregate errors_sum>0 -> FAIL; throttles_sum>0 -> WARN
#    - dynamodb.throttled_requests_sum > 0      -> capacity pressure (WARN)
#    - ec2_fleet.impaired non-empty             -> instance/system status failed
#    - infra_health_components.failing_streaks  -> INTERPRET BOX-LOCALLY:
#      components unreachable from THIS box by design (e.g. GUI bridges,
#      gh-gated CI, GPU hosts that live on another machine) show permanent
#      streaks that are box-locality, not production failure. Cross-check
#      the component list against the agent's box capabilities before
#      treating a streak as a production signal.

# 4. Escalation contract:
#    - Any FAIL leg -> file an Investigate goal (aspirations-add-goal.sh)
#      quoting the failing leg JSON as evidence; HIGH if customer-facing.
#    - WARN trend (same leg WARN across 2+ sweeps) -> findings-board post.
#    - overall PASS -> no goal; log the one-line rollup where the calling
#      goal's close summary needs it.
```

## Input/Output Contract

- Input: optional `--since-minutes N` (default 1440) and `--region R`
  (default us-east-2).
- Output: single JSON object on stdout — `sweep`, `at`, `window_minutes`,
  `region`, `overall`, `legs{lambda_runtime_errors, cloudwatch_lambda_aggregate,
  dynamodb, ec2_fleet, infra_health_components}`.
- The caller (usually a production-monitoring goal in the aspirations loop)
  owns goal-filing and board posts; this skill produces the evidence.

## Error Handling

- Individual leg failures degrade to `{}` inside the script and surface as
  SKIP/PASS-with-zero-data — the sweep never aborts mid-battery.
- `aws-exec.sh missing` on exit 1: the world mirror is stale on this box —
  probe the authoritative store before concluding the script is gone
  (guard-1163 class).
- Credential absence: `env-read.sh has AWS_ACCESS_KEY_ID` discriminates
  missing-creds from broken-wrapper before filing any blocker
  (probe-with-canonical-code-path).

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is the sweep Bash call (or the follow-up
`aspirations-add-goal.sh` when a FAIL leg files an Investigate).
Never end with a text summary.
