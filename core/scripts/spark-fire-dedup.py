#!/usr/bin/env python3
"""spark-fire-dedup.py — consumer-side dedup for the pending_phase_6_spark
sentinel (g-115-1203).

Problem: recurring-close.sh writes the pending_phase_6_spark sentinel
unconditionally at end-of-script AND emits a stdout outcome-aware imperative.
On the FAST path (the LLM sees the stdout inline), the LLM fires
Skill(aspirations-spark) in-turn (fire #1), then re-enters the loop where
aspirations/SKILL.md Phase -0.5c.2 reads the still-set sentinel and fires
Skill(aspirations-spark) AGAIN for the same goal (fire #2). The double-fire is
idempotent but wasteful: it pollutes the spark times_asked metrics (diluting
yield_rate) and writes duplicate evolution-log entries. The sentinel is only
NEEDED on the bg-timeout path (the LLM never sees the stdout); on the fast path
it is redundant.

Fix (shape (a) from the originating finding): track a wm slot
`spark_fired_session = {goal_id: fired_at_iso}`. Both fire paths funnel through
aspirations-spark, which `record`s its firing. Phase -0.5c.2 `check`s before
firing: if the goal was sparked within the dedup window (default 5 min), it
SKIPS the re-fire and just clears the sentinel.

PURE stdin->stdout — this script does NO wm I/O of its own. The caller (a
single Bash-tool pipeline) does the wm read/write via the canonical wm-read.sh
/ wm-set.sh wrappers and pipes the value THROUGH this script:

    record:  wm-read.sh <slot> --json | spark-fire-dedup.py record <gid> | wm-set.sh <slot>
    check:   wm-read.sh <slot> --json | spark-fire-dedup.py check  <gid>

This is deliberate: a python process must NOT spawn `bash wm-*.sh` via
subprocess on this platform — that hangs (the Windows python->bash subprocess
hang, rb-225 / rb-247; the wm slot getter/setter are daemon wrappers invoked
through bash). Keeping the script pure (read map from stdin, emit result to
stdout) sidesteps the hang entirely while leaving the wm I/O on the bash side
where it already works. The pure-function core (recently_fired /
prune_and_record) is unit-tested in test_pending_phase_6_spark_sentinel.py.

Fail-open at every layer: empty/`null`/malformed stdin, a missing entry, or an
unparseable timestamp all default to "fire" (never suppress a legitimate spark
— a redundant fire is wasteful but a suppressed one loses learning).

Cross-refs:
  - g-115-1203 (this Apply), g-115-1174 (sentinel origin), rb-428 (lifecycle)
  - rb-225 / rb-247 (Windows python->bash subprocess hang — why this is pure)
  - .claude/skills/aspirations/SKILL.md Phase -0.5c.2 (check consumer)
  - .claude/skills/aspirations-spark/SKILL.md (record producer)
  - core/scripts/recurring-close.sh (sentinel + stdout imperative writer)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta

SLOT = "spark_fired_session"  # documented here for callers; this script is slot-agnostic
# Consumption-based dedup ( / rb-1674): the PRIMARY path compares the
# recorded fire time against the sentinel's set_at (passed via --sentinel-set-at).
# DEFAULT_WINDOW_MIN is now only the FALLBACK for callers that cannot supply a
# set_at; it is aligned to the sentinel's 60-min TTL (Phase -0.5c.2 expires
# sentinels older than 60min) rather than the original 5 min, which was shorter
# than the bg-timeout wall-clock between record (aspirations-spark Step 0.5) and
# check (next loop entry) and false-fired an already-fired deep spark.
DEFAULT_WINDOW_MIN = 60
# Entries must SURVIVE from record-time to the consuming check, which on the
# bg-timeout path can be up to the sentinel's full ~60-min lifetime away. The
# prior 10-min prune dropped the fired entry before a same-close check could
# find it (on any iteration where an intervening spark fired and pruned). 90 =
# 60-min TTL + slack so the set_at comparison always has the entry to compare.
DEFAULT_PRUNE_MIN = 90
#  /  / rb-2615: a PROACTIVELY-fired Phase-6 spark records its
# fire BEFORE the bg recurring-close finishes writing the sentinel's set_at
# (observed: fire 01:55:14 vs set_at 02:01:25 -> ~6m11s early, because the LLM
# fires Skill(aspirations-spark) off the stdout imperative while recurring-close
# is still backgrounded). The strict `fired_at >= set_at` skip test therefore
# read a proactive fire as a PRIOR-close fire and false-fired the spark next
# iteration. MAX_BG_CLOSE_DURATION_MIN widens the consumption window's LOWER
# bound by this much so a proactive fire still counts as THIS close's
# consumption.
# : widened 10 -> 15. The NON-recurring producer (iteration-close.sh
# do_state_update, ) writes set_at at Phase 8, but the in-turn Phase-6
# spark fires earlier at Phase 6, so the lower bound must cover the whole
# Phase-6 -> Phase-8 execution gap. A slow post-compaction resume with heavy
# between-phase reasoning inflated that gap to 10m08s (fire 23:03:24 vs set_at
# 23:13:32, ), 8s past the old 10-min bound -> false re-fire (caught
# manually, no double-spark). 15 covers that with ~5min margin. Safe in BOTH
# directions: a NON-recurring goal closes exactly ONCE, so there is no genuine
# second close to suppress (the lower bound is purely a recurring-goal concern),
# and recurring intervals are hours (>> 15min), so widening never suppresses a
# genuine SECOND deep-close of a recurring goal either.
MAX_BG_CLOSE_DURATION_MIN = 15

#  / : the bound above was outgrown FOUR times (~10m08s ->
# widened to 15; then 15m09s zeta, 16m31s alpha, 24m00s  -- three
# agents, three days). It cannot be calibrated, because the two directions it
# conflates are not the same quantity:
#   - RECURRING path: lookback models how long a BACKGROUNDED recurring-close.sh
#     takes to write set_at after the LLM fires off its stdout imperative. That
#     IS bounded by script runtime -- MAX_BG_CLOSE_DURATION_MIN is right for it.
#   - NON-RECURRING path: the sentinel is written by iteration-close.sh
#     do_state_update at Phase 8, while the in-turn spark fires at Phase 6, so
#     the lookback must span the LLM's ENTIRE Phase-6 work. Phase 6 is UNBOUNDED
#     BY DESIGN (it files goals, writes stores, notifies, amends self.md), so no
#     constant can bound it and every widening only postpones the next cliff.
# The field reports cannot locate the true distribution either: a gap UNDER the
# bound dedups correctly and is never noticed, so every observation is
# structurally required to exceed it. The sample is LEFT-CENSORED AT THE BOUND,
# which is what produced the deceptive +7s/+9s/+20s pile-up (read at the time as
# a tight central tendency; it is censoring). Calibrating from it is impossible
# in principle, not merely imprecise.
# THE STRUCTURAL FACT that removes the need for a number: a NON-recurring goal
# closes exactly ONCE, so there is no genuine second close to suppress -- the
# lower bound is purely a recurring-goal concern. On that path ANY recorded fire
# for the goal_id is necessarily THIS close's fire, so the correct lower bound is
# not 15 or 20 minutes but ABSENT. Passing lookback_minutes=None expresses that.
UNBOUNDED_LOOKBACK = None
# Sentinel payload `producer` value written by iteration-close.sh do_state_update
# (the non-recurring producer). recurring-close.sh writes NO producer field, and
# pre- sentinels have none either -- both correctly fall through to the
# bounded MAX_BG_CLOSE_DURATION_MIN path, so this is backward-compatible.
NONRECURRING_PRODUCER = "nonrecurring-state-update"

#  (echo's addendum, cc-03, 2026-08-05). SECOND, INDEPENDENT failure
# mode of `check`, and the one the close_id fix this goal originally prescribed
# does NOT reach: the fire HAPPENED and was NEVER RECORDED, so every window in
# the world returns 'fire' — a correct verdict about the RECORD and a wrong one
# about REALITY. Measured: sentinel for  consumed at 05:31 with
# spark_fired_session holding no  key at all, while the execution diary
# carried ` | phase-6-spark` phase_start AND phase_end at 04:53. The
# write-side probe read the same absent key, so BOTH sides agreed and both were
# wrong. Cost: one full redundant Phase-6 re-fire, which would have
# double-counted times_asked across all 20 active spark questions into the
# yield_rate denominators that drive the retire/promote review.
#
# WHY A RECORD GOES MISSING: it is written by aspirations-spark Step 0.5 — one
# bash line at the top of a long LLM-executed skill. Compliance measured 3 of 4
# on the observed session. A missed RECORD and a missed FIRE are indistinguishable
# at this check and demand OPPOSITE actions, so no amount of care on the WM side
# can decide it; only a signal with a DIFFERENT WRITER can.
#
# The execution diary is that signal: script-written (execution-diary.sh, not the
# LLM), keyed by goal_id, already on disk. This is the phase value its rows carry
# for a Phase-6 spark.
DIARY_PHASE = "phase-6-spark"


def _parse_dt(value):
    """Parse an ISO timestamp; return None on any failure (guard-420 pattern:
    a bad timestamp must never raise — it degrades to 'not recently fired')."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def recently_fired(fired_map, goal_id, now, window_minutes=DEFAULT_WINDOW_MIN):
    """True iff goal_id was recorded within window_minutes before `now`.

    Fail-safe: a non-dict map, a missing entry, an unparseable timestamp, or a
    future/negative delta all return False (fire), never True — the dedup must
    never suppress a spark on bad data.
    """
    if not isinstance(fired_map, dict):
        return False
    ts = _parse_dt(fired_map.get(goal_id))
    if ts is None:
        return False
    delta = (now - ts).total_seconds()
    # Negative delta (clock skew / future stamp) is NOT "recent" — fire.
    return 0 <= delta < window_minutes * 60


