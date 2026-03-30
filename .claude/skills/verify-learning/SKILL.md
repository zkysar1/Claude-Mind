---
name: verify-learning
description: "Post-test verification — check agent state against checklist"
triggers:
  - "/verify-learning"
conventions: [aspirations, pipeline, experience, reasoning-guardrails, pattern-signatures, spark-questions, journal, tree-retrieval, goal-schemas, session-state, infrastructure, secrets, handoff-working-memory]
minimum_mode: reader
---

# /verify-learning — Post-Test Verification

User-invocable AND agent-callable (hybrid skill).

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Step 1: Load Checklists

1. Read `core/config/verification-checklist.md` (framework checklist).
2. Read `core/config/verification-checklist-domain-specific.md` (foundational domain checklist).
3. IF `world/verification-checklist.md` exists:
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
- Journal entries in `<agent>/journal/`
- Reasoning bank entries: `reasoning-bank-read.sh --summary`

   # AUTHORITATIVE CHECK SOURCE: All actively-evaluated checks live here in Step 3.
   # core/config/verification-checklist.md is a comprehensive reference catalog
   # (2000+ items) but is too large to load in one context window. When adding new
   # checks for new features, add them HERE, not in the checklist file.
   # The checklist file remains useful for per-section deep dives via targeted reads.

   # 4-Tier Architecture evidence checks (Section 4T)
   Check: `world/.initialized` exists (init-world.sh ran successfully)
   Check: `world/program.md` is non-empty (The Program — shared purpose defined)
   Check: `<agent>/.initialized` exists (init-agent.sh ran successfully)
   Check: `<agent>/self.md` is non-empty (agent identity defined)
   Check: `$AYOAI_AGENT` env var is set and matches agent directory name
   Check: `meta/spark-questions.jsonl` exists (moved from old mind/ to meta/)
   Check: `meta/skill-quality.yaml` exists (moved from old mind/ to meta/)
   Check: `meta/evolution-log.jsonl` exists (moved from old mind/ to meta/)
   Check: Knowledge tree lives in `world/knowledge/tree/` (collective, not per-agent)
   Check: Experience records live in `<agent>/experience.jsonl` (per-agent, not world/)
   Check: `core/scripts/_paths.py` exports WORLD_DIR, AGENT_DIR, META_DIR
   Check: `core/scripts/_platform.sh` has `WORLD_DIR="$(cygpath -m "$WORLD_DIR")"` (Windows path fix)
   Check: No `mind/` directory exists (fully migrated to 4-tier)

   # AYOAI_AGENT env var priority evidence checks (Section 4T continued)
   # _paths.py and _paths.sh must use set-vs-unset detection so AYOAI_AGENT="" overrides .active-agent file.
   # The +x idiom and "in os.environ" pattern are critical — do NOT simplify (see code comments).
   Check: `core/scripts/_paths.py` `_resolve_agent_name` uses `"AYOAI_AGENT" in os.environ` (not `os.environ.get`)
   Check: `core/scripts/_paths.sh` uses `${AYOAI_AGENT+x}` for agent name resolution (not `:-` or `-n`)

   # NO_AGENT state evidence checks (Section 4T continued)
   Bash: AYOAI_AGENT="" python3 core/scripts/session.py state get → verify prints "NO_AGENT" (not crash)
   Bash: AYOAI_AGENT="" python3 core/scripts/session.py persona get → verify prints "no_agent" (not crash)
   Check: `core/scripts/session.py` has `require_agent()` function that exits with clear error
   Check: session.py `cmd_state_set`, `cmd_persona_set`, `cmd_signal_set` all call `require_agent()` before SESSION_DIR access

   # Prime world-only mode evidence checks (Section 4T continued)
   Check: `prime/SKILL.md` has Phase 0.5 "Agent Mode Detection"
   Check: Phase 0.5 handles NO_AGENT by loading world/program.md, guardrails, reasoning bank (no agent-specific files)
   Check: Phase 0.5 outputs "WORLD PRIME (no agent)" header

   # Session binding evidence checks (Section SB)
   Bash: echo $AYOAI_SESSION_ID → verify non-empty (SessionStart hook wrote to CLAUDE_ENV_FILE)
   Check: `.active-agent-$AYOAI_SESSION_ID` file exists and contains current agent name
   Bash: echo $AYOAI_AGENT → verify matches the agent directory name
   Check: `core/scripts/set-active-agent-env.sh` exists (FileChanged hook handler)
   Check: `core/scripts/session-save-id.sh` writes AYOAI_SESSION_ID to CLAUDE_ENV_FILE
   Check: `.claude/settings.json` has FileChanged hook with matcher `.active-agent-*`
   Check: `.gitignore` contains `.active-agent-*` pattern
   Check: `set-active-agent-env.sh` does NOT clear AYOAI_AGENT when another session's file changes (no elif/else branch)
   IF session was resumed (not first start):
       Check: SessionStart hook auto-restored AYOAI_AGENT from `.active-agent-<sid>` file

   # Dual-queue aspiration evidence checks (Section DQ)
   Bash: aspirations-read.sh --active → verify world aspirations exist (asp-001 Explore and Learn)
   Bash: agent-aspirations-read.sh --active → verify agent aspirations exist (asp-001 Maintain Agent Health)
   Bash: goal-selector.sh select 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); sources=set(r.get('source','') for r in d); print('world' in sources, 'agent' in sources)" → verify both sources present
   Check: goal-selector.sh output includes `source` field on every scored goal
   Check: `core/config/conventions/aspirations.md` documents both world and agent script families
   Check: `agent-aspirations-read.sh` exists in core/scripts/ (agent queue access)
   Bash: python3 core/scripts/aspirations.py --source agent read --active 2>/dev/null → verify returns valid JSON (agent queue readable)
   Check: `aspirations.py --source agent claim` → rejected with error (world-only operation)
   IF agent claimed a world goal:
       Check: world/aspirations.jsonl has `claimed_by` and `claimed_at` fields on that goal
       Check: `claimed_by` value matches `$AYOAI_AGENT`
   # JSONL list-field normalization regression check
   Check: `goal-selector.py` defines `_ensure_list()` and every operational `.get("blocked_by")`, `.get("participants")`, `.get("tags")` call is wrapped in it. Only passthrough stores (goal_map building) may use raw `.get()`. Read the file and verify no unguarded iteration of these fields.

   # Experience archive evidence checks
   IF <agent>/experience.jsonl exists:
       Bash: experience-read.sh --summary → verify experience records were created
       Bash: experience-read.sh --meta → verify metadata tracking
       Check: do experience records have corresponding .md files at content_path?
       Check: do pipeline records with experience_ref point to valid experience IDs?

   # Recurring goal evidence checks
   IF asp-001 (Maintain Agent Health) exists in agent-aspirations-read.sh --active:
       Check: asp-001 goals have recurring: true and interval_hours set
       Check: any recurring goal with status completed has lastAchievedAt as full ISO timestamp
       Check: any recurring goal with achievedCount > 0 has updated streak counters
       Bash: goal-selector.sh → verify recurring goals appear/don't appear based on interval elapsed
       Check: if any recurring goal has achievedCount > 1, verify currentStreak is consistent
              (streak should be 1 if previous completion was overdue by > 2x interval)
       Check: goal-selector.sh output recurring_urgency raw value never exceeds 5.0
       Check: g-001-01 has non-empty preconditions in verification block

   # complete-by recurring handling evidence checks
   Check: `core/scripts/aspirations.py` `cmd_complete_by` has `if goal.get("recurring"):` branch
   Check: Recurring branch does NOT set `status = "completed"` (keeps goal pending for next cycle)
   Check: Recurring branch clears `claimed_by` and `claimed_at` (returns goal to pool)
   Check: `complete-by` is NOT in `WORLD_ONLY_COMMANDS` (agents need it for local recurring goals)

   # Exploration noise evidence checks
   Bash: goal-selector.sh → parse first result
       Check: output includes exploration_params with epsilon, noise_scale, noise_weight
       Check: breakdown includes exploration_noise key
       Check: raw includes exploration_noise key (value between 0 and 1)
       Check: exploration_params.noise_weight == epsilon * noise_scale
       Check: core/config/developmental-stage.yaml has exploration.noise_scale
       Check: <agent>/developmental-stage.yaml has exploration.epsilon
       Run goal-selector.sh twice: verify scores differ between invocations (noise is stochastic)

   # Deferred goal evidence checks
   IF any goal has deferred_until set:
       Check: goal with future deferred_until does NOT appear in goal-selector.sh output
       Check: goal with past deferred_until and NO defer_reason DOES appear with deferred_readiness raw = 1.5
       Check: deferred_until persists on goal after completion (not cleared)
   # defer_reason is a functional filter (not just documentation)
   Check: `goal-selector.py` collect_candidates has `if goal.get("defer_reason"): continue` BEFORE deferred_until check
   Check: `goal-selector.py` collect_blocked check 4b has NO `not deferred` guard (blocks regardless of deferred_until)
   IF any goal has defer_reason set:
       Bash: goal-selector.sh 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); ids=[g['goal_id'] for g in d] if isinstance(d,list) else []; print('PASS: defer_reason goals filtered')" → verify deferred goals absent from candidates
       Bash: goal-selector.sh blocked 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); dr=[g for g in d['blocked_goals'] if g['block_reason']=='deferred']; print(f'PASS: {len(dr)} deferred goals in blocked output')" → verify defer_reason goals appear in blocked

   # All-blocked idle path evidence checks (Section IWD)
   Check: `goal-selector.py` cmd_select prints JSON object with `all_blocked: true` when candidates empty but blocked goals exist
   Check: `goal-selector.py` cmd_select prints `[]` when no goals exist at all (not the object)
   Check: `aspirations-select/SKILL.md` Algorithmic Scoring has blocked-goals detection checking for `all_blocked` object
   Check: `aspirations-select/SKILL.md` Phase 2.5b exhaustion check is AFTER the FOR loop (not inside it)
   Check: `aspirations-select/SKILL.md` returns `selection_reason` with value `"all_blocked"` or `"all_blocked_by_gate"`
   Check: `aspirations/SKILL.md` return contract comment (line ~138) includes `selection_reason`
   Check: `aspirations/SKILL.md` all-blocked path is BEFORE the no-goals path (checked first)
   Check: `aspirations/SKILL.md` all-blocked path does NOT invoke create-aspiration, research-topic, or reflect (no busywork)
   Check: `aspirations/SKILL.md` all-blocked path has `sleep 600` with `timeout 600000` (10 min, matches Bash tool max)
   Check: `core/config/stop-skip-conditions.md` "All goals blocked" line mentions 10-minute wait
   Bash: goal-selector.sh 2>/dev/null | python3 -c "
