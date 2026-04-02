---
name: boot
description: "Session entry point — status report with hypothesis readiness, pipeline, accuracy, meta-memory, and handoff to aspirations loop"
user-invocable: false
triggers:
  - "/boot"
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
  known_pitfalls: []
  reconsolidation_trigger: "After 10 invocations with declining success rate, trigger skill review"
conventions: [aspirations, pipeline, session-state, handoff-working-memory, secrets, reasoning-guardrails, tree-retrieval, pattern-signatures, journal, curriculum]
minimum_mode: autonomous
---

# /boot — Session Entry Point & Status Report

Read `core/config/status-output.md` for status line formats (session boundary, goal start, etc.).

The **single entry point** for each session. Resolves pending hypotheses (catch-up), generates a comprehensive status report, then hands off to `/aspirations loop` for perpetual execution. Auto-invoked by /start or during inline restart. Requires RUNNING agent-state.

**Key design**: Boot calls `/review-hypotheses --resolve` (detect outcomes) but does NOT trigger learning. Learning happens downstream when `/aspirations loop` picks up goals that call `/review-hypotheses --learn`.

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Phase -3: Agent State Gate Check

Bash: `session-state-get.sh` → read output.

IF output is "UNINITIALIZED" → ABORT: "Agent not initialized. User must run /start."
IF output is "IDLE" → ABORT: "Agent is stopped. User must run /start to resume."
IF output is "RUNNING" → PROCEED.

This gate means boot can ONLY run when /start has set agent-state to RUNNING.

## Phase -2.5: Crash Recovery Detection

Check for crash marker left by StopFailure hook (context exhaustion in previous session):

```
IF file exists <agent>/session/crash-marker:
  Read content (format: "<timestamp> context_exhaustion sid=<session-id>")
  Log: "⚠ Previous session ended abnormally — context exhaustion detected"
  Log the crash-marker content for diagnostics
  Delete <agent>/session/crash-marker
  Bash: session-counter-clear.sh   (may be stale from the crashed session)
```

## Phase -2: State Initialization (First Boot)

Run the deterministic init scripts. They create world/ (collective state), <agent>/ (per-agent state),
and meta/ (meta-strategies) from core/config/ `initial_state:` sections.
Idempotent — each exits immediately if its .initialized marker already exists.

```
Run: bash core/scripts/init-mind.sh $AYOAI_AGENT
IF exit code != 0: ABORT with error message
IF output contains "First boot": log "First boot detected — agent is a blank slate"
```

Note: init-mind.sh includes migration detection. If world/aspirations.yaml exists
without world/aspirations.jsonl, it auto-runs aspirations-migrate.sh.

## Phase -1.5: Session Temp Cleanup

Remove non-framework files from `<agent>/session/` left by previous goal execution
(domain data exports, analysis artifacts, ad-hoc scripts). Prevents unbounded growth.

```
WHITELISTED = [
  "agent-state", "agent-mode", "persona-active", "loop-active", "stop-loop", "stop-block-count",
  "working-memory.yaml", "handoff.yaml",
  "pending-questions.yaml", "overflow-queue.yaml",
  "last-report-timestamp",
  "compact-checkpoint.yaml",
  "pending-agents.yaml",
  "running-session-id",
  "crash-marker"
]

For each file in <agent>/session/:
  IF filename NOT in WHITELISTED:
    Delete file
    Log: "CLEANUP: removed session temp file {filename}"
```

## Phase -1: Persona State Migration

Ensure persona configuration exists in state files (handles upgrades from pre-persona installations):

1. Read `<agent>/profile.yaml`. If `persona:` key is missing, append persona defaults from `core/config/profile.yaml` `initial_state.persona`
2. Read `<agent>/profile.yaml`. If `focus` key is missing, add `focus: null` (migration for pre-focus installations)
3. Bash: `session-persona-set.sh true` — starting the loop always means full agent mode

## Phase -0.5: Environment Check

Non-blocking check for secrets/credentials availability. Never aborts boot.

