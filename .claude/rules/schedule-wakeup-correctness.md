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

Why this is NOT a violation of Anti-pattern C: `Skill(aspirations)` REMAINS the
primary re-entry — the LAST call, continuing the loop synchronously. The
ScheduleWakeup is a single replace-slot wakeup (the platform keeps ONE pending
wakeup; each iteration's re-arm REPLACES the prior); on a healthy loop the
session is never idle for `delaySeconds`, so it NEVER fires — the Skill chain
always re-arms it forward first. It fires ONLY when the Skill chain breaks,
which is "waiting on a signal the harness cannot track" (the absence of the
next iteration) — the legitimate use, not state-machine advancement. The gate
(`schedule-wakeup-gate.py`) passes the sentinel unconditionally; guard-511
carries the matching carve-out. Verified platform facts (a tool batched after
ScheduleWakeup executes; ScheduleWakeup is turn-terminal, hence the arm goes at
the TERMINAL and never early; a wakeup fires after a text-only turn-end) and
the fail-safe property (worst case a SLOW loop, never a dead one) are recorded
in `core/config/conventions/loop-terminal-protocol.md` §4; design rationale in
`core/config/rationale/deadman-switch.md`.

### Re-arm FIRST on resurrection — and on autocompact resume (rb-4345 / g-115-2771 / g-115-5834)

The net is a SINGLE replace-slot wakeup — FIRING it consumes it, so a
resurrected turn begins with **no net armed**; and an autocompact resume that
re-enters the loop body MID-iteration reaches no terminal pair at all, so it
runs under whatever net already existed — none, if the compaction landed before
any close. The general trigger is "no terminal pair has been emitted", not "the
net fired". Both have killed loops for hours (2026-07-19 cc-04: ~7h, resurrected
turns text-died in an API storm without re-arming; 2026-08-11 cc-05: 7h47m —
a pending net is NOT excluded: clamp≠delivery, 17.1h max, g-115-6629).

**RULE:** on a `<<autonomous-loop-dynamic>>` wakeup firing, **or on an autocompact
resume that re-enters the loop body mid-iteration**, that turn's FIRST tool call
MUST be a `ScheduleWakeup(prompt="<<autonomous-loop-dynamic>>", delaySeconds=600)`
re-arm — restoring the net BEFORE any loop-entry work that could fail — THEN
proceed to Phase -1.5. This is NOT the "arm early" mechanic F2 rejected (that
forbade arming early in a STEADY-STATE iteration, where a turn-terminal arm
truncates a multi-turn iteration); it is a one-shot net-restoration at the very
START of the turn, and the iteration's terminal-pair re-arm at close simply
REPLACES it (double-arm is harmless under replace-slot semantics). The gate
passes the sentinel unconditionally, so the re-arm is always approved. Full
incident traces: `core/config/rationale/deadman-switch.md`.

### D. Using ScheduleWakeup for EXTERNAL polling the harness already tracks

ScheduleWakeup is for waiting on EXTERNAL signals the harness cannot
track. Do NOT use it to advance the loop's own state machine (Anti-pattern
C above) NOR to poll background Bash the harness auto-notifies on
(Anti-pattern A above).

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
- `core/config/conventions/loop-terminal-protocol.md` — platform facts,
  fail-safe property, and the 2026-05-18 origin incident (moved from this rule)
