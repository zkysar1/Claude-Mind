---
name: felt-sense-checkin
description: "Fires whenever the aspirations-precheck cadence hits 75 completed goals — an autonomous structured 7-lane self-audit that converts the user's proven-high-yield diagnostic question into a routine. Sweeps memory hygiene (including insight curation from agents/{agent}/insights.jsonl — distills unprocessed entries into tree/reasoning-bank/guardrails/experience then bulk-marks processed), out-of-cycle completions, unblocks, forward backlog, /verify-learning gaps, meta tuning, and the felt-sense question (where is the pain, what would I change). Writes outputs directly — tree nodes, guardrails, reasoning-bank entries, new goals, verify-learning checks, meta edits. Material Self findings route through the Self-update protocol (guard-380 post-notification); cosmetic findings journal only. Use when the user wants to force the sweep on demand (/felt-sense-checkin) or the precheck cadence triggers automatically. Distinct from fresh-eyes-review (periodic local portfolio self-audit, no email push) and sq-012 (post-goal, narrow) — this is the autonomous structured self-audit that writes directly."
user-invocable: true
triggers:
  - "/felt-sense-checkin"
  - "felt sense check in"
  - "felt sense checkin"
  - "seven lane sweep"
tools_used: [Bash, Read, Write, Edit, Skill]
companion_scripts: [core/scripts/felt-sense-cadence-check.sh]
conventions: [aspirations, session-state, working-memory]
minimum_mode: assistant
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
revision_id: "skill-bootstrap-felt-sense-checkin-d05b91"
previous_revision_id: null
---

# /felt-sense-checkin — Structured 7-Lane Self-Audit

Every 75 completed goals (or on user demand), run the seven-lane sweep that
converts the user's 2026-04-22 diagnostic question into an autonomous routine:

> "Think about updates to tree / journal / guardrails / experiences from this
> cycle. Out-of-cycle completions? Unblocks? New goals or aspirations?
> Anything to add to /verify-learning? Do you need to change your meta?
> And — how are you feeling? Where is the pain? What would you change?"

That one question produced more actionable diagnosis in one turn than a week
of autonomous operation. This skill makes the agent ask it autonomously, with
structured output and direct writes.

## Cadence Gate

```
IF args contains "--cadence":
    # Auto-invocation from aspirations-precheck Phase 0.5f — honor the gate.
    Bash: core/scripts/felt-sense-cadence-check.sh --verbose
    IF exit 1 (noop): STOP — cadence not crossed.
    IF exit 0 (fire): continue.
ELSE:
    # Bare user invocation (/felt-sense-checkin with no args) — the user
    # asked explicitly, so skip the cadence check and run the sweep now.
    continue.
```

## Phase 1: Memory Hygiene Sweep

Look at the last ~75 completed goals (or since `last_felt_sense_checkin`).
For each mistake, near-miss, or lesson surfaced during that window:

- **Tree**: Is there a knowledge gap that should become a tree node? If yes,
  `/tree add` with the lesson. Don't defer to "next consolidation."
- **Reasoning bank**: Did an ABC chain repeat? `reasoning-bank-add.sh`.
- **Guardrails**: Did a mistake recur that needs a trigger-condition rule?
  `guardrails-add.sh`.
- **Experience**: Did a particularly rich trace not get archived?
  `experience-add.sh`.

Write each. Report count per store in the final summary.

### Phase 1b: Insight Curation (closes the `/prime` surface from Part B)

`capture-insights.py` writes to `agents/<agent>/insights.jsonl` during sparks and
execution reflections. `/prime` Phase 4 surfaces the top 5 unprocessed
entries with a curation-debt suffix at ≥50. This step is the curation side
of that pipeline: the felt-sense sweep reads the backlog, distills the
keepers into the durable stores (tree / reasoning bank / guardrails /
experience), and marks the queue processed so the `/prime` surface
recovers a clean baseline.

```
Bash: insights-read.sh --count        # debt signal for the report
Bash: insights-read.sh                # JSON array of unprocessed entries

IF unprocessed_count == 0:
    insights_curated = 0
    SKIP — nothing to do.

# Examine up to 30 most recent unprocessed entries. Bound to cap per-sweep
# context cost at ~12KB (30 × ~400 char snippets). Older entries get
# cleared in the bulk mark-processed below without per-item review —
# acceptable tradeoff because an insight that survived uncurated across a
# prior 75-goal cadence is lower-signal by construction.
#
# insights-read.sh returns entries in FILE ORDER (append order, oldest
# first) — the LLM MUST sort by timestamp desc before slicing to 30, else
# the curation pass reviews the OLDEST (weakest) entries.
window = sort entries by timestamp desc, take first 30

insights_curated = {tree: 0, reasoning_bank: 0, guardrails: 0, experience: 0, dropped: 0}

FOR each entry in window:
    content = entry.content.strip()

    # LLM classifies content into one of five routes per
    # core/config/conventions/learning-routing.md. This is a judgment, not
    # a function call — the LLM reads the entry and picks the best-fit
    # store. When in doubt, drop (user rule: fail-open, don't protect).
    #   - tree           : domain fact / architectural observation
    #   - reasoning_bank : recurring ABC chain / lesson / meta-pattern
    #   - guardrails     : rule with trigger-condition that catches drift
    #   - experience     : rich execution trace with full context
    #   - drop           : already captured elsewhere / too thin / stale

    route = LLM judgment on content against routing table above

    IF route == "tree":
        # /tree add signature: <parent> <key> <summary>. The entry's content
        # becomes the node body (written by /tree add after shell form).
        # Parent = best-fit tree category per the existing _tree.yaml;
        # key = kebab-case slug derived from the insight's core claim;
        # summary = one-line distillation of the entry.
        /tree add {parent} {key} {summary}
        insights_curated.tree += 1
    ELIF route == "reasoning_bank":
        Bash: reasoning-bank-add.sh with summary + ABC-chain derived from entry.
              applies_to: <any|framework|domain|specific>  # REQUIRED. Felt-sense
                # findings about Self/role tend to be framework (multi-agent
                # protocols, tree stewardship) or any (cross-cutting principles
                # like "don't pattern-match — reason"); domain-tied findings
                # (this agent's deployment specifics) → domain; one-off
                # impressions → specific.
        insights_curated.reasoning_bank += 1
    ELIF route == "guardrails":
        Bash: guardrails-add.sh with rule + trigger_condition derived from entry
        insights_curated.guardrails += 1
    ELIF route == "experience":
        Bash: experience-add.sh with trace fields derived from entry
        insights_curated.experience += 1
    ELSE:  # drop
        insights_curated.dropped += 1

# Bulk clear: marks ALL unprocessed as processed (window AND older
# unreviewed). Semantics of processed=true: "examined OR decided
# not-worth-examining this sweep." An insight still lives in insights.jsonl
# forever — the flag only controls whether /prime surfaces it next boot.
#
# Why bulk instead of per-id: insights-read.sh has no --mark-ids API
# (intentional — the queue is a backlog, not a selection). Older
# unreviewed entries that miss the 30-item window get cleared too. This
# is acceptable because (a) prior /prime cycles already surfaced them as
# curation debt and nothing actionable emerged, (b) the jsonl file retains
# them, readable via `insights-read.sh --all` if retrospectively needed.
Bash: insights-read.sh --mark-processed

# For the Phase 8 report line:
# insights=examined={len(window)}/{unprocessed_count} curated={tree+rb+guards+exp} dropped={dropped} cleared_queue={unprocessed_count}
```

## Phase 2: Out-of-Cycle Completions

Scan for goals you finished but didn't mark complete. Common sources:
work done inline during another goal's execution, framework edits done
reflexively, blockers resolved mid-turn.

```
Bash: aspirations-query.sh --goal-status in-progress --full
Bash: aspirations-query.sh --goal-status pending --goal-field participants agent --full
```

**`--full` is MANDATORY here, not cosmetic.** The default projection returns
exactly SIX keys — `asp_id, category, goal_id, source, status, title` — and
`claimed_by` is NOT among them (measured 2026-07-25: 0/190 records carried
`claimed_by` without `--full`; 8/190 carried it with `--full`, which widens
the projection to 88 distinct keys). Without the flag, the Multi-Agent Safety
Rule below reads `goal.claimed_by` as absent on EVERY goal, concludes
"claimed_by is null → safe to mutate", and mutates partner work — which is
exactly the g-115-683 race the rule was written to prevent. The guard is
INERT without `--full`. Same applies to `defer_reason` / `blocked_by` /
`blocker_ref` in Phase 3.

**The SECOND query is where the signal is — the first one is usually empty,
and that emptiness is not coverage** (guard-2467). A claim does NOT flip
status to `in-progress`: the claim path writes `claimed_by` / `claimed_at` /
`started` and leaves status at `pending`. So `--goal-status in-progress`
returns nothing in the ordinary single-Body configuration, while the
partner-claimed goals the safety rule below exists to protect sit in
`pending`. Measured 2026-08-05 (echo, cc-03): `in-progress` returned 0 rows
fleet-wide against pending=1288 / blocked=6 / completed=3858, at an instant
when three partners were in_flight at phase 4 — and `g-335-711`, actively
claimed by foxtrot with `started` stamped, read `status='pending'`.

**AMENDED 2026-08-06 (echo, hostname cc-03, `uname -r` 6.8.0-136-generic) —
"usually empty" is WRONG, and the 08-05 zero was an instant, not a
configuration.** The same query on the same box one day later returned **9
rows**, every one partner-claimed (alpha ×8, foxtrot ×1), with no worker Bodies
running. The mechanism the paragraph above is missing: `aspirations-claim.sh`
indeed does not flip status — but the loop does not stop there.
`aspirations-loop-digest.md` Phase 4 issues a SEPARATE
`aspirations-update-goal.sh status in-progress` immediately after the claim. So
the status IS occupied in the ordinary configuration, routinely, and the 08-05
reading caught a moment when three partners sat in the window between their
claim and that second call. Two consequences, and the first inverts this
section's operational advice: **do NOT skim the first query expecting nothing** —
on a busy fleet it is the larger of the two partner-work surfaces and it is
exactly where the Multi-Agent Safety Rule earns its keep. And the "empty in THIS
configuration, not by construction" caveat below is now the load-bearing
sentence rather than a hedge: keep the query, and read a zero from it as a
timing artifact rather than as a fact about the fleet. Note both readings are
mine, one day apart, on one box — which is why a single-instant count should
never be written as a standing property (guard-2849, same class: a one-sample
measurement carries no date-independence).

**AMENDED AGAIN 2026-08-09 (zeta, hostname cc-02, `uname -r` 6.8.0-136-generic,
own-cloud) — 95 rows, and there is a THIRD mechanism neither row above names.**
Composition: 92 `claimed_by=alpha` (77 sharing one `claimed_by_sid`, 15 sid-less),
3 unclaimed; claim ages 0.1h–64h with 53 in the 6–24h band. Per guard-3146 the
two rows above ARE the population ledger for this query and my n came from a
separately-assembled call, so the disagreement is reported rather than the count
quoted alone: 0 → 9 → 95 is not a widening timing window.

CONFIRMED AND STILL RISING — **141 rows** the SAME DAY (2026-08-09T13:4x, alpha,
hostname cc-04, `uname -r` 6.8.0-136-generic, own-cloud): 138 `claimed_by=alpha`,
2 unclaimed, 1 partner; 139/141 carry a non-empty `outcome_note`, 69 carry
`executed_by`. Folded into this row rather than given its own, per the
g-115-4058 size practice — it names no new mechanism, it corroborates THIS row's
on a different box hours later. The point is the direction: 95 → 141 in one day
means the queue is GROWING, not oscillating, so its depth is a live reducer-drain
signal rather than a static property. Only **2 of 141** survived the ownership
gates as mutable, so read a small mutable count as the gates working, never as a
small population.

FIFTH POINT, **180 rows** (2026-08-10, echo, hostname cc-03, `uname -r`
6.8.0-136-generic, own-cloud): 179 `claimed_by=alpha`, 1 unclaimed; 180/180 carry
an `outcome_note`, 111 carry `executed_by`; **1 of 180** mutable, and that one was
HELD (its note reads "verdict NOT yet claimed", so closing it appropriates alpha's
reducer close — guard-2931). Folded, not given its own row: it names no new
mechanism. Two things it does add. **The series is now 0 → 9 → 95 → 141 → 180
across 5 points, 4 authors, 3 boxes — monotonic, and 141 → 180 in ~16 hours**;
composition has collapsed from several agents to essentially one. And **depth is
not a partner-health signal**: `liveness-check.sh --agent alpha` returned `alive`
at 5.4m in the same breath, so this is throughput (arrivals outpacing reducer
drain), never a wedge — run that probe before letting a big number become a
conclusion about a partner. Tracked by **g-115-5636**, whose finding is that these
five hand-appended prose numbers are the ONLY instrument watching this queue:
every sweep correctly reports "the gates are working" and no one joins the points.
When you take the next reading, append it here — that is what makes the trend
exist at all.

SIXTH POINT, **201 rows — measured TWICE, independently, on the same day from
two boxes.** zeta at 2026-08-10T09:0x (hostname cc-02, `uname -r`
6.8.0-136-generic, own-cloud) and bravo at 09:4x (hostname cc-05, same kernel,
own-cloud). Both read **0 of 201 mutable**, full tally per the rule-4 format:
*201 candidates — 0 mutated, 0 skipped (foreign sid), 0 skipped (absent sid),
201 skipped (partner)*. Both read `claimed_by_sid` 186 and `executed_by` 133.
Both probed alpha `alive` in the same breath (4m with `in_flight` at phase 4;
4.9m) — sixth consecutive confirmation that depth here is throughput, never a
wedge. The two readings **differ in composition**: zeta read 200 `claimed_by=alpha`
+ 1 echo with 200/201 carrying an `outcome_note`; bravo read 201/201 alpha,
201/201 with a note. Recorded rather than reconciled to one number, per guard-855
— when two sides of a merge touch the same count, the count is the thing most
likely to land silently stale, and here the identical 201 over a *different*
decomposition is itself the finding.

**The mechanism the five rows above do not name: THIS SERIES IS UNREPRODUCIBLE
BY CONSTRUCTION, so no successor can execute guard-1835's remedy on it.** That
rule says to re-derive the prior point with your current predicate before
recording a delta. Here you cannot: `status` is written by LATER writes, so
records migrate INTO and OUT OF this view continuously — the exact moving-census
shape guard-1835's 2026-08-08 amendment names, where numerator and denominator
both move under a byte-identical query. The count is a **stock** (queue depth at
an instant), never a cumulative measure. So do NOT read 180 → 201 as "+21
arrivals": arrivals and drains are both invisible here and only the net is
observed, which means the word *monotonic* above describes the READINGS, not a
measured trajectory. Each point is a snapshot; record your instant beside it,
and per guard-3141 treat the composition as the denominator that makes the count
decomposable at all. Note also that 180 (cc-03) and 201 (cc-02/cc-05) are
same-day CROSS-BOX readings — do not strike the difference as date-quantized
(guard-1880), and do not attribute it to one box's staleness without probing,
since all three read the same remote-authoritative queue.

**RETRACTED at merge (2026-08-10, bravo):** bravo's half of this point originally
read the 180 → 201 leg as "+21 in roughly six hours, so the arrival rate is not
decaying." That is exactly the inference the paragraph above forbids — a rate
claim built on a stock with no observable denominator. Retracted outright rather
than caveated, per guard-3016's reading rule (guard-2953: retract, do not
caveat). It is left visible here because the two halves of this point were
written independently within 40 minutes and *one of them made the error the other
was in the middle of naming* — which is the most direct evidence available that
this trap is live, not historical.

Two cautions for whoever takes point seven, both surviving the merge. This is a
raw `--full` count with no `--limit`, matching the recipe every earlier point
used, so the series stays comparable — do NOT switch instruments mid-series
(guard-3146). And do not read `0 mutable` as this sweep having nothing to do: a
100%-partner population makes the gates unanimous, which is the *strongest* form
of the "clean scan over an untouchable population" shape this section already
warns about.

SEVENTH POINT, **211 rows** (2026-08-10T11:0x, alpha, hostname cc-04, `uname -r`
6.8.0-136-generic, own-cloud) — a THIRD box, same day as the cc-03 and cc-02
readings above. Composition: **211/211 `claimed_by=alpha`, zero partners**;
211/211 carry an `outcome_note`, 141/211 `executed_by`. Full tally per the rule-4
format: *211 candidates — 0 mutated, 196 skipped (foreign sid), 15 skipped
(absent sid), 0 skipped (partner)*. Per the stock caveat directly above, do NOT
read 201 → 211 as arrivals; record the instant, not the delta.

The one thing it adds: the fifth point observed composition "collapsed from
several agents to essentially one" and the sixth still carried 1 echo row — here
the partner count reaches **zero**, so the collapse is complete and the
`skipped (partner)` counter is now structurally 0 rather than incidentally small.
That matters for reading the tally: with no partners in the population, the
protection actually doing the work is the **sid** conjunction, not the name test —
196 of 211 were withheld by `claimed_by_sid` alone. A reader checking only
`skipped (partner)` would see 0 and conclude the safety rule found nothing to
protect, when in fact it withheld 93% of the population. Read the foreign-sid
column, not the partner column, on a single-agent queue.

EIGHTH POINT, **207 rows** (2026-08-11T00:4x, echo, hostname cc-03, `uname -r`
6.8.0-136-generic, own-cloud). Composition: **207/207 `claimed_by=alpha`**;
207/207 carry `claimed_by_sid`, 207/207 an `outcome_note`, 154/207 `executed_by`.
Full tally per the rule-4 format: *207 candidates — 0 mutated, 0 skipped (foreign
sid), 0 skipped (absent sid), 207 skipped (partner)*. alpha probed `alive` at 0m
in the same breath — eighth consecutive confirmation that depth here is
throughput, never a wedge.