def fired_in_consumption_window(fired_map, goal_id, set_at,
                                lookback_minutes=MAX_BG_CLOSE_DURATION_MIN,
                                lookahead_minutes=DEFAULT_WINDOW_MIN):
    """Consumption-based dedup ( / rb-1674; window widened by
    g-306-80 / rb-2615). True iff goal_id was recorded within the consumption
    window [set_at - lookback_minutes, set_at + lookahead_minutes] bracketing
    the sentinel's creation time `set_at`.

    A spark fired IN RESPONSE to this close lands inside the window on either
    side of set_at:
      - NORMAL path: the close writes set_at, THEN the LLM fires → fired_at >=
        set_at (the original g-115-1404 case).
      - PROACTIVE path (g-306-80): the LLM fires off recurring-close.sh's stdout
        imperative WHILE the bg close is still writing set_at → fired_at can be
        up to ~MAX_BG_CLOSE_DURATION_MIN BEFORE set_at. The strict `>= set_at`
        test mis-read this as a prior close and false-fired (incident g-115-399:
        fire 01:55:14 vs set_at 02:01:25). The lower bound now catches it.
    A spark from a genuine PREVIOUS close lands well before the lower bound
    (recurring intervals are hours) or has no entry at all → fire. The upper
    bound (set_at + TTL) keeps the match scoped to the sentinel's lifetime; a
    far-future fire (clock skew / a stale entry beyond the sentinel's life) does
    not count as this close's consumption.

    A lookback_minutes of None (UNBOUNDED_LOOKBACK) drops the lower bound
    entirely — used for the NON-recurring producer, where a goal closes exactly
    once so any recorded fire IS this close's (g-115-3351; see the constant's
    comment for why no finite bound is calibratable there).

    Fail-safe: a non-dict map, a None set_at, a missing entry, or an unparseable
    fired timestamp all return False (fire), never True — the dedup must never
    suppress a spark on bad data (a redundant fire is the safe failure
    direction; a suppressed legitimate spark loses learning).
    """
    if not isinstance(fired_map, dict) or set_at is None:
        return False
    ts = _parse_dt(fired_map.get(goal_id))
    if ts is None:
        return False
    hi = set_at + timedelta(minutes=lookahead_minutes)
    if lookback_minutes is None:
        return ts <= hi
    lo = set_at - timedelta(minutes=lookback_minutes)
    return lo <= ts <= hi