import sys,json
d=json.load(sys.stdin)
if isinstance(d,dict) and d.get('all_blocked'):
    assert 'blocked_count' in d and 'by_reason' in d and 'blocked_goals' in d
    print(f'PASS: all_blocked format correct, {d[\"blocked_count\"]} blocked')
elif isinstance(d,list):
    print(f'PASS: normal array output, {len(d)} candidates')
else:
    print('FAIL: unexpected output format')
" → verify output format is correct (either array or all_blocked object)

   # Unreflected hypothesis safety net evidence checks (Section UH)
   Check: `aspirations-consolidate/SKILL.md` has Step 0.5 "Unreflected Hypothesis Sweep" between micro-hypothesis sweep and encoding queue
   Check: Step 0.5 calls `pipeline-read.sh --unreflected` then invokes `/review-hypotheses --learn`
   Check: Consolidation checklist includes `Step 0.5 Unreflected Hyp Sweep`
   Check: `aspirations-learning-gate/SKILL.md` has Phase 9.5c "Unreflected Hypothesis Check"
   Check: Phase 9.5c calls `pipeline-read.sh --unreflected` and invokes `--learn` when count > 0
   Check: Phase 9.5c is MANDATORY for all outcomes (not gated by productive-only)
   Bash: python3 core/scripts/aspirations.py --source agent read --id asp-001 2>/dev/null | python3 -c "
