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
record.  Making that sensor trustworthy was the SIBLING goal (closed 2026-09-04) and is
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

FAIL-SAFE DIRECTION: every unreadable or unparseable input HOLDS.  Stopping a
healthy loop is worse than the disease (guard-1562).  ONE absence is not
ambiguous and does not hold: a session with turn-ends and NO diary at all.
The diary is appended at every phase start/end, so "no diary" means "no
phase ever ran", and the streak is anchored at that session's FIRST turn-end.
Measured 2026-09-17 on a prod vessel (debc47de, run A): 85 consecutive BLOCKs
over 21 minutes, every one answered by prose, no diary ever written -- and
this fence HELD on all 85 because an absent diary read as unreadable.  Note this is
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
import subprocess
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
#: The WORKER sibling's decisive rung (). decide() never returns it:
#: a worker Body must never write the agent-wide stop signal, which the reducer
#: on another machine reads, so its last rung PARKS the Body instead.
VERDICT_PARK = "park"

# rc mirrors the verdict so a shell caller can branch without parsing JSON.
RC_BY_VERDICT = {VERDICT_HOLD: 0, VERDICT_PAUSE: 1, VERDICT_STOP: 2, VERDICT_PARK: 3}

#: Every verdict stop-hook.sh writes at a turn-end.  Built from the EMITTER's
#: format strings, not from prose (guard-4285): the hook writes
#: `<ts> BLOCK sid=<sid> ...` (no gate= field) and
#: `<ts> ALLOW gate=<name> sid=<sid> ...`, so both tokens are space-delimited
#: and this pair is the complete vocabulary (measured over the live log:
#: 815 BLOCK / 535 ALLOW, no third value).
#:
#: WHY BOTH, and not BLOCK alone ().  The fence's own pause rung
#: prescribes ending the turn on a REGISTERED external-wait sleep, and a
#: registered Tier-A job makes Gate 2.6 ALLOW the turn-end instead of BLOCKing
#: it.  Counting BLOCKs alone therefore meant that an agent which ADOPTED the
#: remedy stopped emitting the only observable the ladder counted -- unreachable
#: from below, forever.  Measured: a 10.5h stall produced 9 turn-ends and ZERO
#: BLOCKs, and the fence never fired.
TURN_END_VERDICTS = (" BLOCK ", " ALLOW ")


def _cause_note(budget_zone):
    """What a firing DOES and does NOT establish, carried in the reason string.

    The streak+frozen-diary predicate proves the loop is NOT ADVANCING.  It does
    not prove WHY.  "Exhaustion" in this module's name is the one cause it was
    built from (bravo/cc-05, context genuinely at zero) -- not a finding about
    any later firing.  Measured counter-case: alpha/cc-04 2026-09-11 fired the
    stop rung at 307 BLOCKs with the diary frozen since 06:29:19 while the
    session's context was only ~half consumed.  The stop was CORRECT (a loop
    that has not advanced in 307 turn-ends cannot execute); the cause everyone
    read off the name was not, and that misread sends the next diagnosis at the
    context budget instead of at whatever actually froze the diary.

    So the reason names the recorded zone and states the limit of the claim.
    Still never decisive -- see the module docstring and
    `test_budget_zone_is_recorded_but_never_decisive`.
    """
    return " [proven: no phase advance. cause NOT established -- context budget zone: %s]" % (
        budget_zone or "unrecorded"
    )


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

    `budget_zone` is RECORDED and NAMED IN THE REASON, never decisive -- see the
    module docstring and `_cause_note`.
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
            "turn-end #%d for this session (BLOCK **or ALLOW** -- see TURN_END_VERDICTS) "
            "with the execution diary frozen %.0fs (>= stop_threshold %d): the pause "
            "rung did not restore phase advance, so this loop cannot execute"
            % (streak, stalled_seconds, stop_threshold)
            + _cause_note(budget_zone),
        )

    return _out(
        VERDICT_PAUSE,
        "turn-end #%d for this session (BLOCK **or ALLOW** -- see TURN_END_VERDICTS) "
        "with the execution diary frozen %.0fs (>= pause_threshold %d): pause instead "
        "of re-entering immediately" % (streak, stalled_seconds, pause_threshold)
        + _cause_note(budget_zone),
    )


