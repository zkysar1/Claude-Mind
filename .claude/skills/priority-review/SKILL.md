---
name: priority-review
description: "Priority dashboard — show ranked aspirations, accept reordering, update priorities"
user-invocable: true
triggers:
  - "/priority-review"
tools_used: [Bash, Read, Edit]
conventions: [aspirations, goal-selection]
minimum_mode: reader
---

# /priority-review — Aspiration Priority Dashboard

Shows all active aspirations ranked by aggregate goal score, and lets the user
reorder priorities interactively. Closes the feedback loop between autonomous
aspiration creation and user intent.

**Hybrid skill**: user-invocable AND agent-callable (from `/respond`). Valid from ANY state.
In reader mode: display only (no updates). In assistant/autonomous: full reordering.

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

4. Read <agent>/session/pending-questions.yaml
   Check for any entries with type: "priority-review" AND status: "pending"
   Note their IDs for Phase 4 consumption
```

## Phase 2: Render Dashboard

Build a numbered, ranked view showing both world and agent-local aspirations:

```
═══ PRIORITY REVIEW ═══════════════════════════

World Aspirations (shared queue — all agents):

 #  Pri    ID       Title                                          Goals    Source
 1. HIGH   asp-126  Close the Learning Loop: Fix Aggregate Co...   0/4      user
 2. HIGH   asp-127  Fix Ebbinghaus Memory Decay Mismatch (14...   0/3      user
 3. HIGH   asp-130  Strengthen the Adaptive Learning Moat          0/5      user
 4. HIGH   asp-128  Verify Strategy-to-Behavior Impact             0/4      user
 5. HIGH   asp-129  Bootstrap Environment Server Test Suite         0/6      user
 6. HIGH   asp-115  Recurring Infrastructure Monitoring             0/0+3r   agent
 7. MEDIUM asp-078  Audit and Improve Web App Admin Dashboard       9/10     agent
 8. MEDIUM asp-131  Production Observability: CloudWatch Metr...   0/4      user

Agent-Local Aspirations ({agent-name} private queue):

 #  Pri    ID       Title                                          Goals
 9. MEDIUM asp-L01  Framework maintenance and health checks         0/2+1r

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
1. Read the full aspiration record:
   Bash: aspirations-read.sh --id {asp-id}
   Parse the JSON output into a variable

2. Modify the priority field in the parsed JSON, then pipe the FULL aspiration back:
   echo '{full modified JSON}' | aspirations-update.sh {asp-id}
   # aspirations-update.sh requires COMPLETE aspiration JSON on stdin (full replacement, not merge)
```

After all updates:

```
IF any pending question has type: "priority-review" AND status: "pending":
  Read <agent>/session/pending-questions.yaml
  Update matching entries: set status to "answered", add answer field with summary of changes
  Write back the file via Edit
```

## Phase 5: Confirm and Record

```
1. Show updated ranking (re-run Phase 1-2 with fresh data):
   "Updated priorities:"
   {new dashboard}

2. Post to decisions board:
   Bash: echo '{"subject":"Priority review: {summary of changes}","tags":["priority-review"]}' | board-post.sh --channel decisions

3. Journal entry:
   Bash: journal-add.sh --type "priority-review" --summary "User reordered priorities: {changes}"

4. Output: "Priorities updated. These changes take effect on the next goal selection cycle."
```

## Chaining

- **Called by**: User (`/priority-review`), `/respond` (priority directive routing)
- **Calls**: `load-aspirations-compact.sh`, `aspirations-read.sh`, `agent-aspirations-read.sh`, `goal-selector.sh`, `aspirations-update.sh`, `agent-aspirations-update.sh`, `board-post.sh`, `journal-add.sh`
- **Reads**: `<agent>/session/pending-questions.yaml`, world aspiration compact data, agent aspiration data
- **Modifies**: Aspiration priorities in world queue (via `aspirations-update.sh`) and/or agent queue (via `agent-aspirations-update.sh`), pending-questions status, decisions board, journal
