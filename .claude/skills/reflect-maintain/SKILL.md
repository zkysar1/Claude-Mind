---
name: reflect-maintain
description: "Maintenance reflection — memory curation, active forgetting, aspiration grooming, stuck goal detection"
user-invocable: false
parent-skill: reflect
triggers:
  - "/reflect-maintain"
  - "/reflect --curate-memory"
  - "/reflect --curate-aspirations"
conventions: [aspirations, experience, tree-retrieval, reasoning-guardrails, pattern-signatures, goal-schemas]
minimum_mode: autonomous
---

# /reflect-maintain — Maintenance Reflection

This sub-skill implements all maintenance reflection modes for `/reflect`. It is invoked
by the parent router for two modes:

- **Memory curation mode** (`--curate-memory`): Retire stale/low-utilization artifacts + active forgetting
- **Aspiration grooming mode** (`--curate-aspirations`): Detect stuck goals whose evidence has converged

Each mode section below is self-contained with its own step numbering.

---

## Mode: Curate Memory (--curate-memory)

This sub-skill implements Mode 5 of `/reflect` plus the Active Forgetting & Knowledge Maintenance system. It is invoked by the parent `/reflect` router when `--curate-memory` is specified, or during `--full-cycle` as a light sweep scoped to categories touched in the session. It retires stale/low-utilization strategies, guardrails, reasoning bank entries, and pattern signatures, and implements hippocampal-inspired active forgetting.

Triggered by: spark question sq-c03/sq-c04, `--full-cycle` light sweep, or `stale_strategy` evolution trigger.

## Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Step 1: Gather Candidates

Scope: If triggered by spark/stale_strategy, scope to that category only. If triggered by `--full-cycle`, scope to categories touched this session.

### 1a: Stale Strategies
```
Bash: world-cat.sh knowledge/strategies/extracted-strategies.md
For each strategy with status: active:
  stale = (today - last_reinforced).days > 30 AND times_applied == 0
  If stale: add to candidates with reason "unused for 30+ days"
```

### 1b: Low-Utilization Guardrails
```
Bash: guardrails-read.sh --active
For each guardrail with status: active:
  low_util = utilization_score < 0.20 AND retrieval_count >= 5
  If low_util: add to candidates with reason "low utilization after sufficient retrievals"
```

### 1c: Low-Utilization Reasoning Bank Entries
```
Bash: reasoning-bank-read.sh --active
For each reasoning bank entry with status: active:
  low_util = utilization_score < 0.20 AND retrieval_count >= 5
  If low_util: add to candidates with reason "low utilization after sufficient retrievals"
```

### 1d: Contradicted/Stale/Noisy Pattern Signatures
```
Bash: pattern-signatures-read.sh --active
For each signature with status: active:
  contradicted = hit_rate < 0.30 AND times_triggered >= 10
  noise = utility_ratio < 0.20 AND times_retrieved >= 10
  session_count = aspirations-read.sh --meta → session_count
  stale = times_triggered == 0 AND created_session is not null AND (session_count - created_session) >= 10
  If any condition met: add to candidates with reason
```

### 1e: Experience Archive Curation
```
# Experience archive curation
Bash: experience-archive.sh  # sweep stale experiences to archive

# Curation signals from experience data
Bash: experience-read.sh --least-retrieved 10
For each low-retrieval experience:
    IF experience.created > 30 days ago AND retrieval_count == 0:
        Log: "Experience {id} is unused — already archived by sweep"
    Identify tree_nodes_related from these low-retrieval experiences
    For each such tree node:
        IF node.last_updated > 30 days ago AND low retrieval across experiences:
            Flag as curation candidate (potential MERGE into parent during /tree maintain)
            Log: "Tree node {key} has low-retrieval experiences and aging content — curation candidate"
```

If no candidates found: log "No retirement candidates found", return empty result.

## Step 2: Evaluate (Agent Judgment — NOT Automatic)

For each candidate:
1. Read full content of the artifact
2. Assess:
   - Is there a better replacement already in the knowledge base?
   - Has the domain context changed since this was created?
   - Would removing this leave a gap in our reasoning capabilities?