def compute_streak(log_path, sid, diary_path, now=None):
    """Consecutive TURN-ENDS for `sid` since the diary last advanced.

    PARTIAL mirror of stop-hook.sh's inline advisory block (g-115-8745) -- same
    log, same trailing-space sid anchor, same phase-advance anchor on the
    diary's mtime.  The VERDICT MATCH DELIBERATELY DIVERGES since g-115-9467:
    the hook's inline copy stays BLOCK-scoped because it is message-only and
    runs only while composing the BLOCK payload, whereas this copy drives a
    DECISION and must count every turn-end (see TURN_END_VERDICTS).  The parity
    test pins both halves of that relationship, including the divergence.

    Returns (streak, stalled_seconds); (None, None) when either source is
    unreadable, so `decide()` holds.
    """
    if not sid:
        return (None, None)
    try:
        advanced = datetime.datetime.fromtimestamp(pathlib.Path(diary_path).stat().st_mtime)
    except FileNotFoundError:
        # No diary was ever written: the loop has never advanced a phase, so
        # every turn-end for this sid counts and the anchor is the first one
        # (see FAIL-SAFE DIRECTION in the module docstring).  Any OTHER
        # failure to read the diary still holds, below.
        advanced = None
    except (OSError, ValueError, TypeError):
        return (None, None)
    try:
        text = pathlib.Path(log_path).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError, TypeError):
        return (None, None)

    needle = "sid=" + sid + " "
    streak = 0
    first_turn_end = None
    for line in text.splitlines():
        if needle not in line:
            continue
        if not any(v in line for v in TURN_END_VERDICTS):
            continue
        try:
            when = datetime.datetime.fromisoformat(line.split(" ", 1)[0])
        except ValueError:
            continue
        if advanced is None:
            first_turn_end = when if first_turn_end is None else min(first_turn_end, when)
            streak += 1
        elif when >= advanced:
            streak += 1

    if advanced is None:
        if first_turn_end is None:
            return (0, 0.0)
        advanced = first_turn_end

    now = now or datetime.datetime.now()
    return (streak, max(0.0, (now - advanced).total_seconds()))


# --- The WORKER sibling () ----------------------------------------
#
# MEASURED: alpha worker Body on cc-09, SID 1f257cc9, logged 205 worker-net
# BLOCKs between 2026-09-24T18:51 and 09-26T18:09 while holding no claim, each
# answered with a sleep, and nothing stepped in -- stop-hook.sh's worker-net
# branch exits before either call site of decide(), so the user directive
# behind  never reached a worker. Three things differ for a worker,
# one per design constraint of the goal:
#   (a) its decisive rung PARKS the Body (body-manifest.py park: resumable, on
#       the park orbit, expiring at PARK_MAX_HOURS). It never writes the
#       agent-wide stop signal, which stops the REDUCER on another machine.
#   (b) its predicate is "no claim held by this SID", not a frozen diary. A
#       worker writes the SHARED agent-wide diary (execution-diary.sh has no
#       per-Body routing), so a live reducer or sibling Body would keep
#       resetting a diary anchor and the fence would never fire; and while a
#       claim IS held, diary age equals unit duration. A held claim HOLDS
#       whatever the count.
#   (c) the park ALERTS (a notifying stop-reason path plus a board post): a
#       stall-park is a defect signal, not the quiet reducer-gone park.
#
# THE STREAK is worker-net BLOCKs for this sid since the Body last touched a
# goal record (claim, release, close): the latest ACTIVITY_FIELDS value over
# the goals whose executed_by_sid is this sid. A Body that keeps claiming work
# moves the anchor every unit, however often it text-dies between units; a
# Body that holds nothing and claims nothing does not.
#
# ONLY BLOCKs COUNT, deliberately and not as the  lesson forgotten:
# every remedy this ladder asks for LEAVES the stall -- a claim moves the
# anchor, a park takes the Body out of the worker-net branch (the park valve
# ALLOWs above it) -- so adopting a remedy cannot hide a stalled Body's
# turn-ends as uncounted ALLOWs. If  gives workers an ALLOW that a
# stalled Body can sit on (tracked background jobs), add it to the match.

WORKER_NET_TURN_END = " BLOCK gate=worker-net "

