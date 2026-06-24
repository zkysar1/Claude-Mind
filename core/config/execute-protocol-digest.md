# Phase 4 Execution Protocol Digest

Compact reference for post-compaction re-reads. Full protocol with detailed schemas,
CREATE_BLOCKER, Cognitive Primitives, and edge cases:
`.claude/skills/aspirations-execute/SKILL.md`

Conventions: aspirations, pipeline, experience, tree-retrieval, goal-schemas, infrastructure, reasoning-guardrails

## Inputs (from orchestrator)

- `goal`: Selected goal object from Phase 2
- `aspiration_id`: Parent aspiration ID
- `source`: Queue origin (`"world"` or `"agent"`) — pass `--source {source}` to all `aspirations-*.sh` calls
- `batch_mode`: Boolean (from Phase 2)
- `outcome_class`: Set by Phase 4-post after execution

---

## Preamble: Cost-Ordered Preconditions

Check local/cheap preconditions (timestamps, git log, file existence) BEFORE expensive retrieval. (guard-009)

## Phase 3.9: Pre-Execution Domain Steps

```
Bash: load-conventions.sh pre-execution → Read if returned
Bash: source core/scripts/_paths.sh && test -f "$WORLD_DIR/conventions/pre-execution.md" && echo "exists"
IF exists: follow pre-execution steps. IF any returns SKIP → skip goal, goto Phase 7.
```

## Phase 4: Mark In-Progress

```
Bash: aspirations-update-goal.sh --source {source} <goal-id> status in-progress
Bash: aspirations-update-goal.sh --source {source} <goal-id> started <today>
```

## Phase 4-lw: Trivial-Goal Classification (lightweight mode — g-305-15)

Predict whether this goal is trivial so the Phase 3.9-4.5 ceremony can be
skipped. Runs ONCE here; `trivial_mode` is carried in-context for the rest of
Phase 4 (exactly like `effort_level`), NOT re-read per phase.

```
Bash: tg_json = py -3 core/scripts/trivial-goal-classify.py {goal.id} --source {source} --output json
Parse tg_json.verdict → trivial_mode = (verdict == "trivial")
# Master flag defaults OFF (g-306-08): verdict is "full" for EVERY goal until the
# flag is validated + flipped on, so trivial_mode stays False and the loop is
# byte-identical to pre-change behavior. Fail-to-full: any classifier error also
# yields "full" — the classifier never blocks execution and never exits != 0.
IF trivial_mode:
    Output: "▸ Lightweight mode: TRIVIAL {tg_json.reasons} — skipping Phase 3.9-4.5 ceremony (Step 5e Gate D STILL runs)"
    # Persist for compaction survival (in-flight Phase-4 state; auto-clears at
    # LOOP_CONTINUE). On a mid-Phase-4 autocompact the resumed iteration may
    # re-read phase_progress.trivial_mode OR simply re-run the classifier
    # (idempotent — goal metadata is stable); absent either it defaults to full,
    # which is the SAFE direction (re-pay ceremony, never lose learning).
    Bash: loop-state-save.sh update --set "phase_progress.trivial_mode=true"
    Bash: echo '{"entry_type":"observation","goal_id":"{goal.id}","content":"lightweight-mode trivial; skipping 3.9-4.5"}' | bash core/scripts/execution-diary.sh append
ELSE:
    trivial_mode = false   # full ceremony runs (default)
```

## Intelligent Retrieval Protocol (Steps 1-5c)

LIGHTWEIGHT MODE (g-305-15): IF trivial_mode, SKIP Steps 1 through 5d entirely
(`no_retrieval_call` guarantees retrieval adds nothing — the single largest
saving) and jump to Step 5e. The skip region ENDS at the marker just before
Step 5e; **Step 5e (Gate D) ALWAYS runs regardless of trivial_mode.**

