# Rationale: Execution-Diary Baseline at /start

Referenced from `.claude/skills/start/SKILL.md` (the IDLE → RUNNING sequence,
the Bash step directly after the `session-state-set.sh RUNNING` halt). Why
/start appends one execution-diary entry the moment the loop is RUNNING.

## Why a diary entry, and why at /start

The loop-exhaustion fence's streak is "consecutive turn-ends for this SID
since the execution diary last advanced" (`loop_exhaustion_fence.py::
compute_streak`, anchored on the diary file's mtime). It pauses at 4 and
stops the loop at 10.

`/stop` followed by `/start` in ONE terminal keeps the Claude Code session id,
so nothing about a restart changes the SID the fence counts under. Before this
step existed, a restarted loop inherited every turn-end the stalled loop had
already accumulated: measured 2026-09-25, a 12:20Z restart of a stalled
reducer was killed by the `stop` rung at turn-end #13, most of those counted
before the restart. The restart looked like a continuation of the stall.

One appended entry advances the diary's mtime, so `compute_streak` counts only
turn-ends after it: the restarted loop starts at 0, which is what a fresh
start means.

## Why AFTER the RUNNING flip and its halt

A failed flip halts /start with state still IDLE. Appending before it would
leave a "loop start" breadcrumb for a loop that never started, and the next
genuine start would read a fresh anchor it did not earn. After the halt, the
entry exists only when the loop does.

## Why non-fatal

The fence HOLDS on an unreadable or missing diary (every unreadable input
holds, guard-1562: stopping a healthy loop on a plumbing fault is worse than
the disease). A missing baseline therefore degrades to the pre-fix behaviour
(an inherited streak on a same-SID restart), never to a dead loop, so the
append must not be allowed to halt /start. The `|| echo WARN` surfaces the
failure without silencing it (guard-139 is about decisions; this breadcrumb
drives none).

## Why `state_update`

`execution-diary.py` refuses any `entry_type` outside `VALID_ENTRY_TYPES`
(fail-close since g-240-44). `state_update` is a member and describes the
event. `test_start_diary_baseline_on_running.py` pins the type against the
live allow-list so a rename cannot make the append fail silently at every
/start. The script also exits 0 silently for an observer session, so an
observer /start writes nothing.

## Cross-references

- `.claude/rules/stop-hook-compliance.md` — the loop-exhaustion-fence
  exception (what the fence is allowed to do and on what evidence)
- `core/scripts/loop_exhaustion_fence.py` — `compute_streak` and the
  FAIL-SAFE DIRECTION docstring
- `core/scripts/tests/test_start_diary_baseline_on_running.py` — the pins
- guard-1562 — every unreadable input HOLDS; guard-700 — periodic diary
  entries are the phase-advance signal during long execution
- `start-runner-claim-and-body-fork.md` — the sibling rationale for the
  RUNNING-claim sequence this step is appended to