import sys,json; asp=json.load(sys.stdin); goals={g['id']:g for g in asp['goals']}
g8=goals.get('g-001-08',{})
ok = g8.get('skill')=='/review-hypotheses --learn' and g8.get('recurring')==True and g8.get('interval_hours')==2
print(f'PASS: g-001-08 exists, learn goal, 2h interval' if ok else 'FAIL: g-001-08 missing or misconfigured')
" → verify dedicated --learn recurring goal exists
   Check: `pipeline-read.sh --unreflected` flag exists and returns resolved records with reflected=false
   Check: `boot/SKILL.md` Step 1.5 runs `--resolve` only (learning is downstream — consolidation + learning-gate catch it)

   # Error response protocol evidence checks (Section AJ)
   Check: `.claude/rules/error-response.md` exists with blocker-centric model
   Check: `aspirations-execute/SKILL.md` Phase 4.1 uses `guardrail-check.sh` (NOT `guardrails-read.sh --active`)
   Check: `aspirations/SKILL.md` Phase 0.5a uses `guardrail-check.sh` (NOT `guardrails-read.sh --active`)
   Bash: guardrail-check.sh --context infrastructure --outcome succeeded --phase post-execution --dry-run → verify returns relevant guardrails
   Bash: guardrail-check.sh --context any --phase pre-selection --dry-run → verify returns relevant guardrails

   # Infrastructure health evidence checks (Section AL)
   Check: `.claude/rules/verify-before-assuming.md` exists with probe-before-concluding imperative
   Check: `core/scripts/infra-health.sh` exists and `core/scripts/infra-health.py` implements check/check-all/status/stale
   Check: `<agent>/infra-health.yaml` exists (components defined per domain deployment)
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
   Bash: grep -q "MANDATORY.*goal-selector" core/scripts/postcompact-restore.py → verify reminder present in source
   Check: CLAUDE.md Convention Index includes `goal-selection.md`
   Check: `world/knowledge/tree/system.md` has "Loop Integrity" section

   # Hybrid skill + completion report evidence checks (Section AN)
   Check: `.claude/skills/agent-completion-report/SKILL.md` has `user-invocable: true`
   Check: CLAUDE.md enforcement rule 1 does NOT list /agent-completion-report in the MUST NOT invoke enumeration
   Check: CLAUDE.md Skill Invocation Rules has "Hybrid skills" bullet
   Bash: agent-aspirations-read.sh --id asp-001 → verify g-001-04 skill references `/agent-completion-report` (or forged wrapper)

   # Board section in completion report (Section AN5)
   Check: `agent-completion-report/SKILL.md` Phase 2 has Step 10 reading board channels via `board-read.sh`
   Check: `agent-completion-report/SKILL.md` Phase 3 has "Message Board" section between "Knowledge" and "Active Work"
   Check: `agent-completion-report/SKILL.md` conventions list includes `board`

   # Report persistence (Section AN8)
   Check: `agent-completion-report/SKILL.md` tools_used includes `Write`
   Check: `agent-completion-report/SKILL.md` Phase 4 writes to `<agent>/reports/` and `<agent>/COMPLETION-REPORT.md`
   Check: `agent-completion-report/SKILL.md` Chaining Modifies lists `<agent>/reports/*.md` and `<agent>/COMPLETION-REPORT.md`
   IF `/agent-completion-report` was run during the test:
       Check: `<agent>/COMPLETION-REPORT.md` exists and is non-empty
       Check: `<agent>/reports/` directory exists with at least one `completion-report-*.md` file

   # Stop consolidation evidence checks (Section SC)
   Check: `stop/SKILL.md` RUNNING section invokes `/aspirations-consolidate with: stop_mode = true`
   Check: `stop/SKILL.md` does NOT contain "sensory_buffer" or "mini-consolidation" (old approach removed)
   Check: `aspirations-consolidate/SKILL.md` has `## Parameters` section with `stop_mode` documented
   Check: `stop_mode` parameter description lists ALL 6 skipped steps: 6, 7, 7.5, 8, 8.7, 10
   Check: Steps 6, 7, 7.5, 8, 8.7, 10 each have `(skip in stop_mode)` in their heading
   Check: Step 8.7 "Store user goal count" is indented inside the `IF stop_mode != true:` block
   Check: Steps 3 and 4 have `**MANDATORY**` annotation (must not be skipped even when data is empty)
   Check: `### Execution Checklist (MANDATORY)` section exists between Step 9.5 and Step 10
   Check: Execution Checklist lists 22 steps with valid states (done, empty, skipped variants)
   Check: Step 9 `known_blockers_active` comment references "Step 4 WM archive" (NOT `wm-read.sh` — WM was reset in Step 5)
   Check: Step 9 `knowledge_debts_pending` comment references "Step 2.25" (NOT `wm-read.sh`)
   Check: Step 9 `user_goals_pending` comment mentions stop_mode fallback to aspirations compact data
   Check: Step 8.65 has `IF meta/meta.yaml does not exist:` early exit with log message
   Check: Step 9.5 has `IF file does not exist:` early exit with log message
   Check: Overflow Queue Management has `IF file does not exist:` branch for overflow-queue.yaml
   Check: Step 2.6 has `# MANDATORY` comment for encoding weight adjustment
   IF a /stop was executed during the test:
       Check: `<agent>/session/handoff.yaml` exists (Step 9 ran during stop)
       Bash: wm-read.sh --json → verify working memory is reset (Steps 4-5 ran)
       Check: journal has "## Consolidation" entry with structured format (Step 3 ran)
       Check: output includes "CONSOLIDATION CHECKLIST:" with status for every step
   IF journal has any "## Consolidation" entry (from /stop OR end-of-loop):
       Check: entry uses structured format (contains "Observations processed:" and "Encoded to long-term:")
       Check: journal has WM archive entry in the same session (Step 4 ran before reset)

   # /start RUNNING guard (Section SC continued)
   Check: `start/SKILL.md` RUNNING branch blocks with error message and recovery instructions (no mode-downgrade path)
   Check: `CLAUDE.md` Session Start Protocol RUNNING branch shows error (does NOT invoke boot or auto-resume)
   Check: `CLAUDE.md` Enforcement Rule 6 says auto-resume is stop-hook-only, not Session Start Protocol

   # Mode ordering constraint (Section SC continued)
   # consolidation has minimum_mode: autonomous — mode must still be autonomous when it runs
   Check: `stop/SKILL.md` step 4 has `# MODE ORDER` comment about consolidation requiring autonomous mode
   Check: `stop/SKILL.md` step 5 (session-mode-set.sh reader) comes AFTER step 4 (consolidation invocation)
   Check: `aspirations-consolidate/SKILL.md` has note explaining minimum_mode ordering dependency with /stop

   # Precision encoding evidence checks (Section PE)
   Check: `core/config/conventions/precision-encoding.md` exists with Precision Manifest Schema section
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
   Bash: grep -r "world/conventions/precision-encoding" .claude/skills/ 2>/dev/null | wc -l → verify 0 (no stale path references; convention lives in core/config/conventions/)
   Check: CLAUDE.md Convention Index includes `precision-encoding.md`
   IF agent has run 3+ productive goal cycles since precision encoding was deployed:
       Check: at least 1 tree node has a "## Verified Values" section
       Bash: wm-read.sh encoding_queue --json → verify items include precision_manifest field
       Check: experience records have specific verbatim_anchors (not just "key error messages")

   # MR-Search integration evidence checks (Section BJ)
   Check: `core/config/aspirations.yaml` has `episode_chaining` section and `exploration_mode` section
   Check: `core/config/aspirations.yaml` `chain_on_outcomes` contains only `"failed"` (no blocked/surprise)
   Check: `core/config/memory-pipeline.yaml` `slot_types` includes `episode_chain`
   Check: `aspirations-execute/SKILL.md` Phase 4-chain has infrastructure guard before chaining
   Check: `aspirations-execute/SKILL.md` Phase 4.27 is positioned AFTER Phase 4.26 (not before)
   Check: `aspirations-execute/SKILL.md` Phase 4.26 reflection quality write uses read/append/set pattern (no --append flag)
   Check: `core/config/meta.yaml` reflection_quality_log comment says `{reflection_id, downstream_goal, helpful}`
   Check: `reflect/SKILL.md` Step 0.3 has `total >= 3` guard on reflection effectiveness
   Check: `reflect/SKILL.md` Step 5.8 uses `helpful` field (not `led_to_improvement`)
   Check: `aspirations-state-update/SKILL.md` Step 8.10 reads from `improvement-velocity.yaml` (not experience records)
   Check: `core/config/conventions/experience.md` notes `source_reflection_id` belongs on rb/guardrail records, not experience records
   IF any goal failed and was retried via episode chaining:
       Check: journal mentions "EPISODE CHAIN" with attempt count
       Check: working memory `episode_chain` is null after completion (cleaned up)
   IF any goal ran with `execution_mode: "exploration"`:
       Check: Step 5 evolution triggers were skipped for that goal
       Check: tree encoding still occurred (knowledge retained despite exploration mode)

   # Aspirations compact cache evidence checks (Section BE)
   Check: `core/scripts/aspirations.py` has `COMPACT_GOAL_KEEP` set and `compact_aspiration()` function
   Check: `core/scripts/load-aspirations-compact.sh` exists and follows `load-tree-summary.sh` pattern
   Check: `core/scripts/context-reads.py` has `TRACKED_FILES` list with `aspirations-compact.json`
   Check: `aspirations-select/SKILL.md` Phase 2.9 calls `aspirations-read.sh --id` for full goal detail
   Check: `aspirations-select/SKILL.md` Phase 2.9 comment says "do NOT remove this step"
   Bash: FULL=$(bash core/scripts/aspirations-read.sh --active 2>/dev/null | wc -c) && COMPACT=$(bash core/scripts/aspirations-read.sh --active-compact 2>/dev/null | wc -c) && echo "$FULL $COMPACT" → verify compact < full
   Bash: bash core/scripts/aspirations-read.sh --active-compact 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); g=d[0]['goals'][0]; assert 'id' in g; assert 'description' not in g; assert 'verification' not in g; print('OK')" → verify compact strips heavy fields
   Bash: rm -f <agent>/session/aspirations-compact.json && bash core/scripts/load-aspirations-compact.sh 2>/dev/null → verify returns path
   Check: 16+ skill files reference `load-aspirations-compact.sh` (grep count)
   Check: `boot/SKILL.md`, `backlog-report/SKILL.md`, `decompose/SKILL.md` KEEP `aspirations-read.sh --active` (need full detail)

   # First-principles thinking evidence checks (Section BK)
   Check: `.claude/rules/first-principles.md` exists with "When To Apply" scope limiter and 4 numbered rules
   Check: `core/config/spark-questions.yaml` has `sq-c07` with `category: first_principles` in both `seed_candidates` and `initial_state.candidates`
   Check: `core/config/meta.yaml` improvement_instructions has "First-Principles Analysis" section with System 2 guard
   Check: `aspirations-execute/SKILL.md` episode chain mini-reflection says "four questions" and question 4 mentions "ground truth"
   Check: `reflect-hypothesis/SKILL.md` Step 7 has first-principles escalation gated by "model-error" or "overconfidence"
   IF `meta/improvement-instructions.md` exists:
       Check: file retains "First-Principles Analysis" section (not removed by agent evolution)

   # SkillNet integration evidence checks (Section BL)
   # Skill Relation Graph
   Bash: skill-relations.sh read --composable boot → verify returns JSON array with prime and aspirations
   Bash: skill-relations.sh read --similar replay → verify returns relation with research-topic (symmetric)
   Check: `core/config/skill-relations.yaml` config section has `co_invocation_log_cap` and `discover_min_co_occurrences`
   Check: `core/scripts/skill-relations.py` reads thresholds from config (not hardcoded)

   # Skill Quality Evaluation
   Bash: skill-evaluate.sh report → verify returns JSON with skills, summary, alerts
   Check: `core/config/meta.yaml` strategy_schemas has `skill_quality` with file `meta/skill-quality-strategy.yaml`
   Check: `core/config/meta.yaml` initial_state has `skill_quality_strategy.dimension_weights` summing to 1.0
   Check: `aspirations-state-update/SKILL.md` has Step 8.76 (Skill Quality Assessment) calling skill-evaluate.sh
   Check: `core/scripts/skill-evaluate.py` evaluation entries use key `"overall"` (not "quality")
   Check: `core/scripts/skill-analytics.py` reads scores using key `"overall"` (matches evaluate writer)

   # Dynamic Skill Routing
   Bash: goal-selector.sh select 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print('OK' if not d or 'skill_affinity' in d[0].get('raw',{}) else 'MISSING')" → verify skill_affinity in output
   Check: `core/config/meta.yaml` initial_state.goal_selection_strategy.weights has `skill_affinity`
   Check: `decompose/SKILL.md` has "Skill Inference Refinement (Relation Graph)" section

   # Experience-to-Skill Mining and Curation
   Check: `core/config/skill-gaps.yaml` has `experience_mining` section and `quality_thresholds` section
   Check: `aspirations-consolidate/SKILL.md` has Step 7.5 (Experience-to-Skill Mining)
   Check: `aspirations-evolve/SKILL.md` has Step 9.5 (Skill Curation) calling skill-evaluate.sh underperforming

   # Co-Invocation Logging
   Check: `aspirations-execute/SKILL.md` has Phase 4.28 calling skill-relations.sh co-invoke
   Check: `forge-skill/SKILL.md` Constraints mentions skill-relations.sh --similar for dedup

   IF agent ran 5+ goals after SkillNet deployment:
       Bash: skill-evaluate.sh report → verify summary.total_skills_evaluated > 0
       Check: meta/skill-quality.yaml has entries under skills with evaluations[] and aggregate
       Check: <agent>/skill-relations.yaml co_invocation_log has entries (Phase 4.28 fired)

   # AutoContext-inspired subsystem evidence checks (Section BM)

   # Backpressure gate
   Check: `core/config/meta.yaml` strategy_schemas has `backpressure` with `regression_window`, `graduation_window`, `baseline_tolerance`, `max_active_monitors`
   Check: `core/config/meta.yaml` modifiable has `backpressure_regression_window`, `backpressure_graduation_window`, `backpressure_baseline_tolerance`
   Check: `core/config/meta.yaml` initial_state has `backpressure` with `version: 1`, `active_monitors: []`, `rollback_history: []`
   Bash: meta-backpressure.sh status → verify returns JSON with `active_monitors`, `rollback_history`, `active_count`, `total_rollbacks`
   Check: `meta-yaml.py` `cmd_set` has `is_rollback` guard checking for `"BACKPRESSURE ROLLBACK"` in reason
   Check: `meta-yaml.py` `_create_backpressure_monitor` does NOT have `import subprocess` (dead code was removed)
   Check: `aspirations-state-update/SKILL.md` has Step 8.85 calling `meta-backpressure.sh check`
   Check: `aspirations-evolve/SKILL.md` Step 0.7 has `meta-backpressure.sh cooldown-check`

   # Dead end registry
   Check: `meta-init.py` FILE_MAP does NOT include `dead-ends` (JSONL created by init-meta.sh, not meta-init.py)
   Check: `init-meta.sh` has `touch "$META/dead-ends.jsonl"`
   Bash: meta-dead-ends.sh read → verify returns JSON array (empty or with entries)
   Check: `meta-dead-ends.py` `cmd_check` status filter is `not in ("active", "reviewed")` (both block)
   Check: `meta-dead-ends.py` `cmd_add` dedup merge checks `in ("active", "reviewed")` (merges with reviewed too)
   Check: `aspirations-evolve/SKILL.md` Step 0.7 has `meta-dead-ends.sh read --active` before proposing changes
   Check: `aspirations-evolve/SKILL.md` Step 0.7 has `meta-dead-ends.sh increment` when dead end matched

   # Credit assignment
   Check: `core/config/meta.yaml` initial_state has `credit_assignment` with `version: 1`, `assignments: []`
   Check: `meta-impk.py` snapshot subcommand has `--active-changes` argument
   Check: `meta-yaml.py` `append_log` returns `mc_id` (meta change ID)
   Check: `meta-yaml.py` `next_meta_change_id` generates `mc-NNN` format from meta-log.jsonl
   Check: `aspirations-state-update/SKILL.md` Step 8.8 has `--active-changes` in meta-impk.sh call
   Check: `core/config/conventions/meta-strategies.md` documents credit assignment schema

   # Strategy generations
   Check: `core/config/meta.yaml` initial_state has `strategy_generations` with `version: 1`, `current_generation: 0`
   Bash: meta-generations.sh status → verify returns JSON with `current_generation`, `peak_generation`, `peak_score`
   Check: `meta-generations.py` `STRATEGY_FILES` and `meta-yaml.py` `_trigger_generation_transition` strategy_files list are in sync (same files)
   Check: Both lists include `skill-quality-strategy.yaml` (added post-SkillNet)
   Check: `meta-generations.py` `cmd_update` auto-opens generation 1 when none exists (no error JSON)
   Check: `aspirations-state-update/SKILL.md` Step 8.85 calls `meta-generations.sh update`

   # Curator quality gate
   Check: `core/config/memory-pipeline.yaml` has `curator_gate` section with `pass_threshold: 0.45`
   Check: `core/config/memory-pipeline.yaml` modifiable has `curator_gate_pass_threshold`, `curator_gate_coverage_weight`, `curator_gate_specificity_weight`, `curator_gate_actionability_weight`
   Check: `aspirations-state-update/SKILL.md` Step 8 has Step 8c.5 "CURATOR QUALITY GATE" between WRITE NARRATIVE and PRECISION AUDIT
   Check: Step 8c.5 has three structured questions (Q1 Coverage, Q2 Specificity, Q3 Actionability)
   Check: Step 8c.5 fail path writes to `wm-set.sh curator_overflow` (not direct tree write)

   # Weakness analysis
   Check: `reflect/SKILL.md` has Step 5.55 "Weakness Analysis" in --full-cycle section
   Check: Step 5.55 scans 4 signal sources: pattern_signatures, guardrails, experience, backpressure
   Check: Step 5.55 creates investigation goals for HIGH-severity active weaknesses
   Check: `aspirations-evolve/SKILL.md` Step 0.7 reads `<agent>/weakness-report.yaml`

   # Cross-subsystem integration
   Check: `core/config/conventions/meta-strategies.md` has sections: Backpressure Gate, Dead End Registry, Credit Assignment, Strategy Generations, Weakness Report, Curator Quality Gate
   Check: `core/scripts/_platform.sh` has `META_DIR="$(cygpath -m "$META_DIR")"` (Windows path fix)

   IF agent ran 5+ goals after AutoContext deployment:
       Bash: meta-backpressure.sh status → verify active_count or total_rollbacks reflect real monitoring
       Bash: meta-generations.sh status → verify current_generation >= 1 and current_goals > 0
       Bash: meta-impk.sh compute --window 5 --metric learning_value → verify entries include active_meta_changes field
       Check: at least some imp@k entries in improvement-velocity.yaml have `active_meta_changes` field
   IF any meta-strategy change was made via meta-set.sh during the test:
       Check: meta-log.jsonl entries have `meta_change_id` field (mc-NNN format)
       Check: backpressure.yaml has or had a monitor for that change
       Check: strategy-generations.yaml shows generation transition (current_generation > 1)
   IF any backpressure rollback occurred during the test:
       Check: rollback_history entry has `failed_value` and `total_goals_at_rollback` fields
       Check: journal mentions "BACKPRESSURE ROLLBACK"
       Check: if same field rolled back 2+ times, dead-ends.jsonl has an entry for it

   # External path configuration evidence checks (Section EP)
   # CRITICAL ordering: AGENT_NAME must be assigned BEFORE local-paths.conf sourcing.
   # Without this, $AGENT_NAME is empty and local-paths.conf is never sourced in bash.
   Bash: head -30 core/scripts/_paths.sh | grep -n "AGENT_NAME\|local-paths" → verify AGENT_NAME assignment appears BEFORE the source line
   Check: `core/scripts/_paths.sh` sources `<agent>/local-paths.conf` (not project-root)
   Check: `core/scripts/_paths.sh` WORLD_DIR and META_DIR always have a value (PROJECT_ROOT fallback)
   Check: `core/scripts/_paths.sh` WORLD_DIR uses priority: AYOAI_WORLD > WORLD_PATH > PROJECT_ROOT/world
   Check: `core/scripts/_paths.sh` META_DIR uses priority: AYOAI_META > META_PATH > PROJECT_ROOT/meta
   Check: `core/scripts/_paths.py` has `_read_local_paths()` reading from agent directory
   Check: `core/scripts/_paths.py` WORLD_DIR is always a valid Path (falls back to PROJECT_ROOT/world)
   Check: `core/scripts/_paths.py` META_DIR is always a valid Path (falls back to PROJECT_ROOT/meta)
   Check: `.gitignore` contains `*/local-paths.conf` (per-agent, not project-root)
   Check: `core/scripts/_platform.sh` cygpath for WORLD_DIR and META_DIR are conditional (guarded by -n check)
   Check: `start/SKILL.md` Phase A binds agent name, Phase B configures paths, Phase C asks for program
   Check: `start/SKILL.md` Phase B skipped when `<agent>/local-paths.conf` already exists
   Check: `start/SKILL.md` Phase B step B9-B10 adds Read/Write/Edit permissions to `settings.local.json`
   Check: `start/SKILL.md` Phase C sets state (C8) and invokes /prime (C8.5) BEFORE /create-aspiration (C9) for both assistant and autonomous modes
   Check: `core/scripts/session-save-id.sh` skips auto-resume if agent directory does not exist
   Check: No `factory-reset.sh` exists in core/scripts/
   Check: No `.claude/skills/reset/` directory exists
   Bash: grep -r "factory-reset" core/scripts/ .claude/skills/ 2>/dev/null | grep -v "verify-learning/SKILL.md" | wc -l → verify 0 results
   Bash: grep -r "/reset" CLAUDE.md .claude/rules/ 2>/dev/null | grep -v "git reset" | wc -l → verify 0 results
   IF <agent>/local-paths.conf exists:
       Check: contains WORLD_PATH= line pointing to a valid directory
       Check: contains META_PATH= line pointing to a valid directory
       Check: uses forward slashes (not backslashes)
   IF .claude/settings.local.json exists:
       Check: permissions.allow contains Read/Write/Edit rules for the configured WORLD_PATH (or Read(*)/Write(*)/Edit(*) wildcards)
       Check: permissions.allow contains Read/Write/Edit rules for the configured META_PATH (or Read(*)/Write(*)/Edit(*) wildcards)

   # Domain convention evidence checks (Section DC)
   # Post-execution convention: Phase 4.2 gates on this file every goal execution.
   # Path resolution: Bash: commands in SKILL files MUST use $WORLD_DIR (not hardcoded world/).
   #   "Read world/..." is LLM pseudocode (LLM resolves world/ to WORLD_DIR). But "Bash: test -f world/..."
   #   is a literal shell command where world/ would resolve relative to project root — which does not exist.
   Bash: source core/scripts/_paths.sh && test -f "$WORLD_DIR/conventions/post-execution.md" && echo "exists" → verify convention file exists at resolved WORLD_DIR
   Bash: bash core/scripts/load-conventions.sh post-execution → verify returns a non-empty path
   Check: `aspirations-execute/SKILL.md` Phase 4.2 `test -f` uses `$WORLD_DIR/conventions/` (not hardcoded `world/conventions/`)
   Check: `execute-protocol-digest.md` Phase 4.2 `test -f` uses `$WORLD_DIR/conventions/` (not hardcoded `world/conventions/`)
   Check: `start/SKILL.md` has Phase C0.5 "Configure domain conventions" between C0 and C1
   Check: `start/SKILL.md` Phase C0.5 only runs when `world/conventions/` has no `.md` files (existing world skips)
   Bash: bash core/scripts/guardrails-read.sh --id guard-006 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['status']=='active'; assert all(ord(c)<128 for c in d['rule']); print('OK')" → verify guard-006 exists, is active, has no non-ASCII
   IF agent ran goals after post-execution convention was deployed:
       Check: journal mentions "Committed and pushed" for goals that produced code changes
       Check: no pending-questions with status=pending about uncommitted changes
   # Pre-execution convention
   Bash: source core/scripts/_paths.sh && test -f "$WORLD_DIR/conventions/pre-execution.md" && echo "exists" → verify pre-execution convention exists
   Bash: bash core/scripts/load-conventions.sh pre-execution → verify returns a non-empty path
   Check: `aspirations-execute/SKILL.md` has Phase 3.9 "Pre-Execution Domain Steps" before Phase 4
   Check: `execute-protocol-digest.md` has Phase 3.9 before Intelligent Retrieval Protocol

   # File history evidence checks (Section FH)
   Check: `core/scripts/_fileops.py` has `save_history()` function
   Check: `core/scripts/_fileops.py` `save_history` resolves BOTH path and base_dir (`.resolve()` on lines 63-64)
   #   Without this, `relative_to` fails on Windows when path formats differ (forward vs back slashes, case)
   Check: `core/scripts/_fileops.py` has `acquire_lock()` and `release_lock()` functions
   Check: `core/scripts/_fileops.py` has `locked_write_jsonl()`, `locked_append_jsonl()`, `locked_write_json()`, `locked_write_yaml()`
   Check: `core/scripts/_fileops.py` ALL four `locked_write_*` functions have `path.parent.mkdir(parents=True, exist_ok=True)`
   Check: `core/scripts/_fileops.py` `append_changelog` uses `ensure_ascii=True` (must match locked writes)
   Check: `core/scripts/_fileops.py` `resolve_base_dir()` checks WORLD_DIR and META_DIR
   Check: `core/scripts/_fileops.py` locked writes skip history/changelog when `resolve_base_dir` returns None (agent-only paths)
   Check: `core/scripts/history.py` exists with list, restore, diff, prune subcommands
   Check: `core/scripts/history.py` `resolve_base_dir` imports from `_fileops` (single source of truth, not duplicate)
   Check: `core/scripts/history.py` `cmd_restore` acquires lock before overwriting (acquire_lock/release_lock in try/finally)
   Check: `core/scripts/history.py` `cmd_prune` groups entries > 30 days by ISO week key (date_key.isocalendar()[:2])
   #   Without ISO week grouping, the "one per week" retention policy silently behaves as "one per day"
   Check: `core/scripts/history-save.sh` passes args via sys.argv (not shell string interpolation)
   #   Without sys.argv, single quotes in summary text cause shell injection / SyntaxError
   Check: `core/scripts/history-list.sh`, `history-restore.sh`, `history-diff.sh`, `history-prune.sh`, `history-save.sh` all exist
   Check: `core/scripts/aspirations.py` `write_jsonl` delegates to `locked_write_jsonl` (not inline write)
   Check: `core/scripts/pipeline.py` `write_jsonl` delegates to `locked_write_jsonl`
   Check: `core/scripts/tree.py` `write_tree` uses `acquire_lock`/`release_lock` with `save_history`
   Check: `core/scripts/reasoning-bank.py` `write_jsonl` delegates to `locked_write_jsonl`
   Check: `core/scripts/pattern-signatures.py` `write_jsonl` delegates to `locked_write_jsonl`
   Check: `core/scripts/spark-questions.py` `write_jsonl` delegates to `locked_write_jsonl`
   Check: `core/scripts/meta-yaml.py` `write_yaml` delegates to `locked_write_yaml`
   Check: `core/scripts/meta-dead-ends.py` `write_all` delegates to `locked_write_jsonl`
   Check: `core/scripts/meta-experiment.py` `write_yaml` delegates to `locked_write_yaml`
   Check: `.gitignore` contains `*/.history/` pattern
   Check: `.gitignore` contains `*.lock` pattern
   IF world/.history/ exists:
       Check: at least one snapshot file exists with timestamp_agent.ext naming pattern
       Bash: source core/scripts/_paths.sh && bash core/scripts/history-list.sh "$WORLD_DIR/aspirations.jsonl" → verify lists versions or says "No history"

   # Message board evidence checks (Section MB)
   Check: `core/scripts/board.py` exists with post, read, channels subcommands
   Check: `core/scripts/board-post.sh`, `board-read.sh`, `board-channels.sh` all exist
   Check: `core/scripts/init-world.sh` creates `world/board/` with general, findings, coordination, decisions channels
   Check: `core/config/conventions/board.md` exists with schema and script API
   Check: `prime/SKILL.md` Phase 2 includes step reading board messages (board-read.sh --channel coordination)
   Check: `aspirations/SKILL.md` Phase 4 posts to coordination channel (board-post.sh --channel coordination)
   Check: `aspirations-execute/SKILL.md` has Phase 4.6 posting findings to board
   Check: `forge-skill/SKILL.md` Step 6 posts to `general` channel with `--tags forge,{name},{type}`
   Check: `forge-skill/SKILL.md` Step 9 does NOT send its own notification (comment says "already sent in Step 8")
   Check: `prime/SKILL.md` Phase 2 includes step reading forge announcements (board-read.sh --channel general --tag forge)
   Check: `core/config/conventions/board.md` Agent Integration Points lists forge-skill Step 6
   Check: `core/scripts/init-world.sh` creates `world/skill-catalog.yaml`
   Check: `forge-skill/SKILL.md` Step 7 writes to `world/skill-catalog.yaml` with forged_by, skill_path, companion_scripts_private fields
   Check: `forge-skill/SKILL.md` Step 8 invokes `/notify-user` with category `info` and subject "New Skill Forged"
   Bash: bash core/scripts/board-channels.sh → verify lists channels (or says no board)
   IF world/board/ exists with .jsonl files:
       Bash: echo "verify-learning test message" | bash core/scripts/board-post.sh --channel general → verify returns message ID
       Bash: bash core/scripts/board-read.sh --channel general --last 1 → verify shows the test message

   # Changelog evidence checks (Section CL)
   Check: `core/scripts/changelog.py` exists with read and stats subcommands
   Check: `core/scripts/changelog-read.sh` and `changelog-stats.sh` exist
   Check: `core/scripts/init-world.sh` creates empty `world/changelog.jsonl`
   Check: `core/scripts/_fileops.py` `append_changelog()` writes to `base_dir/changelog.jsonl`
   Check: `core/config/conventions/history.md` documents changelog schema
   IF world/changelog.jsonl exists and is non-empty:
       Bash: bash core/scripts/changelog-read.sh --last 5 → verify shows recent entries
       Bash: bash core/scripts/changelog-stats.sh → verify shows per-agent and per-file stats

   # CLAUDE.md and convention index evidence checks (Section CI)
   Check: CLAUDE.md Core Systems table has rows for: Message board, File history, Changelog, External paths, File operations
   Check: CLAUDE.md Convention Index has rows for: board.md, history.md, external-paths.md
   Check: CLAUDE.md mentions `<agent>/local-paths.conf` (per-agent, not project-root)
   Check: CLAUDE.md does NOT mention /reset in commands table or enforcement rules
   Check: `README.md` has "Removing Data" section with table (One agent, Shared knowledge, etc.)
   Check: `README.md` does NOT have "Resetting" section or mention factory-reset
   Check: `core/config/conventions/external-paths.md` exists and references `<agent>/local-paths.conf`
   Check: `core/config/conventions/board.md` exists
   Check: `core/config/conventions/history.md` exists

   # Script-level restriction evidence checks (Section SR)
   # Write/Edit deny rules in settings.json
   Check: `.claude/settings.json` deny list includes `Write(*/session/agent-state)` and `Edit(*/session/agent-state)`
   Check: `.claude/settings.json` deny list includes `Write(*/session/persona-active)` and `Edit(*/session/persona-active)`
   Check: `.claude/settings.json` deny list includes `Write(*/session/stop-loop)` and `Edit(*/session/stop-loop)`
   Check: `.claude/settings.json` deny list includes `Write(*/session/stop-block-count)` and `Edit(*/session/stop-block-count)`
   Check: `.claude/settings.json` deny list includes `Write(*.active-agent-*)` and `Edit(*.active-agent-*)`
   # Text rules in user-interaction.md
   Check: `.claude/rules/user-interaction.md` has `## Script-Level Restrictions` section
   Check: user-interaction.md lists `session-state-set.sh` restricted to /start and /stop only
   Check: user-interaction.md lists `init-mind.sh` restricted to /start and /boot (not /start only)
   Check: user-interaction.md allows `session-persona-set.sh true` via /boot but restricts `false` to /stop
   Check: user-interaction.md lists read-only scripts (`session-state-get.sh`, `session-persona-get.sh`, `session-signal-exists.sh`, `session-counter-get.sh`) as fully accessible
   Check: user-interaction.md lists `session-signal-set.sh loop-active`, `session-signal-clear.sh *`, `session-counter-clear.sh` as allowed write scripts
   # Agent spawning convention (Section AH)
   Check: `core/scripts/build-agent-context.py` exists
   Check: `core/scripts/build-agent-context.sh` exists
   Check: `core/config/conventions/agent-spawning.md` exists
   Check: `aspirations-execute/SKILL.md` Phase 4 delegation uses `build-agent-context.sh` — NOT "invoke /prime"
   Check: `aspirations-execute/SKILL.md` conventions front matter includes `agent-spawning`
   Bash: grep -c "invoke /prime" .claude/skills/aspirations-execute/SKILL.md 2>/dev/null → verify 0
   Check: `CLAUDE.md` Convention Index has `agent-spawning.md` row
   # Text rules in stop-hook-compliance.md
   Check: `.claude/rules/stop-hook-compliance.md` Rule 2 heading is "Never manually change state" (not old "Never manually set stop-loop")
   Check: stop-hook-compliance.md Rule 2 lists `session-state-set.sh`, `session-signal-set.sh stop-loop`, `session-counter-increment.sh`
   # Consistency: no stale references to removed factory-reset.sh
   Bash: grep -c "factory-reset" .claude/rules/user-interaction.md .claude/rules/stop-hook-compliance.md 2>/dev/null → verify 0 in both files

   # AVO-inspired plateau detection, trajectory view, cycle detection (Section AVO)
   Check: `core/config/aspirations.yaml` has `plateau_detection` section with `velocity_window`, `plateau_threshold`, `diminishing_returns_window`
   Check: `core/config/aspirations.yaml` has `cycle_detection` section with `lookback_window`, `checks`
   Check: `core/config/aspirations.yaml` modifiable section has bounds for `plateau_detection.velocity_window`, `plateau_detection.plateau_threshold`, `cycle_detection.lookback_window`
   Check: `core/scripts/aspiration-trajectory.py` exists
   Check: `core/scripts/aspiration-trajectory.sh` exists
   Bash: bash core/scripts/aspiration-trajectory.sh asp-004 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'plateau_detected' in d; assert 'current_velocity' in d; assert 'inflection_points' in d; print('OK')" → verify returns valid JSON with required fields
   Check: `core/scripts/aspiration-trajectory.py` `load_config` has NO fallback defaults (single source of truth is aspirations.yaml)
   Check: `core/scripts/aspiration-trajectory.py` guardrail attribution matches on goal ID only (NOT date substring)
   Check: `core/scripts/aspiration-trajectory.py` `find_aspiration` takes `asp_sources` as required parameter (no default, no file-based fallback)
   Bash: bash core/scripts/aspiration-trajectory.sh asp-004 asp-034 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'asp-004' in d and 'asp-034' in d; assert 'plateau_detected' in d['asp-004']; print('OK')" → verify multi-ID returns keyed object
   Check: `aspirations-evolve/SKILL.md` has Step 1.5 "Plateau Detection" between Step 1 and Step 2
   Check: `aspirations-evolve/SKILL.md` Step 1.5 collects qualifying_asp_ids then calls `aspiration-trajectory.sh` once (batch call, not per-aspiration loop)
   Check: `aspirations-precheck/SKILL.md` Phase 0.5b is Blocker Resolution Check (NOT cycle detection — 15+ external refs depend on this numbering)
   Check: `aspirations-precheck/SKILL.md` Phase 0.5c is Unproductive Cycle Detection
   Check: `aspirations-complete-review/SKILL.md` Phase 7.6 calls `aspiration-trajectory.sh` for maturity decisions
   Check: `reflect-extract-patterns/SKILL.md` has Step 3.5 "Trajectory-Level Pattern Extraction"

   # Pre-formation calibration gate evidence checks (Section CG)
   Check: `aspirations-spark/SKILL.md` sq-009 handler has Step 0.5 with `pipeline-read.sh --stage resolved`
   Check: `aspirations-spark/SKILL.md` Step 0.5 has "If total == 0: SKIP gate" (zero-data guard)
   Check: `aspirations-spark/SKILL.md` Step 0.5 confidence ceiling uses explicit boundary operators (>= and <), not ambiguous ranges
   Check: `aspirations-spark/SKILL.md` sq-009 handler has Step 0.7 "Adversarial pre-mortem" with confidence > 0.65 threshold
   Check: `aspirations-spark/SKILL.md` Step 0.5 does NOT read `confidence_calibration_bias` (single source of truth: resolved pipeline records)
   Check: `hypothesis-conventions.md` has "Pre-Formation Calibration Gate" section

   # Mid-session evolution + reflection obligation evidence checks (Section RE)
   Check: `core/config/evolution-triggers.yaml` has `evolution_goal_cadence` trigger with `goals_without_evolution: 15`
   Check: `core/config/evolution-triggers.yaml` `modifiable:` has `evolution_goal_cadence_goals` with bounds {min: 8, max: 30, default: 15}
   Check: `core/config/evolution-triggers.yaml` `initial_state.triggers` has `evolution_goal_cadence` entry
   Check: `aspirations/SKILL.md` Phase -0.5 initializes `productive_goals_this_session = 0` and `last_evolution_goal_count = 0`
   Check: `aspirations/SKILL.md` Phase -0.5 initializes `session_signals` with `routine_streak_global` and `productive_streak`
   Check: `aspirations/SKILL.md` `productive_goals_this_session += 1` is AFTER both per-goal and global anti-drift blocks (not inside signal tracking)
   Check: `aspirations/SKILL.md` global anti-drift threshold is 8 (comment explains relationship to per-goal threshold 5)
   Check: `aspirations/SKILL.md` Phase 9 Part A.1 computes `goals_since_last_evolution` and appends `evolution_goal_cadence` to triggers
   Check: `aspirations/SKILL.md` `session_signals` does NOT contain `corrections_this_session` or `confirmations_this_session` (removed: no population mechanism)
   Check: `aspirations-learning-gate/SKILL.md` Phase 9.8 exists with title "Full-Cycle Reflection Obligation"
   Check: `aspirations-learning-gate/SKILL.md` Phase 9.8 reads threshold from `meta/reflection-strategy.yaml` (not hardcoded)
   Check: `aspirations-learning-gate/SKILL.md` Phase 9.8 has team-aware deferral checking coordination board
   Check: `aspirations-learning-gate/SKILL.md` inputs include `productive_goals_this_session`
   Check: `aspirations-learning-gate/SKILL.md` chaining says "Phase 9.5-9.8" (not "9.5-9.7")
   Check: `aspirations/SKILL.md` chaining table says "Phase 9.5-9.8" for learning-gate
   Check: `meta/reflection-strategy.yaml` has `mode_preferences.full_cycle_cadence_goals`
   **Runtime**: After 15+ productive goals in a session, journal should show "OBLIGATION: full-cycle reflection" entry
   **Runtime**: After 15+ goals without evolution in a session, evolution log should show `evolution_goal_cadence` trigger

   # WM goal tracking evidence checks (Section WT)
   # State-update Step 3 must have explicit wm-append/wm-set calls (not prose directives).
   # guard-022 + rb-037 document the root cause: implicit writes don't survive autocompact.
   Bash: grep -c "wm-append.sh goals_completed_this_session" .claude/skills/aspirations-state-update/SKILL.md → verify >= 1 (explicit call exists)
   Bash: grep -c "wm-set.sh aspiration_touched_last" .claude/skills/aspirations-state-update/SKILL.md → verify >= 1 (explicit call exists)
   Check: `aspirations-state-update/SKILL.md` Step 3 wm-append uses dict format `{"goal_id":..., "aspiration_id":...}` (not bare string)
   Check: `goal-selector.py` streak_momentum comment references "aspirations-state-update Step 3" as data source
   Check: `goal-selector.py` streak_momentum uses `s.get("aspiration_id")` matching the dict format from Step 3

   # Retrieval escalation evidence checks (Section RX)
   Check: `core/config/conventions/retrieval-escalation.md` exists with "The Three Tiers" and "Mode Gates" sections
   Check: CLAUDE.md Convention Index includes `retrieval-escalation.md`
   Check: CLAUDE.md has "Knowledge Retrieval (All States)" heading (NOT "Knowledge Tree Retrieval")
   Check: `.claude/rules/user-interaction.md` has "Knowledge Retrieval (MANDATORY)" heading (NOT "Knowledge Tree Retrieval")
   Check: `.claude/rules/user-interaction.md` references `retrieval-escalation.md` convention
   Check: `respond/SKILL.md` conventions list includes `retrieval-escalation`
   Check: `respond/SKILL.md` Step 4 heading contains "Escalated Retrieval"
   Check: `respond/SKILL.md` Step 4 has Tier 1, Tier 2, Tier 3 subsections
   Check: `respond/SKILL.md` CRITICAL header does NOT say "tree retrieval" (says "3-tier escalation")
   Check: `aspirations-execute/SKILL.md` conventions list includes `retrieval-escalation`
   Check: `aspirations-execute/SKILL.md` has "Step 5a.1" (Tier 2) and "Step 5a.2" (Tier 3)
   Check: `aspirations-execute/SKILL.md` retrieval manifest schema includes `tiers_used` and `sufficient`
   Check: `aspirations-learning-gate/SKILL.md` conventions list includes `retrieval-escalation`
   Check: `aspirations-learning-gate/SKILL.md` Phase 9.5b has "Escalation quality check" block
   Check: `core/config/conventions/tree-retrieval.md` opens with cross-reference to `retrieval-escalation.md`
   Bash: grep -rl "Knowledge Tree Retrieval" CLAUDE.md .claude/rules/ .claude/skills/ 2>/dev/null → verify NO files (all renamed)

## Step 4: Summary Report

Provide a summary table:
- Total PASS / FAIL / N/A per section
- List of any FAIL items that need attention

## Chaining
- Calls: nothing
- Called by: User only. NEVER by Claude.
