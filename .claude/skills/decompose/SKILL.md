---
name: decompose
description: "Performs HTN (Hierarchical Task Network) decomposition: breaks a compound goal into a sequence of primitive, directly-executable sub-goals. Use whenever a selected goal is too abstract or multi-step to execute as-is (selector or Phase 4 flags it as compound), the user says \"break down this goal\", or /aspirations-execute encounters a goal whose primary action would span multiple atomic operations. Produces child goals linked back to the parent."
user-invocable: false
triggers:
  - "/decompose"
parameters:
  - name: goal_id
    description: "Goal ID to decompose (e.g., g-001-01)"
    required: true
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
  known_pitfalls: []
  reconsolidation_trigger: "After 10 invocations with declining success rate, trigger skill review"
conventions: [aspirations, goal-schemas, tree-retrieval, pipeline]
minimum_mode: assistant
revision_id: "skill-bootstrap-decompose-b06641"
previous_revision_id: null
---

# /decompose — Hierarchical Task Network Goal Decomposition

Breaks compound goals into primitive, executable sub-goals using a recursive HTN-inspired algorithm. Called automatically by `/aspirations` during the execution loop, or manually for planning.

## Parameters

- `goal_id` (required) — The goal ID to decompose (e.g., `g-003-01`)
- `--dry-run` — Show decomposition plan without writing changes
- `--max-depth N` — Override max decomposition depth (default: 4)

## Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Step 1: Load Goal Context

```
Bash: aspirations-read.sh --active
Find goal by ID in the output (search all aspirations' goals for the goal_id)
Read any files listed in goal.context_needed
```

## Step 1.5: Idempotency Check (fast exit for already-decomposed goals)

Before Step 2's primitiveness test, check whether this goal was decomposed in
a prior session. Re-decomposing would duplicate work.

```
IF goal.status == "decomposed" OR (goal.decomposed_into is a non-empty list):
    # Verify at least one child is still live.
    Bash: aspirations-read.sh --active  (locate children by ID)
    # --active filters to status==active aspirations (g-115-2604 parity fix) —
    # a child parked in a PAUSED aspiration is invisible to this dump. Before
    # concluding a child is gone, fall back to a per-id read of its parent
    # aspiration (derive asp-id from the goal-id prefix: g-336-04 -> asp-336):
    #   Bash: aspirations-read.sh --id <asp-id>  (finds paused/any-status parents)
    # Only children found in NEITHER read count as missing/terminal.
    live_children = [c for c in children if c.status in {pending, in-progress, blocked}]
    IF live_children is non-empty:
        # No-op — prior decomposition is intact and children are actionable.
        # Emit a terminal Bash call and RETURN. Do NOT output any text,
        # insight block, table, or summary paragraph after this call.
        # Return-protocol rule (.claude/rules/return-protocol.md): text after
        # the final Bash kills the autonomous session.
        Bash: echo "decompose no-op — g-{id} already decomposed into {N} live children: {child-ids}"
        RETURN
    # If all children are terminal (completed/expired/skipped), fall through
    # to Step 2 for fresh primitiveness evaluation — parent may warrant
    # re-decomposition under new constraints.
```

## Step 2: Primitiveness Test

A goal is **primitive** (does NOT need decomposition) if ALL five criteria are met:

1. **Single actor**: Only one agent (Claude) or one user action needed
2. **Single session**: Can be completed in one session/invocation
3. **Clear completion**: Has a machine-checkable `verification` field (or legacy `desiredEndState`/`completion_check`)
4. **No hidden decisions**: Doesn't require choosing between approaches mid-execution
5. **Concrete output**: Produces a specific artifact (file, record, index update)

```
IF ALL five met → goal is primitive:
    # Terminal Bash per .claude/rules/return-protocol.md — no text output
    # may follow this call. Naming the satisfied criteria makes the no-op
    # diagnosable from the execution diary without re-reading the goal.
    Bash: echo "decompose no-op — g-{id} is primitive (criteria met: single-actor, single-session, clear-completion, no-hidden-decisions, concrete-output)"
    RETURN
```

