---
name: create-aspiration
description: "Creates new aspirations from three source modes — from-user (parse user text), from-self (autonomous gap-driven, optionally --plan for research-backed deliberation), and from-followup (fast-path: one aspiration with a single 'Design implementation plan and file sub-goals here' seed goal). USE WHENEVER you catch yourself saying 'I'll note this for later', 'worth investigating', 'deserves its own aspiration', 'follow up on', 'file as an idea', 'circle back', or 'noting for later' DURING goal execution — invoke from-followup IMMEDIATELY with the observation, do not defer to insights.jsonl (those accumulate unprocessed). ALSO USE when the user says 'create an aspiration', 'add a project', 'start a new effort', 'capture this', or 'file for later'. The from-followup path defaults to sprint scope + MEDIUM priority, auto-derives title, and skips introspective-scan + interestingness-filter + priority-review. Writes to aspirations.jsonl via aspirations-add.sh."
user-invocable: false
triggers: [aspiration, create-aspiration, aspiration-planning, new-aspiration, aspiration-generation, tactical-aspiration]
conventions: [aspirations, goal-schemas, tree-retrieval]
minimum_mode: assistant
revision_id: "skill-bootstrap-create-aspiration-0db17c"
previous_revision_id: null
---

# /create-aspiration — Self-Driven Aspiration Creation

Creates aspirations aligned with the agent's Self (core purpose). Three modes:

- **from-user**: Parse user's natural language into aspirations + goals
- **from-self**: Autonomous generation — "Given Self and current state, what's needed?"
  - **`--plan`**: Full planning cycle: introspective scan + Self-grounded web research + structured deliberation
- **from-followup**: Fast-path capture of in-flight observations that deserve dedicated follow-through. One aspiration with a single exploratory seed goal ("Design implementation plan and file sub-goals under this aspiration"). No introspective scan, no web research, no interestingness filter, no priority-review. Sprint scope, MEDIUM priority. Fires when the agent catches itself saying "I'll note this for later" during goal execution — invoke IMMEDIATELY with the observation text.

## Invocation Patterns

| Call Pattern | Behavior |
|-------------|----------|
| `from-self` (default) | 5-phase introspective scan (A, A.5, B, C, D). |
| `from-self --plan` | Full planning cycle: introspective scan + Self-grounded web research + structured deliberation |
| `from-self` with context params | Context is available — the LLM decides how much introspective scanning to add. No flag needed. |
| `from-followup "<text>"` | Fast-path: 1 aspiration + 1 exploratory seed goal. Skips Steps 1.5, 1.7, 2–8.6. Sprint scope + MEDIUM priority by default. Optional `--source-goal g-XXX-YY` stamps origin_signal with the goal being executed. Returns in under a second. |

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
- Agent self-observation during goal execution (Phase 4 — "I'll note this for later" / "worth investigating" trigger → `from-followup` fast-path)

## Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Step 1: Read Self

```
Read agents/<agent>/self.md → extract Self (body content after front matter)
IF agents/<agent>/self.md is empty or missing:
    ABORT: "Cannot create aspirations — Self is not defined. Run /start to set up."
```

## Step 1.2: Fast-Path — from-followup

**Purpose**: capture in-flight observations that deserve dedicated follow-through, without
derailing the current goal. No research, no deliberation, sub-500ms round-trip.

