---
name: fresh-eyes-review
description: "Periodic local self-audit (cadence: every 25 goals). Assembles a portfolio-direction briefing (Self snapshot, aspiration portfolio, evolution signals, partner activity), writes it to agents/<agent>/reports/, and posts a one-line summary to the coordination board. No email push, no user-approval gate — the user reviews changes via git log and tracked signals at their own pace. Use whenever the user wants to force a portfolio review on demand (/fresh-eyes-review), or the precheck cadence triggers automatically (--cadence). Distinct from sq-012 (post-goal, narrow) and /priority-review (user-pull, ranking-only)."
user-invocable: true
triggers:
  - "/fresh-eyes-review"
  - "fresh eyes review"
  - "step back review"
tools_used: [Bash, Read, Write, Edit, Skill]
companion_scripts: [core/scripts/fresh-eyes-cadence-check.sh]
conventions: [aspirations, session-state, working-memory]
minimum_mode: assistant
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
revision_id: "skill-bootstrap-fresh-eyes-review-b305df"
previous_revision_id: null
---

# /fresh-eyes-review — Periodic Portfolio-Direction Self-Audit

Every 25 completed goals (or on user demand), step back and produce a
portfolio-direction briefing. The ritual runs autonomously, writes the
briefing to `agents/<agent>/reports/`, and posts a one-line summary to the
coordination board. No email push, no user-approval gate.

The user reviews changes via git log and tracked signals at their own pace.
This follows the same pattern as Self evolution (guard-380, 2026-04-22):
the agent acts, the user reviews retroactively and reverts if they disagree.

## Sub-commands

```
/fresh-eyes-review                 — User-forced review, bypasses cadence gate
/fresh-eyes-review --cadence       — Check cadence; run only if gate passes
                                     (agent-invoked path from precheck)
```

## Step 0: Load Conventions

`Bash: load-conventions.sh` with each name from the `conventions:` front
matter. Read only the paths returned. If output is empty, all conventions
already loaded — proceed.

## Phase 1: Cadence Gate

```
IF invoked with --cadence:
    Bash: core/scripts/fresh-eyes-cadence-check.sh
    IF exit 1: Output "Fresh-eyes: cadence not crossed — noop." → DONE (return)
    IF exit 0: proceed
ELSE (user-invoked, no --cadence flag):
    Proceed directly — user override.
```

The cadence script enforces the 25-goal threshold. User invocation
bypasses it.

## Phase 2: Briefing Assembly (read-only)

Read the inputs. Cache each result so Phase 3 can synthesize without
re-reading.

```
# 2.1 Self — current identity
Read agents/<agent>/self.md
  → capture body content (after YAML front matter) and last_updated
  → compute days_since_self_updated = (today - last_updated).days

# 2.2 Aspiration portfolio — active work snapshot
Bash: load-aspirations-compact.sh
IF path returned: Read it
Extract for each active aspiration:
  - id, title, priority
  - goals: (completed / total), top 3 goal titles with status

# 2.3 Self-evolution signals — what sq-012 has flagged recently
Read agents/<agent>/session/pending-questions.yaml
  → capture entries where id starts with 'sq-012' OR tags include 'self_evolution'
    AND created within last 30 days (any status)

# 2.4 Evolution engine output — dev stage, gap analysis, novelty pressure
Bash: tail -n 5 <META_DIR>/evolution-log.jsonl
  → parse the most recent entry
  → capture current_stage, gap_analysis summary, interestingness_state

# 2.5 Strategic-scan portfolio health — category concentration, uncovered Self priorities
Bash: wm-read.sh portfolio_health_signal
  → capture any recent signal (category_concentration, uncovered_priorities)

# 2.6 Partner activity — the other half of the team
Bash: team-state-read.sh --json
  → capture partner.last_active, partner.current_focus, partner.live_phase, partner.session_goals_completed
  → capture recent_completions (last 5)

# 2.7 Goal-count context — how much work backs this review
Bash: fresh-eyes-cadence-check.sh --verbose
  → capture current goals-completed count, last-fire count, diff
```

## Phase 3: Synthesis

Build the briefing text (plain Markdown, no external links) with sections:

```markdown
# Fresh-eyes review — {today ISO date}

{One paragraph: N goals completed since last review (or "first review").
Where the portfolio has moved. Which aspirations finished. What the
agent's been working on most. Two sentences, not five.}

## Self snapshot (informational)

Self evolves autonomously via sq-012 (post-notification per guard-380). Shown
here so you can revert via direct edit or `/respond` if you disagree — no
answer needed. The autonomous evolution path will not be gated on your
response to this section.

Current Self (last updated {N} days ago):

> {self.md body, full text — inline, no link}

Recent self-evolution signals (FYI):
- {Evidence-backed bullet — e.g., "sq-012 flagged 'core purpose may
  be narrowing' in pq-NNN (2026-04-NN)."}
- {Evidence-backed bullet — dev-stage / gap analysis signal, if any}

## Are we working on the right problems?

Active aspirations ({N} total):
| ID | Title | Priority | Progress |
|----|-------|----------|----------|
| asp-NNN | {title} | HIGH/MED/LOW | N/M goals |

Observations:
- {Category concentration finding — e.g., "70% of recent goals in
  infrastructure; 0 in primary-domain despite Self emphasis."}
- {Completion-health finding — e.g., "3 aspirations above 80%
  completion; 2 below 20% for >5 sessions."}
- {Partner signal — e.g., "<partner-agent> created 4 review goals this
  window; <this-agent> executed 3."}

Candidate portfolio rebalances (if any):
- {Priority shift proposal with rationale}

## Portfolio assessment

Are we working on the right problems — is the portfolio still aligned with
the Self?

{Agent's own assessment based on Phase 5.5 decision: act_now / act_later /
no_change, with rationale. No user response requested.}
```

All observations must follow `.claude/rules/communication-clarity.md` rule 6:
state what the evidence shows, do not hedge. If evidence is ambiguous, say
"the evidence shows X but does not show Y."

## Phase 4: Archive Copy

Write the briefing body to `agents/<agent>/reports/fresh-eyes-{YYYY-MM-DDTHH-MM-SS}.md`
for historical reference. Timestamp includes HH-MM-SS so multiple same-day
invocations (cadence fire + user-forced review) do not collide.

```
Bash: mkdir -p agents/<agent>/reports
Write the briefing body (from Phase 3) to agents/<agent>/reports/fresh-eyes-{today-isotime}.md
  (where {today-isotime} = `date +%Y-%m-%dT%H-%M-%S` — colons replaced with
   hyphens for Windows filesystem compatibility)
```

## Phase 5.5: Self-Assess Decision

Classify the review outcome via the deterministic helper and act on it.
No escalation to the user — the agent decides and proceeds autonomously.

Extract signals from the Phase 3 briefing synthesis (scored 0..1 unless
noted) and pass to the helper:

```
# Build signals JSON from Phase 3 briefing content
SIGNALS_JSON='{
  "portfolio_drift_score":          {0..1 — degree the portfolio has drifted from Self emphasis since last review},
  "completion_health":              {0..1 — average completion ratio across active aspirations},
  "self_evolution_signals_count":   {int — recent sq-012/ABC-chain/pattern-signature self-evolution indicators in last 30d},
  "self_last_updated_days":         {int — days_since_self_updated from Phase 2.1},
  "explicit_user_directive":        {true|false — outstanding /respond about purpose or portfolio},
  "signal_actionable_score":        {0..1 — how clearly the signals map to a specific Self edit}
}'
Bash: echo "$SIGNALS_JSON" | bash core/scripts/self-assess-and-decide.sh --review-type fresh-eyes-review
  → capture decision, rationale, recommended_action from JSON output
```

Branch on decision:

- **`act_now`** — apply the Self edit inline via the existing autonomous
  edit path (Edit tool on `agents/<agent>/self.md`). The Phase 2 hooks
  (`evolution-prepare.sh` → `evolution-record.sh`) capture the change as
  a stub; invoke `bash core/scripts/evolution-complete.sh --revision-id
  <stub-rev> --reasoning "<≥80-char rationale citing fresh-eyes briefing
  signals + Phase 3 evidence>" --signal-source fresh-eyes-review` to
  finalize. Material classification triggers `guard-380` post-notification
  via journal only (no email).
- **`act_later`** — file an Idea goal under `asp-115` (the recurring
  infrastructure aspiration that catches cross-system follow-ups):
  `bash core/scripts/aspirations-add-goal.sh asp-115` with stdin
  `{"title":"Idea: <one-line summary>","priority":"MEDIUM",
  "category":"self-evolution",
  "origin_signal":"idea:fresh-eyes-followup",
  "description":"<copy briefing observations + recommended_action>"}`.