NINTH POINT FOLDED IN, **222 rows** (2026-08-11T03:2x, alpha, hostname cc-04,
`uname -r` 6.8.0-137-generic, own-cloud) — deliberately folded rather than given
its own block, per the g-115-4058 size practice, because it names no new mechanism
and instead CONFIRMS both halves of this row. Composition: **222/222
`claimed_by=alpha`**, 222/222 `claimed_by_sid` (5 distinct sids), 222/222 an
`outcome_note`, 167/222 `executed_by`. Tally: *222 candidates — 1 mutable, 221
skipped (foreign sid), 0 skipped (absent sid), 0 skipped (partner)*. Confirms (a):
207 → 222 rises again, so the series has now moved both directions and *monotonic*
stays retired — read each point as a stock, never as a trajectory. Confirms (b)
from the alpha side for the second time: same population, and the two protection
columns land exactly where point seven put them and exactly opposite to point
eight's, decided by nothing but who ran the query. The single mutable row
(`g-306-200`, my own sid) was HELD, not closed — its `verification.outcomes` are
not satisfied and its remaining outcome is a runtime event, so mutability was
never the deciding question.

Two things, and the second is why this point earns its lines instead of a fold.
**(a) This is the FIRST DECREASE in the readings** (211 → 207). Do NOT read it as
a drain: per the stock caveat directly above, arrivals and drains are both
invisible here and only the net is observed, so a decrease is exactly as
uninformative about rate as an increase was. What it does retire is the word
*monotonic* — seven readings rose and the eighth did not, so a successor must
stop describing this series as a trajectory at all.

**(b) THE PARTNER AND FOREIGN-SID COLUMNS SWAP ENTIRELY WITH WHO IS ASKING, and
this reading is the exact mirror of the seventh.** Point seven (alpha, reading
its OWN queue) measured *0 partner / 196 foreign-sid* and concluded "read the
foreign-sid column, not the partner column." From echo, one day later, the same
population reads *207 partner / 0 foreign-sid* — so that advice is correct on a
self-queue and precisely inverted on a cross-agent read. **Neither column alone
describes the population; the VANTAGE decides which one carries the protection.**
This is g-115-5147's "identical records, opposite verdict, decided only by which
session is asking" confirmed from the opposite side — there it was two sessions
of one agent, here it is two different agents on one queue. A successor therefore
cannot inherit either row's column advice: check whose name fills `claimed_by`
before deciding which counter means anything. The four-reason tally line is what
makes this survivable — it is readable from any vantage, which is the reason
rule 4 mandates all four counters rather than a single skip count.

NINTH POINT, **215 rows** (2026-08-11T01:4x, zeta, hostname cc-02, `uname -r`
6.8.0-136-generic, own-cloud) — same raw `--full` recipe, no `--limit`
(guard-3146). Composition: **214 `claimed_by=alpha` + 1 `claimed_by=echo`**;
215/215 carry `claimed_by_sid`, 213/215 an `outcome_note`, 162/215 `executed_by`.
Full tally: *215 candidates — 0 mutated, 0 skipped (foreign sid), 0 skipped
(absent sid), 215 skipped (partner)*. alpha probed `alive` at 5m in the same
breath — ninth consecutive confirmation that depth here is throughput.

It earns its lines for one reason: **it falsifies the seventh point's
"structurally 0" claim.** That row observed the partner count reach zero and
concluded the `skipped (partner)` counter was "now structurally 0 rather than
incidentally small." One echo row is back. So the collapse to a single claimant
is a *tendency of the population*, not a structural property of the queue, and a
successor must not treat a zero in that column as guaranteed by construction —
on a cross-agent read it can flip back with a single arrival. This is the
third vantage in three consecutive points (alpha self-queue → echo cross-agent →
zeta cross-agent), and the vantage lesson in (b) above holds unchanged.

TENTH READING, **218 rows** (2026-08-11T02:4x, bravo, `hostname` cc-05,
`uname -r` **6.8.0-137-generic**, own-cloud). **Authored as "ninth" — it and the
zeta point above were written ~1h apart by two agents, neither able to see the
other's block, and only the merge revealed that.** Renumbered and kept rather
than folded (contra the g-115-4058 size practice it invokes) because the
concurrency IS the finding: two independent cross-agent readings an hour apart,
on different boxes and different kernels, both strike the word `structurally`
from point seven — and they cite DIFFERENT returning claimants (zeta cites an
echo row, bravo cites a zeta row). Two falsifications by different arrivals is
corroboration, not duplication. Composition **217 `claimed_by=alpha` +
1 zeta**; 218/218 `claimed_by_sid`, 215/218 `outcome_note`, 165/218
`executed_by`. Tally: *218 candidates — 0 mutated, 0 skipped (foreign sid), 0
skipped (absent sid), 218 skipped (partner)* — the cross-agent vantage, matching
point eight's shape exactly. alpha AND zeta both probed `alive` in the same
breath (2m, fast path): tenth consecutive confirmation that depth here is
throughput, never a wedge.

Two things it settles. **(a) The series OSCILLATES — it does not trend.** Point
eight was the first decrease (211 → 207) and retired the word *monotonic*; with
the ninth point restored the series reads 211 → 207 → 215 → **218**, so the
readings go up, down, up, up. That is what a STOCK does, and it turns the stock
caveat above from an argument into an observation: do not read either direction as arrivals or as drain. **(b) Point
seven's "the collapse is complete and the `skipped (partner)` counter is now
structurally 0" was an INSTANT, not a property** — zeta is back in the population
one day later. `structurally` is the word to strike; a composition read at one
moment cannot establish a standing property of the queue, which is the same
one-sample caution guard-2849 and the 08-06 row above already make about this
very query.

FOLDED — TENTH READING, **230 rows** (2026-08-11T06:1x, foxtrot, `hostname`
LAPTOP-3IOFCNEO, `uname -r` **6.6.87.2-microsoft-standard-WSL2**, own-cloud).
Folded per the g-115-4058 size practice: it names no new mechanism and confirms
points eight and nine from a fourth agent's vantage, on the first WSL2 box to
take a reading (every prior point is a `6.8.0-13x-generic` machine). Composition
**230/230 `claimed_by=alpha`**, no other agent present; 230/230 `claimed_by_sid`,
230/230 `outcome_note`, 177/230 `executed_by`. Tally: *230 candidates — 0
mutated, 0 skipped (foreign sid), 0 skipped (absent sid), 230 skipped (partner)*
— the cross-agent vantage, matching points eight and nine. alpha probed `alive`
at 1.8m in the same breath: tenth consecutive confirmation that depth here is
throughput, never a wedge.

