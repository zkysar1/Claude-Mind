#!/usr/bin/env python3
"""loop-state-bump-counters.py — single-writer for loop_state cross-session counters.

g-283-06 fix: g-283-04 retired the LLM-side wm-set loop_state mirror at
LOOP_CONTINUE. The retirement assumed bash gates already wrote
loop_state.goals_completed and loop_state.productive_goals — but
recurring-loop-state-mutate.py only writes goals_completed_this_session
and productive_goals_this_session (different fields). For non-recurring
goals, no writer existed at all. Result: loop_state.goals_completed
froze at the value of the last LLM-mirror execution, and
productivity-stop-gate.sh (which reads loop_state.goals_completed
directly) silently consumed a stale counter that never advanced.

This script provides the missing writer. Called from iteration-close.sh
do_state_update for every closure (recurring AND non-recurring), it
atomically bumps loop_state.goals_completed and (when outcome=deep)
loop_state.productive_goals.

Invocation:
  loop-state-bump-counters.py --outcome <routine|deep> [--goal-id <id>] [--recurring <true|false>]
  loop-state-bump-counters.py --reset-alignment    # zero alignment_check_at ()
  loop-state-bump-counters.py --evolution-fired     # bump evolutions + stamp last_evolution_at ()

g-115-1785 extension: on the --outcome path WITH --recurring false, this is ALSO
the single writer for the NON-RECURRING signal-mutation streak fields
(routine_streaks[goal.id], signals.routine_streak_global,
signals.routine_count_total, signals.productive_streak,
signals.consecutive_blocked_sleeps reset-on-deep) and the _this_session counters
(goals_completed_this_session, productive_goals_this_session) — Block A/B/D plus
the Block C ceiling RESET. Before this, the digest told the LLM to apply Block
A/B/C/D manually for non-recurring goals, but the LOOP_CONTINUE contract forbids
the LLM from persisting loop_state, so those streaks NEVER advanced (routine_
streak_global reflected recurring closes only) and drifted on any interrupted /
manual close. The streak block rides the SAME idempotency gate + CAS RMW + WM
lock as the goals_completed bump. It is GATED to --recurring false: recurring
goals get the identical mutation from recurring-loop-state-mutate.py (invoked by
recurring-close.sh BEFORE the iteration-close phases), so applying it here too
would double-apply. The Block A/C outcome FLIP is intentionally omitted on this
path (core/config/rationale/signal-mutation.md "Non-recurring path").

Atomic read-modify-write on working-memory.yaml under the same advisory
lock recurring-loop-state-mutate.py uses (wm.yaml.lock, stale_seconds=10).

Idempotency (g-115-664): when --goal-id is supplied, the script maintains
loop_state.counted_goals_this_session (a list) and refuses to bump the
counters more than once per goal_id per session. Background: in bravo
session 66, do_state_update returned rc=127 on first attempt, the retry
succeeded, and this script ran twice for the SAME goal — double-bumping
goals_completed. The orchestrator resets counted_goals_this_session
implicitly on the next session boundary (loop_state is re-initialized
when WM has all-null slots in Phase -1; same lifecycle as goals_completed
and the rest of loop_state). Backward-compat: when --goal-id is omitted
(legacy callers, ad-hoc invocations), the bump remains unconditional so
the orchestrator's existing call sites keep working until they are
migrated.

g-115-1561 extension: this script is ALSO the single writer for four fields that
were orphaned when g-283-04 retired the LLM mirror (no bash writer existed, and
the LLM-side learning-gate overlay was unreliable — zeta's g-115-1557 found
touched=[] universally despite 68-76 goals/agent). On the --outcome path WITH
--goal-id, riding the same idempotency gate, it now ALSO (a) appends the goal's
aspiration id to loop_state.touched (deduped) and (b) increments
loop_state.alignment_check_at. Two new modes cover the resets/stamps that do NOT
happen on a goal close: --reset-alignment zeroes alignment_check_at
(aspirations-select, when the Self-Alignment cadence fires); --evolution-fired
increments loop_state.evolutions and stamps last_evolution_at = goals_completed
(aspirations-evolve, every invocation). Call sites + the full ownership table
live in core/config/aspirations-loop-digest.md.

Fail-open: ANY error → exit 0 with stderr WARN. Never blocks iteration-close.

Cross-references:
  - g-283-04 (mirror retirement that introduced the gap)
  - g-283-03 (shape-invariance regression test that passed despite the gap)
  - g-115-664 (this idempotency guard, after bravo session-66 double-bump)
  - recurring-loop-state-mutate.py (sibling single-writer for routine_streaks,
    signals, and _this_session counters)
  - g-115-1561 (touched / alignment_check_at / evolutions / last_evolution_at
    bash ownership — this extension)
  - g-115-1557 (zeta investigation that scoped the four orphaned fields)
  - g-115-1785 (--recurring false → non-recurring Block A/B/C/D streak ownership,
    closing the last split-brain: streaks had a recurring bash writer but no
    non-recurring one, so the digest's LLM-manual path drifted on interrupted closes)
  - productivity-stop-gate.sh:188-189 (the consumer that reads the stale field)
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    import yaml
    from _paths import AGENT_DIR, CORE_ROOT
    from _fileops import acquire_lock, release_lock, loop_state_cas_retry, durable_write_text
except Exception:
    sys.exit(0)


def _now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _update_modified(wm, slot_name):
    """Mark slot_name as modified — mirrors wm.update_modified semantics.

    g-115-682 fix (from g-115-681 F2): bash single-writers of loop_state used
    to persist slots[loop_state] directly via yaml.safe_dump without advancing
    slot_meta.updated_at. wm-prune's stale-detection then evicted loop_state
    mid-session, defeating F1 protected_slots. Cross-module import of wm.py
    is avoided (CLI-shaped, not a clean lib) — instead inline the 3-line
    contract here. Same pattern in recurring-loop-state-mutate.py.
    """
    slot_meta = wm.setdefault("slot_meta", {})
    root = slot_name.split(".")[0]
    if root not in slot_meta or not isinstance(slot_meta.get(root), dict):
        slot_meta[root] = {"updated_at": None, "accessed_at": None, "update_count": 0}
    m = slot_meta[root]
    m["updated_at"] = _now_iso()
    m["update_count"] = m.get("update_count", 0) + 1


def _to_int(v, default=0):
    if isinstance(v, list):
        return len(v)
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        try:
            return int(v)
        except (ValueError, TypeError):
            return default
    return default


# : non-recurring Block C ceiling read. Mirrors
# recurring-loop-state-mutate.py::_read_global_ceiling but FAIL-SAFE, not
# fail-loud. recurring-loop-state-mutate.py RAISES on config-read failure
# because recurring-close.sh's `|| echo "$ORIGINAL_OUTCOME"` catches the
# non-zero exit; this script has NO such caller — iteration-close.sh invokes it
# fail-open and MUST NOT be blocked by a config glitch on the streak path. So a
# missing/corrupt config falls back to the documented default (5) with a stderr
# WARN (visible, never silent). Config is invariant across the CAS retry, so the
# caller hoists this OUT of the RMW loop (read once).
def _read_global_ceiling(default=5):
    try:
        cfg_path = Path(CORE_ROOT) / "config" / "aspirations.yaml"
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        recurring = cfg.get("recurring") or {}
        return int(recurring.get("routine_streak_global_ceiling", default))
    except Exception as e:
        print(
            f"[loop-state-bump-counters] WARN: could not read "
            f"routine_streak_global_ceiling ({e}) — using default {default}",
            file=sys.stderr,
        )
        return default


# : aspiration_id derivation for the orphaned `touched` accumulator.
import re as _re


def _aspiration_id_from_goal(goal_id):
    """g-NNN-NN[-NN] -> asp-NNN (the aspiration the goal belongs to).

    Returns None when goal_id does not match the g-<asp>-<seq> shape, so a
    malformed/None id never pollutes aspirations_touched_this_session.
    """
    m = _re.match(r"^g-(\d+)-", str(goal_id or ""))
    return f"asp-{m.group(1)}" if m else None


# : bounded read retry for the self-heal verify (see _verify_counted).
# A torn read here correlates with the same OneDrive+daemon write contention that
# causes the original bump no-op, so a single bare read must not fail-open before
# retrying — that defeats the  self-heal exactly when it is needed.
_VERIFY_READ_RETRIES = 4
_VERIFY_READ_BACKOFF_S = 0.05


def _read_wm_with_retry(wm_path):
    """The bounded retry-read shared by --verify-counted and --verify-counted-many.

    Extracted (g-115-8219) so a BATCH membership query costs ONE read instead of
    one process per goal. Behaviour is byte-identical to the loop it replaces --
    same retry count, same backoff, same "any non-dict parse is a torn read"
    predicate -- and test_verify_counted_torn_read_retry.py pins all four of its
    outcomes through the single-goal caller.

    Returns the parsed WM dict, or None when it is still unreadable after every
    retry (genuinely indeterminate, not empty).
    """
    for _attempt in range(_VERIFY_READ_RETRIES):
        try:
            parsed = yaml.safe_load(wm_path.read_text(encoding="utf-8")) or {}
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        if _attempt < _VERIFY_READ_RETRIES - 1:
            time.sleep(_VERIFY_READ_BACKOFF_S)
    return None


def _verify_counted_many(wm_path, goal_ids):
    """Answer N membership questions from ONE read. Prints JSON, returns 0.

    WHY THIS MODE EXISTS: the single-goal --verify-counted is correct and stays
    the contract for iteration-close.sh's per-close self-heal. But an entry-time
    SWEEP asks the same question about every goal a session closed, and paying a
    process spawn per goal made that sweep cost 27.7s for 25 goals (measured,
    cc-08 2026-08-29) -- disproportionate for a list-membership test, and enough
    to get a battery lane quietly dropped. One read, N answers: ~1.1s total.

    Added HERE rather than reimplemented in the caller because the retry
    semantics, the torn-read conservatism and the shape of loop_state are this
    module's to own (guard-2676: a scoped call into the shared component, never
    a transcription of it -- a second copy drifts silently and nothing fails
    when it does).

    Output: {"indeterminate": bool, "counted": [...], "absent": [...]}
    `indeterminate` true means the WM was unreadable, so BOTH lists are empty and
    the caller has learned nothing -- deliberately distinct from "counted: [] and
    absent: [...]", which is a real answer. Collapsing them would let an
    unreadable WM render as a clean sweep, which is the exact failure this whole
    check exists to catch, one level up.
    """
    wm = _read_wm_with_retry(wm_path)
    if not isinstance(wm, dict):
        print(json.dumps({"indeterminate": True, "counted": [], "absent": []}))
        return 0
    loop_state = (wm.get("slots") or {}).get("loop_state")
    counted_raw = loop_state.get("counted_goals_this_session") if isinstance(loop_state, dict) else None
    # A missing loop_state or a missing/!list counted field means NOTHING is
    # counted -- the same verdict the single-goal path returns 1 for. It is a
    # real answer, not an indeterminate one.
    counted = set(counted_raw) if isinstance(counted_raw, list) else set()
    hit = [g for g in goal_ids if g in counted]
    miss = [g for g in goal_ids if g not in counted]
    print(json.dumps({"indeterminate": False, "counted": hit, "absent": miss}))
    return 0


def _verify_counted(wm_path, goal_id):
    """Return the exit code for --verify-counted ( +  retry).

    The bump path below always exits 0 (fail-open at every layer), so a caller
    CANNOT use rc to detect a silent no-op (lock-acquire failure / WM-write
    failure under the OneDrive+daemon background latency of guard-685 /
    g-115-1349). This read-only check lets iteration-close.sh re-read the
    idempotency list and self-heal.

    Returns:
      0 = GOAL_ID is in counted_goals_this_session (bump landed), OR the state
          is indeterminate (WM unreadable / unparseable — a torn read while
          another writer holds the file). Conservative: a transient read glitch
          must NOT trigger a spurious re-fire/ledger entry.
      1 = GOAL_ID is confidently ABSENT — loop_state is present but goal_id is
          not counted, or the counted list / loop_state itself is missing after
          a bump that should have written it. The bump silently no-op'd; the
          caller records it durably and re-fires once.
    """
    # : a torn read here hits the SAME OneDrive+daemon write contention
    # that causes the original bump no-op (correlated failures), so a single bare
    # read failing-open to 0 ("indeterminate -> no re-fire") defeats the self-heal
    # exactly when it is most needed ( permanently lost: backgrounded
    # close -> bump no-op -> verify torn-read -> 0 -> re-fire skipped, no ledger).
    # Mirror the write path's loop_state_cas_retry robustness with a bounded read
    # retry; only a genuinely-unreadable state after all retries falls through to
    # the conservative 0. The 1 (absent) triggers an idempotent re-fire (),
    # consistent with iteration-close.sh's "spurious re-fire is a harmless no-op".
    wm = _read_wm_with_retry(wm_path)
    if not isinstance(wm, dict):
        return 0  # still unreadable after retries -> genuinely indeterminate
    loop_state = (wm.get("slots") or {}).get("loop_state")
    if not isinstance(loop_state, dict):
        return 1  # no loop_state dict after a bump -> bump did not take
    counted = loop_state.get("counted_goals_this_session")
    if not isinstance(counted, list):
        return 1  # idempotency list absent -> goal was not counted
    return 0 if goal_id in counted else 1


# : single-field accumulator ops for the LLM-context events that the
# per-close bump cannot derive — the Self-Alignment cadence firing (reset) and
# an evolution completing (increment). Both are bash writes invoked by the LLM
# at the event, preserving the g-283 single-writer invariant (the LLM provides
# the event signal; the bash gate owns the WM write + enforces the field set).
def _field_op_mutate_factory(op):
    def _mutate(wm):
        if not isinstance(wm, dict):
            return False
        slots = wm.get("slots") or {}
        loop_state = slots.get("loop_state")
        if not isinstance(loop_state, dict):
            from _loop_state_defaults import defaults as _loop_state_defaults
            loop_state = _loop_state_defaults()
        if op == "reset-alignment":
            loop_state["alignment_check_at"] = 0
        elif op == "evolution-fired":
            loop_state["evolutions"] = _to_int(loop_state.get("evolutions", 0)) + 1
            # last_evolution_at marks goals_completed at this evolution. It is
            # vestigial for Phase 8.8 (which reads the separate
            # last_evolution_at_time WM slot) but kept consistent for callers
            # that restore last_evolution_goal_count from loop_state.
            loop_state["last_evolution_at"] = _to_int(loop_state.get("goals_completed", 0))
        else:
            return False
        slots["loop_state"] = loop_state
        wm["slots"] = slots
        _update_modified(wm, "loop_state")
        return True
    return _mutate


def _run_field_op(wm_path, op):
    """Lock + CAS RMW for a single-field accumulator op. Fail-open (exit 0)."""
    if not wm_path.exists():
        return 0
    lock_path = wm_path.with_suffix(".lock")
    try:
        acquire_lock(lock_path, stale_seconds=10)
    except Exception as e:
        print(f"[loop-state-bump-counters] WARN: lock acquire failed for {op} ({e})",
              file=sys.stderr)
        return 0
    try:
        def _read():
            return yaml.safe_load(wm_path.read_text(encoding="utf-8")) or {}

        def _write(wm):
            tmp = wm_path.with_suffix(wm_path.suffix + ".tmp")
            durable_write_text(tmp, yaml.safe_dump(wm, sort_keys=False))  # os.fsync before rename via _fileops.durable_write_text ()
            tmp.replace(wm_path)

        try:
            loop_state_cas_retry(_read, _field_op_mutate_factory(op), _write)
        except Exception as e:
            print(f"[loop-state-bump-counters] WARN: {op} RMW failed ({e})",
                  file=sys.stderr)
        else:
            print(f"[loop-state-bump-counters] {op} applied", file=sys.stderr)
    finally:
        release_lock(lock_path)
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--outcome", required=False, choices=["routine", "deep"])
    parser.add_argument(
        "--goal-id",
        required=False,
        default=None,
        help=(
            "Optional. Enables idempotency: when supplied, the script refuses to "
            "double-bump if this goal_id is already in loop_state.counted_goals_this_session."
        ),
    )
    # : non-recurring signal-mutation ownership. When "false", ALSO
    # apply the Block A/B (streak counters) + Block C ceiling-reset + Block D
    # (_this_session counters) mutation, atomically inside the SAME CAS RMW +
    # idempotency gate as the goals_completed bump. "true"/omitted → SKIP the
    # streak block: recurring goals get it from recurring-loop-state-mutate.py
    # (invoked by recurring-close.sh BEFORE the iteration-close phases), and a
    # double-apply here would corrupt cargo-cult detection. iteration-close.sh
    # passes this ONLY for confirmed non-recurring goals; an unknown/failed
    # recurring lookup omits the flag (fail-safe skip, no corruption).
    parser.add_argument(
        "--recurring",
        required=False,
        default=None,
        choices=["true", "false"],
        help=(
            "Optional. 'false' → also apply the non-recurring Block A/B/C/D "
            "streak mutation (routine_streaks, routine_streak_global, "
            "routine_count_total, productive_streak, consecutive_blocked_sleeps "
            "reset-on-deep, goals_completed_this_session, "
            "productive_goals_this_session). 'true'/omitted → skip (recurring "
            "path owns streaks via recurring-loop-state-mutate.py). g-115-1785."
        ),
    )
    # : read-only verification mode for iteration-close.sh self-heal.
    # Exit 1 == GOAL_ID confidently absent from counted_goals_this_session (the
    # bump silently no-op'd -> caller re-fires); exit 0 == counted or
    # indeterminate. Mutually exclusive with the --outcome bump path.
    parser.add_argument(
        "--verify-counted",
        default=None,
        metavar="GOAL_ID",
        help=(
            "Read-only. Exit 1 if GOAL_ID is confidently absent from "
            "loop_state.counted_goals_this_session; exit 0 if counted or "
            "indeterminate. Does not take the lock or mutate WM."
        ),
    )
    # : batch twin of --verify-counted for an entry-time sweep. Same
    # predicate, same retry-read, ONE process instead of N. Read-only.
    parser.add_argument(
        "--verify-counted-many",
        nargs="+",
        default=None,
        metavar="GOAL_ID",
        help=(
            "Read-only. Print JSON {indeterminate, counted[], absent[]} for every "
            "GOAL_ID against loop_state.counted_goals_this_session, from a single "
            "read. Always exits 0 — the verdict is in the JSON, not the rc, "
            "because N goals have N answers. Does not take the lock or mutate WM."
        ),
    )
    # : LLM-context accumulator events (separate from the --outcome bump).
    parser.add_argument(
        "--reset-alignment",
        action="store_true",
        help=(
            "Zero loop_state.alignment_check_at. Invoked by aspirations-select "
            "when the Self-Alignment cadence fires (bash-owned reset, not an "
            "in-context LLM patch). Mutually exclusive with --outcome."
        ),
    )
    parser.add_argument(
        "--evolution-fired",
        action="store_true",
        help=(
            "Increment loop_state.evolutions (evolutions_this_session) and set "
            "last_evolution_at = goals_completed. Invoked when an evolution "
            "completes (Phase 8.8). Mutually exclusive with --outcome."
        ),
    )
    args = parser.parse_args()

    if AGENT_DIR is None:
        sys.exit(0)

    from wm import wm_path as _resolve_wm_path  # Phase 1A per-Body WM routing ()
    wm_path = _resolve_wm_path()

    # : read-only verify path runs BEFORE the existence guard below —
    # a missing WM file read-throws to the conservative exit 0 (indeterminate).
    if args.verify_counted is not None:
        sys.exit(_verify_counted(wm_path, args.verify_counted))

    if args.verify_counted_many is not None:
        sys.exit(_verify_counted_many(wm_path, args.verify_counted_many))

    # : single-field accumulator ops (mutually exclusive with the bump).
    if args.reset_alignment:
        sys.exit(_run_field_op(wm_path, "reset-alignment"))
    if args.evolution_fired:
        sys.exit(_run_field_op(wm_path, "evolution-fired"))

    if not args.outcome:
        parser.error(
            "--outcome is required unless --verify-counted / --verify-counted-many / "
            "--reset-alignment / "
            "--evolution-fired is given"
        )

    # : hoist the Block C ceiling read OUT of the CAS loop below
    # (config is invariant across the retry, per recurring-loop-state-mutate.py's
    # same hoist). Only the non-recurring streak path needs it.
    global_ceiling = _read_global_ceiling() if args.recurring == "false" else None

    if not wm_path.exists():
        sys.exit(0)

    lock_path = wm_path.with_suffix(".lock")
    try:
        acquire_lock(lock_path, stale_seconds=10)
    except Exception as e:
        print(
            f"[loop-state-bump-counters] WARN: lock acquire failed ({e})",
            file=sys.stderr,
        )
        sys.exit(0)

    try:
        # : the read-modify-write runs inside loop_state_cas_retry,
        # which guards the stale-lock-steal race ( mechanism B) via
        # optimistic concurrency on slot_meta.loop_state.update_count. The
        # closures preserve this writer's exact self-init / idempotency /
        # byte-compat-write behaviour; the helper only adds the token re-read.
        summary = {}

        def _read():
            return yaml.safe_load(wm_path.read_text(encoding="utf-8")) or {}

        def _mutate(wm):
            # Returns True to commit; False for an idempotent no-op (no write).
            if not isinstance(wm, dict):
                return False

            slots = wm.get("slots") or {}
            loop_state = slots.get("loop_state")

            #  (from zeta  investigation): self-initialize when
            # loop_state is null instead of WARN-and-skip. See companion change in
            # recurring-loop-state-mutate.py for full rationale (87 silent skips
            # in zeta's session — counters never advanced because both writers
            # bailed early). Self-init uses the shared DEFAULT_LOOP_STATE shape
            # (mirrors orchestrator's first-iteration init) and proceeds with the
            # counter bump. Backfill intentionally not attempted.
            if not isinstance(loop_state, dict):
                from _loop_state_defaults import defaults as _loop_state_defaults
                print(
                    f"[loop-state-bump-counters] info: loop_state was "
                    f"{type(loop_state).__name__} - initializing to default and proceeding",
                    file=sys.stderr,
                )
                loop_state = _loop_state_defaults()

            # : defensive signals sub-init. When loop_state is a dict but
            # loop_state.signals is None/missing/non-dict, restore the default
            # signals dict. Other writers (recurring-loop-state-mutate.py,
            # quiescence-gate.py) self-heal on their next write, but state-update
            # fires every iteration AND is the only writer for goals_completed/
            # productive_goals — heal here so downstream readers (notably
            # productivity-stop-gate.sh's signals.routine_count_total lookup) see
            # a populated dict rather than a 0-fallback that masks real signal.
            if not isinstance(loop_state.get("signals"), dict):
                from _loop_state_defaults import defaults as _loop_state_defaults
                loop_state["signals"] = _loop_state_defaults()["signals"]
                print(
                    "[loop-state-bump-counters] defensive sub-init: "
                    "loop_state.signals was missing/non-dict — restored from defaults",
                    file=sys.stderr,
                )

            #  idempotency: when --goal-id is supplied, gate the bump on
            # membership in counted_goals_this_session. Backward-compat: --goal-id
            # omitted preserves the original unconditional-bump behavior so
            # in-flight callers (and any ad-hoc invocations) keep working until
            # they are migrated.
            counted_raw = loop_state.get("counted_goals_this_session")
            counted = counted_raw if isinstance(counted_raw, list) else []

            if args.goal_id and args.goal_id in counted:
                print(
                    f"[loop-state-bump-counters] idempotent no-op: "
                    f"goal_id={args.goal_id} already counted this session",
                    file=sys.stderr,
                )
                return False

            goals_completed = _to_int(loop_state.get("goals_completed", 0))
            productive_goals = _to_int(loop_state.get("productive_goals", 0))

            goals_completed += 1
            if args.outcome == "deep":
                productive_goals += 1

            if args.goal_id:
                counted.append(args.goal_id)
                loop_state["counted_goals_this_session"] = counted

            loop_state["goals_completed"] = goals_completed
            loop_state["productive_goals"] = productive_goals

            # : write the LLM-owned accumulators that  orphaned
            # (no bash writer post mirror-retirement, so they froze at defaults
            # for ALL agents on the non-recurring path). These ride the SAME
            # idempotency gate as goals_completed (once per goal_id/session),
            # preserving the g-283 single-writer invariant — bash gates own ALL
            # loop_state writes; the LLM never patches them. Design decision B
            # (vs re-allowing the LLM read-merge-write): keeping ONE writer per
            # field is more robust than split LLM/bash ownership, which is the
            # exact subtlety that produced the original double-count.
            #
            # (a) aspirations_touched_this_session ("touched"): derive the
            #     aspiration_id from the goal_id and add to the set. Fixes the
            #     empty consolidation touched-tracking (impact b).
            if args.goal_id:
                asp_id = _aspiration_id_from_goal(args.goal_id)
                if asp_id:
                    touched_raw = loop_state.get("touched")
                    touched = touched_raw if isinstance(touched_raw, list) else []
                    if asp_id not in touched:
                        touched.append(asp_id)
                    loop_state["touched"] = touched
            # (b) alignment_check_at (goals_since_last_alignment_check):
            #     increment per close. aspirations-select reads this and, when it
            #     fires the Self-Alignment cadence, invokes --reset-alignment to
            #     zero it (a bash write, NOT an in-context LLM patch). Without
            #     this the cadence never fired — frozen at 0, incremented
            #     in-context to 1, never persisted (impact a).
            alignment = _to_int(loop_state.get("alignment_check_at", 0))
            loop_state["alignment_check_at"] = alignment + 1

            # : NON-RECURRING signal-mutation (Block A/B/C/D streaks).
            # Gated to --recurring false: recurring goals get this from
            # recurring-loop-state-mutate.py (recurring-close.sh, BEFORE the
            # iteration-close phases), so applying it here for a recurring goal
            # would DOUBLE-apply and corrupt cargo-cult detection. Rides the SAME
            # idempotency gate (counted_goals_this_session, checked above) + CAS
            # RMW + WM lock as the goals_completed bump — an atomic single-writer
            # for the non-recurring streak fields. Mirrors
            # recurring-loop-state-mutate.py Blocks A/B/D + the Block C ceiling
            # RESET. The Block A/C outcome FLIP (routine->deep reclassification)
            # is INTENTIONALLY omitted here — see
            # core/config/rationale/signal-mutation.md "Non-recurring path:
            # counters without the flip": the flip cannot reach the already-run
            # verify/spark of the current iteration (state-update is Phase 8; they
            # ran at 5/6), and omitting it avoids a verify-vs-counter
            # inconsistency. The ceiling RESET preserves the anti-runaway
            # guarantee; the Phase 0-pre.0b boredom surface warns before the NEXT
            # selection.
            if args.recurring == "false":
                signals = loop_state.get("signals")
                if not isinstance(signals, dict):
                    from _loop_state_defaults import defaults as _lsd_sig
                    signals = _lsd_sig()["signals"]
                routine_streaks = loop_state.get("routine_streaks")
                if not isinstance(routine_streaks, dict):
                    routine_streaks = {}

                oc = args.outcome  # no re-flip on the non-recurring path

                # Block A — per-goal streak. Inert for a true once-goal (never
                # re-closes to reach the flip ceiling), applied for symmetry +
                # robustness if a non-recurring id somehow re-closes.
                if args.goal_id:
                    per_goal = _to_int(routine_streaks.get(args.goal_id, 0))
                    per_goal = per_goal + 1 if oc == "routine" else 0
                    routine_streaks[args.goal_id] = per_goal
                else:
                    per_goal = 0

                # Block B — session signals (uses --outcome directly, no flip).
                rsg = _to_int(signals.get("routine_streak_global", 0))
                rct = _to_int(signals.get("routine_count_total", 0))
                pstreak = _to_int(signals.get("productive_streak", 0))
                cbs = _to_int(signals.get("consecutive_blocked_sleeps", 0))
                if oc == "routine":
                    rsg += 1
                    rct += 1
                    pstreak = 0
                else:  # deep
                    rsg = 0
                    pstreak += 1
                    cbs = 0

                # Block C — global ceiling RESET (anti-runaway; no outcome flip).
                if global_ceiling is not None and rsg >= global_ceiling:
                    rsg = 0

                signals["routine_streak_global"] = rsg
                signals["routine_count_total"] = rct
                signals["productive_streak"] = pstreak
                signals["consecutive_blocked_sleeps"] = cbs
                loop_state["signals"] = signals
                loop_state["routine_streaks"] = routine_streaks

                # Block D — _this_session counters. The digest-line-44/45
                # double-count trap: the digest attributed these to the LLM for
                # non-recurring goals, but the LLM does NOT persist loop_state
                # (LOOP_CONTINUE contract), so they never advanced on this path.
                # Bash owns them now. Advancing them ALSO makes
                # recurring-loop-state-mutate.py's Block C ratio denominator
                # (goals_completed_this_session) count ALL closes, not just
                # recurring ones — its routine ratio was previously skewed.
                gcts = _to_int(loop_state.get("goals_completed_this_session", 0)) + 1
                pgts = _to_int(loop_state.get("productive_goals_this_session", 0))
                if oc == "deep":
                    pgts += 1
                loop_state["goals_completed_this_session"] = gcts
                loop_state["productive_goals_this_session"] = pgts

                summary["nonrecurring_streaks"] = (
                    f"routine_streak_global={rsg} routine_count_total={rct} "
                    f"productive_streak={pstreak} per_goal={per_goal} "
                    f"goals_completed_this_session={gcts}"
                )

            slots["loop_state"] = loop_state
            wm["slots"] = slots

            # : advance slot_meta.loop_state.updated_at + increment
            # update_count so wm-prune's stale-detection sees this write.
            # update_count is ALSO the  CAS token the helper compares.
            _update_modified(wm, "loop_state")
            summary["goals_completed"] = goals_completed
            summary["productive_goals"] = productive_goals
            return True

        def _write(wm):
            tmp = wm_path.with_suffix(wm_path.suffix + ".tmp")
            durable_write_text(tmp, yaml.safe_dump(wm, sort_keys=False))  # os.fsync before rename via _fileops.durable_write_text ()
            tmp.replace(wm_path)

        try:
            cas = loop_state_cas_retry(_read, _mutate, _write)
        except Exception as e:
            print(
                f"[loop-state-bump-counters] WARN: WM read/write failed ({e})",
                file=sys.stderr,
            )
            sys.exit(0)

        if cas.get("noop"):
            sys.exit(0)

        note = ""
        if cas.get("exhausted"):
            note = " (CAS retries exhausted — committed last attempt)"
        elif cas.get("conflicted"):
            note = f" (CAS re-applied after stale-steal, attempts={cas.get('attempts')})"
        streak_note = ""
        if summary.get("nonrecurring_streaks"):
            streak_note = f" nonrecurring-streaks[{summary['nonrecurring_streaks']}]"
        print(
            f"[loop-state-bump-counters] outcome={args.outcome} "
            f"goals_completed={summary.get('goals_completed')} "
            f"productive_goals={summary.get('productive_goals')}{note}{streak_note}",
            file=sys.stderr,
        )
    finally:
        release_lock(lock_path)

    sys.exit(0)


if __name__ == "__main__":
    main()