def diary_fired_at(lines, goal_id, phase=DIARY_PHASE):
    """Latest timestamp at which the execution diary records `phase` for
    goal_id, or None when there is no such row (g-115-4201).

    PURE by the same contract as every other helper here: it takes an ITERABLE
    OF JSONL STRINGS, never a path, so the decision logic is unit-testable with
    no filesystem. The single `open()` lives in the CLI layer below.

    LATEST, not first: a recurring goal can carry markers from several closes,
    and only the most recent one can be THIS close's. The caller then runs it
    through the SAME consumption window as a real record, so a marker from a
    genuine previous close falls outside the window and still fires — the diary
    gets no privilege the WM record does not have.

    Fail-open at every layer, matching this module's asymmetry: a non-string
    line, an unparseable row, a row that is not a dict, a missing/renamed field
    or an unparseable timestamp are all SKIPPED, and an input with nothing
    usable returns None -> no corroboration -> 'fire'. A dedup bug must never
    SUPPRESS a spark.
    """
    latest = None
    for line in lines or ():
        if not isinstance(line, str):
            continue
        stripped = line.strip()
        # Cheap prefilter before the json parse: the diary is a few thousand
        # rows and only a handful ever mention a spark phase.
        if not stripped or goal_id not in stripped or phase not in stripped:
            continue
        try:
            row = json.loads(stripped)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        if row.get("goal_id") != goal_id or row.get("phase") != phase:
            continue
        ts = _parse_dt(row.get("timestamp"))
        if ts is None:
            continue
        if latest is None or ts > latest:
            latest = ts
    return latest