```
IF mode == "from-followup":
    Execute the fast-path below, then EXIT the skill.
    SKIP Steps 1.5, 1.7, 2, 3 (other modes), 4, 5, 7, 8.5, 8.6.
    Self was already read in Step 1 — use it for motivation alignment.

    Inputs:
      text           : required (positional, free-form observation)
      --source-goal  : optional (g-XXX-YY of the goal the agent was executing)

    Derive fields:
      title         = "Follow-up: " + first 60 chars of text (word-boundary cut)
      motivation    = first sentence of text (truncate to 200 chars at word boundary)
      description   = full text verbatim
                      IF --source-goal was passed: prepend "Source goal: <g-id>\n\n"
      category      = Bash: category-suggest.sh --text "<text>" --top 1
                      IF script returns empty array: "uncategorized"
      origin_signal = (MUST match a prefix in core/scripts/origin-signal-gate.py
                       ALLOWED_PREFIXES. Aspiration-level values aren't gate-checked
                       at add time, but downstream audits/reports consume this —
                       non-allowed values create forward-compat debt. Do not invent
                       new prefixes without extending ALLOWED_PREFIXES first.)
                      IF --source-goal provided: "idea:<source-goal-id>"
                      ELSE: "user_directive"

    ID assignment — OMIT the "id" field (g-328-29):
      # The daemon mints the next asp-NNN INSIDE the write lock (max+1
      # across live ∪ archive of the target queue). Do NOT pre-compute it —
      # client-side max+1 read outside the lock was the asp-334/asp-335
      # double-mint race (two agents filing concurrently read the same max).
      # Read the assigned id back from the response ("aspiration_id").

    Build aspiration JSON (pipe to aspirations-add.sh) — no "id", empty "goals":
      {
        "title": <derived>,
        "motivation": <derived>,
        "description": <derived>,
        "scope": "sprint",
        "priority": "MEDIUM",
        "source": "followup-capture",
        "origin_signal": <derived>,
        "sessions_active": 0,
        "status": "active",
        "goals": []
      }
      # goals MUST be [] here: the seed goal's origin_signal embeds the
      # minted asp id (parent_aspiration:<asp-id>), unknown until the
      # response arrives. Two-step filing; add-goal mints the goal id
      # in-lock too.

    Create + capture the minted id:
      Bash: echo '<aspiration-json>' | bash core/scripts/aspirations-add.sh
      # Response carries "aspiration_id": "asp-NNN" + "id_allocated": true.
      # Failures surface as-is. Do NOT auto-retry with --override-signal or
      # --override-duplication — the error text is the signal the caller needs
      # to see and act on.

    File the seed goal under the minted id (goal id minted server-side — omit "id"):
      Bash: echo '<seed-goal-json>' | bash core/scripts/aspirations-add-goal.sh <asp-id>
      where <seed-goal-json> =
      {
        "title": "Design implementation plan and file sub-goals under this aspiration",
        "description": "Review the motivation, outline the work lifecycle (research / build / test / integrate / encode as applicable), then EITHER file at least 2 concrete sub-goals under this aspiration OR archive the aspiration via aspirations-retire.sh as not-worth-pursuing. This seed goal forces a decision at pickup so from-followup aspirations cannot silently coast.",
        "skill": null,
        "type": "idea",
        "category": <same as aspiration>,
        "status": "pending",
        "priority": "MEDIUM",
        "origin_signal": "parent_aspiration:<asp-id>",
        "participants": ["agent"],
        "verification": {
          "outcomes": [
            "Implementation plan documented in the aspiration description (via aspirations-update.sh)",
            "At least 2 concrete sub-goals filed under this aspiration OR aspiration archived via aspirations-retire/complete as not-worth-pursuing"
          ],
          "checks": [],
          "preconditions": []
        },
        "blocked_by": []
      }

    Log:
      Bash: echo '{"date":"<today>","event":"aspiration_created","details":"<asp-id>: <title>","trigger_reason":"from-followup","origin_signal":"<origin_signal>"}' | bash core/scripts/evolution-log-append.sh

    Notify user (lean — single-line subject, no priority-review):
      Check world/forged-skills.yaml for a skill whose triggers match
      "notify the user" and invoke with:
        subject: "New follow-up: <title>"
        message: "<motivation>\n\nFiled as <asp-id> with 1 seed goal. Loop will pick up and decompose into sub-goals at next selection."

    Return: <asp-id> and g-NNN-01. Exit the skill.
Bash: echo "from-followup fast-path complete — <asp-id>"
```

**Why the seed goal's verification forces a decision**: `from-followup` captures are
cheap, which means the queue can fill with them. The "2 sub-goals OR archive" outcome
ensures every follow-up aspiration either proves its worth by decomposing into real
work or is honestly retired. Without this, `from-followup` becomes a landfill.

## Step 1.5: Execution Feedback Review

Before creating new goals, review feedback from agents who executed previous goals.
This creates a learning loop on goal quality — if past goals had low clarity or
wrong scope estimates, adjust the new ones accordingly.

```
Bash: board-read.sh --channel feedback --type execution-feedback --since 7d --json
IF feedback messages exist:
    Aggregate scores:
      avg_clarity = mean(feedback.clarity for all messages)
      avg_scope = mean(feedback.scope_accuracy for all messages)
      avg_verify = mean(feedback.verification_quality for all messages)
      high_friction_count = count where friction == "high"

    IF avg_clarity < 3.0:
        Output: "▸ FEEDBACK: Goal clarity averaging {avg_clarity}/5 — improving descriptions"
        # Apply: write more detailed descriptions with explicit prerequisites and context
    IF avg_scope < 3.0:
        Output: "▸ FEEDBACK: Scope accuracy averaging {avg_scope}/5 — calibrating estimates"
        # Apply: be more conservative on scope, break large goals into smaller ones
    IF avg_verify < 3.0:
        Output: "▸ FEEDBACK: Verification quality averaging {avg_verify}/5 — improving checks"
        # Apply: write more specific, testable verification criteria

    # Read any "notes" fields for specific improvement hints
    FOR EACH feedback WHERE notes is non-empty:
        Log pattern for this goal creation session
```

## Step 1.7: Team State Alignment

