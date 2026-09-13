# Rationale: The Vessel Sidecar as the Fourth `stop-requested` Caller

Referenced from `.claude/rules/stop-hook-compliance.md` (the 2026-09-12 exception)
and implemented in `zakcode.session.framework_stop` (Zak-Code, out-of-repo).
Why a served run's ending is raised as the framework's own graceful stop.

## Why a fourth caller at all

A hosted vessel runs a Mind, and a Mind's ending is a protocol: consolidate,
write the handoff, drop to `assistant`, go IDLE. The vessel's conductor owns
something different and narrower — WHEN a run ends. user's ruling (guard-1807,
ZDS world) draws exactly that line: Vinheim decides when (credit, the human, the
clock) and never WHAT the agent does; no injected prompts.

Before this caller existed the two were fused, and the fusion ran the wrong way.
At the cap the conductor interrupted the in-flight turn and then spent its
reserved budget on a prompt we wrote for the agent ("write a short first-person
recap addressed to your customer..."). So the run ended with a recap and WITHOUT
the framework's consolidation or handoff — the receipt was ours, the ending was
severed, and nothing in either system reported a problem. A perpetual turn is
essentially always in flight, so the interrupt was the ending on every served run.

Raising `stop-requested` hands the ending back. The mind reaches Phase -1.4 on its
own next check and stops itself, the way every other stop in the fleet happens.

## Why it is ADDRESSED, not discretionary

The three framework-script callers are script-gated on a predicate the model cannot
influence. This one cannot be, because it lives in another process in another repo.
The equivalent guarantee is that it is an ADDRESS, not a decision: without a
configured agent name there is no session dir to signal and the raise is a no-op.
A non-seed workspace is therefore untouched by construction, and the conductor
keeps the interrupt ending it always had.

That is also why the setting is an agent NAME rather than a boolean. A boolean
would be a policy knob (the codebase's no-knobs ruling forbids those, and "a
disabled knob is still a knob"); a name is the only thing the caller genuinely
cannot derive.

## Why the reserve became the grace

`run_consolidation_reserve` was always the budget for "the ending" — seconds carved
OUT of the cap so that whatever ends the run still has clock. It used to buy the
injected digest turn. It now buys the framework's own consolidation and handoff:
same seconds, same place in the timeline, spent on the real ending instead of a
prompt. Nothing new is added to the cap, so a paid run's ceiling is unchanged.

The interrupt is not removed — it is demoted to the backstop it should always have
been. The watcher raises the stop, then keeps watching only to bound it; a stop
that lands inside its grace never sees an interrupt, and an overrun still gets one.
An interrupted turn cannot consolidate, so the ordering is the whole property.

## Why the grace must be sized, not guessed

Three timeouts sit in series behind a served run and the SHORTEST one decides the
ending: the sidecar's grace, the env-server's terminate wait, and systemd's
`TimeoutStopSec`. A grace larger than either of the two below it is not a grace —
the process is killed mid-consolidation and the ending is severed again, one layer
down, with the sidecar's logs showing a perfectly healthy stop in progress.

Size the outer two from a MEASURED graceful stop rather than from the reserve's
nominal value, and keep the sidecar's grace the smallest of the three.

## Cross-references

- guard-158 — only `/stop` writes `stop-requested`; `stop-target-mode` FIRST, no
  fallback on the read, revert the mode file if the signal write fails
- guard-6251 — `sessions/<SID>/stop-requested` is a turn-end permit and does NOT
  stop the loop; the agent-level `session/stop-requested` is the one that does
- guard-1807 (ZDS world) — Vinheim decides WHEN a run ends, never WHAT the agent does
- `.claude/rules/stop-hook-compliance.md` — the authorized-caller list this explains
- `core/config/rationale/deadman-switch.md` — sibling: the other mechanism that
  decides whether a loop keeps living
- g-373-16 — the goal; lane 368+369+FW, filed from ZDS-Mind's perception-bridge program