def already_fired_this_close(fired_map, goal_id):
    """WRITE-SIDE dedup (): True iff goal_id has a parseable entry in
    the spark_fired_session map at all — no time comparison of any kind.

    Consulted by iteration-close.sh do_state_update BEFORE it writes the
    pending_phase_6_spark sentinel on the NON-recurring path. When the in-turn
    Phase-6 spark already fired for this goal, the sentinel has nothing to
    trigger, so the correct action is to NOT WRITE IT — which makes the in-turn
    path timing-free and leaves the sentinel doing only its real job (covering
    the bg-timeout path where no in-turn fire happened).

    This is the fix that ELIMINATES the window rather than widening it a fourth
    time. It is sound precisely because a non-recurring goal closes exactly once:
    there is no earlier close of this goal_id whose fire could be mistaken for
    this one. That is why no timestamp is compared here, and why adding one back
    would re-introduce the uncalibratable bound (see UNBOUNDED_LOOKBACK).

    Fail-safe direction is INVERTED relative to the read-side helpers, and the
    inversion is deliberate: here a False (write the sentinel) is the safe
    answer, because a redundant sentinel costs at most one extra spark while a
    wrongly-suppressed sentinel could lose the spark entirely on the bg-timeout
    path. So a non-dict map, a missing entry, or an unparseable timestamp all
    return False → write the sentinel.
    """
    if not isinstance(fired_map, dict):
        return False
    return _parse_dt(fired_map.get(goal_id)) is not None


