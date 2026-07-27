# Return Protocol

Text-only output as the last action in a turn kills the autonomous session —
the turn ends, and the loop dies. Every skill must terminate with a tool call,
not a text paragraph. The *kind* of tool call depends on whether the skill is
a sub-skill returning control up, or the orchestrator closing an iteration.

## The Two Cases

There are exactly two valid terminal tool calls, selected by role:

| Role | Terminal tool call | Why |
|---|---|---|
| **Sub-skill** (/reflect, /decompose, /tree, /aspirations-execute, /aspirations-verify, /aspirations-state-update, /aspirations-learning-gate, any other skill invoked mid-iteration) returning control up to the orchestrator | `Bash` — e.g., `echo "Return to orchestrator — continue to next phase"` | A Bash call hands control back without re-entering a new skill context. The orchestrator's next turn resumes from where it was. |
| **Orchestrator** (`/aspirations` itself) at iteration close, after `iteration-close.sh --phase productivity-check` (or `recurring-close.sh`) has fired its `═══ ITERATION COMPLETE ═══` imperative | `Skill(aspirations)` with `args='loop'` | `iteration-close.sh productivity-check` and `recurring-close.sh` are the canonical end-of-iteration markers. After either has run, a Bash echo is **not sufficient** — it ends the turn without queuing another turn. Only `Skill(aspirations)` re-enters the loop. |

### Deadman's-switch pair (orchestrator, DEFAULT-ON since 2026-06-23)

By default (Stage 5 onward — see rationale doc), the orchestrator's terminal
response is the **pair** `[ScheduleWakeup(prompt="<<autonomous-loop-dynamic>>",
delaySeconds=600), Skill(aspirations) args='loop']`. This does NOT violate the
"terminal must be `Skill(aspirations)`" contract above: `Skill(aspirations)` is
still the LAST call and the primary re-entry. The `ScheduleWakeup` is armed
immediately before it as a self-resurrection NET that fires only if a text-death
breaks the Skill chain (it never fires on a healthy loop). It is NOT a
substitute for the Skill call (reconciles guard-511 / schedule-wakeup-correctness.md
Anti-pattern C). Opt-out per agent: when
`agents/<agent>/session/deadman-disabled` is present → terminal is
`Skill(aspirations)` alone, exactly as the table above (the pre-deadman
behavior). Rationale: `core/config/rationale/deadman-switch.md`.

**Re-arm FIRST on resurrection (single-shot-net gap, rb-4345 / g-115-2771).**
The deadman is a SINGLE replace-slot wakeup — firing CONSUMES it. When a
`<<autonomous-loop-dynamic>>` wakeup fires and resurrects a dead loop, that
resurrected turn starts with NO net. If it then text-dies before re-arming (the
likely case: under an API storm the first tool calls fail with
529/ECONNRESET/timeout and the model narrates → text-end), the net is gone and
the loop dies silently a SECOND time with nothing left to resurrect it — the
exact 7h death of 2026-07-19 (cc-04: wakeup fired 00:00:40, resurrected turns
text-died 00:00–00:04 during an API storm WITHOUT re-arming, dead until 07:18
zombie-recovery). RULE: on a `<<autonomous-loop-dynamic>>` firing, the **FIRST
tool call of the resurrected turn is a `ScheduleWakeup(prompt=
"<<autonomous-loop-dynamic>>", delaySeconds=600)` re-arm** — restore the net
BEFORE any work that might fail. Then proceed with the normal loop entry
(Phase -1.5 onward); the iteration's later terminal-pair re-arm is harmless
(replace-slot semantics make a double-arm a no-op). Restoring the net first
costs one tool call and converts "death during resurrection" from a multi-hour
silent death into at most one more 600s resurrection cycle.

## The Trap

The failure mode observed on 2026-04-23 (alpha session 58) was:

