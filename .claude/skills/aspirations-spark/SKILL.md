---
name: aspirations-spark
description: "Spark Check — adaptive spark questions, all sq-XXX handlers, aspiration-level spark, and Phase 6.5 immediate learning"
user-invocable: false
parent-skill: aspirations
triggers:
  - "run_spark_check()"
  - "run_aspiration_spark()"
conventions: [aspirations, spark-questions, reasoning-guardrails, experience]
minimum_mode: autonomous
---

# Spark Check (Micro-Evolution) and Immediate Learning

Invoked after every goal completion as Phase 6 (spark check) and Phase 6.5 (immediate learning) of the aspirations loop. The spark check is the recursive self-improvement mechanism. Phase 6.5 captures reasoning bank entries, guardrails, and forge awareness immediately during execution rather than waiting for /reflect.

---

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Phase 6.5: Immediate Learning (reasoning bank + guardrails)

If this goal's outcome produced a clear, reusable reasoning insight or
a safety lesson, capture it NOW — don't wait for /reflect.
This is for lessons learned during EXECUTION, not hypothesis resolution
(which /reflect handles separately).

SKIP: goal outcome was routine/expected with no new insight.

```
    IF goal outcome revealed a reusable reasoning pattern (heuristic, procedure,
       diagnostic, or causal insight) that would help with FUTURE similar goals:

        # Duplicate/contradiction check before creating reasoning bank entry
        existing_rb = Bash: reasoning-bank-read.sh --category {goal.category}
        IF proposed entry semantically overlaps with an existing entry:
            Strengthen existing: Bash: reasoning-bank-increment.sh {entry.id} utilization.times_helpful
            Log: "Phase 6.5: Strengthened existing {entry.id} instead of creating duplicate"
            SKIP creation
        IF proposed entry contradicts an existing entry:
            Retire old: Bash: reasoning-bank-update-field.sh {entry.id} status retired
            Proceed to create new entry (supersedes old)

        Create reasoning bank entry via reasoning-bank-add.sh:
          id: next rb-NNN (check existing IDs via reasoning-bank-read.sh --summary)
          title: concise name for the insight
          type: heuristic | procedure | diagnostic | causal
          category: goal's category
          content: the insight — what to do and why
          when_to_use: when this insight applies
          source_goal: goal.id
          source_reflection_id: "ref-{goal.id}-{timestamp}"  # MR-Search: enables reflection quality tracking
        Log in journal: "Immediate learning: created {rb-id} from {goal.id}"

    IF goal outcome revealed a safety hazard, a mistake to avoid, or a
       precondition that MUST be checked in future similar work:

        # Duplicate/contradiction check before creating guardrail
        existing_guards = Bash: guardrails-read.sh --category {goal.category}
        IF proposed guardrail semantically overlaps with an existing guardrail:
            Strengthen existing: Bash: guardrails-increment.sh {guard.id} times_triggered
            Log: "Phase 6.5: Strengthened existing {guard.id} instead of creating duplicate"
            SKIP creation
        IF proposed guardrail contradicts an existing guardrail:
            Retire old: Bash: guardrails-update-field.sh {guard.id} status retired
            Proceed to create new guardrail (supersedes old)

        Create guardrail via guardrails-add.sh:
          id: next guard-NNN (check existing IDs via guardrails-read.sh --summary)
          rule: what to check or avoid
          category: goal's category
          trigger_condition: when this guardrail applies
          source: goal.id
          source_reflection_id: "ref-{goal.id}-{timestamp}"  # MR-Search: enables reflection quality tracking
        Log in journal: "Immediate guardrail: created {guard-id} from {goal.id}"

    # Forge awareness: detect recurring manual procedures that should be skills
    IF goal execution required a manual multi-step procedure that was repeated
       across goals, OR that would clearly benefit FUTURE goals as a discoverable
       skill (entry point) rather than inline code:
        Read meta/skill-gaps.yaml
        IF gap already exists for this procedure:
            Increment times_encountered, append to encounter_log
        ELSE:
            Register new gap: id: gap-{next}, status: registered,
              times_encountered: 1, procedure_name, estimated_value
        Write updated meta/skill-gaps.yaml

        # Check forge criteria immediately
        # GUARD: skip already-forged gaps (Phase 9.2 also checks this)
        IF gap.status == "forged": skip forge criteria check

        Read core/config/skill-gaps.yaml → forge_threshold (default: 2)
        Read <agent>/developmental-stage.yaml → current stage
        IF gap.times_encountered >= forge_threshold
           AND gap.estimated_value >= "medium"
           AND developmental stage >= EXPLOIT (developing+):
            # Verify no pending forge goal already exists for this gap
            Bash: load-aspirations-compact.sh → IF path returned: Read it
            (compact aspirations now in context — search goals for this gap ID)
            IF no existing forge goal:
                Route to target aspiration (current → matching category → /create-aspiration)
                Build goal: title "Forge skill: {gap.procedure_name}",
                  skill "/forge-skill", args "skill {gap.id}", priority "MEDIUM"
                Add via aspirations-update.sh
                Log in journal: "Forge-ready gap detected during execution: {gap.id}"
                Log: echo '{"date":"...","event":"forge-ready","details":"Gap {gap.id} detected in Phase 6.5 from {goal.id}","trigger_reason":"immediate-learning-forge"}' | bash core/scripts/evolution-log-append.sh
```

