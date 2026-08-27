# ScheduleWakeup Correctness

## Principle

When the autonomous loop or any agent code path calls `ScheduleWakeup`, the
`prompt` argument MUST be one of:

1. The sentinel `<<autonomous-loop-dynamic>>` — for autonomous-loop
   continuation (no user-typed prompt). The runtime resolves it back to the
   autonomous-loop instructions at fire time.
2. A literal `/loop ...` continuation — ONLY when the loop was originally
   started by a user-typed `/loop` command. Pass the same prompt verbatim
   each turn.
3. A natural-language continuation message (no leading slash) — for genuine
   polling of external state the harness cannot notify on (a CI run, deploy,
   remote queue).

It MUST NEVER be a slash-prefixed command like `/aspirations`, `/boot`,
`/respond`, `/reflect`, `/review-hypotheses`, or any other skill marked
`user-invocable: false` in its front matter. Those skills exist for
Claude-only invocation via the Skill tool; when their slash form is sent
as USER INPUT (which is what `prompt` becomes when the wakeup fires),
Claude Code's slash-command resolver rejects them with:

> This skill can only be invoked by Claude, not directly by users.

The loop then burns a turn on a rejection it cannot recover from
productively, and may stall completely if the orchestrator was relying
on the wakeup to re-enter.

## Anti-patterns

### A. Using ScheduleWakeup to poll background bash

The ScheduleWakeup tool documentation explicitly says:

> "Do NOT schedule a short-interval wakeup to poll for background work
> you started — when harness-tracked work finishes, you are re-invoked
> automatically, so polling is wasted."

If the previous tool call was `Bash` with `run_in_background: true`, the
harness will notify you when it completes. Do NOT call ScheduleWakeup to
re-check progress at 60s or 90s intervals. Terminate the turn with a
Bash echo handing control back, and wait for the harness notification.

### B. Slash-prefix prompts

```
WRONG: ScheduleWakeup({prompt: "/aspirations loop", delaySeconds: 90})
WRONG: ScheduleWakeup({prompt: "/boot", delaySeconds: 60})
WRONG: ScheduleWakeup({prompt: "/respond", delaySeconds: 120})
```

These all fire as user input and get rejected at the user-invocable gate.

```
RIGHT (autonomous loop): ScheduleWakeup({prompt: "<<autonomous-loop-dynamic>>", delaySeconds: 1200})
RIGHT (user /loop):      ScheduleWakeup({prompt: "/loop investigate flaky test", delaySeconds: 300})
RIGHT (external wait):   ScheduleWakeup({prompt: "check GitHub PR #142 CI run status", delaySeconds: 270})
```

### C. Using ScheduleWakeup AS A SUBSTITUTE for the orchestrator return path

The autonomous-loop orchestrator's correct terminal call at iteration
close is `Skill(aspirations)` with `args='loop'` — see
`.claude/rules/return-protocol.md`. ScheduleWakeup is NOT a substitute
for the Skill re-entry. The orchestrator does not need ScheduleWakeup
to continue iterating; the Skill call queues the next turn synchronously.

The prohibition is on SUBSTITUTION (using ScheduleWakeup *instead of* the
Skill call to advance the loop). It is NOT a prohibition on the deadman's
re-arm below, where ScheduleWakeup is a NET *behind* an unchanged Skill
re-entry.

## Sanctioned Exception: the deadman's-switch terminal-pair

The silent-loop-death failure mode (a turn that ends on trailing TEXT instead
of the terminal `Skill(aspirations)` call → no Stop event fires → the loop
dies for hours; 2026-06-21: 5 of 6 agents dead 1.5–4h) cannot be fixed by the
Stop hook alone — Claude Code does not reliably emit the Stop event on a
text-only turn-end (rb-629/guard-454). The deadman's-switch closes that gap
intrinsically: the agent arms its own resurrection.

The mechanism is the **terminal-pair**. By default (Stage 5 onward,
2026-06-23) — unless the per-agent opt-out flag
`agents/<agent>/session/deadman-disabled` is present — the iteration's
terminal response emits TWO batched tool calls, in this exact order:

```
1. ScheduleWakeup(prompt="<<autonomous-loop-dynamic>>", delaySeconds=600)
2. Skill(aspirations) with args='loop'
```

