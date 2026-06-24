# Rationale: Deadman's-Switch Terminal-Pair

Referenced from `.claude/skills/aspirations/SKILL.md` Return Protocol,
`core/scripts/iteration-close.sh` + `core/scripts/recurring-close.sh`
(ITERATION COMPLETE imperative), `.claude/rules/schedule-wakeup-correctness.md`
(Sanctioned Exception), and `guard-511`. Explains why the autonomous loop
self-arms a `ScheduleWakeup` resurrection net paired with — never replacing —
the primary `Skill(aspirations)` re-entry, and why the whole thing is
flag-gated.

## The problem it solves

The autonomous loop stays alive only if a turn's terminal action is the
`Skill(aspirations)` `args='loop'` re-entry that queues the next turn. If a
turn instead ends on trailing TEXT — a summary, an Explanatory `✶ Insight`
block, a stray "next I'll…" sentence — the chain breaks and the loop dies
silently. The agent's `agent-state` stays `RUNNING`, but no further turn
fires.

The Stop hook (`core/scripts/stop-hook.sh`) is the existing net, but it has a
hole exactly where this failure lives: Claude Code does NOT reliably emit the
Stop *event* on a text-only turn-end (rb-629 / guard-454: alpha session 58,
1151 BLOCK entries for the agent vs 0 for the runner SID). When the Stop event
doesn't fire, the hook never runs, so it can't BLOCK-and-recover.

Self-recovery via SessionStart → `recovery-gate.sh` only fires on an
autocompact or a fresh session. A fully-dead, idle session never autocompacts,
so it can sit dead indefinitely. Canonical incident (2026-06-21): 5 of 6 fleet
agents died this way and sat dead 1.5–4h with zero self-recovery; only a manual
`/start` per agent brought them back.

## Why a self-armed wakeup, not an external watchdog

The directive (2026-06-21, user) was explicit: "I do not want you to build it
[a watchdog], I want you to harden the agent framework so this is not a problem
long term … the agent itself can keep it alive." A separate monitor process is
out of scope by design. `ScheduleWakeup` is the intrinsic, session-scoped
primitive that lets the agent arm its OWN resurrection — no external process,
no PID file, no daemon. The wakeup dies with the session (correct: a dead
process needs no resurrection of a different process), and survives a turn-end
within a live session (the case we care about).

## Why the terminal-PAIR, not "arm early then keep working"

The first design sketch was "arm a wakeup as the FIRST action each iteration,
then do the iteration's work; a healthy turn re-arms it forward so it never
fires; a dead turn is resurrected by the timer it already set." A canary
(2026-06-21, dev observer session) falsified the mechanic that sketch needed:

- **F2 — ScheduleWakeup is turn-terminal.** Its own output: "Nothing more to
  do this turn — the harness re-invokes you when the wakeup fires." So you
  cannot arm it early and then continue an iteration that spans many turns —
  the arm ends the turn after the current response batch. (Matches guard-511's
  "ends the turn cleanly.")
- **F1 — a tool batched AFTER ScheduleWakeup in the same response DOES run.**
  A `Bash` placed after the arm executed. So multiple tool calls can share the
  terminal response with the arm.
- **F4 — a scheduled wakeup DOES fire after a text-only turn-end and
  re-invokes the agent.** Proven end-to-end: armed at 03:35:21, ended the turn
  on plain text, the session went idle, the wakeup re-invoked the agent. This
  is the resurrection primitive, and it was the one undocumented fact the whole
  concept hinged on.

F2 kills "arm early." F1 + F4 enable the **terminal-pair**: the iteration's
final response is two batched calls, `ScheduleWakeup(sentinel, 600s)` THEN
`Skill(aspirations) loop`. `Skill` stays the LAST call and the primary,
synchronous re-entry; the wakeup is a single replace-slot net armed just behind
it. On a healthy loop the session is never idle 600s (the Skill chain re-arms
the slot forward every iteration), so the wakeup never fires. On a text-death
the session goes idle, the last-armed wakeup fires after 600s, and the sentinel
resurrects the loop.

## Why this is not a guard-511 / Anti-pattern-C violation

guard-511 and schedule-wakeup-correctness.md Anti-pattern C forbid using
`ScheduleWakeup` as a SUBSTITUTE for the Skill re-entry (using it *instead of*
the Skill call to advance the loop's state machine). The deadman does not
substitute: `Skill(aspirations)` remains the last call and the primary
re-entry. The wakeup is a NET *behind* an unchanged Skill chain, firing only on
the chain's failure — which is "waiting on a signal the harness cannot track"
(the signal being the absence of the next iteration), the legitimate use. Both
rule sites carry the explicit carve-out.

## Rollout: flag-gated → default-ON (Stage 5 complete, 2026-06-23)

The change rewrites the loop's single most safety-critical invariant (the
terminal call, guarded by guard-454, guard-462, return-protocol.md, and the
Stop hook), so it shipped as a staged, reversible rollout rather than a flag
day:

- **Stages 1–4 (opt-IN, `deadman-enabled`):** a per-agent flag made the pair
  imperative inert by default — absent → `iteration-close.sh` /
  `recurring-close.sh` printed byte-identically to the pre-deadman version, so
  the live fleet was unaffected; present → the imperative printed the pair.
  Same flag-gated idiom the codebase uses elsewhere (e.g. `ARC_INGEST_WORLD`
  default-OFF). This let the one unproven link (Q5, below) be validated on a
  single controlled runner (charlie) before the fleet adopted it.