#: A goal in one of these states is NOT a live claim. Anything else -- including
#: a status this set does not know -- counts as held, which HOLDS.
TERMINAL_GOAL_STATUSES = frozenset(
    {"completed", "skipped", "expired", "decomposed", "superseded"})

#: The record timestamps that mark a Body touching a goal. `started` is left out
#: on purpose: it is date-only, reads as midnight, and would move the anchor
#: EARLIER -- the direction that fires the fence sooner.
ACTIVITY_FIELDS = ("last_modified", "completed_at", "claimed_at")


def claim_held(rows, sid):
    """True when `rows` (a claimed_by_sid query result) holds a live claim.

    None when the answer is unusable -- not a list, a non-dict row, or a row
    claimed by a DIFFERENT sid (the store answered another question) -- so
    decide_worker() holds.
    """
    if not sid or not isinstance(rows, list):
        return None
    held = False
    for row in rows:
        if not isinstance(row, dict) or row.get("claimed_by_sid") != sid:
            return None
        if str(row.get("status") or "").strip() not in TERMINAL_GOAL_STATUSES:
            held = True
    return held


def _naive(when):
    """Naive UTC wall time, the fleet's timestamp convention (CLAUDE.md)."""
    if when.tzinfo is not None:
        when = when.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return when


def last_activity(rows, sid):
    """(anchor, readable) from an executed_by_sid query result.

    `anchor` is the latest ACTIVITY_FIELDS timestamp over the rows, or None when
    this Body never touched a goal -- then every worker-net BLOCK counts, as
    with the reducer's no-diary rule. `readable` is False when the answer is
    unusable, so the caller holds.
    """
    if not sid or not isinstance(rows, list):
        return (None, False)
    anchor = None
    for row in rows:
        if not isinstance(row, dict) or row.get("executed_by_sid") != sid:
            return (None, False)
        for field in ACTIVITY_FIELDS:
            raw = row.get(field)
            if not raw:
                continue
            try:
                when = _naive(datetime.datetime.fromisoformat(str(raw)))
            except ValueError:
                continue
            anchor = when if anchor is None else max(anchor, when)
    return (anchor, True)


def compute_worker_streak(log_path, sid, anchor, now=None):
    """Worker-net BLOCKs for `sid` at or after `anchor` -> (streak, stalled_seconds).

    The stall is measured from `anchor`, or from the first counted BLOCK when
    the Body never touched a goal (anchor None). (None, None) when the sid is
    empty or the log unreadable, so decide_worker() holds.
    """
    if not sid:
        return (None, None)
    try:
        text = pathlib.Path(log_path).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError, TypeError):
        return (None, None)

    needle = "sid=" + sid + " "
    streak = 0
    first_block = None
    for line in text.splitlines():
        if WORKER_NET_TURN_END not in line or needle not in line:
            continue
        try:
            when = datetime.datetime.fromisoformat(line.split(" ", 1)[0])
        except ValueError:
            continue
        if anchor is not None and when < anchor:
            continue
        streak += 1
        first_block = when if first_block is None else min(first_block, when)

    start = anchor if anchor is not None else first_block
    if start is None:
        return (0, 0.0)
    now = now or datetime.datetime.now()
    return (streak, max(0.0, (now - start).total_seconds()))


_WORKER_CAUSE_NOTE = " [proven: no claim held and no goal activity. cause NOT established]"


