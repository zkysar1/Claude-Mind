---
description: "Re-derive everything routed away (questions, user-participant goals, blocked/deferred goals) on both axes: premise true AND reason valid."
---

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

## The Two Re-Check Axes

**Both axes must be checked.** An item is genuinely blocked only when the
PREMISE is still true (re-probe it) AND the REASON is still valid (a grant
or convention can retire an excuse class while the condition stays true).
Incident traces and sweep-tooling map: `core/config/conventions/defer-routing.md` §5.

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

4. **Name grants machine-findably**: when recording a standing grant, name the
   specific `defer_reason` text or `user_leg_scope` token it invalidates
   (guard-5518, guard-6262).

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

7. **A reclaim predicate must not be narrower than the creating gate** —
   diff predicates literally (guard-1802, rb-5650), and check which store
   the gate writes DURABLY vs which store you read (guard-1978, guard-1242).

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
  once measuring what its predicate EXCLUDES (rule 7)
- Auto-dropping `user` from participants on a fuzzy or prose match. Adding the
  agent is reversible; removing the human is not. When the evidence is a prose
  cell, match only its declarative head, accept under-matching, and leave the
  decision with a reader
- Re-routing an item to the user a second time with the same reason, without
  recording that the first routing has now aged

## Cross-references

- `core/config/conventions/defer-routing.md` §5 — incident traces, the
  sweep-tooling map (lane P/B/Q auditors, the PREMISE-axis recheck family,
  the scope SSOTs) and the rb/guard entries behind each rule
- `.claude/rules/capability-before-user.md` — gates the routing decision at
  creation; this rule governs the accumulated backlog it leaves behind
- `.claude/rules/probe-before-defer.md` — gates the defer at write time; its
  rule 4 (re-probe on re-entry) is the PREMISE axis of this rule
- `.claude/rules/verify-before-assuming.md` — a stale routing decision is an
  unverified negative claim about the agent's own capability
- `core/config/conventions/learning-routing.md` — where reclaim findings go
