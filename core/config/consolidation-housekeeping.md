# Consolidation Housekeeping Digest

<!-- Derived from aspirations-consolidate/SKILL.md Steps 2.6-10. Sync date: 2026-04-04 -->
<!-- If editing full SKILL.md steps 2.6-10, update this file to match. -->

Fast-path consolidation: all encoding queues were empty (precheck verdict: FAST).
Steps 0-2.25 (micro-hypothesis sweep, unreflected hypothesis sweep, encoding queue
processing, tree encoding, knowledge debt sweep) are **SKIPPED**.
If encoding work is discovered mid-consolidation, invoke `/aspirations-consolidate`.

## Parameters (inherited from caller)

- `stop_mode` (boolean) — skip Steps 7, 7.5, 8, 8.7, 10 (Step 6 tree rebalancing runs always)
- `session_count`, `goals_completed_this_session`, `evolutions_this_session` (from orchestrator)

---

**Step 0: Load Conventions** — `Bash: load-conventions.sh aspirations pipeline experience journal handoff-working-memory session-state tree-retrieval goal-schemas coordination`
Read only paths returned. If output is empty, all conventions already loaded.

## Step 2.6: Experience Archive + Encoding Weights [MANDATORY]

```
Bash: experience-archive.sh
Bash: experience-read.sh --meta → get by_type, by_category stats
IF script errors or returns empty: log "No experience metadata available" and continue
IF total_live + total_archived < 10:
    Log: "Encoding weight adjustment: insufficient data ({total} experiences, need >= 10)"
ELSE:
    Read core/config/memory-pipeline.yaml → encoding_weight_adaptation section
    Read world/memory-pipeline.yaml → current weight_performance_log
    IF world/memory-pipeline.yaml does not exist: log "No weight performance log yet — skipping"
    ELSE:
        Compare utility_ratio (>0.7 = high, <0.3 = low) against encoding weights used.
        Adjust ±0.05 toward high-utility weights, bounded by min/max from config.
        Log adjustment to weight_performance_log in world/memory-pipeline.yaml
```

## Step 2.7: Conclusion Quality Sweep [summary-only]

```
Bash: wm-read.sh conclusions --json
IF conclusions non-empty:
    total, negative, correct, wrong, pending = count by type/outcome
    avg_signals = mean evidence weights
    Log: "Judgment quality: {total} conclusions ({negative} negative), {correct} correct, {wrong} wrong, {pending} pending. Avg signals: {avg_signals:.1f}"
    # Precheck confirmed conclusions == 0. If wrong conclusions ARE found
    # here (race condition), invoke /aspirations-consolidate for full pipeline.
```

## Step 2.8: Pending Questions Re-evaluation

Bash-driven sweep replaces the old per-question LLM probe loop. The sweep
runs in O(N) string/date ops on alpha's data (~milliseconds, not seconds),
emitting a per-entry verdict the LLM acts on in one short pass.