```
1. IF .env.example does not exist at repo root: SKIP this phase entirely

2. Bash: env-read.sh status → parse JSON

3. IF .env.local does not exist:
     Output: "ENV: No .env.local found — copy .env.example and fill in values"
     Create user_action goals for all registered keys (see goal creation below)
     PROCEED to Step 0

4. IF .env.local exists but has missing keys:
     Bash: env-read.sh missing → parse JSON array of missing key names
     Output: "ENV: Missing credentials: {list}"
     For each missing key: create user_action goal if not already exists
     PROCEED to Step 0

5. IF all keys present:
     Output: "ENV: {N} credentials configured"
```

**Goal creation for missing credentials:**
When a missing credential is detected, create an `user_action` goal if one doesn't already exist:

```
Bash: load-aspirations-compact.sh → IF path returned: Read it (compact data has IDs, titles — no descriptions/verification)
Check for existing goals mentioning the key name
IF no existing goal for this key:
  Add goal to the infrastructure aspiration (or first active aspiration):
    title: "Add {KEY_NAME} to .env.local"
    description: "Add {KEY_NAME}={value} to .env.local. See .env.example for details."
    status: pending
    participants: [user]
    skill: null
    priority: MEDIUM
    verification:
      outcomes: ["env-read.sh has {KEY_NAME} returns true"]
      checks:
        - type: command_check
          command: "bash core/scripts/env-read.sh has {KEY_NAME}"
          condition: "exit code 0"
```

## Step 0: Run Aspiration Engine Pre-Checks

Before generating the report:
1. Bash: `load-aspirations-compact.sh` → IF path returned: Read it
   (compact data has IDs, titles, statuses, priorities, categories, skills, recurring, participants, blocked_by, deferred, args — no descriptions/verification)
   Bash: `aspirations-read.sh --meta` (readiness gates, session_count)
2. Run completion check runners (from `/aspirations` Phase 0)
3. Auto-complete user credential goals:
   ```
   IF .env.example exists:
     Bash: env-read.sh status
     For each user goal with a command_check of `env-read.sh has <KEY>`:
       Bash: env-read.sh has <KEY>
       IF exit 0: mark goal completed via aspirations-update-goal.sh
       Log: "AUTO-COMPLETED: {goal.id} — {KEY} now available in .env.local"
   ```
4. Check for recurring goals due (uses interval_hours with remind_days*24 fallback, hours_since comparison)
5. Check for newly unblocked goals
6. Read `core/config/evolution-triggers.yaml` and check performance-based triggers for evolution flags

## Step 0.5: Continuation Detection (Auto-Session)

Check for `<agent>/session/handoff.yaml` to detect auto-continuation from a previous session:

