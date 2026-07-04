---
name: reflect-maintain
description: "Performs maintenance reflection: curates memory (reasoning-bank entries, tree nodes, pattern signatures), applies active forgetting to low-utility items, grooms aspirations, and detects stuck goals that may need unblock conversion or archival. Use whenever the loop hits the maintenance cadence, the reasoning bank exceeds healthy size, the knowledge tree accumulates low-confidence nodes, or goals have stalled without progress. Invoked via /reflect --curate-memory or /reflect --curate-aspirations."
user-invocable: false
parent-skill: reflect
triggers:
  - "/reflect-maintain"
  - "/reflect --curate-memory"
  - "/reflect --curate-aspirations"
conventions: [aspirations, experience, tree-retrieval, reasoning-guardrails, pattern-signatures, goal-schemas]
minimum_mode: autonomous
revision_id: "skill-bootstrap-reflect-maintain-369223"
previous_revision_id: null
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
# rb-245 pre-read: verify counter fields exist before aggregating. Exit 1
# means the pseudocode field path has drifted from the live schema — SKIP
# this sub-phase only (other phases continue), log the mismatch for
# investigation. Do NOT --override: that silences the signal the gate exists
# to produce. Fix the field paths in the pseudocode below instead.
Bash: source core/scripts/_paths.sh && bash core/scripts/audit-schema-gate.sh \
        --jsonl-path "$WORLD_DIR/guardrails.jsonl" \
        --field-names "utilization.times_helpful,utilization.times_cited,utilization.retrieval_count"

Bash: guardrails-read.sh --active
For each guardrail with status: active:
  # Value-density criterion (guard-841) -- NOT bare v1 utilization_score (<0.20).
  # v1 ignores times_helpful/times_cited and is pre-2026-04-23 bulk-load-inflated,
  # over-flagging ~75% of active guardrails (rb-2165). times_helpful and times_cited
  # increment on EXPLICIT use only; times_inferred_helpful is the AUTOMATIC backstop
  # that increments on retrieval-application via utilization-feedback --infer. The
  # rest of the system already counts it (utility_ratio = (th + 0.5*tih)/rc), so the
  # retire bar MUST include it too -- else heavily-retrieved-but-only-inferred-helpful
  # entries (e.g. rb-200 tih=8) mass-retire as false-positive dead (g-115-1605).
  dead = (utilization.times_helpful + utilization.times_cited + utilization.times_inferred_helpful) == 0 AND utilization.retrieval_count >= 200 AND age_days >= 60
  # guard-707 gate (apply in Step 2 BEFORE retiring ANY candidate): grep CLAUDE.md +
  # .claude/skills + .claude/rules + core/config for the entry ID, EXCLUDING .history/ --
  # KEEP entries cited by ID in live framework files regardless of counters (load-bearing).
  # Canonical bulk tool for this criterion: core/scripts/bulk-retire-dead-entries.py.
  If dead: add to candidates with reason "zero value-density (helpful+cited+inferred==0) after 200+ retrievals and 60+ days"
```

### 1c: Low-Utilization Reasoning Bank Entries
```
# rb-245 pre-read: same contract as 1b.
Bash: source core/scripts/_paths.sh && bash core/scripts/audit-schema-gate.sh \
        --jsonl-path "$WORLD_DIR/reasoning-bank.jsonl" \
        --field-names "utilization.times_helpful,utilization.times_cited,utilization.retrieval_count"

Bash: reasoning-bank-read.sh --active
For each reasoning bank entry with status: active:
  # Value-density criterion (guard-841) -- NOT bare v1 utilization_score (<0.20).
  # v1 ignores times_helpful/times_cited and is pre-2026-04-23 bulk-load-inflated,
  # over-flagging ~39% of active reasoning-bank entries (rb-2165). times_helpful and
  # times_cited increment on EXPLICIT use only; times_inferred_helpful is the AUTOMATIC
  # backstop that increments on retrieval-application via utilization-feedback --infer.
  # The rest of the system already counts it (utility_ratio = (th + 0.5*tih)/rc), so the
  # retire bar MUST include it too -- else heavily-retrieved-but-only-inferred-helpful
  # entries (e.g. rb-200 tih=8) mass-retire as false-positive dead (g-115-1605).
  dead = (utilization.times_helpful + utilization.times_cited + utilization.times_inferred_helpful) == 0 AND utilization.retrieval_count >= 200 AND age_days >= 60
  # guard-707 gate (apply in Step 2 BEFORE retiring ANY candidate): grep CLAUDE.md +
  # .claude/skills + .claude/rules + core/config for the entry ID, EXCLUDING .history/ --
  # KEEP entries cited by ID in live framework files regardless of counters (load-bearing).
  # Canonical bulk tool for this criterion: core/scripts/bulk-retire-dead-entries.py.
  If dead: add to candidates with reason "zero value-density (helpful+cited+inferred==0) after 200+ retrievals and 60+ days"
