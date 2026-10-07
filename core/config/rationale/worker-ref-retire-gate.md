# Rationale: the retire gate (`worker_ref_retire_gate.py`)

Referenced from `core/scripts/worker_ref_retire_gate.py` and the `--retire` header of
`core/scripts/worker-ref-consume.sh`. Why an ABSENT in_flight_bodies row no longer licenses
`--retire`, which signals decide instead, and why every unknown reads CARRY.
Unit (d) of g-306-506; the governing guardrail is guard-3660 and its OCC211, OCC286 and OCC299
corrections.

## Why an absent row is not a licence

`--retire` already refused a LIVE row and an unreadable source. It treated an ABSENT row as
"no Body uses this ref", and that reading is wrong by construction: the row is written per
CLAIM and cleared at the unit's close, so every live Body is row-less in the window between
two units, once per unit, forever. The schema-drift probe that guards the null branch catches
a renamed key and cannot catch a Body that is alive between claims. Measured, twice in one
occurrence (OCC211): a Body's ref was retired with its tip 14 minutes old, the Body pushed
again and re-created the ref, and it was retired again with a tip 5 minutes old.

## Why three signals, and why the tip clock alone is not enough

A ref's tip date is the age of the Body's last PUSH, not of the Body. A Body whose units edit
no framework file never pushes. Measured on the live fleet 2026-10-06: all 11 outstanding refs
belonged to Bodies whose heartbeat carrier read fresh-correct (0.3 to 18.6 minutes old), with
tips from 17.8 minutes to 4.7 days old, and 9 of the 11 were reachable from origin/main. A gate
on the tip clock alone, at the reaper's 180-minute threshold, would have permitted 8 of those 9,
every one a live Body's push target. The heartbeat carrier is the signal that was true for all
of them, and the two controls behaved (this Body's own carrier read fresh-correct; a sid that
never existed read absent).

The third signal, origin's current tip, is not a liveness signal but a precondition of the other
two: `--no-fetch` leaves the local ref as old as the last fetch, so the tip clock would describe
a ref that has since moved. A ref that moved after the local copy was made is a Body pushing, and
reads CARRY on that alone.

## Why the reaper's opinion and not a new one

OCC286 asks that the retire decision and `body_row_reaper` hold ONE opinion about one Body, since
two opinions about one Body are worse than one. So the gate imports the reaper's threshold
(`DEFAULT_REAP_STALE_MINUTES`; a test fails on a second literal) and asks the reaper's own
`decide_row` what it would do with a row-less Body that has this carrier. The reaper calls a Body
dead on exactly one verdict, `stale`. `absent` (never written, cleared by a recovery, or not yet
synced), `unreadable`, `fresh-wrong` (a carrier another sid wrote, guard-358) and `closed` all
keep. For retire that has two consequences, both accepted: a ref whose Body left no carrier is
never retire-eligible without `--force-retire-live`, and a Body that closed cleanly becomes
eligible when its carrier ages past the window rather than when it closes. The cost of an
un-retired, already-consumed ref is clutter. The cost of a wrong delete is a lost handle.

No claims census is consulted. The reaper keeps a stalled Body that holds a live claim, but such
a Body has a PRESENT row (written at claim time, reaped only without a live claim), and a present
row is refused before this gate is asked.

## Why every failure reads CARRY, and why the shell holds two keys

A gate's fail-open handler also covers the construction of its own refusal (guard-3803), and
`stranded-claim-sweep._body_carrier_verdict` documents that a CLI-shaped helper can raise
SystemExit through an `except Exception`. So the module catches BaseException, any unreadable
signal is CARRY, and the shell permits only on rc 0 AND an output line that opens `RETIRE: `. A
helper that exits 0 without deciding, a CARRY printed with rc 0, a RETIRE printed with rc 1, a
crash, a usage error and a missing file are each refused by a test that turns exactly one key off.
The helper's line is control-stripped before it is printf-composed into the JSON receipt.

The first run of the wired tests found a defect that fails closed and so would have shipped
silently: bash's `${v: -600}` is the EMPTY string, not the whole value, when the value is shorter
than 600 characters. Every gate line came back empty, every retire was refused, and only the tests
that ran the real shell path said so.

## Why the plan reads the gate without the origin reads

`--drain` is a local, read-only plan, and `--retire` re-reads eligibility inside its own call
(guard-5952), so the plan's candidate lines are advice and `--retire` is the authority. The plan
therefore calls the same function with `check_remote=False`. A plan that listed 9 candidates
without the gate would send the reducer to `--retire` nine times to be refused nine times.

## Known limit

The delete itself is still a plain `git push origin :<ref>`, not a leased one. A Body that pushes
between the gate's origin read and the delete loses the ref; its next carrier push re-creates it
(g-306-505's coupling, measured in OCC211). A `--force-with-lease` delete would close the window
and is a separate change.

## Cross-references

- guard-3660 (and OCC211, OCC286, OCC299), guard-5952, guard-3803, guard-358, guard-5501
- `core/scripts/body_row_reaper.py` — `DEFAULT_REAP_STALE_MINUTES`, `decide_row`
- `core/scripts/stranded-claim-sweep.py` — `_body_carrier_verdict`, the shared carrier verdict
- `core/config/rationale/worker-ref-drain-plan.md` — the plan that prints this gate's line
- `core/scripts/tests/test_worker_ref_retire_gate.py`, `test_worker_ref_consume.py`,
  `test_worker_ref_drain.py` — the gate, the shell wiring and the plan lines