def gap_seconds(fired_map, goal_id, set_at):
    """Return (set_at - fired_at) in seconds for goal_id, or None when absent /
    unparseable. This is the UNCENSORED variable the field reports could never
    observe: every report of the defect necessarily exceeded the bound, so the
    failures alone can never locate the distribution (g-115-3351's third
    confirmation). Logging this for EVERY non-recurring deep close — including
    the ones that dedup correctly — is what removes the censoring.

    May be negative (fire recorded after set_at), which is the normal shape when
    the LLM sparks after state-update rather than before.
    """
    if not isinstance(fired_map, dict) or set_at is None:
        return None
    ts = _parse_dt(fired_map.get(goal_id))
    if ts is None:
        return None
    return (set_at - ts).total_seconds()


def prune_and_record(fired_map, goal_id, now, prune_minutes=DEFAULT_PRUNE_MIN):
    """Return a NEW map with goal_id=now added and entries older than
    prune_minutes dropped. Bounds the slot's size across a session and discards
    unparseable entries. Does not mutate the input. (wm-set REPLACES the slot,
    so a pruned entry is genuinely gone — verified g-115-1203.)"""
    out = {}
    if isinstance(fired_map, dict):
        for gid, ts_str in fired_map.items():
            ts = _parse_dt(ts_str)
            if ts is None:
                continue  # drop unparseable / stale-shaped entries
            if 0 <= (now - ts).total_seconds() < prune_minutes * 60:
                out[gid] = ts_str
    out[goal_id] = now.isoformat(timespec="seconds")
    return out


def _now():
    return datetime.now()


def _read_diary_lines(path):
    """Read an execution-diary JSONL into a list of lines. The ONLY file read in
    this module.

    NOT a breach of the pure-stdin contract in the module docstring, and the
    distinction matters: that contract exists because a python process must not
    spawn `bash wm-*.sh` (rb-225 / rb-247 — the wm slot getter/setter are DAEMON
    wrappers reached THROUGH bash, and that subprocess hangs on this platform).
    The execution diary is a plain local JSONL file, so reading it needs no
    subprocess at all. The DECISION logic stays pure — `diary_fired_at` takes
    lines, not a path — so nothing about the unit tests changes.

    Reading the file whole is deliberate rather than a tail scan: the largest
    live diary measured across the fleet is 644 KB / 2,766 rows (alpha, cc-07,
    2026-08-28), which is single-digit milliseconds, and a tail bound would be a
    PROXY for "recent" that a chatty iteration can push a relevant marker past.

    Fail-open: a missing path, an unreadable file, or any OS error yields [] ->
    no corroboration -> 'fire'.
    """
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.readlines()
    except OSError:
        return []


def _read_stdin_map():
    """Read the current spark_fired_session map from stdin (JSON). Treats
    empty / 'null' / malformed input as {} — fail-open (the wm slot getter
    prints 'null' for an unset slot)."""
    try:
        raw = sys.stdin.read().strip()
        if not raw or raw == "null":
            return {}
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except Exception:
        return {}


def cmd_record(args):
    fired = _read_stdin_map()
    updated = prune_and_record(fired, args.goal_id, _now())
    # Emit the FULL new map to stdout for the caller to pipe into wm-set.sh
    # (which REPLACES the slot — so the emitted map is authoritative).
    sys.stdout.write(json.dumps(updated))
    return 0