```
IF <agent>/session/handoff.yaml EXISTS (auto-continuation / inline restart from consolidation):
    1. Read handoff.yaml for previous session state
    1b. Read <agent>/self.md (Self must be in working context even during fast auto-resume)
    1b2. Read world/program.md (The Program must be in working context even during fast auto-resume)
    1c. User Goals Resume:
        IF handoff.user_goals_pending exists and count > 0:
            Output: "USER GOALS: {count} items waiting for your input"
    1d. Credential Check (runs every session — inlined from Phase -0.5):
        IF .env.example exists at repo root:
            Bash: env-read.sh missing → parse JSON array
            IF array is non-empty (missing keys exist):
                Output: "ENV: Missing credentials: {list}"
                For each missing key:
                    Bash: load-aspirations-compact.sh → IF path returned: Read it
                    (compact data has IDs, titles — no descriptions/verification)
                    Check if user goal already exists for this key
                    IF no existing goal mentions this key:
                        Add goal to first active aspiration via aspirations-update.sh:
                            (follow Phase -0.5 "Goal creation for missing credentials" schema)
            ELSE:
                Output: "ENV: All credentials configured"
    1e. Auto-Complete User Credential Goals (inlined from Step 0 sub-step 3):
        IF .env.example exists:
            Bash: env-read.sh status
            Bash: load-aspirations-compact.sh → IF path returned: Read it
            (compact data has IDs, titles, statuses, participants — no descriptions/verification)
            Find user goals with command_check of env-read.sh has
            For each such goal:
                Bash: env-read.sh has <KEY>
                IF exit 0: mark goal completed via aspirations-update-goal.sh
                Log: "AUTO-COMPLETED: {goal.id} — {KEY} now available in .env.local"
    2. Extract first_action (if present) — pre-scored goal for immediate execution
    3. Extract decisions_locked (if present) — carry forward decisions
       Remove expired entries (current_session - made_session > 3)

       Challenge world_claims (differential expiry):
       FOR EACH entry WHERE kind == "world_claim":
           IF evidence_strength == "weak" AND current_session - made_session >= 1:
               Remove entry. Log: "EXPIRED (weak evidence): {decision}"
               Clear deferred_until/defer_reason on any goal referencing this decision
           ELIF evidence_strength == "moderate" AND current_session - made_session >= 2:
               Remove entry. Log: "EXPIRED (moderate evidence): {decision}"
               Clear deferred_until/defer_reason on any goal referencing this decision
       (All entries MUST have kind — missing kind is a schema violation)
    4. Extract session_summary (if present) — structured context from prior session
    4b. Knowledge Debt Resume:
        IF handoff.knowledge_debts_pending exists and non-empty:
            Seed knowledge_debt slot:
            echo '<carried_debts_json>' | wm-set.sh knowledge_debt
            Promote any debts with sessions_deferred >= 2 to priority: HIGH
            Report: "KNOWLEDGE DEBTS CARRIED: {N} pending ({H} HIGH)"
    5. Delete handoff.yaml (consumed)
    6. Bash: `session-signal-clear.sh loop-active` (cleanup from previous cycle)
    6b. Bash: `session-counter-clear.sh` (stale counter cleanup)
    6d. Bash: `session-signal-clear.sh stop-loop` (stale stop signal cleanup)
    7. Output abbreviated status:
       "## Auto-Continuation from Session {N}
       {hypotheses_pending} hypotheses pending.
       Previous: {session_summary.goals_completed} goals completed.
       Key outcomes: {session_summary.key_outcomes}.
       Curriculum: {curriculum_stage_name} ({gates_passed}/{gates_total} gates).
       First action: {first_action.goal_id} ({first_action.reason}).
       Meta: imp@k {last_session_imp_k} ({trend}) | {active_variant or 'baseline'} | {meta_changes} changes.
       Resuming aspirations loop."
       (Curriculum line: read from Bash: curriculum-status.sh. If not configured, omit line.)
    8. Run Step 1.5 (resolve catch-up) — always check for new resolutions
    8.5. Context Priming (continuation):
        # Look up first_action.goal_id's category from aspirations data (read at sub-step 1)
        invoke /prime --category {goal_category}
    9. SKIP Steps 2-9 (no full dashboard needed for auto-continuation)
    10. Jump directly to Step 10 → handoff to /aspirations loop
        Pass first_action and decisions_locked to the loop

IF <agent>/session/handoff.yaml NOT EXISTS (user-initiated):
    1. Bash: `session-signal-clear.sh loop-active` (cleanup from crashed session)
    1b. Bash: `session-counter-clear.sh` (stale counter cleanup)
    1d. Bash: `session-signal-clear.sh stop-loop` (stale stop signal cleanup)
    2. Proceed with full boot (Steps 1.5 through 12)
```

## Step 1.5: Resolve Pending Hypotheses (Catch-Up)

Before generating the report, catch up on any hypotheses that resolved since the last session:

```
invoke /review-hypotheses --resolve

This will:
  - Check all active hypotheses for resolution (via web research, sub-skills, or timestamp)
  - Move resolved hypotheses from active → resolved
  - Set reflected: false on each (learning happens later via /aspirations)
  - Return resolve_result with newly_resolved count, triggered reviews

Store resolve_result for use in report steps below.
If no active hypotheses exist, this is a no-op.
```

## Step 2: Gather All State