```
Bash: team-state-read.sh --field strategic_focus --json
IF strategic_focus.primary is set:
    Align new aspirations with team strategic focus where applicable
    Output: "▸ Team focus: {strategic_focus.primary}"
```

## Step 2: Read Current State

```
Bash: load-aspirations-compact.sh          → IF path returned: Read it (compact aspirations data — IDs, titles, statuses, priorities, categories, skills, recurring, participants, blocked_by, deferred, args, parent_goal, discovered_by, started — no descriptions/verification)
Bash: aspirations-read.sh --summary       → active aspirations only (one-liner per aspiration)
Bash: tree-read.sh --stats                → knowledge coverage
Read agents/<agent>/developmental-stage.yaml        → maturity level
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

Five-phase autonomous generation (Phases A-D + A.5 stepping stones):

```
Phase A.0 — Consolidation gate (hard block, from-self only):
    # Read portfolio-health data written by aspirations-precheck Phase 0.5.4.
    # The precheck writes {active_count, avg_completion, near_complete,
    # stalled, computed_at} to this WM slot via core/scripts/consolidation-
    # health.sh every iteration. Absent slot = treat as pass-through (fail
    # open) — the loop may be running in a context without precheck (e.g.
    # direct /aspirations evolve invocation).
    # wm-read.sh emits `null` + exit 0 when the slot is absent — no
    # fallback needed. The downstream `!= "null"` guard fails open, so
    # missing-slot passes through without blocking.
    Bash: health_json = wm-read.sh consolidation_health --json
    IF health_json != "null" AND health_json is a well-formed object:
        active_count = health_json.active_count (default 0)
        avg_completion = health_json.avg_completion (default 1.0)
        IF active_count >= 6 AND avg_completion < 0.35:
            # Portfolio is fragmented. Refuse to create a new from-self
            # aspiration; this would violate consolidate-before-expand.
            # User-directed paths (from-user via /respond) are exempt because
            # they don't route through this skill's from-self branch — they
            # call the from-user mode path above, which has its own logic.
            Log refusal message to stderr:
              "▸ CONSOLIDATION GATE: refusing from-self aspiration creation
               (active_count={N}, avg_completion={X:.2%}, thresholds=6/35%).
               Drain the existing tail before adding new work. Override by
               passing --override-consolidation-gate '<justification>', or
               request this work via /respond (from-user path is exempt)."

            # Audit trail — every gate fire is logged with the live thresholds
            # and the portfolio snapshot that triggered it. Reviewable via
            # meta/config-changes.yaml.
            Bash: echo '{"date":"<today>","event":"consolidation_gate_fired","details":"from-self refused: active_count='$active_count', avg_completion='$avg_completion'","thresholds":"active_count>=6 AND avg_completion<0.35"}' | bash core/scripts/evolution-log-append.sh

            # Instead of creating, route to Unblock/Complete-Review work so
            # the existing tail gets attention. Create a HIGH-priority Unblock
            # goal in the most stale active aspiration (highest completion but
            # has unfinished blocked goals — likely a zombie candidate).
            Bash: aspirations-add-goal.sh --source world ... (HIGH-priority Unblock goal
              titled "Unblock: drain tail to close aspiration — {asp-id}"
              targeting the most completion-ready stale aspiration)

            # Return early — the caller (sq-007 handler, evolve gap-analysis,
            # idle playbook) observes NO new aspiration was created. The
            # next loop iteration runs aspirations-precheck Phase 0.5.0a
            # zombie scan, which routes to aspirations-complete-review.
            RETURN {created: 0, gate: "consolidation", action: "routed_to_unblock"}

    # Override path: --override-consolidation-gate "<justification>" bypasses
    # the block but logs the override for audit.
    IF --override-consolidation-gate "<justification>" was passed:
        Log: "▸ Consolidation gate overridden: {justification}"
        Bash: echo '{"date":"<today>","event":"consolidation_gate_override","details":"from-self override: '$justification'","thresholds":"active_count>=6 AND avg_completion<0.35"}' | bash core/scripts/evolution-log-append.sh
        (proceed to Phase A.evidence)