```

### 1d: Contradicted/Stale/Noisy Pattern Signatures
```
# Live schema (verified 2026-04-18 via sig-001 probe, g-115-61):
#   outcome_stats: {confirmed, total, accuracy}
#   utilization: {retrieval_count, last_retrieved}
Bash: source core/scripts/_paths.sh && bash core/scripts/audit-schema-gate.sh \
        --jsonl-path "$WORLD_DIR/pattern-signatures.jsonl" \
        --field-names "outcome_stats.accuracy,outcome_stats.total,utilization.retrieval_count"

Bash: pattern-signatures-read.sh --active
For each signature with status: active:
  contradicted = outcome_stats.accuracy < 0.30 AND outcome_stats.total >= 10
  noise = utilization.retrieval_count >= 10 AND outcome_stats.total < 2   # retrieved often but never matched
  session_count = aspirations-read.sh --meta → session_count
  stale = outcome_stats.total == 0 AND created_session is not null AND (session_count - created_session) >= 10
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
            Convert to guardrail format (omit `id` and `created` — auto-set
            by the script; capture assigned id from stdout's full record):
                rule: "{the IF-THEN rule text}"
                category: "{node category from _tree.yaml}"
                trigger_condition: "{the IF condition}"
                source: "auto-promoted from tree node {node_key}"
            echo '<guardrail_json>' | bash core/scripts/guardrails-add.sh
            Read assigned id from stdout, mark rule in node: append
            [promoted: {guard-id}] to the rule line
            Log: "DECISION RULE PROMOTED: {rule summary} → {guard-id}"
```

### Step 2.55: Decision Rule Active Forgetting (E8)

Tree-node Decision Rules with `applied: 0` (or no `applied:` suffix) that
have aged past `decision_rule_retire_days` (default 60) are candidates for
retirement. The counter is maintained by aspirations-execute Phase 4.04
(E8): if a rule has never been cited as informing execution after 60 days
of being present, it's dead weight and should be removed so retrieval
surfaces fewer noisy rules.

```
Bash: tree-read.sh --leaves
For each leaf:
    Read node .md, parse `## Decision Rules` section
    For each rule line:
        Parse the `— applied: N (YYYY-MM-DD)` suffix if present
        applied_count = N (default 0 if suffix absent)
        last_applied = date from suffix (default null)
        Parse the `— source: g-NNN-NN` field
        # source goal carries creation timing; derive rule age from the
        # source goal's completed date (aspirations-read.sh --id <gid>)
        # OR from node.last_updated as a fallback

        IF applied_count == 0 AND rule_age_days > 60:
            Mark for retirement (delete the rule line from the section)
            Log: "DECISION RULE RETIRED (never applied): {rule_text[:80]}... in {node.key}"

After scanning all leaves, emit one Edit per node with retirements to
remove the dead rule lines. Keep Step 2.5 (auto-promotion) and Step 2.55
(active-forgetting) symmetric — they read the same lines but trigger
opposite actions based on the `applied` counter.

Fail-open: if the source-goal lookup fails for a rule, default to
node.last_updated as the age proxy. Never retire a rule whose age cannot
be determined at all (treat unknown age as fresh).
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

### Step 2.7: Tree-Node Cluster Archival (D2 — g-303-29)

LLM-reviewed, cluster-aware archival of stale LEAF nodes — the tree-node-specific
lane of this fire (design: relevance-aware cluster-resurgence refresh, rb-2338).
Engine: `core/scripts/tree-archive.sh`. **NATURAL-GATED DORMANT**: when
`tree_archival.archives_per_pass == 0` (the default), this lane scans + LLM-reviews +
applies keep/refresh ONLY — it applies ZERO `archive` verdicts. Raising the cap
activates destructive archival. Interior nodes are NEVER archived; default-to-keep
under uncertainty (verify-before-assuming applied to deletion). Single-writer for
`last_relevant_at` + `node_type:archived` (guard-155/rb-254).

```
Bash: bash core/scripts/tree-archive.sh scan
Parse JSON: candidates[], dormant, candidate_count.
IF candidate_count == 0: skip this lane (no output).
# Scoping (mirrors the fire's discipline): on a spark/stale_strategy or --full-cycle
# trigger, restrict to candidates whose category was touched this session (pass
# `scan --scope <comma-keys>`); the weekly recurring full-tree pass runs unscoped.
FOR EACH candidate in candidates:
    IF candidate.refresh_eligible:
        # A cluster member was retrieved within the lookback window → project
        # resurgence. Refresh the whole cluster (non-destructive), skip archival.
        Bash: bash core/scripts/tree-archive.sh apply {candidate.key} refresh
        Report: "tree-archival: refreshed cluster of {candidate.key} (resurgence)"
        CONTINUE
    # Per-candidate LLM review (design §3). Read the node body + cluster context
    # (tree-read.sh the node; the scan JSON already carries cluster + members'
    # last_retrieved). Decide exactly ONE verdict: keep | refresh | archive |
    # decompose-first. DEFAULT TO KEEP under uncertainty — archival is
    # high-blast-radius and the bytes are the team's persistent memory.
    IF verdict in (keep, refresh, decompose-first):
        Bash: bash core/scripts/tree-archive.sh apply {candidate.key} {verdict} --reason "{one-sentence evidence}"
    ELIF verdict == archive:
        IF dormant:
            # Natural gate: report the recommendation but do NOT archive.
            Report: "tree-archival: would archive {candidate.key} (dormant — archives_per_pass=0; not applied)"
        ELSE:
            Bash: bash core/scripts/tree-archive.sh apply {candidate.key} archive --reason "{one-sentence evidence}"
            Report: "tree-archival: archived {candidate.key} (reversible: tree-archive.sh restore {candidate.key})"
    Add to curation_result: {type: "tree_node_archival", key: candidate.key, decision: verdict}
```

## Step 3: Execute Retirements

For each RETIRE decision:
1. Set `status: retired`
2. Set `retirement_date: {today}`
3. Set `retirement_reason: "{brief explanation}"`
4. If strategy: append summary to `meta/strategy-archive.yaml` retired section
5. Log retirement event via `evolution-log-append.sh`:
   ```json
   {"date": "{today}", "event": "memory_curation", "details": "retired {type} {id}: {reason}"}
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
4. Log schema operation to agents/<agent>/developmental-stage.yaml:
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

### Multi-Agent Safety Pre-Check (claimed_by guard)

Before executing ANY status mutation (COMPLETE / SKIP / SCOPE-DOWN / UNBLOCK),
read `claimed_by` from the candidate goal and short-circuit if a partner agent
holds the claim:

1. **Read `claimed_by` first** (it's already in the goal record from Step 1's
   load). If `claimed_by` is set AND `claimed_by != $MIND_AGENT`, the goal is
   partner work — SKIP this candidate entirely. Do NOT execute COMPLETE, SKIP,
   SCOPE-DOWN, or UNBLOCK. Log the skip reason ("partner-claimed") in the
   grooming_result.details so the audit trail shows what was deferred.
2. **Only mutate** when `claimed_by` is null OR `claimed_by == $MIND_AGENT`.
3. The check is per-goal — a single grooming pass can mutate goals I own
   while skipping partner-held ones.

Same multi-agent safety class as `felt-sense-checkin` Phase 2 (g-115-687,
2026-05-13). Sister skill audited a parallel race surface: even though
reflect-maintain scans `pending|blocked` (not `in-progress`), a partner that
CLAIMED a pending goal but hasn't yet flipped status to in-progress can race
a COMPLETE write. The `claimed_by` field is set at claim time (before
status flip), so this guard catches the race window. Origin: g-115-685
zeta investigation + g-115-687 sibling-scan audit. See
`.claude/rules/check-team-state-before-silent.md` for the team-state probe
pattern that complements per-goal claimed_by.

```
For each COMPLETE decision:
  IF goal.claimed_by is not null AND goal.claimed_by != "$MIND_AGENT":
    SKIP — partner-claimed; record skip in grooming_result.details
    CONTINUE
  Bash: aspirations-update-goal.sh --source {asp.source} {goal_id} status completed
  Bash: evolution-log-append.sh with:
    {"date": "{today}", "event": "aspiration_grooming",
     "details": "completed {id}: {reason} (evidence: {refs})"}

For each SKIP decision:
  IF goal.claimed_by is not null AND goal.claimed_by != "$MIND_AGENT":
    SKIP — partner-claimed; record skip in grooming_result.details
    CONTINUE
  Bash: aspirations-update-goal.sh --source {asp.source} {goal_id} status skipped
  Bash: evolution-log-append.sh with:
    {"date": "{today}", "event": "aspiration_grooming",
     "details": "skipped {id}: {reason} (evidence: {refs})"}

For each SCOPE-DOWN decision:
  IF goal.claimed_by is not null AND goal.claimed_by != "$MIND_AGENT":
    SKIP — partner-claimed; record skip in grooming_result.details
    CONTINUE
  Bash: aspirations-update-goal.sh --source {asp.source} {goal_id} description "{revised description}"
  Bash: evolution-log-append.sh with:
    {"date": "{today}", "event": "aspiration_grooming",
     "details": "scoped_down {id}: {reason}"}

For each UNBLOCK decision:
  IF goal.claimed_by is not null AND goal.claimed_by != "$MIND_AGENT":
    SKIP — partner-claimed; record skip in grooming_result.details
    CONTINUE
  Bash: aspirations-update-goal.sh --source {asp.source} {goal_id} blocked_by "[]"
  Bash: evolution-log-append.sh with:
    {"date": "{today}", "event": "aspiration_grooming",
     "details": "unblocked {id}: {reason}"}

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
Bash: echo "reflect-maintain phase documented"
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

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is the last `aspirations-update-goal.sh`, `aspirations-complete.sh`,
or `journal-merge.sh` call. Never end with a text summary of maintenance decisions.