Read all state files (including freshly updated data from Step 1.5):
```
<agent>/self.md                                 → agent Self (core purpose)
world/program.md                                → The Program (world shared purpose)
core/config/profile.yaml                          → system config, evaluation framework (framework)
<agent>/profile.yaml                           → strategy parameters, evaluation state (mutable state)
Bash: aspirations-read.sh --active           → aspirations and goals
Bash: aspirations-read.sh --meta             → readiness gates, session_count, last_updated
<agent>/prep-tasks.yaml                        → pending tasks
world/sources.yaml                           → information source tracking
Bash: pipeline-read.sh --counts              → pipeline stage counts
Bash: pipeline-read.sh --accuracy            → accuracy stats
Bash: pipeline-read.sh --meta                → pipeline metadata
Bash: pipeline-read.sh --stage active        → active hypotheses
Bash: pipeline-read.sh --stage discovered    → unscored hypotheses waiting
meta/meta-knowledge/_index.yaml             → meta-memory (strengths, weaknesses)
world/knowledge/patterns/violations.md       → recent violations (if exists)
<agent>/experiential-index.yaml               → experiential memory cross-references
Bash: journal-read.sh --meta                → session-level totals for episodic retrieval
Bash: journal-read.sh --latest              → context from last session
world/knowledge/tree/_tree.yaml              → identify all depth-1 nodes
For each depth-1 node: read its .md file    → capability level summaries
Bash: curriculum-status.sh                  → curriculum stage, unlocks, gates
Read meta/meta.yaml                             → meta-strategy state (imp@k, evaluations)
Read meta/improvement-velocity.yaml             → last 5 entries for trend
Bash: meta-experiment.sh list --active          → active A/B experiments
```

Display Self and The Program prominently in the dashboard:
```
═══ SELF ══════════════════════════════════════
[contents of <agent>/self.md body — everything after the YAML front matter]
```
If <agent>/self.md is empty or missing, display: "SELF: Not configured — run /start to set up."

```
═══ THE PROGRAM ════════════════════════════════
[contents of world/program.md]
```
If world/program.md is empty or missing, display: "PROGRAM: Not set — define via /start."

## Step 2.7: Context Priming

Load domain knowledge into active context before generating the dashboard.
This transforms the session from "index-aware" to "domain-aware".

```
invoke /prime
```

Prime auto-detects RUNNING state, reads aspirations and focus directive,
and loads the most relevant tree node content, guardrails, reasoning bank,
and pattern signatures. See `/prime` SKILL.md for full details.

## Step 3: Hypothesis Readiness Dashboard

```
## Hypothesis Readiness

Gates are domain-specific and evolve as the agent learns. Core gates:

| Gate | Status | Notes |
|------|--------|-------|
| Domain Knowledge | YES/NO | Relevant articles documented |
| First Research Completed | YES/NO | Topics/hypotheses discovered: N |
| Evaluation Tested | YES/NO | Hypotheses evaluated: N |
| Resolution Checking | YES/NO | Hypotheses resolved: N |
| Accuracy Baseline | YES/NO | Need 10+ resolved (have N) |
| Pattern Extraction | YES/NO | Patterns documented: N |

Overall: {N}/6 gates passed — {BOOTSTRAPPING | OPERATIONAL | LEARNING | MATURE}

Stages:
  BOOTSTRAPPING: 0-2 gates (still setting up)
  OPERATIONAL: 3-4 gates (can make hypotheses)
  LEARNING: 5-6 gates (feedback loop active)
  MATURE: 6 gates (full continual learning cycle running)
```

## Step 4: Pipeline Summary

```
### Pipeline Summary

| Stage | Count | Oldest | Action Needed |
|-------|-------|--------|---------------|
| Discovered | N | YYYY-MM-DD | Evaluate top candidates |
| Evaluating | N | YYYY-MM-DD | Complete evaluations |
| Active | N | YYYY-MM-DD | Monitor for resolution |
| Resolved | N | YYYY-MM-DD | Extract lessons |
| Archived | N | — | — |

### Active Hypotheses
| Hypothesis | Position | Confidence | Type | Current Status | Resolves | Trend |
|------------|----------|-----------|------|----------------|----------|-------|
| ... | YES | 72% | high-conviction | Tracking (+0.05) | 2026-04-15 | Favorable |

### Resolving Within 48 Hours
| Hypothesis | Position | Confidence | Current Status |
|------------|----------|-----------|----------------|
| ... | YES | 80% | On track |
```

## Step 5: Accuracy & Meta-Memory

