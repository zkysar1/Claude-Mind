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

**Echo-harness extension (g-115-1672, 2026-06-27, zeta).** A prior-window echo
observation claimed "emitting [ScheduleWakeup, Skill] stalled -- Skill never
fired, stop-hook bounced," raising whether echo's harness differs. REFUTED on
three independent signals: (1) echo's OWN aspirations re-entry cadence on
2026-06-27 shows gaps of 2.5 / 7.0 / 7.7 min between consecutive
Skill(aspirations) invocations -- all FAR under the 600s wakeup interval, which
is ONLY reachable by Skill-driven re-entry (the wakeup cannot fire faster than
600s and is re-armed forward each iteration), so echo's Skill re-entry IS
firing; (2) echo runs with deadman ON (no agents/echo/session/deadman-disabled
flag) and is alive (21 aspirations invocations across 2026-06-27); (3) a live
zeta first-hand reproduction this same iteration -- ScheduleWakeup returned its
expected turn-terminal "Nothing more to do this turn" message, and the
Skill(aspirations) emitted AFTER it STILL fired and re-entered the loop. The
prior echo claim was a MISREAD of that benign "Nothing more to do this turn"
message (F2's expected text), not a confirmed harness difference. Q5 now holds
across THREE harnesses (charlie, echo, zeta). Action: do NOT set
agents/echo/session/deadman-disabled.

Fail-safe property: if Q5 turns out false (Skill does NOT re-enter after the
arm), the terminal response's wakeup still fires after 600s and resurrects the
loop via the sentinel — so the loop runs at 600s/iteration (slow) but stays
ALIVE and keeps re-arming. The worst case is a slow loop, never a dead one. The
change cannot make survival worse than the status quo (silent death).

## Q6 — RESOLVED FAVORABLE (2026-07-31): overdue wakeups DO fire on next idle

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

**MEASURED 2026-07-31 — the FIRST bullet holds. Overdue wakeups fire on next
idle, essentially instantly.** Observed on foxtrot (`LAPTOP-3IOFCNEO`, WSL2,
kernel `6.6.87.2-microsoft-standard-WSL2`, session `86dd43e9`), read from its
transcript JSONL + `core/logs/stop-hook.log` over SSH:

| | overdue case | on-time control (same day, same agent) |
|---|---|---|
| arm | `09:02:34` delay=600 | `07:36:53` delay=600 |
| due | `09:12:34` | `07:46:53` |
| session state at due | **BUSY** (iteration ran 1h47m; diary `phase-4-execute` 09:17:51) | idle-bound |
| text-death | `09:42:06` (assistant text, no tool call; stop-hook `ALLOW gate=background-jobs` 09:42:07) | `07:39:38` |
| **wakeup fired** | **`09:42:22` — ~16s after idle, ~29.5 min OVERDUE** | `07:47:04` — 11s after due |

So the Q6 residual ("deaths during iterations longer than `delaySeconds` are NOT
netted") **does not exist**. The arm survives its scheduled time while the
session is busy and fires when the session next goes idle — which is exactly the
moment a text-death creates. The narrow uncovered case Q6 reserved for the
unreliable Stop hook is in fact covered by the deadman.

Two consequences. The "more surface" mitigations Q6 floated — a longer delay, or
a mid-long-goal re-arm checkpoint — are **not needed**; do not spend the
complexity. And 600s stays correct for the common case without trading anything
away on the long-iteration case.

Corroborating the *other* half of the design in the same trace: the resurrected
turn at 09:42:23 re-armed FIRST (`reason: "Re-arm deadman net first (rb-4345)"`)
before any other work — the rb-4345 rule below is being followed in production,
not merely documented. That resurrection cycle then self-recovered on schedule;
a human happened to intervene 28s before the next net was due (09:51:55 vs
09:52:23), which is an OBSERVABILITY gap, not a liveness one — see the
turn-end-visibility lane filed 2026-07-31.

This cannot be eliminated by delay tuning alone: no delay ≤ the 3600s clamp
covers an arbitrarily long iteration, and raising the delay to cover long
iterations slows resurrection for the common (short-iteration) case. 600s
optimizes the common case; the residual is the rare intersection of (iteration
> 600s) AND (death at its terminal) AND (Stop hook also missed it). Validate Q6
alongside Q5 on the controlled runner; if unfavorable, options are a longer
delay (trades common-case latency) or a mid-long-goal re-arm checkpoint (more
surface). Documented here so the residual is explicit, not silent.

## The resurrection-death gap and the re-arm-first fix (rb-4345 / g-115-2771, 2026-07-19)

Q5's fail-safe reasoning ("worst case is a SLOW loop, never a dead one") rested
on an unstated assumption: that the resurrected turn RE-ARMS the net before it
can die again. That assumption has a hole. The net is a SINGLE replace-slot
wakeup, and FIRING it consumes it, so a resurrected turn begins with **no net
armed** — it is protected again only once its own terminal-pair re-arms at
iteration close. If the resurrected turn text-dies BEFORE reaching that close,
the single net is already spent and nothing remains to resurrect the loop — a
SECOND silent death with no third net.

This is not hypothetical. On 2026-07-19 (cc-04, SID aae8287f, rb-4345): a turn
text-died at 23:57; the deadman wakeup fired at 00:00:40 and resurrected the
loop (the net worked exactly as designed); but the resurrected turns text-died
again between 00:00 and 00:04 during an Anthropic API storm — 529 Overloaded /
ECONNRESET / request timeouts made the first tool calls fail, and the model
narrated the failures instead of re-entering the loop — WITHOUT re-arming. The
single replace-slot net was already spent by its own firing, so no further
wakeup existed. The loop stayed dead ~7h until the SessionStart recovery-gate
zombie-recovery flipped it IDLE at 07:18.

The failure mode is WORSE than the original single death the terminal-pair was
built to net, because the condition that killed the loop the first time is often
the SAME condition still active when the 600s resurrection fires: an API storm
lasts minutes, the resurrection latency is 600s, so the resurrected turn is
statistically LIKELY to die the same way — and by then the net is already gone.

**Fix (g-115-2771): re-arm FIRST on resurrection.** On a
`<<autonomous-loop-dynamic>>` wakeup firing, the resurrected turn's FIRST tool
call MUST be a `ScheduleWakeup(prompt="<<autonomous-loop-dynamic>>",
delaySeconds=600)` re-arm — restoring the net BEFORE any loop-entry work that
could fail — THEN proceeding to Phase -1.5. This converts "death during
resurrection" from a terminal silent death into at most one more 600s
resurrection cycle: each resurrection re-establishes the net that protects the
NEXT resurrection, so the loop keeps getting chances for as long as the API
storm persists, instead of spending its one-and-only net on the first
resurrection.

Why this does NOT reintroduce the "arm early" mechanic F2 rejected (see "Why the
terminal-PAIR" above): F2 rejected arming early in a STEADY-STATE iteration,
where the arm is turn-terminal and would TRUNCATE the multi-turn iteration
before its work completed. The resurrection re-arm is different in kind — it is
a one-shot net-restoration at the very START of a resurrection turn whose ONLY
job is to re-establish the net the firing consumed; the iteration then proceeds
normally and its terminal-pair re-arm at close simply REPLACES this restoration
arm (double-arm is harmless under replace-slot semantics). The steady-state
terminal-pair is unchanged.

Relationship to Q6 (long-iteration death): Q6 is about the net being OVERDUE
while the session is BUSY; this gap is about the net being SPENT while the
session is idle-then-resurrected. They are independent residuals — the
re-arm-first fix closes THIS one regardless of Q6's resolution.

**Generalized 2026-08-11 (g-115-5834): the trigger is "no terminal pair has been
emitted", not "the net fired".** An autocompact resume reaches the same
unprotected state from the opposite direction — the net is armed at iteration N's
terminal PAIR, so a `SessionStart:compact` resume that re-enters the loop body
MID-iteration emits no pair at all and runs that entire iteration on whatever net
already existed. Measured (bravo, `hostname` cc-05, `uname -r` 6.8.0-137-generic,
session 0a35f258): resumed from autocompact #2 with in-flight goal g-001-02 at
phase `selected`, the turn ended inside a spark sub-skill before the terminal
pair, and the execution diary went silent 03:19:39 → 11:06 — **7h47m** — while 43
goals completed across zeta, alpha and echo inside that same window. The obvious
objection ("the prior iteration's net was still pending") is falsified by
arithmetic: `delaySeconds` is clamped to [60, 3600], so any armed wakeup fires
within an hour and no pending net can span 7h47m.

⚠ **THAT ARITHMETIC IS FALSIFIED BY MEASUREMENT — the clamp bounds when a wakeup
becomes ELIGIBLE, not when the session is RE-INVOKED (rb-8256).** Measured on
echo/`hostname` cc-03, `uname -r` 6.8.0-137-generic, own-cloud, 2026-08-18: a net
armed at ~00:33 with `delaySeconds=640`, tool result naming a fire time of
**00:48:00**, against a last durable write (guard-4241) at **00:34:19** — so idle
began at 00:34 — and the resulting `<<autonomous-loop-dynamic>>` firing re-invoked
the session at **07:09:32**. That is 6h35m of silence and **6h21m past the
scheduled fire**, on a 640s net. So a pending net CAN span multi-hour gaps, and
the paragraph above cannot rule one out by clamp arithmetic alone.
**CAUSE IS UNMEASURED and must not be asserted** — a `/login` occurred at ~07:16,
minutes AFTER the firing, which is suggestive of a session-auth interruption but
post-dates the wakeup and therefore explains nothing on its own; a platform-side
scheduling delay is equally live. What this does NOT change: the deadman is still
fail-safe in DIRECTION (worst case a slow loop, never a dead one), and the
re-arm-first rule is still correct. What it DOES change is that "no net was armed"
was never the only available explanation for the 7h47m gap, so the re-arm remedy
is necessary and possibly not sufficient — and the "~10-minute resurrection
latency" claimed in § Why 600s below is an upper-bound-under-ideal-delivery, not a
measured property.

### The distribution, measured (g-115-6629, 2026-08-19)

The paragraph above is n=1. Here is the population, from the transcripts
themselves — 9 files, 1.25M records, 4,336 armed wakeups, 2026-07-10 → 08-19:

| agent / box / `uname -r` | due gaps | p50 | p90 | p99 | max |
|---|---|---|---|---|---|
| zeta / cc-02 / 6.8.0-137-generic | 1,424 | 149s | 269s | 1.90h | 17.37h |
| echo / cc-03 / 6.8.0-137-generic | 1,262 | 137s | 226s | 3.75h | 39.98h |

Method: for every idle gap ≥120s, find the newest wakeup armed at or before the
silence began; the net becomes ELIGIBLE at `max(scheduled_fire, gap_start)` (it
cannot fire while the session is busy) and latency is `gap_end - eligible`. Both
halves come from one transcript on one machine, and transcript stamps are
ISO-8601 Z (guard-2841 / guard-2613 satisfied). Gaps are split by what BROKE the
silence, because a human typing `continue` is a different rescue mechanism: the
`human-typed` class (n=19 / n=16) has p50 **7,106s / 14,328s** and max **39.98h**
— folding it in would credit the deadman for rescues it never performed. Excluding
it, the deadman-eligible class maxes at **5.74h (zeta) / 17.09h (echo)**.

**VERDICT: delivery is NOT bounded.** The body is fast and strikingly consistent
across boxes (p50 ≈ 2.3 min, p90 ≈ 4 min, p50 identical at 121.0s in the
wakeup class on both), but the tail runs to hours. p99 alone is 1.9h and 3.75h.

The structural reason is stronger than the statistic: **the net is delivered by
re-invoking the very session that went silent.** If the session, process or
container is gone, there is nothing to re-invoke, and delivery waits on an
unbounded external event. An in-session net cannot detect its own session's
absence — so "worst case a SLOW loop, never a dead one" holds only for a
text-death *inside a live session*, which is a strictly narrower claim than the
one § Why 600s makes.

**Do NOT respond by tuning `delaySeconds`.** The clamp floor is 60s and the
observed misses are 5.7–17.1h; no rung reaches them, because the delay is not
where the time goes.

**An external detector already exists — do not build a second one.**
`ayoai-fleet-sweep.timer` runs on `zakbox1` (OUTSIDE every container), classifies
all containers every 10 minutes, and emails on page-worthy conditions; since
g-115-6551 it pages only for autonomous-mode agents in a stopped state. That is
the bounded time-to-detection the in-session net cannot provide, and it is the
capability this section would otherwise have proposed. The residual gap is the
`zakbox1` SPOF — nothing watches the sweeper except its own stamp going stale —
already owned by `g-115-6324` clause (2)(i). See
`world/conventions/fleet-monitoring-coverage.md`.

⚠ **The `system` co-writer column is NOT independent corroboration.** The
2026-08-18 reading that read box/daemon-level absence from `system` changelog
rows tracking the agent column at exactly 43.8% does not support that conclusion:
`_fileops.py::_agent_name` returns `"system"` whenever `MIND_AGENT` is unset, and
**98.3% of all 130,325 `system` rows are writes to `presence/<agent>.jsonl`** —
emitted by `presence-tick.py`, a `PostToolUse` hook with `matcher='*'` that fires
once per tool call. The column is therefore driven entirely by the loop's own tool
calls, so a loop-only text-death drives it to silence at exactly the agent
column's rate. Two columns, one producer: the agreement is what BOTH hypotheses
predict. (`liveness_check.py` independently refuses presence as a cross-box signal
— it does not sync to S3.)

Both rule files therefore state
the re-arm requirement over BOTH entry points; the one-shot restoration argument
above (and its F2 reconciliation) applies unchanged, since a resume boundary is
not a steady-state iteration either.

Detective coverage (Layer C): `core/scripts/aspirations-rejection-audit.py` now
flags `resurrection_risk` by arm CADENCE — a gap between consecutive
deadman-sentinel arms exceeding 1h (longer than any legitimate iteration, so the
net went un-re-armed far too long) that ALSO contains a structured API-error
event (`isApiErrorMessage`). That is the exact 2026-07-19 signature; run over the
default 24h window on cc-04 it flags precisely the incident gap (23:38 → 07:32,
3 API errors) and nothing else. The structured-error requirement discriminates a
storm-death from a legitimate /stop idle period; event density is deliberately
NOT used — a storm-death is high-churn (~577 retry/hook lines over the 7.9h gap),
so density wrongly excluded the real incident when first tried. Catches drift if
the re-arm-first rule is missed; complements trailing-text-detector.py (non-storm
text-deaths).

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