Phase A.evidence — Evidence-citation requirement (from-self only, governs ALL phases A-D):

    **Mandatory rule for every candidate aspiration generated in Phases A,
    A.5, B, C, D below.** Before adding a candidate to the pool, write
    one sentence of the form:

        "The user will see value from this because <specific evidence>."

    `<specific evidence>` must reference ONE of:
      - A named file, test, metric, or deploy artifact that is currently
        broken, stale, or under-performing (cite the path)
      - A specific coordination or findings board post (cite the post id)
      - A specific pending-question (cite the pq-NNN id)
      - A specific user statement from the last 30 days of journal entries
        (cite the journal entry or session id)
      - A specific resolved hypothesis whose outcome implies follow-up
        work (cite the h-YYYY-MM-DD-slug id)
      - A specific low-confidence tree node with recent retrieval hits
        (cite the node path)

    **Category labels are NOT evidence.** A candidate whose evidence reduces
    to "the category 'research' has low coverage" or "Self mentions code
    quality, so test coverage" is REJECTED at this phase. The B2.5
    idle-playbook used exactly that pattern and produced session after
    session of make-work; this gate prevents the same failure mode at the
    aspiration level.

    If you cannot write a specific evidence sentence for a candidate,
    DROP the candidate. An empty pool at the end of Phase D is an
    acceptable outcome — Step 6 and downstream handlers can act on an
    empty result. Do NOT backfill with generic candidates to avoid
    returning empty; that IS the failure mode.

    Every surviving candidate carries its evidence sentence forward as
    the aspiration's `motivation` field (or is prefixed to it). This makes
    the cited evidence auditable in aspirations.jsonl — a reader can
    trace any agent-generated aspiration back to the concrete signal that
    produced it.

    When a child goal of this aspiration reaches `aspirations-add-goal.sh`,
    set `origin_signal` to `parent_aspiration:<parent-asp-id>` so the
    origin-signal gate (core/scripts/origin-signal-gate.py) accepts it —
    the parent aspiration's evidence transitively licenses its goals.
    (Works for both from-user and from-self parents — the name is
    semantically parent-centric, not source-centric.)

Phase A — Purpose scan:
    "What does Self need that isn't covered by current aspirations?"
    Read Self carefully. Consider:
      - What capabilities does Self imply the agent should have?
      - What ongoing activities does Self suggest?
      - What would a person with this purpose naturally prioritize?
    Generate aspirations that advance Self's mission.

    # Meta-strategy aspiration heuristics
    Bash: meta-read.sh aspiration-generation-strategy.yaml
    # Apply agent-learned generation preferences:
    # - preferred_scope_ratio: learned balance of sprint/project/initiative
    # - category_saturation: categories already well-covered
    # - generation_heuristics: learned rules for aspiration generation
    # - stepping_stone_preferences: whether to use stepping stones, K value
    # - interestingness_criteria: weights for novelty/learnability/worthwhileness/diversity
    # - domain_class_targets: per-class portfolio steering (asp-244 E1, g-001-196).
    #   For each candidate aspiration whose primary domain class can be inferred,
    #   invoke the bash-enforced gate (replaces prior LLM-advisory pseudocode —
    #   rb-616 / rb-428 family: recurring portfolio decisions drift when left to
    #   per-cycle judgment; bash gate makes the threshold math the SSOT):
    Bash: bash core/scripts/domain-class-gate.sh check --candidate-class <class>
    #   The gate emits structured JSON. Consume per the `action` field:
    #     - "warn": current_ratio has reached/exceeded the class's max. Candidate
    #       may still be added, but justification MUST be logged. The gate
    #       returns log_payload pre-filled with {date, event, class, current_ratio,
    #       max, reason: null}; fill the `reason` field with a one-line
    #       justification, then:
    Bash: bash core/scripts/domain-class-gate.sh log --payload '<payload-with-reason>'
    #     - "bias": class is below min. Surface preferentially in Phases A-D
    #       (additive, not exclusive — other classes still eligible). The gate
    #       also returns `bias_below_classes`: a list of every under-min class
    #       across the portfolio. Bias extends to candidates in any of them, not
    #       just the queried class.
    #     - "none": no gating — proceed normally.
    #   Fail-open: missing strategy / broken learning-ratio / YAML errors all
    #   return action="none" with `fail_open_reason` populated. The gate's job
    #   is to catch portfolio drift, not block aspiration generation when its
    #   dependencies break.

    # Constraint awareness
    Bash: wm-read.sh active_constraints --json && Bash: wm-read.sh known_blockers --json
    If active constraints or unresolved blockers exist:
      - Avoid generating goals that require blocked resources/skills
      - Prefer goals executable with currently available infrastructure
      - If constraint_context was passed from evolve: strictly exclude avoid_skills

    # Interestingness criteria (OMNI-EPIC-inspired, Stage 1: generation-time)
    # When generating candidates in ALL phases (A-D), explicitly consider:
    #   NOVELTY:        How different is this from what we've done before?
    #   LEARNABILITY:   Is this within reach given current capabilities?
    #                   (not so easy it's trivial, not so hard it'll fail)
    #   WORTHWHILENESS: Does this advance Self's purpose in a meaningful way?
    #   DIVERSITY:      Does this explore a different category/approach than recent work?
    # These criteria shape generation — the post-generation filter (Step 5) evaluates them.

