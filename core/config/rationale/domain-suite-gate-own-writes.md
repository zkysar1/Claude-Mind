# domain-suite-gate: trigger on this unit's own writes

Why the gate keeps only the files THIS unit's sessions wrote, how it learns
which those are, and the failure direction it chose. Traceability: g-115-9084
(filed 2026-09-05 from four independent measurements; sibling framings
g-115-8424, g-115-8468, g-115-8654, g-115-9297, g-115-9377), built 2026-10-02
(alpha, cc-09). Code: `own_writes`, `unit_sessions`, `edits_ledger` in
`core/scripts/domain-suite-gate.py`. Tests:
`core/scripts/tests/test_domain_suite_gate_own_writes.py`.

## The defect

`touched_since()` walks the whole `$WORLD_PATH/scripts` tree and returns every
code file whose mtime is at or after `claimed_at` minus 60 s. That tree is
shared and fleet-synced, so the predicate answers "did anyone on the fleet touch
this area", not "did this goal" (guard-6881). Each firing then runs the full
domain suite, bounded at 900 s.

Measured (bravo and zeta, 2026-09-30, rb-12528 and rb-12529): 91 of 105 firings
on one box and 91 of 97 on another touched no file the closing agent wrote,
pooled 182 of 202 (0.901, an upper bound). On the first box 60 of 213 firings
hit the 900 s ceiling and failed open, 51 of them partner-only. Since the suite
grew past the bound it buys nothing: on one box a plan of 113-119 units ended
with a verdict in 0 of 41 runs, and every run since 2026-10-01 was INCOMPLETE.
So a peer-triggered run was pure latency, about 15 minutes per close.

## Why the window alone was not the fix

The 6 h fallback fires when `claimed_at` is unreadable, which happens when a
terminal status lands before the gate (the claim triple is popped) and for
agent-queue goals, which never claim. But a tighter, truer claim time does not
help: a peer landing inside the window still counts (guard-6881's inversion).
Both directions are needed, so the trigger is intersected with the goal's own
writes whatever window it uses.

## The writer record already existed

The changelog cannot attribute tool writes (they do not pass through
`_fileops`). The record that does is the per-agent edit log
`agents/<agent>/session/uncommitted-edits.jsonl`: `uncommitted-edits-record.sh`,
chained from `tree-sync-check.sh` on every Write/Edit/MultiEdit, appends one row
(`file` relative to the project root, `mtime` in epoch seconds, `edit_ts`,
`goal_id`, `sid`). World scripts are in it because the world dir sits under the
project root and the recorder's `world/` skip matches only the virtual prefix.
The gate reads the log, keeps rows whose `sid` is one of this unit's sessions
(the closing process's `MIND_SID`, plus the claim's `claimed_by_sid` and
`executed_by_sid`) and whose `mtime` is inside the window, and intersects that
set with `touched_since()`. Nothing new is recorded.

Coverage, measured 2026-10-02 on cc-09 (one box, one session, its whole
transcript): 230 successful Write/Edit/MultiEdit calls targeted `world/scripts`
across 26 files, and the log held exactly 230 rows for that session on those
files, none missing and none extra, per-file counts equal.

## What it cannot see, and the direction chosen

The skip needs positive evidence that the changed files came from elsewhere
(rb-12529). Three blind spots the gate can see keep the wide trigger and say why
on the run line: no session id, no log (or an unreadable one), and a world
outside the project root, where the recorder drops every path. Two it cannot see
read as a peer's file: a write made through Bash instead of a Write/Edit tool,
and a write made on another box. The first was measured in the same
transcript: of 15,675 Bash calls none redirected, teed or `sed -i`'d into
`world/scripts`, and of 175 `cp`/`mv` lines with a parsed destination none
targeted it (13 lines would not parse). Guard-1052 adds that a Bash-authored
write to a governed file does not reach the store on an own-cloud box at all.

This is a CHOICE, not an oversight: the gate skips on absence from the log for
those two channels, because the alternative is a 15-minute run on every peer
landing that verifies nothing at today's suite size. The skip line says so, so
the reader can run the suite when the unit did write a domain script that way.
What would change it: a recorder for Bash-mediated world writes (the blind spot
closes), or a suite that fits the bound again (the verdict is then worth more
and the direction should be revisited).

The coverage pin rb-12529 asked for is a test, not a comment: the real recorder
is fed a world-script write payload and the gate must attribute the row it
produces. A recorder "fixed" to match its own header, which says world paths are
skipped, would drop those rows and fail the gate open for every own write; that
test fails first.

## What this does not change

The suite still covers the whole tree, so a peer's red can refuse an own-change
close. `--override-domain-suite` still excuses a red verdict and does not stop
the run. The 900 s bound, the baseline ratchet and the credential tripwire are
untouched. An own write inside the window, including one made in the minute
before the claim, still runs the suite.
