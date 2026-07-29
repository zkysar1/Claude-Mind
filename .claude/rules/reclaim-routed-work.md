# Reclaim Routed Work (MANDATORY)

## Principle

Every decision to route work AWAY from yourself — to the user, to a blocker,
to a defer, to a pending question — was made under the capability model you
held AT THAT MOMENT. Capabilities grow: grants land, skills are forged,
scripts ship, dependencies close, knowledge accumulates. So a routing-away
decision is a **hypothesis with an expiry, not a terminal state**.

The agent has a standing duty to periodically re-derive those decisions
against TODAY's capability surface and take back everything it can now do.

`capability-before-user.md` and `probe-before-defer.md` gate the routing
decision at the moment it is MADE. This rule governs everything already
routed away — the accumulated backlog those gates never revisit. Without it,
a gate that was correct on Monday silently holds work hostage forever.

## The Three Lanes

The duty covers three surfaces. They are one duty, not three chores — the
question is identical in each: *could I do this now?*

| Lane | Surface | The reclaim action |
|---|---|---|
| **Q — open questions** | `agents/<agent>/session/pending-questions.yaml`, status `pending` | Close it with evidence, or close it because the executed `default_action` stood unchallenged. |
| **P — user-participant goals** | Non-terminal goals whose `participants` include `user` | Drop `user` from participants, or close the goal outright if it is already satisfied or moot. |
| **B — blocked / deferred goals** | `status: blocked`, or any non-null `defer_reason` | Clear the defer / unblock, or re-state the block in terms that are still true. |

Lane P is the one most often skipped, because a goal carrying
`participants: [agent, user]` still *looks* like agent work and never
appears in a blocked tally. It is the largest silent accumulator.

## The Two Re-Check Axes (the load-bearing distinction)

A routed-away item can be freed two different ways, and **checking only the
first will hold work frozen indefinitely**:

1. **The PREMISE axis — is the condition still true?**
   Re-probe with the canonical code path. "The service is down" → is it
   still down? This is what every existing recheck sweep does.

2. **The RULE axis — is the reason still a legitimate reason?**
   A standing grant, a new convention, a forged skill, or a user directive
   can retire an entire class of excuse *while the underlying condition
   remains perfectly true*. When that happens the premise probe keeps
   returning "yes, still true" and correctly keeps the item frozen —
   forever. No amount of re-probing can free it, because the probe is
   answering a question that stopped being the deciding one.

**Both axes must be checked.** An item is genuinely blocked only when the
premise still holds AND the reason is still a valid reason to stop.

Canonical incident (2026-07-28, measured): a standing grant stated verbatim
that a named condition "is no longer a valid `defer_reason` — the correct
response is to start one." The condition itself was still true and was
re-probed correctly on a cadence. Two goals stayed frozen for 6 and 8 days
past the grant, stranding a cluster of downstream dependents, because every
sweep tested the premise and nothing tested the rule. The automated gate the
sweeps delegate to reads only the "agent-provisionable capability" table and
never the standing-grants table, so it returned `matches: 0` on the exact
text the grant had invalidated.

## Rules

1. **Re-derive, do not re-read.** When re-checking, ask "what would I decide
   about this item if I met it fresh today?" Do NOT re-read the stored
   `defer_reason` and assess whether it is well-argued — it will be, because
   you wrote it. Re-run the decision.

2. **Well-formed is not valid.** A structured prefix, a schema-conformant
   field, or a carefully-cited narrative attests that the author FORMATTED
   the routing correctly. It is not evidence the routing is still correct.
   Never let the presence of a structured marker short-circuit the re-check
   — that converts a formatting convention into a laundering mechanism, and
   the best-documented defers become the least re-examined.

3. **Age is a trigger, not a verdict.** An item routed away long ago is more
   likely to be stale, so age selects what to re-check first. It never by
   itself justifies closing something. Close on evidence.

4. **When you record a standing grant or convention that retires a class of
   excuse, say so in machine-findable terms** — name the specific
   `defer_reason` text, precondition id, blocker class, or `user_leg_scope`
   token it invalidates. A grant whose consequence is buried in prose cannot
   reach the sweep that needs it, which is precisely how the canonical
   incident happened.

   The same obligation runs in BOTH directions, and both halves were measured
   empty on 2026-07-29. A routing that never declares WHY the user is attached
   cannot be re-derived by anything: 20 of 28 `[agent, user]` goals carried no
   `user_leg_scope`, so no grant could reach them. And a grant whose scope
   *head* avoids the shared vocabulary can never be applied: 4 of 5 standing
   grants were unkeyable, carrying real permission the audit is structurally
   unable to act on. Declaring the leg and wording the grant are one duty seen
   from two ends — neither alone closes anything.

