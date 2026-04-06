# Multi-Agent Coordination Convention

## Overview

Multiple agents share a world goal queue and communicate via the message board.
Coordination follows the CAID pattern (Centralized Asynchronous Isolated Delegation):
structured protocols (typed messages + tags), claim-based isolation (prevent duplicate
work), dependency graphs (`blocked_by`), and self-verification (review gate).

Based on ["Effective Strategies for Asynchronous Software Engineering Agents"](https://arxiv.org/html/2603.21489v1)
(Geng & Neubig, CMU) and ["Language Model Teams as Distributed Systems"](https://arxiv.org/abs/2603.12229).

## Agent Roles

Defined in `world/program.md`. Each agent has a distinct role:
- Roles are complementary, not competing — avoid overlapping work
- `participants` field on goals routes work to the right agent
- `claimed_by` field prevents duplicate execution at runtime

## Claim Protocol

World goals MUST be claimed before execution. See `aspirations.md` for full details.

```
claim (before Phase 4) → execute → complete-by (Phase 5.3, on success)
                                  → release (on failure/revert)
```

Scripts: `aspirations-claim.sh`, `aspirations-release.sh`, `aspirations-complete-by.sh`

**Key rules:**
- On claim conflict (exit non-zero): re-enter the selection loop, do not retry
- On infrastructure failure: release claim so other agent can attempt
- Recurring goals: `complete-by` auto-clears claim for next cycle
- Session end: release all held claims in handoff

## Board Communication

Use typed messages for all board posts. See `board.md` for the full type table and tag taxonomy.

**Actionable types** (scan during idle/boot):
- `escalation` — goal stuck after repeated failures, needs help
- `review-request` — code change needs peer review
- `handoff` — goal done, follow-up needed by other agent
- `blocker-alert` — shared resource blocked, affects multiple goals
- `directive` — strategic direction or priority change

**Noise types** (filter out when scanning for work):
- `status`, `claim`, `release`, `complete`

### Board Scan Protocol (Boot + Idle)

At session start (boot/prime) and during idle time (all-blocked path), scan for
actionable messages from other agents:

```bash
# Escalations — goals the other agent couldn't finish
board-read.sh --channel coordination --type escalation --since 12h --json

# Review requests — code changes awaiting peer review
board-read.sh --channel coordination --type review-request --since 12h --json

# Handoffs — completed work needing follow-up
board-read.sh --channel coordination --type handoff --since 12h --json

# Blocker alerts — shared resources down
board-read.sh --channel coordination --type blocker-alert --since 12h --json
```

For each actionable item: create an investigation or follow-up goal if one doesn't
already exist. Dedup against existing goals by title similarity.

## Circuit Breaker (Escalation)

After 3 consecutive failures on the same goal within a session:

1. Post `escalation` + `urgent` to coordination channel with goal ID
2. Defer the goal: `defer_reason = "Circuit breaker: 3+ consecutive failures, escalated via board"`
3. Other agent picks up during board scan, creates investigation goal
4. Original goal undefers when investigation resolves or `defer_reason` is manually cleared

The circuit breaker operates across goal-selection iterations (not within a single
execution — that's the episode chain protocol in `aspirations-execute`).

## Review Gate (Async Code Review)

After completing a world goal that involves code changes:

1. Executing agent posts `review-request` to coordination channel with goal ID
2. Executing agent sets `review_requested` timestamp on the goal
3. Reviewing agent scans for `review-request` during idle time
4. Reviewing agent reads the experience trace and checks for issues
5. Posts result: `complete` (review passed) or creates investigation goal (issues found)

Review is **asynchronous and non-blocking**. Goals are NOT held pending review.
The reviewing agent picks up reviews during idle time, catching bugs faster than
purely retroactive review without creating bottlenecks.

### Deep Review Protocol (Hypothesis-Driven Review)

Surface-level pass/fail reviews waste a learning opportunity. The deep review protocol
replaces the basic "check for issues" step with a 5-phase hypothesis-driven process
that produces testable predictions, detects downstream risks, and feeds into the
normal pipeline resolution cycle.

**Phases:**

1. **Context Loading (R1)**: Load the full experience trace via `experience-read.sh --goal {goal_id}`,
   read the content `.md` file referenced in the trace, and load the goal description with its
   verification outcomes. The reviewer must have the same evidence the executor had.

2. **Architectural Assessment (R2)** — answer three questions:
   - **Q1: Verification match** — Do ALL verification outcomes match the claimed result?
     Compare each verification check against the experience trace. Flag any mismatch
     between claimed outcome and actual evidence.
   - **Q2: Downstream dependents** — What goals depend on the changed artifact?
     Use `goal-selector.sh blocked` and scan for goals whose `blocked_by` or description
     references the same artifact, file, or module touched by the change.
   - **Q3: Knowledge/hypothesis invalidation** — Does the change invalidate existing
     tree knowledge or active hypotheses? Use `tree-find-node.sh {artifact_or_topic}` and
     `pipeline-read.sh --stage active` to check for stale facts or assumptions that
     depended on the pre-change state.

3. **Hypothesis Formation (R3)**: Form a testable prediction about the change's downstream
   impact (e.g., "Change to X will cause Y in the next N executions"). Apply the same
   calibration gate used in spark Step 0.5 — read recent accuracy for the `code_review`
   category from resolved pipeline entries, compute `recent_accuracy`, and cap confidence
   accordingly (< 0.40 accuracy caps at 0.55, < 0.60 caps at 0.65, < 0.80 caps at 0.80).
   Add to pipeline via `pipeline-add.sh` with `--type code_review --horizon short`.

4. **Findings Post (R4)**: Share the review hypothesis on the findings channel with
   `board-post.sh --channel findings --type finding --tags "code_review,{goal_id}"`.
   This ensures both agents see the architectural assessment and prediction.

5. **Outcome Tracking**: The review hypothesis resolves through the normal pipeline cycle.
   When the predicted downstream effect is observed (or not), the hypothesis is confirmed
   or corrected via `pipeline-move.sh`, producing a standard learning signal. No special
   resolution mechanism is needed — the existing pipeline handles it.

**Issue handling**: If the architectural assessment (R2) reveals concrete issues, the
reviewer creates an investigation goal as before. The deep review protocol augments
the existing review gate; it does not replace issue detection.

**Cost bound**: Maximum 3 deep reviews per B0 scan iteration. Deep reviews require
loading full experience traces and running multiple queries, so the cap prevents
unbounded context growth during the all-blocked path.

## Dependency Chains (Goal Creation)

When creating goals that depend on other goals:

1. **Always populate `blocked_by`** with prerequisite goal IDs
2. **For code-change goals**: include file paths in the description
   (prefix with `Touches: path/to/file1, path/to/file2`)
3. **Use `participants` field** for static routing:
   - `["alpha"]` — only alpha can execute
   - `["bravo"]` — only bravo can execute
   - `["agent"]` — any agent (default)

The `blocked_by` field resolves globally across all active aspirations.
`goal-selector.py` enforces blocks recursively — a goal blocked by a blocked
goal is also ineligible.

### Output-Passing Dependencies (`depends_on`)

For cross-agent workflows, use `depends_on` alongside `blocked_by` to pass factual
outputs from a completed goal into a dependent goal. When the upstream goal completes,
the verify skill reads its `handoff` board message and injects the output into the
downstream goal's description. See `goal-schemas.md` for full schema and
`aspirations-verify/SKILL.md` for the injection protocol.

### Self-Abstention

Agents can decline goals outside their capability band via `abstained_by`. The
goal-selector skips abstained goals for the abstaining agent; other agents see them
normally. See `goal-schemas.md` and `aspirations-select/SKILL.md` Phase 2.55.

## Restricted Files (Concurrent Modification Prevention)

All shared JSONL files are protected by `_fileops.py` file-level locking.
Agents MUST use scripts (never direct Edit/Write) for:

- `world/aspirations.jsonl` — via `aspirations-*.sh`
- `world/board/*.jsonl` — via `board-post.sh`
- `world/knowledge/tree/_tree.yaml` — via `tree-update.sh`
- `world/pipeline.jsonl` — via `pipeline-*.sh`

## Session Boundary Protocol

**At session end (consolidation/handoff):**
1. Release all held world goal claims via `aspirations-release.sh`
2. Post session summary to coordination: `--type status`
3. Include `held_claims: []` in `handoff.yaml`

**At session start (boot):**
1. Run board scan protocol (see above) for cross-agent context
2. Pick up any handoff items from other agents
3. Check for stale claims from own previous session (claim expiry handles this automatically)

## Directive Protocol

Directives are the primary mechanism for one agent to influence another agent's
priority selection in real-time. The `directive` message type (already in the board
schema) carries structured intent that the goal-selector mechanically applies.

### Directive Payload

Post directives to the coordination channel with `--type directive`. The message
text is a human-readable summary. Tags carry structured metadata:

| Tag | Format | Purpose |
|-----|--------|---------|
| `directive_type` | `priority_shift\|focus_window\|veto` | What kind of influence |
| `scope` | `session\|until_completed\|permanent` | How long it lasts |
| `target:<id>` | `target:g-166-06` | Specific goal to boost/deprioritize |
| `category:<name>` | `category:infrastructure` | Category-level influence |
| `weight:<N>` | `weight:+2.0` or `weight:-1.5` | Additive score modifier |
| `expires:<ISO>` | `expires:2026-04-05T00:00:00` | Auto-expiry timestamp |

### Protocol Flow

1. **Post**: Bravo posts directive to coordination channel with relevant tags
2. **Scan**: Alpha's Phase 2.07 (Directive Scan) reads directives since last scan
3. **Score**: `goal-selector.py` reads active directives, applies `directive_boost`
   as a weighted scoring criterion to matching goals/categories
4. **Acknowledge**: Receiving agent posts `--type status --reply-to <directive-id>`
   with tag `acknowledged,<agent-name>`
5. **Expire**: Directives auto-expire per their `expires` tag, or `scope: session`
   directives expire at session end

### Rules

- Directives are **non-blocking** — the agent acknowledges and factors in, never waits
- `veto` directives carry a strong negative weight (-5.0) — effectively deprioritizes a goal
- Multiple active directives stack (additive scoring)
- Directives are advisory, not commands — the agent's own judgment still applies via
  metacognitive assessment (Phase 2.5)

## Team State Protocol

Both agents maintain a shared situational awareness document at `world/team-state.yaml`.
This provides instant context about what the other agent is doing, what's strategically
important, and what's blocked — without scanning hundreds of board messages.

### Schema

```yaml
last_updated: "ISO 8601 timestamp"
last_updated_by: "agent-name"

strategic_focus:
  primary: "Short description of current strategic priority"
  rationale: "Why this is the focus"
  set_by: "agent-name"
  set_at: "ISO 8601 timestamp"
  acknowledged_by: ["agent-names who have read this"]

active_blockers:
  - id: "blocker-identifier"
    description: "What is blocked and why"
    affects: ["goal-ids or patterns"]
    reported_by: "agent-name"
    reported_at: "ISO 8601 timestamp"

recent_completions:  # ring buffer, last 10
  - goal_id: "g-NNN-NN"
    title: "Goal title"
    completed_by: "agent-name"
    completed_at: "ISO 8601 timestamp"
    key_finding: "One-line factual summary of what was produced/discovered"

agent_status:
  <agent-name>:
    last_active: "ISO 8601 timestamp"
    current_focus: "What the agent is working on"
    session_goals_completed: N

critical_blockers:  # updated by consolidation, read by boot
  - goal_id: "g-NNN-NN"
    title: "Goal title"
    cause: "Why it's blocked"
    downstream_count: N
    updated_by: "agent-name"
    updated_at: "ISO 8601 timestamp"
```

### Script API

```bash
# Read full state
bash core/scripts/team-state-read.sh [--json]

# Read a specific field (dot-notation)
bash core/scripts/team-state-read.sh --field strategic_focus.primary

# Set a field
bash core/scripts/team-state-update.sh --field strategic_focus.primary --value '"2-Day Demo"'

# Append to a list (ring buffer enforced for recent_completions)
bash core/scripts/team-state-update.sh --field recent_completions --operation append --value '{"goal_id":"g-165-03","title":"Social framework","completed_by":"bravo","completed_at":"2026-04-03T22:07:40","key_finding":"Town Square IS the framework"}'

# Remove a blocker by id
bash core/scripts/team-state-update.sh --field active_blockers --operation remove --value '"blocker-processor-gpu"'

# Initialize (idempotent — skips if exists)
bash core/scripts/team-state-init.sh
```

### Integration Points

- **Boot** (Step 2): Read `world/team-state.yaml` → display strategic focus and recent completions
- **Boot** (continuation Step 0.5): Read team state for fast situational awareness
- **State Update** (Step 3.5): After meta update, append to recent_completions and update agent_status
- **Consolidation** (Step 8.85): Update agent_status with session summary at session end
- **Create Aspiration**: Read strategic_focus to align new aspirations with team direction
- All writes go through `team-state-update.sh` (locked via `_fileops.py`)