## Step 3: Compound Detection Test

A goal IS compound (needs decomposition) if ANY of these are true:

1. **Contains "and"**: "Research X AND document Y" = at least 2 sub-goals
2. **Requires state reading**: Need to discover information before acting on it
3. **Depends on intermediate findings**: Later steps change based on earlier results
4. **Vague verbs**: "Establish", "Build", "Improve", "Set up" usually hide multiple steps
5. **Multiple skills involved**: If executing the goal would require invoking 2+ different skills
6. **Estimated effort > 1 session**: Complex goals that span sessions need breakdown

If ANY → compound → proceed to decomposition.

## Step 3.5: Domain Retrieval

Load domain context for the compound goal's category before decomposing.

```
IF goal.category exists and != "uncategorized":
    Bash: retrieve.sh --category {goal.category} --depth shallow
    # Provides reasoning bank entries about past decomposition patterns,
    # guardrails for this domain, and tree node summaries.
    # Use to inform sub-goal generation and skill assignment.
```

## Step 4: Context Selection

Before decomposing, load the minimum context needed. Map goal keywords to files:

| Keyword Pattern | Files to Read |
|---|---|
| discover, explore, find | `pipeline-read.sh --counts`, `world/knowledge/tree/_tree.yaml` |
| evaluate, score, predict | `core/config/profile.yaml` (evaluation framework), `pipeline-read.sh --stage discovered` |
| research, learn, document | `world/knowledge/tree/_tree.yaml` |
| review, accuracy, resolve | `pipeline-read.sh --stage active`, `pipeline-read.sh --stage resolved` |
| reflect, pattern, learn | `world/knowledge/patterns/`, `agents/<agent>/journal/` (most recent) |
| evolve, strategy, adjust | `aspirations-read.sh --active`, `meta/meta-knowledge/_index.yaml` |

Only read what's needed — minimize context window usage.

## Step 5: Recursive Decomposition

```
function decompose(goal, depth=0):
    if depth >= MAX_DEPTH (4):
        return [goal]  # safety valve — stop recursing (caller inspects result; if top-level
                       # returned unchanged single-element list, Step 5.5 flags it for human review)

    if is_primitive(goal):
        return [goal]  # base case

    # Generate 3-5 sub-goals that TOGETHER achieve the parent goal
    sub_goals = generate_sub_goals(goal)

    # Each sub-goal gets:
    result = []
    for sg in sub_goals:
        sg.id = "{parent_id}-{letter}"  # e.g., g-003-01-a, g-003-01-b
        sg.depth = depth + 1
        sg.parent_goal = goal.id
        sg.participants = [agent]  # default, override if user action needed
        sg.status = "pending"
        # origin-signal gate: child goal cites the parent it decomposes from.
        # REQUIRED — core/scripts/origin-signal-gate.py rejects unsigned goals
        # at aspirations-add-goal.sh time. "decomposition:<parent_goal_id>"
        # is the canonical signal for HTN decomposition children.
        sg.origin_signal = "decomposition:{parent_id}"
        sg.achievedCount = 0
        sg.currentStreak = 0
        sg.longestStreak = 0
        sg.scheduleType = "once"

        # Assign appropriate skill
        sg.skill = infer_skill(sg)

        # Define unified verification field (replaces desiredEndState + completion_check)
        sg.verification = {
            outcomes: [infer_end_state(sg)],       # human-readable success criteria
            checks: [infer_completion_check(sg)],   # machine-verifiable conditions
            preconditions: [infer_preconditions(sg)] # what must be true before execution
        }
        # HARDENING (g-115-2786 / rb-4371): if infer_preconditions emits a
        # structured {type:goal_completed_after, goal_id} precondition, it MUST
        # also carry after_ref. A missing after_ref silently perma-blocks the
        # sub-goal (predicate.py returns False forever; goal-selector excludes it
        # while status stays pending — the g-115-2688-b/-c shape). validate_verification
        # (aspirations.py) now REFUSES such a precondition at filing, so either emit
        # after_ref or express the dependency via blocked_by (below), never a bare
        # goal_completed_after.

        # Set dependencies (sequential sub-goals block each other)
        sg.blocked_by = [prev_sg.id] if sequential else []

        # Knowledge-debt cross-reference (anti-pattern 2): if the sub-goal's
        # description or category references a node_key that currently appears
        # in working-memory knowledge_debt[], pre-populate the
        # closes_knowledge_debt field so the classifier's semantic override
        # can force deep treatment on completion.
        # See goal-schemas.md "Knowledge-Debt Closure Field" and
        # aspirations-execute/SKILL.md Phase 4-post SEMANTIC OVERRIDE.
        debts = Bash: wm-read.sh knowledge_debt --json  # list of entries
        sg.closes_knowledge_debt = [
            d.node_key for d in debts
            if d.node_key appears in sg.title, sg.description, or sg.category
        ]

        # Recurse
        result.extend(decompose(sg, depth + 1))

    return result
```