- **`no_change`** — silent no-op. Phase 8 cadence stamp still fires so
  counter resets to next window.

ALWAYS log the decision to `agents/<agent>/journal` (one-line tagged
`fresh-eyes-decision`) and append a single board post to the `reasoning`
channel summarizing decision + rationale. The audit trail is the
guardrail's evidence path.

## Phase 8: Record the Tick

Update the WM slot so the cadence gate stops firing until 25 more goals
have completed.

**Critical invariant**: the stamp write is LOAD-BEARING. The cadence gate
reads `last_fresh_eyes_review` to decide whether to fire again. If this
step silently fails, the gate re-fires every iteration (see g-240-60 —
fresh-eyes-2026-04-20 was resolved but left the slot null, so the next
iteration's precheck said "fire!" and would have re-fired 45 min later
when there was still nothing new to review). Do NOT chain the stamp
write into the board-post with `&&` — a failing board-post MUST NOT
eat the stamp write.

### Step 1: Record the stamp (LOAD-BEARING — never skip)

```
Bash: bash core/scripts/fresh-eyes-record-tick.sh
```

This wrapper reads the current completed-goals count via
`fresh-eyes-cadence-check.sh --print-current`, writes the
`last_fresh_eyes_review` WM slot atomically, and verifies the slot is
non-null after the write (fails exit 1 on silent write failure). One
script call, one failure mode — no chaining.

### Step 2: Post to board (best-effort, must not block)

```
Bash: echo "Fresh-eyes review completed; briefing archived. Decision: {decision from Phase 5.5}." | bash core/scripts/board-post.sh --channel general --type status --tags fresh-eyes-review || true
```

The `|| true` ensures board-post failure (board file locked, quota
issue, etc.) does NOT propagate back through the skill and does NOT
affect the already-completed stamp write. Board-post is
cross-agent-visibility nice-to-have, not load-bearing.

The board-post is the terminal action — per Return Protocol requirements,
the skill does NOT end with text output.

## Chaining

- **Called by**: User (`/fresh-eyes-review`), `/aspirations-precheck`
  Phase 0.5e (`/fresh-eyes-review --cadence`)
- **Calls**: `fresh-eyes-cadence-check.sh`, `load-aspirations-compact.sh`,
  `wm-read.sh`, `wm-set.sh`, `team-state-read.sh`,
  `self-assess-and-decide.sh`, `journal-add.sh`, `board-post.sh`
- **Reads**: `agents/<agent>/self.md`, `agents/<agent>/session/pending-questions.yaml`,
  `<meta>/evolution-log.jsonl`, world aspirations compact,
  `agents/<agent>/session/working-memory.yaml`
- **Modifies**: `agents/<agent>/reports/fresh-eyes-*.md` (new),
  `agents/<agent>/session/working-memory.yaml` (update last_fresh_eyes_review slot),
  `agents/<agent>/journal.jsonl` (append), board `general` channel (best-effort)
- **Does NOT modify**: `agents/<agent>/self.md` (unless Phase 5.5 returns act_now),
  aspiration priorities, pending-questions. No email is sent.

## Relationship to Existing Mechanisms

| Mechanism | Scope | Trigger | User-facing? |
|-----------|-------|---------|--------------|
| `sq-012` | Single-outcome self-purpose check | Post-goal | Only for significant changes |
| `aspirations-strategic-scan` S3b | Portfolio category coverage | Autonomous cadence (5 goals / 4h) | No |
| `aspirations-evolve` | Portfolio gap + dev-stage tuning | Autonomous cadence (15 goals / 12h) | No |
| `/priority-review` | Portfolio ranking | User pull | Yes, but pull-only |
| `/fresh-eyes-review` | **Portfolio direction** | **Goal-cadence (25 goals)** | **No — local audit** |

Fresh-eyes is the periodic portfolio-direction self-audit. It does NOT
replace any of the above — sq-012 keeps catching narrow per-goal purpose
drift (and updates Self autonomously per guard-380), strategic-scan keeps
watching category concentration autonomously, evolution keeps running gap
analysis, priority-review stays the user's anytime portfolio pull.
Fresh-eyes produces a local briefing artifact and board post — the user
reviews via git log and tracked signals at their own pace.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call,
not text. The terminal action is the Phase 8 board-post Bash call.
Never end this skill with a text summary of the briefing — the briefing
is in the archive, the agent's job is to record the tick and return control.
