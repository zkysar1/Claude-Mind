---
name: backlog-report
description: "Sprint planning backlog — aspirations, goals, scores, blockers, and user action items as copy-pasteable markdown"
user-invocable: true
triggers:
  - "/backlog-report"
tools_used: [Bash, Read, Write]
conventions: [aspirations, goal-schemas, goal-selection, pipeline]
minimum_mode: reader
---

# /backlog-report — Sprint Planning Backlog

Generates a complete, copy-pasteable markdown backlog of all aspirations, goals,
scores, blockers, and user action items. Writes `<agent>/BACKLOG.md` and displays a compact terminal summary.

**Hybrid skill**: user-invocable AND agent-callable. Valid from ANY state.
Safe: read-only with respect to agent state (only writes the output file).

## Sub-commands

```
/backlog-report              — Generate full backlog report
```

## Phase 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Phase 1: Gather Data

Run these in parallel where possible:

```
1. Active aspirations (full detail)
   Bash: aspirations-read.sh --active
   → Parse JSON → store as aspirations[]

2. Scored goal rankings
   Bash: goal-selector.sh select
   → Parse JSON → store as scored_goals[]

3. Blocked goals with diagnostics
   Bash: goal-selector.sh blocked
   → Parse JSON → store as blocked_data

4. Pending questions (user review items)
   Read: <agent>/session/pending-questions.yaml
   → Parse YAML → filter status == "pending" → store as pending_questions[]
   → IF file missing or empty: pending_questions = []

5. Active hypotheses
   Bash: pipeline-read.sh --stage active
   → Parse JSON → store as active_hypotheses[]
```

## Phase 2: Build Indexes

```
1. SCORE MAP — For each goal in scored_goals[]:
     score_map[goal_id] = {score, breakdown, category, recurring, recurring_urgency}

2. BLOCKED MAP — For each goal in blocked_data.blocked_goals[]:
     blocked_map[goal_id] = {reason_group, block_detail}

3. USER GOALS — Scan all goals across aspirations[]:
     IF "user" in goal.participants → add to user_goals[]
     Include: goal_id, aspiration_id, aspiration_title, title, priority, score (from score_map), category

4. RECURRING HEALTH — Scan all goals across aspirations[]:
     IF goal.recurring == true:
       IF lastAchievedAt is null: overdue_by = infinity (never completed — always most overdue)
       ELSE: overdue_by = max(0, hours_since(lastAchievedAt) - interval_hours)
       Add to recurring_list[]: goal_id, title, interval_hours, lastAchievedAt, overdue_by, currentStreak
     Sort recurring_list by overdue_by descending

5. TESTABLE HYPOTHESES — Filter active_hypotheses[]:
     IF resolves_no_earlier_than is null/missing OR resolves_no_earlier_than <= today:
       add to testable_hypotheses[]
     Sort by confidence descending
```

## Phase 3: Render Markdown

Construct the full markdown document. Use these section templates:

### 3a: Header

```markdown
# Backlog Report

> Generated: {YYYY-MM-DDTHH:MM:SS}

---
```

### 3b: Your Action Items

Only show this section if pending_questions[] or user_goals[] are non-empty.

```markdown
## Your Action Items

### Pending Decisions

| ID | Date | Question | Agent's Default Action |
|----|------|----------|-----------------------|
| pq-001 | 2026-03-15 | I decided X because Y... | {default_action} |
```

IF pending_questions[] is empty: omit "Pending Decisions" subsection.

```markdown
### Goals Needing You

| Goal | Aspiration | Title | Priority | Score | Category |
|------|------------|-------|----------|-------|----------|
| g-001-05 | asp-001 | Review deployment... | HIGH | 6.2 | infrastructure |
```

IF user_goals[] is empty: omit "Goals Needing You" subsection.
IF both are empty: omit entire "Your Action Items" section.

### 3c: Recommended Next Actions

Top 10 goals from scored_goals[] (by score descending).

```markdown
## Recommended Next Actions

| # | Goal | Aspiration | Title | Score | Priority | Category | Recurring |
|---|------|------------|-------|-------|----------|----------|-----------|
| 1 | g-001-01 | asp-001 | Reflect and journal | 8.7 | HIGH | maintenance | 4h |
| 2 | g-001-03 | asp-001 | Research API response... | 7.9 | MEDIUM | intelligence | — |
```

Recurring column: show interval (e.g., "4h", "24h", "0.25h") if recurring, "—" if not.
Title: truncate to 50 chars with "..." if longer.

### 3d: Recurring Health

Only show if any recurring goals exist.

```markdown
## Recurring Health

| Goal | Title | Interval | Last Done | Overdue By | Streak |
|------|-------|----------|-----------|------------|--------|
| g-001-04 | Check agent inbox | 0.25h | 14:30 today | 2.1h | 8 |
| g-001-01 | Reflect and journal | 4h | 10:09 today | — | 15 |
```

