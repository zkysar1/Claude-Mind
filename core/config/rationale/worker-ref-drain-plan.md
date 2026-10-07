# Rationale: the drain plan (`worker-ref-consume.sh --drain`)

Referenced from `core/scripts/worker_ref_drain.py` and the header of
`core/scripts/worker-ref-consume.sh`. Why the first slice of the one-invocation
carrier drain (g-306-506, from the g-306-284 occurrences) is a read-only PLAN
with a chained preview, and which measured facts shaped it.

## Why a plan first

The hand drain is a fixed sequence (pull, report, phantom test, merge in order,
ledger check, verify, push, retire, fresh re-read) and it dominates reducer
time: 8 of one session's last 10 closes, 12 to 15 minutes each, 31 minutes for
six tips. The errors recorded across its occurrences were ORDERING errors, not
judgment errors: a report read before the pull (stale base, guard-6051), a
verification that ran after the merge commit had already deployed daemon code
(rb-12102), a retire of a live Body (guard-3660). A plan removes the ordering
decisions and takes no judgment away, and it can be built and trusted before any
step that moves a ref exists. The apply half reuses its verdicts and stops where
it stops. Until it lands, merge, verify, push and retire stay the reader's.

## Why the preview is chained

Two tips that touch one file can conflict only after the first has merged, so a
dry run of each tip against HEAD alone passes both (g-306-284 occ255). The plan
previews tip N against an unreferenced commit that stands in for merging the
earlier MERGE tips (parents: the previous base and the tip, as the real merge
has them; occ303's recipe). `test_chained_preview_finds_a_conflict_that_exists_only_after_the_earlier_tip`
carries the control: the same second tip measured against HEAD is clean. STOP and
CARRY tips are left out of the chain, so the plan assumes they stay unmerged.

## Why every read has an UNMEASURED face

A read that failed must never read as a clean one (guard-2298). Measured on git
2.43.0 (alpha, cc-08, 2026-10-05): `git merge-tree --write-tree` exits 1 for a
bad ref with NOTHING on stdout, the same rc a conflict returns, and 128 for
unrelated histories. So a conflict is claimed only when stdout opens with a tree
id. The same rule runs through the rest: a binary numstat row is None, never 0; a
ledger path past the audit cap is UNMEASURED; an unreadable `git status` or
re-track field is a STOP; an unreadable surface detector is MERGE-VERIFY-FIRST,
the safe direction (an extra verification costs minutes, an unverified deploy of
daemon code does not).

## Why merge-tree is trusted for ledger paths

`--write-tree` runs the repository's custom merge drivers: a fixture with
`*.drv merge=<driver>` returned the driver's output for that path and the plain
text merge for a control path, with the index, work tree and MERGE_HEAD untouched
(git 2.43.0, 2026-10-05). So the preview tree is what the real merge writes, and
the by-record audit over it audits the real result. rb-6101 reads "merge-tree
does NOT invoke custom merge drivers"; it was measured with the legacy
three-argument form, which does not, and it does not describe `--write-tree`.

## Why the report supplies the selection and the re-track count

TIP selection, carry state and supersession are the report's, and re-deriving
them here would be a second copy that drifts (guard-2676). The one input it did
not export was the re-track count, whose ignore test has a trap (`check-ignore -v`
also lists a path a later `!pattern` re-includes), so it gained one field,
`merge_retrack_real`, appended last under the report's -1 = UNMEASURED
convention. `FRAMEWORK_RE` is the one predicate duplicated in code; a test pins
it to the shell's.

## Why every reason is listed and the strongest decides

An AND/OR probe that reports only its first failing condition hides the shape of
the problem (guard-3644). A tip with a dirty overlap AND a daemon-surface hit
needs both fixes, so both print; the verdict is the strongest of them
(STOP > CARRY > MERGE-VERIFY-FIRST > MERGE). A conflict is judged before a
no-change preview (guard-6648).

## Why the retire candidates carry the retire gate's verdict

Reachability from origin/main answers whether the CONTENT of a ref is delivered, and says
nothing about whether a running Body still pushes to the HANDLE. Measured on the live
fleet 2026-10-06: 9 outstanding refs were reachable, so the plan listed 9 retire
candidates, and every one belonged to a Body whose heartbeat carrier was fresh. A plan
that lists them unqualified sends the reducer to `--retire` nine times to be refused nine
times, and a plan that hid the refusal would invite the `--force-retire-live` the gate
exists to make rare. So each candidate prints the gate's one line. The plan reads the gate
WITHOUT its origin reads (it stays a local, read-only plan), `--retire` re-reads
everything inside its own call and stays the authority (guard-5952), and a gate that
cannot run reads CARRY. The detail is in `worker-ref-retire-gate.md`.

## Cross-references

- g-306-506 (this mode), g-306-284 (the protocol it mechanizes)
- rb-6101 (merge-tree and drivers, legacy form), rb-12486 (merge-tree preflight),
  rb-12820 (snapshot-first verification), rb-12102 (daemon recycles on the merge commit),
  rb-12157 (record-level check on clean ledger merges)
- guard-3660 (an absent row is no licence to retire; the commit clock and the heartbeat
  carrier are asked by `worker_ref_retire_gate.py`, see `worker-ref-retire-gate.md`),
  guard-5718 (loop until a fresh re-read is empty; the plan's pin is for it)
- `core/scripts/tests/test_worker_ref_drain.py` — verdicts pinned on real git fixtures
- `core/config/rationale/worker-carrier-and-closure-evidence.md` — the carrier itself