Phase A.5 — Stepping-stone retrieval (OMNI-EPIC-inspired):
    # Key insight from OMNI-EPIC (arXiv 2405.15568): nearby completed work
    # is the best creative substrate for generating novel directions.
    # Retrieve similar archived aspirations as "stepping stones" — not to
    # repeat them, but to riff off them.

    Bash: aspirations-read.sh --stepping-stones --limit 5
    # Returns: K=5 most recently completed aspirations with their:
    #   - title, motivation, scope, tags
    #   - goal titles + statuses + categories (what was attempted)
    #   - completion counts (what was learned)

    # Partial archive visibility (OMNI-EPIC: showing everything constrains
    # creativity). The script returns only K aspirations, not the full archive.
    # The LLM sees enough context to build on, but not so much that it constrains.

    IF stepping stones returned (archive is non-empty):
      For each stepping stone, ask:
        "What ADJACENT direction does this completed work suggest?
         What variation, extension, or contrast would be novel and learnable?
         What follow-up question does this work raise that wasn't answered?
         What in this completed work is fragile or could break under edge cases?
         What assumptions were made that haven't been validated?"

      Generate stepping-stone candidates from these reflections.
      # These candidates join the pool alongside Phases A-D candidates.
      # They are additive, NOT replacements for introspective scanning.

    IF archive is empty (new agent, no completed aspirations yet):
      SKIP — no stepping stones available. Phases A-D provide sufficient generation.