```
Bash: pending-questions-sweep.sh sweep
# Parse JSON output (subcommand, summary, flags, counts, entries[]).
# Exit 0 = clean (no flags). Exit 1 = flags raised. Exit 2 = input error.
IF exit_code == 0 OR flags is empty:
    Report: "Pending questions: clean ({counts.total} entries, no action needed)"
ELSE:
    # Apply each verdict. The LLM is responsible for the YAML mutation —
    # the script is read-only by design (see header docstring).
    FOR entry in entries:
        IF entry.verdict == "auto_resolve":
            Edit agents/<agent>/session/pending-questions.yaml:
                Set status=resolved, resolution="<entry.reason> (auto-resolved by sweep)",
                resolved_at=<now>
        ELIF entry.verdict == "cleanup_only":
            # Already terminal-with-resolution, OR answered-with-answer.
            # For "answered with answer": flip status to resolved AND copy
            # the answer into resolution (single LLM-side YAML edit per
            # batch is fine — typically 14 in one pass for alpha).
            IF entry.status == "answered":
                Edit: status=resolved, resolution="<answer field copied>", resolved_at=<now>
            # ELSE: already resolved with resolution; nothing to mutate.
            #       The "cleanup_only" verdict surfaces them so the report
            #       counts include them but no YAML edit fires.
        ELIF entry.verdict in {"likely_resolved", "likely_stale"}:
            # Marginal cases — brief LLM judgment per entry. Read the entry
            # body, re-check the heuristic's reason against current world
            # state. Confirm or keep pending.
            IF confirmed:
                Edit: status=resolved, resolution="<entry.reason> (confirmed during consolidation)",
                resolved_at=<now>
            ELSE: keep status pending (will resurface in next sweep)
        ELIF entry.verdict == "flag_for_review":
            # >30d pending. Not auto-resolved. Surface in the report so the
            # user sees the count of long-pending entries on next /prime.
            (no edit)
    IF any status flipped to resolved: Bash: session-signal-set.sh pq-resolved
    Report: "Pending questions: auto={counts.auto_resolve}, cleanup={counts.cleanup_only},
             confirmed={count of likely_* the LLM accepted}, kept={count kept pending},
             review={counts.flag_for_review}, total={counts.total}"
```

Sweep heuristics (priority order, from `core/scripts/pending-questions-sweep.py`):

1. `cleanup_only` — already-terminal status with non-empty resolution
2. `cleanup_only` — status=answered with non-empty answer (just needs status flip)
3. `likely_resolved` — agent_answered + 7-day grace expired
4. `likely_stale` — infra-state question (PID/port/VRAM/etc.) + 14-day age
5. `likely_stale` — ritual entry (fresh-eyes-*) superseded by newer resolved sibling
6. `auto_resolve` — pending + no-op default_action + 14-day age (only auto-resolve)
7. `flag_for_review` — pending > 30 days (catch-all for unbounded growth)
8. `no_action` — fresh / no signal yet

The script is fail-open: malformed entries yield `no_action` with a reason rather than crashing.

## Step 2.9: Experience Distillation (runs on both paths)

```
# Reads from experience archive, NOT WM queues — independent of encoding state
Bash: experience-read.sh --type goal_execution --recent 30 --summary
Group experiences by tree_nodes_related field.

FOR EACH tree node with 3+ related experiences since last distillation:
    FOR EACH experience in cluster:
        Read agents/<agent>/experience/{exp.content_file}
        Extract: verbatim_anchors, key findings, exact values, failure sequences
    Read target tree node .md file
    Compose multi-paragraph synthesis preserving:
        - Specific technical detail (error messages, thresholds, sequences)
        - Patterns across experiences (what worked, what failed, why)
        - Decision rules with concrete conditions
        - Contradictions or evolution in understanding
    Edit target node .md with synthesized content
    Update node metadata: last_updated, article_count++
    Set last_update_trigger: {type: "experience-distillation", session: N}
    Check growth triggers (decompose_threshold, D_max)
    Log: "EXPERIENCE DISTILLATION: {node_key} enriched from {count} experiences"

Budget: max 5 nodes per consolidation (largest clusters first)
Report: "Experience distillation: {distilled_count} nodes enriched, {skipped_count} below threshold"
```

## Step 3 [MANDATORY]: Journal

```
Bash: wm-read.sh sensory_buffer --json
Log to journal (EXACT format, zeros for encoding fields):
"## Consolidation — {date}
Observations processed: {total_sensory_buffer}
Encoded to long-term: 0
Discarded: 0
Flagged for review: 0
Context gaps detected: 0
Judgment quality: {total_conclusions} conclusions, 0 wrong
Articles updated: []
Triage: lean (fast path)"
```

## Step 4 [MANDATORY]: WM Archive

```
Bash: wm-read.sh --json
Archive working memory to journal entry (summary only).
This captures remaining WM state before destruction.
```

