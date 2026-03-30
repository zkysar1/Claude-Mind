---
name: create-aspiration
description: "Self-driven aspiration creation — from user input, autonomous generation, or full planning cycle"
user-invocable: false
triggers: []
conventions: [aspirations, goal-schemas, tree-retrieval]
minimum_mode: assistant
---

# /create-aspiration — Self-Driven Aspiration Creation

Creates aspirations aligned with the agent's Self (core purpose). Two modes:

- **from-user**: Parse user's natural language into aspirations + goals
- **from-self**: Autonomous generation — "Given Self and current state, what's needed?"
  - **`--plan`**: Full planning cycle: introspective scan + Self-grounded web research + structured deliberation

## Invocation Patterns

| Call Pattern | Behavior |
|-------------|----------|
| `from-self` (default) | Current 4-phase introspective scan. Unchanged. |
| `from-self --plan` | Full planning cycle: introspective scan + Self-grounded web research + structured deliberation |
| `from-self` with context params | Context is available — the LLM decides how much introspective scanning to add. No flag needed. |

## Called By

- `/start` (first boot or backfill — user's initial aspirations)
- `/aspirations evolve` (replacement aspirations, gap analysis uses `--plan`)
- `/aspirations` Phase 0.5 health (uses `--plan` when below minimum)
- `/aspirations` Phase 2 alignment check (uses `--plan` when alignment data suggests it)
- `/aspirations` Phase 2 no-goals fallback (uses `--plan`)
- `/aspirations` Phase 7 archival (uses `--plan` when aspiration completes)
- `/aspirations-spark` sq-013 (passes `discovery_context`, auto-detected)
- `/aspirations-consolidate` (passes `batch_context`, auto-detected)
- `/respond` directive routing (user says "add aspiration about X")

## Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Step 1: Read Self

```
Read <agent>/self.md → extract Self (body content after front matter)
IF <agent>/self.md is empty or missing:
    ABORT: "Cannot create aspirations — Self is not defined. Run /start to set up."
```

## Step 2: Read Current State

```
Bash: load-aspirations-compact.sh          → IF path returned: Read it (compact aspirations data — IDs, titles, statuses, priorities, categories, skills, recurring, participants, blocked_by, deferred, args, parent_goal, discovered_by, started — no descriptions/verification)
Bash: aspirations-read.sh --summary       → all including archived (for dedup)
Bash: tree-read.sh --stats                → knowledge coverage
Read <agent>/developmental-stage.yaml        → maturity level
Read core/config/aspirations.yaml              → max_active cap, aspiration_scopes, default_scope
```

## Step 3: Determine Aspirations

### Mode: from-user

Parse the user's input. Extract aspiration titles + descriptions.

Align with Self:
- If user says "learn about cooking" but Self is "an AI research assistant",
  create aspiration that connects cooking to the Self's purpose, or
  create it as-is if the user explicitly wants it.
- Trust the user's intent — Self guides but does not override explicit requests.

### Mode: from-self

Four-phase autonomous generation:

```
Phase A — Purpose scan:
    "What does Self need that isn't covered by current aspirations?"
    Read Self carefully. Consider:
      - What capabilities does Self imply the agent should have?
      - What ongoing activities does Self suggest?
      - What would a person with this purpose naturally prioritize?
    Generate aspirations that advance Self's mission.

    # Meta-strategy aspiration heuristics
    Read meta/aspiration-generation-strategy.yaml
    # Apply agent-learned generation preferences:
    # - preferred_scope_ratio: learned balance of sprint/project/initiative
    # - category_saturation: categories already well-covered
    # - generation_heuristics: learned rules for aspiration generation
    # These are advisory — the agent uses judgment on whether to follow.

    # Constraint awareness
    Bash: wm-read.sh active_constraints --json && Bash: wm-read.sh known_blockers --json
    If active constraints or unresolved blockers exist:
      - Avoid generating goals that require blocked resources/skills
      - Prefer goals executable with currently available infrastructure
      - If constraint_context was passed from evolve: strictly exclude avoid_skills

Phase B — Data acquisition scan:
    Read world/knowledge/tree/_tree.yaml → node summaries + entity_index
    For each node: does it reference data sources, systems, APIs,
    environments, or files that the agent hasn't directly accessed?
    "What data do I KNOW ABOUT in my knowledge tree that would be
    great to actually obtain and analyze?"
    Example: a node documenting API access to production data
    → the agent should aspire to access and catalog that data.
    Generate aspirations for high-value data acquisition.

Phase C — Capability gap scan:
    What does Self need to DO that the agent can't yet?
    What skills/knowledge are missing to serve Self's purpose?
    What domains are mentioned in Self but have no tree nodes?
    Generate aspirations for capability building.

Phase D — Pain scan:
    "What is broken, blocked, or degraded right now?"
    Bash: wm-read.sh known_blockers --json
    Bash: journal-read.sh --recent 3        → recent failures, skipped goals
    Read <agent>/experiential-index.yaml       → categories with declining accuracy

    For each pain signal found:
    - Unresolved blockers → blocker_resolution goals
    - Repeated skill failures → diagnosis goals
    - Declining category accuracy → research or diagnosis goals
    - Stale/contradicted knowledge → cleanup goals

    If pain requires user input (access grants, design decisions, external
    system changes the agent can't make): create user_action goal AND
    attempt to notify user with the pain context.
    Don't just queue it silently — surface it.

    Pain-driven aspirations default to priority: HIGH (they're blocking real work).
    Skip if no pain signals found — not every session has problems.
```

Combine results from all four phases. Deduplicate and prioritize.

### Step 3.7: Scope Classification

For each candidate aspiration from Step 3, classify its scope.
Read `core/config/aspirations.yaml` → `aspiration_scopes` for definitions.

```
Classification criteria (in order):
  1. Does this require learning something new?                  → project+
  2. Does this involve building or changing code/infrastructure? → project+
  3. Will this take more than one session of focused work?       → project+
  4. Does it span multiple knowledge tree categories?            → initiative
  5. Is it a quick tactical response to a known problem?         → sprint

Default: "project" for from-self without explicit context.
Sprint: only for genuinely small tactical work (a single well-understood fix,
  a cleanup, a reactive response to a specific known problem).
Initiative: cross-cutting concerns, strategic directions, or phased multi-project work.

Cognitive primitive goals (Unblock/Investigate/Idea) are scope-exempt —
  they are always lightweight regardless of the parent aspiration's scope.

Set scope on the aspiration JSON: "scope": "sprint" | "project" | "initiative"
Also set: "sessions_active": 0

Log: "Scope: {scope} — because {brief justification}"
```

### Context-Aware Routing (no flag needed)

When `follow_up_context`, `discovery_context`, `forge_context`, or `batch_context`
is passed by a caller, the LLM has specific work that needs an aspiration. It should
use that context directly. The introspective scan (Phases A-D) is typically less
relevant in this case, but the LLM decides — maybe the context sparks a broader idea
worth scanning for. Self is still read in Step 1 (alignment with purpose always matters).
The caller just passes context; the skill exercises judgment on how to use it.

### Step 2.5: Self-Grounded Research (`--plan` only, scope-dependent depth)

When `--plan` is set, research externally before generating aspirations.
Self drives the research — ask Self what it WANTS to know.
**Research depth scales with scope.**

```
IF scope == "sprint":
    SKIP this step — sprint aspirations don't need external research.

Read <agent>/self.md → what are Self's priorities, curiosities, blind spots?
Read world/knowledge/tree/_tree.yaml → where are the knowledge gaps?

IF scope == "project" or scope == "initiative":
    # DEEP RESEARCH MODE — genuine understanding of the problem space.
    # This is NOT lightweight scanning. Project+ aspirations require
    # the agent to understand what it's getting into before committing goals.
    #
    # 1. Formulate 2-4 research questions grounded in Self + aspiration direction
    # 2. WebSearch each question
    # 3. For top 2-3 results per question: WebFetch and READ full content
    #    (not just snippets — understand approaches, pitfalls, testing strategies)
    # 4. Tree deep-dive for the aspiration's likely category:
    #    Bash: retrieve.sh --category {likely_category} --depth medium
    #    Cross-reference research findings against existing knowledge.
    #    What does the agent already know? What contradicts? What's new?
    #
    # 5. Synthesize into a research_brief:
    #    - What approaches exist for this problem?
    #    - What are the common pitfalls and failure modes?
    #    - What testing strategies are used in this domain?
    #    - What dependencies or prerequisites exist?
    #    - What does the agent's existing knowledge say?

    research_brief = {findings, approaches, pitfalls, testing_strategies, prerequisites}
    # Feed into Step 3.5 alongside introspective phases
```

### Step 3.5: Structured Deliberation (`--plan` only)

Self is the judge — re-read Self and evaluate candidates against it.

```
Read <agent>/self.md (explicit re-read — Self anchors the deliberation)

# The LLM now has: introspective candidates (Phases A-D) + web findings
# Self decides what matters. The LLM should:
#   - Ask "Would Self prioritize this?" for each candidate
#   - Consider alignment_data if passed (uncovered priorities, category gaps)
#   - Use judgment on balance — Self may have shifted emphasis since
#     aspirations were created
#
# No rigid rules about "ensure at least 1 of X" — the LLM reads Self,
# reads the data, and makes the call.

Journal the planning cycle for future reflection:
  echo '{"date":"<today>","event":"planning_cycle","details":"--plan deliberation: {N} candidates evaluated, {M} selected","trigger_reason":"<caller context>"}' | bash core/scripts/evolution-log-append.sh
```

## Step 4: Generate Goals

### Step 4a: Domain Context Scan

Quick orientation before generating goals:

```
Read <agent>/self.md → what domain is this agent for?
Read world/knowledge/tree/_tree.yaml → what does the agent already know?
IF Self references an external codebase:
    Scan that project's CLAUDE.md for test frameworks (pytest, jest, gradle test, etc.)
```

This is a 2-3 file scan, not a formal research step.

### Step 4a.5: Plan the Plan (project+ scope only)

Before generating individual goals, outline the work lifecycle. This prevents
the "3 goals and done" pattern by forcing the agent to think about the full arc.

```
IF scope == "sprint": SKIP — sprint goals don't need lifecycle planning.

For each aspiration, outline:
  1. RESEARCH phase: What needs to be understood first?
     (web research, code audits, data analysis, reading existing knowledge)
  2. BUILD phase: What concrete artifacts will be created or changed?
     (code changes, scripts, configurations, infrastructure)
  3. TEST phase: How will each build artifact be verified?
     (unit tests, integration tests, behavioral verification, regression checks)
  4. INTEGRATION phase: Do the pieces work together end-to-end?
     (cross-component verification, live system validation)
  5. KNOWLEDGE phase: What should be encoded to the knowledge tree?
     (findings, patterns, lessons learned)

Each phase should map to 1-3 goals. Not every phase applies to every aspiration
(e.g., pure research aspirations skip BUILD), but the agent must explicitly
consider each phase and explain why it's included or skipped.

For INITIATIVE scope — also plan WORK PHASES (temporal):
  - Phase 1 goals (this session + next 1-2): research + initial build
  - Phase 2 goals (sessions 3-5): deepen, test, iterate
  - Phase 3 goals (sessions 5+): advanced, integration, transfer
  Only generate Phase 1 goals now. Record Phase 2-3 plans in
  the aspiration's description for future goal generation.
```

### Step 4b: Generate Core Goals

Goal count is determined by aspiration scope (read from `core/config/aspirations.yaml` → `aspiration_scopes`):
- **sprint**: 2-5 goals (tactical, quick — the only scope that allows < 5 goals)
- **project**: 5-15 goals (research + build + test lifecycle from Step 4a.5)
- **initiative**: first 8-12 goals of an eventual 10-30 (Phase 1 only; later phases deferred)

```
Requirements:
  - Specific and actionable (not "learn about X" but "Connect to the
    data source and catalog the data structure" or "analyze JSONL files
    to understand usage patterns")
  - Each with: skill assignment, verification (outcomes + checks),
    priority, blocked_by chain if sequential
  - Use goal types from core/config/aspirations.yaml → goal_templates as
    patterns, but don't force-fit
  - Primitive (single skill, single session, clear deliverable)

CALIBRATION CHECK (project+ scope — mandatory):
  After generating goals, verify the list includes:
  - At least 1 research/understanding goal (understand the space before building)
  - At least 1 test/verification goal per build/change goal
  - At least 1 knowledge encoding goal (capture what was learned)
  If any category is missing: ADD the missing goals. This is structural, not optional.
  Sprint scope: calibration check is advisory (guidance, not required).

Goal structure (pipe to aspirations-add.sh):
  - id: g-NNN-NN
  - title: descriptive action
  - skill: which skill executes this (/research-topic, /review-hypotheses, etc.)
  - participants: [agent] (default)
  - category: tree node key for this goal's domain
    # Determine: Bash: category-suggest.sh --text "{goal.title}. {goal.description}" --top 1
    # Use the top match's key. If no match (score 0): use "uncategorized"
  - status: pending
  - priority: HIGH/MEDIUM/LOW
  - verification:
      outcomes: ["Human-readable success criteria"]
      checks: [{type, target, condition}]
      preconditions: ["What must be true before execution"]
  - blocked_by: [goal-ids] if sequential dependency
```

### Step 4c: Verification Reflection

After generating core goals, pause and think:

> "I just planned work that will change something in the world. How would I
> know if it actually worked? What would a senior engineer do to verify this?"
>
> For each goal that BUILDS, CHANGES, or CREATES something:
> - What could go wrong?
> - How would I detect if it went wrong?
> - What test would catch a regression later?
>
> If the answer suggests a test goal: add one.

**For sprint scope** — four principles (guidance, not rules):

1. **Research-only aspirations** usually don't need test goals. Knowledge encoding IS the verification.
2. **Code-change aspirations** should have substantial test goals. "Substantial" means the test exercises the change, not a one-liner checking if a file exists.
3. **Complex multi-component aspirations** need integration tests. If 3 goals change 3 modules, add a goal verifying they work together.
4. **Test goals should be blocked_by the goals they verify.**

**For project+ scope** — verification is MANDATORY, not advisory:

1. Every goal that BUILDS, CHANGES, or CREATES something **MUST** have a companion
   test goal (`blocked_by` the build goal). No exceptions.
2. "Test" means: exercise the change and verify behavior — not just check a file exists.
3. If you cannot articulate a test for a build goal, the build goal is under-specified.
   Revise the build goal's description until a test becomes obvious.
4. Multi-component aspirations **MUST** have at least one integration test goal that
   verifies the pieces work together end-to-end.

### Step 4d: Final Shaping

Review the full goal list for:
- Primitiveness (single skill, single session, clear deliverable)
- Correct blocked_by chain (tests after builds, analysis after research)
- Appropriate priority (tests same priority as what they verify)

### Step 4.5: Examples

Don't copy these — use them to calibrate scope-based planning.

**Sprint: "Diagnose hypothesis accuracy drop in API caching"** (4 goals, scope: sprint)
1. Audit recent hypothesis outcomes in API caching category (`null`)
2. Diagnose root cause of accuracy decline (`null`, blocked_by: audit)
3. Research corrective patterns (`/research-topic`, blocked_by: diagnosis)
4. Form calibration hypothesis to verify fix (`/review-hypotheses`, blocked_by: research)
- *Pain-driven and tactical. Diagnosis IS the work. Sprint scope is correct.*

**Project: "Fix authentication token refresh bug"** (8 goals, scope: project)
1. Research OAuth token refresh best practices (`/research-topic`)
2. Audit current token refresh flow across all services (`null`)
3. Analyze failure logs for token refresh race conditions (`null`, blocked_by: audit)
4. Implement fix for token refresh race condition (`null`, blocked_by: analysis)
5. **Add regression test for token refresh under concurrent requests** (`null`, blocked_by: fix)
6. **Verify fix in deployed environment via log analysis** (`null`, blocked_by: fix)
7. **Integration test: verify token refresh works across all dependent services** (`null`, blocked_by: fix + regression)
8. Encode token refresh patterns into knowledge tree (`null`)
- *Project scope: research → build → test → integrate → encode. Each build has a test companion.*

**Project: "Research and catalog cloud-native patterns"** (6 goals, scope: project)
1. Research cloud-native architecture patterns — containers, serverless, orchestration (`/research-topic`)
2. Deep-dive: read 3 reference architectures in full, extract patterns (`/research-topic`)
3. Audit current system against discovered patterns (`null`, blocked_by: research)
4. Form testable predictions about container vs serverless tradeoffs (`/review-hypotheses`, blocked_by: audit)
5. Build comparison matrix of approaches with pros/cons (`null`, blocked_by: deep-dive)
6. Encode findings into knowledge tree with cross-references (`null`, blocked_by: all research)
- *Even pure research aspirations need depth at project scope — multiple research passes, analysis, hypothesis formation.*

**Initiative: "Build automated data pipeline validation framework"** (Phase 1: 10 goals, scope: initiative)
*Phase 1 — Research + Foundation (this session + next 2):*
1. Research existing data validation patterns and frameworks (`/research-topic`)
2. Research industry testing strategies for data pipelines (`/research-topic`)
3. Audit current data structure for testable assertions (`null`)
4. Design validation schema based on research + audit (`null`, blocked_by: research + audit)
5. Implement core validation runner (`null`, blocked_by: design)
6. Implement 3 smoke validations (`null`, blocked_by: runner)
7. **Add automated regression tests for validation runner** (`null`, blocked_by: runner)
8. **Integration test: run full validation against live environment** (`null`, blocked_by: smoke)
9. Encode Phase 1 findings and architecture decisions (`null`)
10. Form hypothesis: "Validation runner catches 80% of data regressions" (`/review-hypotheses`)
*Phase 2 planned (sessions 3-5): lifecycle validations, edge case coverage, performance benchmarks*
*Phase 3 planned (sessions 5+): cross-pipeline integration, user review, production deployment*
- *Initiative scope: phased delivery. Only Phase 1 goals generated now. Testing is ~40% of goals.*

## Step 5: Validate

```
1. Novelty check:
   For each candidate aspiration:
     Compare title + description against aspirations-read.sh --summary
     IF too similar to existing active aspiration → REJECT
     IF too similar to recently archived aspiration → REJECT (unless explicitly requested)

2. Cap check:
   Read core/config/aspirations.yaml → max_active
   current_count = count of active aspirations from Step 2
   new_count = count of candidate aspirations
   IF current_count + new_count > max_active:
     Remove lowest-priority/oldest aspirations first:
       IF aspiration has no completed goals and last_worked is null:
         Bash: aspirations-retire.sh <asp-id>   # never started
       ELSE:
         Bash: aspirations-complete.sh <asp-id>  # had progress
     Until within cap

3. ID assignment:
   Determine next asp-NNN ID from existing aspirations
   Assign sequential goal IDs: g-NNN-01, g-NNN-02, etc.
```

## Step 6: Create

```
For each validated aspiration:
  Build aspiration JSON with all goals
  Pipe to: echo '<aspiration-json>' | bash core/scripts/aspirations-add.sh
  IF exit code != 0: report validation error, skip this aspiration
```

## Step 7: Log

```
For each created aspiration:
  echo '{"date":"<today>","event":"aspiration_created","details":"<asp-id>: <title>","trigger_reason":"<from-user|from-self: phase>"}' | bash core/scripts/evolution-log-append.sh
```

## Step 8: Report

Output created aspirations with titles, goals, and rationale:

```
### Aspirations Created

**asp-NNN: [Title]** (priority: HIGH)
Motivation: [Why this serves the Self]
Goals:
  1. g-NNN-01: [Goal title] (skill: /research-topic)
  2. g-NNN-02: [Goal title] (skill: /review-hypotheses)
  ...
```

## Step 8.5: Notify User

After creating aspirations, notify the user so they have visibility into what the agent is autonomously building.

```
For each created aspiration:
  Build a concise summary: asp-id, title, motivation, goal count, goal titles
  Reach out to the user about the new aspiration:
    Subject: "New Aspiration Created: <asp-title>"
    Message: "<summary>"
  If unable to reach the user, create a participants: [user] goal to inform them. Do NOT block aspiration creation.
```

## Chaining

- **Called by**: `/start`, `/aspirations evolve` (`--plan` for gap analysis), `/aspirations loop` (Phase 0.5/2/7 with `--plan`, no-goals with `--plan`), `/aspirations-spark` (sq-007, sq-c05, sq-013 with context), `/aspirations-consolidate` (with `batch_context`), `/reflect-hypothesis` (with `forge_context`), `/respond`
- **Calls**: `aspirations-add.sh`, `aspirations-complete.sh`, `aspirations-retire.sh`, `evolution-log-append.sh`, user notification (Step 8.5)
- **Reads**: `<agent>/self.md`, `aspirations-read.sh`, `tree-read.sh`, `<agent>/developmental-stage.yaml`, `core/config/aspirations.yaml`
- **Web research** (`--plan` only): WebSearch for Self-grounded queries (Step 2.5)