def cmd_check(args):
    fired = _read_stdin_map()
    set_at = _parse_dt(getattr(args, "sentinel_set_at", None))
    # CORROBORATION FOR AN ABSENT RECORD (). Consulted ONLY when the
    # WM map has no parseable entry for this goal — a present record is
    # authoritative and the diary may never override it, in either direction.
    # The diary timestamp is then substituted INTO the map and runs through the
    # unchanged window logic below, so it inherits every producer semantic a
    # real record has: bounded lookback on the recurring path, unbounded on the
    # non-recurring one, upper bound at set_at + TTL. That is what stops a
    # marker from a genuine PREVIOUS close of a recurring goal counting as this
    # close's consumption — the diary gets no privilege the record lacks.
    # Fail-open is UNCHANGED and is the whole safety argument: absent record AND
    # absent diary marker still prints 'fire', exactly as before this existed.
    if _parse_dt(fired.get(args.goal_id)) is None:
        diary_ts = diary_fired_at(
            _read_diary_lines(getattr(args, "diary_file", None)), args.goal_id)
        if diary_ts is not None:
            fired = dict(fired)
            fired[args.goal_id] = diary_ts.isoformat(timespec="seconds")
    if set_at is not None:
        # PRIMARY path (): consumption-based — is the recorded fire
        # inside THIS sentinel's consumption window? The window brackets set_at
        # on both sides (): [set_at - MAX_BG_CLOSE_DURATION_MIN,
        # set_at + TTL], so a proactive fire that lands just BEFORE set_at still
        # counts as this-close consumption. Robust against the bg-timeout
        # wall-clock that broke the 5-min window (rb-1674) AND the proactive
        # before-set_at fire that broke the strict >= test (rb-2615).
        # : the NON-recurring producer gets an UNBOUNDED lower bound.
        # Its gap is the LLM's whole Phase-6 span, which no constant can bound;
        # and it needs no lower bound at all, because a non-recurring goal
        # closes exactly once (no prior close to mis-match). Any other producer
        # — recurring-close.sh, or a pre- sentinel with no producer
        # field — keeps the bounded MAX_BG_CLOSE_DURATION_MIN behavior, which is
        # correct for it: there the lookback models a bounded script runtime.
        producer = (getattr(args, "producer", None) or "").strip()
        lookback = (UNBOUNDED_LOOKBACK if producer == NONRECURRING_PRODUCER
                    else MAX_BG_CLOSE_DURATION_MIN)
        deduped = fired_in_consumption_window(fired, args.goal_id, set_at,
                                              lookback_minutes=lookback)
    else:
        # FALLBACK (no sentinel set_at supplied): time-window heuristic.
        deduped = recently_fired(fired, args.goal_id, _now(), args.window_min)
    if deduped:
        print("skip")  # already fired for this close — caller dedups (clears sentinel, no re-fire)
        return 1
    print("fire")
    return 0


def cmd_fired(args):
    """WRITE-SIDE gate (). stdin = spark_fired_session map.

    Prints `skip-write` when the goal already has a recorded in-turn fire (so
    iteration-close.sh do_state_update must NOT write the pending_phase_6_spark
    sentinel), else `write`. Callers compare the STRING, not the exit code, so
    the decision survives any `set -e` / pipeline-status handling in bash.

    Also prints the measured gap (set_at - fired_at) when --set-at is supplied
    and an entry exists, as `write|skip-write<TAB>gap_seconds`, so the caller can
    emit the uncensored telemetry without a second parse of the map.
    """
    fired = _read_stdin_map()
    already = already_fired_this_close(fired, args.goal_id)
    gap = gap_seconds(fired, args.goal_id, _parse_dt(getattr(args, "set_at", None)))
    verdict = "skip-write" if already else "write"
    sys.stdout.write(verdict if gap is None else f"{verdict}\t{gap:.0f}")
    return 1 if already else 0


