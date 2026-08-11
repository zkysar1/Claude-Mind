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
  Two non-negotiable riders. **(a) guard-1944 — run it TWICE**: `DEFAULT_LANE`
  is the directive's literal ids (asp-335, asp-334), which is not necessarily
  your product vertical; re-run with `--lane <your own vertical>` and report
  BOTH, never the default alone under its printed label. **(b) guard-2692 —
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
Bash: journal-add.sh stdin JSON {journal_file: "{agent}/journal/YYYY/MM/YYYY-MM-DD.md", key_events: [...], tags: [...]}
  # NOTE: journal-add.sh actual API is stdin-JSON only — no --kind / --summary
  # flags. The .md narrative file is written separately (see journal.md
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
