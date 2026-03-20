---
name: reflect-curate-memory
description: "Memory curation — gather candidates, evaluate, execute retirements, journal. Plus active forgetting: decay model, retrieval strengthening, interference detection, reconsolidation"
user-invocable: false
parent-skill: reflect
triggers:
  - "/reflect-curate-memory"
  - "/reflect --curate-memory"
conventions: [aspirations, experience, tree-retrieval, reasoning-guardrails, pattern-signatures]
---

# /reflect-curate-memory — Memory Curation + Active Forgetting

This sub-skill implements Mode 5 of `/reflect` plus the Active Forgetting & Knowledge Maintenance system. It is invoked by the parent `/reflect` router when `--curate-memory` is specified, or during `--full-cycle` as a light sweep scoped to categories touched in the session. It retires stale/low-utilization strategies, guardrails, reasoning bank entries, and pattern signatures, and implements hippocampal-inspired active forgetting.

Triggered by: spark question sq-c03/sq-c04, `--full-cycle` light sweep, or `stale_strategy` evolution trigger.

## Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Step 1: Gather Candidates

Scope: If triggered by spark/stale_strategy, scope to that category only. If triggered by `--full-cycle`, scope to categories touched this session.

### 1a: Stale Strategies
```
Read mind/knowledge/strategies/extracted-strategies.md
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
4. If strategy: append summary to `mind/strategy-archive.yaml` retired section
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
      → ARCHIVE to mind/knowledge/archived/ (don't delete validated work)
    Else:
      → DEPRECATE: move to mind/knowledge/deprecated/
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
4. Log schema operation to mind/developmental-stage.yaml:
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

Update `mind/knowledge/meta/_index.yaml` knowledge_articles_by_recency on every cycle.