---

## Spark Check (Micro-Evolution)

Run after EVERY goal completion. This is the recursive self-improvement mechanism.

### Goal-Level Spark

### Adaptive Spark Questions
Read active spark questions via script instead of using hardcoded spark questions.
1. `bash core/scripts/spark-questions-read.sh --active` → get active questions as JSON
2. Ask each active question about the just-completed goal
3. If a spark is generated: `bash core/scripts/spark-questions-increment.sh <id> sparks_generated`
4. Always: `bash core/scripts/spark-questions-increment.sh <id> times_asked` (script auto-recomputes yield_rate)

Every `evolution_rules.review_interval_sessions` sessions:
- Retire questions with yield_rate < retire_threshold AND times_asked >= min_asks_before_retire
- Promote highest-priority candidate to replace retired question
- Log the change via `echo '<json>' | bash core/scripts/evolution-log-append.sh`

```
Bash: spark-questions-read.sh --active
For each question in result:
    Ask the question about the just-completed goal
    Bash: spark-questions-increment.sh <question.id> times_asked
    If spark generated:
        Bash: spark-questions-increment.sh <question.id> sparks_generated
        Execute the spark action (add source, create article, log gap, etc.)
    # yield_rate is auto-recomputed by the increment script — no manual update needed

If any spark fires → log via:
  echo '{"event":"spark","details":"Goal {id} sparked: {description of change}","date":"<today>"}' | bash core/scripts/evolution-log-append.sh
```

#### Hypothesis Generation via sq-009

When sq-009 fires ("Could we form a TESTABLE PREDICTION..."), it creates a hypothesis goal:
0. Load domain context for informed hypothesis formation:
   ```
   Bash: retrieve.sh --category {goal.category} --depth shallow
   Bash: pipeline-read.sh --stage active
   Bash: pipeline-read.sh --stage discovered
   ```
   Check retrieved active/discovered hypotheses for semantic overlap with the proposed prediction.
   IF a hypothesis already covers this prediction → SKIP creation, log: "sq-009: Duplicate of {existing_id}, skipped"
0.5. Calibration gate (BEFORE assigning confidence):
     a. Read recent accuracy: `Bash: pipeline-read.sh --stage resolved`
        - Count CONFIRMED vs CORRECTED in this category (or overall if <3 in category)
        - If total == 0: SKIP gate (no track record yet), proceed to Step 0.7
        - Compute recent_accuracy = confirmed / total
     b. Apply confidence ceiling:
        - If recent_accuracy < 0.40: cap at 0.55
        - If recent_accuracy >= 0.40 and < 0.60: cap at 0.65
        - If recent_accuracy >= 0.60 and < 0.80: cap at 0.80
        - If recent_accuracy >= 0.80: no cap
        - Log: "Calibration gate: {N} resolved, {accuracy}% accurate → cap {cap}"
     c. The agent MAY assign confidence below the cap freely.
        The cap only prevents overconfidence, not underconfidence.
0.7. Adversarial pre-mortem (required when proposed confidence > 0.65):
     Before finalizing confidence, articulate:
     a. "The strongest reason this prediction could be WRONG is: ___"
     b. "The code/system might actually handle this because: ___"
     c. If (b) identifies a plausible mechanism the code already handles it,
        reduce confidence by 0.15 (the "well-engineered codebase" prior).
     d. Record the pre-mortem in the experience archive (Step 2.5 content).
     SKIP this step only if the prediction is about external systems
     (AWS behavior, third-party APIs) rather than AyoAI code quality.