```
IF NOT trivial_mode:   # Steps 1-5d run only on the full path (skip region ends at the END marker before Step 5e)
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

# Step 4: Supplementary stores (auto-writes retrieval-session.json when --goal set)
Bash: retrieve.sh --supplementary-only --category {goal.category} --goal {goal.id} --tree-nodes "{comma-separated primary_node keys from Step 3}"
# Returns: reasoning_bank, guardrails, pattern_signatures, experiences, beliefs
# Side effect: writes agents/<agent>/session/retrieval-session.json for utilization tracking
Output: "▸ Supplementary: {N} reasoning, {N} guardrails, {N} patterns, {N} experiences"

# Memory Deliberation: assess each supplementary item
FOR EACH item in reasoning_bank + guardrails + pattern_signatures:
    Mark: ACTIVE (will inform execution) or SKIPPED (not applicable)

# Step 4b: Strategy application (closes meta-strategy → execution feedback loop)
# Heuristics in meta/*-strategy.yaml were write-only until this step existed —
# they accumulated via reflection/evolution but never fed back into execution,
# so times_applied stayed at 0 forever (g-001-113 / g-001-117 root-cause fix).
Bash: strategy-apply.sh --goal-category {goal.category} \
         --goal-keywords "{comma-separated significant tokens from goal.title + goal.description}" \
         --increment
# Returns JSON {matched:[{id,description,file,phase,times_applied}], count}.
# Increments times_applied on each matched heuristic so /reflect --extract-patterns
# sees real usage signal and can weed stale heuristics.
IF matched is non-empty:
    Output: "▸ Strategies applied: {count} ({comma-separated matched ids})"
    FOR EACH m in matched: briefly note how it shapes execution (one line)
ELSE:
    Output: "▸ Strategies: none applicable"

# Step 5: Evaluate sufficiency — read secondary_nodes if context insufficient
IF context insufficient: Read additional nodes, increment retrieval_count

# Step 5b: Retrieval manifest (AUTO-GENERATED by retrieve.sh --goal)
# retrieve.sh auto-writes agents/<agent>/session/retrieval-session.json with tree_nodes_loaded,
# supplementary_items, counts, and utilization_pending: true.
# Optional enrichment: pipe deliberation details to wm-set.sh active_context.retrieval_manifest
# The utilization-gate.sh hook guarantees feedback runs even if Phase 4.26 is skipped.
#
# SCOPE LIMITATION (g-001-122 / bravo iter 52): utilization-feedback `--infer` and
# `times_inferred_helpful` counters are only populated when this Step 4 executes.
# Goals that skip intelligent retrieval (routine recurring goals: email check,
# operator health, hippocampal replay) do NOT produce retrieval-session.json,
# so iteration-close.sh learning-gate silently passes (line 288: `[[ -f "$ret_file" ]]`).
# Effect: `times_inferred_helpful=0` across all stores is expected for sessions
# dominated by recurring goals. --infer is a signal for retrieval-using goals only.
# Not a bug — a scope boundary. See g-001-122 reasoning for future widening option.

# Step 5c: Articulate retrieval influence (MANDATORY)
IF active_items non-empty:
    influence_line = "{how active items inform execution}"
    Output: "▸ Retrieval influence: {influence_line}"
ELSE:
    influence_line = "none — executing without retrieved context"
    Output: "▸ Retrieval influence: none — executing without retrieved context"

# G10 / R12: persist the line to working memory so iteration-close.sh
# journal-append surfaces it in the journal entry. wm-set accepts bare
# strings — no JSON wrapping needed.
Bash: echo "{influence_line}" | bash core/scripts/wm-set.sh retrieval_influence_last
# Quoting note: prose influence lines should not contain literal $ or `
# characters (bash expansion). If they do, prefer a heredoc instead:
#   bash core/scripts/wm-set.sh retrieval_influence_last <<'EOF'
#   {influence_line}
#   EOF

# Step 5d: Tier-2 Codebase Probe (G19 — conditional)
# When goal metadata names a concrete target, probe it BEFORE execution
# so the agent enters Phase 4 with first-hand knowledge of the artifact.
# Closes the gap detected post-hoc by aspirations-learning-gate Phase 9.5b
# escalation-quality check (lines ~161-167 of that SKILL.md).
IF goal.primary_action OR goal.description names a concrete file path or script:
    Read that file (or the relevant section if large)
ELIF goal.primary_action OR goal.description references a subsystem without naming a file:
    Grep for the subsystem's key identifiers (function names, class names, config keys)
    Read the top hit if unambiguous
# Skip silently when goal metadata contains no concrete target reference.
# This is a retrieval enrichment step, not a gate — never blocks execution.