1. Sub-skill /reflect finished its work and emitted a terminal Bash echo (correct — sub-skill case).
2. Orchestrator resumed, ran the four iteration-close phases successfully.
3. Orchestrator then produced a friendly text summary of what happened.
4. Orchestrator emitted a terminal Bash echo to "comply" with this rule.
5. **Turn ended.** No `Skill(aspirations)` call was made.
6. The Stop hook did not log a BLOCK for the runner SID — investigation
   (2026-04-24) against 1151 BLOCK entries for `agent=alpha` found zero
   entries for the active runner SID, indicating Claude Code does not fire
   the Stop event reliably when a turn ends with a text message and no
   pending tool call. The Stop hook therefore cannot be relied on as the
   sole enforcement layer for this contract.
7. Loop died silently — `agent-state` stayed `RUNNING`, `running-session-id`
   stayed set, but no new turn fired until the user sent a message.

Root cause: the previous version of this rule said only "terminate with Bash, not
text" — which made the Bash echo feel like the correct terminal action even at
iteration close. It was not. Bash is the sub-skill terminal; `Skill(aspirations)`
is the orchestrator terminal. Conflating them kills the loop.

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

## Verification

`/verify-learning` enforces (1) with a dynamic grep: it iterates every
`.claude/skills/*/SKILL.md`, skips the exempt list above, and fails on any
remaining file that lacks `## Return Protocol`. New forged skills inherit the
requirement via the `.claude/skills/forge-skill` template (Step 3). The exempt
list is the single source of truth for (1) — edit it in
`.claude/skills/verify-learning/SKILL.md` if a genuinely new user-only skill
is added.

The orchestrator-vs-sub-skill split in rules (2) and (3) is enforced at
runtime by `core/scripts/iteration-close.sh` and `core/scripts/recurring-close.sh`,
which now print an explicit imperative as the terminal line of their stdout:
```
[iteration-close] ═══ ITERATION COMPLETE ═══
[iteration-close] NEXT ACTION REQUIRED: Call Skill(aspirations) with args='loop' as your VERY NEXT tool call.
```
Emitted from the final line of `do_productivity_check()` in iteration-close.sh
and the final line of recurring-close.sh. The Stop hook
(`core/scripts/stop-hook.sh`) reads `iteration-checkpoint.json` at BLOCK time
and restates the phase-specific required next action in its decision payload.

## Layered Defense When Combined With Explanatory Output Style

The Explanatory output style mandates trailing `✶ Insight ─────` blocks
"before AND after writing code." That mandate collides with rule (2) above:
the trailing insight makes prose, not the tool, the last content of the
message, and the turn ends. Four screenshot-evidenced silent loop deaths on
2026-04-29 (rb-629, guard-454) all matched this shape.

The combination has four enforcement layers — the same pattern documented
under `capability-routing-enforcement` for honor-system rules:

| Layer | Defense | Artifact |
|-------|---------|----------|
| **A** — tactical (LLM remembers) | This rule + the tree node below | `.claude/rules/return-protocol.md`, `world/knowledge/tree/system/system-constraints-loop/return-protocol-vs-explanatory-style.md` |
| **B** — automated gate | `/start` refuses mode=autonomous + Explanatory style at session entry | `world/scripts/output-style-mode-guard.sh` (Layer-B gate, exit 2 refused / 3 override+audit) |
| **C** — preventive observability | Stop hook flags trailing-text patterns post-hoc with specific diagnostic | `world/scripts/trailing-text-detector.py` (4 sub-patterns: insight_block HIGH, phase_summary HIGH, next_step_narration MEDIUM, trailing_prose LOW) |
| **D** — recurring audit | 24h transcript scan via the detector, files Investigate on hit | `g-115-315` (recurring under asp-115, interval 24h) |

Wiring goals: `g-115-316` (HIGH, Idea — Layer-B into /start Phase 0.6),
`g-115-317` (MEDIUM, Idea — Layer-C into stop-hook.sh).

See `world/knowledge/tree/system/system-constraints-loop/return-protocol-vs-explanatory-style.md`
for the full incident shape, decision procedure, and diagnostic signature.
