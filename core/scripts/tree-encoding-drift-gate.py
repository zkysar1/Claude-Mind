#!/usr/bin/env python3
"""tree-encoding-drift-gate.py — Phase 8.0.5/8.0.6 bash-enforcement ().

Fires from iteration-close.sh do_state_update. Single writer for
loop_state.signals.goals_since_last_tree_update.

Atomic read-modify-write on working-memory.yaml (direct file I/O — no
subprocess to bash, which fails on Windows because subprocess-spawned bash
has a different MSYS view of the filesystem):
  1. Read loop_state.signals.goals_since_last_tree_update.
  2. Increment by 1.
  3. If counter >= tree_encoding_drift_threshold (config; default 3):
       set force_tree_encoding=true (own slot)
       reset counter to 0
  4. Persist working-memory.yaml.

Fail-open: ANY error → silent exit 0. Never blocks state-update.

Caller: core/scripts/tree-encoding-drift-gate.sh (which iteration-close.sh
wraps with `|| echo WARN`). Replaces LLM-residue Phase 8.0.5/8.0.6 from
core/config/aspirations-loop-digest.md.

Single-writer rationale (rb-428 family): the LLM-residue path drifted
(counter=8, sentinel=false observed iter-101 alpha session 58, 2026-04-25).
Bash is now the only writer. The LLM at LOOP_CONTINUE re-reads loop_state
from WM at Phase -0.5 of the next iteration, picking up bash-modified values.
"""

import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    import yaml
    from _paths import AGENT_DIR, CORE_ROOT
    from _fileops import acquire_lock, release_lock
except Exception:
    sys.exit(0)


def _read_threshold():
    """Read tree_encoding_drift_threshold from aspirations.yaml (default 3)."""
    try:
        cfg_path = Path(CORE_ROOT) / "config" / "aspirations.yaml"
        if not cfg_path.exists():
            return 3
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        v = cfg.get("tree_encoding_drift_threshold", 3)
        return int(v)
    except Exception:
        return 3


def _now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def main():
    if AGENT_DIR is None:
        sys.exit(0)

    # --tree-updated flag (, iter-141): when caller signals tree
    # encoding work happened this state-update, short-circuit to counter=0
    # without setting the sentinel. Without this signal the gate over-fires
    # on iterations whose deep encoding work landed in non-tree stores
    # (rules/board/docs/RB/guards) — those are legitimate encoding work but
    # the gate could not see them before the flag was wired through. The
    # caller is iteration-close.sh:529 which receives --tree-updated from
    # the LLM at iteration-close.sh:105 and was already passing it to
    # state-update-audit.sh at line 509.
    tree_updated = "--tree-updated" in sys.argv[1:]

    wm_path = Path(AGENT_DIR) / "session" / "working-memory.yaml"
    if not wm_path.exists():
        sys.exit(0)

    # Acquire the same advisory lock wm.py uses ( / rb-508). Without
    # this, this gate's read-modify-write races with wm-set.sh writes — two
    # writers can each read the same baseline, mutate independently, and the
    # second commit clobbers the first. Lock path mirrors WM_LOCK_PATH in
    # wm.py: <wm_path>.lock (working-memory.lock as a sibling of WM file).
    lock_path = wm_path.with_suffix(".lock")
    try:
        # stale_seconds=10 mirrors wm.py wm_lock — same WM file, same RMW cadence.
        acquire_lock(lock_path, stale_seconds=10)
    except Exception as e:
        # Lock acquisition failed (timeout / IO error). Fail-open: skip this
        # iteration's drift tick rather than blocking iteration-close.
        print(
            f"[tree-encoding-drift-gate] WARN: lock acquire failed ({e}) — "
            f"skipping drift tick",
            file=sys.stderr,
        )
        sys.exit(0)
    try:
        try:
            wm = yaml.safe_load(wm_path.read_text(encoding="utf-8")) or {}
        except Exception:
            sys.exit(0)

        if not isinstance(wm, dict):
            sys.exit(0)

        slots = wm.get("slots") or {}
        loop_state = slots.get("loop_state")

        # The loop_state slot stores data DIRECTLY (slots.loop_state.<keys>),
        # NOT wrapped in a {value:} envelope. Confirmed by direct inspection
        # 2026-04-25 — wm.py round-trips dicts inline. slot_meta lives in a
        # parallel slot at slots.<name>__meta if at all (most slots omit it).
        if not isinstance(loop_state, dict):
            sys.exit(0)

        signals = loop_state.get("signals")
        if not isinstance(signals, dict):
            sys.exit(0)

        threshold = _read_threshold()
        counter = signals.get("goals_since_last_tree_update", 0)
        try:
            counter = int(counter)
        except (TypeError, ValueError):
            counter = 0

        sentinel_set = False
        short_circuited = False

        if tree_updated:
            # Caller signalled tree encoding work happened this state-update.
            # Reset counter to 0 WITHOUT setting the sentinel — there is no
            # drift to force-correct. Skip the increment entirely. Mirrors
            # the threshold-cross reset path but does not touch
            # slots["force_tree_encoding"]. ()
            counter = 0
            short_circuited = True
        else:
            counter += 1
            if counter >= threshold:
                # Set the force_tree_encoding sentinel in its own slot. Stored as
                # the string "true" to match aspirations-state-update SKILL.md
                # Step 8's `IF force_tree_encoding == "true"` check.
                slots["force_tree_encoding"] = "true"
                # ALSO set force_tree_maintain so the precheck Phase 0-pre
                # consumer fires /tree maintain --backlog. The aspirations-
                # state-update SKILL.md Step 8 consumer for force_tree_encoding
                # only fires if the LLM explicitly invokes that sub-skill;
                # the normal loop path (iteration-close.sh --phase state-update
                # + recurring-close.sh) bypasses it entirely, leaving
                # force_tree_encoding orphaned. Dual-write ensures the forced
                # encoding action ALWAYS fires via the precheck consumer
                # regardless of routing path (, hypothesis
                # 2026-05-13_force-tree-encoding-sentinel-stuck CONFIRMED).
                slots["force_tree_maintain"] = {
                    "triggered_at": _now_iso(),
                    "source": "encoding-drift",
                    "threshold": threshold,
                }
                counter = 0
                sentinel_set = True

        signals["goals_since_last_tree_update"] = counter
        loop_state["signals"] = signals
        slots["loop_state"] = loop_state
        wm["slots"] = slots

        try:
            tmp = wm_path.with_suffix(wm_path.suffix + ".tmp")
            tmp.write_text(yaml.safe_dump(wm, sort_keys=False), encoding="utf-8")
            tmp.replace(wm_path)
        except Exception as e:
            print(
                f"[tree-encoding-drift-gate] WARN: failed to write WM ({e})",
                file=sys.stderr,
            )
            sys.exit(0)
    finally:
        release_lock(lock_path)

    if sentinel_set:
        print(
            f"[tree-encoding-drift-gate] threshold {threshold} crossed — "
            f"force_tree_encoding=true + force_tree_maintain set, counter reset to 0",
            file=sys.stderr,
        )
    elif short_circuited:
        print(
            f"[tree-encoding-drift-gate] --tree-updated short-circuit — "
            f"counter reset to 0, sentinel not set (g-115-282)",
            file=sys.stderr,
        )

    sys.exit(0)


if __name__ == "__main__":
    main()