3. Decision: **RETIRE** | **KEEP** | **REVISE** (flag for update, don't retire)

IMPORTANT: Retirement is never automatic. The agent must reason about each candidate.

### Step 2.5: Decision Rule Auto-Promotion to Guardrails

Scan tree nodes with established Decision Rules and promote mature rules to guardrails.

```
Bash: tree-read.sh --leaves
For each leaf with utility_ratio >= 0.5 AND retrieval_count >= 5:
    Read node .md, check for ## Decision Rules section
    For each rule NOT already marked [promoted: guard-NNN]:
        IF rule has been in the node for 2+ sessions (check last_update_trigger.session):
            Convert to guardrail format:
                id: next guard-NNN
                rule: "{the IF-THEN rule text}"
                category: "{node category from _tree.yaml}"
                trigger_condition: "{the IF condition}"
                source: "auto-promoted from tree node {node_key}"
            echo '<guardrail_json>' | bash core/scripts/guardrails-add.sh
            Mark rule in node: append [promoted: {guard-id}] to the rule line
            Log: "DECISION RULE PROMOTED: {rule summary} → {guard-id}"
```

### Step 2.6: Tree Node Utility Curation

Identify tree nodes for DISTILL or RETIRE based on accumulated utility signals.

```
Bash: tree-read.sh --distill-candidates
IF candidates exist:
    Report: "{N} tree nodes eligible for DISTILL"
    For each candidate:
        Add to curation_result as: {type: "tree_node", key, utility_ratio, action: "DISTILL"}

Bash: tree-read.sh --leaves
For each leaf with retrieval_count == 0:
    Bash: aspirations-read.sh --meta → session_count
    IF node has existed for retire_sessions_unused (5) sessions:
        Add to curation_result as: {type: "tree_node", key, action: "RETIRE", reason: "never retrieved"}

# Actual DISTILL/RETIRE execution happens via /tree maintain (session-end consolidation Step 6).
# This step identifies candidates and reports them.
```

## Step 3: Execute Retirements

For each RETIRE decision:
1. Set `status: retired`
2. Set `retirement_date: {today}`
3. Set `retirement_reason: "{brief explanation}"`
4. If strategy: append summary to `meta/strategy-archive.yaml` retired section
5. Log retirement event via `evolution-log-append.sh`:
   ```json
   {"event": "memory_curation", "action": "retired", "artifact_type": "{type}", "artifact_id": "{id}", "reason": "{reason}", "date": "{today}"}
   ```

## Step 4: Journal Entry + Return Result

Log curation activity to journal. Return:
```yaml
curation_result:
  candidates_found: {N}
  retired: {N}
  kept: {N}
  flagged_for_revision: {N}
  details: [{artifact_id, decision, reason}]
```

---

## Active Forgetting & Knowledge Maintenance (Hippocampal)

Enhanced decay model inspired by hippocampal memory consolidation and active forgetting.
Reference: `core/config/memory-pipeline.yaml` for full configuration.

### Decay Model

```
retention = e^(-days_since_reinforcement / (lambda * importance * type_decay))

Where:
  days_since_reinforcement = days since last RETRIEVAL or last UPDATE (whichever is more recent)
  lambda = 30 (base half-life in days, from core/config/memory-pipeline.yaml)
  importance = encoding_score from memory pipeline (0.15 - 1.0)
  type_decay factors (from core/config/memory-pipeline.yaml):
    validated_strategy: 3.0   # Strategies with strong evidence decay slowest
    strategy: 2.0
    pattern: 1.5
    violation: 1.0
    analysis: 0.7
    refuted: 0.3              # Refuted strategies decay fastest
```

### Retrieval Strengthening

When any skill reads a knowledge article during active use (not just indexing):
```
Update article's YAML front matter:
  retrieval_count += 1
  last_retrieved = today
This resets days_since_reinforcement to 0 — the hippocampal "retrieval practice" effect.
Frequently-used knowledge persists; unused knowledge decays.
```

### Active Pruning (run during /aspirations evolve)

```
For each leaf node article:
  Calculate retention score using decay model

  If retention >= 0.7 → ACTIVE: use freely
  If retention 0.4-0.7 → AGING: flag for reinforcement or re-research
  If retention < 0.4:
    If article has validated evidence (statistical significance):
      → ARCHIVE to world/knowledge/archived/ (don't delete validated work)
    Else:
      → DEPRECATE: move to world/knowledge/deprecated/
      → bash core/scripts/tree-update.sh --remove-child <parent-key> <child-key>
      → Log: "ACTIVE FORGETTING: {article} pruned (retention {score})"
```

### Interference Detection

When new knowledge contradicts existing knowledge:
```
1. Flag both articles as "interfering" in YAML front matter:
   interference_with: ["path/to/contradicting-article.md"]
2. During next /replay session, prioritize resolving the interference
3. Resolution: one article strengthened (evidence wins), other weakened or accommodation triggered
4. Log schema operation to <agent>/developmental-stage.yaml:
   type: "accommodation" if framework changes, "assimilation" if framework holds
```

### Reconsolidation on Retrieval

```
When an article is retrieved for use in a hypothesis (during evaluation):
  Set reconsolidation_window: true in article front matter
  During that session, any new evidence about the article's topic
  can UPDATE the article directly (not just append) — beliefs become temporarily labile
  After session end (consolidation pass): reset reconsolidation_window: false
  This is how strategies get updated with new evidence rather than accumulating stale content
```

Update `meta/meta-knowledge/_index.yaml` knowledge_articles_by_recency on every cycle.

---

## Mode: Curate Aspirations (--curate-aspirations)

This sub-skill implements backlog grooming for the aspiration system. It is invoked
by the parent `/reflect` router when `--curate-aspirations` is specified, or during
`--full-cycle` as step 1.75. It detects stuck goals whose evidence has already
converged and closes, re-scopes, or unblocks them.

**Why this exists**: Aspirations get stuck on operational blockers (infrastructure
sessions, external compute, user input) while evidence converges elsewhere — sibling
goals complete, hypotheses resolve, knowledge tree fills in. Without grooming, these
aspirations sit blocked indefinitely until manual intervention.

Triggered by: `--full-cycle` step 1.75, or direct invocation `--curate-aspirations`.

## Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Step 1: Gather Candidates

```
Bash: load-aspirations-compact.sh → IF path returned: Read it
(compact data has IDs, titles, statuses, priorities, categories, recurring, blocked_by, deferred, started — no descriptions/verification)
For each aspiration:
  For each goal where status in (pending, blocked):
    candidate = false

    # Skip recurring goals — they reset naturally via interval mechanism
    IF goal.recurring == true: SKIP

    # 1a: Stuck goals — started but never completed
    IF goal.started is set AND (goal.achievedCount is unset OR achievedCount == 0):
      candidate = true, reason = "started but never completed"

    # 1b: Stale blockers — all dependencies resolved but blocked_by not cleared
    IF goal.blocked_by is non-empty:
      Look up each dependency goal ID in the same aspiration
      resolved_deps = [dep for dep in blocked_by where dep.status in (completed, skipped)]
      IF len(resolved_deps) == len(blocked_by):
        candidate = true, reason = "all dependencies resolved but still marked blocked"

    # 1c: Expired deferral — deferred_until has passed, goal never picked up
    IF goal.deferred_until is set AND deferred_until < now AND (achievedCount is unset OR achievedCount == 0):
      candidate = true, reason = "deferral expired, never executed"

    # 1d: Mature aspiration, stale goal — aspiration is 50%+ done, this goal is lagging
    IF aspiration.progress.completed_goals >= aspiration.progress.total_goals * 0.5:
      IF goal.started is set AND (achievedCount is unset OR achievedCount == 0):
        candidate = true, reason = "aspiration mature (50%+ done), goal stale"

    # 1e: Orphaned deferral — defer_reason references infrastructure but
    #     no matching decisions_locked entry exists (decision was invalidated/expired)
    IF goal.defer_reason is set:
      IF defer_reason mentions blocked/unavailable/down/infrastructure:
        Check decisions_locked from current session context (passed from boot)
        IF no decisions_locked entry substantiates the defer_reason:
          candidate = true, reason = "deferral reason not backed by active decision"

    IF candidate: add {aspiration_id, goal, reason} to grooming_candidates
```

If no candidates found: log "No grooming candidates found", return empty result. STOP.

## Step 2: Evidence Cross-Reference (Agent Judgment)

For each candidate, evaluate whether existing evidence already covers the goal:

```
1. Read goal.verification.outcomes — what would "done" look like?

2. Cross-reference against accumulated evidence:

   a. Experience archive:
      Bash: experience-read.sh --category {goal.category}
      Do any experiences demonstrate the goal's expected outcomes?

   b. Knowledge tree:
      Bash: retrieve.sh --category {goal.category} --depth shallow
      Does the tree already contain the answers this goal would produce?

   c. Sibling goal outcomes:
      Review completed/skipped goals in the SAME aspiration.
      Did a sibling goal already produce this goal's expected outputs?
      (e.g., a live test completed that covers the same data as a benchmark goal)

   d. Pipeline hypotheses:
      Are there resolved hypotheses whose outcomes render this goal moot?
      (e.g., hypothesis confirmed that makes the goal's premise false)

3. Decision — one of:

   COMPLETE — evidence shows ALL verification.outcomes already satisfied.
             Cite specific evidence: experience IDs, tree node keys, sibling goal IDs.

   SKIP    — goal's thesis falsified or rendered moot by other outcomes.
             Cite the falsifying evidence.

   SCOPE-DOWN — partial evidence exists. Revise goal description to cover
                only the remaining gap. Cite what's already covered.

   UNBLOCK — all blocked_by dependencies are resolved. Clear blocked_by,
             leave goal as pending for normal execution.
             Also for orphaned deferrals (reason 1e): clear deferred_until
             and defer_reason (set to null). Log: "UNBLOCKED: {goal.id} —
             deferral reason orphaned, no supporting decision"

   KEEP    — still needed, no existing evidence covers the outcomes.
             No action taken.
```

**IMPORTANT**: Grooming is never automatic. The agent MUST reason about each
candidate and cite specific evidence for every COMPLETE or SKIP decision.
A goal that "feels done" is not done — the evidence must be traceable.

## Step 3: Execute Decisions

```
For each COMPLETE decision:
  Bash: aspirations-update-goal.sh --source {asp.source} {goal_id} status completed
  Bash: evolution-log-append.sh with:
    {"event": "aspiration_grooming", "action": "completed", "goal_id": "{id}",
     "reason": "{reason}", "evidence": ["{refs}"], "date": "{today}"}

For each SKIP decision:
  Bash: aspirations-update-goal.sh --source {asp.source} {goal_id} status skipped
  Bash: evolution-log-append.sh with:
    {"event": "aspiration_grooming", "action": "skipped", "goal_id": "{id}",
     "reason": "{reason}", "evidence": ["{refs}"], "date": "{today}"}

For each SCOPE-DOWN decision:
  Bash: aspirations-update-goal.sh --source {asp.source} {goal_id} description "{revised description}"
  Bash: evolution-log-append.sh with:
    {"event": "aspiration_grooming", "action": "scoped_down", "goal_id": "{id}",
     "reason": "{reason}", "date": "{today}"}

For each UNBLOCK decision:
  Bash: aspirations-update-goal.sh --source {asp.source} {goal_id} blocked_by "[]"
  Bash: evolution-log-append.sh with:
    {"event": "aspiration_grooming", "action": "unblocked", "goal_id": "{id}",
     "reason": "{reason}", "date": "{today}"}

# Post-decision sweep
After all decisions executed:

  # Auto-complete aspirations where all goals are now done
  For each aspiration touched:
    Bash: aspirations-read.sh --source {asp.source} --id {asp_id}
    IF any goal has recurring == true:
      SKIP — aspirations with recurring goals are perpetual (data layer blocks archival)
    ELIF all goals have status in (completed, skipped):
      Bash: aspirations-complete.sh --source {asp.source} {asp_id}

  # Knowledge reconciliation (M.11-12 pattern)
  For each COMPLETE or SKIP decision:
    Check knowledge tree nodes referenced by the goal's category
    IF any nodes contain "TBD" entries or stale status values:
      Edit the node to resolve TBDs and correct stale data
      Update last_update_trigger front matter
```

## Step 4: Journal + Return Result

Log grooming activity to journal via `journal-merge.sh` (append key_events).

Return:
```yaml
grooming_result:
  candidates_found: {N}
  completed: {N}
  skipped: {N}
  scoped_down: {N}
  unblocked: {N}
  kept: {N}
  aspirations_closed: [{asp_id, ...}]
  details:
    - goal_id: {id}
      decision: {COMPLETE|SKIP|SCOPE-DOWN|UNBLOCK|KEEP}
      reason: "{explanation}"
      evidence_refs: ["{experience_id}", "{tree_node_key}", "{sibling_goal_id}"]
```

---

## Chaining Map (All Modes)

| Direction | Skill/Script | Modes | How |
|-----------|-------------|-------|-----|
| Called by | `/reflect --curate-memory` | Memory | Mode routing from parent |
| Called by | `/reflect --curate-aspirations` | Aspirations | Mode routing from parent |
| Called by | `/aspirations-spark` (sq-c03) | Memory | Scoped to category |
| Calls | `guardrails-read.sh`, `guardrails-add.sh` | Memory | Read candidates, promote rules |
| Calls | `reasoning-bank-read.sh` | Memory | Read candidates |
| Calls | `pattern-signatures-read.sh` | Memory | Read candidates |
| Calls | `experience-archive.sh`, `experience-read.sh` | Memory | Archive curation |
| Calls | `tree-read.sh` | Memory | Decision rule promotion, utility curation |
| Calls | `evolution-log-append.sh` | Both | Audit trail |
| Calls | `load-aspirations-compact.sh` | Aspirations | Load goal data |
| Calls | `aspirations-update-goal.sh` | Aspirations | Execute decisions |
| Calls | `aspirations-complete.sh` | Aspirations | Auto-close finished aspirations |
| Calls | `retrieve.sh` | Aspirations | Evidence cross-reference |
| Calls | `journal-merge.sh` | Both | Activity logging |
| Does NOT call | `/aspirations`, `/reflect`, `/boot` | — | No recursive skill invocations |
| Does NOT modify | agent-state, session signals, working memory | — | Clean boundaries |
