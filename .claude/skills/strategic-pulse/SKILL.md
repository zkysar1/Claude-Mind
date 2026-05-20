---
name: strategic-pulse
description: Surface portfolio-shape patterns the user should consider acting on. Tail consolidation pressure, work-class skew, aged aspirations, all-blocked aspirations. Hybrid skill — user-invocable AND agent-callable. Auto-fires every 50 completed goals via aspirations-precheck cadence.
user-invocable: true
minimum_mode: assistant
companion_scripts:
  - core/scripts/strategic-pulse-detectors.py
chaining:
  calls: [notify-user]
  called_by: [aspirations-precheck]
conventions: []
revision_id: "skill-bootstrap-strategic-pulse-79de28"
previous_revision_id: null
---

# /strategic-pulse — Portfolio-Shape Decision Surface

Closes the gap between what the agent computes (portfolio state, work-class
distribution, aspiration ages) and what the user can ACT on (which 3 to
close, whether to rebalance, whether the queue is stalled). The data
already exists; this skill makes it visible without the user having to
ask.

Origin: LifingPolls plan item 10 (2026-05-08).

Distinct from:
- `/priority-review` — user-pull dashboard for re-ranking active aspirations
- `/backlog-report` — full sprint planning material
- `/agent-completion-report` — what changed since last status marker
- `/fresh-eyes-review` — meta-review (Self + Program correctness)
- `/felt-sense-checkin` — autonomous structured 7-lane self-audit

This skill is the **proactive** strategic surface: "if I were your
strategist, here's what I'd flag." Auto-fires on cadence (every 50
completed goals via aspirations-precheck) AND on user demand (`/strategic-pulse`).

## Detectors

Four orthogonal portfolio-shape patterns:

1. **tail_consolidation** — N+ active aspirations at high completion ratio.
   Surfaces consolidation pressure: finishing 3 vs starting 3 new produces
   compounding learning. Triggers at 5+ aspirations ≥75% complete.

2. **work_class_skew** — One work_class is severely over-represented vs its
   target (`core/config/aspirations.yaml § class_balance.targets`). Triggers
   when actual ≥ 2× target. Suggests rebalancing or revisiting target
   fractions.

3. **aged_aspirations** — Active aspirations with no goal completions in
   60+ days. Likely stalled or paused-without-decision. Triggers on any
   match.

4. **idle_aspirations** — Active aspirations with ALL goals blocked or
   deferred — zero actionable work. Triggers on any match.

## Phase 0: Load Conventions

**Step 0**: `Bash: load-conventions.sh` with each name from `conventions:` (none for this skill).

## Phase 1: Detect Patterns

Bash: `py -3 core/scripts/strategic-pulse-detectors.py --json`

The script reads world + agent aspiration state, runs all four detectors,
and emits a JSON list of pattern records. Empty list = portfolio looks
healthy; emit a "no notable patterns" message and return.

Each record has: `pattern`, `magnitude` (medium|high), `evidence` (dict),
`suggestion` (paragraph).

## Phase 2: Compose Output

Branch by invocation context:

**User-invocable (terminal output):**

  Bash: `py -3 core/scripts/strategic-pulse-detectors.py --text`

  The detector renders a terminal-friendly block. Output it directly.

**Agent-callable (auto-fire from precheck cadence):**

  Compose an email body via `/notify-user`:

  Subject: "Strategic pulse: {N} pattern(s) detected"
  Body: rendered text from `--text` mode, plus a footer:

    "This is an autonomous portfolio-shape report. Reply with directives
    or run /priority-review to re-rank active aspirations."

  (Check world/forged-skills.yaml for a skill whose triggers match
  "notify the user" and invoke it with the subject and message. If no
  matching skill is registered, fall back to a `participants: [agent, user]`
  goal via aspirations-add-goal.sh. Never block on notification failure.)

## Phase 3: Cadence Tracking

When agent-invoked, increment `meta/strategic-pulse-runs.jsonl` with
the timestamp + pattern count. The next aspirations-precheck cadence check
reads the latest run timestamp to decide whether to re-fire.

  Bash: append-only write to meta-log via:
    `echo '{"ts":"<iso>","pattern_count":<N>}' >> $META_DIR/strategic-pulse-runs.jsonl`

## Cadence Configuration

Auto-fire trigger lives in `aspirations-precheck/SKILL.md` Phase 0.7
(strategic-pulse cadence check) — fires every 50 goals_completed_this_session.
Configurable via `core/config/aspirations.yaml § strategic_pulse.cadence_goals`
(default 50).

## Return Protocol

See `.claude/rules/return-protocol.md`. As a sub-skill called from
the loop or a hybrid skill called by the user, the terminal action is the
last `Bash` invocation (the detector run or the notify-user composition).
Do NOT terminate with a text summary — that kills the loop when invoked
mid-iteration.

## Chaining

- **Calls**: `/notify-user` (when agent-invoked at cadence)
- **Called by**: `/aspirations-precheck` (cadence check at 50 goals);
  user-invocable as `/strategic-pulse`
- **Modifies**: `meta/strategic-pulse-runs.jsonl` (append-only cadence log)
