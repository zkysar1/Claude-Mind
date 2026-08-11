---
name: sprint-planning
description: "Full sprint-planning exercise over the world + agent queues: runs /backlog-report, gathers the priority dashboard, probes every live agent's selector vantage against standing directives, sweeps hygiene lanes (duplicates, routing, priority mismatches, recurring health, reclaim-routed-work, zombie aspirations), applies every change through canonical scripts with live-state verification and a write ledger, and publishes the plan (decisions board + coordination heads-up + per-agent queues). Use when the user says \"sprint planning\", \"plan the sprint\", \"clean up the backlog and set priorities\", or when the recurring sprint-planning goal fires. User-invocable AND agent-callable."
user-invocable: true
triggers:
  - "/sprint-planning"
  - "sprint planning"
  - "plan the sprint"
tools_used: [Bash, Read, Write, Edit, Skill]
conventions: [aspirations, goal-schemas, goal-selection, coordination, board]
minimum_mode: assistant
revision_id: "skill-bootstrap-sprint-planning-2026-08-10"
previous_revision_id: null
---

# /sprint-planning — Fleet Sprint Planning Exercise

Turns the two report skills into a full planning pass: measure → analyze →
verify → apply → publish. A plan that changes no queue state is a report, not
a plan (asp-353 directive lineage) — but every queue change must survive a
live-state verification first. Formalized 2026-08-10 from a user-directed
sprint session (74-agent ultracode pass, 276 proposed changes, 28 refuted by
adversarial verification — the refutation rate is why Phase 4's verify step
is not optional).

**Hybrid skill**: user-invocable AND agent-callable (the recurring
sprint-planning goal invokes it in standard mode). Requires assistant or
autonomous mode — it writes queue state.

## Sub-commands

```
/sprint-planning            — Standard pass: inline analysis, bounded lanes
/sprint-planning --ultra    — USER-INVOKED ONLY: authorizes a multi-agent
                              Workflow fan-out (per-aspiration analysts +
                              adversarial verifiers). An agent-initiated
                              (recurring-goal) run MUST NOT pass --ultra:
                              Workflow orchestration requires explicit user
                              opt-in, and a recurring firing is not one.
```

## Phase 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Phase 1: Foundation Reports

```
1. Invoke Skill(backlog-report) — produces agents/<agent>/BACKLOG.md and the
   structural indexes this skill reuses (score map, blocked map, user goals,
   recurring health, testable hypotheses).

2. Priority dashboard data (inline — do NOT invoke /priority-review here; its
   Phases 3-4 are interactive and this skill's reorder decisions come from
   Phase 3 analysis instead):
   Bash: load-aspirations-compact.sh → Read returned path
   Bash: goal-selector.sh select → scored_goals (NOTE: output is a bare JSON
     array, not a dict) → aggregate score per aspiration
   Bash: echo '<[{asp_id,priority,score}...]>' | priority-review-mismatch.sh
     → flagged score-priority mismatches (needs 3+ consecutive runs to flag)

3. Snapshot for the ledger: record counts (active aspirations, non-terminal
   goals, selectable goals, blocked, user-routed, overdue recurring) BEFORE
   any write — these are the plan's before/after evidence.
```

## Phase 2: Fleet Vantage & Directive Check

The selector's ranking from ONE agent's vantage is not the fleet's. Standing
directives fail silently when scorer terms outvote them (measured: a
class-balance term at -2.24 defeating a directive boost of +1.50 made the
directive's aspiration invisible to 4 of 5 agents while every honor banner
reported it fired).

```
1. Roster: team-state-read.sh --json → agent_status keys = live agents.
   For staleness questions use liveness-check.sh — never conclude a partner
   dormant from last_active alone (check-team-state-before-silent.md).

2. Per-agent vantage probe (bounded — top ~20 each):
   FOR each live agent A:
     Bash: MIND_AGENT=<A> bash core/scripts/goal-selector.sh select
     Record: top goals, and whether any standing-directive lane surfaces.

3. Directive verdict: for each standing directive (strategic-focus entries,
   directive board posts, program-level mandates): is its work EXECUTABLE
   (pending, unblocked, routed to a live agent) and VISIBLE (ranked where an
   obeying agent would pick it)? Three verdicts:
     exhausted   → propose retiring the directive (evidence: zero executable)
     enforced    → nothing to do
     defeated    → the directive is live but structurally loses in the scorer
                   → file/raise the scorer-fix goal; do NOT hand-reorder
                   around it every sprint (the scorer-sovereignty gate's own
                   message: tune the scorer, don't route around it)
```

