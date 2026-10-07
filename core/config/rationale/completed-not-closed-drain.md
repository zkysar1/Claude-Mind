# Rationale: The Completed-Not-Closed Drain

Referenced from `.claude/skills/aspirations-precheck/SKILL.md` Phase 0.5g.7. Why the
drain's CLOSE runs through `iteration-close.sh --phase verify --drain`, why the drainer
writes the evidence rows, and the history of the `--hold` ledger.

## Why the drain closes through verify (g-375-146)

Until g-375-146 the drain closed a row with `aspirations-complete-by.sh --key-finding`
plus an `outcome_class` write. complete-by runs no gate, so every check in `do_verify`
(the domain-suite gate, the closure-evidence gate, close-review checks A and B, the
candidate refusal, the uncommitted-work gate on the status write) skipped every drained
close. Measured 2026-10-06 on hostname cc-14 (rb-12914's method, the gate-firing log in
the 60 s before each close): the cc-04 reducer's closes of goals other sessions executed
carried zero do_verify firings. A close-review gate switched on for the minds' own
sessions would therefore have left the drain as a way around it, so the drain now closes
the way every other close does, and `verify` stays the only writer of a close
(guard-7587).

`do_verify` was written for the session that ran the unit. With `--drain` the steps that
assume so take a drain branch, and every gate still runs:

- **Checkpoint writes** are skipped. The checkpoint is the drainer's own iteration's,
  anchored to its own goal or absent mid-precheck, so the writes were refused with a WARN
  prescribing `init --goal-id <drained goal>`, which would re-anchor the drainer to a goal
  it never ran.
- **The uncommitted-work gate** reads this box's working tree, which is the drainer's
  work, not the unit's. The auto-override says so, for both outcomes. Its audit row is
  written only when something is dirty.
- **The domain-suite gate** counts only the claim's sessions as the unit's (`--drain`).
  Counting the closer charged the drainer's own recent domain writes to the drained close
  and ran the suite for them. Those writes are checked at the closes of the drainer's own
  units.
- **The NEXT line** says to run no spark, state-update or learning-gate phase for the
  goal. The reducer's imperative would have run state-update for a goal the drainer never
  executed.
- **A refusal** prints the drain's remedy, not a retry or a revert, when the live
  status shows the write did not land. A write that landed is a close, even when an
  interruption ended the call after it, and an unreadable status is probed first.
  A malformed call (rc 2) gets the corrected retry.
- **The key finding** lands on the record and as the team-state `recent_completions`
  row, as complete-by's did. No state-update phase runs for a drained goal, and the
  goal-duplication gate matches new filings against those rows.

## Why the drainer writes the evidence rows

The closure-evidence gate (g-375-05) refuses a completed close unless the note carries
one `OUTCOME <n>: MET — <value>. Source: ...` row per verification outcome. Census of
the drain population in the world queue, 2026-10-06 on hostname cc-14, with each gate's
verdict function called directly (no gate firing logged):

| Population | Closure evidence | Check B | Close-risk tier |
|---|---|---|---|
| 99 rows | 75 refused (69 have no evidence table), 23 have no outcomes, 1 passes | 0 refused | 87 tier 2, 12 tier 1 |

Most backlog notes predate the table. The drain's own rule already asks the drainer
to judge each outcome on the evidence the note cites, so the drainer writes that
judgment down: the rows go on a copy of the note, passed as `--outcome-note-file`.
The rows must cite a source that resolves from the drainer's box. An outcome it cannot
source from there is a HOLD, because evidence only a dead Body holds cannot be reviewed
(guard-7485).

## Why a refusal is counted, and held

A refused close leaves the row open. The conservation line names it, with its gate, so
`consumed = closed + released + held + refused + skipped-moved` still adds up. A refused
row is held, so the next iteration does not meet the same gate at once. The exception is
a CLOSE REVIEW refusal: the gate has already requested the review, and the slate's review
hold keeps the row out until a verdict answers. An extra 24 h hold would only delay the
re-offer.

Once check A is on, each tier-2 drained close needs a peer verdict like any other. With
87 tier-2 rows in the 2026-10-06 backlog, switching check A on before the backlog drains
would queue them all for review at once.

## History: whose ledger a hold writes

The PEER LEG paragraph once said a peer's `--hold` writes to THAT peer's ledger and that
the hold belongs to the goal's holder. That was never true of the command it prescribed.
That `--hold` carries no `--agent`, so it always landed in the acting agent's ledger,
while the peer slate read the peer's. The hold suppressed nothing on any lane but your
own, silently, while still incrementing hold_count toward the third-hold Investigate
escalation (fixed by g-115-6494).

Passing `--agent` to `--hold` no longer changes the ledger. On the `(unattributed)` lane
it used to create `agents/(unattributed)/session/`, a directory for a bucket key that is
not an agent (path-resolution.md L1 cruft).

## Cross-references

- guard-7587: verify is the only writer of a close
- rb-12914: telling from the gate-firing log whether a close ran do_verify
- `core/config/rationale/domain-suite-gate-own-writes.md`: the own-writes narrowing the
  drained close's session set feeds
- `core/scripts/completed-not-closed-slate.py`: the population, the holds, the review hold
- `.claude/skills/aspirations-precheck/SKILL.md` Phase 0.5g.7: the consumer
