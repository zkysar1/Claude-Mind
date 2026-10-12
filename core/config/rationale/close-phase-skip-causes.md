# Rationale: Close-Phase Skip — the cause catalog and why it became a classifier

Referenced from `core/scripts/close_phase_skip.py` (`render`, `classify_close_path`).
Holds the measured catalog of close paths that leave a goal uncounted, extracted
from `render()`'s emitted text where it had grown to five numbered causes and
~4,000 characters.

## Why this is a file and not a longer message

`close-phase-skip-check` reports goals closed this session whose
`--phase state-update` never ran. Several close paths do that **legitimately**,
and each time one was discovered the response was to append another numbered
caveat to the emitted string. Two of those five ended by instructing the reader
to confirm the shape and open nothing — a detector telling its own reader to
ignore it.

guard-4649: *a caveat in a detector's output is not a filter on the output.*
Writing the warning into the emitted text does not reduce firings; every reader
still paid the discrimination cost by hand. Measured on one iteration (alpha,
cc-04): an 11.1 MB `changelog-read` grep, two diary reads, three record reads
and one falsified hypothesis — to re-derive a cause that had already been
established seven hours earlier. Two goals (g-115-9180, g-115-9182) are closed
having each delivered one more paragraph.

So the discrimination moved into `classify_close_path()` and the prose shrank.

## The cause catalog (what the classifier encodes)

| # | Shape | Changelog fingerprint | Verdict |
|---|---|---|---|
| 1 | **Autocompact resume** re-entering at the close sequence | `complete-by`, then a LONE `outcome_class` **minutes** later (g-115-4138: 15m28s) | FINDING — the incident this check was built for (victim g-326-447) |
| 2 | **Batch close** through a direct path | same record fingerprint as (1) — absent `outcome_class` + `completed_by_role` | FINDING (record cannot separate; changelog can) |
| 3 | **Bare-status close** — `aspirations-update-goal.sh <id> status completed` on an unclaimed goal | a lone `update-goal <id> status`, no `complete-by` at all | FINDING |
| 4 | **CNC-drain** (precheck Phase 0.5g.7) disposing another Body's finished-but-unbanked unit | `complete-by` then `outcome_class` **seconds** apart; claim rows belong to a DIFFERENT agent | **EXCLUDE** — the closer executed nothing |
| 5 | **Interrupted recurring close** whose RECORD LIES | a LONE `complete-by` with nothing after it | FINDING (guard-3511) |
| 6 | **Hypothesis-resolution close** | `complete-by` then `outcome_class` seconds apart; NO claim row in the goal's entire lifetime | **CREDIT** — work happened, so the absent bump is a real accounting gap |
| 7 | **Finding-disposition close at iteration open** — a precheck finding whose correct disposition IS closing the subject goal (premise cleared / moot) | **identical to (3)**: a lone `update-goal <id> status`, no `complete-by` | **EXCLUDE** in principle — the closer executed no iteration — but see below: it is NOT separable, so it reports as (3) |
| 8 | **Lock-contention retry out of band** — the close's status flip failed on a peer `aspirations.lock`, and the retry went through a bare `aspirations-complete-by.sh` instead of back through `iteration-close.sh --phase verify` | `outcome_class` **BEFORE** `complete-by` (reversed vs 1/4/6), same-agent claim present | FINDING — genuine skip; repair per g-115-8289 |
| 9 | **Claimed-and-executed bare-status close** — the goal WAS claimed and worked, but the close was typed as `aspirations-update-goal.sh <id> status completed` instead of `complete-by` / `iteration-close.sh --phase verify` | shares (3)/(7)'s fingerprint — no `complete-by` **anywhere in the goal's lifetime** — but a **same-agent `claim` row precedes the status flip**, and the record carries a substantive `outcome_note` | **CREDIT** — work happened, so the absent bump/journal/tree-drift reset is a real accounting gap. The repair is still FORBIDDEN (guard-1235) |
| 10 | **DRAIN close** — `iteration-close.sh`'s DRAIN branch (`--drain`) disposing finished-but-unbanked work; emits no `complete-by` (that call is gated to the recurring branch) but stamps its own close-time row | `update-goal key_finding` within 60s of the close triple (`status` or `status+outcome_note`, `outcome_class`), NO `complete-by` anywhere in the goal's changelog; claim rows may belong to the SAME agent | **EXCLUDE** — `--drain` is the closer's own declaration that it executed nothing, so the claim history is irrelevant for THIS shape (unlike 4/6, where it is the only separator) |
| 11 | **Resolve-pass close** — a hypothesis RESOLVER goal closed directly by the resolve pass (an iteration of an enclosing recurring goal) with a bare `complete-by`, or typed as a bare status flip; the goal was never claimed | a lone `complete-by` (or, with none, the last `status` write), a note write seconds before it, NO `claim` row from any agent, NO `outcome_class` row at all — **byte-identical to the lone direct close of causes 2/5, and to cause 3 for the status flip** — plus the pipeline's own resolution stamp (`resolved_by` = the closer, `resolved_at` at or before the close) joined on the goal's `hypothesis_id` | **EXCLUDE** — no iteration of the goal's own existed and the work is durably recorded by the resolution; the enclosing pass's state-update booked the pass |