Phase B — Data acquisition scan:
    Bash: world-cat.sh knowledge/tree/_tree.yaml  # node summaries + entity_index
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
    Read agents/<agent>/experiential-index.yaml       → categories with declining accuracy

    For each pain signal found:
    - Unresolved blockers → blocker_resolution goals
    - Repeated skill failures → diagnosis goals
    - Declining category accuracy → research or diagnosis goals
    - Stale/contradicted knowledge → cleanup goals

    If pain requires resolution:
      Apply Capability Check Before User Routing (.claude/rules/capability-before-user.md):
      check skill registry, forged skills, companion scripts, provisionability,
      and domain convention (world/conventions/capability-routing.md) before
      assigning participants. ONLY genuinely human-only actions get
      participants: [user]. If BOTH agent and human work needed: [agent, user].
    Notify the user about the urgent pain.
    (Check world/forged-skills.yaml for a skill whose triggers match
    "notify the user" and invoke it with a short subject ("Urgent pain: <one-line>")
    and message (the pain signals and proposed goals). If no matching skill is
    registered, fall back to a `participants: [agent, user]` goal via
    aspirations-add-goal.sh. Don't just queue silently. Never block on
    notification failure.)

    Pain-driven aspirations default to priority: HIGH (they're blocking real work).
    Skip if no pain signals found — not every session has problems.
```

**Phase D.5 — Consolidation scan** ("What existing work could be made excellent?"):
```
    Bash: load-aspirations-compact.sh → IF path returned: Read it
    FOR EACH active aspiration, compute completion_ratio (completed / total active goals):
      IF completion_ratio > 0.50 AND < 1.0:
        "What remaining goals need attention? What quality gaps exist?
         What would make this aspiration's outcomes robust and durable?
         What was done hastily that deserves a careful second pass?"
      IF completion_ratio < 0.30 AND sessions_active > 2:
        "Is this aspiration stuck? What's blocking progress?
         Should goals be rescoped, dependencies resolved, or approach changed?"
    Generate consolidation-focused candidates for existing aspirations.
    These are ALTERNATIVES to new aspirations — prefer strengthening what
    exists over starting something new.
    Skip if all aspirations are either very new (<2 sessions) or nearly done (>90%).
```

```
Phase E — World observation ("What has the world shown me?"):
    # Unlike Phases A-D which ask "what does Self need?" (introspective, exhaustible),
    # Phase E asks "what has the world TOLD me that I haven't acted on?" (observational,
    # inexhaustible). Self is finite. The world is not. This phase reads dynamic state
    # that refreshes every iteration — it can never run dry.

    # E1: Strategic scan signals (if available in working memory)
    Bash: wm-read.sh strategic_scan_signals --json
    IF strategic_scan_signals is not null and len > 0:
        For each signal, generate aspiration candidates that ADDRESS the signal:
          - regression → investigation + fix aspiration
          - anomaly → research + understanding aspiration
          - stale_knowledge → refresh + deepening aspiration
          - thin_knowledge → exploration aspiration
          - unexplored_territory → discovery aspiration
          - uncovered_priorities → purpose-advancing aspiration
          - cross_pollination → transfer learning aspiration

    # E2: Recent experience themes (cross-goal pattern detection)
    # Look for themes across the last N goal executions — not just one goal.
    Bash: experience-read.sh --recent 10
    recent_experiences = parse result
    IF recent_experiences:
        Cluster by category/theme. Ask: "What theme is emerging from
        recent work that deserves its own aspiration? What question
        keeps coming up? What concern keeps appearing across goals?"
        Generate aspiration candidates from emerging themes.

    # E3: Caller-provided scan context (from strategic scan medium signals)
    IF scan_context was passed by caller:
        For each signal in scan_context:
            Generate aspiration candidates directly from signal data.
            These are higher-confidence candidates (already triaged as MEDIUM severity).

    # E4: Curiosity-driven generation (intrinsic motivation)
    # This is pure intrinsic motivation — not reactive to problems, not derived
    # from gaps, just genuine intellectual curiosity about the domain.
    Read agents/<agent>/self.md
    Ask: "Given Self and everything I now know, what am I genuinely
          CURIOUS about? What would be fascinating to explore?
          What counterintuitive hypothesis would I love to test?
          What adjacent domain might illuminate mine?"
    Generate 1-2 curiosity-driven aspiration candidates.
    Tag these as source: "curiosity" for tracking.
```

Combine results from all seven phases (A, A.5, B, C, D, D.5, E). Deduplicate and prioritize.

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

Read agents/<agent>/self.md → what are Self's priorities, curiosities, blind spots?
Bash: world-cat.sh knowledge/tree/_tree.yaml  # where are the knowledge gaps?

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
Read agents/<agent>/self.md (explicit re-read — Self anchors the deliberation)

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
Read agents/<agent>/self.md → what domain is this agent for?
Bash: world-cat.sh knowledge/tree/_tree.yaml  # what does the agent already know?
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

6. COORDINATION MODE (multi-agent — Amdahl's Law awareness):
   Classify this aspiration's parallelizability:
   - "parallel": Goals are largely independent — multiple agents can work on
     different goals simultaneously without blocking each other. Example:
     research on 3 unrelated topics, independent bug fixes in different modules.
   - "serial": Goals form a dependency chain — each depends on the previous
     one's output. Example: research → design → implement → test → deploy.
   - "mixed" (default): Some goals are independent, some are chained.
     The blocked_by graph determines which. Example: research is parallel,
     but implementation depends on research completing first.
   Set coordination_mode on the aspiration JSON accordingly.
   (Informed by "LLM Teams as Distributed Systems" Finding 1: speedup is
   bounded by the parallelizable fraction — Amdahl's Law.)
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
  - origin_signal: "parent_aspiration:<this-asp-id>"
    # REQUIRED. The origin-signal gate (core/scripts/origin-signal-gate.py)
    # rejects goals without this field at aspirations-add.sh time. The
    # parent aspiration's own origin_signal (evidence cited in Phase A.evidence
    # or "user_directive" for from-user) transitively licenses its goals.
  - verification:
      outcomes: ["Human-readable success criteria"]
      checks: [{type, target, condition}]
      preconditions: ["What must be true before execution"]
  - blocked_by: [goal-ids] if sequential dependency
```

### Step 4b.5: Dependency Analysis (multi-agent coordination)

For each generated goal, improve delegation quality for multi-agent execution:

1. **Populate `blocked_by` explicitly**: Review all goals — does goal B require output
   from goal A? Add `A.id` to `B.blocked_by`. Prefer over-specifying dependencies
   (safe ordering) to under-specifying (race conditions).

2. **File path annotations** (code-change goals only): Include a `Touches:` line at the
   start of the description listing files the goal will modify. This enables other agents
   to detect potential conflicts when scanning goals.
   ```
   description: |
     Touches: src/main/java/FooVerticle.java, src/test/java/FooTest.java

     Implement the new endpoint for ...
   ```

3. **Route via `participants`**: If a goal is clearly developer work (code changes,
   deployments), set `participants: ["alpha"]`. If it's PM work (audits, research,
   monitoring), set `participants: ["bravo"]`. Default `["agent"]` for either.

(Informed by CAID finding: delegation quality is the #1 factor in multi-agent success.
Explicit dependency chains prevent agents from executing work out of order.)

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

## Step 5: Interestingness Filter + Validate (OMNI-EPIC two-stage)

The original novelty check is replaced by a two-stage Model of Interestingness
inspired by OMNI-EPIC (arXiv 2405.15568). Stage 1 (generation-time) shapes
candidate generation in Phases A-D via interestingness criteria. Stage 2
(post-generation) evaluates candidates against the archive.