# ════ END lightweight-mode skip region (Steps 1-5d) ═══════════════════════
# Step 5e ALWAYS RUNS — NEVER gated by trivial_mode. The retrieval-skip guard
# above is scoped to Steps 1-5d ONLY (g-305-15 / brief §6). GATE-INTEGRITY: the
# experiment seam stays byte-identical on BOTH the trivial and full paths.
# ── Step 5e: Gate D commons-pattern injection (DORMANT — DEFAULT OFF) ──────
# GATE-INTEGRITY (methodology 9.5): omni-blessed seam; agents MUST NOT modify.
# Added to this digest 2026-06-11 (omni) — the seam originally lived only in the
# full SKILL.md, which this digest supersedes at runtime; first pilot goal
# (g-250-120) produced an orphan OUTCOME with no ASSIGNMENT as a result.
Bash: gate_d_status = bash core/scripts/gate-d-check.sh
IF gate_d_status == "on":
    Bash: gate_d_json = bash core/scripts/gate-d-inject.sh             --goal-id "{goal.id}"             --goal-text "{goal.title}. {goal.description[:500]}"             --category "{goal.category}"
    Parse gate_d_json -> arm, status, assignment_hash, patterns, patterns_injected,
          pattern_signatures, injection_tokens, retrieval_precision, corpus_size, corpus_source
    # SINGLE-BLIND: arm B + status "injected" ONLY — emit each pattern under the
    # NEUTRAL heading below. NO "arm" / "Gate D" / "experiment" / "commons" token
    # may appear in the execution context or any narration of this step.
    IF arm == "B" AND status == "injected":
        FOR index, p IN enumerate(patterns, start=1):
            Output (into execution context, before goal.skill runs):
              "-- SUPPLEMENTARY REFERENCE PATTERN ({index}/{patterns_injected}) --"
              "Context:  {p.context}"
              "Approach: {p.approach}"
              "Lesson:   {p.lesson}"
    # arm A / no_patterns / error: COMPLETE no-op.
    # ASSIGNMENT telemetry — ONE line, append-only, BEFORE execution:
    assignment_record = {
        "record_type": "assignment", "goal_id": goal.id,
        "aspiration_id": goal.aspiration_id, "agent": "$MIND_AGENT",
        "world": "$GATE_D_WORLD", "arm": arm, "assignment_hash": assignment_hash,
        "injection_status": status, "patterns_injected": patterns_injected,
        "pattern_signatures": pattern_signatures, "injection_tokens": injection_tokens,
        "retrieval_precision": retrieval_precision, "goal_category": goal.category,
        "estimated_depth": ("deep" if goal is substantive else "routine"),
        "excluded": (status == "error"), "corpus_source": corpus_source,
        "corpus_size": corpus_size, "experiment_version": "gate-d-v1",
        "timestamp": "$(date +%Y-%m-%dT%H:%M:%S)"
    }
    Bash: append the one-line assignment_record JSON to
          agents/$MIND_AGENT/session/gate-d-telemetry.jsonl
    # Diary breadcrumb (single-blind: NO status/arm — marker only, prevents re-run):
    Bash: echo '{"entry_type":"observation","goal_id":"{goal.id}","content":"step-5e context preparation complete"}' | bash core/scripts/execution-diary.sh append
# IF gate_d_status == "off" (DEFAULT): skip entirely — zero overhead.
# ── End Step 5e ─────────────────────────────────────────────────────────────
```

Execute primary goal: `result = invoke goal.skill with goal.args`

## Outcome Classification (Binary)

```
outcome_class = "deep"  # default: immediate tree encoding
IF goal.recurring AND goal_succeeded AND no actionable items/new info:
    outcome_class = "routine"
# Everything else remains "deep" — learning is the mission.
# Non-recurring, failed, or uncertain → always "deep"

# ESCAPE HATCH (lightweight mode — g-305-15 / brief §7): the classifier was a
# PREDICTION. If execution falsified it, re-enable the full post-execution
# ceremony so no learning is lost — converting a dangerous false-positive into a
# recoverable one (we pay the tax slightly late, never lose it). Runs HERE,
# before the Phase 4.25/4.26/4.5 guards below read trivial_mode.
IF trivial_mode:
    Bash: git -C {repo} diff --stat   # the repo(s) this goal could have touched
    IF the goal produced a non-empty diff, OR a surprise fired, OR the goal failed:
        trivial_mode = false
        outcome_class = "deep"   # a falsified trivial prediction is deep by definition
        Output: "▸ Lightweight mode: ESCAPE HATCH fired (diff/surprise/failure) — reverting to FULL; Phase 4.25/4.5/4.27 will run"
        Bash: loop-state-save.sh update --set "phase_progress.trivial_mode=false"
```

## Phase 4.0: SKIP Fast-Path

If skill returns INFRASTRUCTURE_UNAVAILABLE or RESOURCE_BLOCKED:
1. Probe via `infra-health.sh check {component}` — if ok, retry once
2. If provisionable: invoke provision_skill, retry if different from goal.skill
3. If recovery fails:
   → CREATE_BLOCKER: `echo '<goal-json>' | bash core/scripts/aspirations-add-goal.sh --source {source} <aspiration_id>`
   → `Bash: aspirations-update-goal.sh --source {source} <goal-id> status pending`
   → continue

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
    4.1e: If not fixed → CREATE_BLOCKER:
          echo '<goal-json>' | bash core/scripts/aspirations-add-goal.sh --source {source} <aspiration_id>
    IF goal failed:
        Bash: aspirations-update-goal.sh --source {source} <goal-id> status pending
        continue (skip Phases 4.25-9)

IF guardrail_found_issues: outcome_class = "deep"  # override routine
```