Not a violation of Anti-pattern C: `Skill(aspirations)` REMAINS the primary
re-entry — the LAST call, continuing the loop synchronously. The wakeup is a
single replace-slot net (each iteration's re-arm REPLACES the prior) that never
fires on a healthy loop, only when the Skill chain breaks — the legitimate
"signal the harness cannot track" use, not state-machine advancement. The gate
passes the sentinel unconditionally; guard-511 carries the carve-out. Platform
facts and the fail-safe property (worst case a SLOW loop, never a dead one):
`core/config/conventions/loop-terminal-protocol.md` §4; design rationale:
`core/config/rationale/deadman-switch.md`.

### Re-arm FIRST on resurrection — and on autocompact resume (rb-4345 / g-115-2771 / g-115-5834)

FIRING the net consumes it, so a resurrected turn begins with **no net armed**;
and an autocompact resume that re-enters the loop body MID-iteration reaches no
terminal pair at all, so it runs under whatever net already existed — none, if
the compaction landed before any close. The trigger is "no terminal pair has
been emitted", not "the net fired". Both have killed loops for hours (2026-07-19
cc-04 ~7h; 2026-08-11 cc-05 7h47m — a pending net is NOT excluded:
clamp≠delivery, g-115-6629).

**RULE:** on a `<<autonomous-loop-dynamic>>` wakeup firing, **or on an autocompact
resume that re-enters the loop body mid-iteration**, that turn's FIRST tool call
MUST be a `ScheduleWakeup(prompt="<<autonomous-loop-dynamic>>", delaySeconds=600)`
re-arm — restoring the net BEFORE any loop-entry work that could fail — THEN
proceed to Phase -1.5. This is a one-shot net-restoration at the START of the
turn, NOT the "arm early" mechanic F2 rejected; the close's terminal-pair re-arm
simply REPLACES it (double-arm is harmless). The gate always approves the
sentinel. Rationale + full incident traces:
`core/config/rationale/deadman-switch.md`.

### D. Using ScheduleWakeup for EXTERNAL polling the harness already tracks

ScheduleWakeup is for waiting on EXTERNAL signals the harness cannot
track. Do NOT use it to advance the loop's own state machine (Anti-pattern
C above) NOR to poll background Bash the harness auto-notifies on
(Anti-pattern A above).

### E. Cancelling the deadman net on a LIVE loop

`ScheduleWakeup(stop: true)` while RUNNING deletes the single replace-slot
wakeup that is the loop's ONLY resurrection path — converting a recoverable
text-death into a hard stop that needs a human to notice (measured 2026-08-25:
four faults compounded, and this was the one that made the other three
unrecoverable). **Pausing is not stopping.** Low on context, blocked, waiting on
a background run? RE-ARM the sentinel and end on your normal terminal call. The
genuine stop is the user's `/stop`, which writes `stop-requested` FIRST — that
signal, not a flag, is what tells the gate a cancel is legitimate.

## Enforcement

| Layer | Mechanism | What it catches |
|-------|-----------|-----------------|
| **A** — gate | `core/scripts/schedule-wakeup-gate.{py,sh}` (PreToolUse[ScheduleWakeup]) refuses (i) slash-prefix prompts other than `/loop`, (ii) `stop: true` while agent-state is RUNNING with no `stop-requested`. Fail-open by contract. Tests: `tests/test_schedule_wakeup_gate.py`. | Both the wrong prompt (A-D) and the net-cancel (E), at write time. Denies name the correct re-arm. |
| **B** — rule (this file) | Behavioral guidance read on demand | Documents the correct patterns for human and LLM authors. |
| **C** — detective | `core/scripts/aspirations-rejection-audit.py` scans recent transcripts for the rejection message + the originating ScheduleWakeup call. Predicate is shared with the gate via `core/scripts/_swakeup_predicate.py` (single source of truth). | Catches drift if the gate is bypassed (hook timeout, fail-open path). Reports only; `--exit-on-hits` makes it file Investigate goals. |

## Cross-references

- `.claude/rules/return-protocol.md` — orchestrator terminal-call contract
- `core/scripts/schedule-wakeup-gate.py` — Layer A enforcement
- `core/scripts/aspirations-rejection-audit.py` — Layer C detective
- ScheduleWakeup tool documentation in the system prompt (the authoritative
  spec for the sentinel and the polling anti-pattern)
- `core/config/conventions/loop-terminal-protocol.md` — platform facts,
  fail-safe property, and the 2026-05-18 origin incident (moved from this rule)
