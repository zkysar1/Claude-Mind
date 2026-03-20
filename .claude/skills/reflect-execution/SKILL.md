---
name: reflect-execution
description: "Execution reflection — pattern signatures, contradiction detection, and investigation goal creation from goal outcomes"
user-invocable: false
parent-skill: reflect
triggers:
  - "/reflect-execution"
  - "/reflect --on-execution"
conventions: [reasoning-guardrails, pattern-signatures, tree-retrieval, experience, aspirations]
---

# /reflect-execution — Execution Outcome Reflection

Lightweight reflection on goal execution outcomes. Complements Phase 6.5 (immediate
learning) which handles guardrails and reasoning bank entries during spark checks.
This sub-skill handles what Phase 6.5 does NOT:

1. **Pattern signature** matching/creation from execution patterns
2. **Contradiction detection** against existing knowledge tree nodes
3. **Investigation goal creation** for findings that need follow-up
4. **Experience archival** of execution learning events

**Does NOT create guardrails or reasoning bank entries** — Phase 6.5 already owns those.
Clear separation: Phase 6.5 = create new artifacts (guardrails + rb),
this skill = cross-reference against institutional knowledge + schedule follow-up work.

## Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Step 0.5: Notability Gate (Structural)

Four structural checks on the execution outcome. If NONE fire, RETURN immediately
with zero learning overhead. This gate keeps the step nearly free for routine goals.

```
signals = []

# 1. MISTAKE: execution had errors, retries, workarounds, or failed verification
IF execution had errors, required retries, used workarounds, or verification failed:
    signals.append("mistake")

# 2. SURPRISE: outcome differed from what the agent expected before executing
#    Compare: goal.verification.outcomes (expected) vs actual result
IF execution outcome materially differs from expected outcomes:
    signals.append("surprise")

# 3. RECURRING PATTERN: same procedure/condition seen in a different goal category
#    Check working memory + recent journal for similar outcomes across categories
IF execution involved a procedure or condition encountered in a DIFFERENT goal category:
    signals.append("recurring_pattern")

# 4. VERIFICATION_GAP: Phase 5 escalation flagged a missing negative check,
#    or goal involved code changes with no test execution.
#    Catches "subtly wrong" outcomes invisible to signals 1-3.
IF goal completed AND goal.verification.checks is empty:
    Bash: wm-read.sh sensory_buffer --json
    IF sensory_buffer contains verification_gap entry for this goal.id:
        signals.append("verification_gap")
    ELIF goal execution involved code edits (Edit/Write to source files) but no test command was run:
        signals.append("verification_gap")

IF len(signals) == 0:
    Output: "▸ Exec reflection: No notable execution signals — skipped"
    RETURN
```

## Step 1: Pattern Signature Check

Match execution against existing pattern signatures. Record outcomes for matched
signatures, create new signatures for recurring patterns.

```
Bash: pattern-signatures-read.sh --active → load active signatures

# 1a. MATCH: Does this execution match an existing pattern signature?
matched = false
FOR EACH signature:
    IF execution conditions align with signature.conditions
       AND signature.category matches or is related to goal.category:

        IF execution succeeded (goal completed, verification passed):
            bash core/scripts/pattern-signatures-record-outcome.sh {sig-id} CONFIRMED
        ELSE:
            bash core/scripts/pattern-signatures-record-outcome.sh {sig-id} CORRECTED

        IF "surprise" in signals:
            # Add separation marker — what made this case different from expectation
            separation_marker = extract distinguishing factor from execution context
            bash core/scripts/pattern-signatures-update-field.sh {sig-id} separation_markers '<appended JSON>'
            Log: "EXEC PATTERN MATCH: {sig-id} — new separation marker: {marker}"

        matched = true
        BREAK

# 1b. NEW: Create a new pattern signature for recurring execution patterns
IF NOT matched AND "recurring_pattern" in signals:
    # Check if same procedure/mistake has occurred 2+ times
    # (check working memory recent_outcomes + journal for similar execution patterns)
    Bash: wm-read.sh active_context --json  # recent outcomes are part of active_context
    IF similar_execution_count >= 2:
        sig_id = next sig-NNN (check existing via pattern-signatures-read.sh --summary)
        echo '<JSON>' | bash core/scripts/pattern-signatures-add.sh
        # JSON: {
        #   id: sig_id, name: descriptive name,
        #   description: the recurring pattern,
        #   conditions: [conditions from execution context],
        #   expected_outcome: what typically happens,
        #   category: goal.category, status: "active",
        #   source: "execution-reflection",
        #   source_goals: [goal.id, prior similar goal IDs]
        # }
        Log: "NEW EXEC PATTERN: {name} — discovered from {goal.id}"
```

