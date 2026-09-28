---
description: "ScheduleWakeup prompt: the <<autonomous-loop-dynamic>> sentinel, a user /loop, or plain text; never a slash skill; re-arm first on wakeup."
---

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
RIGHT (autonomous loop): ScheduleWakeup({prompt: "<<autonomous-loop-dynamic>>", delaySeconds: 1200, noop: false, reason: "deadman resurrection net"})
RIGHT (user /loop):      ScheduleWakeup({prompt: "/loop investigate flaky test", delaySeconds: 300, noop: false, reason: "next /loop iteration"})
RIGHT (external wait):   ScheduleWakeup({prompt: "check GitHub PR #142 CI run status", delaySeconds: 270, noop: false, reason: "waiting on the CI run"})
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
re-entry. Nor is the idle sleep's wakeup ever the re-entry — every harness
reports a background job's exit (loop-terminal-protocol.md §4.2).

## Sanctioned Exception: the deadman's-switch terminal-pair

By default (Stage 5 onward), the iteration's terminal response emits TWO
batched tool calls:

```
1. ScheduleWakeup(prompt="<<autonomous-loop-dynamic>>", delaySeconds=600, noop=false, reason="deadman resurrection net")
2. Skill(aspirations) with args='loop'
```

Not Anti-pattern C: `Skill(aspirations)` remains the primary re-entry; the
wakeup fires only when the Skill chain breaks. Opt-out:
`agents/<agent>/session/deadman-disabled`. Detail:
`core/config/rationale/deadman-switch.md`.

### Re-arm FIRST on resurrection and on autocompact resume

**RULE:** on a `<<autonomous-loop-dynamic>>` wakeup firing, **or on an autocompact
resume that re-enters the loop body mid-iteration**, that turn's FIRST tool call
MUST be the sentinel re-arm — restoring the net BEFORE any loop-entry work that
could fail — THEN proceed to Phase -1.5. The gate approves it only while
RUNNING. Incident traces (rb-4345 / g-115-2771 / g-115-5834):
`core/config/rationale/deadman-switch.md`.

### D. Using ScheduleWakeup for EXTERNAL polling the harness already tracks

ScheduleWakeup is for waiting on EXTERNAL signals the harness cannot
track. Do NOT use it to advance the loop's own state machine (Anti-pattern
C above) NOR to poll background Bash the harness auto-notifies on
(Anti-pattern A above).

### E. Cancelling the deadman net on a LIVE loop

`ScheduleWakeup(stop: true)` while RUNNING deletes the loop's ONLY resurrection
path. **Pausing is not stopping.** RE-ARM the sentinel and end on your normal
terminal call. The genuine stop is the user's `/stop`, which writes
`stop-requested` FIRST — that signal is what tells the gate a cancel is
legitimate.

## Enforcement

Three layers (gate, rule, detective). Full table:
`core/config/conventions/loop-terminal-protocol.md` § "ScheduleWakeup enforcement layers".

## Cross-references

- `.claude/rules/return-protocol.md` — orchestrator terminal-call contract
- `core/scripts/schedule-wakeup-gate.py` — Layer A enforcement
- `core/scripts/aspirations-rejection-audit.py` — Layer C detective
- ScheduleWakeup tool documentation in the system prompt (the authoritative
  spec for the sentinel and the polling anti-pattern)
- `core/config/conventions/loop-terminal-protocol.md` — platform facts,
  fail-safe property, and the 2026-05-18 origin incident (moved from this rule)