- **Stage 5 (default-ON, `deadman-disabled`):** once charlie proved the arm
  over 24h (ARMED-OK: 23/23 loop re-entries, 0 deaths, via
  `deadman-arm-audit.py`) and Q5 came back favorable, the gate was **inverted**:
  the pair imperative is now the unconditional default for every agent, and the
  flag became an *opt-OUT* (`deadman-disabled`). Rationale for the flip: the
  opt-IN flag is runtime-only and does NOT survive a fresh `/start`, so a reset
  session silently lost protection until re-flagged — default-ON in the
  committed scripts closes that durability gap. The per-agent escape hatch is
  preserved (`touch agents/<agent>/session/deadman-disabled` reverts one agent
  to the bare Skill imperative; `rm` re-enables), so the fail-safe/reversible
  property is intact. Tracker: g-115-1622.

## Q5 — RESOLVED FAVORABLE (2026-06-23) and why it was fail-safe regardless

Q5: does `Skill(aspirations)` actually *re-enter the loop* when batched right
after `ScheduleWakeup`? F1 proved a `Bash` runs after the arm; `Skill` should
behave the same, but "should" is not "did," and it could only be confirmed on a
real autonomous iteration (not from the observer session that built this).

**Resolution:** confirmed favorable over charlie's first 24h ON the flag. The
Stage-3 audit (`deadman-arm-audit.py`) observed 23 arms, every one followed by
a `Skill(aspirations)` loop re-entry (2 batched same-response, 21 split into a
following response, gaps 11–188s) — 0 deaths, verdict ARMED-OK. The 2 batched
cases are the direct Q5 evidence (Skill batched after the arm, loop continued);
the 21 split cases independently prove re-entry even when not batched. The
fail-safe path below was never exercised because re-entry never failed.

Fail-safe property: if Q5 turns out false (Skill does NOT re-enter after the
arm), the terminal response's wakeup still fires after 600s and resurrects the
loop via the sentinel — so the loop runs at 600s/iteration (slow) but stays
ALIVE and keeps re-arming. The worst case is a slow loop, never a dead one. The
change cannot make survival worse than the status quo (silent death).

## A second open question (Q6): overdue-wakeup behavior + long iterations

The net covering iteration N+1's execution is arm_N — the wakeup armed at
iteration N's terminal, ~600s in the future. If iteration N+1 runs LONGER than
600s (e.g. a ~32-min full-suite goal), arm_N's scheduled time passes while the
session is BUSY, so it does not fire (fires only on idle). On a CLEAN long
iteration this is harmless: N+1's terminal re-arms (replaces) arm_N before it
matters. The open question is the DEATH-during-a-long-iteration case: if N+1
dies (trailing text) at minute 32, arm_N has been "overdue" since minute 10 —
does it fire when the session goes idle at the death, or was it dropped?

- If overdue wakeups fire on next idle (the likely reading of "fires only when
  idle", and consistent with the canary firing slightly after its scheduled
  time): long-iteration-deaths are covered, resurrected right at the death.
- If overdue wakeups are dropped: deaths DURING iterations longer than
  `delaySeconds` are NOT netted by the deadman (the Stop hook remains the only —
  unreliable — net for that narrow case).

This cannot be eliminated by delay tuning alone: no delay ≤ the 3600s clamp
covers an arbitrarily long iteration, and raising the delay to cover long
iterations slows resurrection for the common (short-iteration) case. 600s
optimizes the common case; the residual is the rare intersection of (iteration
> 600s) AND (death at its terminal) AND (Stop hook also missed it). Validate Q6
alongside Q5 on the controlled runner; if unfavorable, options are a longer
delay (trades common-case latency) or a mid-long-goal re-arm checkpoint (more
surface). Documented here so the residual is explicit, not silent.

## Why 600s

The wakeup fires only after `delaySeconds` of CONTINUOUS session idle. On a
healthy loop, inter-turn idle gaps are sub-second (the Skill chain re-enters
immediately), and long goals keep the session busy (not idle), so the wakeup
never approaches firing. In RUNNING mode the Stop hook also prevents intentional
idle, so there is no legitimate 600s idle window — only a true death produces
one. 600s therefore carries no false-fire risk while giving a ~10-minute
resurrection latency (versus the 1.5–4h observed), a 9–24× improvement.
Tunable: it is a literal in the two close-script imperatives and the SKILL.md
spec; raise it for more false-fire margin, lower it for faster resurrection.

## Cross-references

- `guard-511` — the guardrail carrying the sanctioned-exception carve-out
- `guard-454` — trailing-text-kills-the-loop (the failure this nets)
- `rb-629` — Stop-event-not-fired-on-text-end incident lineage
- `.claude/rules/schedule-wakeup-correctness.md` — Sanctioned Exception section
- `.claude/rules/return-protocol.md` — orchestrator terminal-call contract
- `.claude/skills/aspirations/SKILL.md` — Return Protocol (deadman terminal-pair)
- `core/scripts/iteration-close.sh` / `core/scripts/recurring-close.sh` — the
  flag-gated ITERATION COMPLETE imperatives
- `core/scripts/schedule-wakeup-gate.py` / `_swakeup_predicate.py` — the gate
  that passes the `<<autonomous-loop-dynamic>>` sentinel unconditionally
