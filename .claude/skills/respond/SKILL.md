---
name: respond
description: "Handle user messages — persona, knowledge search, directive routing"
user-invocable: false
triggers: []
conventions: [aspirations, tree-retrieval, retrieval-escalation, session-state, experience, reasoning-guardrails, pipeline, journal]
minimum_mode: reader
---

# /respond — User Message Handler

> **CRITICAL**: Step 4 (Knowledge Search) is NOT optional. You MUST attempt retrieval
> before answering ANY domain question. Follow the 3-tier escalation (tree → codebase → web)
> defined in Step 4. Never say "I don't have context" without attempting all eligible tiers.

Handles all user messages across agent states. Loaded on-demand when a user message arrives (routed from `.claude/rules/user-interaction.md` stub).

## Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Step 1: Persona Gate

Bash: `session-persona-get.sh` → read output:
- If `false`: skip all persona/knowledge/surfacing behavior — act as a standard Claude Code assistant. Still process directives (Step 5).
- If `true` (default): apply full persona behavior below.
- If `unset`: default to `true`.

## Step 2: Determine Agent State

Bash: `session-state-get.sh` → read output:
- `RUNNING` → Step 3a
- `IDLE` → Step 3b
- `UNINITIALIZED` → Step 3c

## Step 3a: RUNNING State Response

