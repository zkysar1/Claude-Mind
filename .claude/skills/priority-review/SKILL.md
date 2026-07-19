---
name: priority-review
description: "Shows the priority dashboard (all active aspirations ranked by aggregate goal score) and lets the user interactively reorder priorities to update the aspiration queue. Use whenever the user says \"what are you working on next\", \"re-rank my priorities\", \"show me the priority dashboard\", \"show the queue\", or invokes /priority-review directly. Closes the feedback loop between autonomous goal selection and user strategic direction. Valid in any mode including reader."
user-invocable: true
triggers:
  - "/priority-review"
tools_used: [Bash, Read, Edit]
conventions: [aspirations, goal-selection]
minimum_mode: reader
revision_id: "skill-bootstrap-priority-review-3714f6"
previous_revision_id: null
---

# /priority-review — Aspiration Priority Dashboard

Shows all active aspirations ranked by aggregate goal score, and lets the user
reorder priorities interactively. Closes the feedback loop between autonomous
aspiration creation and user intent.

**Hybrid skill**: user-invocable AND agent-callable (from `/respond`). Valid from ANY state.
In reader mode: display only (no updates). In assistant/autonomous: full reordering.

**Related**: This is the user's anytime portfolio **pull**. See
`.claude/skills/fresh-eyes-review/SKILL.md` for the every-25-goals scheduled
**push** that asks the Self + portfolio meta-questions together. Priority
ranking changes flow naturally from either entry point.

## Sub-commands

```
/priority-review                    — Show dashboard, accept reordering
/priority-review <user-input>       — Process priority feedback directly (from /respond routing)
```

## Phase 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Phase 1: Gather Data

```
1. World aspirations (shared queue — what all agents work on):
   Bash: load-aspirations-compact.sh
   IF path returned: Read the compact JSON
   Extract all active aspirations with: id, title, priority, status, scope, source, tags, progress

2. Agent-local aspirations (this agent's private queue):
   Bash: agent-aspirations-read.sh --active-compact
   Extract active agent aspirations (maintenance, decomposed sub-goals)

3. Bash: goal-selector.sh select
   Parse scored goal rankings to compute aggregate score per aspiration:
     For each aspiration, sum the scores of its eligible (pending/in-progress) goals
     Sort aspirations by: aggregate score (descending)

4. Read agents/<agent>/session/pending-questions.yaml
   Check for any entries with type: "priority-review" AND status: "pending"
   Note their IDs for Phase 4 consumption

5. OHS-delta rollup per aspiration (g-245-03)
   Bash: meta-read.sh improvement-velocity.yaml
   → Parse YAML → velocity_map[goal_id] = ohs_delta_since_previous_ohs_run
   → For each aspiration, sum numeric deltas across its completed goals
   → ohs_delta_map[asp_id] = signed 2-decimal float OR "n/a" (when no numeric deltas)
   → Display: "+0.45" / "-0.12" / "n/a" (compact — reports are width-constrained)
   → Gracefully handle entries missing the field (pre-schema data) as 'no_ohs_data'
```

## Phase 1.5: Score-Priority Mismatch Detection (g-244-19)

Detects aspirations whose aggregate score persistently exceeds the median score
of HIGH-priority aspirations — a signal that EITHER the priority field is mis-set
OR the score formula is over-weighting that aspiration's work pattern. Persistence
requires 3+ consecutive priority-review runs above the threshold (avoids
exploration_noise false positives from single observations). Either signal is
useful: the user decides whether to bump priority or whether the formula needs
tuning. Skips silently when fewer than 3 HIGH-priority aspirations exist (no
stable median to compare against).

State persists across runs in `meta/priority-review-mismatch-history.yaml`,
maintained exclusively by `core/scripts/priority-review-mismatch.py`.

```
# Build the input payload from Phase 1 step 3 aggregate scores. Include BOTH
# world and agent-local aspirations — score-priority drift can occur in either
# queue. Each entry: {asp_id, priority, score}
payload_json = json-encode the list of {asp_id, priority, score} for all active aspirations

# Run the detector. It updates history (idempotent — safe to call multiple times).
echo "$payload_json" | Bash: priority-review-mismatch.sh
parsed = JSON output
flagged_map = {f.asp_id: f for f in parsed.flagged}
high_median = parsed.high_median  # may be null if <3 HIGH aspirations
```

Flagged aspirations are surfaced as a sub-line under their dashboard row in
Phase 2. The history file purges entries automatically when an aspiration drops
at-or-below the HIGH median, becomes HIGH-priority itself, or becomes inactive.

## Phase 2: Render Dashboard

Build a numbered, ranked view showing both world and agent-local aspirations:

```
═══ PRIORITY REVIEW ═══════════════════════════

World Aspirations (shared queue — all agents):

 #  Pri    ID       Title                                       Goals    OHS Δ   Source
 1. HIGH   asp-126  Close the Learning Loop: Fix Aggregate...   0/4      n/a     user
 2. HIGH   asp-127  Fix Ebbinghaus Memory Decay Mismatch ...    0/3      n/a     user
 3. HIGH   asp-130  Strengthen the Adaptive Learning Moat       0/5      +0.12   user
 4. HIGH   asp-128  Verify Strategy-to-Behavior Impact          0/4      -0.08   user
 5. HIGH   asp-129  Bootstrap Environment Server Test Suite     0/6      n/a     user
 6. HIGH   asp-115  Recurring Infrastructure Monitoring         0/0+3r   n/a     agent
 7. MEDIUM asp-078  Audit and Improve Web App Admin Dash...     9/10     +0.45   agent
 8. MEDIUM asp-131  Production Observability: Monitoring ...    0/4      n/a     user

Agent-Local Aspirations ({agent-name} private queue):

 #  Pri    ID       Title                                       Goals    OHS Δ
 9. MEDIUM asp-L01  Framework maintenance and health checks     0/2+1r   n/a

To reorder:
  - "asp-125 should be HIGH" / "make 3 higher than 1"
  - "drop asp-131 to LOW" / "swap 2 and 5"
  - "looks good" to confirm current ranking
═══════════════════════════════════════════════
```

