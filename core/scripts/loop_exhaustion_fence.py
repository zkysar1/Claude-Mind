#!/usr/bin/env python3
"""loop_exhaustion_fence — the decision half of the loop-exhaustion ladder.

WHY THIS EXISTS (g-115-8939, USER DIRECTIVE 2026-09-04: "the loop needs a
branch for 'no context to execute' that isn't 'iterate emptily.'").

MEASURED FAILURE (bravo, cc-05, 2026-09-04): context reached zero remaining
tokens.  No goal could be claimed and executed without stranding the claim, so
the loop ran ~35 consecutive null iterations over 2h21m with ZERO phase advance
-- the execution diary's mtime frozen at 13:18:36 across every one of them.
Each iteration was four calls (session-state-get -> heartbeat-tick ->
ScheduleWakeup(sentinel) -> Skill(aspirations)); every one of the four is
individually correct and mandated.  The livelock is EMERGENT: the no-self-stop
invariant, the "context filling up is not a stop condition" rule, the stop
hook's unconditional BLOCK, and the /start-and-/stop-only restriction on
session-signal-set.sh together leave the empty iteration as the only legal
action.  The framework's whole response to context pressure was SOFT
DEGRADATION (evolution skip, batch shrink, episode-chain caps, deferrable-sweep
drops), and every one of those assumes there is still room to execute
SOMETHING.  There was no rung below them.

THE PREDICATE IS BEHAVIOURAL, NOT A BUDGET READ, AND THAT IS THE DESIGN.
The obvious sensor -- context-budget-status.py's zone/headroom -- is exactly
what failed in the incident: it read `fresh` with `headroom_tokens: 479998`
right up to hard exhaustion because it was computing off a dead half of the
record.  Making that sensor trustworthy is the SIBLING goal (g-115-8310) and is
deliberately out of scope here.  So this fence keys on an observable the sensor
cannot lie about: N consecutive stop-hook BLOCKs for one sid with the execution
diary's mtime frozen throughout.  A loop that is advancing writes its diary
between turns and resets the count however often it blocks; a loop that cannot
execute does not, whatever any budget field claims.  Read a budget zone if you
have one -- `decide()` records it -- but nothing DECIDES on it.

HOW IT DISTINGUISHES "OUT OF CONTEXT" FROM "FEELS DONE".  Structurally: the
model supplies no input to this decision at all.  Both inputs (the hook's own
BLOCK log, the diary's mtime) are written by machinery the LLM does not control,
and the thresholds are config.  There is no code path by which the model can
elect to stop because it feels finished -- which is the invariant rb-629 /
guard-454 exist to protect (text-death silent loop death, 5 of 6 agents dead
1.5-4h on 2026-06-21).

THE LADDER, and why two rungs rather than one:

  hold   -- the healthy case and every ambiguous one.
  pause  -- streak >= pause_threshold.  The turn ends on a REGISTERED
            external-wait sleep instead of another immediate re-entry.  Cheap,
            fully reversible, and it needs about one tool call of budget --
            which matters, because a session with no room cannot execute an
            elaborate remedy.  The loop stays alive and netted; if autocompact
            or a smaller iteration frees room, the next wake resumes normally.
  stop   -- streak >= stop_threshold.  The pause demonstrably did not help, so
            the wrapper writes stop-target-mode then stop-requested and Phase
            -1.4 runs the ordinary graceful stop.  Needs ZERO model budget,
            which is the point: at this rung the session may have none.

Cheap-first-then-decisive mirrors reducer_self_fence.py, whose
`sustained-renewal-gap` trigger likewise waits out a duration before acting on
an ambiguous signal and acts at once on an unambiguous one.

FAIL-SAFE DIRECTION: every unreadable, absent, or unparseable input HOLDS.
Stopping a healthy loop is worse than the disease (guard-1562).  Note this is
the OPPOSITE direction from the worker-side reducer-liveness poll, deliberately
and for the same reason as that pair: there, an unobservable reducer means work
nobody will merge; here, an unobservable stall means a loop that is probably
fine.

THE STREAK ALGORITHM IS MIRRORED FROM stop-hook.sh's inline advisory (added by
g-115-8745), not invented here (guard-2783).  Same two sources, same match
shape, same phase-advance anchor.  `test_loop_exhaustion_fence.py` pins the two
against one fixture so a change to either fails loudly.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import sys

# Defaults chosen AGAINST the measured incident rather than picked round.
# bravo/cc-05 emitted 11 BLOCKs across the 2h21m livelock, so:
#   pause at 4  -- fires inside the first hour, well before the cost accrues;
#   stop at 10  -- fires late in the observed run, so the incident WOULD have
#                  been caught at both rungs rather than neither.
# A higher stop_threshold than 11 would have made the fence inert on the only
# occurrence anyone has measured, which is how a gate ships and never fires.
DEFAULT_PAUSE_THRESHOLD = 4
DEFAULT_STOP_THRESHOLD = 10

# Wall-clock floor under BOTH rungs.  A burst of blocks inside one legitimately
# long phase is not a stall, and the diary is written at phase start/end, so a
# single long phase can hold the mtime for a while.  In the incident the diary
# was frozen for 1h44m before the first BLOCK and ~4h by the end, so this floor
# cannot suppress the case it was built for.
DEFAULT_MIN_STALLED_SECONDS = 900.0

VERDICT_HOLD = "hold"
VERDICT_PAUSE = "pause"
VERDICT_STOP = "stop"

# rc mirrors the verdict so a shell caller can branch without parsing JSON.
RC_BY_VERDICT = {VERDICT_HOLD: 0, VERDICT_PAUSE: 1, VERDICT_STOP: 2}


def decide(
    streak,
    stalled_seconds,
    *,
    stop_requested_already=False,
    pause_threshold=DEFAULT_PAUSE_THRESHOLD,
    stop_threshold=DEFAULT_STOP_THRESHOLD,
    min_stalled_seconds=DEFAULT_MIN_STALLED_SECONDS,
    budget_zone=None,
):
    """Pure decision.  Returns a dict; never raises, never touches the disk.

    `budget_zone` is RECORDED and never decisive -- see the module docstring.
    """
    result = {
        "verdict": VERDICT_HOLD,
        "reason": "",
        "streak": streak,
        "stalled_seconds": stalled_seconds,
        "pause_threshold": pause_threshold,
        "stop_threshold": stop_threshold,
        "budget_zone": budget_zone,
    }

    def _out(verdict, reason):
        result["verdict"] = verdict
        result["reason"] = reason
        result["rc"] = RC_BY_VERDICT[verdict]
        return result

    if stop_requested_already:
        return _out(VERDICT_HOLD, "a stop is already in progress; nothing to add")

    # Unreadable inputs HOLD.  An absent hook log, an absent diary, a sid the
    # hook never learned -- all of them arrive here as None.
    if streak is None or stalled_seconds is None:
        return _out(VERDICT_HOLD, "stall signal unreadable; holding (fail-safe)")

    try:
        streak = int(streak)
        stalled_seconds = float(stalled_seconds)
    except (TypeError, ValueError):
        return _out(VERDICT_HOLD, "stall signal unparseable; holding (fail-safe)")

    # Misconfiguration must not arm the decisive rung ahead of the cheap one.
    if stop_threshold <= pause_threshold:
        return _out(
            VERDICT_HOLD,
            "thresholds misconfigured (stop_threshold %s <= pause_threshold %s); holding"
            % (stop_threshold, pause_threshold),
        )

    if streak < pause_threshold:
        return _out(
            VERDICT_HOLD,
            "streak %d < pause_threshold %d" % (streak, pause_threshold),
        )

    if stalled_seconds < min_stalled_seconds:
        return _out(
            VERDICT_HOLD,
            "streak %d reached but the diary has been frozen only %.0fs "
            "(< %.0fs floor) -- a burst inside one long phase is not a stall"
            % (streak, stalled_seconds, min_stalled_seconds),
        )

    if streak >= stop_threshold:
        return _out(
            VERDICT_STOP,
            "BLOCK #%d for this session with the execution diary frozen %.0fs "
            "(>= stop_threshold %d): the pause rung did not restore phase "
            "advance, so this loop cannot execute" % (streak, stalled_seconds, stop_threshold),
        )

    return _out(
        VERDICT_PAUSE,
        "BLOCK #%d for this session with the execution diary frozen %.0fs "
        "(>= pause_threshold %d): pause instead of re-entering immediately"
        % (streak, stalled_seconds, pause_threshold),
    )


def compute_streak(log_path, sid, diary_path, now=None):
    """Consecutive BLOCKs for `sid` since the diary last advanced.

    MIRROR of stop-hook.sh's inline advisory block (g-115-8745) -- same log,
    same " BLOCK " match, same trailing-space sid anchor, same phase-advance
    anchor on the diary's mtime.  Kept in step by a parity test.

    Returns (streak, stalled_seconds); (None, None) when either source is
    unreadable, so `decide()` holds.
    """
    if not sid:
        return (None, None)
    try:
        advanced = datetime.datetime.fromtimestamp(pathlib.Path(diary_path).stat().st_mtime)
    except (OSError, ValueError, TypeError):
        return (None, None)
    try:
        text = pathlib.Path(log_path).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError, TypeError):
        return (None, None)

    needle = "sid=" + sid + " "
    streak = 0
    for line in text.splitlines():
        if " BLOCK " not in line or needle not in line:
            continue
        try:
            when = datetime.datetime.fromisoformat(line.split(" ", 1)[0])
        except ValueError:
            continue
        if when >= advanced:
            streak += 1

    now = now or datetime.datetime.now()
    return (streak, max(0.0, (now - advanced).total_seconds()))


def _int_env(name, default):
    try:
        v = os.environ.get(name)
        return default if v in (None, "") else int(v)
    except (TypeError, ValueError):
        return default


def _float_env(name, default):
    try:
        v = os.environ.get(name)
        return default if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return default


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--sid", default=os.environ.get("HOOK_SID", ""))
    p.add_argument("--log", default=os.environ.get("HOOK_LOG", ""))
    p.add_argument("--diary", default="")
    p.add_argument("--stop-requested", action="store_true")
    p.add_argument("--budget-zone", default=None)
    p.add_argument(
        "--pause-threshold",
        type=int,
        default=_int_env("LOOP_EXHAUSTION_PAUSE_THRESHOLD", DEFAULT_PAUSE_THRESHOLD),
    )
    p.add_argument(
        "--stop-threshold",
        type=int,
        default=_int_env("LOOP_EXHAUSTION_STOP_THRESHOLD", DEFAULT_STOP_THRESHOLD),
    )
    p.add_argument(
        "--min-stalled-seconds",
        type=float,
        default=_float_env("LOOP_EXHAUSTION_MIN_STALLED_SECONDS", DEFAULT_MIN_STALLED_SECONDS),
    )
    args = p.parse_args(argv)

    streak, stalled = compute_streak(args.log, args.sid, args.diary)
    out = decide(
        streak,
        stalled,
        stop_requested_already=args.stop_requested,
        pause_threshold=args.pause_threshold,
        stop_threshold=args.stop_threshold,
        min_stalled_seconds=args.min_stalled_seconds,
        budget_zone=args.budget_zone,
    )
    print(json.dumps(out))
    return out["rc"]


if __name__ == "__main__":
    sys.exit(main())