```
### Accuracy Stats

Overall: {N}/{M} ({X}%)
Last 5 hypotheses: {X}%
Trend: {Improving/Declining/Stable}
Best category: {category} ({X}%)
Worst category: {category} ({X}%)
Confidence calibration: {well-calibrated | overconfident | underconfident}

### Self-Model (Meta-Memory)

**Strengths**: {categories where accuracy > 70%}
**Weaknesses**: {categories where accuracy < 50%}
**Blind spots**: {categories never attempted}
**Exploration ratio**: {X}% (1.0 = all exploration, 0.0 = all exploitation)

### Source Reliability (top 5)
| Source | Times Used | Reliability | Category |
|--------|----------|------------|---------|
| ... | 12 | 75% | politics |

### Knowledge Coverage
| Category | Articles | Depth | Freshest | Stalest |
|----------|----------|-------|----------|---------|
| {category-a} | N | moderate | YYYY-MM-DD | YYYY-MM-DD |
| {category-b} | N | shallow | YYYY-MM-DD | YYYY-MM-DD |

### Meta-Strategy Status
**Improvement Velocity**: {meta.yaml.last_session_imp_k} (trend: {improving|stable|declining based on delta})
**Meta Evaluations**: {evaluation_count} total ({total_meta_changes} strategy changes)
**Active Experiment**: {experiment id + description, or "none — baseline strategies active"}
**Strategy Files**: {count of meta/ strategy files with non-default content}
```

## Step 5b: Context Health Check

Monitor knowledge freshness, contradiction, and declining reliability:

```
### Context Health

Read world/knowledge/patterns/_index.yaml, world/knowledge/tree/_tree.yaml, world/sources.yaml

1. STALE KNOWLEDGE CHECK:
   For each node article at any depth with last_updated > 14 days:
     If article was cited in a corrected hypothesis (check <agent>/experiential-index.yaml):
       FLAG: "STALE + CORRECTED: {article} — last updated {date}, cited in {corrected_hypothesis}"
   For each pattern signature with outcome_stats:
     If confirmed_rate < 50% in last 10 uses:
       FLAG: "DECLINING PATTERN: {sig-NNN} — {confirmed}/{total} recent ({rate}%)"

1b. KNOWLEDGE DEBT CHECK:
   knowledge_debt = Bash: wm-read.sh knowledge_debt --json
   IF knowledge_debt has items:
     For each HIGH priority debt:
       FLAG: "KNOWLEDGE DEBT (HIGH): {node_key} — {reason} (deferred {N} sessions)"
     Report count summary in health dashboard

2. CONTRADICTION CHECK:
   For each article with interference_with entries:
     FLAG: "CONTRADICTION: {article1} ↔ {article2} — requires resolution"

3. SOURCE RELIABILITY CHECK:
   For each source in world/sources.yaml:
     If reliability < 50% AND times_used > 5:
       FLAG: "UNRELIABLE SOURCE: {source} — {reliability}% over {times_used} uses"

4. CONTEXT GAP TRENDS (from <agent>/experiential-index.yaml):
   Read by_context_quality section
   If context_gap_identified count > 0:
     Report: "{N} hypotheses had context gaps — common gaps: {list}"
   Report: "Context manifest coverage: {full_context}/{total} hypotheses ({pct}%)"

Output format:
| Check | Status | Details |
|-------|--------|---------|
| Stale knowledge | {N flags} | {brief list} |
| Contradictions | {N flags} | {brief list} |
| Source reliability | {N flags} | {brief list} |
| Context gaps | {N gaps} | {common patterns} |

If all clear: "Context health: ALL CLEAR — no staleness, contradictions, or reliability issues detected."

### Temporal Validity Alerts
Read articles at all tree depths in `world/knowledge/tree/`. For each article with `temporal_validity` front matter:
1. Calculate days since `last_confirmed`: today - last_confirmed
2. If days > `staleness_days`: flag as STALE
3. Report stale articles in the status dashboard:
   "STALE KNOWLEDGE: [article] last confirmed [N] days ago (threshold: [staleness_days] days)"
4. Stale articles should be prioritized for re-research or confirmation
```

## Step 5C: Cross-Session Reflection

Bash: `journal-read.sh --recent 5` to get last 5 session entries. Look for:
1. **Repeated topics**: Same category touched 3+ sessions without accuracy improvement → stale strategy alert
2. **Repeated patterns**: Same pattern signature triggered 3+ sessions → well-exercised, check if `validation_status` should update
3. **Coverage gaps**: Categories NOT touched in last 5 sessions → potential blind spot alert
4. **Encoding overflow persistence**: Read `<agent>/session/overflow-queue.yaml`. Items with `deferred_count >= 3` → promote to consolidation priority or discard

Report findings in the boot dashboard under "Cross-Session Insights" section.

## Step 6: Accuracy Summary

Display hypothesis accuracy from resolved hypotheses:

