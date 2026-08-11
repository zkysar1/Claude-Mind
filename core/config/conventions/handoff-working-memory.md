# Handoff Schema (Enhanced)

The continuation handoff (`agents/<agent>/session/handoff.yaml`) includes structured fields for
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
  - decision: "Port 8686 blocked by firewall"
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

Anti-suppression ceiling source of truth: `agents/<agent>/session/consolidation-lean-streak` (plain integer).
Written by consolidation Step 9, read by Step 0.1 triage. If >= 3, forces `full` tier.
This file is NOT consumed by boot (unlike handoff.yaml itself).
See `aspirations-consolidate/SKILL.md` Step 0.1.

---

# Reasoning Trajectory (Cross-Session Context) — NOT IMPLEMENTED

**Status: documented design, never built. No producer, no consumer.** The schema
below is retained as a design record so the intent is not lost and so a future
reader does not mistake its absence for a regression. If this behaviour is wanted
it must be BUILT; there is nothing here to repair.

MEASURED 2026-08-06 (zeta, hostname cc-02, uname -r 6.8.0-136-generic; g-328-46),
extending alpha's 2026-08-05 cc-04 measurement, which covered the three
diary-archival claims only:

- At measurement time `reasoning_trajectory` occurred exactly TWICE in the whole
  repo, both in THIS file: the schema key below, and one prose line. (This note
  adds further occurrences here, so re-measure that total rather than quoting it.)
  The load-bearing half is unchanged and re-checkable: ZERO hits in source, config,
  or skills across `core/`, `mind_api/`, `.claude/`, and the rest of `core/config/`
  — no producer, no consumer. (Repo-wide the one other occurrence is a
  `mind_api/state/access.log` line for `GET /v1/wm/read?slot=reasoning_trajectory`,
  emitted by this measurement's own probe. Runtime logs will accrue more from any
  future check; grep source, not logs.)
- The only reference to `execution-diary.sh read` anywhere was the "Construction"
  paragraph this block replaces. It named no real caller.
- `aspirations-consolidate/SKILL.md` contains ZERO occurrences of `diary`
  (case-insensitive) — consolidation neither reads, renames, nor archives it.
- `execution-diary.py` declares exactly `append, read, summary, trim` (plus
  `phase_start`/`phase_end`); `archive|rotate` match ZERO times across all 21,813
  bytes. `execution-diary.sh` is `exec python3 execution-diary.py` — direct, NOT
  daemon-routed — so the `.py` is the live implementation (guard-742 does not apply).
- No `execution-diary-session-*.jsonl` exists on this box, against a positive
  control of 7 live `execution-diary.jsonl` files. No code anywhere produces that
  filename; the only two references were prose here and in `compact-recovery.md`
  (corrected in the same change).
- Live `handoff.yaml` carries `session_summary` and no `reasoning_trajectory`.

**Git cannot date any of this, so do not infer a chronology.** `48ffffb4e`
(2026-06-18) is the ROOT commit of this repository — 3,638 files, 545,074
insertions — and both this passage and `def cmd_trim` entered in it. "trim
superseded archival" and "archival was never built" are therefore
indistinguishable from this repo's history, and a pickaxe result of "archive never
appeared" is bounded by that history floor rather than being a claim about all time.

```yaml
# DESIGN RECORD ONLY — nothing produces or consumes this block.
reasoning_trajectory:
  diary_entry_count: 42              # Total entries this session
  key_decisions:
    - context: "g-206-03: API domain exploration"
      decision: "Depth-first over breadth"
      rationale: "CALIBRATE level reached, focused exploration more productive"
      outcome: "3 tree nodes encoded"
  failed_approaches:
    - goal: "g-206-05"
      approach: "Direct service call for data seeding"
      failure: "Firewall blocks port — switched to alternative endpoint"
  emerging_patterns:
    - "Deploys require integration test to verify propagation"
  open_threads:
    - "Data seeding deployed but not yet verified via integration test"
```

**What the diary lifecycle actually is.** `execution-diary.jsonl` is a single
per-agent append-only file at `agents/<agent>/session/execution-diary.jsonl`. It is
never renamed, never session-scoped, and its entries carry no `session_id` — live
entry keys are exactly `content`, `entry_type`, `phase`, `timestamp`. Retention is
age-based trimming, not archival: `iteration-close.sh` calls
`execution-diary.sh trim --hours 8`. Boot does not read a prior-session diary,
because no prior-session diary is ever produced.

**Why the absence of session-scoping has teeth.** Scoped neither by filename nor by
field, a `phase_start` left unclosed by a stopped session remains the LAST marker
indefinitely. `phase-wedge-check.py` reads exactly that marker — which is how
recovery-gate Path D read a 70.9-minute-old marker from a session the user had
stopped and auto-recovered a 5-minute-old replacement session (g-328-45). Had the
archival this section once described actually existed, the new session would have
opened on an empty diary and the wedge check would have returned its clean verdict.

**The agent-wide scope is DELIBERATE, and that question is SETTLED — do not re-open
it.** g-306-129 decided it on 2026-08-03 (alpha): per-session diary routing was
considered and REJECTED, because the cross-box loss it would prevent measures ZERO
for a structural reason — the diary has exactly one writer, so a worker Body never
writes it. That goal also CORRECTED its own filed premise: routing would misfire ONE
probe (`agent-watchdog.py::StalledProbe`), not three, because `_advance_heartbeat`
touches the agent-wide heartbeat on every append and both `recovery-gate.sh` and
`runner-dead-check.sh` AND-gate diary staleness with heartbeat staleness, so they
fail SAFE. So the Path D consequence above is real, but session-scoping is NOT its
remedy; the live remedy lane is the runner-age gate (g-328-47). Read g-306-129's
outcome_note before proposing any change to diary scoping.

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

**Two writers produce these entries** — `core/scripts/create-blocker.py` (the
canonical CREATE_BLOCKER path) and `core/scripts/infra-health.py` (streak
alerts, `blocker_id` prefixed `streak-`). Both now emit the canonical keys
below. Until g-115-3348 create-blocker.py alone wrote `id`/`created_at`/
`failure_reason` instead of `blocker_id`/`detected_at`/`reason`, which made
blockers born there invisible to EVERY reader at once (aged-blocker recheck,
proactive user escalation, and goal-selector's block-reason renderer). It now
emits both spellings; the legacy trio is retained only so blockers already in
agents' working memories stay readable. **Readers must tolerate the legacy
names** (`blocker_id or id`, `detected_at or created_at`, `reason or
failure_reason`) until fleet blockers have cycled — and must NOT be "fixed" by
flipping to the legacy names, which would break infra-health.py's entries.

Fields:
- `blocker_id` — `infra-{skill-slug}-{date}` or user goal ID. Legacy alias: `id`
- `reason` — human-readable description
- `type` — `infrastructure` | `resource` | `user_action`
- `affected_skills` — list of skill paths
- `affected_categories` — list of goal categories (optional). When a goal has skill=null, goal-selector falls back to checking if goal.category matches. Secondary to affected_skills.
- `affected_goals` — list of goal IDs (appended as new goals hit this blocker)
- `unblocking_goal` — goal ID created to resolve this blocker (null for legacy backfills)
- `detected_session`, `detected_at` — when first detected. Legacy alias for
  `detected_at`: `created_at`. Both writers stamp `detected_at` at record
  creation; infra-health.py CARRIES IT FORWARD across its re-derivation sweep
  (it rebuilds `streak-*` entries every run, so re-stamping would reset the age
  each sync and the blocker could never age into a recheck or escalation)
- `reason` — human-readable description. Legacy alias: `failure_reason`
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