## Step 5: WM Reset

`Bash: wm-reset.sh`

## Step 6: Tree Rebalancing (runs always, including stop_mode)

```
IF stop_mode == true:
    # Small fixed budget — drains structural debt incrementally without
    # making /stop slow. Caps live in core/config/tree.yaml `stop_mode_caps`
    # (default 5 decomposes, 2 distills, others ≤1). Lane D Magic Wand #4.
    Invoke /tree maintain --stop-mode
ELSE:
    Invoke /tree maintain (ALL ops: DECOMPOSE, REDISTRIBUTE, DISTILL, SPLIT, SPROUT, MERGE, PRUNE, RETIRE)
Report structural changes to journal
```

NOTE: Pre-2026-05-08 this step was a deferral path under stop_mode + tight context
("deferred (stop_mode+tight)") which let candidates accumulate. The new behavior
honors the meta-principle in `core/config/modes/autonomous.md` "Stop-mode does
work" — /stop pays a small wall-clock cost (≤90s) to drain a few candidates per
invocation, instead of a zero cost that lets debt grow forever.

## Steps 7-8: Skill Maintenance (skip in stop_mode)

```
IF stop_mode != true:
  7. Read meta/skill-gaps.yaml
     Report: new gaps, forge-ready gaps, dismissed gaps

  7.5. Bash: experience-read.sh --type goal_execution --recent 30 --summary
       Read meta/skill-gaps.yaml + core/config/skill-gaps.yaml
       Group by category+skill. Clusters of 3+ → register new gaps (max 3 per pass)
       Report: "{N} scanned, {M} new gaps, {K} strengthened"

  8. Read .claude/skills/_tree.yaml
     Bash: skill-evaluate.sh report
     Bash: skill-relations.sh discover
     Bash: skill-analytics.sh recommendations
     Report: active skills, gaps, quality summary, recommendations
```

## Step 8.5: Aspiration Archive Sweep

`Bash: aspirations-archive.sh`

## Step 8.6: Curriculum Gate Evaluation

`invoke /curriculum-gates`

## Step 8.65: Meta-Strategy Session Review

```
Read meta/meta.yaml — IF missing: log skip and continue
Read meta/improvement-velocity.yaml — IF missing: log skip and continue
session_imp_k = mean of this session's learning_value entries
delta = session_imp_k - overall_imp_k
IF |delta| > 0.1: log direction
Edit meta/meta.yaml: update overall_imp_k (rolling avg), last_session_imp_k, sessions_evaluated++
```

## Step 8.7: User Goal Recap (skip in stop_mode)

```
IF stop_mode != true:
    Bash: load-aspirations-compact.sh → IF path returned: Read it
    Filter goals where participants contains "user" and status != "completed"
    IF any: output visible recap with goal IDs and titles
    Store count for handoff
```

## Step 8.9: Release Held Claims

Enumerates BOTH queues and threads `--source` through to the release. Agent-queue
goals began carrying claims at g-306-238, so a world-only predicate — correct by
construction before that — now strands exactly the recurring cadence
(`g-001-01`..`g-001-10`) at session end. Full rationale in the authoritative copy;
see `aspirations-consolidate/SKILL.md` Step 8.9 (g-306-258).

```
# aspirations-query.sh, NOT aspirations-read.sh --active-compact: the query
# endpoint reads BOTH queues and tags each row with `source`, which is what makes
# the release below addressable. (verification-checklist.md item 30 already
# declares the query shape canonical for this step — this copy had drifted.)
Bash: MIND_AGENT={agent} aspirations-query.sh --goal-field claimed_by {agent_name} --goal-status pending,in-progress,blocked
FOR EACH returned goal:                      # NOT `FOR EACH world goal`
    Bash: aspirations-release.sh <goal-id> --source {goal.source}
echo "Session ending: released all held claims" | Bash: board-post.sh --channel coordination --type status
```