### Sub-Goal Generation Rules

1. **Max 5 sub-goals per decomposition pass** — if more are needed, the parent needs splitting first
2. **Each sub-goal must map to exactly one skill** — if it spans skills, decompose further
3. **Sub-goals must be ordered** — set `blocked_by` for sequential dependencies
4. **Each sub-goal needs a `verification` field** — outcomes (human-readable) + checks (machine-verifiable)
5. **Preserve the parent goal's intent** — sub-goals must collectively achieve the parent
6. **No speculative decomposition** — don't decompose goals that are blocked by unresolved dependencies

### Skill Inference Table

| Sub-goal pattern | Skill |
|---|---|
| Research/learn/document a topic | `/research-topic` |
| Discover/explore/find opportunities | `/research-topic` or domain-specific forged skill |
| Evaluate/score/assess a hypothesis | Domain-specific forged skill or manual evaluation |
| Review/check/resolve hypotheses | `/review-hypotheses` |
| Form/test/verify a hypothesis | `/review-hypotheses --hypothesis <id>` |
| Reflect/extract patterns/learn from outcomes | `/reflect` |
| Further decompose a complex sub-goal | `/decompose` (recursive) |
| Update strategy/evolve aspirations | `/aspirations evolve` |
| Execute/run/build/create an artifact | Forged skill with Bash, or direct goal with tools_used |
| Fetch/call/query an API endpoint | Forged skill with WebFetch, or `/research-topic` |
| Generate/produce/export a report | Direct goal with Write tool |
| Analyze/process/transform data | Direct goal with Bash (scripts) |

### Skill Inference Refinement (Relation Graph)

After the static Skill Inference Table lookup, refine skill assignment using the
skill relation graph (`core/config/skill-relations.yaml` + `world/skill-relations.yaml`):

```
IF sg.skill is null OR confidence is low:
    # Check compose_with chain from parent goal's skill
    Bash: skill-relations.sh read --composable {parent_goal.skill}
    composable_skills = parse JSON output
    IF any composable skill's description matches sg's pattern:
        sg.skill = matched_composable_skill

IF sg.skill is set:
    # Check for similar alternatives with better quality scores
    Bash: skill-relations.sh read --similar {sg.skill}
    IF similar skills found:
        Bash: skill-evaluate.sh read --all --summary
        FOR EACH similar_skill:
            IF similar_skill.quality.overall > sg_skill.quality.overall + 0.15:
                sg.skill = similar_skill
                Log: "SKILL ROUTING: Substituted {similar_skill} for {original} (quality delta +{delta})"
```

### Verification Inference

For each sub-goal, infer the `verification` field using these patterns:

| Output type | verification.outcomes | verification.checks |
|---|---|---|
| Knowledge article | "Article exists at {path} with {topic} content" | `{type: file_check, target: "{path}", condition: "exists"}` |
| Pipeline records | "Pipeline has {N}+ records in {stage}" | `{type: pipeline_count, stage: "{stage}", min: N}` |
| Journal entry | "Journal entry for {date} documents {topic}" | `{type: file_check, target: "agents/<agent>/journal/{path}", condition: "exists"}` |
| Config update | "Config field {field} set to {value}" | `{type: config_check, file: "{path}", field: "{field}", value: "{val}"}` |
| Pattern identified | "Pattern documented in patterns article" | `{type: file_check, target: "world/knowledge/patterns/...", condition: "updated"}` |
| Script/code artifact | "File {path} exists and is functional" | `{type: file_check, target: "{path}", condition: "exists"}` |
| Hypothesis resolved | "Hypothesis {id} has outcome" | `{type: pipeline_check, id: "{id}", field: "outcome", not_null: true}` |

### Step 5.5: Post-Decomposition Empty-Result Guard

After Step 5's recursive decomposition completes, verify it actually produced
new sub-goals. If the top-level call returned `[goal]` unchanged — caused by
max-depth safety valve firing at depth 0, or by no sub-goals being generated —
fall through WITHOUT running Step 6 (which would rewrite the aspiration with
no new children and potentially corrupt state).

```
IF decompose() returned [goal] unchanged (single-element list containing only the input):
    # Safety valve fired at top level OR sub-goal generation produced nothing.
    # Either is a skill-level failure, not silently-correct behavior. Log it
    # and emit terminal Bash — do NOT write changes via aspirations-update.sh.
    Bash: echo "decompose safety — g-{id} recursion produced no new sub-goals (max_depth={MAX_DEPTH}, initial_depth=0). Flagged for human review. No aspiration changes written."
    # TERMINAL BASH per .claude/rules/return-protocol.md — no text output may follow.
    RETURN
```

### Step 5.6: PDDL Symbolic-Plan Constraint (optional overlay; LLM tree retained as R3 fallback)

If this world ships a PDDL decomposition-planning overlay, obtain a VALIDATED
symbolic plan for the goal's decomposition SHAPE and use it to constrain the
Step-5 LLM tree's leaves. The Step-5 LLM tree is the R3 fallback and is ALWAYS
retained — the symbolic plan CONSTRAINS it, it does not replace it. This is the
GO-path completion of the /decompose planning capstone (spike → per-shape
domains → this wiring).

The capability is a **world overlay**: the routing map and STRIPS domains live
in `world/conventions/pddl-domain/` (`shape-map.json` + `mind-decompose-*.pddl`
+ `validate_plan_generic.py`). The core helper carries no domain terms; when no
overlay exists (a fresh world) or the goal's shape is unmapped, it returns
`applicable=false` and this step is a no-op — pure-LLM decomposition stands.

SCOPE: this is the framework's own `/decompose` ONLY. Do NOT invoke or touch any
domain-specific behavior planners (e.g. a separate htn/astar/dual
behavior-planning track) — those are out of scope and carry their own goals.

