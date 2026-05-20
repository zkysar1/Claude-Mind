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

### C. Using ScheduleWakeup in the orchestrator return path

The autonomous-loop orchestrator's correct terminal call at iteration
close is `Skill(aspirations)` with `args='loop'` — see
`.claude/rules/return-protocol.md`. ScheduleWakeup is NOT a substitute
for the Skill re-entry. The orchestrator does not need ScheduleWakeup
to continue iterating; the Skill call queues the next turn synchronously.

ScheduleWakeup is for waiting on EXTERNAL signals the harness cannot
track, not for advancing the loop's own state machine.

## Enforcement

| Layer | Mechanism | What it catches |
|-------|-----------|-----------------|
| **A** — gate | `core/scripts/schedule-wakeup-gate.{py,sh}` (PreToolUse[ScheduleWakeup] in `.claude/settings.json`) refuses slash-prefix prompts other than `/loop` | Prevents the wrong prompt from being scheduled in the first place. Hard block with educational deny message. |
| **B** — rule (this file) | Behavioral guidance read on demand | Documents the correct patterns for human and LLM authors. |
| **C** — detective | `core/scripts/aspirations-rejection-audit.py` scans recent transcripts for the rejection message + the originating ScheduleWakeup call. Predicate is shared with the gate via `core/scripts/_swakeup_predicate.py` (single source of truth). | Catches drift if the gate is bypassed somehow (hook timeout, fail-open path). The script itself only reports — pair it with `--exit-on-hits` in a recurring goal or cron wrapper if you want auto-filed Investigate goals when hits appear. |

## Cross-references

- `.claude/rules/return-protocol.md` — orchestrator terminal-call contract
- `core/scripts/schedule-wakeup-gate.py` — Layer A enforcement
- `core/scripts/aspirations-rejection-audit.py` — Layer C detective
- ScheduleWakeup tool documentation in the system prompt (the authoritative
  spec for the sentinel and the polling anti-pattern)

## Origin

Discovered 2026-05-18 from zeta session f1f3066e: four consecutive
ScheduleWakeup calls with `prompt: "/aspirations loop"` over four hours
(08:54 / 09:47 / 10:41 / 11:07 UTC), each followed ~2 min later by the
"can only be invoked by Claude" rejection. Layer D audit of 18 transcripts
showed zeta was the only agent doing this — alpha, delta, and echo all
used `<<autonomous-loop-dynamic>>` or natural-language correctly. The
pattern is easy to drift into once it survives in context, so the gate
protects against future occurrences in any agent.
