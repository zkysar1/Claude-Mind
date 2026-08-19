# Defer Routing — the `defer_reason` chokepoint, its four enforcement layers, and Layer-D auto-conversion

Mechanism reference behind `.claude/rules/probe-before-defer.md`. The rule keeps
the imperatives (probe before deferring; narrative defers are suspect; defer is
expensive; re-probe on re-entry; expect two independent refusals). This file
carries the enforcement table with the ungated chat lane and why no gate is
possible there, the Layer-D auto-conversion invariants, the guard-3882
"best-probed defers get refused" gradient, the `blocker_ref_required` second
refusal, the `human_blocked:` caveats, and traceability. Loaded on demand
(`load-conventions.sh defer-routing`); moved out of the rule on 2026-08-17 under
g-115-6581 (context-window diet — the rule loaded ~13.3 KB on every turn of
every agent).

Sibling references: `core/config/conventions/goal-schemas.md` § Structured-prefix
bypass (schema of `STRUCTURED_DEFER_PREFIXES`; `gates/defer_classifier.py` is the
SSOT), `.claude/rules/capability-before-user.md` (the `participants: [user]`
door), `.claude/rules/reclaim-routed-work.md` (the accumulated backlog both
doors leave behind).

## 1. `human_blocked:` — what the prefix does and does not establish

`human_blocked:` (added 2026-06-25, g-115-1646) is the structured prefix for a
HUMAN gate specifically — an approval click, outside counsel, a credential only
a person can grant. It is a member of `STRUCTURED_DEFER_PREFIXES`, so it
bypasses the capability-gate and the blocker_ref gate, and it is the ONE member
that never auto-clears: `goal-selector` exempts it from the 120h fall-through,
keeping the goal suppressed-from-selector but counted-in-blocked so quiescence
can fire during a human-gated plateau. Visibility comes from the precheck
`human_blocked` age-escalation, not from re-selection.

Using the prefix does NOT establish that the gate is real. It says
"I have concluded this is human-only" in machine-readable form; rule 1
still has to be satisfied first, and `reclaim-routed-work.md` rule 2
applies with full force — a structured prefix attests that the routing
was FORMATTED correctly and is not evidence it is still correct.
Because this prefix never expires on its own, it is the defer most
exposed to the RULE axis: a standing grant can retire the reason while
the condition stays perfectly true, and nothing will re-surface the
goal on its own. Schema detail: `core/config/conventions/goal-schemas.md`
§ Structured-prefix bypass.

## 2. Enforcement — four layers, one of them ungated

Four layers catch this at different moments — and the fourth has **no
automated gate at all**. That row is stated rather than omitted because an
enforcement table listing only the gated lanes reads as complete, which is
exactly how the chat lane stayed invisible until 2026-08-03 (g-115-4787).

| Moment | Mechanism | What it catches |
|---|---|---|
| Defer-time write | `capability-gate.py` invoked from `cmd_update_goal` when `field == defer_reason` and value is non-null. On match, ALSO files an Unblock goal atomically — see "Auto-conversion" below. | Prevents the wrong defer from being written in the first place AND queues the action the agent should perform instead. Override flag: `--force-defer "<justification>"`. |
| Re-entry sweep | `aspirations-precheck` Phase 0.5b re-probe with canonical script | Catches defers that slipped through or whose premise has expired. Clears them automatically. |
| Notification time | `/notify-user` Step 1.5 gate on approval-request patterns | Prevents the third leak path: agent bypasses the defer write and just emails the user asking for approval. |
| **Chat reply** — handing the user a command block to run | **NONE. Honor-system by construction**, plus an optional post-hoc detective (see below). | Nothing. This is the largest lane and the only ungated one. See `capability-before-user.md` § "The Fourth Surface". |

**Why no gate is possible here.** The first three layers all inspect a
WRITTEN RECORD — a `participants` field, a `defer_reason` value, an outbound
payload — and a PreToolUse hook fires on TOOL CALLS. Assistant prose is
neither: writing "here, run these commands" in a reply routes identical work
to the identical human while producing no record and invoking no tool, so
there is nothing for a hook to intercept. This is a structural limit, not an
unbuilt feature; do not file work to "add the missing gate".

**A post-hoc detective IS possible, and that is the honest remedy.** Measured
2026-08-08 (g-115-4787): assistant TEXT blocks are readable from the session
transcript at `~/.claude/projects/<project-slug>/<sid>.jsonl` — 2,965 in a
single live session — and fenced command blocks are extractable from them by
regex. So a Layer-C sweep in the shape of
`core/scripts/aspirations-rejection-audit.py` (scan recent transcripts, extract
fenced blocks from assistant prose, cross-check the commands against
`capability-gate.py`) is buildable. It catches the lane AFTER the fact, which
is worth more than a fourth honor-system paragraph, and it is the only
mechanism this lane can have.

