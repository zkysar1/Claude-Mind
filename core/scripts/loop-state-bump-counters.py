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

Fail-open: ANY error → exit 0 with stderr WARN. Never blocks iteration-close.

Cross-references:
  - g-283-04 (mirror retirement that introduced the gap)
  - g-283-03 (shape-invariance regression test that passed despite the gap)
  - g-115-664 (this idempotency guard, after bravo session-66 double-bump)
  - recurring-loop-state-mutate.py (sibling single-writer for routine_streaks,
    signals, and _this_session counters)
  - productivity-stop-gate.sh:188-189 (the consumer that reads the stale field)
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    import yaml
    from _paths import AGENT_DIR
    from _fileops import acquire_lock, release_lock
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--outcome", required=True, choices=["routine", "deep"])
    parser.add_argument(
        "--goal-id",
        required=False,
        default=None,
        help=(
            "Optional. Enables idempotency: when supplied, the script refuses to "
            "double-bump if this goal_id is already in loop_state.counted_goals_this_session."
        ),
    )
    args = parser.parse_args()

    if AGENT_DIR is None:
        sys.exit(0)

    wm_path = Path(AGENT_DIR) / "session" / "working-memory.yaml"
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
        try:
            wm = yaml.safe_load(wm_path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            print(
                f"[loop-state-bump-counters] WARN: WM read failed ({e})",
                file=sys.stderr,
            )
            sys.exit(0)

        if not isinstance(wm, dict):
            sys.exit(0)

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
            sys.exit(0)

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
        slots["loop_state"] = loop_state
        wm["slots"] = slots

        # : advance slot_meta.loop_state.updated_at + increment
        # update_count so wm-prune's stale-detection sees this write.
        _update_modified(wm, "loop_state")

        try:
            tmp = wm_path.with_suffix(wm_path.suffix + ".tmp")
            tmp.write_text(yaml.safe_dump(wm, sort_keys=False), encoding="utf-8")
            tmp.replace(wm_path)
        except Exception as e:
            print(
                f"[loop-state-bump-counters] WARN: failed to write WM ({e})",
                file=sys.stderr,
            )
            sys.exit(0)

        print(
            f"[loop-state-bump-counters] outcome={args.outcome} "
            f"goals_completed={goals_completed} productive_goals={productive_goals}",
            file=sys.stderr,
        )
    finally:
        release_lock(lock_path)

    sys.exit(0)


if __name__ == "__main__":
    main()
