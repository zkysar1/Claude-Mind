---
description: "End every turn with a tool call, never text: sub-skills with Bash; the orchestrator with ScheduleWakeup(sentinel) + Skill(aspirations loop)."
alwaysApply: true
---

# Return Protocol

Text-only output as the last action in a turn kills the autonomous session —
the turn ends, and the loop dies. Every skill must terminate with a tool call,
not a text paragraph. The *kind* of tool call depends on whether the skill is
a sub-skill returning control up, or the orchestrator closing an iteration.

The incident behind the two-case split, the verification wiring, and the layered
defense against the Explanatory output style live in
`core/config/conventions/loop-terminal-protocol.md` (`load-conventions.sh
loop-terminal-protocol`); the deadman design and its incident traces in
`core/config/rationale/deadman-switch.md`. This file keeps the imperatives.

## The Two Cases

There are exactly two valid terminal tool calls, selected by role:

| Role | Terminal tool call | Why |
|---|---|---|
| **Sub-skill** (/reflect, /decompose, /tree, /aspirations-execute, /aspirations-verify, /aspirations-state-update, /aspirations-learning-gate, any other skill invoked mid-iteration) returning control up to the orchestrator | `Bash` — e.g., `echo "Return to orchestrator — continue to next phase"` | A Bash call hands control back without re-entering a new skill context. The orchestrator's next turn resumes from where it was. |
| **Orchestrator** (`/aspirations` itself) at iteration close, after `iteration-close.sh --phase productivity-check` (or `recurring-close.sh`) has fired its `═══ ITERATION COMPLETE ═══` imperative | `Skill(aspirations)` with `args='loop'` | `iteration-close.sh productivity-check` and `recurring-close.sh` are the canonical end-of-iteration markers. After either has run, a Bash echo is **not sufficient** — it ends the turn without queuing another turn. Only `Skill(aspirations)` re-enters the loop. |

### Deadman's-switch pair (orchestrator, DEFAULT-ON since 2026-06-23)

By default (Stage 5 onward), the orchestrator's terminal response is the
**pair** `[ScheduleWakeup(prompt="<<autonomous-loop-dynamic>>",
delaySeconds=600), Skill(aspirations) args='loop']`. This does NOT violate the
"terminal must be `Skill(aspirations)`" contract above: `Skill(aspirations)` is
still the LAST call and the primary re-entry. The `ScheduleWakeup` is armed
immediately before it as a self-resurrection NET that fires only if a text-death
breaks the Skill chain (it never fires on a healthy loop). It is NOT a
substitute for the Skill call (reconciles guard-511 /
schedule-wakeup-correctness.md Anti-pattern C). Opt-out per agent: when
`agents/<agent>/session/deadman-disabled` is present → terminal is
`Skill(aspirations)` alone. Rationale: `core/config/rationale/deadman-switch.md`.

**Re-arm FIRST on resurrection AND on an autocompact resume (rb-4345 /
g-115-2771 / g-115-5834).** The deadman is a SINGLE replace-slot wakeup — firing
CONSUMES it, so a resurrected turn starts with NO net; and a
`SessionStart:compact` resume that re-enters the loop MID-iteration emits no
terminal pair at all, so it runs the whole iteration on whatever net existed —
none, if the compaction landed before any close. Both have produced multi-hour
silent deaths (7h 2026-07-19; 7h47m 2026-08-11 — a pending net is NOT
excluded: clamp≠delivery, 17.1h max, g-115-6629). **RULE: on a
`<<autonomous-loop-dynamic>>` firing or an autocompact resume that re-enters the
loop mid-iteration, the FIRST tool call of that turn is a
`ScheduleWakeup(prompt="<<autonomous-loop-dynamic>>", delaySeconds=600)`
re-arm** — restore the net BEFORE any work that might fail, then proceed with
the normal loop entry (Phase -1.5 onward). The iteration's later terminal-pair
re-arm is harmless (replace-slot semantics make a double-arm a no-op).

## Required SKILL.md section

Every skill under `.claude/skills/*/SKILL.md` MUST:

1. Include a `## Return Protocol` section pointing to this file, AND
2. End its final procedural step with a Bash or Skill tool call — never with a
   text summary, `Output:` block, or `✶ Insight` paragraph as the last output.
3. For the orchestrator (`/aspirations`), that final tool call MUST be
   `Skill(aspirations)` with `args='loop'` once `iteration-close.sh
   --phase productivity-check` OR `recurring-close.sh` has printed the
   `═══ ITERATION COMPLETE ═══` imperative. For every other skill, it is Bash.

EXCEPT for the hard-exempt user-only skills that never run inside the aspirations
loop:

- `start`, `stop`, `open-questions` — user control commands
- `tree-reader` — portable read-only viewer, not invoked from the loop
- `verify-learning` — called by user only per its Chaining section
- `init`, `review`, `security-review` — Claude Code built-in commands (not in
  this repo but listed here for completeness)

Do NOT rely on `user_invocable` / `user-invocable` front matter as the
discriminator — it is spelled inconsistently across the repo (underscore on
forged skills, hyphen on base skills) and is not the field `/verify-learning`
checks. Hybrid skills (agent-completion-report, backlog-report, priority-review)
are NOT exempt — they can be invoked mid-loop and so need the section.

## Decision Procedure Before Terminating a Turn

Before the final tool call of any turn, ask:

1. **Did `iteration-close.sh --phase productivity-check` or `recurring-close.sh`
   just print `═══ ITERATION COMPLETE ═══` in this turn's tool output?**
   → YES: your next tool call MUST be `Skill(aspirations)` with `args='loop'`.
   Do not emit text. Do not emit another Bash. Skill only.
   → NO: continue below.

2. **Am I a sub-skill (anything other than /aspirations) that was invoked from
   the orchestrator and has completed my work?**
   → YES: terminate with a Bash tool call handing control back, e.g.,
   `Bash: echo "Return to orchestrator — continue to next phase"`.
   Do NOT call Skill(aspirations) from a sub-skill — that re-enters the loop
   from the wrong level.
   → NO: continue below.

3. **Am I the orchestrator mid-iteration (e.g., between Phase 4 and Phase 5),
   or in a non-iteration flow (spark questions, precheck, etc.)?**
   → Terminate with whatever tool call makes sense for the flow — usually a Bash
   call invoking the next script in sequence. The Phase pseudocode names the
   tool explicitly; follow it.

## Anti-patterns

- Orchestrator writes a "here's what I did this iteration" paragraph and
  terminates with `echo "done"`. **Kills the loop.**
- Sub-skill calls `Skill(aspirations)` as its terminal action instead of Bash.
  Re-enters the loop from the wrong level, causing double execution.
- Any skill emits text AFTER its terminal tool call. The terminal tool call must
  be LAST — no trailing prose, no trailing `✶ Insight` block, no "returning now"
  sentence.
- Assuming autocompact is the only scenario the stop hook covers. It is not —
  the hook BLOCKs any turn-end during RUNNING unless `stop-requested`,
  `stop-loop`, or `pending-agents` is set. A terminal text paragraph without a
  tool call triggers the hook regardless of autocompact status.

## Verification and enforcement (summary)

`/verify-learning` enforces the `## Return Protocol` section requirement with a
dynamic grep over every SKILL.md minus the exempt list above (edit the exempt
list in `.claude/skills/verify-learning/SKILL.md` if a genuinely new user-only
skill is added). At runtime `iteration-close.sh` / `recurring-close.sh` print
the `═══ ITERATION COMPLETE ═══` + `NEXT ACTION REQUIRED: Call Skill(aspirations)
with args='loop'` imperative as their terminal line, and the Stop hook restates
the phase-specific next action at BLOCK time. Against the Explanatory-style
collision (trailing `✶ Insight` blocks — four silent deaths on 2026-04-29,
rb-629, guard-454) there are four layers (A this rule + tree node, B `/start`
refuses autonomous+Explanatory, C the trailing-text detector, D a 24h
transcript audit) — table and wiring in the convention.