## 3. Auto-conversion at defer time (Layer D)

Refusing a defer is half a solution — the original action that motivated the
defer still has to be performed by *someone*. When the gate matches an
agent-provisionable capability, it ALSO emits a structured suggestion
(matched capability + action verb + Unblock title/description), and
`cmd_update_goal` files an `Unblock:` goal into `asp-001` BEFORE refusing the
defer. Both writes happen under the same `aspirations.jsonl` lock, so queue
state stays consistent: either both land or neither does. The original goal
stays `pending` (its `defer_reason` was never written), but the action it
depended on is now visibly queued — refusal alone would leave the agent
stuck at the same decision point on the next iteration.

Four invariants govern the auto-conversion:

1. **Atomic with the refusal.** Filing happens inside the same lock as the
   defer write. If filing fails, the failure reason is surfaced in the
   BLOCKED message, but the refusal still stands. The original write is
   never committed either way.
2. **Idempotent across retries.** Three OR-ed dedup strategies prevent a
   defer re-attempt from filing duplicate Unblocks:
   `origin_signal` exact match, title-regex `Unblock:.*for {G}`, and
   description-proximity (verb + goal-id within 80 chars). Cross-queue
   scan covers world + agent. Resolved/skipped Unblocks do NOT block
   re-filing.
3. **Title describes the action, not the match.** The Unblock title uses
   the failure_reason's first action verb (e.g. `"deploy"`), not the
   matched-capability keyword (e.g. `"human"` matched from a skill name
   that happens to contain that token but whose actual action is unrelated).
   The title tells the agent what to DO; `matched_capability` is preserved
   separately for callers needing the gate's signal independent of the
   title. (rb-574)
4. **Single-writer responsibility.** `capability-gate.py` emits the SPEC
   for the Unblock; `cmd_update_goal` does the filing. The gate never
   spawns a subprocess that would deadlock on the same lock. (rb-403)

Override: `--force-defer "<justification>"` bypasses the entire gate (no
refusal, no Unblock filed, defer applies). Use only for genuine false
positives — the override is echoed to stderr for audit and disqualifies
the goal from quiescence eligibility.

