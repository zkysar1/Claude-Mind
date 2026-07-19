#!/usr/bin/env python3
"""dry-idle-tick.py -- single-writer tick for loop_state.signals.dry_idle (4-c, Layer 3).

Wires the Layer-2 pure functions (_dry_idle.py) into the two dry-state call
sites: the loop digest's Phase-2 goal-is-None branch (quiescence_decision=na)
and the all-blocked B7 backoff entry (quiescence_decision=denied). One tick
per dry evaluation: it decides dry vs not, applies the streak transition
(including the interlude reset below), persists the sub-slot under the SAME
WM lock + CAS RMW discipline as loop-state-bump-counters.py, and prints one
JSON line the pseudocode branches on.

Interlude reset (criterion 3, reset_on_executable): the tick's call sites are
dry-branch-only, so _dry_idle.advance_streak's is_dry=False reset path never
fires organically -- a streak of 5 would survive a 20-goal productive
interlude and resume at 3840s. The tick detects the interlude itself: when
any loop_state.goals_completed_this_session item carries an _item_ts NEWER
than prev.last_dry_at, executable work happened since the last dry cycle, so
the streak is reset (next_dry_signals with is_dry=False) BEFORE the current
dry advance. Board/user-msg wakes that produce no work intentionally do NOT
reset (criterion 4's fast-response intent is served by the wake itself --
interruptible-sleep exits 2 immediately; the streak only governs the NEXT
sleep length if the queue is still dry. Resetting on workless partner chatter
would pin the curve at base_seconds forever -- the g-001-213/g-001-222 noise
class).

Fail-open contract: ANY error prints {"dry": false, ...} and exits 0. The
safe failure direction is the legacy hot re-entry (spin), never a wrong or
unbounded sleep. A tick bug must not be able to freeze the loop.

Output JSON (single line):
  {"dry": bool, "enabled": bool, "streak": int, "sleep_seconds": int,
   "at_cap": bool, "cap_cycles": int, "stop_after_cap_cycles": int|null}
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _fail_open(msg, enabled=True):
    print(json.dumps({"dry": False, "enabled": enabled, "error": msg}))
    return 0


def _interlude_happened(wm, prev):
    """True iff a goal completed AFTER the last dry cycle (executable
    interlude) -- the reset_on_executable trigger for streak reset.

    Reads the TOP-LEVEL canonical `goals_completed_this_session` LIST (each item
    a dict carrying `_item_ts`), NOT `loop_state.goals_completed_this_session` --
    the latter is the INT counter, so `for item in <int>` raised TypeError on the
    2nd+ dry cycle and silently fail-opened to dry:false (g-115-2228). Same
    canonical list read by goal-selector.py:1138, self-drift-gate.py:145,
    session_artifacts_count.py:367, wm-contamination-check.py:209,
    precompact-checkpoint.py:93."""
    last_dry_at = (prev or {}).get("last_dry_at")
    if not last_dry_at:
        return False
    items = wm.get("goals_completed_this_session") or []
    for item in items:
        ts = item.get("_item_ts") if isinstance(item, dict) else None
        if isinstance(ts, str) and ts > last_dry_at:
            return True
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--executable-count", required=True,
                        help="Executable-goal count from the selector (0 = candidate dry state)")
    parser.add_argument("--quiescence-decision", required=True,
                        choices=["approved", "denied", "na"],
                        help="Quiescence gate outcome for this cycle (na = gate did not run)")
    args = parser.parse_args()

    try:
        import _dry_idle
    except Exception as e:  # pragma: no cover -- module ships with this file
        return _fail_open(f"_dry_idle import failed: {e}")

    cfg = _dry_idle.load_config()
    if not cfg.get("enabled", True):
        print(json.dumps({"dry": False, "enabled": False}))
        return 0

    dry = _dry_idle.is_dry_state(args.executable_count, args.quiescence_decision)

    try:
        from _paths import AGENT_DIR
        from _fileops import acquire_lock, release_lock, loop_state_cas_retry, durable_write_text
        import yaml
    except Exception as e:
        return _fail_open(f"import failed: {e}")

    wm_path = Path(AGENT_DIR) / "session" / "working-memory.yaml"
    if not wm_path.exists():
        return _fail_open("working-memory.yaml absent")

    now = _now_iso()
    result = {}

    def _mutate(wm):
        if not isinstance(wm, dict):
            return False
        slots = wm.get("slots") or {}
        loop_state = slots.get("loop_state")
        if not isinstance(loop_state, dict):
            return False  # no loop_state -> nothing to transition (fail-open upstream)
        signals = loop_state.get("signals")
        if not isinstance(signals, dict):
            signals = {}
            loop_state["signals"] = signals
        prev = signals.get("dry_idle")
        if _interlude_happened(wm, prev):
            prev = _dry_idle.next_dry_signals(prev, False, now, cfg)
        new = _dry_idle.next_dry_signals(prev, dry, now, cfg)
        signals["dry_idle"] = new
        result["streak"] = new["streak"]
        result["cap_cycles"] = new["cap_cycles"]
        slots["loop_state"] = loop_state
        wm["slots"] = slots
        # slot_meta freshness -- mirrors loop-state-bump-counters._update_modified
        # (wm-prune stale-detection must not evict loop_state mid-session).
        slot_meta = wm.setdefault("slot_meta", {})
        meta = slot_meta.setdefault("loop_state", {})
        meta["updated_at"] = now
        return True

    lock_path = wm_path.with_suffix(".lock")
    try:
        acquire_lock(lock_path, stale_seconds=10)
    except Exception as e:
        return _fail_open(f"lock acquire failed: {e}")
    try:
        def _read():
            return yaml.safe_load(wm_path.read_text(encoding="utf-8")) or {}

        def _write(wm):
            tmp = wm_path.with_suffix(wm_path.suffix + ".tmp")
            durable_write_text(tmp, yaml.safe_dump(wm, sort_keys=False))  # os.fsync before rename via _fileops.durable_write_text ()
            tmp.replace(wm_path)

        loop_state_cas_retry(_read, _mutate, _write)
    except Exception as e:
        return _fail_open(f"RMW failed: {e}")
    finally:
        release_lock(lock_path)

    if "streak" not in result:
        return _fail_open("loop_state absent -- no transition applied")

    streak = result["streak"]
    sleep_seconds = _dry_idle.dry_sleep_seconds(streak, cfg) if dry else 0

    # Layer 4 (4-d): maintain the dry-cycle short-circuit baseline so
    # consecutive dry re-entries can re-sleep WITHOUT reloading the heavy skill
    # chain (aspirations/SKILL.md Phase -0.5e.0b -> dry-idle-cycle-cache.py).
    # This tick is the SINGLE WRITER of that cache -- the DRY analog of
    # quiescence-gate.py writing quiescence-last-cycle.json on the approved path.
    # On a dry tick write the baseline (goal_count + earliest_wake_at + this
    # sleep, cycle_count=0); on a non-dry tick delete it so a stale baseline can
    # never license a short-circuit after the dry period ends. Fail-soft -- a
    # cache-helper exception is swallowed here so the dry_idle-signal write above
    # (this tick's primary job) still stands.
    try:
        import importlib
        _cache = importlib.import_module("dry-idle-cycle-cache")
        if dry:
            _cache.write_baseline_cache(sleep_seconds, streak, datetime.now())
        else:
            _cache.delete_cache()
    except Exception as e:
        print(f"[dry-idle-tick] cache upkeep skipped: {e}", file=sys.stderr)

    print(json.dumps({
        "dry": dry,
        "enabled": True,
        "streak": streak,
        "sleep_seconds": sleep_seconds,
        "at_cap": _dry_idle.at_cap(streak, cfg) if dry else False,
        "cap_cycles": result["cap_cycles"],
        "stop_after_cap_cycles": cfg.get("stop_after_cap_cycles"),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