Rules for the dashboard:
- **World section**: Sort by aggregate goal score (descending) — this reflects what the goal selector would actually pick next
- **Agent-local section**: Shown separately below. These are the current agent's private tasks (maintenance, decomposed sub-goals). Numbered continuously after the world list.
- Show priority level (HIGH/MEDIUM/LOW), aspiration ID, title (truncated to fit), goal progress (completed/total + recurring count), source (user/agent for world; omitted for agent-local)
- If an aspiration was created in the current session, mark it with `NEW`
- If a `type: priority-review` pending question exists, note it above the table: "Priority review requested — your input shapes what I work on next."
- **OHS Δ column** (g-245-03): signed 2-decimal float when aspiration has completed
  goals with numeric OHS deltas in `meta/improvement-velocity.yaml`
  (`ohs_delta_since_previous_ohs_run`). Shows `n/a` when no numeric deltas
  exist yet (aspirations completed before `ohs-trend.jsonl` started
  populating, or recent work where OHS runs haven't bracketed the
  completion timestamp). This is the "does this aspiration actually
  move product quality" signal — pair it with priority to detect
  aspirations that feel urgent but don't shift product outcomes.
- **Score-priority mismatch flag** (g-244-19): when `flagged_map` from Phase 1.5
  contains the row's aspiration id, append a sub-line under that row:
  ```
      Δ priority candidate: aggregate score persistently exceeds HIGH median (run_count={f.run_count}, score={f.score} vs HIGH-median {f.high_median})
  ```
  The flag means: either bump priority or revisit the score formula. Either
  decision is valuable. Indented two spaces under the row to keep the table
  alignment clean. Renders for BOTH world and agent-local sections (mismatches
  occur in either queue).

## Phase 3: Present

Output the dashboard to the terminal.

IF the skill was called with user input already (from `/respond` routing with arguments):
  Skip presentation — go directly to Phase 4 with the provided input.
  (The user already saw the context in conversation; showing the full dashboard is redundant.)

IF mode is `reader`:
  Output the dashboard, then: "Read-only mode — switch to assistant or autonomous to reorder."
  DONE — skip Phases 4-5.

## Phase 4: Process User Input

Parse the user's natural language reordering intent. Common patterns:

| User says | Interpretation |
|-----------|---------------|
| "asp-125 should be HIGH" | Set asp-125 priority to HIGH |
| "make asp-125 HIGH, asp-131 LOW" | Set multiple priorities |
| "drop 8 to LOW" | Use dashboard rank number, set to LOW |
| "swap 2 and 5" | Exchange priorities of ranked items 2 and 5 |
| "1 is most important" | Confirm rank 1 as HIGH (may already be) |
| "looks good" / "confirm" | No changes — mark pending question as answered |
| "asp-125 is more important than asp-126" | Set asp-125 priority >= asp-126's; if equal, bump asp-125 up |

For each priority change:

```
1. Apply the priority change directly — field-merge, no read-modify-write needed:
   Bash: aspirations-update.sh {asp-id} priority {new-priority} --source world
   # Positional <asp_id> <field> <value>. The daemon (update_aspiration) merges
   # ONLY the named field — `asp[field] = value` — preserving all other
   # aspiration fields. The pre-2026-05-14 stdin full-replacement form is retired.
   # --source world: this path handles WORLD-queue rows (the dashboard's agent-local
   # rows route through agent-aspirations-update.sh, which forces --source agent).
```

After all updates:

```
IF any pending question has type: "priority-review" AND status: "pending":
  Read agents/<agent>/session/pending-questions.yaml
  Update matching entries: set status to "answered", add answer field with summary of changes
  Write back the file via Edit
```

## Phase 5: Confirm and Record

```
1. Show updated ranking (re-run Phase 1-2 with fresh data):
   "Updated priorities:"
   {new dashboard}

2. Post to decisions board (single source of observability — the board post
   is cross-agent visible and archived; a separate journal-add here was
   redundant AND was silently failing on the argv form, since journal-add.sh
   requires stdin JSON):
   Bash: echo '{"subject":"Priority review: {summary of changes}","tags":["priority-review"]}' | board-post.sh --channel decisions

3. Output: "Priorities updated. These changes take effect on the next goal selection cycle."
Bash: echo "priority-review phase documented"
```

## Chaining

- **Called by**: User (`/priority-review`), `/respond` (priority directive routing)
- **Calls**: `load-aspirations-compact.sh`, `aspirations-read.sh`, `agent-aspirations-read.sh`, `goal-selector.sh`, `aspirations-update.sh`, `agent-aspirations-update.sh`, `board-post.sh`, `journal-add.sh`
- **Reads**: `agents/<agent>/session/pending-questions.yaml`, world aspiration compact data, agent aspiration data
- **Modifies**: Aspiration priorities in world queue (via `aspirations-update.sh`) and/or agent queue (via `agent-aspirations-update.sh`), pending-questions status, decisions board, journal

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
When called mid-loop (`/respond` priority routing), the terminal action is
`aspirations-update.sh`, `journal-add.sh`, or `board-post.sh`. Never end with a
text summary of reorderings.