## Step 2: Contradiction Detection

Compare execution outcome against the target tree node's content. Fix contradictions
inline when possible, flag for investigation when not.

```
IF "surprise" in signals OR "mistake" in signals:
    # Load the tree node for this goal's category
    node=$(bash core/scripts/tree-find-node.sh --text "{goal.category}" --leaf-only --top 1)
    IF node found:
        Read node.file

        # Compare execution outcome against node's "Key Insights" section
        IF execution outcome CONTRADICTS a specific insight in the node:
            IF contradiction is simple (can be corrected in 1-2 sentences):
                # Fix inline — Edit the contradicted insight
                Edit node.file: replace or annotate the contradicted insight
                # last_update_trigger lives in .md front matter, last_updated in _tree.yaml
                Edit node.file front matter: last_update_trigger: "execution-contradiction"
                bash core/scripts/tree-update.sh --set <node.key> last_updated "$(date +%Y-%m-%dT%H:%M:%S)"
                Log: "EXEC CONTRADICTION FIXED: {node.key} — insight updated after {goal.id}"
            ELSE:
                # Too complex for inline fix — flag for Step 3 investigation goal
                contradiction_for_investigation = {
                    node_key: node.key,
                    old_insight: the contradicted text,
                    new_evidence: execution outcome,
                    reason: why it can't be fixed inline
                }

            # Log transition if fundamental (not minor refinement)
            IF contradiction is fundamental:
                Read mind/knowledge/transitions.yaml
                Append transition entry:
                    entity: node.key
                    from: "old insight summary"
                    to: "corrected insight summary"
                    evidence: "goal {goal.id} execution outcome"
                    status: "detected"
                    date: today

        ELIF execution outcome REFINES understanding (not contradiction):
            # Extract precision from refinement before compressing
            # See mind/conventions/precision-encoding.md for extraction heuristics
            IF refinement contains exact values (numbers, thresholds, code refs, formulas):
                Append to node "## Verified Values" section (create if missing):
                  - **{label}**: `{value}` {unit} — {context}
            Edit node.file: append 1-2 sentence qualitative refinement to Key Insights
            # last_update_trigger lives in .md front matter, last_updated in _tree.yaml
            Edit node.file front matter: last_update_trigger: "execution-refinement"
            bash core/scripts/tree-update.sh --set <node.key> last_updated "$(date +%Y-%m-%dT%H:%M:%S)"
            Log: "EXEC REFINEMENT: {node.key} — insight refined after {goal.id}"
```

## Step 3: Investigation Goal Creation

When a finding can't be fully resolved now, create a goal for later follow-up
using the three cognitive primitives. This turns in-the-moment observations into
scheduled work.

```
goals_to_create = []

# 3a. Contradiction too deep to fix inline (from Step 2)
IF contradiction_for_investigation exists:
    goals_to_create.append({
        title: "Investigate: {node.key} contradicts {goal.id} execution outcome",
        priority: "MEDIUM",
        type: "investigate",
        description: "Tree node {node.key} states: '{old_insight}'\n\n"
                     "But goal {goal.id} execution showed: '{new_evidence}'\n\n"
                     "Needs deeper analysis to determine which is correct and why."
    })

# 3b. Recurring pattern needs root cause analysis
IF "recurring_pattern" in signals AND similar_execution_count >= 3:
    # Pattern has been seen 3+ times — warrants investigation beyond just a signature
    goals_to_create.append({
        title: "Investigate: why {pattern_description} keeps recurring",
        priority: "MEDIUM",
        type: "investigate",
        description: "Pattern seen {count} times across goals: {goal_ids}.\n\n"
                     "Root cause analysis needed — is this a systemic issue?"
    })

# 3c. Mistake reveals broader systemic issue
IF "mistake" in signals:
    # Check if the mistake class has occurred before (search journal + experience)
    IF same_mistake_class_count >= 2:
        goals_to_create.append({
            title: "Unblock: systemic {mistake_class} across {category}",
            priority: "HIGH",
            type: "unblock",
            description: "Same mistake class occurred {count} times.\n\n"
                         "Latest: {goal.id}. Previous: {prior_goal_ids}.\n\n"
                         "Systemic fix needed — guardrail alone is insufficient."
        })

# 3d. Execution reveals improvement opportunity
IF execution revealed a technique, shortcut, or optimization that could benefit
   other goals or categories:
    goals_to_create.append({
        title: "Idea: {improvement_description}",
        priority: "MEDIUM",
        type: "idea",
        description: "During {goal.id}, discovered: {improvement_details}.\n\n"
                     "Could benefit: {benefiting_categories_or_goals}."
    })

# ── Create goals with dedup guard ─────────────────────────────────
IF len(goals_to_create) > 0:
    # Dedup: scan goal titles to avoid duplicates (same pattern as Step 8.5)
    Bash: load-aspirations-compact.sh → IF path returned: Read it
    (compact data has IDs, titles, statuses — no descriptions/verification)
    active_titles = extract goal titles with status pending/in-progress from ALL aspirations
    # Also check completed siblings — a finished goal in the same aspiration may have already addressed this
    parent_asp = goal's parent aspiration
    sibling_completed_titles = extract goal titles with status completed from parent_asp ONLY
    dedup_titles = active_titles + sibling_completed_titles

    FOR EACH new_goal in goals_to_create:
        IF similar title already exists in dedup_titles:
            Output: "▸ Exec reflection: {new_goal.type} goal already exists — skipped"
            continue

        goal_json = {
            title: new_goal.title,
            status: "pending",
            priority: new_goal.priority,
            skill: null,
            participants: ["agent"],
            category: goal.category,
            description: new_goal.description + "\n\nDiscovered by: Step 8.75 Execution Reflection",
            verification: {
                outcomes: ["Investigation complete — finding resolved or documented with reasoning"],
                checks: []
            },
            discovered_by: goal.id,
            discovery_type: new_goal.type
        }
        target_asp = parent_asp  # Route to same aspiration as source goal
        echo '<goal_json>' | bash core/scripts/aspirations-add-goal.sh <target_asp>
        Output: "▸ Exec reflection: Created '{new_goal.title}' in {target_asp}"

    goals_created_count = number of goals actually created (not skipped)
```