Causes 4 and 6 produce a **byte-identical** fingerprint, which is why the
classifier keys on `sanctioned path that executed no iteration` and then splits
EXCLUDE from CREDIT on the claim history — never on drain-ness.

Causes 3, 7 and 9 are the **no-`complete-by` family**, and the classifier cannot
enter the split above for any of them: with `complete_by is None`,
`classify_close_path` reads the claim history for none of them — 3, 7 and 9 all
report as `PATH_UNCLASSIFIED` — and the family spans all three verdicts —
FINDING (3), EXCLUDE (7), CREDIT (9). Do not read a member of this family as
cause 3 because cause 3 is the row that names a verdict.

Cause 10 is also no-`complete-by`, but it alone is separable: the DRAIN branch
stamps `key_finding` at close time, and that row has exactly two writers —
`aspirations-complete-by.sh`, which always emits a `complete-by` row beside it,
and the DRAIN branch itself — so a `key_finding` row with NO `complete-by`
anywhere in the goal's changelog is path-proven as a drained close.
`classify_close_path` reads that fingerprint at the top of its no-`complete-by`
branch and returns `PATH_SANCTIONED_DISPOSAL` (EXCLUDE) regardless of the claim
history, because `--drain` is the closer's own declaration that it executed no
unit: a `--phase state-update` repair would book that unit's work to the
drainer's session. (Measured 2026-10-08 to 10-11, g-115-8295 rechecks: every
such close — including the same-agent ones, triples 3 to 13 s apart — was
reported as a lost state-update by the pre-branch classifier.)

Cause 11 is the one member of the lone-`complete-by` family (causes 2 and 5) that
a JOIN separates and the changelog cannot. `classify_close_path` takes an optional
`resolution` — the pipeline's own `{resolved_at, resolved_by}` stamp for the goal's
`hypothesis_id`, written by `_stamp_resolution_provenance`
(`mind_api/src/world/pipeline_write.py`) when the hypothesis is resolved and never
overwritten — and returns `PATH_SANCTIONED_DISPOSAL` (EXCLUDE) only when ALL of:
no `claim` row from any agent, no `outcome_class` row at all, the `complete-by` is
the closing agent's own, and the hypothesis was resolved by that same agent at or
before that `complete-by`. The CLI half reads the pipeline lazily, only for a close
whose changelog shape already matches, and fails open to a finding.

Measured 2026-10-11 (zeta, cc-02): g-115-11976 and g-115-12073, resolver goals for
two hypotheses the same pass resolved CORRECTED (pipeline stamps 02:55:08 and
03:10:57, `resolved_by` zeta), closed by `complete-by` at 02:56:50 and 03:16:23,
102 s and 326 s later. The enclosing pass is g-001-02 occurrence 81, whose
journal block is stamped 03:25 and which reads counted; the two resolver goals
read absent. EXCLUDE where cause 6 is CREDIT because the cause-6 closer ran the
close machinery (an `outcome_class` row) and lost only the last phase, so the work
is genuinely unbooked; here no close machinery ran at all, and `--phase
state-update` would book the enclosing pass twice.

