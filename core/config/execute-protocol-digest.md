# Phase 4 Execution Protocol Digest

Compact reference for post-compaction re-reads. Full protocol with detailed schemas,
CREATE_BLOCKER, Cognitive Primitives, and edge cases:
`.claude/skills/aspirations-execute/SKILL.md`

Conventions: aspirations, pipeline, experience, tree-retrieval, goal-schemas, infrastructure, reasoning-guardrails

---

## Preamble: Cost-Ordered Preconditions

Check local/cheap preconditions (timestamps, git log, file existence) BEFORE expensive retrieval. (guard-009)

## Phase 3.9: Pre-Execution Domain Steps

```
Bash: load-conventions.sh pre-execution → Read if returned
Bash: source core/scripts/_paths.sh && test -f "$WORLD_DIR/conventions/pre-execution.md" && echo "exists"
IF exists: follow pre-execution steps. IF any returns SKIP → skip goal, goto Phase 7.
```

## Intelligent Retrieval Protocol (Steps 1-5c)

```
Output: "▸ Intelligent retrieval: scanning knowledge tree..."

# Step 1: Tree index (cached)
Bash: load-tree-summary.sh
IF output non-empty: Read the returned path

# Step 2: Reason about goal needs
Given goal description, skill, category, verification:
- Which tree summary nodes are relevant? Use tree-find-node.sh for concept→node
- Identify: primary_nodes (must read), secondary_nodes (might need), experience_categories

# Step 3: Read tree node .md files (effort-gated)
FOR EACH node_key in primary_nodes:
    IF effort_level in ("minimal", "standard"):
        # Try active-only retrieval first (Decision Rules + Verified Values only)
        Bash: tree-read.sh --active-content {node_key}
        IF active_content is not null:
            Use active_content (saves context on routine goals)
        ELSE:
            Read {node.file}  # fallback: full read if no active sections
    ELSE:
        Read {node.file}  # full content for "full" effort goals
    Bash: tree-update.sh --increment {node_key} retrieval_count
Output: "▸ Tree nodes: {keys} ({N} loaded, {A} active-only)"

# Step 4: Supplementary stores
Bash: retrieve.sh --supplementary-only --category {goal.category}
# Returns: reasoning_bank, guardrails, pattern_signatures, experiences, beliefs
Output: "▸ Supplementary: {N} reasoning, {N} guardrails, {N} patterns, {N} experiences"

# Memory Deliberation: assess each supplementary item
FOR EACH item in reasoning_bank + guardrails + pattern_signatures:
    Mark: ACTIVE (will inform execution) or SKIPPED (not applicable)

# Step 5: Evaluate sufficiency — read secondary_nodes if context insufficient
IF context insufficient: Read additional nodes, increment retrieval_count

# Step 5b: Write retrieval manifest (MANDATORY)
echo '<manifest_json>' | Bash: wm-set.sh active_context.retrieval_manifest
# Fields: goal_id, goal_title, timestamp, tree_nodes_loaded,
#   supplementary_counts: {reasoning_bank, guardrails, patterns, experiences},
#   deliberation: {active_items: [{id,type}], skipped_items: [{id,type}]},
#   utilization_pending: true

# Step 5c: Articulate retrieval influence (MANDATORY)
IF active_items non-empty:
    Output: "▸ Retrieval influence: {how active items inform execution}"
ELSE:
    Output: "▸ Retrieval influence: none — executing without retrieved context"
```

Execute primary goal: `result = invoke goal.skill with goal.args`

## Outcome Classification

```
outcome_class = "productive"  # default
IF goal.recurring AND goal_succeeded AND no actionable items/new info:
    outcome_class = "routine"
# Non-recurring, failed, or uncertain → always "productive"
```

## Phase 4.0: SKIP Fast-Path

If skill returns INFRASTRUCTURE_UNAVAILABLE or RESOURCE_BLOCKED:
1. Probe via `infra-health.sh check {component}` — if ok, retry once
2. If provisionable: invoke provision_skill, retry if different from goal.skill
3. If recovery fails → CREATE_BLOCKER, set goal pending, continue

## Phase 4.1: Post-Execution Guardrails + Error Response

```
IF involved_infrastructure (skill or category in infra-health.yaml mappings):
    Bash: guardrail-check.sh --context infrastructure --outcome {flag} --phase post-execution
    FOR EACH matched guardrail: run action_hint command
    IF issues found: guardrail_found_issues = true

IF guardrail_found_issues OR (goal failed AND infrastructure):
    4.1a: Check error alerts (sleep 45 if not from guardrails, read via error_check config)
    4.1b: Cascade detection — sort by time, earliest = root cause
    4.1c: Severity: confirmed_infrastructure | explicit_failure | soft_failure
    4.1d: Try inline fix from knowledge tree/reasoning bank/experience
    4.1e: If not fixed → CREATE_BLOCKER protocol
    IF goal failed: set pending, continue (skip Phases 4.25-9)

IF guardrail_found_issues: outcome_class = "productive"  # override routine
```

## Phase 4.2: Domain Post-Execution Steps

```
Bash: load-conventions.sh post-execution → Read if returned
Bash: source core/scripts/_paths.sh && test -f "$WORLD_DIR/conventions/post-execution.md" && echo "exists"
IF exists: follow steps, collect external_changes + behavioral_observations
```

## Phase 4.25: Experience Archival (SKIP if routine)

```
IF productive:
    experience_id = "exp-{goal.id}-{skill_slug}"
    Write <agent>/experience/{experience_id}.md (full trace)
    echo '<experience-json>' | bash core/scripts/experience-add.sh
    # Include: retrieval_audit, verbatim_anchors, content_path
    echo '{"experience_refs": ["{experience_id}"]}' | Bash: wm-set.sh active_context.experience_refs
```

## Phase 4.26: Context Utilization Feedback

```
Bash: wm-read.sh active_context.retrieval_manifest --json
IF productive AND manifest exists AND manifest.goal_id == current goal:
    FOR active items: increment times_helpful or times_noise
    FOR skipped items: increment times_skipped
# Always clear utilization_pending (even for routine):
IF manifest exists: echo 'false' | Bash: wm-set.sh active_context.retrieval_manifest.utilization_pending
```

## Phase 4.5: Knowledge Reconciliation

```
IF external_changes (from Phase 4.2):
    For each tree node used in retrieval:
        If stale/contradicted: update now or log to knowledge_debt
ELIF hypothesis CORRECTED:
    Reconcile affected nodes (HIGH priority — knowledge was wrong)
```