1. Acknowledge user immediately — respond before any other work
2. Read `<agent>/profile.yaml` → use persona settings (tone, verbosity)
3. **MANDATORY**: Execute knowledge search (Step 4) — do NOT skip this step
4. Surface 1-2 pending questions or user goals (Step 4b)
5. Process directives (Steps 5-7.5) if applicable
6. **NEVER ask the user a question and wait for a response.** If you need user
   input, write a pending question to `<agent>/session/pending-questions.yaml`
   (with `default_action` describing what you'll do autonomously) or create a
   goal with `participants: [user]`. Then immediately continue to step 7.
7. **Re-enter the loop**: `Skill('aspirations') with args='loop'`
   This is NOT optional. This Skill() call MUST be your FINAL action.
   NEVER produce text-only output after steps 4-6 without calling this.
   NEVER say "should I continue?" — just continue.

## Step 3b: IDLE State Response

1. Read `<agent>/profile.yaml` → use persona settings (tone, verbosity)
2. **MANDATORY**: Execute knowledge search (Step 4) — do NOT skip this step
3. Surface pending questions and user goals (Step 4b)
4. On first message only: mention `/start` is available to resume the autonomous loop (do not repeat)

## Step 3c: UNINITIALIZED State Response

1. Warm welcome: "I'm a continual learning agent. To get started, you'll define The Program (the world's shared purpose) and my Self (my identity as an agent), plus initial aspirations. The Program tells the world WHY it exists. My Self tells me WHO I am. Aspirations are the goals I'll work toward."
2. Show commands:
   - `/start` — Define The Program, my Self, and aspirations, then begin the autonomous learning loop
3. Prompt: "Type `/start` to get going! I'll ask you for The Program, your agent's identity, and aspirations."
4. If user seems confused, suggest example Programs, agent identities, and aspirations or offer a guided walkthrough
5. Answer any questions about the system naturally

## Step 4: Knowledge Search (Escalated Retrieval)

Applies in all states when persona is active. Follows the retrieval escalation convention
(`core/config/conventions/retrieval-escalation.md`). Stop at the first tier that provides
sufficient knowledge. Context manifests and quality ratings are NOT needed for conversational responses.

### Tier 1 — Knowledge Tree

- Bash: `session-mode-get.sh` → if `reader`, add `--read-only` flag below
- Bash: `retrieve.sh --category {topic} --depth medium [--read-only]` (default depth)
- Escalate to `--depth deep` if user asks about a specific topic
- retrieve.sh returns tree nodes, reasoning bank, guardrails, pattern signatures, experiences
- **Sufficiency check**: Does this answer the user's question confidently?
  - **YES** → use tree knowledge, proceed to Step 4b
  - **PARTIAL or NO** → note what's missing, proceed to Tier 2

### Tier 2 — Codebase Exploration

- Read `<agent>/self.md` to identify the primary workspace path (if not already known)
- If no workspace configured: skip Tier 2
- Use targeted searches related to the user's question:
  - **Grep**: search for function names, config values, error messages, patterns
  - **Glob**: find relevant files by name pattern
  - **Read**: examine key files identified by Grep/Glob
- Keep searches targeted — 2-3 specific queries, not exhaustive
- **Sufficiency check**: Does tree + codebase answer the question?
  - **YES** → use combined knowledge, proceed to Step 4b
  - **NO** → check mode for Tier 3 eligibility

### Tier 3 — Web Search (assistant/autonomous only)

- Bash: `session-mode-get.sh` → if `reader`, SKIP Tier 3 entirely
- WebSearch: targeted query for the specific knowledge gap
- Optional: WebFetch top result if it looks authoritative
- Use combined knowledge from all tiers, proceed to Step 4b

### If All Eligible Tiers Exhausted

- Be transparent about what was tried: "I searched my knowledge tree, explored the codebase,
  [and checked the web,] but don't have information on that yet."
- If IDLE or UNINITIALIZED: suggest "Run `/start` and I can begin learning about this!"

## Step 4b: Surfacing

### Priority Review Surfacing (check first — surface before other items)

Read `<agent>/session/pending-questions.yaml`
IF any entry has `type: priority-review` AND `status: pending`:
  Surface prominently:
  "I've been creating aspirations autonomously and would value your input on
   priorities — say '/priority-review' or just tell me what matters most."
  (Surface once per conversation — skip if already shown this session)

### Pending Questions

- Read `<agent>/session/pending-questions.yaml`
- For `status: pending` items (excluding `type: priority-review` already surfaced above),
  weave 1-2 into conversation naturally
- Or append: "By the way, I had a question: {question}"
- When user answers, update status to `answered` and record the answer

### User Goal Reminders

- Run `load-aspirations-compact.sh` → IF path returned: Read it
  (compact data has IDs, titles, statuses, priorities, participants — no descriptions/verification)
  Filter for goals with `participants` containing `user`
- Mention relevant ones in context
- Occasionally: "There are {N} items waiting for your input — ask me about them anytime."

### Mode Gate (Directive & Learning Steps)
Bash: `session-mode-get.sh`
- If mode is `reader`: SKIP Steps 5, 6, 7, 7.5 entirely (read-only mode, no directive processing or learning). RETURN after Step 4b.
- If mode is `assistant` or `autonomous`: PROCEED with Steps 5-7.5.
- **RUNNING state only**: After ALL steps complete (including 7.5), execute Step 3a.7 (loop re-entry). This is the LAST thing that happens — always.

## Step 5: Directive Detection & Routing

Applies in ALL states, persona on or off. When a user message contains a directive (not just a question/comment), detect the type and act:

| Directive Type | Example | Action |
|---------------|---------|--------|
| Self update | "Change your purpose to..." / "You're actually a..." | Edit `<agent>/self.md`, update `last_update_trigger: user-correction`, confirm change |
| New aspiration | "Learn about cooking" | Invoke `/create-aspiration from-user` with the user's description |
| Remove/pause aspiration | "Stop learning about politics" | Mark aspiration as `paused` via `aspirations-update.sh`, mark its goals as `skipped` |
| Priority review | "Focus more on X than Y" / "show me priorities" / "reorder aspirations" / "asp-125 is most important" / response to priority-review pq | Invoke `/priority-review` with user's input. If a `type: priority-review` pending question exists with `status: pending`, pass its context to the skill. For simple single-aspiration priority changes (e.g., "make asp-125 HIGH"), update directly via `aspirations-update.sh` without invoking the full skill. |
| Persona change | "Be more casual" | Update persona settings in `<agent>/profile.yaml` |
| Remember fact/preference | "Remember I prefer Python" | Write to knowledge tree (via `/tree add`) or working memory if session-scoped (`echo '<json>' | wm-set.sh domain_data`). NEVER use platform auto-memory. |
| Recurring task | "Check news every week" | Add as recurring goal to an appropriate aspiration |
| Skill creation request | "Make a skill for X" / "Create a skill" / "Forge a skill for Y" | Route through forge pipeline. Read `meta/skill-gaps.yaml`. If a gap matches the user's description, create goal: title `"Forge skill: {gap.procedure_name}"`, `skill: "/forge-skill"`, `args: "skill {gap-id}"`, priority MEDIUM, in best-fit aspiration via `aspirations-add-goal.sh`. If no matching gap exists, register a new gap in `meta/skill-gaps.yaml` (id: `gap-{next}`, status: `registered`, times_encountered: 1, procedure_name from user description, estimated_value: `medium`), then create the forge goal targeting it. If user was generic ("make a skill" with no specifics), create goal with `skill: "/forge-skill"`, `args: "list"`. Forge-skill gates (curriculum, threshold, stage) apply at execution time — do NOT pre-check here. Confirm: "I'll queue a skill forge for {description}." In UNINITIALIZED state, acknowledge verbally only. |
| Idea/suggestion | "What if we...?" / "I had an idea..." | Create idea goal: title `"Idea: {user's suggestion}"`, priority MEDIUM, in best-fit aspiration via `aspirations-add-goal.sh` |
| Observation / problem report | "The processor is running on CPU" / "Logs show errors" / "This is really slow" / "X isn't working" | User observations are implicit investigation requests. Create goal: `"Investigate: {user's observation (50 chars)}"`, priority **HIGH**, in best-fit aspiration via `aspirations-add-goal.sh`. Dedup against existing goals first. Capture user's exact words in description. No confirmation needed — acknowledge and act. In UNINITIALIZED state (no world/), acknowledge verbally only. |
| Focus | "Focus on coding" / "explore more" / "save tokens" / "go back to normal" | Update `focus` in `<agent>/profile.yaml` (null clears focus) |

### Processing Rules

1. Detect directive intent naturally from conversation (no special syntax needed)
2. Confirm what you're about to change before doing it: "I'll create a new aspiration to learn about cooking — sound right?"
3. Execute the state change using existing conventions (asp-NNN IDs, goal structure, etc.)
4. Confirm completion: "Done — added aspiration asp-003: Explore Cooking. I'll start working on this in my next loop cycle."
5. In RUNNING state: directive takes effect on next aspirations loop iteration
6. In IDLE state: state is updated, takes effect when user runs `/start`
7. In UNINITIALIZED state: do NOT write files (world/ and agent dir don't exist yet). Acknowledge conversationally: "Got it — once you run `/start`, I'll set that up." Process the directive immediately after `/start` creates world/ and agent dir.

## Step 6: Knowledge Freshness Check

After responding, assess: did the user just tell me something that corrects or
extends knowledge in my tree?

1. If the user provided a correction (e.g., "that's not how X works", fixing a
   misconception, providing updated information):
   a. Identify affected tree nodes via _tree.yaml scan
   b. Read those nodes
   c. Update immediately — user corrections are authoritative
   d. Set last_update_trigger: {type: "user-correction", session: N}
   e. Log: "KNOWLEDGE UPDATE (user correction): {node_key} — {summary}"
   f. Archive user correction as experience:
      experience_id = "exp-user-correction-{node_key}-{date}"
      Write <agent>/experience/{experience_id}.md with:
          - Exact user statement (verbatim)
          - What was corrected (prior belief/knowledge)
          - Which tree nodes were updated
          - Impact assessment
      echo '<experience-json>' | bash core/scripts/experience-add.sh
      Experience JSON:
          id: "{experience_id}"
          type: "user_correction"
          created: "{ISO timestamp}"
          category: "{node category}"
          summary: "User corrected: {brief description}"
          tree_nodes_related: ["{affected node keys}"]
          verbatim_anchors:
              - key: "user-statement"
                content: "{exact user message}"
              - key: "prior-belief"
                content: "{what the agent previously believed}"
          content_path: "<agent>/experience/{experience_id}.md"

2. If the user provided new information that extends but doesn't contradict:
   a. Append insight to relevant node
   b. Update front matter: last_update_trigger
   c. bash core/scripts/tree-update.sh --set <node-key> last_updated <today>

3. If broader implications exist (other nodes may also be affected):
   echo '<debt-json>' | wm-append.sh knowledge_debt  # for those nodes

This step is lightweight — skip if the user message was a simple question,
command, or directive with no knowledge-bearing content.

## Step 7: Discovery Check (RUNNING Only)

SKIP if: agent state is not RUNNING.

After responding and processing directives, evaluate: did this interaction
reveal something worth acting on that is NOT already captured by Step 5
directives or existing goals?

This captures **agent-initiated** discoveries — things the agent noticed while
searching knowledge (Step 4), checking system state, or formulating the response.
User-reported observations are already handled by Step 5's directive table.

1. Quick assessment (mental pass — no script calls):
   - Did I notice something anomalous, unexpected, or broken?
   - Did this interaction spark an improvement idea?
   - If NEITHER: skip. Most responses produce no agent discoveries.

2. Dedup: `Bash: load-aspirations-compact.sh` → IF path returned: Read it
   (compact data has IDs, titles, statuses — no descriptions/verification)
   Check goal titles for semantic overlap with proposed discovery.
   IF a similar goal already exists: skip creation.

3. Create goal(s) — max 2 per response, in best-fit aspiration:
   `echo '<JSON>' | bash core/scripts/aspirations-add-goal.sh <aspiration_id>`

   Investigation format:
   ```json
   {
       "title": "Investigate: {observation (50 chars)}",
       "status": "pending",
       "priority": "MEDIUM",
       "skill": null,
       "participants": ["agent"],
       "category": "{relevant category}",
       "description": "Noticed during user response: {observation}\n\nContext: {what retrieval or reasoning revealed}",
       "verification": { "outcomes": ["Root cause understood and documented"], "checks": [] },
       "blocked_by": []
   }
   ```

   Idea format:
   ```json
   {
       "title": "Idea: {creative insight (50 chars)}",
       "status": "pending",
       "priority": "MEDIUM",
       "skill": null,
       "participants": ["agent"],
       "category": "{relevant category}",
       "description": "Idea from user response: {full description}\n\nExpected benefit: {why this matters}",
       "verification": { "outcomes": ["Idea evaluated — implemented, formed hypothesis, or retired"], "checks": [] },
       "blocked_by": []
   }
   ```

4. Log: `"DISCOVERY: Created {goal_id}: {title} in {asp_id}"`

This step is fire-and-forget — no experience archival, no spark check.
The goal enters the aspirations queue and gets full treatment when executed.

## Step 7.5: Interaction Learning (All Initialized States)

SKIP if: agent state is UNINITIALIZED (no world/ directory).
SKIP if: persona is false (standard Claude assistant mode, no learning).

After completing the response, directives, knowledge freshness, and discovery checks,
evaluate whether this interaction warrants persistent learning artifacts.

### Notability Assessment

Quick assessment of the user's message — same pattern as Step 7's mental pass.
The agent uses its judgment to answer four questions:

```
1. Did the user share DOMAIN KNOWLEDGE worth preserving?
   (Causal reasoning, architectural insight, "how things actually work",
   lessons from experience, explanations of WHY — not just questions or commands)
   → If yes: note as INSIGHT

2. Did the user give FEEDBACK on how the agent should operate?
   (Process corrections, behavioral guidance, "do this differently",
   approval/criticism of approach — distinct from Step 6 FACT corrections)
   → If yes: note as FEEDBACK

3. Did the user express a TESTABLE BELIEF or PREDICTION?
   (Uncertain claims about system behavior, theories about why something
   happens, predictions about what will happen — something the agent
   could later confirm or correct)
   → If yes: note as HYPOTHESIS

4. Did the user share an OPERATIONAL LESSON or warning?
   (Error patterns, environment quirks, "gotcha" knowledge, debugging tips,
   "always do X when Y", "watch out for Z" — procedural friction knowledge
   that causes repeated pain across sessions)
   → If yes: note as OPS_GOTCHA

IF none apply: RETURN — most interactions are simple Q&A. No script calls needed.
```

The agent's natural language understanding determines notability — not keyword matching.
User messages are freeform; rigid keyword gates would miss novel phrasings and
subtle insights. Trust the model to distinguish "the way X works is Y" (insight)
from "how does X work?" (question).

### Sub-step 7.5a: Reasoning Bank / Guardrail Creation

**From INSIGHT → consider reasoning bank entry:**

1. Determine relevant category from conversation topic
2. Dedup: `Bash: reasoning-bank-read.sh --category {category}` — scan for semantic overlap
3. If existing entry covers it: `Bash: reasoning-bank-increment.sh {entry.id} utilization.times_helpful` → done
4. Otherwise create:
   ```
   echo '<JSON>' | bash core/scripts/reasoning-bank-add.sh
   ```
   JSON structure (id and created are required — determine next rb-NNN from dedup read):
   ```json
   {
       "id": "rb-{NNN}",
       "title": "User insight: {brief title}",
       "type": "user_provided",
       "category": "{relevant category}",
       "content": "{user's insight with agent interpretation}",
       "created": "{today}",
       "when_to_use": {
           "conditions": ["{when this applies}"],
           "category": "{category}"
       },
       "tags": ["user-provided"]
   }
   ```

**From FEEDBACK → consider guardrail:**

1. Dedup: `Bash: guardrails-read.sh --category {category}` — scan for semantic overlap
2. If existing guardrail covers it: `Bash: guardrails-increment.sh {guard.id} times_triggered` → done
3. Otherwise create:
   ```
   echo '<JSON>' | bash core/scripts/guardrails-add.sh
   ```
   JSON structure (id and created are required — determine next guard-NNN from dedup read):
   ```json
   {
       "id": "guard-{NNN}",
       "rule": "{what to do or avoid, derived from user's guidance}",
       "category": "{relevant category}",
       "trigger_condition": "{when this guardrail applies}",
       "source": "user-interaction",
       "created": "{today}"
   }
   ```

**From OPS_GOTCHA → reasoning bank entry (and optionally guardrail):**

Operational gotchas are procedural friction knowledge that causes repeated pain
across sessions (error patterns, environment quirks, debugging lessons). These
get MANDATORY encoding — the whole point is to stop re-discovering them.

1. Determine relevant category from conversation topic
2. Dedup: `Bash: reasoning-bank-read.sh --category {category}` — scan for semantic overlap
3. If existing entry covers it: `Bash: reasoning-bank-increment.sh {entry.id} utilization.times_helpful` → done
4. Otherwise create:
   ```
   echo '<JSON>' | bash core/scripts/reasoning-bank-add.sh
   ```
   JSON structure (id and created are required — determine next rb-NNN from dedup read):
   ```json
   {
       "id": "rb-{NNN}",
       "title": "Gotcha: {brief title}",
       "type": "user_provided",
       "category": "{relevant category}",
       "content": "{user's operational lesson with context and resolution}",
       "created": "{today}",
       "when_to_use": {
           "conditions": ["{error pattern, symptom, or trigger scenario}"],
           "category": "{category}"
       },
       "tags": ["user-provided", "ops-gotcha"]
   }
   ```
5. IF the gotcha is prescriptive ("always do X" / "never do Y") → ALSO create a guardrail:
   ```
   echo '<JSON>' | bash core/scripts/guardrails-add.sh
   ```
   ```json
   {
       "id": "guard-{NNN}",
       "rule": "{the prescriptive rule from the user's gotcha}",
       "category": "{relevant category}",
       "trigger_condition": "{when this gotcha applies}",
       "source": "user-interaction",
       "created": "{today}",
       "tags": ["ops-gotcha"]
   }
   ```

Not every insight needs a reasoning bank entry and not every piece of feedback needs
a guardrail. Use judgment — create artifacts for things that are **reusable across
future interactions**, not one-off clarifications. OPS_GOTCHA is the exception:
always encode these — the user shared them specifically because they keep getting lost.

### Sub-step 7.5b: Hypothesis Formation

**From HYPOTHESIS → consider pipeline record:**

1. Dedup: `Bash: pipeline-read.sh --stage active` and `Bash: pipeline-read.sh --stage discovered`
2. If existing hypothesis covers it → skip
3. Otherwise create:
   ```
   echo '<JSON>' | bash core/scripts/pipeline-add.sh
   ```
   JSON structure:
   ```json
   {
       "id": "{today}_{slug-from-prediction}",
       "title": "{user's prediction (80 chars max)}",
       "stage": "discovered",
       "horizon": "{short|long — based on when testable}",
       "type": "{high-conviction|exploration}",
       "confidence": 0.6,
       "position": "{user's stated belief}",
       "formed_date": "{today}",
       "category": "{relevant category}",
       "rationale": "User shared this prediction during interaction: {user's words}"
   }
   ```
   - Type: `high-conviction` if user was certain, `exploration` if uncertain
4. If RUNNING and a best-fit aspiration exists: also create evaluation goal via
   `echo '<JSON>' | bash core/scripts/aspirations-add-goal.sh <asp-id>` with
   `skill: "/review-hypotheses --hypothesis {hypothesis_id}"`

Only form hypotheses from claims that are genuinely testable — "I think X causes Y"
qualifies, but "I think we should do Z" is a directive (Step 5), not a hypothesis.

### Sub-step 7.5c: Experience Archival

If any artifacts were **created** (not just strengthened) → archive the interaction:

1. Write content to `<agent>/experience/exp-interaction-{date}-{slug}.md`:
   ```markdown
   ---
   type: user_interaction
   date: {today}
   agent_state: {RUNNING|IDLE}
   ---
   # User Interaction: {brief topic}

   ## User Message
   {relevant user message text — the parts that triggered learning}

   ## Agent Response Summary
   {1-2 sentence summary of what the agent responded}

   ## Artifacts Created
   {list: rb-NNN, guard-NNN, hypothesis ID, or "strengthened existing {id}"}
   ```

2. Index: `echo '<JSON>' | bash core/scripts/experience-add.sh`
   ```json
   {
       "id": "exp-interaction-{date}-{slug}",
       "type": "user_interaction",
       "created": "{today}",
       "category": "{relevant category}",
       "summary": "Interaction learning: {what was learned}",
       "content_path": "<agent>/experience/exp-interaction-{date}-{slug}.md",
       "tree_nodes_related": ["{nodes consulted in Step 4}"],
       "verbatim_anchors": [{"key": "user-statement", "content": "{user's key statement}"}]
   }
   ```

Skip archival for interactions that only strengthened existing artifacts — that's
routine, not notable enough for a full experience record.

### Sub-step 7.5d: Journal Entry

If any learning occurred (artifacts created OR strengthened):

Append to journal via `echo '<JSON>' | bash core/scripts/journal-add.sh` or
`bash core/scripts/journal-merge.sh` if session entry exists:

```
## {timestamp} — Interaction Learning
Topic: {conversation topic}
Artifacts: {list of created/strengthened artifact IDs}
```

### Ownership Boundaries

- **Step 6** owns fact corrections to knowledge tree nodes (user says "X is actually Y")
- **Phase 6.5** (aspirations-spark) owns goal-execution-derived guardrails and reasoning bank
- **Step 7.5** owns user-interaction-derived guardrails, reasoning bank, hypotheses, and experiences
- No overlap: if Step 6 already handled a user correction, Step 7.5 does not re-process it

## Persona Configuration Reference

- Persona framework defaults defined in `core/config/profile.yaml` under `persona:`
- Live persona state in `<agent>/profile.yaml` under `persona:` (seeded from config by `/boot`)
- Tone options: `direct`, `friendly`, `formal`, `casual`
- Verbosity options: `terse`, `concise`, `detailed`, `thorough`
- `personality_notes`: free-form string for additional persona guidance (empty by default)
- `use_knowledge_in_chat`: whether to call retrieve.sh for conversational responses
- `surface_pending_questions`: whether to weave pending questions into responses
- `surface_user_goals`: whether to mention user-participant goals
- Persona state: `session-persona-get.sh` returns `true`, `false`, or `unset` (default `true`)
- Only `/start`, `/stop`, and `/boot` may set persona via `session-persona-set.sh`
- User directives like "be more casual" update `<agent>/profile.yaml` persona fields directly

## Pending Questions Queue Format

When the agent needs user input during autonomous operation, it writes to `<agent>/session/pending-questions.yaml` instead of blocking:

```yaml
questions:
  - id: pq-001
    date: "YYYY-MM-DD"
    context: "goal or skill context"
    question: "What the agent needs to know"
    default_action: "What the agent will do autonomously"
    status: pending  # pending | answered | superseded
    answer: null
```

- **pending**: awaiting user response (agent proceeds with default_action)
- **answered**: user provided an answer (agent incorporates on next loop iteration)
- **superseded**: agent resolved the situation before user answered
- ID format: `pq-NNN` (zero-padded 3-digit, monotonically increasing)
