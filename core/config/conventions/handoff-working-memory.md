# Handoff Schema (Enhanced)

The continuation handoff (`<agent>/session/handoff.yaml`) includes structured fields for
fast cross-session resume:

```yaml
# Core fields (existing)
session_number: 14
timestamp: "2026-03-09T15:30:00"
last_goal_completed: "g-001-03"
goals_in_progress: ["g-001-05"]
hypotheses_pending: 3
next_focus: "Continue API caching research"

# Pre-scored first action (skips Phase 2 scoring on resume)
first_action:
  goal_id: "g-001-05"
  score: 8.7
  effort_level: "standard"
  reason: "In-progress from prior session, highest scoring"

# Locked decisions (differential expiry by kind)
decisions_locked:
  - decision: "Depth-first API domain exploration"
    made_session: 14
    reason: "CALIBRATE level reached, focused exploration more productive"
    kind: "strategy"              # "strategy" | "world_claim" (required)
  - decision: "Port 8686 blocked by security groups"
    made_session: 48
    reason: "Connection refused when curling port 8686"
    kind: "world_claim"           # Claims about infrastructure/external state
    evidence_strength: "weak"     # "weak" | "moderate" | "strong"

# Structured session summary (replaces free-form next_focus for context)
session_summary:
  goals_completed: 4
  goals_failed: 1
  key_outcomes:
    - "Encoded 3 patterns to api-caching tree node"
    - "Hypothesis H-42 resolved CONFIRMED (surprise: 2)"
```

Boot Step 0.5 reads `first_action` and passes it to the aspirations loop.
First iteration skips Phase 2 scoring.

**Decision classification** (`kind` field):
- `strategy`: Approach/priority/sequencing decisions. No external truth value. Expires after 3 sessions.
- `world_claim`: Assertions about infrastructure, availability, configuration, external systems. Has a truth value. Differential expiry by `evidence_strength`:
  - `weak` (single failed attempt, ambiguous error): expires after 1 session
  - `moderate` (multiple corroborating observations): expires after 2 sessions
  - `strong` (direct diagnostic output, authoritative source): expires after 3 sessions

`kind` is required on all entries. Missing `kind` is a schema violation.

**Consolidation triage metadata** (written by Step 9, read by boot for status reporting):
```yaml
consolidation_meta:
  triage_tier: "lean"               # "lean" or "full" — which path was taken
  consecutive_lean_sessions: 2      # informational copy for boot status output
```

Anti-suppression ceiling source of truth: `<agent>/session/consolidation-lean-streak` (plain integer).
Written by consolidation Step 9, read by Step 0.1 triage. If >= 3, forces `full` tier.
This file is NOT consumed by boot (unlike handoff.yaml itself).
See `aspirations-consolidate/SKILL.md` Step 0.1.

---

# Reasoning Trajectory (Cross-Session Context)

The handoff captures the reasoning *journey*, not just the end-state. Built from the
execution diary (`<agent>/session/execution-diary.jsonl`) during consolidation Step 9.

```yaml
reasoning_trajectory:
  diary_entry_count: 42              # Total entries this session
  key_decisions:
    - context: "g-206-03: API domain exploration"
      decision: "Depth-first over breadth"
      rationale: "CALIBRATE level reached, focused exploration more productive"
      outcome: "3 tree nodes encoded"
  failed_approaches:
    - goal: "g-206-05"
      approach: "Direct API invocation for task seeding"
      failure: "Firewall blocks port — switched to API Gateway"
  emerging_patterns:
    - "Deploys require integration test to propagate to shared state"
  open_threads:
    - "Task seeding deployed but not yet verified via integration test"
```

Boot Step 0.5 reads `reasoning_trajectory` and includes key decisions and open threads
in the boot status output, giving the agent continuity of reasoning across sessions.