```
1. Interestingness filter (Stage 2 — post-generation evaluation):
   # Stage 1 already ran during Phases A-D (interestingness criteria in prompting).
   # Stage 2 compares candidates against the archive for structured evaluation.

   # Use stepping stones already retrieved in Phase A.5 (avoid re-fetching)
   # If Phase A.5 was skipped (empty archive), use aspirations-read.sh --summary instead

   For each candidate aspiration:
     a. Identify the 3 most similar items from stepping stones or active aspirations
        (by category overlap, title similarity, domain affinity)

     b. Evaluate against interestingness criteria:
        NOVEL:       Is this sufficiently different from the 3 most similar?
                     (different angle, different scope, different approach)
        LEARNABLE:   Given developmental stage + tree coverage, can we make progress?
                     (not so easy it's trivial, not so hard it'll fail)
        WORTHWHILE:  Does Self endorse this direction? (re-read Self as "judge")
                     Improvement aspirations that deepen existing work score HIGH here.
                     "Improve X" and "Harden X" are worthwhile — do not penalize for
                     similarity to existing work. Similarity is the point for improvements.
        NON-TRIVIAL: Would completing this teach something non-obvious?

     c. Score: ACCEPT (interesting — proceed to cap check)
              REFINE (good direction but needs sharpening — attempt to sharpen)
              REJECT (redundant, trivial, or unlearnable — drop from candidates)

     d. For REFINE candidates: attempt to sharpen the aspiration
        (more specific scope, different angle, harder variant, adjacent problem)
        Re-evaluate sharpened version. If still REFINE → REJECT.

     e. Log filter decisions to evolution-log for meta-learning:
        echo '{"date":"<today>","event":"interestingness_filter","details":"<title>: <verdict> — <reason>","trigger_reason":"create-aspiration"}' | bash core/scripts/evolution-log-append.sh

   # Reject if DUPLICATING existing work (same scope, same approach, same goals).
   # Do NOT reject aspirations that DEEPEN, IMPROVE, or HARDEN existing work —
   # "Improve X" is intentionally similar to "X" and that is correct.
   Compare accepted candidates against aspirations-read.sh --summary
   IF duplicates existing active aspiration (same scope + approach) → REJECT
   IF deepens/improves existing active aspiration → KEEP (consolidate-before-expand)

1.5. Cross-store contradiction check (G7 / R9):
   Title-matching against active aspirations catches obvious duplicates but
   misses contradictions encoded in reasoning-bank or guardrails — e.g.,
   "we already tried this approach and it failed for reason X" or
   "guardrail Y forbids this class of work right now". Per
   `.claude/rules/retrieve-before-deciding.md` decision point 5 ("adding a
   new aspiration") and `.claude/rules/consolidate-before-expand.md`.

   For each ACCEPTED candidate:
     Bash: retrieve.sh --category "{candidate.category} {candidate.title}" --depth shallow

     From the returned JSON, evaluate:
       - guardrails[] whose rule constrains this category right now
         (e.g., "no new X aspirations while blocker Y exists")
         → If a guardrail blocks: REJECT, log the guard-NNN ID
       - reasoning_bank[] entries describing failed attempts at this aspiration
         shape in the last 30 days (look for failure_lesson + same category)
         → If 2+ recent failures: DOWNGRADE priority by one level
         (HIGH→MEDIUM, MEDIUM→LOW); add a precondition goal that addresses
         the failure pattern before the main work
       - reasoning_bank[] entries that describe a SIMPLER alternative that
         would achieve the same outcome
         → If a simpler alternative is named: REFINE the aspiration to use
         that approach instead of the originally generated one

     Log filter decisions to evolution-log:
       echo '{"date":"<today>","event":"crossstore_contradiction_check","details":"<title>: <verdict> — guards=<count> rb=<count>","trigger_reason":"create-aspiration"}' | bash core/scripts/evolution-log-append.sh

     Fail-open: if retrieve.sh errors, log and proceed with the candidate
     unchanged. A retrieval failure must not block aspiration creation.

2. Cap check:
   Read core/config/aspirations.yaml → max_active
   current_count = count of active aspirations from Step 2
   new_count = count of ACCEPTED candidate aspirations
   IF current_count + new_count > max_active:
     Remove lowest-priority/oldest aspirations first:
       # Guard: NEVER archive aspirations with recurring goals (data layer blocks it)
       IF any goal has recurring == true: SKIP this aspiration
       ELIF aspiration has no completed goals and last_worked is null:
         Bash: aspirations-retire.sh --source {asp.source} <asp-id>   # never started
       ELSE:
         Bash: aspirations-complete.sh --source {asp.source} <asp-id>  # had progress
     Until within cap

3. ID assignment (g-328-29 — server-side, in-lock):
   OMIT "id" on the aspiration AND on every embedded goal. The daemon mints
   asp-NNN inside the write lock (max+1 across active ∪ archive) and goal
   ids g-NNN-01.. in array order; read them back from the response
   ("aspiration_id" + "aspiration".goals[].id).
   # Client-side max+1 was the asp-334/asp-335 double-mint race — two
   # agents filing concurrently read the same max. Explicit ids remain
   # supported for transplant/migration callers only.
   IF goals need intra-record blocked_by references (g-NNN-02 waits on
   g-NNN-01): file the aspiration with "goals": [], then add goals one at
   a time via aspirations-add-goal.sh (which also mints in-lock), wiring
   blocked_by to the ids read back from each response.
```