def decide_worker(
    held,
    streak,
    stalled_seconds,
    *,
    pause_threshold=DEFAULT_PAUSE_THRESHOLD,
    stop_threshold=DEFAULT_STOP_THRESHOLD,
    min_stalled_seconds=DEFAULT_MIN_STALLED_SECONDS,
):
    """The worker ladder: hold / pause / park. Pure; never raises, never writes.

    Same thresholds and wall-clock floor as decide(), gated first on the claim:
    an unreadable claim store HOLDS, and a held claim HOLDS whatever the count.
    """
    result = {
        "role": "worker",
        "verdict": VERDICT_HOLD,
        "reason": "",
        "claim_held": held,
        "streak": streak,
        "stalled_seconds": stalled_seconds,
        "pause_threshold": pause_threshold,
        "stop_threshold": stop_threshold,
    }

    def _out(verdict, reason):
        result["verdict"] = verdict
        result["reason"] = reason
        result["rc"] = RC_BY_VERDICT[verdict]
        return result

    if held is None:
        return _out(VERDICT_HOLD, "claim store unreadable; holding (fail-safe)")
    if held:
        return _out(VERDICT_HOLD, "this Body holds a live claim; a held claim is never fenced")
    if streak is None or stalled_seconds is None:
        return _out(VERDICT_HOLD, "stall signal unreadable; holding (fail-safe)")
    try:
        streak = int(streak)
        stalled_seconds = float(stalled_seconds)
    except (TypeError, ValueError):
        return _out(VERDICT_HOLD, "stall signal unparseable; holding (fail-safe)")
    if stop_threshold <= pause_threshold:
        return _out(
            VERDICT_HOLD,
            "thresholds misconfigured (stop_threshold %s <= pause_threshold %s); holding"
            % (stop_threshold, pause_threshold),
        )
    if streak < pause_threshold:
        return _out(VERDICT_HOLD, "streak %d < pause_threshold %d" % (streak, pause_threshold))
    if stalled_seconds < min_stalled_seconds:
        return _out(
            VERDICT_HOLD,
            "streak %d reached but only %.0fs without goal activity "
            "(< %.0fs floor)" % (streak, stalled_seconds, min_stalled_seconds),
        )
    if streak >= stop_threshold:
        return _out(
            VERDICT_PARK,
            "worker-net BLOCK #%d for this Body, holding no claim, %.0fs without "
            "goal activity (>= stop_threshold %d): the pause rung did not "
            "restore a claim" % (streak, stalled_seconds, stop_threshold)
            + _WORKER_CAUSE_NOTE,
        )
    return _out(
        VERDICT_PAUSE,
        "worker-net BLOCK #%d for this Body, holding no claim, %.0fs without "
        "goal activity (>= pause_threshold %d; the fence parks this Body at "
        "#%d)" % (streak, stalled_seconds, pause_threshold, stop_threshold)
        + _WORKER_CAUSE_NOTE,
    )


def _query_goals(field, value, timeout=20):
    """Rows from `aspirations-query.sh --goal-field <field> <value> --full`.

    None on any failure (daemon down, non-zero rc, non-JSON output), which
    reads as unreadable and holds. That includes the daemon's
    `unknown_goal_field` refusal, which it returns when NO record in the queried
    queues carries the key. A close REMOVES claimed_by_sid, so a queue where
    nobody holds a claim cannot answer, and the fence holds until one does.
    That is deliberate: a renamed field must never read as "no claim held".
    """
    script = pathlib.Path(__file__).resolve().parent / "aspirations-query.sh"
    try:
        from _runtime_bash import bash_cmd
        proc = subprocess.run(
            bash_cmd(script, "--goal-field", field, value, "--full"),
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:  # noqa: BLE001 -- an unreachable store holds, never raises
        return None
    if proc.returncode != 0:
        return None
    try:
        rows = json.loads(proc.stdout)
    except ValueError:
        return None
    return rows if isinstance(rows, list) else None


def evaluate_worker(sid, log_path, *, query=_query_goals, now=None, **thresholds):
    """Read both stores and decide. The claim is checked first, so a Body that
    holds one never pays for the second query or the log read."""
    held = claim_held(query("claimed_by_sid", sid), sid) if sid else None
    if held is not False:
        return decide_worker(held, None, None, **thresholds)
    anchor, readable = last_activity(query("executed_by_sid", sid), sid)
    if readable:
        streak, stalled = compute_worker_streak(log_path, sid, anchor, now=now)
    else:
        streak, stalled = None, None
    return decide_worker(False, streak, stalled, **thresholds)


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
    # `worker` is the  sibling: decide_worker() over the claim store
    # and this sid's worker-net BLOCKs. The default keeps the reducer path
    # byte-identical to what it was before the flag existed.
    p.add_argument("--role", choices=("reducer", "worker"), default="reducer")
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

    if args.role == "worker":
        out = evaluate_worker(
            args.sid,
            args.log,
            pause_threshold=args.pause_threshold,
            stop_threshold=args.stop_threshold,
            min_stalled_seconds=args.min_stalled_seconds,
        )
        print(json.dumps(out))
        return out["rc"]

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
