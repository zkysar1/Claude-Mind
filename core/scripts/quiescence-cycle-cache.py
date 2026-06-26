#!/usr/bin/env python3
"""quiescence-cycle-cache.py -- hash-collapse short-circuit for identical
quiescence cycles (g-303-12).

When the autonomous loop quiescence-approves repeatedly under an UNCHANGED
blocker set, each loop re-entry otherwise re-runs the full precheck/select/
all-blocked/quiescence-gate chain only to re-derive the same "all blocked,
approve, sleep" decision -- the alpha session-60 #1 LIVELOCK (~20 wasted
full-reload iterations under a stable blocker set).

This script is the FAST PATH the orchestrator (aspirations/SKILL.md Phase
-0.5e) calls BEFORE idle-tick.sh: it re-validates that the just-approved
quiescence cycle is still valid (same blocker hash, no blocker expired, no
new work, no pending signal) WITHOUT loading the heavy skill chain, and on a
HIT emits a sleep directive so the loop re-sleeps directly.

Contract (mirrors idle-tick.sh):
  Exit 0 + empty stdout : cache MISS -- proceed to idle-tick / full skill.
  Exit 0 + stdout text  : "=== QUIESCENCE CACHE HIT ===" directive -- the
                          caller MUST NOT load the skill; emit the one sleep
                          tool call.

Safety (why a short-circuit cannot strand executable work or mask drift):
  - active_snapshot gate: only fires while loop_state.signals.quiescence
    .active_snapshot is set (a quiescence sleep is genuinely in progress).
    Cheap WM read; avoids the expensive selector during productive work.
  - hash match: re-runs _compute_hash on the LIVE blocker set; any change in
    the blocker set invalidates the cache (the counter resets via a fresh gate
    approval, which rewrites the cache with cycle_count=0).
  - expires_at re-validation (gate C3): if any cached blocker_ref.expires_at
    has passed, a goal may now be executable -- MISS, run the full cycle.
  - newly-arrived-work guard (gate Magic-Wand-2 pattern): more total goals
    than at entry -- MISS.
  - pending blocker-class wake signal -- MISS.
  - cap (default 3): after N consecutive short-circuits, force one full cycle
    so slow drift the cheap checks miss is still caught.

Session-47 reconciliation (guard-147): each short-circuit sleep is capped at
600s (SLEEP_CAP_S), identical to idle-tick.sh's LIGHT_PRECHECK_CAP. So this
fast path re-validates the blocker set at least every 600s and the cap forces
a FULL precheck (a superset of the light-precheck) every 3 cycles -- it does
NOT re-introduce the silent long-backoff-without-health-check that session-47
fixed. The savings vs the status quo is replacing the heavier light-precheck
(completion runners + blocker resolution + capability recheck + digest load)
with this targeted blocker re-validation on the intermediate checkpoints.

The gate (quiescence-gate.py) is the SINGLE WRITER of the cache on approval;
this script only increments cycle_count on a HIT. All paths fail-open (any
error -> MISS -> normal full cycle).
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))
from _paths import AGENT_DIR  # noqa: E402

# MUST equal quiescence-gate.py CYCLE_CACHE_NAME (the single WRITER of this
# file). A drift only causes a fail-open MISS here (the safe direction), never
# a wrong short-circuit.
CACHE_NAME = "quiescence-last-cycle.json"
DEFAULT_CAP = 3
# Cap a single short-circuit sleep at idle-tick.sh's LIGHT_PRECHECK_CAP so the
# blocker set is re-validated at least this often and session-47 health-check
# cadence (guard-147) is preserved.
SLEEP_CAP_S = 600
# While blocked_sleep_until still has more than this many seconds remaining, a
# fresh quiescence sleep is mid-flight -- defer to idle-tick.sh's checkpoint
# logic rather than starting a competing sleep.
DEFER_REMAINING_S = 60

# Blocker-class wake signal files (the subset of interruptible-sleep.sh's set
# that implies the blocker state may have changed). Informational signals
# (board-activity, goal-claim-released) are demoted under QUIESCENCE_SLEEP and
# are intentionally NOT included.
BLOCKER_SIGNAL_FILES = ("blocker-cleared", "pq-resolved", "email-received")


def _cache_path():
    if AGENT_DIR is None:
        return None
    return AGENT_DIR / "session" / CACHE_NAME


def _read_cache():
    p = _cache_path()
    if p is None or not p.exists():
        return None
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _write_cache(cache):
    p = _cache_path()
    if p is None:
        return
    tmp = p.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(cache, ensure_ascii=False, default=str),
                       encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        print(f"[quiescence-cycle-cache] write failed: {e}", file=sys.stderr)


def _pending_blocker_signal():
    if AGENT_DIR is None:
        return False
    sess = AGENT_DIR / "session"
    return any((sess / name).exists() for name in BLOCKER_SIGNAL_FILES)


def _blocked_sleep_remaining(now):
    """Seconds remaining on blocked_sleep_until, or None if unset/unparseable."""
    try:
        import _rt
        raw = _rt.wm_read(slot="blocked_sleep_until")
    except Exception:
        return None
    if raw is None:
        return None
    val = str(raw).strip().strip('"')
    if not val or val == "null":
        return None
    try:
        wake = datetime.fromisoformat(val.rstrip("Z"))
    except (ValueError, TypeError):
        return None
    return (wake - now).total_seconds()


def evaluate_cache(cache, active_snapshot, blocked_remaining, current_hash,
                   current_goal_count, pending_signal, now, cap=DEFAULT_CAP):
    """Pure decision. Returns (decision, reason) where decision is 'hit'|'miss'.

    Ordered cheap-to-expensive so callers can gate the expensive hash recompute:
    when a cheap gate (no cache / not-in-quiescence / blocked-active / cap)
    already decides MISS, the caller may pass current_hash=None and this
    function will still return the right MISS without dereferencing it.
    """
    if cache is None:
        return ("miss", "no-cache")
    if not active_snapshot:
        return ("miss", "not-in-quiescence")
    if blocked_remaining is not None and blocked_remaining > DEFER_REMAINING_S:
        return ("miss", "blocked-sleep-active")
    cycle_count = int(cache.get("cycle_count", 0) or 0)
    if cycle_count >= cap:
        return ("miss", f"cap-reached:{cycle_count}>={cap}")
    if current_hash is None or current_hash != cache.get("blocker_set_hash"):
        return ("miss", "hash-changed")
    for r in (cache.get("blocker_refs") or []):
        exp = (r or {}).get("expires_at")
        if exp:
            try:
                if datetime.fromisoformat(str(exp)) <= now:
                    return ("miss", f"blocker-expired:{(r or {}).get('external_id')}")
            except (ValueError, TypeError):
                return ("miss", "blocker-expiry-unparseable")
    cached_count = cache.get("goal_count")
    if (isinstance(cached_count, int) and isinstance(current_goal_count, int)
            and current_goal_count > cached_count):
        return ("miss", f"new-work:{current_goal_count}>{cached_count}")
    if pending_signal:
        return ("miss", "pending-blocker-signal")
    return ("hit", "ok")


def _emit_hit_directive(cache, cap):
    sleep_seconds = int(cache.get("sleep_seconds") or SLEEP_CAP_S)
    if sleep_seconds > SLEEP_CAP_S or sleep_seconds < 1:
        sleep_seconds = SLEEP_CAP_S
    agent = os.environ.get("MIND_AGENT", "") or "unknown"
    short = int(cache.get("cycle_count", 0) or 0)
    bhash = cache.get("blocker_set_hash", "")
    print(
        "=== QUIESCENCE CACHE HIT ===\n"
        f"Identical quiescence cycle (blocker hash {bhash}, short-circuit {short}/{cap}).\n"
        "Blocker set unchanged, no blocker expired, no new work, no pending signal --\n"
        "skipping the precheck/select/all-blocked reload for this cycle.\n"
        "DO NOT load Skill(aspirations). DO NOT run selection or execution.\n"
        "Emit exactly ONE tool call:\n"
        f"  Bash(\"MIND_AGENT={agent} QUIESCENCE_SLEEP=1 bash core/scripts/interruptible-sleep.sh {sleep_seconds}\", run_in_background=true)\n"
        "When the harness notifies you of its exit, call Skill('aspirations') with args='loop'.\n"
        f"After {cap} consecutive short-circuits a full cycle is forced for drift detection.\n"
        "================="
    )


def cmd_check(_args):
    now = datetime.now()
    cap = int(os.environ.get("QUIESCENCE_CACHE_CAP", DEFAULT_CAP))

    cache = _read_cache()
    if cache is None:
        return 0  # cheap miss -- no cache

    # Cheap gate 1: are we genuinely mid-quiescence? (avoids the selector)
    active_snapshot = None
    try:
        import importlib
        qg = importlib.import_module("quiescence-gate")
        loop_state = qg._wm_read_loop_state()
        active_snapshot = (((loop_state.get("signals") or {})
                            .get("quiescence") or {}).get("active_snapshot"))
    except Exception:
        active_snapshot = None
    if not active_snapshot:
        return 0  # not in a quiescence period -- cheap miss, no selector cost

    # Cheap gate 2: defer to idle-tick while a blocked-sleep timer is live.
    blocked_remaining = _blocked_sleep_remaining(now)
    if blocked_remaining is not None and blocked_remaining > DEFER_REMAINING_S:
        return 0

    # Cheap gate 3: cap reached -- force a full cycle for drift detection.
    if int(cache.get("cycle_count", 0) or 0) >= cap:
        return 0

    # Expensive: recompute the LIVE blocker hash + total goal count.
    current_hash = None
    current_goal_count = None
    try:
        import importlib
        qg = importlib.import_module("quiescence-gate")
        blocked = qg._collect_blocked_entries()
        refs = [e.get("blocker_ref") for e in blocked
                if isinstance(e.get("blocker_ref"), dict)]
        current_hash = qg._compute_hash(refs)
        current_goal_count = qg._total_goal_count()
    except Exception:
        return 0  # fail-open: selector unavailable -> miss -> normal full cycle

    pending_signal = _pending_blocker_signal()
    decision, _reason = evaluate_cache(
        cache, active_snapshot, blocked_remaining, current_hash,
        current_goal_count, pending_signal, now, cap)
    if decision != "hit":
        return 0

    # HIT -- increment the consecutive-short-circuit counter and re-sleep.
    cache["cycle_count"] = int(cache.get("cycle_count", 0) or 0) + 1
    cache["last_hit_at"] = now.isoformat(timespec="seconds")
    _write_cache(cache)
    _emit_hit_directive(cache, cap)
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Quiescence-cycle hash-collapse short-circuit cache (g-303-12)")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("check", help="Evaluate the cache; emit a sleep directive on a hash-matched hit.")
    args = parser.parse_args()
    if args.cmd == "check":
        sys.exit(cmd_check(args))
    parser.print_help()
    sys.exit(0)


if __name__ == "__main__":
    main()