## Step 9: Continuation Handoff

```
Bash: goal-selector.sh → get top-ranked goal for next session
Write agents/<agent>/session/handoff.yaml:
  session_number: {session_count}
  timestamp: "{ISO 8601}"
  last_goal_completed: "{last goal id}"
  goals_in_progress: [{in-progress goal IDs}]
  hypotheses_pending: {from pipeline-read.sh --counts → active}
  next_focus: "{recommendation}"
  first_action:
    goal_id, score, effort_level, reason (from goal-selector.sh)
  decisions_locked:
    # Carry forward unexpired (session delta <= 3), classify kind: strategy|world_claim
    - decision, made_session, reason, kind, evidence_strength
  session_summary:
    goals_completed: {count}
    goals_failed: {count}
    key_outcomes: ["{outcome 1}", "{outcome 2}"]
  known_blockers_active:
    # From Step 4 WM archive data (WM was reset)
    - blocker_id, reason, affected_skills, detected_session
  knowledge_debts_pending: []  # None — fast path had no debts
  user_goals_pending:
    # From Step 8.7, or aspirations compact if 8.7 skipped
    count: {N}
    goals: [{id, title}]
  meta_state:
    improvement_velocity_trend: "{from Step 8.65}"
    Bash: meta-experiment.sh list --active → active_variant or null
    meta_changes_this_session: {count}
  consolidation_meta:
    triage_tier: "lean"
    # Increment counter — must match aspirations-consolidate/SKILL.md Step 9
    consecutive_lean_sessions: {prior_lean + 1}  # read from previous handoff, default 0
```

## Step 9.5: Transfer Profile Update

```
Read meta/experiments/completed-experiments.yaml
IF missing: log skip. ELSE: filter adopted → Edit meta/transfer-profile.yaml
```

## Execution Checklist (MANDATORY)

Output this checklist before Step 10. Each step: `done`, `empty`, `skipped (stop_mode)`, `skipped (file missing)`.

```
CONSOLIDATION CHECKLIST:
  Step 0  Micro-Hypothesis Sweep:  empty (fast path)
  Step 0.5 Unreflected Hyp Sweep:  empty (fast path)
  Step 1  Encoding Queue:          empty (fast path)
  Overflow Queue:                  empty (fast path)
  Step 2  Tree Encoding:           empty (fast path)
  Step 2.25 Knowledge Debt:        empty (fast path)
  Step 2.6  Experience Archive:    {done}
  Step 2.6  Encoding Weights:      {done|skipped (insufficient data)|skipped (file missing)}
  Step 2.7  Conclusion Quality:    {done|empty}
  Step 2.8  Pending Questions:     {done|empty|skipped (file missing)}
  Step 2.9  Experience Distill:    {done|empty}
  Step 3  Journal (structured):    {done}    ← MANDATORY
  Step 4  WM Archive:              {done}    ← MANDATORY
  Step 5  WM Reset:                {done}
  Step 6  Tree Rebalancing:        {done}
  Step 7  Skill Gap Review:        {done|skipped (stop_mode)}
  Step 7.5 Experience Mining:      {done|skipped (stop_mode)}
  Step 8  Skill Health:            {done|skipped (stop_mode)}
  Step 8.5 Aspiration Archive:     {done}
  Step 8.6 Curriculum Gates:       {done}
  Step 8.65 Meta-Strategy Review:  {done|skipped (file missing)}
  Step 8.7 User Goal Recap:        {done|skipped (stop_mode)}
  Step 8.9 Release Claims:         {done}
  Step 9  Handoff:                 {done}
  Step 9.5 Transfer Profile:       {done|skipped (file missing)}
```

## Step 10: Restart (skip in stop_mode)

```
IF stop_mode == true:
    Output: "Consolidation complete (stop mode — no restart)."
    RETURN
invoke /boot
# Boot detects handoff.yaml → continuation mode (abbreviated report → fast loop handoff).
```