**Construction**: Consolidation Step 9 reads `execution-diary.sh read --json`, filters
entries by type, and synthesizes:
- `key_decisions` from `decision` entries
- `failed_approaches` from `failure` + `approach_change` entries
- `emerging_patterns` from `finding` entries with cross-goal relevance
- `open_threads` from the last 5 `observation` entries that reference incomplete work

**Diary archival**: Consolidation renames `execution-diary.jsonl` to
`execution-diary-session-{N}.jsonl`. Boot reads last 20 entries from the prior session
diary. After 3 sessions, old diary files are deleted (keep current + previous).

---

# Working Memory Experience Integration

For full working memory schema, script API, and pruning rules see `core/config/conventions/working-memory.md`.
All working memory access uses `wm-*.sh` scripts (never direct file read/write).

Working memory slots gain optional `experience_refs` field:
```yaml
slots:
  active_context:
    summary: "Executing g-001-05: API caching research."
    experience_refs: ["exp-g001-05-research"]
  archived_context:  # pointer-only slot type
    summary: "Prior session research on database indexing patterns"
    experience_refs: ["exp-g002-03-database"]
```

`archived_context` is a pointer-only slot — no inline content, just summary + experience_refs.

---

# Working Memory Blocker Tracking

Working memory tracks infrastructure/resource blockers that prevent classes of goals from executing:

```yaml
slots:
  known_blockers:
    - blocker_id: "infra-some-service-2026-03-15"
      reason: "Required service unavailable"
      type: "infrastructure"
      affected_skills: ["/some-forged-skill"]
      affected_categories: ["processor-pipeline"]   # Optional. Fallback when goal.skill is null
      affected_goals: ["g-136-03", "g-169-08"]
      unblocking_goal: "g-136-NN"
      detected_session: 48
      detected_at: "2026-03-15T12:00:00"
      resolution: null
      diagnostic_context:
        error_emails: 0
        cascade_chain: null
        attempted_fix: null
```

Fields:
- `blocker_id` — `infra-{skill-slug}-{date}` or user goal ID
- `reason` — human-readable description
- `type` — `infrastructure` | `resource` | `user_action`
- `affected_skills` — list of skill paths
- `affected_categories` — list of goal categories (optional). When a goal has skill=null, goal-selector falls back to checking if goal.category matches. Secondary to affected_skills.
- `affected_goals` — list of goal IDs (appended as new goals hit this blocker)
- `unblocking_goal` — goal ID created to resolve this blocker (null for legacy backfills)
- `detected_session`, `detected_at` — when first detected
- `resolution` — null (active) or string describing how it was resolved
- `diagnostic_context` — object with `error_emails` (count), `cascade_chain` (report or null), `attempted_fix` (description or null)

Blockers persist across sessions via `handoff.yaml.known_blockers_active`. Blockers also clear when their linked `unblocking_goal` completes (Phase 0.5b primary check). Other resolution paths: user goal completion, 3-session expiry (tentative retry), infra-health probe success.

---

# Proactive Escalation Log

Tracks when proactive user notifications were sent to prevent spam. Written by
Phase 0.5b.1 (blocker age) and Step B7.1 (all-blocked sleep). Phase 5.5 (circuit
breaker) does not use this slot — it has natural cooldown via counter reset + defer.

```yaml
slots:
  proactive_escalation_log:
    - blocker_id: "infra-some-service-2026-03-15"  # matches known_blockers entry
      sent_at: "2026-04-04T14:30:00"
    - blocker_id: "_all_blocked"                   # synthetic ID for B7 notifications
      sent_at: "2026-04-04T16:00:00"
```

Session-scoped (cleared on session reset). Cross-session reset is intentional —
if a blocker persists into a new session, the user should hear about it again.
Cooldown period: `proactive_escalation.blocker_age_hours` from `core/config/aspirations.yaml`.

The phrase "Notify the user" in the pseudocode resolves via forged-skill-resolution
to a forged notification skill (if available), which handles email → pending-question → participant-goal fallback.