```
### Accuracy Summary

| Period        | Confirmed | Total    | Accuracy |
|---------------|-----------|----------|----------|
| This Week     | {wk_c}    | {wk_t}  | {wk_a}% |
| This Month    | {mo_c}    | {mo_t}  | {mo_a}% |
| All-Time      | {at_c}    | {at_t}  | {at_a}% |

### Accuracy by Category
| Category          | Confirmed | Total  | Accuracy |
|-------------------|---------|--------|----------|
| {best category}   | {c}     | {t}    | {a}%     |
| {2nd best}        | {c}     | {t}    | {a}%     |
| {worst category}  | {c}     | {t}    | {a}%     |

### Active Hypotheses
{N} hypotheses outstanding, awaiting resolution
```

## Step 6b: Capability Dashboard

Display the memory tree capability levels:

```
## Capability Dashboard (Memory Tree)

| Domain | Topic | Level | Confidence | Trend |
|--------|-------|-------|-----------|-------|
| (populated from tree nodes in world/knowledge/tree/) |

Read actual values from L1 domain files (world/knowledge/tree/*.md) YAML front matter.
On first boot with no nodes beyond L1: "No capability data yet — explore a domain first."

Recent capability changes: {list any level transitions since last boot}
Next capability unlock: {which topic is closest to next threshold}
```

Read <agent>/developmental-stage.yaml for stage context:
Read <agent>/profile.yaml for focus:
Report: "Stage: {current_stage} | Highest capability: {highest_capability} | Exploration budget: {epsilon}%"
If focus is set: append "| Focus: \"{focus text}\""

### Step 6b.5: Curriculum Stage

Display the agent's current curriculum stage, unlocks, and gate progress.
Data source: `curriculum-status.sh` output from Step 2.

```
IF curriculum-status.sh shows configured: true:
    ### Curriculum Stage

    Current: {stage_name} ({current_stage})
    Unlocks: self_edits={yes/no} | forge={yes/no} | parallel={yes/no}
    Gates: {gates_passed}/{gates_total} passed

    | Gate | Status | Current | Required | Description |
    |------|--------|---------|----------|-------------|
    | {gate.id} | PASS/FAIL | {current_value} | {threshold} | {description} |

    Next promotion: {next_stage name, or "Terminal stage — fully autonomous"}
    Promotion requires: {plain-language description of remaining gates}

IF curriculum-status.sh shows configured: false:
    "Curriculum: Not configured — agent has no staged capability restrictions."
```

### Step 6c: Domain Health Summary

Compute headline health metric per L1 domain from existing tree data.

```
# Read domain_health config
weights from core/config/tree.yaml → domain_health section
capability_weight = 0.50, coverage_weight = 0.25, confidence_weight = 0.25
min_data = 2  # minimum leaves with data to compute

# Must match capability_level values from developmental-stage.yaml
competence_mapping = {
  EXPLORE: 0.25, CALIBRATE: 0.50, EXPLOIT: 0.75, MASTER: 1.00
}

For each L1 node key in _tree.yaml (direct children of root):
  leaves = bash core/scripts/tree-read.sh --leaves-under {L1_key}

  total_leaves = count(leaves)
  populated = [l for l in leaves if l.article_count > 0]

  IF len(populated) < min_data:
    row = "| {L1_key} | — | No data | {len(populated)}/{total_leaves} | — |"
    continue

  coverage = len(populated) / total_leaves
  confidence = mean(l.confidence for l in populated if l.confidence exists)
  capability_scores = [competence_mapping[l.capability_level] for l in populated if l.capability_level exists]
  capability = mean(capability_scores) if capability_scores else 0

  health = (capability_weight * capability) + (coverage_weight * coverage) + (confidence_weight * confidence)
  health_pct = round(health * 100)

  # Find dominant capability level (most common among populated leaves)
  dominant_capability = mode(l.capability_level for l in populated)

  row = "| {L1_key} | {health_pct}% | {dominant_capability} | {len(populated)}/{total_leaves} | {confidence:.2f} |"
```

Output table:
```
### Domain Health
| Domain | Health | Capability | Coverage | Confidence |
|--------|--------|-----------|----------|------------|
{rows}
```

If no L1 nodes have sufficient data, output: "Domain health: insufficient data across all domains."