## Phase 3: Hygiene Lanes (bounded, analysis only — no writes yet)

Each lane emits PROPOSED changes into a working list `{change_id, kind,
target, from, to, evidence}`. Bound every lane (top-K candidates); a sprint
is a pass, not an exhaustive audit — note what was NOT swept (no silent
caps).

```
L1 DUPLICATES: title-similarity over pending goals (bounded ≤30 pairs/run).
   Proposal shape: close loser via skip with outcome_note naming the keeper
   + a run stamp (sprint-YYYY-MM-DD). Known-twin pairs from prior runs first.

L2 ROUTING: intended_agent not in live roster (dead agents, 'any',
   deployment names) → re-route; role-pin violations (an agent's self.md
   pins, e.g. a no-code pin) → move implementation goals to a capable agent;
   goals locked to one agent that any agent can do → widen to 'either'.

L3 PRIORITY: mismatch-detector flags + directive alignment + severity
   inversions (silent-data-loss defects at LOW, tail-work at HIGH). Metric
   honesty per guard-2829: EXCLUDE single-goal cross-world import wrappers
   from any completion-ratio evidence.

L4 RECURRING HEALTH: overdue-by list from Phase 1. A stopped recurring has
   TWO distinct causes needing OPPOSITE fixes (guard-2004): cadence fiction
   (interval smaller than reality → widen, with the overdue evidence) vs
   execution wedge (goal broken → file Unblock, do NOT widen to hide it).

L5 RECLAIM ROUTED WORK (.claude/rules/reclaim-routed-work.md — both axes:
   premise AND rule):
   Q: pending-questions — answer with evidence where possible; an answered
      question is TERMINAL, never batch-close without evidence per question.
   P: participants:[...user] goals — capability checklist per
      capability-before-user.md; drop the user leg ONLY on a verified agent
      path; NEVER auto-drop on a fuzzy/prose match (under-match; removing
      the human is the irreversible direction). Record user_leg_scope on
      keeps.
   B: deferred/blocked — re-derive, don't re-read; a well-written
      defer_reason is not evidence it is still correct.

L6 ZOMBIE ASPIRATIONS: completion_ratio ≥ 0.8 with only blocked/stale
   remainder → route to aspirations-complete-review Phase 7.4 (intent
   satisfaction), or propose retire with goal transplant for genuinely-live
   stragglers. Single-goal wrapper aspirations whose twin is resolved →
   retire.
```

**--ultra variant**: when (and only when) the user invoked with `--ultra`,
Phases 2-3 may run as a Workflow: per-aspiration analyst agents + cross-cut
lane agents, then one adversarial verifier per proposed change (schema-forced
verdicts), then synthesis. Keep verifier prompts refutation-framed. Expect
~10% refutation — that is the pass working, not failing.

## Phase 4: Verified Apply

Every proposed change passes a LIVE re-check immediately before its write —
analysis-time state is stale by apply time (measured refutations: targets
changed status mid-analysis).

```
FOR each proposed change (canonical scripts ONLY — never edit stores):
  1. Live re-check: aspirations-query.sh / fresh read of the target.
     NOTE: query projections omit most fields (only asp_id, category,
     goal_id, source, status, title) — a None for an unprojected field is
     NOT evidence; read the update echo or a full dump for field values.
     NOTE: goal id field in query output is `goal_id`; archived goals
     VANISH from live queries (check --archive before concluding absence).
  2. Closure guards: refuse skip/expire when target is in-progress, is
     terminal, is absent from the live set, or is referenced by any live
     goal's blocked_by (guard-1690). Recurring retire = recurring=false +
     status=expired + outcome_note (guard-1031) — never bare skip.
  3. Write via aspirations-update-goal.sh / aspirations-add-goal.sh /
     aspirations-update.sh / agent-aspirations-update.sh. After add-goal,
     re-query the REAL id via --title-contains (never parse ids out of
     echoed descriptions). After routing writes, read back intended_agent.
  4. Ledger EVERY write (OK/FAIL + note) to a JSONL in the session scratch
     dir (agents/<agent>/sessions/<SID>/scratch/). Retry lock-contention
     FAILs once; document unresolved FAILs in the plan.
  5. Every closure's outcome_note names where the work lives now.
```