**The best-probed defers are the ones most likely to be refused, and it is the
EVIDENCE that trips the gate, not the routing** (guard-3882, measured
2026-08-15). The gate keyword-matches the `defer_reason` TEXT against
agent-provisionable capabilities. Rule 1 tells you to probe first, and a defer
that honors it CITES its probe output — which is exactly where service, tool and
command names live. So satisfying this rule's central instruction is what makes
the refusal fire, while an unprobed narrative defer ("blocked on external
signal") carries no such tokens and sails through. The gradient runs against the
discipline.

Two things follow. **Read the gate's own sub-analysis before rewording
anything**: when it reports `unblock_suggested: false` with
`unblock_suppressed_reason` naming a bare-token match ("no imperative verb in
failure_reason; matched keyword X is a bare token, not an action" — the
g-115-1872 noun-as-verb guard), the gate has already classified its own
top-level match as a noun hit, and `--force-defer` is the sanctioned response
rather than laundering the evidence out of the text. Never delete the probe
citation to get past the gate: that trades an auditable override for a defer
that now looks unprobed to every future reader, which is the failure this whole
rule exists to prevent.

**Then expect a SECOND, independent refusal — `blocker_ref_required`, which
`--force-defer` does NOT satisfy.** Only a `STRUCTURED_DEFER_PREFIXES` member
bypasses it (`gates/defer_classifier.py` is the SSOT). For a time or
measurement window use `precondition_unmet:`, which routes to the Phase 0.5b.3
precondition-defer-recheck lane and therefore re-probes; do NOT reach for
`human_blocked:` on a non-human gate, since it never auto-clears and would
suppress the goal indefinitely.

Implementation traceability: g-257-02 (gate `--suggest-unblock` flag),
g-257-03 (atomic in-process filing under existing lock), g-257-04 (3-strategy
dedup helper with cross-queue scan), g-257-05 (6-case integration test
matrix + 1-command test-suite aggregator). Knowledge-tree node
`world/knowledge/tree/system/system-constraints-loop/capability-routing-enforcement/`
documents this as **Layer D** of the 4-Layer enforcement pattern (A:
tactical disambiguation, B: automated gate, C: aged-decision recheck,
D: constructive auto-routing at block time).

## 4. Cross-references

- `.claude/rules/capability-before-user.md` — sister rule for
  `participants: [user]`
- `.claude/rules/probe-with-canonical-code-path.md` — canonical-script
  probing discipline
- `core/scripts/capability-gate.py` — shared gate implementation
- `world/conventions/capability-routing.md` — agent-provisionable vs
  human-only catalog
- `world/conventions/post-execution.md` — commit/push autonomy (the
  canonical example of a capability that kept being wrongly deferred)
- rb-246, guard-147 — prior lessons on synthetic-probe false-positive
  blockers


## 5. Reclaiming routed-away work — the evidence behind `.claude/rules/reclaim-routed-work.md` (moved 2026-08-17, g-115-6581)

The rule keeps the three lanes, the two re-check axes and rules 1–7 as
imperatives. The measured incidents and the sweep-tooling map live here.

### The canonical RULE-axis incident (2026-07-28)

Canonical incident (2026-07-28, measured): a standing grant stated verbatim
that a named condition "is no longer a valid `defer_reason` — the correct
response is to start one." The condition itself was still true and was
re-probed correctly on a cadence. Two goals stayed frozen for 6 and 8 days
past the grant, stranding a cluster of downstream dependents, because every
sweep tested the premise and nothing tested the rule. The automated gate the
sweeps delegate to reads only the "agent-provisionable capability" table and
never the standing-grants table, so it returned `matches: 0` on the exact
text the grant had invalidated.

### Both halves of the grant↔leg join were measured empty (2026-07-29, rule 4)

The same obligation runs in BOTH directions, and both halves were measured
empty on 2026-07-29. A routing that never declares WHY the user is attached
cannot be re-derived by anything: 20 of 28 `[agent, user]` goals carried no
`user_leg_scope`, so no grant could reach them. And a grant whose scope
*head* avoids the shared vocabulary can never be applied: 4 of 5 standing
grants were unkeyable, carrying real permission the audit is structurally
unable to act on. Declaring the leg and wording the grant are one duty seen
from two ends — neither alone closes anything.

### Rule 7 — the reclaim predicate that was narrower than its creating gate, and the variant a predicate diff cannot catch

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

**Second instance, and the variant a predicate diff will NOT catch:**
`blocker-recheck.py` enumerated only the `known_blockers` working-memory
slot. `create-blocker.py` writes the blocker to TWO places — that slot AND
`blocker_ref` on the goal record — and its own comment calls the WM entry
"the authoritative record" and the goal copy "a redundancy". That is
inverted with respect to durability: the WM slot is per-agent, per-box and
ephemeral, while the goal record is shared, fleet-wide and durable.
Measured 2026-08-01: all five agents read `known_blockers=null` while six
non-terminal goals carried a live `blocker_ref`, so the sweep reported
`total_blockers: 0` — the same permanent all-clear as the first instance,
from a different cause. Here the predicate was not narrower than the
creating gate's; it was pointed at the wrong one of the two stores that
gate writes. So diffing predicates cannot find this variant. The question
that does: **which store does the creating gate write DURABLY, and is that
the store I am reading?** Widen the READ; do not assume the WRITE widens
with it — this sweep's clear path is a keyword match with no probe behind
it (guard-1978), and extending it over goal records would mutate other
agents' goals, so the goal-sourced half is deliberately report-only.
(guard-1242 — the `--goal-status blocked` projection omits `blocker_ref`
entirely, so the obvious probe for this population returns a false zero
too.)

### Sweep tooling and cross-references

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
- `core/scripts/gates/defer_scope.py` — the SHARED scope set the four lanes
  draw from, so a lane extension does not fork a fifth vocabulary. Lane
  `user-leg` is `VALID_USER_LEG_SCOPES` BY IMPORT from the SSOT above, never
  re-typed. Read it before adding a scope to ANY lane; two of the shapes it
  declares were already in lane P's enum, which is why one set rather than
  four. Its consumer `core/scripts/defer-scope-coverage.py` turns each
  sweep's printed exclusion count into keyable/unkeyable per lane WITH the
  observed text — report-only, it never writes a scope onto a goal
- `rb-7289` — the measured warning about those exclusion counts: a sweep's
  "cannot key N items" is a claim about the SWEEP's key-space, not the
  population. `credential-defer-recheck` called 16 of 17 defers human-only
  when 7 of 7 in its lane were keyable, 6 by an IAM action string its
  env-var-shaped predicate could not see. Read five skipped items verbatim
  before believing any such aggregate
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
