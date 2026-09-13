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

## Cross-references

- `guard-399` — an instruction's form vs. who executes it
- `guard-6178` — an un-evaluatable check must not read as an all-clear
- `.claude/rules/stop-hook-compliance.md` — the four authorized writers
- `core/config/rationale/vessel-sidecar-stop-caller.md` — the sidecar writer
- `core/scripts/tests/test_session_live_stop_guard.py` — REFUSE/PERMIT pins
