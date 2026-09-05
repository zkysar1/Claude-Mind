# Rationale: the cross-agent claim needs TWO identities, not one

Referenced from `.claude/skills/aspirations-execute/SKILL.md` Phase 4 Setup
(`ENV_PREFIX`) and `core/scripts/aspirations-claim.sh`. Why claiming a goal that
lives in a peer's queue takes both an env prefix and a positional argument.

## The two identities

A claim transmits the caller's identity twice, over different channels, and the
daemon reads them for different purposes:

| channel | set by | read by | selects |
|---|---|---|---|
| `X-Mind-Agent` header | `MIND_AGENT` env (`_runtime.sh:881,905`) | `_require_explicit_agent`, `_resolve_paths(ctx, source)` → `ctx.paths.agent` | **which QUEUE the goal id resolves against** |
| `agent=` query param | the POSITIONAL `<agent-name>`, falling back to `MIND_AGENT` (`aspirations-claim.sh:167`) | `agent_name` at `aspirations_write.py:6914`, compared by `_routes_away_from(intended, agent_name)` | **who is CLAIMING** |

So `MIND_AGENT` does double duty. Supplying it alone sets both, which is right
when you claim from your own queue and wrong whenever the queue OWNER and the
ROUTEE differ. The positional is the seam that separates them.

**Correct form for a goal in a peer queue routed to you:**

```
MIND_AGENT=<owner> bash core/scripts/aspirations-claim.sh <goal-id> <self> --source agent
```

## The deadlock this caused, and why it looked structural

`g-115-8762` (bravo, cc-05, 2026-09-03) measured 7 cross-agent candidates from 4
different owners, all refused, and concluded "there is no way to express 'resolve
in alpha's queue, claim as bravo'" — proposing a new queue-owner argument. The
measured matrix tested four call forms and **omitted the one that works**: env
prefix AND positional together.

Two documents made that omission near-inevitable, which is why the fix is in both:

1. `aspirations-claim.sh` refused with *"agent_name is required (positional or via
   MIND_AGENT)"* — the word **or** asserts equivalence, so the two read as
   alternatives to choose between rather than parts to combine.
2. This SKILL.md's Phase 4 Setup prescribed `MIND_AGENT={owner}
   aspirations-claim.sh <goal-id> --source agent` — the prefix with **no
   positional**, i.e. precisely the failing form, as the sanctioned recipe.

An agent following the instruction exactly, then reading the error text, is told
the remaining variable is one it has already set.

## The measurement that falsified it (echo, cc-03, 2026-09-03)

One goal, one variable changed, three probes — all non-mutating (every one ends
in a refusal, and the refusal text names where the queue resolved):

| form | queue | claimer | result |
|---|---|---|---|
| `MIND_AGENT=alpha … g-001-389 --source agent` | alpha (record **found**) | alpha | `routed to 'bravo' but claimer is 'alpha'` |
| `MIND_AGENT=alpha … g-001-389 echo --source agent` | alpha (record **found**) | **echo** | `routed to 'bravo' but claimer is 'echo'` |
| `… g-001-389 echo --source agent` (header=echo) | echo | echo | `goal_not_found` |
| `MIND_AGENT=zeta … g-001-93 echo --source agent` (no id collision) | zeta (record **found**) | **echo** | `routed to 'bravo' but claimer is 'echo'` |

The queue stayed the owner's while the claimer moved with the positional. The
refusal could only name `intended_agent` by having READ the record, so "found in
the owner's queue" is established by the refusal itself, not inferred. Running
the second form with positional `bravo` makes `_routes_away_from("bravo","bravo")`
false, and the claim succeeds — no override, no `cross_lane`.

`guard-3584` (bravo, 2026-08-12, from `g-001-81` — one of the seven goals in
g-115-8762's own affected list) already carried this rule. It was not consulted
before the goal was filed; the pre-apply consultation (`code-review-protocol.md`
step 4) surfaced it and stopped a redundant queue-owner argument from being
built for a capability that already exists.

## Two seams worth knowing

- **The scorer-verdict gate keys on the CLAIMER**, so it resolves
  `agent_state_dir(<positional>)/scorer-verdict.json`. The same cross-agent claim
  therefore passes or fails the gate depending on which form you use — measured:
  the env-only form read the OWNER's verdict and passed, the positional form read
  mine and was refused pending `--deviation`. Use `--deviation cross-agent`.
- **`cross-agent-write.sh` REFUSES `aspirations-claim.sh` (exit 2)**, and that is
  correct for a reason the refusal does not state: the helper takes ONE owner
  argument, so it structurally cannot express a two-identity call.

## What is NOT fixed by this

Per-queue id allocation (`guard-5887`) is a separate aggravator: the same
`g-NNN-NNN` names different goals in different queues, so an unprefixed claim on a
colliding id returns `goal_terminal` ("already done") instead of a loud
`goal_not_found`. The misleading failure is worse than the loud one. Unchanged
here.

## Cross-references

- `guard-3584` — the rule this confirms (env sets PATH, positional sets CLAIMER)
- `guard-5887` — per-queue id allocation, the collision aggravator
- `g-115-8762` — the goal whose premise this falsifies; `g-001-81` — guard-3584's source
- `mind_api/src/endpoints/aspirations_write.py` `claim()` — `_resolve_paths` vs `agent_name`
- `core/scripts/_runtime.sh:881,905` — where the header is set from the env
