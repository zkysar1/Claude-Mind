---
name: decompose
description: "HTN goal decomposition — break compound goals into primitive executable sub-goals"
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

## Step 2: Primitiveness Test

A goal is **primitive** (does NOT need decomposition) if ALL five criteria are met:

1. **Single actor**: Only one agent (Claude) or one user action needed
2. **Single session**: Can be completed in one session/invocation
3. **Clear completion**: Has a machine-checkable `verification` field (or legacy `desiredEndState`/`completion_check`)
4. **No hidden decisions**: Doesn't require choosing between approaches mid-execution
5. **Concrete output**: Produces a specific artifact (file, record, index update)

If ALL five → goal is primitive → STOP, return as-is.

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
| reflect, pattern, learn | `world/knowledge/patterns/`, `<agent>/journal/` (most recent) |
| evolve, strategy, adjust | `aspirations-read.sh --active`, `meta/meta-knowledge/_index.yaml` |

Only read what's needed — minimize context window usage.

## Step 5: Recursive Decomposition

```
function decompose(goal, depth=0):
    if depth >= MAX_DEPTH (4):
        return [goal]  # safety valve — stop recursing

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

        # Set dependencies (sequential sub-goals block each other)
        sg.blocked_by = [prev_sg.id] if sequential else []

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
| Journal entry | "Journal entry for {date} documents {topic}" | `{type: file_check, target: "<agent>/journal/{path}", condition: "exists"}` |
| Config update | "Config field {field} set to {value}" | `{type: config_check, file: "{path}", field: "{field}", value: "{val}"}` |
| Pattern identified | "Pattern documented in patterns article" | `{type: file_check, target: "world/knowledge/patterns/...", condition: "updated"}` |
| Script/code artifact | "File {path} exists and is functional" | `{type: file_check, target: "{path}", condition: "exists"}` |
| Hypothesis resolved | "Hypothesis {id} has outcome" | `{type: pipeline_check, id: "{id}", field: "outcome", not_null: true}` |

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
1. Read current aspiration: Bash: aspirations-read.sh --id <aspiration-id>
2. Set parent goal status to "decomposed"
3. Add field: decomposed_into: [list of sub-goal IDs]
4. Insert sub-goals into the aspiration's goals array
5. Update aspiration progress.total_goals count
6. Pipe updated aspiration JSON: echo '<aspiration-json>' | bash core/scripts/aspirations-update.sh <asp-id>
7. Update _index files if needed
8. Reach out to the user about the decomposition:
   Subject: "Aspiration Updated: <asp-title>"
   Message: "Aspiration updated: <asp-id>: <asp-title>. Decomposed <parent-goal> into <N> sub-goals: <sub-goal-titles>"
   If unable to reach the user, create a participants: [user] goal to inform them. Do NOT block.
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
