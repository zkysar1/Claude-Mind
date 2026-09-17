# Rationale: The Live-Stop Clear Guard

Referenced from `.claude/skills/start/SKILL.md` Step 2.5 and implemented by
`core/scripts/session.py::live_stop_decision`. Explains why clearing
`stop-requested` on IDLE entry is conditional rather than unconditional.

## Why the original premise expired

Step 2.5 read, verbatim: *"State is already IDLE, so no loop polling could be
interrupted; clearing is purely hygienic."* That was true while `/stop` was the
only writer of the signal — a leftover `stop-requested` could only be debris
from a partial stop, so deleting it lost nothing.

`stop-hook-compliance.md` now authorizes four programmatic writers, and the
newest is not a framework script at all: the **vessel sidecar** (`zakcode`)
raises `stop-target-mode` then `stop-requested` when a served run ends — the
human's `/run/stop`, or the run's own duration cap. That writer is OUTSIDE the
session and picks its own moment. So "the signal can only be debris" is false,
and on a vessel the unconditional unlink deletes the run's only ending:
consolidation never starts, no handoff is written, and the run is killed at its
hard cap with nothing recorded.

This is the ordinary shape of a stale caution: the sentence named one specific
pair (IDLE state, `/stop` as writer) and stayed persuasive after a new writer
made its binding false.

## Why the guard is in the script, not the skill

A SKILL.md instruction is executed by a model reading a file, so adding "probe
`stop-requested` at each page boundary" to `/start` would be the same
enforcement class as the absence it replaced (guard-399: changing an
instruction's FORM does not change WHO executes it). The vessel mind measured
below demonstrably dropped three `/aspirations` sections from its own plan in
the same run. `/start` already runs `session-signal-clear.sh` unconditionally,
so moving the decision INTO that script is the one place the check cannot be
skipped.

`live_stop_decision` is pure and exhaustively branch-tested, matching its three
sibling stop-gates (`reducer_self_fence.decide`, `loop_exhaustion_fence.decide`,
and the productivity gate) — the predicate is script-owned, never
LLM-discretionary.

## Why these four conditions, and why the legitimate clears still pass

REFUSE requires ALL of: the signal is present; `stop-loop` is ABSENT; the
session start is KNOWN (`binding.yaml` `started_at`); and the signal is NEWER
than that start.

* **`aspirations-graceful-stop`** sets `stop-loop` at D2 and clears the signal at
  D3, in that order — so its clear is always permitted. The ordering is what
  makes a handled stop distinguishable from a live one, which is why no override
  flag was needed on the legitimate path.
* **`/start`'s real hygiene case** — a partial `/stop` from a PREVIOUS session —
  leaves a signal older than `started_at`, so it still clears.
* **An unreadable start time** returns its own verdict (`clear-undetermined`)
  rather than `clear`: it clears, preserving prior behaviour, but never reads as
  a verdict that the signal was stale (guard-6178 shape).
* **`--force`** is the deliberate override, used by the lifecycle smoke test in
  `core/config/verification-checklist.md` item 13, which SETS the signal and must
  then be able to clear it.

## The reserve interaction, which points the opposite way to intuition

Measured 2026-09-13 (g-373-16, dev-lane vessel `i-022f74084032d8c56`, with
`ZAKCODE_RUN_CONSOLIDATION_RESERVE=350` read off the live `.env.local`): the
sidecar raised at 16:49:25 and `/start` ran this clear at 16:45:35 — 3m50s
apart, so the race did not fire.

It tightens as the reserve grows. `turn_deadline = T0 + SOFT_S - RESERVE`, so a
LARGER consolidation reserve moves the raise EARLIER, toward this clear. On that
run a reserve near 590s would have put the raise before 16:45:35 and the stop
would have been erased outright — the vessel running to its hard kill with no
stop signal at all. So "just raise the reserve" is not merely an ineffective
remedy for a mind that detects the stop too late; past a threshold it converts a
late-detection failure into a total signal loss.

## The binding-time hole, and the sidecar's signature (measured 2026-09-17)

The mtime rule assumed "this session's start" is close to the moment /start
began. On a vessel it is not: `started_at` is written by /start's binding
site, pages into the ceremony, and a slow model takes minutes to get there.
Prod vessel debc47de (user's Alien 2, run B, seed v2.12.71 — the guard
above was DEPLOYED and ran): the sidecar raised at 20:29:29, /start wrote
`binding.yaml started_at: 20:31:43`, Step 2.5 ran the clear at 20:32:09.
`signal_mtime < started_at` read as "stale", the verdict was CLEAR, and the
run's only ending was deleted; the Mind booted on until the sidecar's 350s
window ran out. The race the section above measured at 3m50s of headroom is
the same race with the headroom gone — a short cap, or a human `/run/stop`
inside the first minutes, lands the raise before the binding exists.

The fix does not move the timestamp; it stops consulting it for the one
writer that needs no clock. The sidecar (`zakcode.session.framework_stop`)
now writes its signature as the signal's first line —
`raised_by: vessel-sidecar`, then `raised_at: <utc>` — and
`live_stop_decision` REFUSES a signed signal outright (still subject to
`stop-loop` and `--force`, so the graceful-stop D2/D3 shape and the smoke
test's override are untouched). The framework's own writers leave the marker
EMPTY, so nothing else is affected; an unsigned signal takes the mtime rule
exactly as before, and the two halves ship independently (a signed signal
under the old guard, or the new guard over an unsigned one, is today's
behaviour).

Why a signed signal can be trusted without a clock: a sidecar raise is by
construction from the current run. The sidecar retires its own unconsumed
pair when its grace expires (g-373-92, `abandon_framework_stop`), and since
this change also at its next start when a signed raise is older than the
grace (`retire_expired_sidecar_stop`) — so a signed signal that survives to
/start was raised now, by the process that is still waiting on it.

## What a refusal leads to

A refused clear leaves the live signal on disk for the rest of `/start`, on
purpose. Keeping it is only half the job: something has to act on it before a
loop exists, because Phase -1.4 is unreachable from `/start`. Two readers do
that. The PreToolUse[Bash] advisory fires on every Bash call and names
`Skill(aspirations-graceful-stop)`. `/start`'s hand-off marker names the same
handler instead of `/boot` while the signal is present. Measured case and the
reasoning: `start-handoff-stop-route.md` (g-373-16 R3).

## Cross-references

- `guard-399` — an instruction's form vs. who executes it
- `guard-6178` — an un-evaluatable check must not read as an all-clear
- `.claude/rules/stop-hook-compliance.md` — the four authorized writers
- `core/config/rationale/vessel-sidecar-stop-caller.md` — the sidecar writer
- `core/scripts/tests/test_session_live_stop_guard.py` — REFUSE/PERMIT pins