```
# One fail-open call. The helper resolves the world overlay itself and re-runs
# the planner only when a cached plan is missing/stale. Exit code is ALWAYS 0 —
# branch on the JSON content, never the exit code (rb-2207: the plan is
# independently re-validated inside the helper; trust `valid`, not "pyperplan
# returned").
Bash: py -3 core/scripts/decompose-pddl-plan.py \
        --category "{goal.category}" \
        --verb "{goal.skill or the goal's primary decomposition verb}" \
        --title "{goal.title}" \
        --goal-id "{goal.id}"
plan = parse JSON stdout

IF plan.applicable == false:
    # No overlay, or shape unmapped. The Step-5 LLM tree IS the decomposition (R3).
    Log: "decompose: PDDL constraint N/A ({plan.reason}) — pure-LLM tree retained"
    # fall through to Companion Hypothesis Generation / Step 6 unchanged.

ELIF plan.applicable == true AND plan.solvable == true:
    # The shape's decomposition is symbolically solvable AND the plan was
    # independently re-validated (plan.valid). plan.plan is the canonical
    # primitive ORDERING for this shape. CONSTRAIN the Step-5 sub-goals to it:
    #   - Re-order / set blocked_by so the generated sub-goals' sequence and
    #     dependencies follow plan.plan's primitive order (the validated DAG).
    #   - A generated leaf with no counterpart primitive is KEPT (the LLM tree
    #     is richer than the skeleton). A primitive with no generated leaf is a
    #     COVERAGE GAP — add a sub-goal covering it so the decomposition is
    #     complete against the validated plan.
    #   - Annotate the parent goal object (in-context, persisted by Step 6's
    #     write) with:
    #       pddl_plan: {shape: plan.shape, plan: plan.plan, valid: plan.valid,
    #                   plan_length: plan.plan_length,
    #                   source: "decompose-pddl-plan g-306-54"}
    Log: "decompose: PDDL plan ({plan.shape}, {plan.plan_length} steps, valid={plan.valid}) constrains tree leaves"

ELIF plan.applicable == true AND plan.solvable == false:
    # The shape maps to a domain but has NO valid symbolic plan (structural
    # decomposition problem). File a blocker inline — the goal cannot be cleanly
    # decomposed under its shape. The Step-5 LLM tree is STILL retained as the R3
    # fallback (do NOT discard it); the blocker flags the structural concern for
    # review while the LLM decomposition proceeds.
    invoke CREATE_BLOCKER(
        affected_skill="/decompose",
        issue="g-{goal.id} shape '{plan.shape}' is symbolically UNSOLVABLE: {plan.blocker_reason}",
        ... per the CREATE_BLOCKER protocol in aspirations-execute)
    Log: "decompose: PDDL UNSOLVABLE for {plan.shape} — blocker filed; LLM tree retained as R3"
```

### Companion Hypothesis Generation

When decomposing a goal, check: does this goal's work naturally produce a testable prediction?

If yes, create a companion hypothesis goal alongside the sub-goals:
1. Create pipeline record: `echo '<record-json>' | bash core/scripts/pipeline-add.sh` (stage defaults to discovered)
2. Add hypothesis goal to the same aspiration with:
   - `participants: [agent]`
   - `skill: "/review-hypotheses --hypothesis {hypothesis_id}"`
   - `blocked_by: [prerequisite sub-goal IDs]` (the research/work that informs the prediction)
   - `hypothesis_id`, `horizon`, `resolves_no_earlier_than`, `resolves_by`
3. Move to active: `bash core/scripts/pipeline-move.sh <id> active`
4. This is optional — only create when a genuine testable prediction exists. Do not force hypotheses.

## Step 6: Write Decomposition

