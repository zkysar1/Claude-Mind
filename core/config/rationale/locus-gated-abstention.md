# Locus-Gated Abstention — why Phase 2.55's double-abstention defer is conditional

Rationale for the `locus_gated` branch in `.claude/skills/aspirations-select/SKILL.md`
Phase 2.55 (Self-Abstention Check). Filed as g-115-9179; landed 2026-09-06.

## The anti-pattern

The double-abstention defer's premise is *"both agents abstained, therefore nobody
can do this."* That premise is **false whenever the blocker is a LOCUS rather than a
skill** — a goal needing a capability that exactly ONE box has: a GUI/Studio host, a
127.0.0.1-bound bridge, an attached instance profile, a physical device.

There, every OTHER agent abstaining is **correct and expected**. The rule reads N-1
correct abstentions as proof of impossibility and defers the goal — hiding it from
the one agent that can do it. **The more agents behave correctly, the faster the goal
is stranded.** A defer suppresses a goal in every selector, so "someone else's job"
silently becomes "nobody's job".

Three existing guardrails converge on this and none of them reached Phase 2.55:

- **guard-2937** names the class: two individually-correct safety mechanisms
  composing into a dead end that silently disables a capability. Correct abstention
  and correct ping-pong prevention are each right; their composition is not.
- **guard-4310** states it from the defer side: ask whether a blocker is a TIME or a
  LOCUS, because "a defer suppresses the goal on every box INCLUDING the one that
  satisfies it."
- **guard-2361** gives the disposition: for a machine-bound capability the answer is
  ROUTING (`intended_agent` + handoff fields), **never** a defer. It also warns that
  the obvious `defer_reason: precondition_unmet:<host>_required` shape is itself a
  trap, because `precondition-defer-recheck` evaluates only STRUCTURED dict
  preconditions and skips free-form ones — so a host-gated defer can never auto-clear
  and sits until a human notices.

## Why the original branch is narrowed, not deleted

Both branches have live populations. Measured 2026-09-06 (alpha, DESKTOP-O91DLK2)
over 2,398 pending goals via core/scripts/aspirations-query.sh --goal-status pending
--full, with the field declaration read from core/scripts/_goal_fields.py and the
consuming comparison from core/scripts/goal-selector.py: **16 carry `abstained_by`.** Evaluated as `alpha`:

| branch | count | goals |
|---|---|---|
| LOCUS-GATED (new — no defer) | 7 | incl. g-318-21, g-326-84, g-350-10, g-350-207, g-350-331 |
| DEFER (original case, still fires) | 5 | g-115-20, g-115-22, g-115-7707, g-250-107, g-335-199 |
| ELSE (same agent re-abstaining) | 4 | — |

Of the 16, **9 are locus-gated and 6 of those are HIGH**. The defer branch is
therefore correct for a real population and must not be disabled outright; it is
made conditional.

The live instance that produced the goal: **g-350-207** (HIGH,
`intended_agent=foxtrot`, needs a live DEV Studio session) already carried
`abstained_by=zeta` at 2026-09-06T07:59:45. Under the old rule the next headless
agent to reach Phase 2.55 was *instructed* to defer a HIGH goal whose outcomes 1/2/5
were already done and whose PR was open. An agent that declined to defer it was
deviating from the written skill, not complying with it.

## Why the locus branch writes no `abstained_by` either

**Not overwrite.** `abstained_by` is a **scalar** (`core/scripts/_goal_fields.py`).
Stamping your own name over the first abstainer's simply re-arms the same trap
pointing the other way — the exact failure this exemption exists to stop.

**Not accumulate into a list.** `core/scripts/goal-selector.py` tests it as
`goal.get("abstained_by") == AGENT_NAME` — a scalar equality — so a list **never**
matches and abstention suppression silently stops working fleet-wide. This is not
hypothetical: **g-250-107 already carries `abstained_by: ['echo']`** and its
abstention has consequently never suppressed anything. Changing the field's shape
means changing every reader first (guard-562); that is a different goal.

## The accepted cost, recorded so it is not rediscovered as a bug

Because the locus branch records nothing, the goal keeps resurfacing to the same
agent on later cycles and it will re-abstain each time. That is deliberate and it is
the cheap direction: **a repeated no-op beats a stranded HIGH goal.** The re-offering
itself is a separate, already-known mechanism — idle-reallocation re-admitting
routed-away goals when `intended_agent` is in `idle_agents` (g-115-1766 gap #4) — and
is not fixed here.

## Known limitation

Phase 2.55 is LLM-executed pseudocode with **no bash gate enforcing this branch**
(guard-399's pairing is unmet). The change narrows an existing branch rather than
adding a new mandatory action, so guard-399's trigger is not strictly met, but the
absence of enforcement is real: nothing detects an agent that defers a locus-gated
goal anyway. The natural chokepoint is `capability-gate.py`, which already runs at
`cmd_update_goal` when `field == defer_reason` — a `locus_gated` predicate there
would cover **every** door to this stranding, not just this one. guard-2937's own
action hint ("audit the whole allowlist against every mandating rule, not just the
one that surfaced") argues for exactly that, and the goal record names a second door
already: `requires_capability` applied without the host first asserting
`RUNNER_CAPABILITIES_PROVIDES`. Out of scope for g-115-9179; relayed for an owner.

## Cross-references

- `.claude/skills/aspirations-select/SKILL.md` Phase 2.55 — the branch this explains
- guard-2937, guard-4310, guard-2361, guard-562, guard-399
- g-115-9179 (this fix), g-350-207 (the live instance), g-115-1766 gap #4
  (idle-reallocation re-offering)