1. Create pipeline record: `echo '<record-json>' | bash core/scripts/pipeline-add.sh` (stage defaults to discovered)
2. Add goal to aspiration: read current aspiration via `aspirations-read.sh --id <asp-id>`,
   add new goal with hypothesis fields, then pipe updated aspiration JSON to
   `echo '<aspiration-json>' | bash core/scripts/aspirations-update.sh <asp-id>`
   Goal fields:
   - `participants: [agent]`
   - `skill: "/review-hypotheses --hypothesis {hypothesis_id}"`
   - `hypothesis_id` linking to the pipeline file
   - `horizon` — select using decision tree below
   - `resolves_no_earlier_than`, `resolves_by` from default windows for chosen horizon
   - `priority: MEDIUM` (default, agent can adjust)

   **Horizon selection** (pick the FIRST that matches):
   - **long** — prediction about a trend, scaling limit, or outcome that needs weeks+ to observe
     *Example: "Storage rotation threshold will need adjustment as data volume grows"*
   - **short** — prediction about what will happen after a future change (next commit, deploy, refactor)
     *Example: "Refactoring auth caching will require service health-check interval changes"*
     Also use short when predicting: user's next likely focus area, whether a pattern holds
     across future aspirations, or consequences of a known TODO/tech-debt item
   - **session** — prediction verifiable NOW by reading current state
     *Example: "The service uses two-phase scheduling"*

   **Bias toward short/long**: If the prediction is about current state, it's probably already
   captured by a goal outcome — don't duplicate it as a session hypothesis. Prefer forming
   predictions about what WILL change or what WOULD happen IF something changes.

2.5. Archive hypothesis formation context:
        experience_id = "exp-{hypothesis_id}"
        Write <agent>/experience/{experience_id}.md with:
            - Full context manifest content (what was actually read, not just paths)
            - Evidence consulted and reasoning chain
            - Why this confidence level was chosen
            - What would change the prediction
        echo '<experience-json>' | bash core/scripts/experience-add.sh
        Experience JSON:
            id: "{experience_id}"
            type: "hypothesis_formation"
            created: "{ISO timestamp}"
            category: "{hypothesis category}"
            summary: "Hypothesis: {claim} (confidence: {N})"
            hypothesis_id: "{hypothesis_id}"
            tree_nodes_related: [nodes from context manifest]
            verbatim_anchors: [key evidence excerpts that informed the prediction]
            content_path: "<agent>/experience/{experience_id}.md"
        Set experience_ref on pipeline record:
            bash core/scripts/pipeline-update-field.sh {hypothesis_id} experience_ref "{experience_id}"
