# Why the worker loop flushes the gate-firings spool (g-306-432)

Holds the WHY for `worker-loop/SKILL.md` Phase -0.15, which carries imperatives
only — `worker-loop/SKILL.md` is a Tier-1 hot-path file under
`hot-path-size-budget.md` (ratchet: no growth), and this file is deliberately
not budgeted, so narrative belongs here.

## The defect

Under `STORAGE_BACKEND=own-cloud`, `_gate_log.log()` appends each gate firing to
a MACHINE-LOCAL spool (`meta/gate-firings.spool.jsonl`, lockless O_APPEND)
instead of paying a whole-object S3 RMW per record (measured 3.8–10.1 s per
append against a ~40 MB store). `gate-firings-flush.py` drains that spool into
the shared `meta/gate-firings.jsonl` with ONE batched locked RMW.

That flusher had exactly **one** caller: `iteration-close.sh`, inside
`do_productivity_check`. `productivity-check` is a REDUCER-ONLY phase
(`worker_execute.py reducer-only-phases`), which the worker loop skips by
design. So on a worker box the wiring existed and was never reached.

Nothing else carried the records either: the spool is in
`owncloud_sync._EXCLUDE_NAMES` — correctly, since syncing one box's spool to the
shared meta prefix would clobber every peer's spool (the franken-copy class the
spool exists to avoid).

Measured: 522 records / 188,562 B stranded ~17 h on cc-09 (alpha worker Body
2fda1f3e, 2026-09-03T10:42:40 oldest); independently reproduced on cc-08 at
claim time for this goal — 194 records / 68,556 B, oldest 2026-09-04T00:12:34,
newest 2 minutes old, i.e. actively accumulating with no drain.

## Why the existing check did not catch it

`verification-checklist.md` item 42 asserted that `gate-firings-flush.py`
APPEARS IN `iteration-close.sh`. That was true, stayed true, and passed on every
box while the lane was dead on all of them. Classic **guard-3448** — a gate is
only as broad as its ENTRY POINTS. Item 42 is widened by this goal to assert
worker-reachability, not mere presence.

## The class

Fourth instance of "a reducer protection never reaches the second orchestrator":

| goal | what a worker never got |
|---|---|
| g-306-233 | never pulled the framework |
| g-306-227 | had no heartbeat |
| g-306-370 | never pulled product repos |
| g-306-432 | never flushed gate telemetry |

## What it confounded

g-306-413's strongest reading asks for cross-box-body-liveness firings
attributed to WORKER boxes. The three that answer it exist only in one box's
spool and are structurally invisible to `gate-stats.sh`, whose `firings_paths()`
deliberately excludes spools. So any goal measuring worker-box gate telemetry
from the shared store was blind BY CONSTRUCTION — and g-306-413's own pre-fix
baseline ("cross-box-body-liveness recorded ZERO firings on 2026-09-03, all
prior ones from a box where running-session-id is present") is exactly what a
reducer-only flush lane produces regardless of whether the helper was ever
called. That zero corroborates the g-306-412 diagnosis LESS strongly than it
appears to.

## Why every cycle is safe

The cadence lives in the `.py`, not the caller: `--min-interval-seconds`
(default 300) skips the flush when the last one was recent, unless the spool has
`--burst-records` (default 200) lines. That bounds churn to roughly one S3 RMW
per five minutes per box. So the loop calls it unconditionally and never
hand-rolls a cadence — a caller-side interval check would be a second,
drifting copy of a bound the script already owns.

It is idempotent and duplicate-safe (dedup by serialized line — the same
identity `merge_append_only_jsonl` unions by), so a missed tick simply leaves
the spool for the next cycle. Fail-open; never branch on its rc.

## Verifying it

**Verify in the SPOOL, never in the shared store (guard-4040).** An unmoved
`gate-firings.jsonl` mtime is not evidence of anything here — the destination
legitimately sits hours behind while writes land fine, and reading it as
"telemetry is dead" is a documented false HIGH. guard-4040 also warns there are
FOUR candidate destinations, not two: the spool, the DATED SEGMENT
(`gate-firings-YYYY-MM-DD.jsonl`, live when `GATE_FIRINGS_SEGMENTED` is set),
the legacy store, and any explicit `meta_dir` a daemon caller passed.

## Why a `.sh` wrapper exists for a one-line call

`core/scripts/gate-firings-flush.sh` exists solely so SKILL.md pseudocode never
names the `.py`. Per **guard-350**, `bash <file>.py` makes bash parse the Python
docstring as shell: every line errors and a trailing `|| true` masks the whole
thing as exit 0, so the step appears to run and contributes zero signal.
Reproduced while building this (negative control):

    $ bash core/scripts/gate-firings-flush.py
    core/scripts/gate-firings-flush.py: line 48: _gate_log.py: command not found
    core/scripts/gate-firings-flush.py: command substitution: line 49: syntax error
