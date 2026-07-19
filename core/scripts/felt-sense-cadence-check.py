#!/usr/bin/env python3
"""Cadence gate for /felt-sense-checkin.

Exits 0 when the ritual should fire (goal-count since last fire has
reached `goal_cadence`). Exits 1 on any noop or error (fail-open — must
not block the loop).

Invoked from aspirations-precheck Phase 0.5f and from
/felt-sense-checkin Phase 8 (tick-record readout via --print-current).

Pattern mirror of fresh-eyes-cadence-check.py — same mechanism
(goal-count cadence + WM slot), different cadence (75 vs 25) and
different question surface (7-lane structured self-audit vs the
Self-and-portfolio briefing). Kept as a sibling script rather than a
shared one so each ritual can evolve independently.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import _paths  # noqa: E402
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)
from _goal_census import census_completed  # noqa: E402  (B9-deep evicted-goal counts)

CONFIG_PATH = _paths.CONFIG_DIR / "aspirations.yaml"
SLOT_NAME = "last_felt_sense_checkin"


def _load_yaml(path: Path):
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None


def count_completed_goals() -> int:
    """Sum of status=='completed' goals across world+agent aspirations & archive."""
    agent = os.environ.get("MIND_AGENT", "")
    candidates = [
        _paths.WORLD_DIR / "aspirations.jsonl",
        _paths.WORLD_DIR / "aspirations-archive.jsonl",
    ]
    if agent:
        candidates.extend([
            _paths.agent_dir(agent) / "aspirations.jsonl",
            _paths.agent_dir(agent) / "aspirations-archive.jsonl",
        ])
    total = 0
    for p in candidates:
        if not p.exists():
            continue
        try:
            with p.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        asp = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    for g in asp.get("goals", []):
                        if g.get("status") == "completed":
                            total += 1
                    # B9-deep: evicted completed goals live only in the per-status
                    # census now (removed from the goals list) — fold them back so
                    # the cadence count is eviction-invariant.
                    total += census_completed(asp)
        except OSError:
            continue
    return total


def wm_slot_value(slot_name: str = SLOT_NAME):
    """Read `slot_name` via daemon. --json semantics preserved
    (wm_read as_json=True returns the same JSON text the deleted wm.py CLI printed).
    Default slot_name keeps backward-compat with bare wm_slot_value() callers;
    g-115-1054 added the slot_name parameter so the min_session_goals gate can
    also read `loop_state` from this script without a second helper."""
    try:
        raw = _rt.wm_read(slot=slot_name, as_json=True)
    except _rt.RtError:
        return None
    raw = (raw or "").strip()
    if not raw or raw == "null":
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument(
        "--print-current",
        action="store_true",
        help="Print current completed-goals count and exit 0 (skill Phase 8 tick-record uses this).",
    )
    args = ap.parse_args()

    if args.print_current:
        print(count_completed_goals())
        return 0

    cfg = _load_yaml(CONFIG_PATH)
    if cfg is None:
        print("felt-sense-cadence-check: config read failed — noop", file=sys.stderr)
        return 1
    fs = cfg.get("felt_sense") or {}
    if not fs.get("enabled", True):
        print("felt-sense-cadence-check: disabled in config — noop")
        return 1
    goal_cadence = int(fs.get("goal_cadence", 75))

    current = count_completed_goals()
    last = wm_slot_value() or {}
    # Defensive type-guard (2, mirror of l1-skew-check.py:307): a
    # legacy/restored WM slot may hold a bare timestamp string (the
    # pre-dict-migration shape) instead of the {goals_count_at_last_fire: N}
    # dict the recorder writes. Without this, last.get() raises AttributeError
    # at read time -- and because the read crash precedes the dict-write, the
    # slot can never self-heal (self-heal deadlock, rb-2482). Coercing a
    # non-dict to {} routes through the first-fire seed path, which rewrites
    # the correct dict shape.
    if not isinstance(last, dict):
        last = {}
    last_count = int(last.get("goals_count_at_last_fire", 0) or 0)

    # Seed-stagger fix (, mirror of fresh-eyes-cadence-check.py
    # ): when the WM slot is unset (last_count==0) AND
    # `first_fire_offset` is configured, write a staggered seed in-place
    # and return noop this iteration. Next iteration reads the seeded
    # slot via normal cadence math.
    #
    # Rationale: zeta session 64 fired hollow felt-sense Phase 1 (0 goals
    # to sweep) on iteration 1 because the cadence had nothing to anchor
    # against. The  cap below prevents "infinitely overdue"
    # narration but still fires on iter 1, producing a no-op ritual.
    # Seeding pushes the first fire out by `first_fire_offset` goals so
    # there's substantive material to sweep.
    #
    # Backward-compat: when `first_fire_offset` is missing/0, this branch
    # is skipped and the  first-fire cap behavior is preserved
    # (immediate fire on first detection of an unseeded slot).
    first_fire_offset = int(fs.get("first_fire_offset", 0) or 0)
    if last_count == 0 and first_fire_offset > 0 and current >= goal_cadence:
        seed_count = current - goal_cadence + first_fire_offset
        seed_payload = json.dumps({
            "timestamp": "0000-00-00T00:00:00",  # sentinel: ritual never fired
            "goals_count_at_last_fire": seed_count,
            "seeded_for_stagger": True,
            "seeded_offset": first_fire_offset,
        })
        try:
            subprocess.run(
                [sys.executable, str(HERE / "wm.py"), "set", SLOT_NAME],
                input=seed_payload,
                capture_output=True,
                text=True,
                check=True,
            )
            print(
                f"felt-sense-cadence-check: seeded {SLOT_NAME} for stagger "
                f"(offset={first_fire_offset}, seed_count={seed_count}, "
                f"current={current}, cadence={goal_cadence}) — noop this iter"
            )
            return 1  # noop — seed written, next iter reads it normally
        except (subprocess.CalledProcessError, OSError) as exc:
            # Fail-open: if the seed write fails, fall through to the
            # legacy cap behavior so the ritual still fires (better than
            # never firing because of a transient write failure).
            print(
                f"felt-sense-cadence-check: seed write to {SLOT_NAME} failed "
                f"({exc!r}) — falling through to legacy first-fire cap",
                file=sys.stderr,
            )

    diff = current - last_count
    # First-fire normalization (): when the WM slot is unset (last_count==0)
    # but the world has accumulated history, raw diff equals the full goal count
    # (e.g., 2132 vs cadence 75), making the ritual present as infinitely-overdue.
    # Cap at cadence so first-fire reads as "exactly due" — fires once
    # deterministically, doesn't trigger emergency narration.
    #
    # Reached when first_fire_offset is unset/0 (legacy behavior) OR when the
    # seed-write above failed (fail-open).
    if last_count == 0:
        diff = min(diff, goal_cadence)
    if args.verbose:
        print(
            f"felt-sense-cadence-check: current={current} last={last_count} "
            f"diff={diff} cadence={goal_cadence}"
        )
    # Negative-diff self-heal (6 pattern, ported here by 4).
    # A DOWNWARD count-basis correction (census double-count repair, store
    # surgery, archival, count-basis change) leaves the stamped slot ABOVE the
    # live count. Without this branch diff stays negative and the ritual
    # SILENTLY STARVES until the count regrows past the stale stamp.
    #
    # This is not hypothetical: measured 2026-07-14, this slot held
    # goals_count_at_last_fire=5686 against a live count of 5351 (diff=-335),
    # so the 7-lane self-audit needed 335+75=410 MORE completed goals (~8
    # sessions) before it could fire again. It reported `rc=1 noop
    # (diff=-335 < cadence=75)` the whole time — byte-identical to a healthy
    # not-yet-due skip. The ritual whose JOB is noticing drift was the one
    # drifting, and nothing could tell.
    #
    # fresh-eyes-cadence-check.py got this heal (6 per-agent,
    # 1 team-layer); its two siblings (this file, l1-skew-check.py)
    # never did. Same count basis, same failure, no guard.
    #
    # Re-stamp to the current count and NOOP — do NOT fire. Firing here would
    # trade a starved ritual for one that fires on every basis correction
    # (banner fatigue, guard-1090). Preserve the last REAL fire timestamp: a
    # re-baseline is not a fire and must not masquerade as one. Fires at most
    # once per correction; upward jumps and the last_count==0 first-fire path
    # are unaffected (diff >= 0 there).
    if diff < 0:
        # ZERO-GUARD (guard-1091; fresh-eyes-code F-001, 2026-07-14). A FAILED
        # measurement is not a measurement of ZERO. count_completed_goals()
        # above returns 0 as a SILENT FAILURE SENTINEL: every candidate file
        # missing (`if not p.exists(): continue`) or unreadable (`except
        # OSError: continue`) leaves total=0. The world store is S3-backed on
        # an own-cloud deployment, so a mid-sync read miss is routine, not
        # hypothetical.
        #
        # Re-baselining on that 0 would PERSIST the transient error as the new
        # basis (goals_count_at_last_fire=0) and then SPURIOUSLY FIRE next
        # iteration via the last_count==0 first-fire path. Note what that costs:
        # BEFORE this heal existed a transient 0 was HARMLESS — diff<0 =>
        # fire=False => noop => NO WRITE => self-recovering on the next check.
        # The heal must not convert a self-recovering error into permanent
        # state corruption. Noop WITHOUT re-stamping; the next check retries.
        #
        # `current == 0` inside `diff < 0` already implies `last_count > 0`, so
        # this cannot mask a legitimate basis: a real count never falls to zero
        # (it folds in archives + census_completed and is eviction-invariant).
        # A genuinely empty store is not starved either — it self-heals here the
        # moment ONE goal completes (current >= 1 takes the re-baseline below).
        if current == 0:
            print(
                f"felt-sense-cadence-check: negative diff ({diff}) with current=0 "
                f"vs last={last_count} — FAILED MEASUREMENT, not a real basis "
                f"(count_completed_goals returns 0 on read failure); noop WITHOUT "
                f"re-stamp — retries next check",
                file=sys.stderr,
            )
            return 1
        rebase_payload = json.dumps({
            "timestamp": last.get("timestamp", "0000-00-00T00:00:00"),
            "goals_count_at_last_fire": current,
            "rebaselined_from": last_count,
        })
        try:
            subprocess.run(
                [sys.executable, str(HERE / "wm.py"), "set", SLOT_NAME],
                input=rebase_payload,
                capture_output=True,
                text=True,
                check=True,
            )
            print(
                f"felt-sense-cadence-check: negative diff ({diff}) — count basis "
                f"moved backward (last={last_count} > current={current}); "
                f"re-baselined {SLOT_NAME} to {current} — noop this iter"
            )
        except (subprocess.CalledProcessError, OSError) as exc:
            print(
                f"felt-sense-cadence-check: negative-diff re-baseline write "
                f"failed ({exc!r}) — noop without re-stamp; retries next check",
                file=sys.stderr,
            )
        return 1
    if diff >= goal_cadence:
        # 4 min_session_goals gate: world-goal completions tick every
        # agent's cadence counter, but felt-sense lanes 1-6 require firing-
        # agent's session-scoped data (sensory-buffer, recent goals, in-flight
        # signals). When the firing-agent has <min_session_goals completed
        # THIS session, lanes 1-6 produce no-op output anyway; gate the fire
        # so the ritual doesn't burn context on hollow lanes.
        # Fail-open: missing loop_state slot or read error → no-gate behavior.
        min_session_goals = int(fs.get("min_session_goals", 0) or 0)
        if min_session_goals > 0:
            loop_state = wm_slot_value("loop_state") or {}
            session_done = int(loop_state.get("goals_completed_this_session", 0) or 0)
            if session_done < min_session_goals:
                if not args.verbose:
                    print(
                        f"felt-sense-cadence-check: noop "
                        f"(diff={diff}>=cadence={goal_cadence} but min_session_goals gate: "
                        f"session_done={session_done} < min_session_goals={min_session_goals})"
                    )
                return 1
        if not args.verbose:
            print(
                f"felt-sense-cadence-check: fire "
                f"(current={current}, last={last_count}, diff={diff}, cadence={goal_cadence})"
            )
        return 0
    if not args.verbose:
        print(
            f"felt-sense-cadence-check: noop "
            f"(diff={diff} < cadence={goal_cadence})"
        )
    return 1


if __name__ == "__main__":
    # DO NOT REMOVE the bare-except outer guard below. The precheck loop
    # treats exit 1 as "noop, continue" — ANY crash must be converted to
    # exit 1, otherwise a bug in this cadence gate would block the entire
    # aspirations loop. Fail-open is load-bearing here, not defensive fluff.
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"felt-sense-cadence-check: unexpected error: {exc} — noop", file=sys.stderr)
        sys.exit(1)