3. Move pipeline file from `discovered/` to `active/` (it's immediately actionable)
4. Log spark via `echo '<json>' | bash core/scripts/evolution-log-append.sh`

#### Self-Evolution Spark Handler

**sq-012**: "Does this outcome change how I think about my core purpose? Should my Self evolve?"

When sq-012 fires after goal completion:
1. Read `<agent>/self.md` — current Self content
2. Assess: does the goal outcome suggest a refinement, expansion, or course correction?
2.5. CONTRACT CHECK (before acting on Self):
   Bash: `curriculum-contract-check.sh --action allow_self_edits`
   IF exit code 1 (not permitted):
       Log: "sq-012: Self edit blocked by curriculum stage {stage_name from JSON output}"
       Skip to step 4 — increment sparks_generated but DO NOT edit Self or write pending question
3. IF YES — choose ONE path (never both):
   a. IF highly confident AND the change is a minor refinement (not a rewrite):
      Edit `<agent>/self.md` — update body, set last_update_trigger: self_evolution
      Log: "SELF EVOLUTION: {summary of change}"
   b. ELSE (uncertain or significant change):
      Write proposed update to `<agent>/session/pending-questions.yaml`:
        question: "Based on [outcome], I think my Self should evolve from [current summary] to [proposed]. Should I update?"
        default_action: "Keep current Self unchanged"
        status: pending
4. Increment `sparks_generated` on the spark question

#### Data Acquisition Spark Handler

**sq-c05**: "Does my knowledge tree reference external data sources, systems, files, APIs, or environments that I haven't directly accessed? What would I learn from obtaining that data?"

When sq-c05 fires after goal completion:
1. Read `world/knowledge/tree/_tree.yaml` — scan node summaries for data source references
2. Read entity_index — look for external system references (SSH endpoints, file paths, APIs, databases)
3. Identify accessible but unaccessed data sources
4. IF found:
   invoke /create-aspiration from-self (Phase B will pick up the data acquisition opportunity)
5. Increment `sparks_generated` on the spark question

#### Memory Curation Spark Handlers

**sq-c03**: "Did completing this goal make any of our existing STRATEGIES, GUARDRAILS, or PATTERN SIGNATURES obsolete or irrelevant?"

When sq-c03 fires after goal completion:
1. Identify the completed goal's category/domain
2. Scan that category for strategies, guardrails, and pattern signatures
3. For each item: "Does this goal's outcome make this artifact obsolete or irrelevant?"
4. If YES to any: invoke `/reflect --curate-memory` scoped to that category
5. Increment `sparks_generated` on the spark question

**sq-c04**: "Is there knowledge in our memory tree that CONTRADICTS what we just learned, or that we now know is STALE?"

When sq-c04 fires after goal completion:
1. Load tree nodes for the completed goal's category using `tree-read.sh --leaves-under {category_key}`
2. For each leaf node with articles: check if key insights conflict with the goal's outcome
3. If contradiction found: flag article for re-research:
      echo '"<article_key> contradicts goal outcome: <summary>"' | wm-append.sh knowledge_debt
4. If a belief is affected: weaken it via existing belief weakening logic (Step 7.6 or equivalent)
5. Increment `sparks_generated` on the spark question

#### Work Discovery Spark Handler

**sq-013**: "Did executing this goal reveal actionable work — a requirement, dependency, follow-up, fix, capability gap, or opportunity — that isn't already tracked?"

When sq-013 fires after goal completion:
1. Classify the discovery: `requirement` | `dependency` | `follow-up` | `fix` | `capability_gap` | `opportunity`
2. Determine target aspiration:
   a. Default: current aspiration (if the work fits its scope/motivation)
   b. If out of scope: scan active aspirations (`aspirations-read.sh --summary`)
      for one whose motivation covers this work → use that aspiration
   c. If no existing aspiration fits: invoke `/create-aspiration` with the discovery
      context (title, description, category) — skip to step 9 (log + increment)
3. Read target aspiration: `bash core/scripts/aspirations-read.sh --id <target-asp-id>`
4. Compute next goal ID: find max `g-NNN-NN` sequence in target's goals, increment by 1
5. Build goal object:
   - `id`: computed next goal ID
   - `title`: concise description of the discovered work
   - `description`: what was discovered and why it matters
   - `status`: `pending`
   - `skill`: appropriate skill for the work
   - `priority`: `dependency`/`requirement`/`fix` → `HIGH`, `follow-up`/`capability_gap` → `MEDIUM`, `opportunity` → `LOW`
   - `verification`: `outcomes` + `checks` + `preconditions`
   - `discovered_by`: the completed goal ID that triggered this spark
   - `discovery_type`: the classification from step 1
5.5. **Quality gate for project+ aspirations** (scope-aware goal addition):
   IF target aspiration's scope is "project" or "initiative"
   AND discovery_type NOT in ("fix", "dependency"):  # cognitive primitives exempt
     - `description` MUST include: what was discovered, why it matters, and brief tree consultation
       (`Bash: tree-find-node.sh --text "{goal.title}" --leaf-only --top 1` — enrich with existing knowledge)
     - `verification.outcomes` MUST include meaningful success criteria (not just "task completed")
     - For `capability_gap` or `opportunity` discoveries: consider whether a companion
       test/verification goal should also be created (same pattern as Step 4c in create-aspiration)
6. Add new goal to the target aspiration's `goals` array
7. Pipe the updated aspiration JSON to: `bash core/scripts/aspirations-update.sh <target-asp-id>`
8. If discovery type is `dependency`: add new goal ID to `blocked_by` on dependent goals
9. Log spark event: `echo '{"event":"spark","details":"sq-013: Goal <completed-id> discovered <type>: <title> → <target-asp-id>","date":"<today>"}' | bash core/scripts/evolution-log-append.sh`
10. Increment `sparks_generated` on the spark question

#### Integration Path Coverage Spark Handler

**sq-015**: "Does the test coverage verify the INTEGRATION PATH (trigger -> handler -> side effect), or only the extracted function in isolation?"

When sq-015 fires after goal completion:
1. Did this goal produce or modify code (Edit/Write to source files)? If no → SKIP (not applicable)
2. Trace the integration path from the change point:
   - What triggers the changed code? (API call, event bus message, scheduler, user action)
   - What side effects does it produce? (state change, message publish, file write)
   - Is there a test that exercises trigger → changed code → side effect?
3. IF no integration path test exists:
   Create investigation goal (via Cognitive Primitives):
   - Title: `"Investigate: integration path coverage for {changed module}"`
   - Priority: MEDIUM, category: from goal's category
   - Verification outcome: "Integration path traced with test gap documented or closed"
   - `discovered_by`: the completed goal ID
4. ELIF integration path test exists but is incomplete:
   Create idea goal: `"Idea: extend integration test for {module} to cover {gap}"`
5. ELSE: integration path is covered — no spark generated, SKIP to step 7
6. Log spark event: `echo '{"event":"spark","details":"sq-015: Goal <completed-id> integration path check for <module>","date":"<today>"}' | bash core/scripts/evolution-log-append.sh`
7. Increment `sparks_generated` on the spark question ONLY if step 3 or 4 created a goal

#### Idea Generation Spark Handler

**sq-014** (candidate for promotion): "Is there a better way to do what I just did? What adjacent problem could benefit from what I just learned?"

When sq-014 fires after goal completion:
1. Reflect on the execution approach: was it optimal? Could it be improved?
2. Consider adjacent problems: does this technique/insight apply elsewhere?
3. If YES: create an idea goal (via Cognitive Primitives) in the most relevant aspiration
   - Title: `"Idea: {creative insight (50 chars)}"`
   - Priority: MEDIUM, skill: null
   - Description: what the idea is, expected benefit, which goal inspired it
4. Increment `sparks_generated` on the spark question

#### Aspiration Generation Spark Handler

**sq-007**: "Is there a new ASPIRATION suggested by this outcome?"

When sq-007 fires after goal completion:
1. Assess: does the goal's outcome suggest an entirely new direction that doesn't fit within any existing aspiration?
2. If YES: invoke `/create-aspiration from-self` — the skill reads Self, scans for purpose gaps, and generates aligned aspirations
3. Log spark event: `echo '{"event":"spark","details":"sq-007: Goal <completed-id> suggested new aspiration direction: <brief description>","date":"<today>"}' | bash core/scripts/evolution-log-append.sh`
4. Increment `sparks_generated` on the spark question

#### sq-c06: Meta-Improvement Spark

**Handler for sq-c06** — "Did this outcome suggest a better improvement PROCEDURE?"

When sq-c06 fires after goal completion:
1. Read meta/improvement-instructions.md
2. Compare: did the approach used in this goal deviate from the documented procedure?
   - Deviated AND succeeded: procedure may be outdated → note for evolve phase
   - Deviated AND failed: procedure may be correct → reinforcing signal
   - Followed AND succeeded: procedure validated → reinforcing signal
   - Followed AND failed: procedure may need revision → note for evolve phase
3. IF meta-insight found:
   - Append to meta/meta-log.jsonl via meta-log-append.sh:
     {"date":"<today>","event":"meta_spark","goal_id":"<goal.id>",
      "insight":"<what the meta-insight is>","procedure_match":"<deviated|followed>",
      "outcome":"<succeeded|failed>"}
   - Log: "META SPARK: {insight} from {goal.id}"
4. Bash: spark-questions-increment.sh sq-c06 sparks_generated

### Aspiration-Level Spark (when entire aspiration completes)
```
Ask these 3 questions:
1. What did we learn from completing this entire aspiration?
   → Write reflection to journal
2. Does this completion unlock a new strategic direction?
   → YES: Create new aspiration via gap analysis
3. Should the system's self-model update?
   → YES: Update meta/meta-knowledge/_index.yaml
4. Did completing this aspiration teach us something about HOW we generate aspirations?
   → IF yes:
       Bash: curriculum-contract-check.sh --action allow_meta_edits
       IF permitted: Read and update meta/aspiration-generation-strategy.yaml with learned heuristic.
     Append to meta/meta-log.jsonl: {"date":"<today>","event":"aspiration_meta_learning","aspiration":"<asp-id>","insight":"<insight>"}

Replacement aspiration generation is handled by Phase 7 archival in aspirations/SKILL.md
(with --plan for full planning treatment). Do NOT duplicate generation here.
```