Overdue By: show hours if overdue, "—" if on time.
Last Done: show relative time (e.g., "14:30 today", "yesterday 18:00", date if older).
Sort by overdue_by descending (most overdue first), then on-time goals by next-due.

### 3e: Aspirations (one section per active aspiration)

```markdown
## asp-001: Maintain Agent Health [ACTIVE] (3/12 goals)

**Priority**: MEDIUM | **Motivation**: Keep the agent's knowledge fresh...

| Goal | Title | Status | Pri | Score | Category | Skill | Recur | Who |
|------|-------|--------|-----|-------|----------|-------|-------|-----|
| g-001-01 | Reflect and journal | pending | HIGH | 8.7 | maintenance | /reflect | 4h | agent |
| g-001-03 | Tree maintenance | pending | MED | 5.2 | maintenance | /tree maintain | 24h | agent |
| g-001-05 | Run test circuits | blocked | HIGH | BLK | testing | /some-domain-skill | — | agent |
```

Rules:
- Title: truncate to 40 chars with "..."
- Pri: abbreviated (HIGH, MED, LOW)
- Score: from score_map; "BLK" if blocked; "—" if terminal (completed/skipped/expired)
- Recur: interval string if recurring, "—" if not
- Who: comma-joined participants list, or "agent" if unset
- Sort order: in-progress first, then pending (by score desc), then blocked, then terminal
- Terminal goals (completed/skipped/expired): show as a summary count below the table,
  not as full rows. E.g., "3 completed, 1 skipped"
- Aspirations ordered by priority (HIGH → MEDIUM → LOW), then by asp-NNN id

### 3f: Blocked Goals

Only show if blocked_data.summary.total_blocked > 0.

```markdown
## Blocked Goals ({total_blocked} of {total_active_goals} active)

### Bottlenecks

| Root Goal | Aspiration | Title | Cause | Downstream |
|-----------|------------|-------|-------|------------|
| g-001-04 | asp-001 | Test server start... | INFRA: external-service | 3 |
```

Then list by reason group. Omit any group with count 0.

```markdown
### Infrastructure ({count})
- g-001-04: Test server start — external-service unavailable

### Dependencies ({head_count} heads, {downstream_count} downstream)
- g-003-02: Deploy config — waiting on g-001-02

### Deferred ({count})
- g-001-06: Check inbox — until 2026-03-17 (cooldown)

### Hypothesis Gate ({count})
- g-001-06: Verify spatial memory — resolves_no_earlier_than 2026-03-20

### Explicitly Blocked ({count})
- g-001-07: Review cache depth — reason: awaiting session data
```

### 3g: Hypotheses Ready to Test

Only show if testable_hypotheses[] is non-empty.

```markdown
## Hypotheses Ready to Test ({count})

| ID | Title | Confidence | Type | Category | Window |
|----|-------|------------|------|----------|--------|
| 2026-03-10_api-latency | API latency at P99... | 0.70 | calibration | api-performance | Mar 15 – Apr 10 |
```

Title: truncate to 40 chars.
Window: "{resolves_no_earlier_than} – {resolves_by}" formatted as short dates.

### 3h: Summary Footer

```markdown
---

**Summary**: {N} aspirations | {G} active goals | {S} selectable | {B} blocked | {P} pending decisions | {H} testable hypotheses
```

## Phase 4: Write File

```
Write: <agent>/BACKLOG.md ← rendered markdown from Phase 3

IF <agent>/BACKLOG.md already exists: overwrite (regenerated snapshot, not append-only)
```

## Phase 5: Terminal Summary

Display compact summary in the terminal:

```
═══ BACKLOG REPORT ════════════════════════════

Generated: {timestamp}

Your Action Items: {N} pending decisions, {M} goals needing you
  (list goal ids + short titles if ≤ 5 items)

Top 5 Next Actions:
  1. [{score}] g-XXX-XX: {title (60 chars)} ({priority})
  2. [{score}] g-XXX-XX: {title (60 chars)} ({priority})
  3. [{score}] g-XXX-XX: {title (60 chars)} ({priority})
  4. [{score}] g-XXX-XX: {title (60 chars)} ({priority})
  5. [{score}] g-XXX-XX: {title (60 chars)} ({priority})

Recurring: {N} overdue, {M} on-streak
Aspirations:
  asp-001: {title} [ACTIVE] ({completed}/{total}) — {priority}
  asp-001: {title} [ACTIVE] ({completed}/{total}) — {priority}

Blocked: {N} goals, {B} bottlenecks
Hypotheses: {H} ready to test

Full report written to: {absolute_path_to_<agent>/BACKLOG.md}
═══════════════════════════════════════════════
```

## Chaining

- **Called by**: User directly (`/backlog-report`), OR by agent during RUNNING state
- **Calls**: No other skills — only framework scripts (`aspirations-read.sh`, `goal-selector.sh`, `pipeline-read.sh`) and file reads
- **Modifies**: Writes/overwrites `<agent>/BACKLOG.md`