## Step 6: Create

```
For each validated aspiration:
  Build aspiration JSON with all goals
  Pipe to: echo '<aspiration-json>' | bash core/scripts/aspirations-add.sh
  IF exit code != 0: report validation error, skip this aspiration
```

**Goal-duplication gate (asp-248 / g-248-01)** fires automatically inside
`aspirations.py cmd_add` — for each goal in the aspiration, it cross-checks
against (1) partner's team-state `recent_completions`, (2) partner's git
commits in last 48h, (3) active `insight_trigger` findings on the board.
If ANY goal overlaps with partner work, the whole aspiration is blocked.
Pass `--override-duplication "<justification>"` to `aspirations-add.sh`
when the overlap is intentional (e.g. different scope on the same file);
the override is audited to `world/goal-duplication-overrides.jsonl`.

## Step 7: Log

```
For each created aspiration, emit one evolution-log event. The `origin_signal`
field carries forward the evidence cited in Phase A.evidence — or `user_directive`
for from-user mode. Downstream audits (`/backlog-report`, `/agent-completion-report`)
surface the distribution of origin_signal values so `idle_fallback` and similar
soft signals are visible rather than hidden in the goal stream.

  origin_signal_value:
    - from-user mode                → "user_directive"
    - from-self with cited evidence → the evidence token (e.g. "board_post:f-218",
                                      "failing_test:widget-task-001",
                                      "pending_question:pq-042", "resolved_hypothesis:h-...")
    - from-self, no evidence        → this should not happen; Phase A.evidence
                                      drops candidates rather than backfill.

  echo '{"date":"<today>","event":"aspiration_created","details":"<asp-id>: <title>","trigger_reason":"<from-user|from-self: phase>","origin_signal":"<origin_signal_value>"}' | bash core/scripts/evolution-log-append.sh
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
  Notify the user about the new aspiration.
  (Check world/forged-skills.yaml for a skill whose triggers match
  "notify the user" and invoke it with:
    subject: "New Aspiration Created: <asp-title>"
    message: "<summary>"
  If no matching skill is registered, fall back to a `participants: [agent, user]`
  goal via aspirations-add-goal.sh. Never block aspiration creation on
  notification failure.)
```

## Step 8.6: Priority Review Request (from-self only)

After notifying about individual aspirations (Step 8.5), present the full priority picture
so the user can reorder if needed. Only fires for `from-self` mode — `from-user` aspirations
already reflect the user's intent.

IF invocation mode is NOT "from-self": SKIP this step.

```
1. Build ranked list of ALL active aspirations:
   Bash: aspirations-read.sh --active-compact
   Sort by: priority (HIGH→MEDIUM→LOW), then aspiration ID
   Format each as: "[{priority}] {asp-id}: {title} ({completed}/{total} goals)"

2. Write priority-review pending question to agents/<agent>/session/pending-questions.yaml:
   - id: pq-NNN (next available)
   - date: "{today}"
   - context: "priority-review"
   - question: |
       I just created new aspirations. Current priority ranking:
       {numbered ranked list}
       NEW this batch: {list of newly created asp-ids and titles}
       Does this match your priorities? Say which should be higher/lower,
       or "looks good" to confirm. You can also run /priority-review.
   - default_action: "Proceeding with current ranking"
   - status: pending
   - type: priority-review

3. Notify the user (via forged notification skill if available, else pending-questions):
   category: decision-needed
   subject: "Priority Review: {N} Active Aspirations ({M} new)"
   body: the ranked list + "Reply or run /priority-review to reorder"

4. Continue immediately — do NOT block.
Bash: echo "create-aspiration phase documented"
```

## Chaining

- **Called by**: `/start`, `/aspirations evolve` (`--plan` for gap analysis), `/aspirations loop` (Phase 0.5/2/7 with `--plan`, no-goals with `--plan`), `/aspirations-spark` (sq-007, sq-c05, sq-013 with context), `/aspirations-consolidate` (with `batch_context`), `/reflect-on-outcome` (with `forge_context`), `/respond`
- **Calls**: `aspirations-add.sh`, `aspirations-complete.sh`, `aspirations-retire.sh`, `evolution-log-append.sh`, user notification (Step 8.5), priority-review pending question (Step 8.6)
- **Reads**: `agents/<agent>/self.md`, `aspirations-read.sh`, `tree-read.sh`, `agents/<agent>/developmental-stage.yaml`, `core/config/aspirations.yaml`
- **Web research** (`--plan` only): WebSearch for Self-grounded queries (Step 2.5)

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The pending-question write (Step 8.6) and notification call are tool calls and satisfy
this when present. If neither fires, end with `Bash: echo "create-aspiration complete"`.