def main(argv=None):
    p = argparse.ArgumentParser(description="pending_phase_6_spark fire dedup (g-115-1203)")
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("record", help="stdin=current map JSON; stdout=new map with goal_id stamped")
    pr.add_argument("goal_id")
    pr.set_defaults(func=cmd_record)
    pc = sub.add_parser("check", help="stdin=current map JSON; stdout=fire|skip (exit 1=skip, 0=fire)")
    pc.add_argument("goal_id")
    pc.add_argument("--window-min", type=int, default=DEFAULT_WINDOW_MIN,
                    help="fallback time-window minutes, used only when --sentinel-set-at is absent")
    pc.add_argument("--sentinel-set-at", default=None,
                    help="ISO timestamp of the sentinel's creation (set_at). When supplied, dedup is "
                         "consumption-based instead of the --window-min heuristic: skip iff fired_at "
                         "falls in the window BRACKETING set_at on both sides — "
                         "[set_at - MAX_BG_CLOSE_DURATION_MIN, set_at + DEFAULT_WINDOW_MIN]. The lower "
                         "bound matters: on the non-recurring path the in-turn Phase-6 spark fires "
                         "BEFORE do_state_update writes set_at, so fired_at < set_at is the NORMAL "
                         "shape there and still skips. Robust against bg-timeout wall-clock "
                         "(g-115-1404 / rb-1674) AND the proactive before-set_at fire that the "
                         "retired strict `fired_at >= set_at` test mis-read as a prior close "
                         "(g-306-80 / rb-2615; lower bound widened 10 -> 15 by g-115-2988).")
    pc.add_argument("--producer", default=None,
                    help="sentinel payload's `producer` field. When it is "
                         f"'{NONRECURRING_PRODUCER}' the consumption window's LOWER bound is "
                         "DROPPED (g-115-3351): that path's gap is the LLM's unbounded Phase-6 "
                         "span, and a non-recurring goal closes exactly once so any recorded "
                         "fire is necessarily this close's. Absent/other (recurring-close.sh, "
                         "or a pre-g-115-3351 sentinel) keeps the bounded behavior.")
    pc.add_argument("--diary-file", default=None,
                    help="path to the executing agent's execution-diary.jsonl. Consulted ONLY "
                         "when spark_fired_session has no entry for the goal (g-115-4201): the "
                         "WM record is written by aspirations-spark Step 0.5, which is "
                         "LLM-discretionary, so an ABSENT record cannot distinguish 'Phase 6 "
                         "never ran' from 'Phase 6 ran and did not record it' -- and those demand "
                         "OPPOSITE actions. The diary has a DIFFERENT (script) writer and is keyed "
                         "by goal_id, so a `phase-6-spark` row for this goal decides it. The "
                         "timestamp found is run through the SAME consumption window as a real "
                         "record, so it earns no privilege a record lacks. Omitting the flag, or "
                         "an unreadable file, changes nothing: no corroboration -> 'fire'.")
    pc.set_defaults(func=cmd_check)
    pf = sub.add_parser("fired", help="stdin=current map JSON; stdout=write|skip-write[\\tgap_seconds] "
                                      "(write-side sentinel gate, g-115-3351)")
    pf.add_argument("goal_id")
    pf.add_argument("--set-at", default=None,
                    help="ISO timestamp to measure the gap against (normally now). When supplied "
                         "and an entry exists, the gap in seconds is appended after a TAB so the "
                         "caller can log the UNCENSORED distribution.")
    pf.set_defaults(func=cmd_fired)
    args = p.parse_args(argv)
    try:
        return args.func(args)
    except Exception as e:
        # Fail-open: never block/suppress a spark on a dedup bug. A redundant
        # fire is the safe failure direction; a suppressed legitimate spark
        # loses learning. For `record`, still emit a valid map so the wm-set
        # pipe never receives garbage.
        print(f"[spark-fire-dedup] WARN: {e}", file=sys.stderr)
        cmd = getattr(args, "cmd", None)
        if cmd == "check":
            print("fire")
        elif cmd == "fired":
            # Safe direction is INVERTED here vs `check`: write the sentinel. A
            # redundant sentinel costs one extra spark; a wrongly-suppressed one
            # could lose the spark entirely on the bg-timeout path.
            sys.stdout.write("write")
        elif cmd == "record":
            try:
                sys.stdout.write(json.dumps({args.goal_id: _now().isoformat(timespec="seconds")}))
            except Exception:
                sys.stdout.write("{}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