## Phase 5: Publish

```
1. PLAN-VS-STORE CONSISTENCY CHECK (do this BEFORE publishing): every goal
   the plan names as a top item must have store state matching the plan
   (priority, intended_agent, status). A plan that says "X is agent A's #1"
   while X sits at MEDIUM/either is a contradiction shipped to the fleet.

2. Decisions board (required outcome of the recurring goal):
   Bash: echo '{"subject":"Sprint planning YYYY-MM-DD: <tallies + verdicts>","text":"<D-verdicts, apply tallies, per-agent top 2-3 for ~3 days>","tags":["sprint-planning"]}' | board-post.sh --channel decisions

3. Coordination heads-up when writes touched other agents' lanes or claims:
   what closed, what moved, what each agent should pick up next. Routing
   moves the fleet via intended_agent — a board post alone routes nothing
   (rb-7368); post the heads-up AND make the field writes.

4. Escalate only genuine human decisions: pending-questions entries or
   participants:[user] goals with user_leg_scope, batched into the plan's
   "needs you" list — never a blocking ask.

5. Every ~10th firing (recurring-goal counter): widen into the full
   portfolio-shape briefing. Notify the user about the sprint plan.
   (Check world/forged-skills.yaml for a skill whose triggers match
   "notify the user" and invoke it with a short subject and message. If no
   matching skill is registered, fall back to a `participants: [agent, user]`
   goal via aspirations-add-goal.sh. Never block on notification failure.)

6. User-invoked sessions: optionally publish the plan as an artifact
   (utilitarian treatment; stats band, directive verdicts, what-changed,
   per-agent queues, needs-you list, risks, provenance).
```

## Phase 6: Self-Review (fresh eyes before signing off)

Re-probe the riskiest write classes from LIVE state (not the ledger, not
memory): closures vs live blocked_by references; recurring shapes on
anything expired; team-state in_flight vs closed set (never close under a
live claim); spot-read patched fields. Fix what the probes falsify, then
re-run the Phase 5.1 consistency check. This phase exists because the
formalizing session's own review caught its plan/store contradiction only
here.

## Scale & Cadence Rules

- Standard mode is BOUNDED: lanes cap their candidate lists, the vantage
  probe reads top-20 per agent, and un-swept remainder is named in the plan.
- The recurring goal (world queue, asp-353 lineage) fires this skill in
  standard mode. Interval management belongs to the recurring goal's fields,
  not this skill.
- Depth escalation (--ultra) is a USER decision per the Workflow opt-in
  policy. When the backlog has visibly drifted (dup candidates > ~50,
  directive defeated, portfolio inversion), SAY SO in the plan and suggest
  the user run `/sprint-planning --ultra`.

## Anti-patterns

- Applying analysis-time conclusions without the live re-check (the measured
  ~10% refutation rate is the cost of skipping it)
- Closing a goal referenced by a live blocked_by, or under a live claim
- Parsing new-goal ids from add-goal's echoed description text
- Treating a query projection's missing field as a field value
- Auto-dropping the user participant on a fuzzy match
- Publishing a plan whose named priorities don't match store state
- Passing --ultra from an agent-initiated run
- A "sprint" that only reports: at least one concrete queue action, or an
  explicit no-change rationale, every firing

## Chaining

- **Called by**: User (`/sprint-planning`), recurring sprint-planning goal
  (standard mode), `/respond` (sprint-planning directive routing)
- **Calls**: `Skill(backlog-report)`; scripts: `load-aspirations-compact.sh`,
  `goal-selector.sh`, `priority-review-mismatch.sh`, `team-state-read.sh`,
  `liveness-check.sh`, `aspirations-query.sh`, `aspirations-update-goal.sh`,
  `aspirations-add-goal.sh`, `aspirations-update.sh`,
  `agent-aspirations-update.sh`, `pending-questions-sweep.sh`,
  `audit-user-to-agent.sh`, `audit-deferred-defers.sh`, `board-post.sh`
- **Modifies**: world + agent aspiration queues (via scripts), pending
  questions, decisions/coordination boards, session-scratch ledger

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call,
not text. When called mid-loop, the terminal action is the Phase 5 board
post (or the ledger-append Bash call when no post was warranted). Never end
with a text summary of the plan.
