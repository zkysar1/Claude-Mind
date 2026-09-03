---
name: close-review
description: "Adversarial independent close-time review for tier-2 goals. Receives ONLY the goal record, the artifact paths, and an adversarial mandate — never the executing context — runs the four mandatory checks (requirements traceability, source fidelity, criteria adequacy, side-effects/scope), and emits the verdict artifact that close-review-gate.py reads. A REJECT both blocks the close and routes its defects back into the goal, so rework is an ordinary loop iteration rather than a special state. Invoked from the close path when the risk tier resolves to 2; not a user command."
user-invocable: false
parent-skill: aspirations
tools_used: [Bash, Read, Grep]
companion_scripts: [core/scripts/close-review-verdict.py, core/scripts/close-review-gate.py, core/scripts/liveness-check.sh]
conventions: [goal-schemas, coordination, aspirations]
minimum_mode: autonomous
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
revision_id: "skill-bootstrap-close-review-7f1a2c"
previous_revision_id: null
---

# /close-review — Adversarial Independent Close-Time Review

The verdict-PRODUCER half of the blocking close-review gate. The consumer
(`close-review-gate.py`) refuses a tier-2 close without an APPROVE verdict at
the goal-keyed path it owns. This skill is what writes that verdict.

## The One Rule That Makes This Worth Running

**The reviewer never sees the executing context.** It receives the goal record,
the artifact paths, and the mandate below — nothing else. A reviewer handed the
executor's reasoning inherits the executor's blind spots and re-derives the
executor's conclusion, which is a rubber stamp with extra steps. If you cannot
run this without the executing context in view, escalate to a fresh-context
reviewer per Step 1 instead of proceeding.

## Step 0: Load Conventions

`Bash: load-conventions.sh` with each name from the `conventions:` front matter.

## Step 1: Establish Independence

Independence is the GATE's definition, not this skill's — the producer imports
`independence_defect` rather than reimplementing it, so a self-review is refused
at production time (`test_self_review_is_REFUSED_at_production_time`). Choose
the reviewer before assembling anything:

```
Bash: bash core/scripts/liveness-check.sh --agent <candidate-peer> --json
```

- **`alive`** → MIND-level review. Route through the existing review-request
  lane per `coordination.md` Review Gate. A peer mind carries independent
  priors and so also catches shared-prior blind spots.
- **`dormant` / `unknown` / `retired`, or no peer exists** → fresh-context
  subagent fallback. Always available, including on solo deployments.
  Session-level fresh eyes is sufficient for the whole did-not-re-read-the-
  source class: the founding incident was refutable from the goal description
  text alone.

Never conclude a peer is unavailable from a stale `last_active` alone
(`check-team-state-before-silent.md` rule 5) — `unknown` is not `dormant`.

## Step 2: Assemble the Reviewer's Packet

Exactly three things, and nothing more:

1. **The goal record** — title, description, `verification` criteria, and every
   referenced source message id RESOLVED TO ITS TEXT. An unresolved id is a
   hole in check 2; resolve it or record it as unreviewable.
2. **The artifact paths** produced by the goal.
3. **The adversarial mandate** — Step 3.

## Step 3: The Four Mandatory Checks

Run all four. Record each in `--check`, whether it passed or failed; a check
that was not run must not be reported as one that passed.

1. **Requirements traceability** — every `verification.outcome` maps to concrete
   produced evidence. QUOTE the evidence. An outcome you cannot quote evidence
   for is NOT MET, and a partially-met bar may not be narrated away
   (`guard-2541`).
2. **Source fidelity** — diff EVERY enumerated entity in the description/source
   against the artifact VERBATIM. This is the mechanized check and the one the
   founding incident turned on: a source enumerated 16 entities, the artifact
   carried 16 entities, the count-based criterion went green, and 6 identities
   had been silently substituted. A count cannot see that; a set difference sees
   it instantly. Do not hand-roll the diff — Step 4 runs it.
3. **Criteria adequacy** — construct one plausible WRONG artifact that would
   still pass the stated criteria, then test the REAL artifact for exactly that
   wrongness. This is what catches count-vs-identity and existence-vs-content at
   close even when filing-time lint missed it.
4. **Side-effects / scope** — files touched outside the goal's scope, broken
   conventions, dead code left behind.

## Step 4: Produce the Verdict

The script owns the artifact path, the schema, and the entity regex — do not
re-derive any of them here. It shares `named_entities` with the tier classifier,
so a goal can never be routed to review for entities the reviewer cannot see.

```
Bash: py -3 core/scripts/close-review-verdict.py \
        --goal <goal-id> --reviewer <reviewer-id> --closer <closing-agent> \
        --source-file <path> --artifact-file <path> \
        --check <check-name>:<pass|fail> ... \
        [--finding "<defect>"] ... \
        [--approve | --reject] \
        --route-to-goal <world|agent> --write
```

**The label never outruns the predicate** (`guard-2564`). `--approve` is an
assertion YOU make about the judgment checks 1, 3 and 4, and it is REFUSED
outright when the mechanical fidelity diff is non-empty. The asymmetry is
deliberate and load-bearing: the machine may VETO an approval on its own
evidence and may never GRANT one. A verdict the script emits on its own
authority is always a REJECT.

The record reproduces its own verdict (`guard-3743`) — it carries the source
set, the artifact set, and BOTH directions of the diff, so a later reader can
recompute the verdict without the session that wrote it.

## Step 5: A REJECT Must Reach the Goal

`--route-to-goal` appends the findings to the goal's `progress_note` through the
shared writer. Without it the defects live only in a verdict artifact nothing
reads at claim time, and the next Body either re-derives them or misses them —
"blocks the close" without "routes the rework" is a stall, not a review.

The goal stays NON-TERMINAL and returns to selection. Rework is an ordinary loop
iteration, not a special state; a re-review is let through by the marker, a
repeat of the same review is not.

## Cost Posture

Deliberate, per user directive: a tier-2 close spawns a full fresh-context
reviewer every time. **Tiering is the cost control** — tier 0 and tier 1 goals
skip this skill entirely and pay nothing.

## Return Protocol

See `.claude/rules/return-protocol.md` — the last action must be a tool call,
not text. The terminal action is the `close-review-verdict.py` invocation in
Step 4 (or the `liveness-check.sh` call when Step 1 escalates and this skill
hands off). Never end with a prose summary of the review; the verdict artifact
IS the output.