## Step 4: Experience Archival

If any learning occurred in Steps 1-3, archive as an experience record.

```
IF matched OR new signature created OR contradiction handled OR goals_created_count > 0:
    experience_id = "exp-exec-{goal.id}"
    Write mind/experience/{experience_id}.md with:
        ---
        type: execution_reflection
        goal_id: {goal.id}
        category: {goal.category}
        signals: {signals}
        date: {today}
        ---
        # Execution Reflection: {goal.title}

        ## Signals
        {list of signals detected and why}

        ## Pattern Signature
        {matched/created/none — details}

        ## Contradiction Detection
        {found/none — details, node affected}

        ## Goals Created
        {list of investigation/unblock/idea goals, or "none"}

        ## Key Takeaway
        {one-liner summary of what was learned}

    echo '<experience-json>' | bash core/scripts/experience-add.sh
    # JSON: {
    #   id: experience_id, type: "execution_reflection",
    #   created: today, category: goal.category,
    #   summary: "Exec reflection on {goal.title}: {signals} → {outcomes}",
    #   goal_id: goal.id,
    #   tree_nodes_related: [node.key if any],
    #   content_path: "mind/experience/{experience_id}.md"
    # }
```

## Step 5: Journal Entry

Append execution reflection summary to the session journal.

```
Append to mind/journal/YYYY/MM/YYYY-MM-DD.md:

    ## {timestamp} — Execution Reflection: {goal.title}
    Signals: {signals joined by ", "}
    Pattern: {matched sig-id / created sig-id / none}
    Contradiction: {fixed node.key / flagged for investigation / none}
    Goals created: {count} ({goal titles})
    Experience: {experience_id or "none — no learning occurred"}

Update journal index via scripts (same pattern as state-update Step 7):
    IF session entry exists: pipe update JSON to `bash core/scripts/journal-merge.sh <session-num>`
    IF session entry does not exist: pipe new entry JSON to `bash core/scripts/journal-add.sh`
```

## Chaining Map

| Direction | Skill | How |
|-----------|-------|-----|
| Called by | `/reflect --on-execution` | Mode routing from parent |
| Called by | `/aspirations-state-update` | Step 8.75 after productive goals |
| Calls | `pattern-signatures-read.sh` | Load active signatures |
| Calls | `pattern-signatures-add.sh` | Create new execution patterns |
| Calls | `pattern-signatures-record-outcome.sh` | Record outcomes on matched signatures |
| Calls | `tree-find-node.sh` | Find target node for contradiction check |
| Calls | `aspirations-add-goal.sh` | Create investigation/unblock/idea goals |
| Calls | `aspirations-read.sh` | Dedup check before goal creation |
| Calls | `experience-add.sh` | Archive execution reflection |
| Updates | Tree node `.md` files | Inline contradiction fixes and refinements |
| Updates | `mind/knowledge/transitions.yaml` | Fundamental contradictions |
| Updates | `mind/journal/` | Execution reflection entries |
