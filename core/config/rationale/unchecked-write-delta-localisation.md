# Rationale: localising a ratchet delta by revision diff

Referenced from `core/scripts/unchecked-write-audit.py` (`--new-since`) and
`core/config/conventions/audit-baselines.md` ("Localising a regression"). It explains why
the unchecked-write ratchet names the sites behind a regression by rebuilding an old
corpus from git, and why nothing is persisted beside its baseline.

## The failure it answers

The ratchet stores a count. When the count grew, its REGRESSED line told the operator to
run `unchecked-write-audit.sh --list-unverified 20`, which prints the first 20 of several
hundred unverified sites in file order. The site that joined is among them only if it
happens to sort early, so the instruction could be followed to the letter and still leave
the regression unnamed. A regression nobody can chase stays: each box that re-reads the same
+2 can record the count and nothing else.

## Why nothing is persisted beside the baseline

The obvious fix is a member set stored next to the scalar, so the next run can diff against
it. The file is merge-protected, and the merge handler has a rule for four fields only:
`baseline` is the MIN of the two sides, `history` is a content-union, `last_recorded` is the
later and `last_verdict` is the later side's. Every other key is taken WHOLE from the side
whose canonical JSON sorts higher, and `baseline` sorts first in it. Measured by running the
real handler on two entries with baselines 444 and 446, each carrying its own member list:
the merged baseline was 444, from the first entry, and the merged member list was the
second's. The merged entry described two different readings, and swapping the argument order
changed nothing. A static key with differing values behaved the same way, with the
higher-baseline side's value winning.

A member set would therefore need a rule of its own, and neither obvious rule works: a union
would never shrink, so a fixed site would stay listed forever, and an intersection would
drop every site only one box has seen. That rule would also have to reach every box before
any box wrote the new field, which is the rollout-order trap that already applies to a
change in the measuring code.

A snapshot file beside the baselines has the same problem in a second store that needs its
own handler. Listing the most recently modified unverified sites stores nothing, but it
cannot name the two ways this ratchet has actually moved: a wrapper that gained a mutating
call pulls its existing callers into the population (no skill file changed at all), and an
edit to a neighbouring line removes the credit a site had (the file changed, the call line
did not).

## Why a revision diff

Git already holds every corpus the count was ever taken over, and the audit's inputs are
tracked, so they are identical on every box at a given revision. The baseline is a fleet
minimum, so the reading it names may have come from another box; the revision identifies it
all the same. `--new-since` rebuilds the old inputs with `git archive` (the checkout is not
touched) and runs the CURRENT matcher over both corpora, which leaves the corpus as the only
variable. A matcher change shows up too, but in the open: the report prints what the old
tree reads beside the recorded baseline and says so when they differ.

Records are keyed by (file, wrapper, line text) and counted per key. Inserting lines above a
site moves every line number and changes nothing else, so it is not a change. Identical
lines are counted rather than collapsed and are matched by order, so the one that flipped is
named; if that does not reproduce the count, every unverified twin is listed instead, which
is wide but never wrong. Both lists are printed, because a net +2 may be +5 and -3, and
joined minus left always equals the change in `unverified`. A tree that rebuilds as empty is
refused: an empty "before" would show every site as new and an empty "after" every site as
fixed. The regression it was built against, two sites losing the credit that a nearby branch
header had given them (444 to 446), comes back as exactly those two sites in about a second.

`--new-since baseline` finds the old revision from the history already in the file: the
newest retained row whose count equals the baseline, mapped to the last commit at or before
its timestamp. Row stamps are naive UTC and are handed to git as UTC.

## What it does not do

- It is not exact about the revision. The commit is matched by date, so the report prints
  the commit it chose, and `--new-since <sha>` overrides it.
- History is capped at 50 rows and every run appends one, regressed runs included. A
  regression left unfixed long enough pushes the last row at the baseline out of the window;
  `baseline` then fails with a message saying so instead of guessing, and a named revision
  still works.
- A shallow or pruned clone cannot rebuild an old revision. That is a visible failure too.
- It reads the baselines file and writes nothing, so it is safe from any checkout. The
  ratchet is not: it appends history, so run it only where the reading should be recorded.
- It does not change what is counted. On one corpus the default census output was
  byte-identical before and after the change that added this mode, which matters because a
  measuring change that lands on one box first moves the shared floor under the others.

## Cross-references

- `core/scripts/coordination_merge.py` `merge_audit_baselines` — the merge rules above.
- `core/config/conventions/audit-baselines.md` — schema, verdicts, merge and localisation.
- `core/scripts/tests/test_unchecked_write_audit.py` — pins each claim about the delta.