## Step 7: Aspiration Progress

```
### Aspiration Progress

| Aspiration | Priority | Progress | Status | Cooldown |
|-----------|----------|----------|--------|----------|
| asp-001: Explore and Learn | HIGH | 0/1 goals | active | — |

### Recurring Goals
| Goal | Interval | Last Done | Next Due | Status |
|------|----------|----------|----------|--------|
| (populated from recurring goals in active aspirations — show interval_hours, lastAchievedAt, computed next due) |

### Goals Ready to Execute (unblocked)
1. g-001-01: Identify learning domain (priority score: 5.0)
```

## Step 8: Alerts & Triggered Reviews

```
### Alerts

- Hypotheses resolving within 48 hours: {list}
- Stale discovered records (> 7 days unactioned): {list}
- Goals blocked for > 3 days: {list}
- Recurring goals overdue: {list}
- Knowledge articles going stale (> 30 days): {list}
- Research queue items pending > 14 days: {list}
- Accuracy dropping (last 5 below last 10): {flag}
- Confidence calibration significantly off: {flag}
- Status report: last generated {from <agent>/session/last-report-timestamp or "never"}

### Triggered Reviews
{List any auto-review triggers from resolve_result.triggered_reviews (from Step 1.5)}
```

## Step 9: Prep Tasks

```
### Prep Tasks Due

| ID | Task | Status | Blocked By |
|----|----|--------|-----------|
| pt-001 | Test API connectivity | not-started | — |
| pt-002 | Research categories | not-started | pt-001 |
```

## Step 10: Recommended Next Actions & Handoff

Based on all gathered data, prioritize actions:

```
### Recommended Next Actions

1. {Highest priority — critical blocker or expiring deadline}
2. {Second priority — unblocked goal with highest score}
3. {Third priority — recurring goal coming due}
4. {Fourth priority — discovery action if pipeline is thin}

### Unlearned Hypotheses
{N} hypotheses resolved but not yet reflected on — /aspirations loop will handle learning.
(This count comes from resolve_result.newly_resolved in Step 1.5)
```

After displaying the report, **hand off to the perpetual loop**:

```
invoke /aspirations loop
```

The aspirations loop will pick up fresh resolved data (reflected: false) and select learning goals automatically.

## Step 11: Journal Entry

Append boot report to journal .md file (NOT _index.yaml — that is owned by /aspirations State Update Step 7):

```markdown
## Boot — HH:MM

Pipeline: {N} discovered, {N} evaluating, {N} active, {N} resolved
Accuracy: {X}% overall ({N} hypotheses)
Readiness: {N}/6 gates ({stage})
Focus: {today's recommended focus area}
Alerts: {count} ({brief list})
Stage: {developmental stage} (exploration budget {N}%)
Curriculum: {stage_name} ({gates_passed}/{gates_total} gates)
```

Note: /aspirations State Update Step 7 is the authoritative owner of <agent>/journal.jsonl.
Boot creates the session's journal .md entry; the aspirations loop creates/updates the journal.jsonl session record on first goal completion via `journal-add.sh` and `journal-merge.sh`.

## Step 12: Evolution Check

Check `core/config/evolution-triggers.yaml` performance-based triggers (accuracy drop, consecutive losses, pattern divergence, capability unlock, stale strategy).
Read aspiration state: Bash: `aspirations-read.sh --active`
Read evolution history: `meta/evolution-log.jsonl`
1. Review accuracy trends
2. Review meta-memory changes since last evolution
3. Propose aspiration changes
4. Output evolution recommendations in the report
5. Suggest: `/aspirations evolve`

---

## Chaining

- **Called by**: `/start` (user command), `/aspirations` session-end consolidation (inline restart)
- **Calls**: `/prime` (context priming — Step 2.7 full, Step 8.5 continuation), `/review-hypotheses --resolve` (catch-up on resolutions, NO learning), `/aspirations` completion checks (Phase 0), `/aspirations loop` (handoff to perpetual heartbeat)
- **Does NOT call**: `/reflect` (learning happens downstream via `/aspirations` goals calling `/review-hypotheses --learn`)
- **Auto-session**: When `<agent>/session/handoff.yaml` exists, runs in continuation mode (abbreviated report, fast handoff). See `/aspirations` Auto-Session Continuation Protocol for details.