```
1. File each sub-goal under the aspiration — one call per sub-goal. The daemon
   appends to the goals array AND recomputes progress.total_goals automatically.
   Pass --override-duplication on EACH child. WHY: decomposition children
   legitimately overlap their parent AND their already-filed siblings (all share
   the parent's vocabulary + the "decomposition:<parent>" origin_signal). The
   goal-duplication gate's Strategy-1 (origin_signal exact-match) already exempts
   them, and _lineage_relation demotes a child-vs-PARENT structural overlap — but
   Strategy-2 (structural keyword/file overlap) still HARD-BLOCKS a child against
   its already-filed SIBLINGS (g-115-1446 deliberately keeps Strategy-2 firing on
   siblings as the only backstop against a decompose emitting two IDENTICAL
   children; _lineage_relation has no decomposition-sibling case). So the 2nd+
   child of every multi-child decomposition trips structural_overlap and needs the
   override (observed: g-115-2688-a/b/c each needed it — g-115-2702; note the
   symptom reads "overlap with pending PARENT" but the real blocker is the
   siblings). Passing it consciously here IS the review g-115-1446 intends —
   /decompose KNOWS these are distinct deliverables of one parent — and the
   override is audit-logged to world/goal-duplication-overrides.jsonl with the
   justification, so a genuinely-duplicate child still surfaces there for review:
   echo '<sub-goal-json>' | bash core/scripts/aspirations-add-goal.sh <aspiration-id> --source {goal.source} --override-duplication "decomposition child of <parent-goal-id> — distinct deliverable that legitimately shares parent+sibling vocabulary; structural_overlap expected (g-115-1446 backstop preserved via this audit-logged override; g-115-2702)"
2. Mark the parent goal decomposed (goal-level field-merge):
   Bash: aspirations-update-goal.sh <parent-goal-id> status decomposed --source {goal.source}
3. Record the children on the parent goal:
   Bash: aspirations-update-goal.sh <parent-goal-id> decomposed_into '[<sub-goal-ids>]' --source {goal.source}
   # --source = the parent goal's queue from selector output (Source Routing
   # Protocol rules 1-2: propagate to every call; children go to the parent's queue)
4. Update _index files if needed
5. Notify the user about the decomposition.
   (Check world/forged-skills.yaml for a skill whose triggers match
   "notify the user" and invoke it with:
     subject: "Aspiration Updated: <asp-title>"
     message: "Aspiration updated: <asp-id>: <asp-title>. Decomposed <parent-goal> into <N> sub-goals: <sub-goal-titles>"
   If no matching skill is registered, fall back to a `participants: [agent, user]`
   goal via aspirations-add-goal.sh. Never block decomposition on notification failure.)
Bash: echo "decompose phase documented"
```

**All invocations**: Execute immediately, no approval needed. Show decomposition in report output.

## Step 7: Report

Output:
```
## Decomposition: {goal.id} — {goal.title}

Primitiveness: COMPOUND (failed criteria: {list})
Depth: {depth}
Sub-goals generated: {count}

| # | ID | Title | Skill | Depends On |
|---|---|---|---|---|
| 1 | g-003-01-a | ... | /research-topic | — |
| 2 | g-003-01-b | ... | /review-hypotheses | g-003-01-a |

Next: Execute first unblocked sub-goal via /aspirations next
```

## Guardrails

- **Max depth 4** — if decomposition reaches depth 4 without primitives, flag for human review
- **Max 5 sub-goals per pass** — forces appropriate granularity
- **No speculative decomposition** — only decompose goals whose dependencies are resolved
- **Auto-decompose always** — execute immediately in all contexts, no approval gates
- **Preserve parent intent** — sub-goals must collectively achieve what the parent described
- **Every sub-goal gets a skill** — if you can't assign a skill, the sub-goal is too vague

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.

This skill has **four exit paths**, each with its own terminal Bash call. No path
may end with text output, summary paragraph, insight block, or table after its
final Bash call — that kills the autonomous session (incident 2026-04-22 alpha
session-56: /decompose no-op emitted terminal Bash, then appended a summary
paragraph, and the aspirations loop never re-entered).

| Exit path | Where | Terminal Bash |
|---|---|---|
| Already-decomposed no-op | Step 1.5 | `echo "decompose no-op — g-{id} already decomposed..."` |
| Primitive no-op | Step 2 | `echo "decompose no-op — g-{id} is primitive..."` |
| Empty-result safety valve | Step 5.5 | `echo "decompose safety — g-{id} recursion produced no new sub-goals..."` |
| Normal completion | Step 6 (main path) → Step 7 output → final `Bash: echo "decompose phase documented"` | see Step 6 line 256 |

After ANY of the four terminal Bash calls: **no text output of any kind**. No
"Decomposition: ..." table, no "Next: ..." hint, no "✶ Insight" block. Whatever
needs to be said to the orchestrator or the user goes INTO the echo string
before the Bash call, not into text after it.
