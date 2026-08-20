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
that a named condition "is no longer a valid `defer_reason`"; the condition
was still true and was re-probed correctly on a cadence, so two goals stayed
frozen 6 and 8 days past the grant — every sweep tested the premise and
nothing tested the rule. Full trace and the sweep-tooling map:
`core/config/conventions/defer-routing.md` §5 (`load-conventions.sh
defer-routing`).

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
   The obligation runs in BOTH directions: a routing that never declares WHY
   the user is attached (`user_leg_scope`) cannot be re-derived by anything,
   and a grant whose scope head avoids the shared vocabulary can never be
   applied. Measured 2026-07-29: 20 of 28 `[agent,user]` unscoped, 4 of
   5 grants unkeyable. 2026-08-19: 8 of 36 - better, not fixed.

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
   population** — the gate's *correct* operation then fills the blind spot and
   the sweep reports clean forever (measured: `audit-user-to-agent.py` required
   `participants == ["user"]` while the creating advisory tested `"user" in
   participants`; live candidate set zero against 28 invisible goals —
   guard-1802, rb-5650). Before trusting any reclaim sweep, diff its predicate
   against the creating gate's, literally, and measure what it EXCLUDES. And
   the variant a predicate diff cannot catch: **which store does the creating
   gate write DURABLY, and is that the store I am reading?** —
   `blocker-recheck.py` read the ephemeral WM slot while the durable
   `blocker_ref` on six goals went unread, `total_blockers: 0` (guard-1978,
   guard-1242). Widen the READ; do not assume the WRITE widens with it.

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

- `core/config/conventions/defer-routing.md` §5 — incident traces, the
  sweep-tooling map (lane P/B/Q auditors, the PREMISE-axis recheck family,
  `gates/user_leg_scope.py` + `gates/defer_scope.py` SSOTs,
  `defer-scope-coverage.py`), rb-5633 / rb-5650 / rb-7289, guard-1783 /
  guard-1802 / guard-349
- `.claude/rules/capability-before-user.md` — gates the routing decision at
  creation; this rule governs the accumulated backlog it leaves behind
- `.claude/rules/probe-before-defer.md` — gates the defer at write time; its
  rule 4 (re-probe on re-entry) is the PREMISE axis of this rule
- `.claude/rules/verify-before-assuming.md` — a stale routing decision is an
  unverified negative claim about the agent's own capability
- `core/config/conventions/learning-routing.md` — where reclaim findings go