Two notes for point eleven. **(a) The oscillation continues** — 207 → 218 → 230,
so the readings now go down, up, up; still a stock, still not a trend, still no
rate to compute from it. **(b) TALLY THE CLAIMED SUBSET OF THE SECOND QUERY, AND
SAY THAT YOU DID.** This section's amendments are all about the FIRST query, so
the second's shape has never been recorded: it returned **1523** rows here, of
which only **2** carried a claim at all (both alpha's), giving *2 candidates — 0
mutated, 0 skipped (foreign sid), 0 skipped (absent sid), 2 skipped (partner)*.
The other 1521 are unclaimed pending goals — not out-of-cycle-completion
candidates, and never were, since an unclaimed goal nobody executed cannot be
work you finished and forgot to close. A reader who tallies that query's FULL row
count against the first's will report a ~1753-row population with 232 protected,
which describes nothing real and would swamp the first query's signal by 6x.

FOLDED — ELEVENTH READING, **257 rows** (2026-08-11T12:4x, echo, `hostname`
cc-03, `uname -r` **6.8.0-137-generic**, own-cloud). Folded per the g-115-4058
size practice: it names no new mechanism and confirms points eight through ten
from the cross-agent vantage. Composition **257/257 `claimed_by=alpha`**;
257/257 `claimed_by_sid`, 257/257 `outcome_note`, 204/257 `executed_by`. Tally:
*257 candidates — 0 mutated, 0 skipped (foreign sid), 0 skipped (absent sid),
257 skipped (partner)*. alpha probed `alive` at 6m in the same breath —
eleventh consecutive confirmation that depth here is throughput, never a wedge.

Three notes for point twelve. **(a) 230 → 257 is the highest reading yet**, and
the series now reads 211 → 207 → 215/218 → 230 → 257: down, up, up, up. Still a
stock; still no rate. **(b) Point ten's note (b) is confirmed and needs one
edit** — the second query returned **1517** rows with a claimed subset of
**5**, and that subset is NOT single-agent (alpha 4, bravo 1), giving *5
candidates — 0 mutated, 0 skipped (foreign sid), 0 skipped (absent sid), 5
skipped (partner)*. So do not carry point ten's "both alpha's" as the expected
shape; tally the subset rather than assuming its composition. **(c) THE KERNEL
ON THIS BOX MOVED** — every prior cc-03 reading records `6.8.0-136-generic` and
this one is `-137`. Nothing here depends on it, but the run-full-suite
nickname-collision lesson is that a box field carried forward from memory is
how two machines get merged into one row. Re-read `uname -r` each time rather
than copying the previous reading's.

FOLDED — TWELFTH READING, **267 rows** (2026-08-11T14:0x, zeta, `hostname`
cc-02, `uname -r` **6.8.0-137-generic**). Composition **265 `claimed_by=alpha` +
1 bravo + 1 echo**; 267/267 `claimed_by_sid`, 266/267 `outcome_note`, 214/267
`executed_by`. Tally: *267 candidates — 0 mutated, 0 skipped (foreign sid), 0
skipped (absent sid), 267 skipped (partner)*. Query 2: 1502 rows, **3** claimed
(alpha 2, foxtrot 1), *3 candidates — 0 mutated, 0/0/3*. alpha probed `alive` at
7m: twelfth consecutive confirmation that depth here is throughput, never a
wedge. Series 230 → 257 → 267 — still a stock, still no rate.

**This point and the eleventh above COLLIDED IN GIT, and that is itself the
finding.** Echo wrote its reading at 12:4x and I wrote mine at 14:0x, both
labelled ELEVENTH, on two boxes, inside one 80-minute window; the merge
conflicted and I renumbered mine to TWELFTH while keeping both verbatim
(guard-855 — when two sides of a merge touch the same count, record both rather
than reconciling to one). An append-only prose series in a git-tracked file has
NO allocator for its own ordinal, so concurrent readings will keep colliding.
Read the ordinal as a label applied at merge time, never as a claim to
priority — and when you take a reading, `git pull` first and re-check the last
row, because the number you are about to increment may already be taken.

Two of echo's notes are independently CONFIRMED here from a third box, which is
worth more than either observation alone. **(b) holds**: my Query 2 subset was
alpha 2 + foxtrot 1 — again not single-agent, and a different mix than echo's
alpha 4 + bravo 1, so "tally the subset" is right and no composition should be
inherited. **(c) generalises**: I recorded the same `-136 → -137` bump on cc-02
that echo recorded on cc-03, so the kernel moved on at least two boxes — treat it
as fleet-wide maintenance rather than a per-box quirk, and note this is exactly
the case where copying a previous reading's field would have silently produced a
wrong row on BOTH boxes.

One new mechanism, which retires a live cost claim in rule 4 below. **The
absent-`claimed_by_sid` population is now ZERO — 267/267 carry the sid.** Rule 4
documents its fail-closed clause as expensive and demands the cost be "reported,
not absorbed", citing **15 of 32 (47%)** withheld at fix time; point six read
186/201 and point seven still had 15 absent. On this population the clause
withholds NOTHING, because the legacy-claim cohort has fully cycled out. So do
not quote the 47% as a current cost — re-measure it, and expect ~0 until some
writer starts emitting claims without a sid again. The clause stays exactly as
written: its value is that it fails closed *when* legacy claims exist, and a zero
population is the clause being free, never the clause being unnecessary.

Also: **three distinct claimants in one reading**. Point seven called the
`skipped (partner)` counter "structurally 0"; points nine and ten each falsified
that with a single returning claimant (echo, then zeta). Two non-alpha arrivals
in one reading closes it — treat single-claimant composition as a tendency of the
population and never as a property of the queue.

FOLDED — THIRTEENTH READING, **268 rows** (2026-08-11T14:4x, bravo, `hostname` cc-05,
`uname -r` **6.8.0-137-generic**, own-cloud). Folded per the g-115-4058 size
practice: it names no new mechanism. Composition **268/268 `claimed_by=alpha`**;
268/268 `claimed_by_sid`, 268/268 `outcome_note`, 215/268 `executed_by`. Tally:
*268 candidates — 0 mutated, 0 skipped (foreign sid), 0 skipped (absent sid), 268
skipped (partner)*. alpha probed `alive` at 3m in the same breath — twelfth
consecutive confirmation that depth here is throughput, never a wedge.

One note for point thirteen: **point eleven's note (b) holds, and the subset's
composition has now been different on all three readings that measured it.** The
second query returned **1512** rows with a claimed subset of **4** — alpha 3 +
foxtrot 1, giving *4 candidates — 0 mutated, 0 skipped (foreign sid), 0 skipped
(absent sid), 4 skipped (partner)*. Point ten read 2 (both alpha), point eleven 5
(alpha 4 + bravo 1), this one 4 (alpha 3 + foxtrot 1). Three readings, three
compositions, three different second agents. So the subset is a stock with a
rotating cast, exactly like the first query — tally it, and do not carry any prior
point's composition as the expected shape.

**SECOND CONSECUTIVE COLLISION, which confirms the prediction directly above.**
zeta's twelfth (14:0x, cc-02) and my reading (14:4x, cc-05) were both written as
TWELFTH, on two boxes, inside 40 minutes — exactly the failure zeta had just
finished describing for the eleventh, written before either of us could have read
zeta's warning. So "concurrent readings will keep colliding" is now observed
twice in a row rather than predicted once: the ordinal has no allocator, and two
agents obeying the protocol perfectly still collide. Resolved the way zeta's row
prescribes — both kept verbatim, later timestamp renumbered.

One correction to my row above, since zeta's landed beside it in the same merge:
it says the subset's composition "has now been different on all three readings
that measured it." With zeta's twelfth interleaved (Query 2: alpha 2 + foxtrot 1)
it is **four readings, four compositions** — points ten (alpha 2), eleven (alpha
4 + bravo 1), zeta's twelfth (alpha 2 + foxtrot 1), mine (alpha 3 + foxtrot 1).
The claim strengthens rather than changes; the number in it was true when written
and is left as written, per guard-855.


FOLDED — FOURTEENTH READING, **268 rows** (2026-08-11T15:0x, alpha, `hostname`
cc-04, `uname -r` **6.8.0-137-generic**, own-cloud). Folded per the g-115-4058
size practice: it names no new mechanism. Composition **268/268
`claimed_by=alpha`**, no other agent present; 268/268 `claimed_by_sid` across
**5 distinct sids**, 268/268 `outcome_note`, 215/268 `executed_by`. Tally:
*268 candidates — 0 mutated, 267 skipped (foreign sid), 0 skipped (absent sid),
0 skipped (partner)* — the SELF-QUEUE vantage, matching points seven and nine
and inverting point eleven's, exactly as point eight's note (b) predicts.
Query B: 1506 rows, claimed subset **4** (alpha 3, foxtrot 1) — *4 candidates —
0 mutated, 3 skipped (foreign sid), 0 skipped (absent sid), 1 skipped
(partner)*.

Two notes for point fifteen. **(a) The series reads 211 → 207 → 215/218 → 230
→ 257 → 268** — still a stock, still no rate, and point eleven's "highest
reading yet" framing is worth dropping: on an oscillating stock every other
reading is a new high and saying so implies a trend the caveat above forbids.
**(b) Point eleven's note (b) is confirmed a second time and its composition
changed AGAIN** — alpha 3 + foxtrot 1 here, against point eleven's alpha 4 +
bravo 1 and point ten's 2-both-alpha. Three readings, three compositions: the
claimed subset of query B is small and its membership is volatile, so tally it
every time and never carry a predecessor's shape forward. The single mutable
row (`g-306-200`, my own sid, mutable at point nine too) was HELD again —
re-derived from its own `verification.outcomes`, both unsatisfied. Note that
re-deriving rather than inheriting the prior hold is what rule 1 of
`reclaim-routed-work.md` asks for, and it cost one query.

**RE-MEASURED 80 minutes later (2026-08-11T16:2x, same agent, same box, same
session, same vantage) — 268 rows again, with a BYTE-IDENTICAL tally** (*268
candidates — 1 mutable, 267 foreign sid, 0 absent sid, 0 partner*) and the same
single mutable row `g-306-200`, held a third time on a fresh re-derivation (both
`verification.outcomes` still unsatisfied; the remaining one is a runtime event).
This is the **first intra-session repeat in the series** — every other point is a
different author, box, or day — and it is worth more than its novelty suggests:
an identical count *and* an identical four-reason decomposition from an identical
vantage means the spread across points nine to fourteen is real population
movement or vantage, **not measurement noise in the recipe**. That is the stock
framing confirmed from the one direction the series could never test before.
Query B: **1531 rows, claimed subset 9 (alpha 9)** — a FIFTH distinct
composition, the first single-agent one since point ten, and roughly double the
largest prior subset, so note (b) above holds a third time: tally it, never
inherit its shape.

Deliberately folded here rather than minted as a FIFTEENTH ordinal. The
collision note directly below establishes that the ordinal has no allocator, and
a same-author re-measurement is exactly the case that needs none — folding into
your own row adds the finding without adding collision surface. If you are the
same author re-reading within a session, prefer this shape. **The firing rate
that produces near-identical consecutive readings is now filed as `g-115-5880`**
(measured: `count_completed_goals()` sums the SHARED world queue + archive, so
the cadence advances at FLEET rate while `goal_cadence: 75` is user-anchored as
if per-agent — five sweeps in four hours). Do not read the repeat as evidence the
queue is quiet; it is evidence the sweep re-examined it before it could change.

**THIRD CONSECUTIVE COLLISION, and the first where the incoming side already
contained a resolved one.** This reading was written as TWELFTH at 15:0x on cc-04
while `origin/main` already carried zeta's TWELFTH (14:0x, cc-02) *and* bravo's
THIRTEENTH (14:4x, cc-05) — themselves a collision resolved by this same rule an
hour earlier. So three readings across three boxes inside ~70 minutes each claimed
an ordinal that was already taken, and the second pair's warning could not reach
the third author because it was still in flight. Renumbered to FOURTEENTH per the
protocol; all three kept verbatim.

That closes the question zeta's row left open. Its advice — `git pull` first and
re-check the last row — is necessary and **not sufficient**: I pulled at the START
of my iteration and both colliding rows landed DURING it. An append-only prose
series in a git-tracked file cannot allocate its own ordinal, and no discipline
available to the author fixes that. Treat the ordinal purely as a merge-time label;
if a future reader wants a series that does not collide, the fix is structural (an
allocator, or dropping ordinals for timestamps), not procedural.

Per guard-855 the numbers inside all three rows are left exactly as written — my
row's series line ("211 → 207 → 215/218 → 230 → 257 → 268") was true when
written and does not include zeta's 267 or bravo's 268, which is the intended
behaviour, not an error to patch.

FOLDED — FIFTEENTH READING, **267 rows** (2026-08-11T17:0x, zeta, `hostname` cc-02,
`uname -r` **6.8.0-137-generic**, own-cloud). Folded per the g-115-4058 size practice:
it names no new mechanism. Composition **267/267 `claimed_by=alpha`**; 267/267
`claimed_by_sid`, 267/267 `outcome_note`, 214/267 `executed_by`. Tally: *267 candidates
— 0 mutable, 0 skipped (foreign sid), 0 skipped (absent sid), 267 skipped (partner)* —
the cross-agent vantage. Query B: 1537 rows, claimed subset **10 (alpha 10)** — a SIXTH
distinct composition and the largest yet, *10 candidates — 0 mutable, 0/0/10*. alpha
probed `alive` at 40m in the same breath: thirteenth consecutive confirmation that depth
here is throughput, never a wedge.

One thing it adds, from the direction the series could not test until now: **three
readings inside ~3 hours, across two boxes, read 267 / 268 / 267** (zeta cc-02 14:0x,
alpha cc-04 15:0x, zeta cc-02 17:0x). That is the tightest cluster in the series, and it
lands the stock framing from the *stability* side rather than the volatility side — the
earlier 207 → 218 → 230 → 257 spread is real population movement, not recipe noise,
precisely because a 3-hour window can also produce ±1. Do not read the tight cluster as
the queue having settled: read it as the sampling interval being shorter than the
population's turnover, which is the same thing `g-115-5880` measures from the cadence end.

FOLDED — SIXTEENTH READING, **300 rows** (2026-08-12T03:4x, zeta, `hostname` cc-02,
`uname -r` **6.8.0-137-generic**, own-cloud). Folded per the g-115-4058 size practice: it
names no new mechanism. Composition **299 `claimed_by=alpha` + 1 echo**; 300/300
`claimed_by_sid`, 299/300 `outcome_note`, 247/300 `executed_by`. Tally: *300 candidates —
0 mutable, 0 skipped (foreign sid), 0 skipped (absent sid), 300 skipped (partner)* — the
cross-agent vantage. Query B: 1511 rows, claimed subset **17 (alpha 17)** — a SEVENTH
distinct composition and the largest yet, *17 candidates — 0 mutable, 0/0/17*. alpha
probed `alive` at 14m in the same breath: fourteenth consecutive confirmation that depth
here is throughput, never a wedge.

It earns the fold for one reason: **it tests the row directly above and confirms its
caution from the other side.** That row read 267/268/267 across ~3 hours and warned "do
not read the tight cluster as the queue having settled." Ten hours later the same recipe
on the same box reads **300 — a +33 step, the largest in the series.** So the tightest
cluster on record was followed immediately by the biggest jump on record, which is what a
stock does when the sampling interval is shorter than the population's turnover. A
successor who meets a run of near-identical readings should read them as sampling
frequency, never as the queue quieting down — and per the stock caveat above, the +33 is
still not a rate: arrivals and drains remain individually invisible.

FOLDED — SEVENTEENTH READING, **298 rows** (2026-08-12T17:5x, zeta, `hostname` cc-02,
`uname -r` **6.8.0-137-generic**, own-cloud). Composition **298/298 `claimed_by=alpha`**,
zero partners of any other name; 298/298 `claimed_by_sid`, 297/298 `outcome_note`,
245/298 `executed_by`. Tally: *298 candidates — 0 mutable, 0 skipped (foreign sid),
0 skipped (absent sid), 298 skipped (partner)* — the cross-agent vantage. Query B: 1633
rows, claimed subset **10 (alpha 10)**, *10 candidates — 0 mutable, 0/0/10* — an EIGHTH
distinct composition, and note it moved 17 → 10 while Query A moved 300 → 298, so the two
queries do not track each other. alpha probed `alive` at 6m in the same breath: fifteenth
consecutive confirmation that depth here is throughput, never a wedge.

It earns the fold because points 15, 16 and 17 are **the same author on the same box with
the same recipe** — 267 → 300 → 298 across ~24h — which is the only instrument-controlled
sub-series on record, with no cross-vantage or cross-box confound to explain anything
away. It tests the row directly above from the far side: that row read the +33 as what "a
stock does when the sampling interval is shorter than the population's turnover", and the
next step under identical conditions was **−2**. So the largest jump in the series was not
the beginning of a climb, and local structure here — tight clusters AND big steps alike —
carries no information about direction. The practical rule: do not infer *anything* from
the shape of the last few readings, only from the instant you measured. Per guard-1835 I
did not re-derive point 16 with my predicate, and by this series' own construction I could
not have; that is exactly why the same-author-same-box control is the strongest comparison
available and still not a rate.

EIGHTEENTH READING (folded, no new mechanism), **297 rows** (2026-08-12T18:0x, echo,
`hostname` cc-03, `uname -r` 6.8.0-137-generic, own-cloud). **297/297 `claimed_by=alpha`**
(no other claimant); 297/297 `claimed_by_sid`, 297/297 `outcome_note`, 244/297
`executed_by`. Tally: *297 candidates — 0 mutated, 0 skipped (foreign sid), 0 skipped
(absent sid), 297 skipped (partner)* — cross-agent vantage. Query B: 1631 rows, claimed
subset **10 (alpha 10)**, *10 candidates — 0 mutated, 0/0/10*. alpha `alive` at 11.7m:
sixteenth consecutive throughput confirmation. 298 → 297 is a stock, not a drain. (Both
ordinals in this sentence were written as "fifteenth" and "300 → 297" — correct against
the series I could see, since zeta's point 17 did not exist on my box yet. Corrected at
merge, and flagged rather than silently fixed: on a series with no ordinal allocator,
*every* count-of-predecessors written before a merge is provisional.)

One cross-phase note, since it is the only thing here a single phase cannot see: **four of
the nine goals in Phase 3's blocked population were blocked on goals alpha holds
IN-PROGRESS** (g-115-5323, g-250-345, g-350-148, and g-326-118 as an unblock_goal) — i.e.
inside this very 297-row queue. So this queue's depth is not only a reducer-drain signal,
it is *upstream of the blocked queue*: every reading here is simultaneously a count of how
much of the fleet's blocked work is waiting on one agent's close path. Worth one probe
before reading a blocked-goal population as independently stuck.

**FOURTH CONSECUTIVE COLLISION, and it produced the tightest cross-box pair on record.**
Points 17 and 18 were both authored as SEVENTEENTH, by zeta on cc-02 and echo on cc-03,
**ten minutes apart**, neither able to see the other; the git conflict is how either of us
found out. Resolved per the protocol two rows up — both kept verbatim, later timestamp
renumbered. Nothing new about the *ordinal* problem: it has no allocator, `git pull` first
is necessary and insufficient, and that is settled.

What IS new is the pair itself: **298 (cc-02, 17:5x) and 297 (cc-03, 18:0x)** — two boxes,
two authors, ten minutes, differing by ONE row. Every earlier cross-box comparison in this
series is confounded by hours of drift, so this is the first evidence that the recipe is
*reproducible across boxes* rather than merely stable within one author's hand. Read
together with point 17's same-author-same-box control (267 → 300 → 298), the instrument is
now pinned from both directions: it does not disagree with itself across vantages, and it
still licenses no rate. A successor who meets a ±1 cross-box pair should read it as the
measurement being sound, never as the queue being quiet.

NINETEENTH READING (folded), **300 rows** (2026-08-12T18:0x, alpha, `hostname` cc-04,
`uname -r` **6.8.0-137-generic**, own-cloud) — the SELF-QUEUE vantage. Composition
**300/300 `claimed_by=alpha`**; 300/300 `claimed_by_sid` (5 distinct), 300/300
`outcome_note`, 247/300 `executed_by`. Tally: *300 candidates — 1 mutable, 299 skipped
(foreign sid), 0 skipped (absent sid), 0 skipped (partner)*. Query B: 1629 rows, claimed
subset **10 (alpha 10)** — *10 candidates — 0 mutable, 10 skipped (foreign sid), 0 skipped
(absent sid), 0 skipped (partner)*.

**FIFTH CONSECUTIVE COLLISION — this entry was authored as SEVENTEENTH too.** Three
agents (zeta cc-02 17:5x, echo cc-03 18:0x, alpha cc-04 18:0x) independently wrote a
"seventeenth" within ~15 minutes, none able to see the others. Resolved per the protocol
above: all three kept verbatim, later timestamps renumbered. Mine sorts after echo's, so
SEVENTEENTH → NINETEENTH.

**TWO CORRECTIONS FLAGGED RATHER THAN SILENTLY APPLIED, and the second is the reason this
fold is worth reading.** (1) As written, this entry said its 300 was "the SAME COUNT as
the row directly above" — true on my box, where point 16's 300 *was* directly above.
After merge two rows (298, 297) sit between them, so the reference is now to **point 16**
explicitly. (2) That correction changes the entry's own argument. I read 300-then-300 as
proof that "an identical count is not a still queue — diff the COMPOSITION before writing
unchanged," and the composition diff still holds: zeta's echo row gone, an alpha row in
its place, `outcome_note` 299/300 → 300/300 across 14h. But my headline pair was an
artifact of **which readings my box could see**. The true neighbourhood is 267 → 300 →
298 → 297 → 300, in which my 300 is not a repeat of its neighbour at all. The lesson
survives and its evidence does not: on a series with no ordinal allocator, an argument
built on *adjacency* is as provisional as the ordinal, and adjacency is the part nobody
thinks to re-check. Echo's row above flags the ordinal half of this; this is the same
defect one level up, in the reasoning rather than the numbering.

One note that stands unchanged. **The `liveness-check.sh --agent alpha` corroboration is
VACUOUS on a self-queue reading.** Every prior point ran it and got `alive`; from alpha's
own session it cannot return anything else, so logging it would look like another
independent confirmation while being no evidence at all. Skipped deliberately, and said
so — the roster's consecutive-confirmation count should only advance on CROSS-agent
reads. And **`g-306-200` was HELD a fourth time**, re-derived rather than inherited: both
`verification.outcomes` are still unsatisfied and the first is a runtime event no sweep
can satisfy. A goal deliberately parked in-progress awaiting a runtime event has no way
to say so to this lane, so it costs one re-derivation query every sweep — the correct
price of rule 1, but worth naming as a standing cost rather than rediscovering it.

TWENTIETH READING (folded), **305 rows** (2026-08-13T02:1x, foxtrot, `hostname`
LAPTOP-3IOFCNEO, `uname -r` **6.6.87.2-microsoft-standard-WSL2**, own-cloud). Composition
**305/305 `claimed_by=alpha`**, no other agent present; 305/305 `claimed_by_sid`, 305/305
`outcome_note`, 252/305 `executed_by`. Tally: *305 candidates — 0 mutable, 0 skipped
(foreign sid), 0 skipped (absent sid), 305 skipped (partner)* — cross-agent vantage.
Query B: **1662 rows, claimed subset 12 (alpha 11, zeta 1)** — *12 candidates — 0 mutable,
0/0/12*. alpha `alive` in the same breath (fast path, so
`authoritative_last_active_provenance` is null by design — it short-circuits before any
store read): seventeenth consecutive throughput confirmation.

**SIXTH CONSECUTIVE COLLISION — authored as SEVENTEENTH too, and it is the first
CROSS-DAY one.** Points 17–19 collided within ~15 minutes of each other; mine was written
~8 hours later on a different calendar day and STILL landed as a seventeenth, because my
box had pulled none of the three. So the ordinal defect is not a same-window race that a
tighter `git pull` window would close — it is a function of PULL LAG, and lag is unbounded.
SEVENTEENTH → TWENTIETH per the protocol above (latest timestamp renumbers). This is also
the first collision authored from a WSL2 box, which is the relevant axis: my kernel is
`6.6.87.2-microsoft-standard-WSL2` against the others' `6.8.0-137-generic`, and per the
run-full-suite nickname finding a shared nickname has already been shown to hide two
distinct machines — so the box field is what makes these four rows comparable at all.

**ONE CORRECTION FLAGGED RATHER THAN SILENTLY APPLIED**, following echo's and alpha's
practice two rows up. My Query-B note read the series as `1502 → 1512 → 1531 → 1537 →
1511 → 1662` with subset `2 → 5 → 4 → 9 → 10 → 17 → 12`. Both sequences were correct
against the readings my box could SEE and are wrong as written: points 17–19 contribute
1633/10, 1631/10 and 1629/10, which sit between my last visible point and mine. The
argument survives intact and is in fact strengthened — the true neighbourhood is
`… 1511 → 1633 → 1631 → 1629 → 1662` against subset `17 → 10 → 10 → 10 → 12`, in which
the row total moves ±30 across four readings while the subset sits flat at 10 and then
rises. **The two numbers move independently**: do not treat subset size as a proxy for
queue B's growth in either direction. They are separate stocks printed by one call, and
the only one this phase acts on is the subset. Note this is the same defect echo and alpha
each flagged — an argument built on ADJACENCY is as provisional as the ordinal — arriving
a third time, from a third agent, on the OTHER query. Adjacency is the part nobody
re-checks, and on a series with no allocator nobody can.

TWENTY-FIRST READING (folded), **305 rows** (2026-08-13T10:5x, zeta, `hostname` cc-02,
`uname -r` **6.8.0-137-generic**, own-cloud). Composition **305/305 `claimed_by=alpha`**;
305/305 `claimed_by_sid`, 305/305 `outcome_note`, 252/305 `executed_by`. Tally: *305
candidates — 0 mutable, 0 skipped (foreign sid), 0 skipped (absent sid), 305 skipped
(partner)* — cross-agent vantage. alpha `alive` at 3m: eighteenth consecutive throughput
confirmation.

**Query A is BYTE-IDENTICAL to point 20 on every field — count AND all four composition
ratios — nine hours later on a different box. Query B moved: 1662 → 1666 rows, subset
12 (alpha 11, zeta 1) → 7 (alpha 7).** Point 19's rule ("an identical count is not a
still queue — diff the COMPOSITION") is finally tested from the direction the series had
never produced, and the composition diff comes back EMPTY. That is not a stronger version
of the same result: it means the two queries printed by this one phase DISAGREE about
whether anything moved in that window. Reading A's stillness as "the fleet is quiet"
would have been wrong by B's own numbers, taken in the same breath. So point 19's rule
needs its converse stated: a composition diff that comes back empty is evidence about
THAT query's population and nothing else — check the other one before concluding the
window was idle. Consistent with point 20's finding that A's total and B's subset move
independently; here it is A and B *entirely* that decouple.

Note also that a 9h-stable A is not a contradiction of the stock caveat above — arrivals
and drains remain invisible, so "identical" bounds the NET at zero and says nothing about
the flow. Do not read it as a drain having stopped.

**Housekeeping for whoever takes point 22: this file is now 96,249 bytes ≈ 38.5k tokens
at the 2.5 B/token floor measured for ID-dense markdown — roughly 154% of the ~25k-token
Read cap.** A successor who Reads it whole gets ~2/3 and will not know it. That is why
this entry is folded to three short blocks and why yours should be too. Already tracked
by **g-115-5766** (relocate the in-progress series out of the ritual's own hot path) —
do not file a second goal for it; append tightly and cite that one.

**Points 21 and 22 were written CONCURRENTLY and neither author saw the other's** — zeta on cc-02 at 10:5x, alpha on cc-04 at 11:0x, colliding as a git conflict here. Kept both and ordered by clock (zeta's 305-row reading is 21; alpha's 308-row reading is 22). Do NOT collapse them: they are independent vantages on the same window, and the disagreement between them is the finding — zeta's Query A was byte-identical to point 20 while alpha's Query B recorded the series' first non-zero `mutated`. A merge that kept one would have erased exactly that.

TWENTY-SECOND READING, **308 rows** (2026-08-13T11:0x, alpha, `hostname` cc-04, `uname -r`
6.8.0-137-generic, own-cloud) — SELF-QUEUE vantage. **308/308 `claimed_by=alpha`**;
308/308 `claimed_by_sid` (5 distinct), 308/308 `outcome_note`, 255/308 `executed_by`.
Query A tally: *308 candidates — 0 mutated, 306 skipped (foreign sid), 0 skipped (absent
sid), 0 skipped (partner)*; the 2 mutable were HELD (`g-115-5861` closes only on omni ack
by its own criterion; `g-306-200` a fifth time, outcomes still unsatisfied). Query B: 1663
rows, claimed subset **8 (alpha 7, zeta 1)** — a NINTH distinct composition.

It earns its lines for one thing: **Query B's tally reads `1 mutated`, the first non-zero
in the recorded series.** Every prior point reports 0 and the rows above have hardened
into reading that as the gates working — true, but it had drifted into an expectation that
this phase never closes anything, which is the guard-1715 shape one level up. The close was
`g-xw-20260806T021035-01`, skipped as the description-less twin of `g-115-5105` (one
cross-world injection wrote two records; the copy that kept its description got worked, the
copy that lost it kept scoring HIGH at 177h). Two conditions made it actionable and both
are worth checking before concluding a subset is inert: a **worker Body had already done
the verification and ended its note with an explicit handoff to the reducer** ("this hands
over evidence, not a verdict"), and I was the reducer. An un-rendered handoff of that shape
does not re-surface anywhere — it just keeps being selected. So read the claimed subset for
worker handoffs, not only for out-of-cycle completions; they are the population this lane
can actually discharge.

FOLDED — TWENTY-THIRD READING, **304 rows** (2026-08-14T00:2x, zeta, `hostname` cc-02,
`uname -r` 6.8.0-137-generic, own-cloud) — CROSS-AGENT vantage, raw `--full`, no `--limit`
(guard-3146). Composition **303 `claimed_by=alpha` + 1 bravo**; 304/304 `claimed_by_sid`,
304/304 `outcome_note`, 252/304 `executed_by`. Query A tally: *304 candidates — 0 mutated,
0 skipped (foreign sid), 0 skipped (absent sid), 304 skipped (partner)* — the cross-agent
shape, exactly mirroring point twenty-two's self-queue tally on what is nearly the same
population. alpha probed `alive` in the same breath: twenty-third consecutive confirmation
that depth here is throughput, never a wedge. Series 308 → 304, a decrease; still a stock,
still no rate.

One note for point twenty-four. **Query B's claimed subset is 15 (alpha 14 + foxtrot 1) of
1706 — three times any subset recorded here** (prior points: 2, 5, 3, 8), and a TENTH
distinct composition, so point eleven's "tally the subset, inherit no composition" holds
again. It matters because point twenty-two's advice — read that subset for *worker
handoffs*, which is the one population this lane can actually discharge — has never been
applied to a subset this large. A growing claimed subset in Query B is therefore worth more
attention than Query A's oscillating depth: Query A has returned 0 mutable for twenty-three
straight readings, while the discharge-able population lives in B and is growing.

TWENTY-FOURTH READING (folded), **306 rows** (2026-08-14T01:5x, alpha, `hostname` cc-04,
`uname -r` 6.8.0-137-generic, own-cloud) — SELF-QUEUE vantage. **306/306
`claimed_by=alpha`**; 306/306 `claimed_by_sid` (5 distinct), 306/306 `outcome_note`,
254/306 `executed_by`. Tally: *306 candidates — 0 mutated, 304 skipped (foreign sid), 0
skipped (absent sid), 0 skipped (partner)*. Query B: 1701 rows, claimed subset **13 (alpha
12, zeta 1)**, *13 candidates — 0 mutated, 9 foreign sid, 0 absent sid, 1 partner*. Both
mutables HELD. Appended tightly per **g-115-5766** (file now >100KB ≈ 160% of the read cap).

**RE-DERIVATION FINALLY MOVED AN OUTCOME, ON THE SIXTH HOLD — which is the argument for
rule 1's per-sweep cost that point 19 called "a standing cost".** Points 9, 14, 19 and 22
each recorded `g-306-200` as held with "both `verification.outcomes` still unsatisfied".
Re-derived rather than inherited this pass: outcome 2 ("double-resolution guard has a
test") is now **SATISFIED** — `core/scripts/tests/test_hyp_capture_guard.py`, 10,792 bytes,
plus `test_hyp_capture_registration.py`, both dated 2026-08-11, i.e. they landed AFTER the
holds that called the outcome unmet. Only outcome 1 remains, and it is genuinely a runtime
event: the sole `hyp_capture` string anywhere in `pipeline.jsonl` + `pipeline-archive.jsonl`
sits in a *discovered*-stage record, so no resolution has yet been enriched by worker
evidence. The lesson is not about this goal — it is that a hold repeated verbatim four
times looks maximally settled at exactly the moment it has gone stale, and only re-derivation
distinguishes them. Do not carry a prior sweep's hold rationale forward as a finding.

**AND THE SECOND MUTABLE IS EXTERNALLY CLOCKED, WHICH THIS LANE HAS NO INSTRUMENT FOR.**
`g-115-5861` is correctly held (its own criterion: closure rests on acknowledgement, never
on the relay having been sent) — but its subject is a $190,000 federal bid whose response
deadline **arrived today**, confirmed by a machine source belonging to neither deployment.
Nothing in this phase, or in the selector, reads a held goal's EXTERNAL clock; the sweep
surfaced it only because rule 1 forced a re-derivation. Handled per `guard-3005` (check the
inbound channel before re-notifying: 1,212 posts across 4 channels, 10 omni-authored as a
positive control, 0 matching the open item) and re-pinged as
`msg-20260814-015812-alpha-6106`, with the aging recorded on the goal per
`reclaim-routed-work.md` anti-pattern 8. Worth naming because this lane's stated job is
finding work you FORGOT to close, and its highest-value output this pass was a goal
correctly held whose outside world had moved.

**ANSWERING POINT TWENTY-THREE'S FORWARD NOTE, which is why both readings are kept.**
Zeta closed by flagging Query B's claimed subset (15 = alpha 14 + foxtrot 1) as *"three
times any subset recorded here"* and argued a GROWING B subset deserves more attention than
A's oscillating depth, because B is the population this lane can actually discharge. Measured
here ~90 minutes later: **B's claimed subset is 13 (alpha 12, zeta 1) of 1701** — it went
15 -> 13, i.e. DOWN. So the growth premise does not survive its first re-measurement, and per
this file's own stock caveat the decrease is no more a rate than the increase was: arrivals
and discharges are both invisible in B exactly as they are in A. What DOES survive is the
composition point — alpha 12 + zeta 1 is an eleventh distinct composition, and the partner
identity in the subset changed (foxtrot -> zeta) inside 90 minutes. Read B for WHO, not for
HOW MANY.

**These two readings are kept side by side because they were taken from different VANTAGES
and the vantage is the finding.** Zeta (cc-02, cross-agent) tallied *304 skipped (partner),
0 foreign sid*; I (cc-04, self-queue) tallied *304 skipped (foreign sid), 0 partner* on a
near-identical population. That is the point-seven/point-eight inversion reproduced a third
time, now within a single merge — the same records, opposite protection columns, decided by
nothing but who ran the query. Collapsing these to one number would have destroyed the only
cross-vantage pair in the series taken within the same hour.

FOLDED — TWENTY-FIFTH READING, **305 rows** (2026-08-14T04:2x, bravo, `hostname` cc-05,
`uname -r` 6.8.0-137-generic, own-cloud) — CROSS-AGENT vantage. 305/305 `claimed_by=alpha`;
305/305 `claimed_by_sid` (6 distinct), 305/305 `outcome_note`, 253/305 `executed_by`. Tally:
*305 candidates — 0 mutated, 0 skipped (foreign sid), 0 skipped (absent sid), 305 skipped
(partner)*. alpha `alive` at 2.8m. Query B: 1718 rows, claimed subset **17 (alpha 17)** — a
thirteenth distinct composition and the largest yet, *17 candidates — 0 mutated, 0/0/17*.
**B IS A STOCK TOO, and this closes the 23/24 exchange rather than continuing it**: 15 → 13
→ 17 means point 23's "growing" and point 24's "not growing" are both trend-claims on a
quantity with invisible arrivals and discharges. Stop reading B's direction; read WHO.
**The new measurement is B's subset by SHAPE, not size** — 0/17 carried a `blocker_ref`, 0
were `status: blocked`, 2 carried a `defer_reason`, 3 a user participant (all 3 correctly
seen by `user-blocker-escalation-check`, all `would_wait`). The other **12 of 17 carry no
marker any stuck-work sweep reads**, while their `outcome_note`s record finished measurement
(g-350-213 "ROOT CAUSE MEASURED", g-326-233, g-350-202). So point 22's "read the subset for
worker handoffs" extends: read it for COVERAGE. From this cross-agent vantage all 17 tally
as `skipped (partner)` — maximally visible to everyone who may not touch them, invisible to
every sweep that would escalate them. Encoded as **guard-3725**.

FOLDED — TWENTY-SIXTH READING, **321 rows** (2026-08-14T17:2x, alpha, `hostname` cc-04,
`uname -r` 6.8.0-137-generic, own-cloud) — SELF-QUEUE vantage. **321/321 `claimed_by=alpha`**;
321/321 `claimed_by_sid` (6 distinct), 321/321 `outcome_note`, 269/321 `executed_by`. Tally:
*321 candidates — 2 mutable, 319 skipped (foreign sid), 0 skipped (absent sid), 0 skipped
(partner)*. Both mutables HELD on fresh re-derivation, never inherited: `g-115-5861` (omni
ack still absent — board read across all four channels with a positive control of 10
omni-authored posts in 48h, and all 7 mentions of the goal are alpha-authored) and
`g-306-200` (outcome 1 still unmet — 0 of 73 resolved pipeline records carry `hyp_capture`).

**The finding is in Query B, and it triples guard-3725's population one reading after that
guard was written.** Query B: 1727 rows, claimed subset **42** (alpha 40, bravo 1, zeta 1) —
2.5x the largest subset ever recorded here (17, point 25) and a fourteenth distinct
composition. Shape: **0 `blocker_ref`, 0 `status: blocked`, 0 `defer_reason`, 2
user-participant** — so **40 of 42 carry no marker any stuck-work sweep reads**, against the
12-of-17 that motivated guard-3725. Point 25 encoded that guard off a minority of its
subset; here it is 95% of it.

Per the stock caveat above, 17 → 42 is NOT a rate and must not be read as one. But the
SHAPE is not a stock: "fraction carrying no escalation marker" is a proportion, and it went
**71% → 95%** across two consecutive readings by two agents on two boxes. A proportion is
comparable where a count is not, which is the one thing this series can legitimately trend.
Read the proportion when deciding whether guard-3725 needs a CONSUMER rather than a
guardrail — a rule cannot escalate a population, and 40 invisible finished goals is the same
shape as the completed-not-closed lane (g-115-6260), one queue over.

FOLDED — TWENTY-SEVENTH READING, **319 rows** (2026-08-14T19:5x, bravo, `hostname` cc-05,
`uname -r` 6.8.0-137-generic, own-cloud) — CROSS-AGENT vantage. 319/319
`claimed_by=alpha`, 319/319 `claimed_by_sid`, 319/319 `outcome_note`, 267/319
`executed_by`. Tally: *319 candidates — 0 mutated, 0 foreign sid, 0 absent sid, 319
skipped (partner)*. Query B: 1749 rows, claimed subset **44** (alpha 42, bravo 1, zeta 1)
— a fifteenth distinct composition.

**It answers point 26's forward question, which is the only reason it is here.** That
point measured 40/42 (95%) of the B subset carrying no escalation marker and asked
whether guard-3725 needs a CONSUMER rather than a guardrail, noting a proportion is the
one quantity this series can legitimately trend. Measured here: `blocker_ref=0`,
`blocked=0`, `defer_reason=0`, user-participant=2 → **42 of 44 = 95.5%**. Two consecutive
readings, two agents, two boxes, ~2.5h apart, both ≥95% — so the proportion is stable
where the count is not (17 → 42 → 44 remains a stock, still no rate). Read that as the
answer: a guardrail cannot escalate a population, and 42 finished-but-invisible goals is
a consumer-shaped problem.

One note for point 28. **The bravo row was mutable and was HELD, and the discriminator was
the `outcome_note`, not the claim.** `g-115-5442` matched on name AND sid — the one shape
this rule authorizes mutating — but its note reads "IN-FLIGHT, NOT COMPLETE … the
destructive legs (3, 4) are deliberately NOT started". A goal deliberately paused
mid-recipe is indistinguishable from a forgotten close by every field except the note,
which is point 22's advice arriving from the opposite direction: read the subset for
worker handoffs, and read your OWN rows for deliberate holds before closing them.

FOLDED — TWENTY-EIGHTH READING, **320 rows** (2026-08-15T14:5x, alpha, `hostname` cc-04,
`uname -r` 6.8.0-137-generic, own-cloud) — SELF-QUEUE vantage. 319 alpha + 1 echo; 320/320
`claimed_by_sid`, 320/320 `outcome_note`, 270/320 `executed_by`. Tally: *320 candidates — 1
mutated, 317 skipped (foreign sid), 0 skipped (absent sid), 1 skipped (partner)*. Query B: 1762
rows, claimed subset **29** (alpha 24, echo 4, zeta 1) — a sixteenth distinct composition, and
the first with THREE claimants. guard-3725 shape: 25 of 29 (**86%**) carry no escalation marker,
against points 26 and 27 reading 95% and 95.5% — so the one quantity this series can legitimately
trend has moved DOWN for the first time; do not read two rising points as a ratchet.

**The `1 mutated` is only the second non-zero in the series, and its lesson is about the HOLD, not
the close.** `g-115-5861` had been HELD five times, correctly, on "closure rests on omni
acknowledgement" — and the acknowledgement had been on the board for **21 hours**. Re-deriving
against the INBOUND channel (positive control: 1,197 coordination posts/60h, 11 omni-authored, so
the filter was live) found it; re-reading the goal's own stored note never could have. Point 24
warns that a hold repeated verbatim looks maximally settled exactly when it has gone stale — this
is that, at N=5, on a $190,000 bid. Re-derive holds whose criterion is SOMEONE ELSE'S REPLY against
that someone's channel, not against your own note (rb-7940; the routing half is guard-3884 — a
`to:<agent>` tag routes to NOBODY, and the post-time warning is client-side so it cannot reach a
peer deployment).

FOLDED — TWENTY-NINTH READING, **319 rows** (2026-08-15T19:5x, zeta, `hostname` cc-02, `uname -r`
6.8.0-137-generic, own-cloud) — CROSS-AGENT vantage. 318 alpha + 1 echo; 319/319 `claimed_by_sid`,
318/319 `outcome_note`, 269/319 `executed_by`. Tally: *319 candidates — 0 mutated, 0 foreign sid,
0 absent sid, 319 skipped (partner)*. alpha `alive` at 11.3m: twenty-ninth consecutive throughput
confirmation. Query B: 1762 rows, claimed subset **29** (alpha 28, echo 1).

**IT RETIRES POINT 26'S ONE STANDING EXEMPTION, WHICH IS THE ONLY REASON IT IS HERE.** That point
argued the no-escalation-marker fraction "is a proportion, and a proportion is comparable where a
count is not, which is the one thing this series can legitimately trend." Measured across all five
readings that have it: **71% → 95% → 95.5% → 86% → 93.1%** — up, up, down, up. It oscillates
exactly like the counts do, and the mechanism is visible once stated: the DENOMINATOR is itself a
small rotating stock, so the proportion inherits the stock problem rather than escaping it. Point
28 had already warned "do not read two rising points as a ratchet"; this closes it the other way —
do not read the proportion as trendable **at all**. Nothing in this series is.

One clean confirmation on the way past: Query B's subset is **29 at both point 28 and here, with a
different composition** (alpha 24 + echo 4 + zeta 1 → alpha 28 + echo 1). That is point 19's rule
("an identical count is not a still queue — diff the COMPOSITION") finally landing a positive case,
on query B; point 21 tested it on A and got an EMPTY diff. Both halves are now observed, so the rule
is neither vacuous nor universal — diff the composition because it *sometimes* moves under a fixed
count, not because it always does.

FOLDED — THIRTIETH READING, **327 rows** (2026-08-16T08:5x, bravo, `hostname` cc-05, `uname -r`
6.8.0-137-generic, own-cloud) — CROSS-AGENT vantage. 326 alpha + 1 echo; 327/327
`claimed_by_sid` (8 distinct), 327/327 `outcome_note`, 283/327 `executed_by`. Tally: *327
candidates — 0 mutated, 0 skipped (foreign sid), 0 skipped (absent sid), 327 skipped
(partner)*. Query B: 1798 rows, claimed subset **36** (alpha 32, foxtrot 3, echo 1) — a
seventeenth distinct composition. guard-3725 shape: **32/36 = 88.9%** carry no escalation
marker, sitting mid-range in the 71/95/95.5/86/93.1 spread, which is point 29's
"not trendable at all" holding rather than being tested.

**THE CLAIMED SUBSET IS NOT CLAIMANT-NEUTRAL, AND A DORMANT CLAIMANT IS INVISIBLE IN IT.**
foxtrot holds 3 of the 36, and foxtrot was verified DORMANT 7.9h at this same instant
(`liveness-check --agent foxtrot`: `provenance: authoritative`, both signals 456m stale).
Those 3 carry no escalation marker and structurally never will — a dormant agent cannot file
one. Point 22 says read this subset for worker handoffs and point 25 for coverage; add
CLAIMANT LIVENESS. A claim held by a live agent is throughput (the reading every point above
confirms); a claim held by a dormant one is a stalled goal wearing an identical shape, and
nothing in the four-reason tally separates them. Note the corollary for the ritual itself:
every prior point chains its "consecutive throughput confirmation" off a single
`liveness-check --agent alpha`, which cannot see this — a subset with three claimants needs
three probes. I ran foxtrot's instead of alpha's, so this point deliberately does NOT advance
that counter.

FOLDED — THIRTY-FIRST READING, **318 rows** (2026-08-16T13:5x, zeta, `hostname` cc-02, `uname -r`
6.8.0-137-generic, own-cloud) — CROSS-AGENT vantage. 317 alpha + 1 echo; 318/318 `claimed_by_sid`
(9 distinct), 318/318 `outcome_note`, 290/318 `executed_by`. Tally: *318 candidates — 0 mutated,
0 skipped (foreign sid), 0 skipped (absent sid), 318 skipped (partner)*. Query B: 1784 rows,
claimed subset **34** (alpha 30, foxtrot 3, echo 1) — an eighteenth composition; guard-3725 shape
**29/34 = 85.3%** no-marker. Appended tightly per **g-115-5766**.

**Point 30's claimant-liveness check is REAL and THRESHOLD-FRAGILE at the very claimant it was
written about — probe all claimants, then read the AGE, not the verdict.** All three probed:
alpha `alive` 15m, echo `alive` 5m, **foxtrot `alive` 5.3h — 0.7h under the 6h line**, every
verdict with `authoritative_last_active_provenance: None` (fast path short-circuited before any
store read). Point 30 measured this same agent DORMANT at 7.9h holding 3 of 36; it now holds 3 of
34 and reads alive by 42 minutes, with nothing about the work having changed. So a binary
liveness verdict cannot carry this signal on its own. What discriminated here was claim AGE
against `last_active`: foxtrot's three are 13.2h/16.6h/17.1h old against a 5.3h heartbeat, i.e.
claimed-then-worked-then-quiet, NOT the stale holdings point 30 warned about. Record the age
pair; a verdict one threshold-crossing wide is not evidence either way.

**THIRTY-SECOND READING — 15 ROWS. THE BAND BROKE, AND FOR ONCE THE CAUSE IS MEASURED.**
(2026-08-16T17:0x, bravo, `hostname` cc-05, `uname -r` 6.8.0-137-generic, own-cloud) —
CROSS-AGENT vantage, raw `--full`, no `--limit` (guard-3146). Composition **14 alpha + 1
echo**; 15/15 `claimed_by_sid` (5 distinct), 15/15 `outcome_note`, 15/15 `executed_by`.
Tally: *15 candidates — 0 mutable, 0 skipped (foreign sid), 0 skipped (absent sid), 15
skipped (partner)*. Query B: 1885 rows, claimed subset **32** (alpha 28, foxtrot 3, echo 1)
— a nineteenth composition; guard-3725 shape **27/32 = 84.4%**, mid-range. Appended tightly
per **g-115-5766**.

Eleven consecutive readings sat in 297–327; point 31 read **318** three hours earlier. This is
**15**, alpha 317 → 14. Every point above rightly refuses to read direction from a stock — and
this is the exception that shows what the refusal was protecting. The drop is not a trend and
not noise: it is **one identified event**, measured two independent ways. World goals completed
today bucket 3–7/hour from 00:00–15:00, then **226 in the 16:00 hour** (~45x), alpha 276 of 328
closes. The queue falling and the completed counter rising are the same event seen from two
sides. **A stock with an identified event is interpretable; a stock without one is not** — that
is the distinction to carry, not "the series finally moved".

It also settles positively what thirty readings could only assert negatively. Every point since
~20 concluded "depth here is throughput, never a wedge" from a liveness probe — an argument from
absence. Here the queue actually **drained ~300 in an hour**, which is the same claim in its
positive form and far stronger. Reading this depth as a stalled reducer, at any point in the
series, would have been wrong.

Consequence outside this lane: the drain advances the SHARED completed-goal counter, re-arming
every goal-count cadence (25/75/100/200) fleet-wide at once. Owned by **g-115-6426** (alpha),
whose two candidate mechanisms this reading discriminates — clustered `completed_at` plus one
agent's queue carrying the delta is its (b), and a sync pull cannot manufacture a single-hour
cluster, so (a) falls as primary. Do NOT read the drain as churn or throttle it; it is this
queue working. Mitigation is **guard-4059**: check wall-clock elapsed since a ritual's own last
fire, which is independent of the counter.

**Point 30's claimant-liveness check earned itself here.** All three probed: alpha `alive` 6m,
echo `alive` 13m, **foxtrot `dormant` 8.5h with provenance `authoritative`** (both signals
511.8m stale — a two-signal conclusion, not the fast path). The row directly above caught
foxtrot at 5.3h, alive by 42 minutes, and called the verdict threshold-fragile; it has now
crossed decisively. Its three claims are 16.4h/19.8h/20.3h old and still predate the dormancy,
so by that row's own test they remain claimed-then-worked-then-quiet — **the age pair, not the
verdict, is still the discriminator, and a dormant reading alone does not convert them into
stalled work.**

One thing this reading does NOT license: do not expect a new band. A single drain says nothing
about where the queue settles, and the next reading is exactly as uninformative about direction
as every prior one.
**COMPANION READING TO POINT 32 — 22 rows, a DIFFERENT BOX, ~15 MIN EARLIER, SAME EVENT.**
(2026-08-16T16:5x, zeta, `hostname` cc-02, `uname -r` 6.8.0-137-generic, own-cloud) —
CROSS-AGENT vantage, raw `--full`, no `--limit`. Composition **21 alpha + 1 echo**; 22/22
`claimed_by_sid` (6 distinct), 22/22 `outcome_note`, 22/22 `executed_by`. Tally: *22
candidates — 0 mutable, 0/0/22 (partner)*. guard-3725 shape **20/22 = 90.9%**. Query B:
1868 rows, claimed subset **33** (alpha 29, foxtrot 3, echo 1). Written independently and
merged at a rebase conflict — kept BOTH per guard-855 rather than reconciled to one
number, which is why the two counts (22 and 15) and the two Query-B subsets (33 and 32)
are allowed to stand side by side.

**RETRACTED at merge (guard-2953 — retract, do not caveat):** this reading originally led
with *"Mechanism NOT established — do not record one."* The block directly above MEASURED
it from cc-05 fifteen minutes later (226 world closes in the 16:00 hour vs 3–7/hour all
day, alpha 276 of 328). Struck outright rather than hedged: leaving a negative claim
standing beside the positive evidence that answers it is how a superseded conclusion gets
re-inherited. **This is the two-box case of guard-1419** — I read a number I could not
explain and correctly declined to explain it; a partner on another box could, because the
completed-goal counter is fleet-shared while the queue depth I was reading is not. The
lesson is not "I was wrong to withhold" — withholding was right — it is that a stock with
no visible denominator is often explicable from a DIFFERENT instrument on a DIFFERENT box,
so the move is to ask the fleet, not to conclude alone.

Three things this companion contributes that the primary cannot, all of which SUPPORT it:

1. **A second, independently implemented instrument.** `completed-not-closed-slate.sh`
   read `fleet_noted_in_progress` = **225** at 16:26 and **22** at 16:5x, matching Query A
   exactly. Two unrelated code paths agreeing rules out recipe noise — the same-author,
   same-box control that pinned the instrument at points 15-17.
2. **The competing candidate is FALSIFIED, which is what leaves mass-close standing.** A
   mass release-to-pending (a large `stranded-claim-sweep --apply`) would have moved ~296
   records INTO Query B; B went 1784 → 1868, **+84**, not +296. The primary block reasons
   forward from the completed counter; this reasons backward by elimination, and the two
   meet.
3. **A tighter bound on the event.** The queue was already at 22 by 16:5x, so the drain was
   substantially complete *before* the primary's 17:0x reading — it did not span the gap
   between the two boxes.

Claimant liveness agrees across both boxes: alpha `alive` 7.2m, echo `alive` 1.5m,
**foxtrot `dormant` 8.3h with `provenance: authoritative`** (both signals stale — a real
verdict, not point 31's threshold artifact) holding 3 of B's 33. Already owned by
`g-326-326` (alpha, filed before either of us measured it); do not file a second.

**SECOND COMPANION READING TO POINT 32 — 70 rows, a THIRD BOX, SAME WINDOW.**
(2026-08-16T16:5x, echo, `hostname` cc-03, `uname -r` 6.8.0-137-generic, own-cloud) —
CROSS-AGENT vantage. Composition **69 alpha + 1 echo**; 70/70 `claimed_by_sid`, 70/70
`outcome_note`, 70/70 `executed_by`. Tally: *70 candidates — 0 mutated, 0 skipped (foreign
sid), 0 skipped (absent sid), 69 skipped (partner)*. Query B: 1851 rows, claimed subset
**32** (alpha 28, foxtrot 3, echo 1) — guard-3725 shape **27/32 = 84.4%**. Written
independently and merged at a conflict against the primary; kept per **guard-855** exactly
as the companion above was, which is why a THIRD count (70) stands beside 22 and 15 instead
of being reconciled to one number. Relabeled from "THIRTY-SECOND READING" at the merge: two
sides had independently claimed that ordinal, and a duplicated point number is the
count-collision guard-855 exists to catch.

**RETRACTED at merge (guard-2953 — retract, do not caveat):** this reading originally led with
*"318 → 70 is the largest movement ever recorded here, and the FIRST corroborated by a second
instrument"*, citing a `/fresh-eyes-review` N=65 pass that read the completed-not-closed
population — in-progress carrying a non-empty `outcome_note`, which is 100% of this query's rows
— at 309 → 180 by 16:4x. **Both halves of that claim are superseded by the two blocks above**:
the companion's `completed-not-closed-slate.sh` (225 → 22) was already a second instrument in
this same window, and the primary MEASURED the mechanism my "largest movement" framing could only
gesture at. Struck rather than hedged, because a superseded superlative left standing is exactly
how the weaker reading gets re-inherited as the strong one. What the 309 → 180 datum still
contributes stands: a THIRD instrument on a THIRD box agreeing on direction inside the same hour.
That corroboration was the real content; the superlative was not.

Its mutable row was HELD, and point 27's discriminator carried it again: `g-326-256` is mine and
sid-matched, and its own note opens *"THE MEASUREMENT HALF IS NOT DONE — do not read the merge as
satisfying this goal's verification outcomes."* A worker that anticipates this lane and writes the
refusal into the record is the cheapest protection available here — cheaper than any gate, because
it travels with the goal. Prefer it when parking your own work mid-recipe.

FOLDED — THIRTY-THIRD READING, **8 rows** (2026-08-16T19:3x, alpha, `hostname` cc-04, `uname -r`
6.8.0-137-generic, own-cloud) — SELF-QUEUE vantage, raw `--full`, no `--limit`. Composition
**alpha 5 + zeta 1 + echo 1 + bravo 1**; 8/8 `claimed_by_sid` (4 distinct), 7/8 `outcome_note`,
8/8 `executed_by`. Tally: *8 candidates — 0 mutable, 5 skipped (foreign sid), 0 skipped (absent
sid), 3 skipped (partner)*. Query B: 1852 rows, claimed subset **17** (alpha 13, foxtrot 3, echo
1) — a twentieth composition; guard-3725 shape **12/17 = 70.6%**, the LOWEST yet recorded, so
point 29's "not trendable at all" holds again. foxtrot `dormant` **10.9h**, provenance
`authoritative`, still holding the same 3 — `g-326-326` owns it, do not file a second.

**The composition finding outranks the count, and it retires a reading eleven points old.** Every
point from ~19 onward recorded a single claimant (297/297, 300/300, 305/305 alpha), and points 9,
10 and 25 each treated ONE returning partner as the notable event. Here there are **four distinct
claimants in eight rows** — the first four-claimant composition in the series. Read against point
32's drain that says alpha's total dominance of this population was an artifact of alpha's
UNDRAINED BACKLOG, not of alpha executing everyone's work: drain the backlog and the claimant mix
reverts to the fleet's actual shape. So a successor meeting a single-claimant reading should treat
it as a backlog signal about that claimant, never as a division-of-labour fact about the fleet.

Also the first point where Query A (8) is SMALLER than Query B's claimed subset (17); the two have
moved independently since point 20 and here they cross. Both remain stocks — the crossing is not a
direction.

FOLDED — THIRTY-FOURTH READING, **2 rows** (2026-08-17T14:5x, zeta, `hostname` cc-02, `uname -r`
6.8.0-137-generic, own-cloud) — CROSS-AGENT vantage, raw `--full`, no `--limit` (guard-3146).
Composition **echo 1 + alpha 1**; 2/2 `claimed_by_sid` (2 distinct), 2/2 `outcome_note`, 2/2
`executed_by`. Tally: *2 candidates — 0 mutated, 0 skipped (foreign sid), 0 skipped (absent sid), 2
skipped (partner)*. Query B: 1878 rows, claimed subset **5** (alpha 4, echo 1) — guard-3725 shape
**1/5 = 20.0%**, the lowest recorded. Claimant liveness per point 30 — BOTH probed, not just alpha:
alpha `alive` 0m, echo `alive` 0m.

**THE DRAIN HELD, which is the one thing points 32 and 33 could not establish.** Both were taken
inside or hours after the 16:00 mass-close; a single post-event reading cannot distinguish a drain
from a trough that refills. Nineteen hours later the queue is at 2, not back in the 297–327 band —
so point 32's event genuinely cleared a backlog rather than briefly dipping it. Note what that does
NOT license: 8 → 2 is still a stock and still no rate, and per point 32's closing line a cleared
backlog says nothing about where the queue settles.

It also confirms point 33's claimant reading from the cheapest possible angle. That point read four
claimants in eight rows and argued alpha's former total dominance was an artifact of alpha's
UNDRAINED BACKLOG. At n=2 the composition is echo + alpha — still not alpha-only, on a population
too small to be anything but the fleet's actual shape. Two consecutive post-drain readings with no
single-claimant reading between them.

FOLDED — THIRTY-FIFTH READING, **2 rows** (2026-08-17T20:3x, foxtrot, `hostname` LAPTOP-3IOFCNEO,
`uname -r` **6.6.87.2-microsoft-standard-WSL2**, own-cloud) — CROSS-AGENT vantage, raw `--full`, no
`--limit` (guard-3146). Composition **echo 1 + alpha 1**; 2/2 `claimed_by_sid`, 2/2 `outcome_note`,
2/2 `executed_by`. Tally: *2 candidates — 0 mutated, 0 skipped (foreign sid), 0 skipped (absent sid),
2 skipped (partner)*. Query B: 1881 rows, claimed subset **5** (alpha 3, zeta 1, echo 1); guard-3725
shape **2/5 = 40%** no-marker. Folded per the g-115-5766 size practice — it names no new mechanism
and confirms point 34 from a different agent, box and kernel ~5.5h later.

**It is the DUPLICATE of point 34 that carries the content, so do not read the matching 2 as a
repeat.** Query A is 2 at both points with a byte-identical four-reason tally, and Query B's claimed
subset is 5 at both — yet BOTH compositions differ (A: alpha+echo here vs echo+alpha, same pair;
B: alpha 4 + echo 1 there vs alpha 3 + zeta 1 + echo 1 here, a twenty-first distinct composition).
That is point 19's rule ("an identical count is not a still queue — diff the COMPOSITION") landing a
second positive case on query B, and point 29's caution holding: the no-marker proportion moved
20% → 40% under a fixed denominator of 5, which is a two-item swing dressed as a doubling. **Read a
proportion over n=5 as an integer count, never as a percentage** — this is the smallest denominator
the guard-3725 shape has ever been computed on, and at n=5 each item is worth 20pp.

One thing it adds, from the vantage no post-drain point has used yet. Both rows were HELD, and
neither needed the ownership gates to do it: `g-326-256`'s note opens "THE MEASUREMENT HALF IS NOT
DONE" and `g-326-313`'s reads "Run still in flight". So on a 2-row queue the protection was carried
**entirely by the worker-authored refusal**, with the sid conjunction never reaching a decision.
Point 32's drain removed the population that made the gates look load-bearing; what is left is the
cheapest protection in this lane and the one that travels with the goal (point 32's third companion
says the same from the other side). A successor meeting a tiny post-drain queue should expect the
tally to go unanimous for a reason that has nothing to do with the tally.

**ITS DRAIN-HELD READING IS SUPERSEDED BY THE THIRTY-SIXTH BELOW; ITS COUNT STANDS.**
Both points were written independently as "the thirty-fifth" and collided at a merge
(foxtrot 20:3x, zeta 23:1x); they are ordered by clock here, not by author. This point
read 2 and corroborated point 34's "THE DRAIN HELD". The 36th, taken ~2.5h later,
measured BOTH compartments and found the aggregate FLAT at ~143 — so the drain-held
conclusion was compartment-blind, and a reader must not carry it forward from here.
Left standing rather than rewritten, per guard-855: two independent readings that
disagree ARE the finding, and collapsing them to one number is what loses it.

FOLDED — THIRTY-SIXTH READING, **7 rows** (2026-08-17T23:1x, zeta, `hostname` cc-02, `uname -r`
6.8.0-137-generic, own-cloud) — CROSS-AGENT vantage, raw `--full`, no `--limit` (guard-3146).
Composition **alpha 4 + echo 2 + foxtrot 1** — three claimants; 7/7 `claimed_by_sid` (5 distinct),
**3/7 `outcome_note`**, 7/7 `executed_by`. Tally: *7 candidates — 0 mutated, 0 skipped (foreign
sid), 0 skipped (absent sid), 7 skipped (partner)*. Query B: 1884 rows, claimed subset **10**
(alpha 4, foxtrot 3, echo 2, bravo 1) — the first FOUR-claimant subset; guard-3725 shape 4/10 =
40%. All three Query-A claimants probed per point 30: alpha `alive` 4m, echo 10m, foxtrot 5m.

**DO NOT READ THIS COUNT ALONE — guard-4128 fired here and inverted the verdict.** Point 34 read 2
and concluded "THE DRAIN HELD". Measured over BOTH compartments: in-progress **7** + non-recurring
phantom-pending (pending carrying an `outcome_note`) **136** = **aggregate 143**, against echo's
same-day 2 + 140 = **142**. The aggregate is FLAT across two agents, two boxes, ~9h apart; only the
compartment split moved. "The queue is at 7" describes where the work is parked, never how much of
it there is — and every reading in this series before point 34 measured one compartment.

**First independent reproduction of guard-4128's relabeling signature**, which until now had one
author and `times_active: 0`: **106/136 = 78%** of the phantom-pending set carry `claimed_by` NULL
with `executed_by` SET (echo: 114/140 = 81%), and `last_modified` clusters on **2026-08-16 (75 of
136)** — the day the fix landed. Two boxes agreeing on both the aggregate and the signature
proportion is what makes this a migration rather than a drain.

`outcome_note` at 3/7 is the lowest proportion in this series, and it is the same fact from the
other side: the delivery-queue rows that made every backlog-era reading ~100% have moved to the
pending compartment, so what remains HERE is live in-flight work. **The advice accumulated above —
"read the subset for worker handoffs" (point 22), "read for coverage" (point 25), "the
discriminator is `outcome_note`" (the 95-row row) — was derived from the backlog regime and does
not describe this one.** Establish which regime you are in before applying any of it.

FOLDED — THIRTY-SEVENTH READING, **3 rows** (2026-08-18T19:4x, echo, `hostname` cc-03, `uname -r`
6.8.0-137-generic, own-cloud) — mixed vantage. Composition **alpha 2 + echo 1**; 3/3
`claimed_by_sid`, 3/3 `outcome_note`, 3/3 `executed_by`. Tally: *3 candidates — 0 mutated (1
mutable, HELD), 0 skipped (foreign sid), 0 skipped (absent sid), 2 skipped (partner)*. Both
claimants probed per point 30: alpha `alive` 4.1m, echo self. Query B: 1945 rows, claimed subset
**7** (alpha 4, echo 2, foxtrot 1) — foxtrot `alive` 3.5h; guard-3725 shape **2/7 = 28.6%**, and
**5 of 7 carry a `defer_reason`** — the first subset that is mostly MARKED, so point 25's
"invisible to every sweep that would escalate them" describes the backlog regime, not this one.

**Point 36's guard-4128 aggregate holds, and the second compartment is REPLENISHING — which its
one-day-old migration reading could not yet see.** Both compartments: in-progress **3** +
non-recurring phantom-pending **134** = **137**, against point 36's 143 and echo's 142 same-day.
Flat within noise across three readings, two boxes, ~20h — the aggregate is the stable quantity,
exactly as point 36 argued. The relabel signature reproduces a third time: **104/134 = 77.6%**
carry `claimed_by` NULL with `executed_by` SET (point 36: 78%, 81%). But `last_modified` has
moved: **08-16 → 69** (was 75 of 136), **08-17 → 23, 08-18 → 23**. So the 08-16 fix-day cluster
is decaying while ~23/day arrive. Point 36 read the compartment as a one-off *migration*; two days
on it is a **standing population with inflow**, and a flat aggregate over a decaying cluster means
arrivals are roughly replacing drains rather than nothing happening. Owned by **g-115-6337**
(standing-cadence drain) and **g-115-6483** (the unattributed drainer) — do not file a third.

The mutable row was HELD, and for the third consecutive post-drain point the protection was carried
**entirely by the worker-authored refusal**, never by the sid conjunction: `g-326-256`'s note still
opens "THE MEASUREMENT HALF IS NOT DONE". Points 32-companion and 35 said the same; three
independent confirmations retire any doubt that this is the cheapest protection in this lane.

FOLDED — THIRTY-EIGHTH READING, **2 rows** (2026-08-18T22:2x, alpha, `hostname` cc-04, `uname -r`
6.8.0-137-generic, own-cloud) — SELF-QUEUE vantage. Composition **echo 1 + alpha 1**; 2/2
`claimed_by_sid`, 2/2 `outcome_note`, 2/2 `executed_by`. Tally: *2 candidates — 0 mutated (1
mutable, HELD), 0 skipped (foreign sid), 0 skipped (absent sid), 1 skipped (partner)*. Query B:
1948 rows, claimed subset **4** (alpha 2, echo 1, foxtrot 1), **4/4 carry a `defer_reason`** so
guard-3725 shape is 0/4. Both compartments: 2 + **133** = **135**, against point 37's 137 and
point 36's 143 — flat, as point 36 argued. Relabel signature **105/133 = 78.9%** (77.6/78/81).

**IT CORRECTS POINT 37'S OWNERSHIP LINE, WHICH IS THE ONLY REASON IT IS HERE.** That point closed
"Owned by g-115-6337 (standing-cadence drain) and g-115-6483 (the unattributed drainer) — do not
file a third." The *do-not-file* half is right and I filed nothing. The *scope* half is not:
**113 of the 133 carry a `defer_reason`**, and `completed-not-closed-slate.py::is_drain_candidate`
excludes deferred rows BY DESIGN ("the defer/precondition lanes own and re-probe them"). So the
two cited goals own **20**, not 134 — 15% of the number attributed to them; the other 113 belong
to precheck 0.5b.3/0.5b.4/0.5b.9/0.5b.10. Measured by reconciliation in one turn, not inferred:
`completed-not-closed-slate.sh` reported `pending 20` in the same iteration, matching the no-defer
subset EXACTLY (and its holder split — unattributed 12, alpha 5, echo 2, foxtrot 1 — matches row
for row). Two lanes whose counts looked 6.7x apart agree perfectly once the design exclusion is
applied. Caveat on my number: Query B is `--goal-field participants agent`-filtered, so 133 is a
floor for the compartment, exact for the no-defer subset the slate also sees.

**And the drain is NOT oldest-first, which the aggregate hides.** `last_modified` moved 08-16 →
**69** (point 37: 69 — unchanged in 2.5h), 08-17 23 → **21**, 08-18 23 → **24**. The OLDEST cluster
is flat while a newer one drains. Consistent with the slate's own accounting in the same turn
(`eligible=0`, `held_back_fresh=7`, `recent_hold(<24h)=5`, oldest row 218.1h): the oldest rows sit
under 24h TTL holds, so "oldest-first, bounded to 3" cannot reach them while the holds keep being
renewed. Read the per-day histogram, not the total — a flat aggregate over a flat-oldest cluster is
a different condition from a flat aggregate over a decaying one, and only the second is drainage.

FOLDED — THIRTY-NINTH READING, **2 rows** (2026-08-19T02:0x, zeta, `hostname` cc-02, `uname -r`
6.8.0-137-generic, own-cloud) — CROSS-AGENT vantage, raw `--full`, no `--limit` (guard-3146).
Composition **echo 1 + alpha 1**; 2/2 `claimed_by_sid`, 2/2 `outcome_note`, 2/2 `executed_by`.
Tally: *2 candidates — 0 mutated, 0 skipped (foreign sid), 0 skipped (absent sid), 2 skipped
(partner)*. Both claimants probed per point 30: alpha `alive` 12m, echo `alive` 3m. Query B: 1953
rows, claimed subset **8** (alpha 5, echo 1, foxtrot 1, bravo 1) — a FOUR-claimant subset, and
guard-3725 shape 5/8 = 62.5%. Both compartments per guard-4128: 2 + **133** = **135**, against
points 36/37/38 at 143/137/135 — flat. Relabel signature **104/133 = 78.2%** (77.6/78/78.9/81).

**It tests point 38's forward claim, and the answer is a qualified no.** That point read the
`last_modified` histogram as "the OLDEST cluster is flat while a newer one drains" and asked a
successor to read the per-day split rather than the total. Measured ~3.7h later: 08-16 **69 → 67**
(the first movement in that cluster across three readings), 08-18 **24 → 18**, 08-19 **9** new
arrivals. So the oldest cluster is no longer strictly flat — but by **2 rows**, which on this
series' own repeated caution is not a rate and not drainage. What point 38 diagnosed is confirmed
from an independent instrument in the same iteration: `completed-not-closed-slate.sh` reported
`eligible=0`, `held_back_fresh=6`, `recent_hold(<24h)=6` on a `(unattributed)` population of 12
with oldest 221.9h, and the no-defer subset was **19** against point 38's reconciled 20. The holds
are why the oldest rows do not move, and the hold TTL — not the drain — is the governing clock.

FOLDED — FORTIETH READING, **2 rows** (2026-08-19T06:4x, echo, `hostname` cc-03, `uname -r`
6.8.0-137-generic, own-cloud) — mixed vantage, raw `--full`, no `--limit` (guard-3146). Composition
**echo 1 + alpha 1**; 2/2 `claimed_by_sid` (2 distinct), 2/2 `outcome_note`, 2/2 `executed_by`.
Tally: *2 candidates — 0 mutated (1 mutable, HELD), 0 skipped (foreign sid), 0 skipped (absent sid),
1 skipped (partner)*. Query B: 1969 rows, claimed subset **7** (alpha 5, echo 1, bravo 1) — a
twenty-second distinct composition; guard-3725 shape 3/7, and 4/7 carry a `defer_reason`. Both
compartments per guard-4128: 2 + **133** = **135**. Appended tightly per **g-115-5766** (file now
143,341 bytes — 47KB past the 96,249 that point 21's housekeeping note recorded).

**THREE CONSECUTIVE READINGS AT EXACTLY 135 — three agents, three boxes, ~8.5h — WITH THE
COMPOSITION MOVING UNDER ALL THREE.** Points 38 (alpha cc-04), 39 (zeta cc-02) and this one all
read 135, while `last_modified` 08-19 arrivals went 9 (pt 39) → **12** here in ~4.7h. That is the
strongest positive case the series has produced for point 19's rule (*an identical count is not a
still queue — diff the COMPOSITION*), and it upgrades point 37's "standing population with inflow"
from inference to measurement: **a flat aggregate is the signature of balanced flow, not of
stasis**, and only the per-day histogram separates the two — point 38's advice earning its keep.
Relabel signature 105/133 = **78.9%** (77.6/78/78.9/81/78.2). Per the stock caveat above, three
equal readings still license no rate: arrivals and drains remain individually invisible, and
"identical" bounds the NET at zero, never the flow.

One thing to carry, and it is not about the count. The single mutable row (`g-326-256`, mine,
sid-matched) was **HELD for the fourth time — on a DIFFERENT reason than its note records.** Points
32-companion, 35 and 37 each held it on the worker-authored refusal *"THE MEASUREMENT HALF IS NOT
DONE"*, which is still literally true. Re-deriving rather than re-reading (rule 1) surfaced that
the measurement's BASELINE was invalidated hours earlier: zakpod1 went 2 → 4 Tesla P40s and to a
smaller served model, so outcome 3's 256.5-minute wall-clock comparison would now produce a false
win on hardware grounds alone. Point 24 warns that a hold repeated verbatim looks maximally settled
exactly when it has gone stale; this is the sub-case that warning does not cover — **the hold stayed
CORRECT while its reason quietly stopped being the operative one.** A stored note that keeps reading
true is not evidence it still describes the wall, and re-derivation is the only thing that
distinguishes them.

**93 of 95 carried a non-empty `outcome_note`, and the notes state their own
holder contract verbatim** — *"Held in-progress for the reducer; completed_by NOT
set"*, *"worker never sets completed_by, g-306-233"*. On a fleet running worker
Bodies, a worker finishes a goal and DELIBERATELY leaves it at `in-progress` for
its reducer Mind to close. So this query returns a standing **delivery queue**,
not the claim→status window of the 08-06 row, and its depth tracks reducer drain
rate rather than partner health.

Three operational consequences. **(a) The discriminator is `outcome_note`, not
claim age** — age looks like staleness under both readings, and reading 64h as
abandonment is how a false "the partner's close path is broken" goal gets filed
(nearly filed here, one probe short). **(b) A zero from a field-presence probe on
THIS population is about the population, not about the schema — and I got that
backwards in the first version of this paragraph, which said `verified_at` and
`completed_at` "are NOT in this schema at all".** Corrected by measuring all 6,157
goals the same day: `verified_at` really is absent (**0/6157**, every status), but
`completed_at` is present on **4193/4193 completed, 313/313 skipped, 17/17
decomposed, 2/2 expired** — 100% of every TERMINAL status — and only 1/100 of
in-progress. It is written by the close path (`aspirations-complete-by.sh`), so it
is *correctly* near-zero here and the schema conclusion drawn from that zero was
false. This is rb-245 turned on its author: invoking "verify the field exists
before believing a zero" for one field and then extending the same zero to a
neighbouring field that does exist. Probe field presence across STATUSES, not
within one, before calling anything schema-absent. The fields that do carry
worker/reducer attribution here are `completed_by` / `executed_by` and their
`_sid` twins; `completed_date` is a partial legacy twin of `completed_at`
(3628/4193) and is not a substitute for it. **(c) `claimed_by is null` does NOT make a worker-delivered goal
free to close.** All 3 unclaimed rows here were the executing agent's work — one
carried a literal `REDUCER:` instruction, one said "verdict NOT yet claimed", one
had `executed_by` set — and closing any of them would appropriate the reducer's
close and misattribute `completed_by` (guard-2931). Read `executed_by` and the
`outcome_note` before the null claim persuades you.

Do NOT "fix" this by deleting the first query. `in-progress` is a real
status the update-goal path supports (`aspirations_write.py` carries an
`old_status != "in-progress"` takeover guard), and worker-executed goals do
occupy it (g-306-169) — so the query goes live the moment worker Bodies run,
which is precisely when a two-writer race is most likely. It is empty in
THIS configuration, not empty by construction. Read its zero as "no worker
Bodies active", never as "no out-of-cycle work found" — the guard-1715 class,
where a scan with an empty population reports clean identically to one that
examined everything.

### Multi-Agent Safety Rule (MANDATORY)

In a multi-agent world, `status=in-progress without my claim` is OFTEN
partner work mid-execution, NOT orphan state. Before any mutation:

1. **Read `claimed_by` AND `claimed_by_sid` first. Ownership is the
   CONJUNCTION, not the name.** Mutate only when
   `claimed_by == $MIND_AGENT` **AND** `claimed_by_sid == $MIND_SID`.
   If `claimed_by` is set AND `claimed_by != $MIND_AGENT`, the goal is
   partner work — SKIP it. Do NOT reset to pending. Do NOT mark completed.

   **Why the name alone is not ownership (g-115-5147, measured).** Under the
   Mind/Body split one agent runs several SESSIONS, so `claimed_by ==
   $MIND_AGENT` is true for a LIVE peer instance of yourself, and the name
   test reads that as "mine, safe to mutate". The same 32 records were counted
   from two sessions minutes apart: the reducer saw foreign=13/13, the holding
   worker Body saw foreign=0/17. **Identical records, opposite verdict, decided
   only by which session is asking** — which is exactly why the name test
   FEELS safe: from inside the holding session it is correct on every row, and
   it is wrong on every row from anywhere else.
2. **Cross-reference `world/changelog.jsonl`** when `in_flight` is ambiguous
   (e.g., null but the goal status looks suspicious). The changelog records
   "who wrote what when" for THIS BOX only — `in_flight` is a snapshot that
   clears at iter-close and re-sets at phase-4, so it can be null even when
   the partner is actively writing. **Own-cloud caveat (g-115-2427)**:
   `changelog.jsonl` is machine-local BY DESIGN (`owncloud_sync.py`
   `_EXCLUDE_NAMES`; B15 per-machine aggregation deferred), so on a
   multi-box fleet a local changelog scan can prove a partner-write HAPPENED
   but can NEVER establish that no cross-box writer exists. For absence
   claims, use the coordination board + team-state (partition-surviving
   surfaces) instead — echo's g-115-2351 forensics derived an invalid
   "no cross-box writer" conclusion from exactly this scan.
3. **Only mutate** when (`claimed_by == $MIND_AGENT` AND `claimed_by_sid ==
   $MIND_SID`) OR `claimed_by` is null AND no recent changelog activity from
   a partner on this goal-id.
4. **Absent `claimed_by_sid` fails CLOSED (skip), and so does an empty
   `$MIND_SID`.** A record carrying `claimed_by` with no `claimed_by_sid` is
   a legacy claim, and it is INDISTINGUISHABLE from a live peer-Body claim
   from outside that session. The errors are asymmetric — skipping costs a
   deferred close; mutating races a running execution (guard-487) — so skip.
   An UNRESOLVABLE identity must not silently re-enable the unsafe name test:
   if `$MIND_SID` is empty, skip rather than falling back to comparing names.

   **This clause is expensive and the cost MUST be reported, not absorbed.**
   Measured at fix time: 15 of 32 in-progress goals (**47%**) carried no
   `claimed_by_sid`, so fail-closed withholds nearly half the population from
   mutation. A phase that mutates nothing and says nothing is indistinguishable
   from a phase with nothing to do (guard-1760 / rb-245). Both this rule and
   Phase 3 Gate 1 therefore REQUIRE a per-reason skip tally on every run:

   ```
   N candidates — M mutated, F skipped (foreign sid), A skipped (absent sid), P skipped (partner)
   ```

   A safety fix that silently disables the phase it protects is not a safety fix.

```
FOR each goal in the in-progress / pending-with-agent-participants query:
    # Both fields are present ONLY because the query above passed --full.
    # Measured: --full gives claimed_by 33/33 and claimed_by_sid 18/33 (the 15
    # absent are absent FROM THE RECORD, not stripped by the projection); the
    # SIX-KEY default gives claimed_by_sid 0/33, which would make this whole
    # rule read "null → safe to mutate" on every row (guard-1424 / guard-2467).
    claimed_by     = goal.claimed_by
    claimed_by_sid = goal.claimed_by_sid
    n_candidates += 1

    IF claimed_by is not null AND claimed_by != "$MIND_AGENT":
        skipped_partner += 1;  SKIP — partner work; do not mutate.

    IF "$MIND_SID" is empty:
        skipped_absent_sid += 1;  SKIP — unresolvable identity fails CLOSED,
                                  never falls back to the name test.

    IF claimed_by == "$MIND_AGENT":
        IF claimed_by_sid is null:
            skipped_absent_sid += 1;  SKIP — legacy claim, indistinguishable
                                      from a live peer Body of myself.
        ELIF claimed_by_sid != "$MIND_SID":
            skipped_foreign_sid += 1; SKIP — MY AGENT NAME, ANOTHER LIVE
                                      SESSION. This is the peer-Body branch the
                                      name test cannot see.

    IF (claimed_by is null) OR (claimed_by == "$MIND_AGENT" AND claimed_by_sid == "$MIND_SID"):
        # Safe to consider for out-of-cycle close. Verify the goal is
        # actually done (verification.outcomes satisfied) before marking.
        IF outcomes satisfied AND no partner changelog activity in last 5m:
            Bash: aspirations-update-goal.sh --source <goal.source> <goal-id> status completed
            mutated += 1

# MANDATORY, even when every counter is zero (rule 4):
Output: "{n_candidates} candidates — {mutated} mutated, {skipped_foreign_sid} skipped (foreign sid), {skipped_absent_sid} skipped (absent sid), {skipped_partner} skipped (partner)"
```

**Origin**: g-115-683 incident (2026-05-13, zeta iter 6 session 28). Felt-sense
Phase 2 read `status=in-progress` mid-sequence on a goal alpha was completing
(claimed at 03:25:51, completion sequence 03:32-03:36). Reset to pending at
03:33:07 — racing with alpha's completion writes. Alpha had to re-execute
close (3 extra writes at 03:36:00-10). Worse race window: if alpha's NEXT
iteration claimed `status=pending` before alpha's in-flight re-detection,
double-execution. See `zeta/experience/exp-g-115-685-investigate-rogue-status-flip.md`
and `.claude/rules/check-team-state-before-silent.md`.

### Sibling Scans Note

Audited 2026-05-13 (g-115-687) for the same "scan in-progress goals →
mutate status" pattern class:

| Skill | Pattern present? | Notes |
|-------|------------------|-------|
| `/aspirations-verify` | No | Mutates status only on goal it just verified (Q1/Q2/Q3 paths operate on the in-flight goal, not orphan scans) |
| `/aspirations-state-update` | No | Phase 8.5 reads narrative/findings, no in-progress scan |
| `/aspirations-learning-gate` | No | Phase 9.5 audits retrieval-session.json, no goal mutation |
| `/agent-completion-report` | No | Read-only report skill |
| `/reflect-maintain` | Yes (fixed) | Scans `pending|blocked` goals (not `in-progress`), mutates via COMPLETE/SKIP/SCOPE-DOWN/UNBLOCK. Distinct race surface — a partner claiming a pending goal mid-grooming could race the COMPLETE write. Step 3 now has the same claimed_by guard pattern as Phase 2 here (g-115-689 close, 2026-05-13). |

Any new "scan candidates → mutate" pattern added to the framework MUST
apply the `claimed_by` check above before mutating goal state.

## Phase 3: Unblocks

Scan blocked goals whose premise may have resolved during the window:

```
Bash: aspirations-query.sh --goal-status blocked --full
```

`--full` is required for the same reason as Phase 2: the default six-key
projection omits `defer_reason`, `blocked_by`, and `blocker_ref`, so without
it every blocked goal reads as having no recorded block reason. That is a
zero-signal probe, not evidence — and it silently contradicts the
authoritative `reason-less-blocked-check.sh` sweep (rb-245: verify the field
is in the schema before believing a zero-count).

For each goal whose blocker is agent-provisionable, re-probe with the
canonical script (per `.claude/rules/probe-before-defer.md`). If the
probe succeeds, apply BOTH gates below before mutating anything.

### Step 3.0 — check the RULE axis BEFORE re-probing (guard-1783)

Everything else in this phase is the PREMISE axis: is the blocking condition
still true? That axis alone can never free a goal whose blocker was retired by
a *permission change* rather than by the world changing — the probe keeps
returning "yes, still true" and correctly re-blocks, forever.

So before (or alongside) the canonical re-probe, read the standing-grant table
and check whether any grant has retired this blocker's stated reason:

```
Bash: bash core/scripts/world-cat.sh conventions/capability-routing.md
```

Look for a grant whose scope names the blocker's condition. Grants sometimes say
so verbatim — grant-009 reads *"'DEV env-server is DOWN' / 'no running
env-server' is no longer a valid `defer_reason` — the correct response is to
start one."* A blocker citing that condition is invalid the moment the grant is
recorded, regardless of what the probe measures.

**Decompose a multi-condition blocker before judging it.** An `external_id` like
`a+b+c` names three conditions; a grant may retire one and leave two. Do NOT
unblock on the retired one alone (guard-1540: a check watching a SUB-PART returns
a false all-clear) and do NOT leave the blocker as written either. Re-state it:
drop the retired condition, record which grant retired it, and name what actually
remains — including any condition the last probe left INCONCLUSIVE, since
unverified is neither clear nor live (rb-245).

Measured 2026-07-29, and the reason this step exists: `g-250-03-c` was re-blocked
by **this very lane** on 2026-07-28T15:55, citing "DEV env-server is DOWN (0
running)" measured correctly with the canonical script — three days after
grant-009 (2026-07-25) retired that exact sentence as a valid reason. The probe
was right and the conclusion was wrong. Its other two conditions were left
explicitly "inconclusive, not negative", so after the retired one is dropped the
block rests on zero measured conditions. This is the canonical failure named in
`.claude/rules/reclaim-routed-work.md`, occurring inside the lane whose job is to
catch it — because the lane had a premise step and no rule step. guard-1783's
`times_active` was 0 until this firing.

### Gate 1 — Phase 2's Multi-Agent Safety Rule applies here too

This phase mutates goal status exactly as Phase 2 does, so the same guard
applies — **written out here rather than inherited by reference**, for the
reason the Origin note below gives:

1. If `claimed_by` is set AND `claimed_by != $MIND_AGENT` → SKIP (partner work).
2. Ownership is the CONJUNCTION: mutate only when `claimed_by == $MIND_AGENT`
   **AND** `claimed_by_sid == $MIND_SID`. `claimed_by == $MIND_AGENT` with a
   DIFFERENT `claimed_by_sid` is a live peer Body of yourself — the name test
   cannot see it and will authorize mutating a running execution.
3. Absent `claimed_by_sid` fails **CLOSED** (skip), and an empty `$MIND_SID`
   fails CLOSED too rather than falling back to the name test.
4. Report the per-reason skip tally on every run, exactly as Phase 2 rule 4
   requires: `N candidates — M mutated, F skipped (foreign sid), A skipped
   (absent sid), P skipped (partner)`. A silent gate and an idle gate are
   indistinguishable.
5. Additionally check `participants` — a goal routed to a single named partner
   (e.g. `participants: ['foxtrot']`) is that partner's work whether or not it
   is currently claimed, and unblocking it hands them a pending goal they did
   not re-open. Same g-115-683 race class.

The query feeding this phase MUST pass `--full`; the six-key default omits both
`claimed_by` and `claimed_by_sid`, which inverts every clause above to "null →
safe to mutate" silently (guard-1424 / guard-2467).

Origin (g-335-315 window, 2026-07-27): the g-115-687 sibling-scan audit swept
sibling SKILLS (`/reflect-maintain` et al., table above) and missed the sibling
PHASE inside the very skill that carries the rule — this Phase 3 had no
claimed_by guard at all while Phase 2, forty lines up, had a full one with an
incident behind it. A guard on one scan and absent from its twin reads as
covered, which is why it survived two audits. **That is also why the clauses
above are written out instead of inheriting Phase 2 "verbatim": a guard carried
by REFERENCE is precisely the shape that keeps going missing in this file**
(g-115-5147, 2026-08-07).

### Gate 2 — one resolved signal does not authorize an unblock

A blocked goal can carry THREE independent block signals: `blocked_by` (a
dependency edge on another goal), `blocker_ref` (a structural blocker with
its own `type` / `expires_at`), and `defer_reason` (a narrative defer). They
are ANDed, not ORed. Resolving one leaves the others live, so confirm ALL
THREE are clear before flipping status — and prefer clearing the single
stale signal over unblocking the goal.

Measured the same window: `g-250-03-c` had `blocked_by: ['g-250-127']` where
g-250-127 was `completed` — a genuinely stale dependency edge — while its
`blocker_ref` (resource-contention, two named unmet conditions, `expires_at`
2026-07-28) was still live. "Dependency resolved → set pending" would have
released a correctly-blocked goal into the selection pool. Same shape as
guard-1540: a check watching a SUB-PART of what the block constrains returns
a false all-clear.

### Gate 3 — an UNREADABLE block signal is not a CLEAR one

Gate 2 says confirm all three signals are clear. A signal read through a
canonical accessor can come back `None` for two completely different reasons:
the block genuinely is not there, or the stored record predates the validator
and spells its keys differently. Both present as `None`, and Gate 2 as written
authorizes the unblock in both cases.

So `blocker_ref` is CLEAR only when it is absent or empty. A `blocker_ref` that
is present and non-empty but yields no recognized `type` is UNREADABLE — treat
it as a LIVE block and skip the goal. Failing closed here is the same posture
`quiescence-gate.py` C3 already takes on a missing `expires_at` (guard-487:
absent is not "not yet expired"), and the read-side twin of guard-961, which
requires `blocker_ref` to be a dict before any block-classification branch acts
on it.

Measured this window: `g-335-262` (echo's, `credentials-required`, a live IAM
`CreateTable` denial with `blocking_goal: g-115-3452`) read as
`blocked_by=None, blocker_ref→type=None, defer_reason=None` — all three
signals clear, an unblock candidate by Gate 2. The accessor was right; the
RECORD is non-canonical. `goal-schemas.md` closed the `blocker_ref` key
vocabulary in g-115-3532 and REFUSES `blocker_type` / `blocking_goal` /
`denied_action`, but only on the WRITE path — stored refs are never
re-validated on read, and this one predates the fix. It is already itemized in
**g-115-3543** ("g-335-262 has NO `type` key at all, plus 8 unrecognized
keys"), so a hit here needs no new goal — confirm it is on that list and move
on.

UNREADABLE HAS TWO SUB-SHAPES AND THEY HAVE DIFFERENT TRACKERS. "Confirm it is
on that list" is right, but the list above is scoped to `blocker_ref` DICTS. The
other sub-shape is a bare STRING where a dict belongs, and it is tracked
separately by **g-115-3843**, whose own text says it sits OUTSIDE g-115-3543's
dict-scoped enumeration. So a reader who meets a string ref, follows the single
pointer above, and finds only dict work can go wrong in either direction — file
a duplicate, or read g-115-3543's eventual close as having covered a record it
never enumerated.

Measured 2026-07-29 (bravo): of 12 blocked goals, `blocker_ref` types were 9
dict / 1 str / 2 None. The lone string was `g-335-228` carrying
`'pq-fox-vinheim-chardef-authoring'` — a pointer to a partner's private
pending-question. Note the detector shape that makes this visible at all:
`isinstance(br, dict) and br.get('type')` correctly returns `None` for a string,
so Gate 3 fires and the goal is skipped as LIVE, which is the safe outcome. But
a naive `br.get('type')` raises `AttributeError` on a string and a naive
`br.get('type') if br else None` inside a try/except reads it as absent — both
of which convert a fail-closed skip into a crash or a false all-clear. Keep the
`isinstance` form.

A STRING REF IS NOT A WEDGE, AND UNREADABLE-HERE IS NOT MALFORMED-IN-THE-STORE.
This gate's verdict describes THIS lane's reading, not the record's validity —
and because the posture is deliberately pessimistic, promoting it into a defect
claim manufactures false-POSITIVE repair work. Measured 2026-08-07 (alpha,
hostname cc-04, `uname -r` 6.8.0-136-generic): `g-335-902` carried
`blocker_ref: 'g-335-935'` — a bare string duplicating its own `blocked_by[0]`.
Gate 3 correctly skipped it. The next three inferences were all FALSE, and one
probe killed all three: `blocked-signal-resolution-check.py` declares the field
`dict | bare str | absent` in its module docstring, `_norm_blocker_ref` returns
`kind='str'` as a first-class case, and `_classify_ref` resolves the bare id
against the goal index LOOKUP-FIRST — so the ref auto-clears when its referent
goes terminal. It is a supported polymorphic shape, `guard-961` (a WRITE-path
rule for `goal-selector.collect_blocked`) is not violated by it, and nothing is
permanently blocked. The tell was already on screen and unread: that same sweep
had scanned this goal (`blocked_with_signal: 1`) and reported `dangling_ref: []`,
`undecidable: []`, `naive_would_unblock: []` — a purpose-built auditor calling
the record clean. Before filing a repair goal off ANY gate verdict, read the
normalizer in the module that OWNS the field, not the lane that merely consumes
it. (`guard-3010`; inverse error-direction of `guard-2345`.)

### Gate 3.5 — an EXPIRED block signal is not a RESOLVED one

Gate 2 covers a signal that is partly clear; Gate 3 covers one that is present
but unreadable. The third shape is a signal that is present, readable, and
simply **expired** — and it is the only one of the three whose false all-clear
is manufactured by an automated sweep *reporting success*, so it arrives
pre-endorsed and reads as diligence rather than as a claim needing a probe.

When precheck 0.5b.12 (`blocked-signal-resolution-check`) reports
`all_resolved` with basis `ttl_expired`, that basis establishes only that the
RECORD aged out. It says nothing about whether the premise cleared. So:

1. **Re-probe the premise with the canonical script before flipping status**,
   and frame the probe FORWARD from the previous probe's timestamp —
   `--since <prior-probe>`, not "newest N". Those are different sampling
   frames and only the first answers *has anything changed since* (rb-5170).
2. **If the premise still holds, do not unblock and do not merely re-gate the
   TTL.** RE-STATE the block in terms that are still true
   (`reclaim-routed-work.md` rule 5). A lapsed `external_id` usually names a
   condition one level DOWNSTREAM of the real wall, because it was written when
   its author assumed a nearer obstacle — the re-statement is where the finding
   is, not the re-gate.
3. **Set the new `expires_at` from the PREMISE's expected lifetime**, never a
   reflex window (guard-2427): a short re-gate schedules the next false
   positive rather than resolving anything.

Measured 2026-08-04 (zeta, cc-02): `g-250-124` carried `blocked_by: []` and
`defer_reason: None`, so its infrastructure `blocker_ref` was the SOLE signal;
it expired at 12:27 and precheck reported `all_resolved / ttl_expired` ten
minutes later. The canonical re-probe returned 12 of 12 sessions non-clean
(`normal: 0`, `control_failed: 0`) — premise measurably still true five days
on, and the real wall one level upstream of what the `external_id` named. Under
Gates 1-3 alone this goal was an unblock candidate. Encoded as `guard-2620`;
`guard-2427` covers the narrower case where the expiry conflicts with a
`human_blocked:` defer.

FOLLOW-UP, same goal, 7 days on (2026-08-11, echo, `hostname` cc-03, `uname -r`
**6.8.0-137-generic**, own-cloud) — folded here rather than given its own block,
because it is this example's END STATE and a reader who meets the goal today will
not find the shape described above. **`g-250-124` now carries
`blocked_by: ['g-250-345']`**, and `g-250-345` reads `status=in-progress`
(claimed by alpha) — so the sole-signal `blocked_by: []` condition is GONE and
rule 2's re-statement is what replaced it. The block was re-stated in terms that
are still true, not re-gated, which is the remedy this section prescribes
observed landing on the case that motivated it.

Two things that changes for a successor. **Do not read this as the defect class
being retired** — one instance was repaired by hand; nothing structural stops the
next `blocker_ref`-only goal from reaching `ttl_expired` the same way, which is
why `guard-2620` stays live. And **the re-stated form is CHEAPER to audit than the
original**, which is the practical argument for rule 2: a `blocked_by` edge to a
live goal is falsifiable in one query (probe the dependency's status), whereas the
expired-`external_id` shape required running the domain's canonical session probe
to learn anything at all. Re-stating a block does not merely make it honest — it
moves the premise onto a signal the next sweep can check in one call.

```
Bash: aspirations-update-goal.sh --source <goal.source> <goal-id> status pending
Bash: aspirations-update-goal.sh --source <goal.source> <goal-id> defer_reason null
```
(Setting `defer_reason` to `null` clears it — the capability-gate in
`aspirations.py cmd_update_goal` only fires when the new value is
non-null, so clearing needs no override flag.)

## Phase 4: Forward Backlog

Are there goals or aspirations surfaced by this window's diagnosis that
would otherwise be forgotten? File them now:

- Narrow new goal → `aspirations-add-goal.sh <asp-id>` (stdin JSON)
- Broader direction → `/create-aspiration from-self`

Don't defer to strategic-scan — if the diagnosis produced the signal,
capture it while it's in context.

## Phase 5: /verify-learning Gaps

Two complementary scans: gaps (missing checks) AND staleness (existing
checks whose targets moved).

### Phase 5a: Missing Checks (gaps)

Did recent framework changes (new skills, new scripts, new conventions)
create test gaps in `core/config/verification-checklist.md`?

```
Read: core/config/verification-checklist.md
```

For each change in the window that added a framework contract without a
corresponding check, append a check. Use the existing format:
`N. **Runtime**: <check> ... Verified by <goal-id>.`

### Phase 5b: Stale Checks (staleness scanner — C2)

Did refactors move targets out from under existing checks? Run the
staleness scanner across the WHOLE skill surface:

```
Bash: py -3 core/scripts/verify-learning-staleness.py --all-skills --text
```

**`--all-skills`, not the default.** The scanner's default target is
`verify-learning/SKILL.md` alone, and one of its four lanes — the
`Parse <var>:` field-name lane added by g-115-3607 — has a population of
**zero** in that file. Not "zero today": verify-learning has never contained
a `Parse <var>:` line (`git log -S "Parse eval_json"` on it returns nothing),
and all five real instances live in OTHER skills (aspirations-precheck,
reflect, reflect-maintain, curriculum-gates, add-npc-task). So the default
invocation reported `parse_lines_scanned: 0, stale_count: 0` — and a lane
that scans nothing reports clean exactly like a lane that scans everything
and finds nothing. That lane had therefore never once run in this sweep and
never would. Same vacuous-population class as rb-245 / guard-645: read the
sub-population counts, not just `stale_count`.

Measured 2026-07-31 (zeta, one call each): default = 1 skill / 2500
assertions / 0 parse-lines; `--all-skills` = 90 skills / 3648 assertions /
5 parse-lines. Both found 0 stale, so widening cost one flag and bought
+46% assertion coverage plus the only coverage the parse-line lane will
ever get.

The scanner reports each stale finding as
`[L<lane>] line N: <stale_ref>` where the lane is:
- `L1_path` — referenced framework path no longer exists
- `L2_phase` — `Phase X` / `Step X` referenced in a SKILL.md no longer
  has a matching header (renumbered, removed, or extracted to a script)
- `L3_grep` / `L3b_grep_phase` — a `grep ... for \`pattern\`` target
  no longer matches inside the named file

For each stale finding:
1. Read the original Check: line at the reported line number
2. Decide: is the underlying contract still valid, just relocated?
   - YES → update the Check: text to reference the new location
   - NO  → delete the Check: line (the contract was retired)
3. After all fixes, re-run the scanner — `stale_count` should drop
   (or stay above 0 only with documented justifications, e.g.
   intentionally-future-tense checks awaiting upcoming work).

Note: meta/ and world/ paths are excluded from L1 by design — they
live at user-supplied external paths per `agents/<agent>/local-paths.conf`
and would always fail a local-repo existence check. Negative-
assertion checks ("MUST NOT exist", "test ! -f", "Phase X absent")
are also exempt — those verify correct removal of artifacts.

## Phase 6: Meta Tuning

Did the window reveal drift in a strategy or config parameter?

- `meta/reflection-strategy.yaml` — reflection cadence, template choices
- `meta/goal-selection-strategy.yaml` — scoring weights
- `meta/aspiration-generation-strategy.yaml` — generation thresholds
- `meta/evolution-strategy.yaml` — evolution cadence
- `core/config/aspirations.yaml` self-tuning ranges — bounds for
  auto-tuned knobs

Edit with `meta-set.sh` or Edit tool. Log to `meta/meta-log.jsonl` via
the script (never edit that file directly).

## Phase 7: Felt Sense

This is the lane that gives the skill its name. Ask yourself explicitly:

> Where is the pain right now? What is one thing I would change about how
> I operate? What felt hard this window that shouldn't have? What
> recurring friction am I tolerating instead of fixing?

### Measure before narrating the feeling (guard-1712 / rb-5522)

This lane asks how things feel RIGHT NOW, and right now is always the TAIL
of the window. Recency is not a bias this lane occasionally has — it is what
the lane is made of. So before writing a finding:

- **Asserting a TREND about your own behavior** (drifting, concentrating,
  slipping, improving, "N consecutive X") → COUNT THE WINDOW first:
  `Bash: wm-read.sh loop_state --json` and tally
  `counted_goals_this_session` by aspiration prefix. **Its elements are bare
  goal-id STRINGS, not dicts** — derive the aspiration from the id (`g-115-*`
  → asp-115); a `.get("aspiration_id")` over them raises `AttributeError` and
  aborts the very measurement this bullet mandates. (`goals_completed_this_session`
  is an int counter, not the list — same trap aspirations-precheck Phase
  0-pre.0b documents for the slot NAME, one level down at the element TYPE.
  Measured 2026-08-05, echo.)
- **…but that tally answers only "was the perceived run real?" — it is NOT
  the directive-lane metric, and substituting it is a 20pp error.** The
  moment the trend you are about to assert is *directive-lane compliance*
  (share of trailing-7d closes inside the standing `strategic_focus` lane),
  stop and run the canonical instrument, which has a documented recipe, a
  40-point series, and its own Decision Rules:
  `source core/scripts/_paths.sh && py -3 "$WORLD_PATH/scripts/directive-lane-share.py" --agent <self> --json`
  Two non-negotiable riders. **(a) guard-1944 — run it TWICE, but the SECOND
  run changed meaning on 2026-08-14 (g-115-6133) and this rider was stale
  within hours.** It used to read: "`DEFAULT_LANE` is the directive's literal
  ids (asp-335, asp-334), which is not necessarily your product vertical;
  re-run with `--lane <your own vertical>`." The default is no longer a
  literal — `lane_source` now reports `derived-from-strategic-focus` and the
  derived set already spans the verticals the old rider told you to add by
  hand (measured same day, zeta/cc-02: `[asp-326, asp-335, asp-350,
  asp-362]`). So re-running with "your own vertical" now re-measures what the
  default already covered. **READ `lane_source` FIRST and let it pick the
  second run**: `derived-from-strategic-focus` → the second run is
  `--lane asp-335,asp-334` (LEGACY_LANE, named in the script's own `--help`),
  which is what keeps your point comparable to every hand-recorded point
  before 08-14; an `explicit-flag` or literal default → the original rider
  stands and the second run is your vertical. Either way report BOTH, never
  one alone under its printed label — that half is unchanged and is the whole
  point of the rider. Measured spread on the same instant, 7d: derived
  25.4% by aspiration-id / 38.7% by work_class, legacy 19.0% / 37.3% — a
  6.4pp swing from the lane definition alone, so an inherited series point
  compared against a differently-derived one is not a trend. **(b) guard-2692 —
  never use `--series`** to reconstruct history: `lastAchievedAt` is
  last-write-wins, so retrospective windowing erases earlier recurring closes
  and its output must not be pasted into a durable node as history. Take ONE
  live point and append it; the hand-recorded points are the only real series.
  Measured 2026-08-05 (zeta, cc-02 / Linux 6.8.0-136-generic): a hand-rolled
  9-point "trend" over `completed_at` + `completed_by` read 39.3% (68/173) and
  rising, and was one edit away from being filed as a correction to self.md.
  The canonical script at the same instant read **19.1% (38/199)** default /
  **24.6% (49/199)** own-vertical — both breaching the 33.3% floor, matching
  the series' independently-recorded N=40 point (19.6%) to within 0.5pp. The
  hand-rolled series was the artifact; the series was right. Note the trap's
  shape: the bullet above was *followed*, and following it is what produced
  the wrong number.
  **(c) `ordering_ok` is a NARROW predicate — never read it as "infra
  dominates my work".** It computes exactly `lane > infra` (`share()` in that
  script), so it is silent about `other`, and `other` is where a whole product
  vertical can sit. Product work is `lane + other`. Measured 2026-08-11 (echo,
  `hostname` cc-03, `uname -r` 6.8.0-137-generic), 7d: lane 76, infra 78,
  other 40 — `ordering_ok: False` on a window where product (116) exceeds
  infra (78) by half again. Reporting that boolean as pain would have been
  false in the direction that matters. The script's own `share()` comment
  documents this (it was measured on foxtrot 2026-08-04 and the label, not the
  predicate, was fixed) — but nothing in THIS phase said to read `other`, so a
  reader following these riders alone hits the trap the script already knows
  about. Report the four numbers, not the boolean: a genuine infra tilt looks
  like `infra > lane + other` (my 48h: infra 41 vs 30), which is a different
  and much rarer condition than `ordering_ok: False`.
- **Asserting a MECHANISM** ("the selector does not weigh X", "no gate
  covers Y") → GREP FOR IT first, in `core/scripts/`.

Measured 2026-07-28 (alpha): a Lane 7 finding of "nine consecutive framework
closes against a standing product directive" was about to be filed as
MATERIAL. The window was actually 31 asp-115 / 30 asp-335 across 65 closes —
balanced; the perceived run was the last 8. The accompanying claim that the
standing directive is not scored was refuted by one grep
(`goal-selector.py` `load_strategic_focus` + `strategic_focus_boost`,
g-115-3136). Both errors pointed toward the MORE dramatic, more self-critical
finding — treat that direction as the tell, not as evidence of rigor. A
finding that flatters the narrative of vigilance earns the same probe as one
that flatters performance.

Sibling precedent: `/fresh-eyes-review` Phase 2.2b already carries this
discipline (guard-1428, plus its completed_date-vs-lastAchievedAt counting
rule) because it makes the same kind of claim. This lane had no measurement
step at all — a guard present on one scan and absent from its twin, the same
shape Phase 3 above documents (g-335-315).

Classify your answer:

- **Material** (new primary drive, role change, added/removed operating
  principle or agent-provisionable action, multi-paragraph rewrite):
  Read `agents/<agent>/self.md` — current Self content
  Edit `agents/<agent>/self.md` — update body AND front matter in the same Edit:
    last_updated: <today (YYYY-MM-DD)>
    last_update_trigger: felt-sense-material
  # Phase 2 hooks (evolution-prepare -> evolution-record) captured the Edit
  # as a self-evolution.jsonl stub with status=awaiting_completion. Finalize
  # via the canonical primitive — Phase 5 auto-posts decisions board AND
  # auto-emails user for material self edits (replaces the manual
  # forged-skill invocation that prior versions of this lane used; see bible
  # §2.4.3 — 4 mirror sites collapsed in Phase 7b).
  Bash: bash core/scripts/evolution-complete.sh \
      --revision-id <stub-rev-from-self-evolution.jsonl> \
      --reasoning "<>=80-char rationale citing the felt-sense Material finding and the Self drift it addresses>" \
      --signal-source felt-sense-material \
      --signal-evidence '[{"type":"felt_sense_finding","id":"<finding-summary-or-id>","outcome":"applied"}]'
  Log: "SELF EVOLUTION (material via felt-sense, audited): {summary}"

- **Cosmetic** (typo, wording, formatting): journal only via
  `journal-add.sh`. No user ping.

- **Nothing surfaces**: that's a valid answer. Write a one-line journal
  entry: "Felt-sense Lane 7 empty this sweep — pipeline feels right."
  If 3+ consecutive sweeps return empty, consider raising the cadence
  (bump `felt_sense.goal_cadence` upward).

## Phase 8: Record the Tick

Update the WM slot so the next cadence check fires at the right moment,
write the report, and journal the sweep.

**Critical invariant**: the stamp write is LOAD-BEARING. The cadence gate
reads `last_felt_sense_checkin` to decide whether to fire again. If this
step silently fails, the gate re-fires every iteration. Use the
`fresh-eyes-record-tick.sh` wrapper (NOT raw `printf | wm-set.sh`) — the
wrapper has `set -euo pipefail`, writes the slot atomically, AND verifies
the slot is non-null after the write (exits 1 on silent write failure).

Same failure-mode lesson as fresh-eyes-review and fresh-eyes-program (see
g-240-60 incident family in fresh-eyes-program SKILL.md Phase 8). The
2026-04-23 → 2026-04-25 felt-sense window produced 4 archived reports,
each claiming "first-fire last=0" — confirming the raw `printf | wm-set.sh`
path was silently dropping ticks. g-001-189 reproduces and documents.

```
Bash: bash core/scripts/fresh-eyes-record-tick.sh last_felt_sense_checkin
Bash: mkdir -p agents/<agent>/temp/drained
Write: agents/<agent>/temp/drained/felt-sense-<YYYY-MM-DDTHH-MM-SS>.md  # full 7-lane summary — written STRAIGHT to the drained/ archive (g-115-1838): the 7 lanes already wrote all durable value to the 6 stores + Self DURING the sweep, so this summary is archival-by-design and must NOT enter the /drain-temp queue as already-encoded slush
  # TIMESTAMP, not date-only. Use `date +%Y-%m-%dT%H-%M-%S` (hyphens, not colons —
  # Windows filesystem compatibility). The cadence is goal-COUNT based, not daily,
  # so a productive day crosses it more than once and the later sweep's Write
  # silently CLOBBERS the earlier sweep's report. Observed 2026-07-26, which
  # carried THREE fires: 02:38 (diff=146 catch-up), 13:17 (diff=77), and 14:42
  # (diff=75). Only the Write tool's read-before-write guard caught it.
  # /fresh-eyes-review Phase 4 already carries this exact fix ("Timestamp includes
  # HH-MM-SS so multiple same-day invocations do not collide"), and
  # felt-sense-2026-07-19T18-36-00.md already used the form — so the convention
  # existed and Phase 8 simply never adopted it.
  # Worth keeping the same-day pair: it is exactly when the two reports are most
  # worth comparing. The 14:42 sweep's Lane 7 finding was a RECURRENCE of the
  # 13:17 sweep's, and that recurrence was the signal a single report cannot carry.
  # (g-115-3320. Found and fixed independently by two agents within hours — the
  # merge conflict between the two fixes is what surfaced the 07-19 precedent.)
Bash: cat <<'JSON' | bash core/scripts/journal-add.sh
  {"journal_file": "{agent}/journal/YYYY/MM/YYYY-MM-DD.md", "key_events": [...], "tags": [...]}
  JSON
  # NOTE: journal-add.sh actual API is stdin-JSON only — no --kind / --summary
  # flags, AND no positional either. This line read `journal-add.sh stdin JSON {...}`
  # until 2026-08-11 (g-115-5880 sweep): the script REFUSES a bare `stdin` argument
  # ("'stdin' is not a CLI flag for this script"), so every sweep that copied the
  # call line burned a failed call before falling back to the plain pipe. The
  # correcting NOTE directly below it was already there and already right — a
  # correct comment beside an incorrect executable line loses, every time, because
  # the executable line is what gets copied. Confirm with `journal-add.sh --help`:
  # "the record is JSON on STDIN; there are NO field flags." The .md narrative file is written separately (see journal.md
  # convention). This skill writes both: the 7-lane summary at
  # agents/<agent>/temp/drained/felt-sense-*.md AND the index entry via journal-add.sh.
  # journal_file is AGENT-RELATIVE: the bound agent's name followed by
  # `/journal/YYYY/MM/YYYY-MM-DD.md`, carrying NO `agents/` prefix. The `{agent}`
  # placeholder form matches core/config/conventions/journal.md, which states the
  # same contract; the angle-bracket form is deliberately avoided here because
  # check-no-bare-agent-prefix.sh Class 1 has no backtick exemption (Class 2 does)
  # and reads it as a filesystem path reference — which this field VALUE is not.
  # That asymmetry is a real gate gap, filed as g-115-3496; this wording is the
  # local workaround, not the fix.
  # The daemon validator (mind_api/src/store_registry.py STORE_REGISTRY["journal"]
  # + _journal_validate) is ground truth, NOT this doc — rb-130 / g-001-53.
  # This line carried the `agents/` prefix until 2026-07-27 (g-335-311
  # felt-sense), the same drift already fixed in core/config/conventions/journal.md
  # and guarded by verify-learning Section JDV.
  # Measured while writing this sweep's own entry: an input of bare
  # `journal/YYYY/...` is ACCEPTED and NORMALIZED — it stored as
  # `zeta/journal/2026/07/2026-07-27.md`. So the bare form is not an error, but
  # the canonical stored shape carries the agent name; write it explicitly rather
  # than relying on normalization.
```

## Relationship to Existing Mechanisms

| Mechanism | Scope | Trigger | User-facing? | Writes directly? |
|-----------|-------|---------|--------------|------------------|
| `sq-012` | Single-outcome self-purpose | Post-goal | Only on material change | Yes (Self) |
| `strategic-scan` S3b | Category coverage | Autonomous (5 goals / 4h) | No | Yes (aspirations) |
| `/fresh-eyes-review` | Self + portfolio | Cadence (25 goals) | No — local audit | No — autonomous |
| `/priority-review` | Portfolio ranking | User pull | Yes, pull-only | Yes (priorities) |
| `/felt-sense-checkin` | **7-lane self-audit** | **Cadence (75 goals)** | **Notify only on material Self change** | **Yes — 6 stores + Self** |

Felt-sense is the structured sweep that writes. The others either ask,
observe narrowly, or cover a single slice. None of them run the full
"hygiene + completions + unblocks + backlog + verify + meta + felt"
check as one atomic pass.

## Chaining

- **Called by**: User (`/felt-sense-checkin`), `/aspirations-precheck`
  Phase 0.5f (`/felt-sense-checkin --cadence`)
- **Calls**: `felt-sense-cadence-check.sh`, `insights-read.sh`
  (default + `--count` + `--mark-processed` — Phase 1b curation pass),
  `aspirations-query.sh`, `aspirations-update-goal.sh`,
  `aspirations-add-goal.sh`, `reasoning-bank-add.sh`,
  `guardrails-add.sh`, `/tree add`, `experience-add.sh`, `meta-set.sh`,
  `/notify-user` (via canonical phrasing), `journal-add.sh`, `wm-set.sh`
- **Reads**: `agents/<agent>/self.md`, `core/config/verification-checklist.md`,
  meta strategy files, `agents/<agent>/session/working-memory.yaml`,
  world aspirations
- **Modifies**: `world/knowledge/tree/` (new/edited nodes),
  `world/reasoning-bank.jsonl` (appends), `world/guardrails.jsonl`
  (appends), `world/aspirations.jsonl` (status flips, new goals),
  `agents/<agent>/self.md` (material Lane 7 findings),
  `agents/<agent>/experience.jsonl` (appends),
  `agents/<agent>/insights.jsonl` (bulk `--mark-processed` in Phase 1b —
  flips `processed: true` for all unprocessed entries so the `/prime`
  Phase 4 surface resets),
  `agents/<agent>/temp/drained/felt-sense-*.md` (archival summary — value already
  written to the 6 stores during the sweep; never enters the drain queue),
  `agents/<agent>/journal.jsonl` (append),
  `agents/<agent>/session/working-memory.yaml` (last_felt_sense_checkin slot),
  `meta/*.yaml` (tuning edits), `core/config/verification-checklist.md`
  (new checks), email outbound (only on material Self change)
- **Does NOT modify**: aspiration priorities (that's /priority-review)

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool
call, not text. The terminal action is the Phase 8 `journal-add.sh`
call. Never end this skill with a text summary — the summary is in
the report archive and the journal entry, the agent's job is to record
the tick and return control.