The same conjunction is read for the SAME close typed as a bare status flip (no
`complete-by` row), anchored on the goal's LAST `status` write — `status`, or
`status+outcome_note` when the note rode the same locked write. Anchored on the
first one, the ordering test would fail on a goal whose promote wrote a status
days before the resolution (g-115-11976 carries exactly that row). PROVENANCE OF
THIS VARIANT: its shape is as REPORTED by foxtrot (2026-10-11: three
hypothesis-review sub-goals closed by `update-goal outcome_note` then
`update-goal status`, inside one occurrence of g-001-02); it has NOT been measured
on cc-02, whose two instances both closed by `complete-by`. The tests model it on
the measured rows of g-115-11976 with the close typed as a status flip, and say so.
The drain branch still runs first, so a drain triple is never read as this shape.

NO TIME BOUND, on purpose. The store-wide probe (every completed goal carrying a
`hypothesis_id`, against the changelog window) found only g-115-11976 and
g-115-12073 inside the window, so a bound would be a parameter fitted to n=2 and would not change the
verdict anyway: no claim and a recorded resolution already say no iteration
existed and the work is recorded. The ORDER is kept, because a close that precedes
the resolution cannot be its bookkeeping. The neighbours the discriminator must
not admit are each pinned by a test: a CLAIMED resolver goal (a real lost
iteration, same stamp), any `outcome_class` row (the cause-6 pair keeps CREDIT),
another agent's resolution or `complete-by`, a close that precedes the resolution,
and every unreadable input (missing, unresolved, unparseable, raising).

TWO LIMITS OF THE "NO CLAIM ROW" CONDITION (found by the alpha-path fresh-eyes
review of g-115-8295, 2026-10-12). It is read off a changelog that the CLI half
parses with ONE regex, so it is only as good as that parse: a claim row that carries
a trailing note (measured, `claim g-115-817 (take-back from alpha)`, 1 of 480 claim
rows in a 20000-row window) and every row of a goal id with a letter suffix (12 ids,
78 rows) are dropped silently, so a take-back-claimed resolver goal reads as
unclaimed and is excluded. The repair is g-115-12426. The condition also presumes
claim-before-execute (guard-3740, measured 2026-08-14: an agent executed a goal it
had never claimed): an unclaimed iteration of a resolver goal that lost its
state-update is excluded when the same-agent stamp precedes the close and no
`outcome_class` row exists, and the checker cannot tell that case from a
resolve-pass close.

### The no-`complete-by` family is EVERY non-recurring goal, not an exceptional shape (measured 2026-09-20, g-369-404, echo/cc-03)

Causes 3, 7 and 9 each describe the missing `complete-by` row as evidence that
the closer took an irregular path — a bare `update-goal status` typed instead of
`complete-by` / `iteration-close.sh --phase verify`. **On a non-recurring goal
that inference does not hold, because the correct path emits no `complete-by`
row either.** `iteration-close.sh` calls `aspirations-complete-by.sh` only inside
its recurring branch (`iteration-close.sh:1366-1404`, whose own comment reads
"CRITICAL — recurring goals MUST go through aspirations-complete-by.sh"), since
what `cmd_complete_by` exists to do — bump `lastAchievedAt`, `achievedCount`,
the streak pair — is meaningless for a goal that closes once. Its absence is
therefore a property of RECURRENCE, not of how the close was typed.

Measured on `g-369-404` (echo, cc-03, 2026-09-20): a fully conventional close —
`claim` 21:09:54, `update-goal status` (in-progress) 21:10:02, three
`progress_note` appends, then `iteration-close.sh --phase verify` at 21:18:54
emitting `update-goal status+outcome_note` and `outcome_class` three seconds
later. No `complete-by` row anywhere in the goal's lifetime. Every element of
cause 9's fingerprint is present — same-agent claim, no `complete-by`,
substantive `outcome_note` — yet nothing was mistyped: `--phase verify` ran, and
the ONLY phase actually skipped was `--phase state-update`, which the operator
omitted while hand-sequencing verify → spark → learning-gate →
productivity-check.

