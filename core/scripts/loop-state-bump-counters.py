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
  loop-state-bump-counters.py --outcome <routine|deep> [--goal-id <id>]
  loop-state-bump-counters.py --reset-alignment    # zero alignment_check_at (1)
  loop-state-bump-counters.py --evolution-fired     # bump evolutions + stamp last_evolution_at (1)

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
  - productivity-stop-gate.sh:188-189 (the consumer that reads the stale field)
"""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    import yaml
    from _paths import AGENT_DIR
    from _fileops import acquire_lock, release_lock, loop_state_cas_retry
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


# 1: aspiration_id derivation for the orphaned `touched` accumulator.
import re as _re


def _aspiration_id_from_goal(goal_id):
    """g-NNN-NN[-NN] -> asp-NNN (the aspiration the goal belongs to).

    Returns None when goal_id does not match the g-<asp>-<seq> shape, so a
    malformed/None id never pollutes aspirations_touched_this_session.
    """
    m = _re.match(r"^g-(\d+)-", str(goal_id or ""))
    return f"asp-{m.group(1)}" if m else None


# 5: bounded read retry for the self-heal verify (see _verify_counted).
# A torn read here correlates with the same OneDrive+daemon write contention that
# causes the original bump no-op, so a single bare read must not fail-open before
# retrying — that defeats the 0 self-heal exactly when it is needed.
_VERIFY_READ_RETRIES = 4
_VERIFY_READ_BACKOFF_S = 0.05


def _verify_counted(wm_path, goal_id):
    """Return the exit code for --verify-counted (0 + 5 retry).

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
    # 5: a torn read here hits the SAME OneDrive+daemon write contention
    # that causes the original bump no-op (correlated failures), so a single bare
    # read failing-open to 0 ("indeterminate -> no re-fire") defeats the self-heal
    # exactly when it is most needed (6 permanently lost: backgrounded
    # close -> bump no-op -> verify torn-read -> 0 -> re-fire skipped, no ledger).
    # Mirror the write path's loop_state_cas_retry robustness with a bounded read
    # retry; only a genuinely-unreadable state after all retries falls through to
    # the conservative 0. The 1 (absent) triggers an idempotent re-fire (),
    # consistent with iteration-close.sh's "spurious re-fire is a harmless no-op".
    wm = None
    for _attempt in range(_VERIFY_READ_RETRIES):
        try:
            parsed = yaml.safe_load(wm_path.read_text(encoding="utf-8")) or {}
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            wm = parsed
            break
        if _attempt < _VERIFY_READ_RETRIES - 1:
            time.sleep(_VERIFY_READ_BACKOFF_S)
    if not isinstance(wm, dict):
        return 0  # still unreadable after retries -> genuinely indeterminate
    loop_state = (wm.get("slots") or {}).get("loop_state")
    if not isinstance(loop_state, dict):
        return 1  # no loop_state dict after a bump -> bump did not take
    counted = loop_state.get("counted_goals_this_session")
    if not isinstance(counted, list):
        return 1  # idempotency list absent -> goal was not counted
    return 0 if goal_id in counted else 1


# 1: single-field accumulator ops for the LLM-context events that the
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
            tmp.write_text(yaml.safe_dump(wm, sort_keys=False), encoding="utf-8")
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
    # 0: read-only verification mode for iteration-close.sh self-heal.
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
    # 1: LLM-context accumulator events (separate from the --outcome bump).
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

    # 0: read-only verify path runs BEFORE the existence guard below —
    # a missing WM file read-throws to the conservative exit 0 (indeterminate).
    if args.verify_counted is not None:
        sys.exit(_verify_counted(wm_path, args.verify_counted))

    # 1: single-field accumulator ops (mutually exclusive with the bump).
    if args.reset_alignment:
        sys.exit(_run_field_op(wm_path, "reset-alignment"))
    if args.evolution_fired:
        sys.exit(_run_field_op(wm_path, "evolution-fired"))

    if not args.outcome:
        parser.error(
            "--outcome is required unless --verify-counted / --reset-alignment / "
            "--evolution-fired is given"
        )

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
        # 4: the read-modify-write runs inside loop_state_cas_retry,
        # which guards the stale-lock-steal race (9 mechanism B) via
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

            # 1: write the LLM-owned accumulators that  orphaned
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

            slots["loop_state"] = loop_state
            wm["slots"] = slots

            # : advance slot_meta.loop_state.updated_at + increment
            # update_count so wm-prune's stale-detection sees this write.
            # update_count is ALSO the 4 CAS token the helper compares.
            _update_modified(wm, "loop_state")
            summary["goals_completed"] = goals_completed
            summary["productive_goals"] = productive_goals
            return True

        def _write(wm):
            tmp = wm_path.with_suffix(wm_path.suffix + ".tmp")
            tmp.write_text(yaml.safe_dump(wm, sort_keys=False), encoding="utf-8")
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
        print(
            f"[loop-state-bump-counters] outcome={args.outcome} "
            f"goals_completed={summary.get('goals_completed')} "
            f"productive_goals={summary.get('productive_goals')}{note}",
            file=sys.stderr,
        )
    finally:
        release_lock(lock_path)

    sys.exit(0)


if __name__ == "__main__":
    main()