## Phase 4.2: Domain Post-Execution Steps (IF NOT trivial_mode — already conditional on domain post-exec existing)

```
Bash: load-conventions.sh post-execution → Read if returned
Bash: source core/scripts/_paths.sh && test -f "$WORLD_DIR/conventions/post-execution.md" && echo "exists"
IF exists: follow steps, collect external_changes + behavioral_observations
```

## Phase 4.25: Experience Archival (SKIP if routine OR trivial_mode)

```
IF productive:
    experience_id = "exp-{goal.id}-{skill_slug}"
    Write agents/<agent>/experience/{experience_id}.md (full trace)
    echo '<experience-json>' | bash core/scripts/experience-add.sh
    # Include: retrieval_audit, verbatim_anchors, content_path
    echo '{"experience_refs": ["{experience_id}"]}' | Bash: wm-set.sh active_context.experience_refs
```

## Phase 4.26: Context Utilization Feedback (SKIP under lightweight mode — no retrieval-session.json; utilization-gate.sh backstop applies --all-unknown)

```
# PRIMARY PATH (script-based — one command replaces the manual loop):
Bash: utilization-feedback.sh --goal {goal.id} --helpful "node1,node2,rb-001,guard-042"
# --helpful: comma-separated IDs of items that informed execution (others marked noise)
# IDs can be tree-node keys, reasoning-bank IDs (rb-NNN), OR guardrail IDs (guard-NNN).
# --all-helpful: uniform helpful classification.
# --all-noise: uniform noise classification (LEGACY — poisons times_noise on
#   unattested-but-relevant nodes; prefer --all-unknown for the backstop case).
# --all-unknown: no-op on counters; just clears utilization_pending. The
#   preferred backstop. phase-4-26-gate still blocks goal completion (same as
#   all_noise) but no times_noise pollution.
# Reads retrieval-session.json, increments tree + supplementary counters, clears pending flag.

# BACKSTOP: utilization-gate.sh hook auto-applies --all-unknown before state-update
# if Phase 4.26 is skipped entirely (post-2026-05-07; was --all-noise pre-fix).
# The system NEVER has zero utilization data; the gate still flags backstop-only
# goals to force the LLM to attest or pass --no-retrieval-applicable.
```

## Phase 4.5: Knowledge Reconciliation (IF NOT trivial_mode — no_diff ⇒ nothing to reconcile; escape hatch re-enables on diff)

```
IF external_changes (from Phase 4.2):
    For each tree node used in retrieval:
        If stale/contradicted: update now or log to knowledge_debt
ELIF hypothesis CORRECTED:
    Reconcile affected nodes (HIGH priority — knowledge was wrong)
```

---

## ═══ POST-EXECUTION OBLIGATIONS (Phases 5-11) ═══

After Phase 4.5 completes, execute these phases **in this order**.
Each Skill() below is a **literal tool call** — do NOT inline them manually.
Writing a manual journal entry or WM update does NOT satisfy these obligations.

1. **Phase 5 VERIFY** ← MANDATORY: `Skill(aspirations-verify)` — pass goal + result
2. **Phase 6 SPARK** ← IF productive: `Skill(aspirations-spark)` — pass goal, result, effort_level
3. **Phase 7 COMPLETION REVIEW** ← IF aspiration fully complete: `Skill(aspirations-complete-review)`
4. **Phase 8 STATE** ← MANDATORY: `Skill(aspirations-state-update)` — pass goal, result, session_count, outcome_class
5. **Phase 9 EVOLUTION** ← IF cadence triggers fire: per orchestrator
6. **Phase 9.5 LEARN** ← MANDATORY: `Skill(aspirations-learning-gate)` — learning gate, retrieval gate, reflection
7. **Phase 10 STOP CHECK**: `session-state-get.sh` — if not RUNNING, break
8. **Phase 11 MAINTAIN** ← MANDATORY: Working memory maintenance — sensory buffer aging, prune stale slots

After Phase 4.5, the NEXT action must be `Skill(aspirations-verify)` — not a journal write, not a WM update, not a stop check.
If ANY mandatory phase is skipped, output: `"OBLIGATION SKIPPED: {phase}"` before continuing.