CONSEQUENCE FOR A READER OF THIS CHECK: on a non-recurring goal, `complete_by is
None` carries no information about the close path — with ONE exception: if a
`key_finding` row sits within 60 s of the close triple, that is cause 10 (the
DRAIN branch's own close-time stamp), and the check EXCLUDEs the close on that
fingerprint alone. Absent that row, `PATH_UNCLASSIFIED` is the expected verdict
rather than a signal. Discriminate on what remains — a
same-agent `claim` row plus a substantive `outcome_note` means work happened
(CREDIT), and the question worth asking is not "was the status flip typed
correctly" but "which of the five phases did not run". The repair stays
FORBIDDEN either way (guard-1235); the accounting gap on `g-369-404` was left
unrepaired for exactly that reason — counter stuck at 160, the goal absent from
`counted_goals_this_session`, no journal row, and `goals_since_last_tree_update`
not reset despite a tree write in that iteration.

Sibling shape: `guard-4957` — a detector's detection output must not resemble
its own failure signature. Here the FINDING fingerprint is byte-identical to the
correct path's, for the whole non-recurring population. `guard-5782` is the
other half of the same split (a RECURRING goal closed through `--phase verify`
is the error this one is not).

Measured instance of 9: `g-115-10311` (bravo, cc-05, 2026-09-20) — `claim`
14:43:13, `update-goal status` 14:54:43, `outcome_class` 14:54:46,
`outcome_note` 14:54:49, and no `complete-by` row in the goal's entire lifetime
(positive control: 24,511 goal-id-bearing rows in the 24.1 MB changelog read).
Its `outcome_note` is a 1,575-char detector diagnosis, which is what settles
CREDIT over EXCLUDE — see the claim-row caveat below.

## guard-2523's REPAIR defeats guard-2523's DETECT

Cause 8 is the one shape that **erases its own evidence**, and the eraser is a
guardrail's own prescribed remedy — so it is worth stating separately from the row.

guard-2523 DETECTs a skipped verify as *null `outcome_class` on a completed
non-recurring goal*, and REPAIRs it with a hand-written
`aspirations-update-goal.sh <g> outcome_class <deep|routine>`. Once that repair has
been applied, the goal presents as a **healthy close** to that same detector: the
field it keys on is now populated, by hand, and nothing on the record says by whom.
The repair is correct and still worth doing — but it buys the field at the cost of
the signal, so after it there is no record-derived way to tell a repaired skip from
a close that never skipped.

Only the changelog ORDER survives it. A close that ran writes `complete-by` first
and `outcome_class` seconds later (rows 1/4/6); a repaired skip can write
`outcome_class` **first**. Measured: `g-115-10132` (bravo, cc-05, 2026-09-18) —
`outcome_class` 12:00:24, `complete-by` 12:01:45, 81s apart, same-agent claim
throughout, `--phase state-update` never run. Two `complete-by` attempts had already
failed on a peer lock (`write_failed: Could not acquire lock`), and the retry was
issued as a bare `complete-by` rather than re-entering `iteration-close.sh --phase
verify` — which is precisely what guard-2523 forbids, committed by an agent that
had the guardrail's own repair recipe in hand.

**Practical consequence for a triager:** do not read a populated `outcome_class` as
proof the close sequence ran. It is proof only that *something* wrote the field.
And for a closer: when a status flip fails on lock contention, retry through
`iteration-close.sh --phase verify`, never through the bare script — the lock is
transient, the dropped state-update leg is not.

Cause 7 is the harder one, and it is listed to be *recognised by a reader*, not
classified: it shares cause 3's fingerprint exactly. A bare-status close is a
bare-status close whether an LLM typed it to dispose a precheck finding or typed
it to shortcut a real execution, and no changelog row separates the two — the
lane lives only in the closer's intent. Cause 4 escapes this because its closer
is a SCRIPT with a distinguishable claim history; cause 7's closer is the
orchestrator's own hand. So do not add a discriminator for it: by the section
below, a content-derived one will fail, and there is no changelog-derived one to
find. Confirm the shape from the goal's `outcome_note`, which on this lane states
the disposition verbatim, and move on.

One qualification, added with cause 9 (2026-09-20, g-369-404) so the sentence above is not
read as stronger than it is: the **claim row is a changelog-derived separator in
ONE direction only.** Its ABSENCE is decisive — a closer that never held the goal
executed no iteration, which rules cause 9 out and leaves 3/7. Its PRESENCE
decides nothing, because a same-agent claim cannot separate "executed and typed
the wrong close" (9) from "claimed, found the premise moot, disposed it" (7) —
the identical asymmetry `classify_close_path` already documents on its sanctioned
branch. So the claim row narrows the family; it never closes it, and the
`outcome_note` read prescribed above remains the deciding one. What that note
settles is not the shape but the VERDICT: a disposition note states a premise
cleared, an execution note carries the work.

**Do not "repair" a cause-7 row with `--phase state-update`** — that is the
g-115-8289 remedy for a genuine skip, and applying it to a close that executed no
iteration corrupts the session counters it is meant to fix. Measured instance:
`g-326-880`, closed bravo/cc-05 2026-09-11T18:02 while disposing a
user-blocker-escalation finding, reported by this check the next morning as the
single unattributed close of five.

## Why the record cannot do this, measured

Four record-derived discriminators were measured to failure across three boxes:

- `key_finding` present — 2 of 10, and one of those was not a drain. It separates
  hand-invoked `complete-by` from the iteration-close one, not the lane.
- no live claim at close time — `claimed_by` was None on **all ten**, including
  both genuine-loss candidates. Zero discriminating power.
- `completed_by` != `executed_by` — does not separate.
- `Maintain:` title prefix — 40 of 83 completed `Maintain:` goals carry
  `outcome_class`, i.e. ran a full close. Excluding the prefix would suppress
  ~40 genuine losses to save 1 false alarm.

They fail for one structural reason: each describes what the goal **was about**
or **who touched it**, while sanctioned-vs-lost is a fact about **how it was
closed** — which lane called which script. Only the changelog records that. A
fifth content-derived candidate will fail the same way.

The durable fix is for each closing path to **stamp itself at close time**; the
classifier infers it afterward because no such stamp exists yet. Cause 10 is
the one path that has one: its `key_finding` row IS the close-time stamp, and
`classify_close_path` reads it directly (`_drain_fingerprint`) — which is why
cause 10 is the one member of the no-`complete-by` family that is separable
instead of reported as `PATH_UNCLASSIFIED`.

## Why the gap threshold is 60s

Measured sanctioned gaps: 2, 4, 4, 5, 6, 10, 10, 22 seconds (n=8, four boxes).
The resume shape it must not absorb: 15m28s. `SANCTIONED_GAP_SECONDS = 60` sits
2.7x above the largest observed sanctioned gap and ~15x below the resume gap.

Under-matching is the safe direction and the threshold is chosen for it: a
wrongly-suppressed real loss is invisible, one extra reported row costs a reader
thirty seconds. Every ambiguous input — unparseable stamp, a row interposed
between the pair, a same-agent claim — returns `PATH_UNCLASSIFIED` and stays a
finding. The no-`complete-by` drain branch follows the same posture: a partial
triple, a stray row inside the close cluster, a cluster gap beyond the bound, or
an unparseable `key_finding` stamp breaks the fingerprint and the close stays a
finding.

## Rate, with the populations separated

`--limit` bounds the window and the drain skews OLD, so the default under-reports
this lane ~4x (`1 of 5` became `4 of 17` on one session). Size it with an
explicit `--limit`.

Measured no-state-update rates: 10 of 91 (11.0%, cc-05) replicating 10 of 92
(10.9%, two days earlier); 9 of 130 (6.9%, cc-02). The **sanctioned share
inverts between boxes** — cc-05 read 8 of 10 sanctioned (true loss 2.2%), cc-02
read 3 of 9 (true loss 3.8-4.6%). Neither ratio is the fleet's, and "mostly
false alarms" must not be inherited as a prior: on cc-02 the check's output was
majority true positive.

Reading 2026-10-12 (zeta, cc-02, Linux 6.8.0-142-generic), one session, the 32
closes its `completed_by_sid` names (`--limit 60` covers all of them; they span
2026-09-28 to 2026-10-12 by close time — the record's `completed_at`, which only 11
of the 32 carry, else the changelog's closing row): **4 of 32 uncounted
(12.5%)**. By cause: 2 resolve-pass closes (cause 11, g-115-11976 and g-115-12073 —
EXCLUDED by the classifier); 2 closes nested inside another goal's iteration (next
section, still reported). Lost iterations — a goal whose OWN
iteration skipped state-update: **0 of the 4**. So the not-lost share on cc-02
reads 4 of 4 here (2 of them classified by code, 2 only by forensics) against 3 of 9
sanctioned on the earlier cc-02 reading: the ratio follows the session's mix of work
(this one ran a hypothesis review and an Unblock chain), and neither number is a
prior for the next.

## Closes nested inside another goal's iteration (residue; not separable today)

Two of the four uncounted closes in that reading were executed INSIDE another goal's
counted iteration and are still reported: g-374-656 (an Unblock child, closed by a
bare `complete-by` at 04:03:18 inside g-374-140's iteration — claimed 03:03:12,
closed 04:19:05, counted) and g-115-5442 (measured 2026-10-08: closed inside
g-115-11112, whose own state-update ran). Each is one counter unit and one journal
block short, and neither is owed: `--phase state-update` on the nested goal would
book the enclosing iteration twice.

Why the classifier cannot separate them from the changelog today (measured
2026-10-12 over the same 32 closes): the strict window test — the closer claimed an
enclosing goal Y before X's close, Y is counted, Y closes within 60 minutes after X —
also matches 4 of the 28 COUNTED closes (g-115-5633, g-115-12203, g-115-1538,
g-326-516), a 14% coincidence rate, because long-held claims overlap. The
conjunction that does separate this session is "X has no claim row of its own" (each
of those four has 1 to 5), and it covers g-374-656. It does NOT cover an enclosure by
a goal in the AGENT store (g-001-02, the cause-11 enclosure): no row for it appears
in the world changelog read. Adopting the partial test would also leave the 60-minute
bound as a parameter fitted to the cases in view.

The durable fix is the one named under "Why the record cannot do this": stamp at
close time. `aspirations-complete-by` can read the iteration anchor
(`iteration-checkpoint.json`, written at claim, removed by iteration-close.sh at the
end of productivity-check) and, when it names a DIFFERENT goal, record that goal on
the close. Only that code path would write the stamp, so it is path-proven in the
way `key_finding` is for cause 10, and the classifier would join it with the
enclosing goal's membership — no time bound. It is a daemon-endpoint change
(`mind_api/src/endpoints/aspirations_write.py`, the complete-by unit), which is why
it is not part of the classifier unit above. Design hazards: the anchor is
body-keyed, and a stale one survives an autocompact that lands between phases.

## Cross-references

- guard-4649 — a caveat in a detector's output is not a filter on the output
- guard-3511 — repair procedure for cause 5; its step (a) reads the record,
  which is exactly what misleads there
- guard-2523 — NOT violated by cause 4 (its DETECT is null `outcome_class` +
  stale `in_flight` + unreleased claim)
- guard-2660 — the INVERSE shape (a refused close: siblings land, status does not)
- `core/scripts/close_phase_skip.py` — `classify_close_path()`, the classifier;
  `_drain_fingerprint()` (cause 10) and `_resolve_pass_close()` (cause 11)
- `mind_api/src/world/pipeline_write.py` — `_stamp_resolution_provenance`, the
  resolution stamp cause 11 joins
- guard-2898 — a carve-out's stated reason must visibly exclude its neighbours, and
  each neighbour is pinned by a test (cause 11)
- g-115-8289 — established `--phase state-update` as the repair for a genuine
  skip. Applying it to an EXCLUDE-class close corrupts counters.
