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

Causes 4 and 6 produce a **byte-identical** fingerprint, which is why the
classifier keys on `sanctioned path that executed no iteration` and then splits
EXCLUDE from CREDIT on the claim history — never on drain-ness.

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
classifier infers it afterward because no such stamp exists yet.

## Why the gap threshold is 60s

Measured sanctioned gaps: 2, 4, 4, 5, 6, 10, 10, 22 seconds (n=8, four boxes).
The resume shape it must not absorb: 15m28s. `SANCTIONED_GAP_SECONDS = 60` sits
2.7x above the largest observed sanctioned gap and ~15x below the resume gap.

Under-matching is the safe direction and the threshold is chosen for it: a
wrongly-suppressed real loss is invisible, one extra reported row costs a reader
thirty seconds. Every ambiguous input — unparseable stamp, missing
`complete-by`, a row interposed between the pair, a same-agent claim — returns
`PATH_UNCLASSIFIED` and stays a finding.

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

## Cross-references

- guard-4649 — a caveat in a detector's output is not a filter on the output
- guard-3511 — repair procedure for cause 5; its step (a) reads the record,
  which is exactly what misleads there
- guard-2523 — NOT violated by cause 4 (its DETECT is null `outcome_class` +
  stale `in_flight` + unreleased claim)
- guard-2660 — the INVERSE shape (a refused close: siblings land, status does not)
- `core/scripts/close_phase_skip.py` — `classify_close_path()`, the classifier
- g-115-8289 — established `--phase state-update` as the repair for a genuine
  skip. Applying it to an EXCLUDE-class close corrupts counters.