5. **Escalate what genuinely remains.** Items that survive both axes are the
   real human-only residue. Batch them into a digest for the next user
   check-in rather than re-probing them every cycle. An autonomous cadence
   can never close a genuinely-human item; its job is to make sure that set
   is SMALL and correct.

6. **The duty survives budget pressure.** These sweeps are individually
   droppable under context pressure, and dropping them is invisible — no
   error, no signal, just a queue that quietly grows. If the reclaim lanes
   have not run in a long while, that is itself the finding: run them, and
   record that they were skipped.

7. **A reclaim predicate must not be narrower than the gate that creates the
   population.** When it is, the gate's *correct* operation is what fills the
   blind spot, and the sweep reports clean forever while the backlog grows
   behind it. Measured: `audit-user-to-agent.py` required
   `participants == ["user"]` while the creation-time advisory tested
   `"user" in participants` — and because `capability-before-user.md` tells
   the fleet to file `[agent, user]` whenever both legs are real, every
   correctly-routed goal landed exactly where the audit could not see it.
   Live candidate set: zero, against 28 invisible goals. Before trusting any
   reclaim sweep, diff its predicate against the creating gate's, literally,
   and measure what it EXCLUDES rather than what it returns. A zero-result
   run and a genuinely clean queue produce the same output (guard-1802,
   rb-5650).

## Anti-patterns

- Re-probing the premise, finding it still true, and re-deferring — without
  ever asking whether the reason is still a valid reason (the canonical
  incident)
- Treating `participants: [agent, user]` as agent work that needs no review,
  because it does not show up in any blocked tally
- Closing a stale item on age alone, with no evidence probe
- Letting a structured prefix or schema-valid field stand in for a validity
  check (rule 2)
- Building the reclaim tooling and never invoking it — a sweep with no call
  site is indistinguishable from a sweep that always returns clean, and a
  presence-only verification check ("the script exists") will pass forever
  while it never runs
- Reading a long-running sweep's empty output as "the queue is clean" without
  once measuring what its predicate EXCLUDES (rule 4a)
- Auto-dropping `user` from participants on a fuzzy or prose match. Adding the
  agent is reversible; removing the human is not. When the evidence is a prose
  cell, match only its declarative head, accept under-matching, and leave the
  decision with a reader
- Re-routing an item to the user a second time with the same reason, without
  recording that the first routing has now aged

## Cross-references

- `guard-1783` — the enforcement-time rail: when a premise probe comes back
  still-true, check the RULE axis before re-deferring
- `rb-5633` — the measured incident trace behind this rule (orphaned sweeps,
  the 72.5% structured-prefix launder, and the untested rule axis)
- `guard-1802` / `rb-5650` — rule 4a: the audit predicate that was a strict
  subset of its creating gate's, so correct routing drained it to zero; plus
  the `user_leg_scope` <-> standing-grant join and why it matches only a
  grant's declarative head
- `core/scripts/gates/user_leg_scope.py` — SSOT for the scope vocabulary and
  the creation-time advisory whose predicate lane P must mirror
- `guard-349` — read the standing-grants section before routing to the user.
  This rule generalizes it: guard-349 is scoped to commit/push approval and
  is honor-system only, because no code reads that section.
- `.claude/rules/capability-before-user.md` — gates the routing decision at
  creation; this rule governs the accumulated backlog it leaves behind
- `.claude/rules/probe-before-defer.md` — gates the defer at write time, and
  its rule 4 (re-probe on re-entry) is the PREMISE axis of this rule
- `.claude/rules/verify-before-assuming.md` — a stale routing decision is an
  unverified negative claim about the agent's own capability
- `core/config/conventions/learning-routing.md` — where reclaim findings go
- `core/scripts/audit-user-to-agent.{sh,py}` — lane P auditor
- `core/scripts/audit-deferred-defers.{sh,py}` — lane B auditor
- `core/scripts/pending-questions-sweep.{sh,py}` — lane Q auditor
- `core/scripts/blocker-recheck.{sh,py}`, `defer-recheck`,
  `precondition-defer-recheck`, `credential-defer-recheck` — the PREMISE-axis
  recheck family
