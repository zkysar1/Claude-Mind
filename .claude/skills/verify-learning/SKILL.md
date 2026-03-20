---
name: verify-learning
description: "Post-test verification — check agent state against checklist"
triggers:
  - "/verify-learning"
conventions: [aspirations, pipeline, experience, reasoning-guardrails, pattern-signatures, spark-questions, journal, tree-retrieval, goal-schemas, session-state, infrastructure, secrets, handoff-working-memory]
---

# /verify-learning — Post-Test Verification

USER-ONLY COMMAND. Claude must NEVER invoke this skill.

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Step 1: Load Checklists

1. Read `core/config/verification-checklist.md` (framework checklist).
2. Read `core/config/verification-checklist-domain-specific.md` (foundational domain checklist).
3. IF `mind/verification-checklist.md` exists:
   Read it (agent-discovered domain checks).
   ELSE: Note "No agent-discovered domain checks — skipping."

## Step 2: Evaluate Each Section

For each item in ALL sections from all checklists (A through the last section):
1. Read the referenced file
2. Report **PASS**, **FAIL**, or **N/A** (if the agent didn't reach that stage)

For section G (Known Design Limitations):
- Confirm these are expected behaviors, not bugs

## Step 3: Evidence Check

Focus on what actually happened during the test — did the agent USE the new features, or did it just have them available? Look at:
- Resolved pipeline records: `pipeline-read.sh --stage resolved`
- Journal entries in `mind/journal/`
- Reasoning bank entries: `reasoning-bank-read.sh --summary`

   # Experience archive evidence checks
   IF mind/experience.jsonl exists:
       Bash: experience-read.sh --summary → verify experience records were created
       Bash: experience-read.sh --meta → verify metadata tracking
       Check: do experience records have corresponding .md files at content_path?
       Check: do pipeline records with experience_ref point to valid experience IDs?

   # Recurring goal evidence checks
   IF asp-002 (Maintain) exists in aspirations-read.sh --active:
       Check: asp-002 goals have recurring: true and interval_hours set
       Check: any recurring goal with status completed has lastAchievedAt as full ISO timestamp
       Check: any recurring goal with achievedCount > 0 has updated streak counters
       Bash: goal-selector.sh → verify recurring goals appear/don't appear based on interval elapsed
       Check: if any recurring goal has achievedCount > 1, verify currentStreak is consistent
              (streak should be 1 if previous completion was overdue by > 2x interval)
       Check: goal-selector.sh output recurring_urgency raw value never exceeds 5.0
       Check: g-002-01 has non-empty preconditions in verification block

   # Exploration noise evidence checks
   Bash: goal-selector.sh → parse first result
       Check: output includes exploration_params with epsilon, noise_scale, noise_weight
       Check: breakdown includes exploration_noise key
       Check: raw includes exploration_noise key (value between 0 and 1)
       Check: exploration_params.noise_weight == epsilon * noise_scale
       Check: core/config/developmental-stage.yaml has exploration.noise_scale
       Check: mind/developmental-stage.yaml has exploration.epsilon
       Run goal-selector.sh twice: verify scores differ between invocations (noise is stochastic)

   # Deferred goal evidence checks
   IF any goal has deferred_until set:
       Check: goal with future deferred_until does NOT appear in goal-selector.sh output
       Check: goal with past deferred_until DOES appear with deferred_readiness raw = 1.5
       Check: deferred_until persists on goal after completion (not cleared)
       Check: defer_reason is present alongside deferred_until (context for follow-up)

   # Error response protocol evidence checks (Section AJ)
   Check: `.claude/rules/error-response.md` exists with blocker-centric model
   Check: `aspirations-execute/SKILL.md` Phase 4.1 uses `guardrail-check.sh` (NOT `guardrails-read.sh --active`)
   Check: `aspirations/SKILL.md` Phase 0.5a uses `guardrail-check.sh` (NOT `guardrails-read.sh --active`)
   Bash: guardrail-check.sh --context infrastructure --outcome succeeded --phase post-execution --dry-run → verify returns relevant guardrails
   Bash: guardrail-check.sh --context any --phase pre-selection --dry-run → verify returns relevant guardrails

   # Infrastructure health evidence checks (Section AL)
   Check: `.claude/rules/verify-before-assuming.md` exists with probe-before-concluding imperative
   Check: `core/scripts/infra-health.sh` exists and `core/scripts/infra-health.py` implements check/check-all/status/stale
   Check: `mind/infra-health.yaml` exists (components defined per domain deployment)
   Check: Phase 2.5b in aspirations/SKILL.md includes verification probe before accepting blockers
   Check: Phase 0.5b includes success-based blocker clearing via infra-health.yaml
   Bash: bash core/scripts/infra-health.sh status → verify JSON output
   IF any goal was skipped due to infrastructure blocker:
       Check: infra-health.sh check was called BEFORE the blocker was accepted
       Check: agent never declared infrastructure unavailable without a failed probe command

   # Actionable Findings Gate evidence checks (Section AF)
   Check: `aspirations-state-update/SKILL.md` includes Step 8.5 between Step 8 and closing block
   Check: Step 8.5 keyword patterns: root_cause, bug_identified, proposed_fix, unimplemented_action
   Check: Investigation override exists (binary fallback when no keywords match)
   IF any Investigation goal completed with productive outcome during the test:
       Check: journal mentions "Findings gate" or "Step 8.5" for that goal
       Check: if root cause was found, an Unblock goal was created with discovered_by field

   # Aspiration Completion Review evidence checks (Section ACR)
   Check: `aspirations/SKILL.md` Phase 7 includes Phase 7.5 between spark and archival
   Check: `goals_added_to_completing_asp` initialized at Phase 7.5 entry (not a separate flag)
   Check: single archival point guarded by `goals_added_to_completing_asp == 0`
   IF any aspiration completed during the test:
       Check: journal contains "Completion Review" entry for that aspiration
       Check: if outstanding findings detected, goals were created with discovery_type field
       Check: if goals added to completing aspiration, archival was deferred (aspiration still in live file)

   # Mandatory Goal Selection evidence checks (Section AV)
   Check: `core/config/conventions/goal-selection.md` exists with Single Authority Rule
   Check: `aspirations/SKILL.md` conventions list includes `goal-selection`
   Check: `aspirations/SKILL.md` Phase 2 ELSE block has assertion comment referencing convention
   Bash: python3 core/scripts/postcompact-restore.py 2>/dev/null | grep -q "MANDATORY.*goal-selector" → verify reminder present
   Check: CLAUDE.md Convention Index includes `goal-selection.md`
   Check: `mind/knowledge/tree/system.md` has "Loop Integrity" section

   # Hybrid skill + completion report evidence checks (Section AN)
   Check: `.claude/skills/completion-report/SKILL.md` has `user-invocable: true`
   Check: CLAUDE.md enforcement rule 1 does NOT list /completion-report in the MUST NOT invoke enumeration
   Check: CLAUDE.md Skill Invocation Rules has "Hybrid skills" bullet
   Bash: aspirations-read.sh --id asp-002 → verify g-002-05 skill references `/completion-report` (or forged wrapper)

   # Stop consolidation evidence checks (Section SC)
   Check: `stop/SKILL.md` RUNNING section invokes `/aspirations-consolidate with: stop_mode = true`
   Check: `stop/SKILL.md` does NOT contain "sensory_buffer" or "mini-consolidation" (old approach removed)
   Check: `aspirations-consolidate/SKILL.md` has `## Parameters` section with `stop_mode` documented
   Check: Steps 6, 7, 8, 8.7, 10 each have `(skip in stop_mode)` in their heading
   Check: Step 8.7 "Store user goal count" is indented inside the `IF stop_mode != true:` block
   IF a /stop was executed during the test:
       Check: `mind/session/handoff.yaml` exists (Step 9 ran during stop)
       Bash: wm-read.sh --json → verify working memory is reset (Steps 4-5 ran)
       Check: journal has "## Consolidation" entry (Step 3 ran)

   # Precision encoding evidence checks (Section PE)
   Check: `mind/conventions/precision-encoding.md` exists with Precision Manifest Schema section
   Check: `aspirations-state-update/SKILL.md` Step 8 has "EXTRACT PRECISION" substep before "WRITE NARRATIVE"
   Check: `reflect-hypothesis/SKILL.md` Step 2.7 encoding queue includes `precision_manifest` field
   Check: `aspirations-consolidate/SKILL.md` Step 2b has "EXTRACT PRECISION from encoding queue item"
   Check: `aspirations/SKILL.md` Phase -0.5c has "PRECISION-FIRST ENCODING" comment
   Check: `reflect-tree-update/SKILL.md` Step 2 minor insight path has "EXTRACT PRECISION" step
   Check: `reflect-execution/SKILL.md` refinement path has "Verified Values" section write
   Check: `aspirations-execute/SKILL.md` verbatim_anchors has "MANDATORY: capture ALL precise technical values"
   Bash: grep -c "Verified Values" .claude/skills/*/SKILL.md → verify >= 5 files
   Bash: grep -c "precision_manifest" .claude/skills/*/SKILL.md → verify >= 3 files
   Bash: grep -c "PRECISION AUDIT" .claude/skills/*/SKILL.md → verify >= 3 files
   IF agent has run 3+ productive goal cycles since precision encoding was deployed:
       Check: at least 1 tree node has a "## Verified Values" section
       Bash: wm-read.sh encoding_queue --json → verify items include precision_manifest field
       Check: experience records have specific verbatim_anchors (not just "key error messages")

   # Aspirations compact cache evidence checks (Section BE)
   Check: `core/scripts/aspirations.py` has `COMPACT_GOAL_KEEP` set and `compact_aspiration()` function
   Check: `core/scripts/load-aspirations-compact.sh` exists and follows `load-tree-summary.sh` pattern
   Check: `core/scripts/context-reads.py` has `TRACKED_FILES` list with `aspirations-compact.json`
   Check: `aspirations/SKILL.md` Phase 2.9 calls `aspirations-read.sh --id` for full goal detail
   Check: `aspirations/SKILL.md` Phase 2.9 comment says "do NOT remove this or execution runs blind"
   Bash: FULL=$(bash core/scripts/aspirations-read.sh --active 2>/dev/null | wc -c) && COMPACT=$(bash core/scripts/aspirations-read.sh --active-compact 2>/dev/null | wc -c) && echo "$FULL $COMPACT" → verify compact < full
   Bash: bash core/scripts/aspirations-read.sh --active-compact 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); g=d[0]['goals'][0]; assert 'id' in g; assert 'description' not in g; assert 'verification' not in g; print('OK')" → verify compact strips heavy fields
   Bash: rm -f mind/session/aspirations-compact.json && bash core/scripts/load-aspirations-compact.sh 2>/dev/null → verify returns path
   Check: 16+ skill files reference `load-aspirations-compact.sh` (grep count)
   Check: `boot/SKILL.md`, `backlog-report/SKILL.md`, `decompose/SKILL.md` KEEP `aspirations-read.sh --active` (need full detail)

## Step 4: Summary Report

Provide a summary table:
- Total PASS / FAIL / N/A per section
- List of any FAIL items that need attention

## Chaining
- Calls: nothing
- Called by: User only. NEVER by Claude.
