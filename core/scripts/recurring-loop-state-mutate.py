#!/usr/bin/env python3
"""recurring-loop-state-mutate.py — single-writer for loop_state during recurring-close.

Magic Wand #1 (alpha session-60 reflection, 2026-05-07): eliminate LLM-side
patching of loop_state after every recurring-close. recurring-close.sh wrote
the goal record (consecutive_routine, lastAchievedAt) but the LLM had to
re-mutate loop_state.signals + routine_streaks + counters on every iteration.
That hand-off was the silent-corruption class flagged in the magic-wand
analysis: bash owns half the bookkeeping, LLM owns the other half, and any
missed manual patch corrupted cargo-cult detection on the next iteration.

This script makes recurring-close the single writer for the full set of
loop_state mutations the recurring path produces. The LLM at LOOP_CONTINUE
re-reads loop_state from WM at Phase -0.5 of the next iteration and picks
up bash-modified values — same pattern tree-encoding-drift-gate established
for goals_since_last_tree_update (rb-428 family, g-248-75).

Invocation:
  recurring-loop-state-mutate.py --goal-id <id> --outcome <routine|deep>

Atomic read-modify-write on working-memory.yaml under the same advisory
lock wm.py uses (sibling .lock file, stale_seconds=10). Applies the four
mutation blocks from core/config/aspirations-loop-digest.md "Phase 4.1
SIGNAL MUTATION":

  Block A — per-goal streak (may flip routine→deep):
    IF outcome == routine:
      routine_streaks[goal.id] += 1
      IF routine_streaks[goal.id] >= cargo_cult_threshold:
        outcome = deep
        routine_streaks[goal.id] = 0
    ELIF outcome == deep:
      routine_streaks[goal.id] = 0

  Block B — session signals (re-reads outcome after Block A):
    IF outcome == routine:
      signals.routine_streak_global += 1
      signals.routine_count_total += 1
      signals.productive_streak = 0
    ELSE (deep):
      signals.routine_streak_global = 0
      signals.productive_streak += 1
      signals.consecutive_blocked_sleeps = 0

  Block C — global + ratio anti-drift (may flip again):
    IF signals.routine_streak_global >= recurring.routine_streak_global_ceiling
       (default 5; was 8 before 2026-05-12):
      outcome = deep
      signals.routine_streak_global = 0
    IF outcome == routine AND goals_completed_this_session >= 6
       AND (signals.routine_count_total / goals_completed_this_session) > 0.80:
      outcome = deep

  Block D — count productive only after all reclassification:
    goals_completed_this_session += 1
    IF outcome == deep:
      productive_goals_this_session += 1

Stdout: post-mutation outcome (routine|deep). recurring-close.sh captures
this and uses it for the iteration-close phase calls so verify/state-update/
learning-gate run with the FINAL (post-flip) outcome class. The Block A flip
is RETROACTIVE per the digest — current iteration is reclassified as deep.

Threshold for Block A flip is the routine-streak ceiling configured at
core/config/aspirations.yaml → recurring.routine_streak_flip_threshold,
default 5. Block A's existing per-goal-streak ceiling and the cargo-cult
detector's consecutive_routine threshold are independent — flip ceiling
governs in-session signal-mutation; cargo-cult threshold governs cross-
session interval calibration.

Fail-open: ANY error → exit 0 with the caller's claimed outcome on stdout.
Never blocks recurring-close. Surfaces stderr noise so the failure isn't
invisible.

Exit codes:
  0  success (post-mutation outcome on stdout, mutations persisted)
  0  error path (caller's claimed outcome on stdout, no mutation)
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    import yaml
    from _paths import AGENT_DIR, CORE_ROOT
    from _fileops import acquire_lock, release_lock, loop_state_cas_retry, durable_write_text
except Exception:
    # Cannot resolve framework — fail-open with no-op echo.
    # We don't know the outcome arg yet; this branch should be unreachable
    # in practice (would mean python3 / yaml / _paths missing entirely).
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
    contract here. Same pattern in loop-state-bump-counters.py.
    """
    slot_meta = wm.setdefault("slot_meta", {})
    root = slot_name.split(".")[0]
    if root not in slot_meta or not isinstance(slot_meta.get(root), dict):
        slot_meta[root] = {"updated_at": None, "accessed_at": None, "update_count": 0}
    m = slot_meta[root]
    m["updated_at"] = _now_iso()
    m["update_count"] = m.get("update_count", 0) + 1


def _read_flip_threshold():
    """Read recurring.routine_streak_flip_threshold (default 5).

    Raises on missing/unreadable aspirations.yaml — propagation is
    intentional. recurring-close.sh's `|| echo "$ORIGINAL_OUTCOME"` catches
    the resulting non-zero exit and falls back to the caller's claimed
    outcome (stderr traceback stays visible). A silent default would mask
    framework-config corruption.
    """
    cfg_path = Path(CORE_ROOT) / "config" / "aspirations.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    recurring = cfg.get("recurring") or {}
    return int(recurring.get("routine_streak_flip_threshold", 5))


def _read_global_ceiling():
    """Read recurring.routine_streak_global_ceiling (default 5).

    Cross-goal session-wide routine-streak ceiling. Distinct from
    per-goal flip_threshold above — that one fires when ONE goal
    repeats routine; this one fires when N goals across the session
    all closed routine. Lowered from 8 → 5 on 2026-05-12 per bravo
    session-66 feedback.

    Raises on missing/unreadable aspirations.yaml (same rationale as
    _read_flip_threshold). Per-key default of 5 is by-design when the
    key is simply absent from a valid YAML.
    """
    cfg_path = Path(CORE_ROOT) / "config" / "aspirations.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    recurring = cfg.get("recurring") or {}
    return int(recurring.get("routine_streak_global_ceiling", 5))


def _to_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--goal-id", required=True)
    parser.add_argument("--outcome", required=True, choices=["routine", "deep"])
    args = parser.parse_args()

    goal_id = args.goal_id
    outcome = args.outcome

    # Default echo: caller's claimed outcome. Updated to post-flip on success.
    final_outcome = outcome

    if AGENT_DIR is None:
        print(final_outcome)
        sys.exit(0)

    from wm import wm_path as _resolve_wm_path  # Phase 1A per-Body WM routing ()
    wm_path = _resolve_wm_path()
    if not wm_path.exists():
        print(final_outcome)
        sys.exit(0)

    lock_path = wm_path.with_suffix(".lock")
    try:
        acquire_lock(lock_path, stale_seconds=10)
    except Exception as e:
        print(
            f"[recurring-loop-state-mutate] WARN: lock acquire failed ({e}) — "
            f"skipping mutations",
            file=sys.stderr,
        )
        print(final_outcome)
        sys.exit(0)

    try:
        # Config reads (static aspirations.yaml, NOT working memory) are hoisted
        # out of the CAS loop below. They RAISE on missing/corrupt config and the
        # exception propagates (release_lock fires via finally) so
        # recurring-close.sh's `|| echo "$ORIGINAL_OUTCOME"` provides the
        # fallback — a silent default would mask framework-config corruption
        # (see _read_flip_threshold). Reading once (not per CAS attempt) is also
        # correct: the config is invariant across the retry.
        flip_threshold = _read_flip_threshold()
        global_ceiling = _read_global_ceiling()

        # : the read-modify-write runs inside loop_state_cas_retry,
        # which guards the stale-lock-steal race ( mechanism B) via
        # optimistic concurrency on slot_meta.loop_state.update_count. On a
        # stale-steal the helper re-reads fresh and re-applies Blocks A-D on the
        # peer's landed counters — the correct serialised result. The closures
        # preserve this writer's exact self-init / Block A-D / byte-compat write.
        result = {}

        def _read():
            return yaml.safe_load(wm_path.read_text(encoding="utf-8")) or {}

        def _mutate(wm):
            # Returns True to commit; False for an idempotent no-op (no write).
            if not isinstance(wm, dict):
                return False

            slots = wm.get("slots") or {}
            loop_state = slots.get("loop_state")

            #  (from zeta  investigation): self-initialize when
            # loop_state is null instead of WARN-and-skip. The prior skip branch
            # silently bypassed all four mutation blocks (A/B/C/D), letting routine
            # closes accumulate without advancing streaks — cargo-cult detector
            # went blind, productivity-stop-gate read stale 0, routine_streak_global
            # never advanced (87 silent skips in zeta's session before discovery).
            # Self-init writes the shared DEFAULT_LOOP_STATE shape (matches the
            # orchestrator's first-iteration init in /aspirations Phase -0.5) and
            # proceeds with mutations against the fresh defaults. Backfill is
            # intentionally NOT attempted — over-counting historical closes would
            # corrupt calibration. One-shot self-init is sufficient; future closes
            # advance counters from the newly-written defaults.
            if not isinstance(loop_state, dict):
                from _loop_state_defaults import defaults as _loop_state_defaults
                print(
                    f"[recurring-loop-state-mutate] info: loop_state was "
                    f"{type(loop_state).__name__} - initializing to default and proceeding",
                    file=sys.stderr,
                )
                loop_state = _loop_state_defaults()

            # Initialize sub-containers if missing. Block A/B/C/D all assume
            # these exist. Pre-existing loop_state may legitimately omit them on
            # the very first iteration of a session before signals were seeded.
            signals = loop_state.get("signals")
            if not isinstance(signals, dict):
                signals = {}
            routine_streaks = loop_state.get("routine_streaks")
            if not isinstance(routine_streaks, dict):
                routine_streaks = {}

            # Local outcome copy — Blocks A/C may flip routine->deep. Kept local
            # (not the outer `outcome`) so a CAS retry recomputes from the
            # caller's claimed outcome against the peer's freshly-read counters.
            oc = outcome

            # ---- Block A — per-goal streak ----
            per_goal_streak = _to_int(routine_streaks.get(goal_id, 0))

            if oc == "routine":
                per_goal_streak += 1
                if per_goal_streak >= flip_threshold:
                    # FLIP — Block B/C/D will see deep
                    oc = "deep"
                    per_goal_streak = 0
            elif oc == "deep":
                # Successful deep clears the per-goal streak — the goal advanced
                # past the routine-only treadmill.
                per_goal_streak = 0
            routine_streaks[goal_id] = per_goal_streak

            # ---- Block B — session signals (re-reads oc) ----
            routine_streak_global = _to_int(signals.get("routine_streak_global", 0))
            routine_count_total = _to_int(signals.get("routine_count_total", 0))
            productive_streak = _to_int(signals.get("productive_streak", 0))
            consecutive_blocked_sleeps = _to_int(
                signals.get("consecutive_blocked_sleeps", 0)
            )

            if oc == "routine":
                routine_streak_global += 1
                routine_count_total += 1
                productive_streak = 0
            else:  # deep
                routine_streak_global = 0
                productive_streak += 1
                # Deep work breaks the blocked-sleep run. Same reset the LLM
                # applied previously (Phase 4.1 Block B per the digest).
                consecutive_blocked_sleeps = 0

            # ---- Block C — global + ratio anti-drift ----
            # Pre-Block-C goals_completed_this_session: pre-INCREMENT value the
            # ratio check uses. Block D increments it. If Block C flips outcome
            # to deep, Block D will count this iteration as productive.
            goals_completed_this_session = _to_int(
                loop_state.get("goals_completed_this_session", 0)
            )
            productive_goals_this_session = _to_int(
                loop_state.get("productive_goals_this_session", 0)
            )

            # 1. Global routine-streak ceiling — when >= ceiling routines in a row
            #    across goals, force deep regardless of per-goal streak. Default 5,
            #    configurable via recurring.routine_streak_global_ceiling. Lowered
            #    from 8 → 5 on 2026-05-12 per bravo session-66 feedback.
            if routine_streak_global >= global_ceiling:
                oc = "deep"
                routine_streak_global = 0

            # 2. Ratio check — only when >=6 goals completed this session AND
            #    >80% of them were routine. The 6-goal floor avoids early-
            #    session false positives where a single deep would yield
            #    routine_count_total/goals_completed = 1.0 trivially.
            if (
                oc == "routine"
                and goals_completed_this_session >= 6
                and routine_count_total > 0.80 * goals_completed_this_session
            ):
                oc = "deep"

            # ---- Block D — count productive AFTER all reclassification ----
            goals_completed_this_session += 1
            if oc == "deep":
                productive_goals_this_session += 1
                # productive_streak handling differs by flip path — INTENTIONAL
                # asymmetry that mirrors the digest spec. Block A flip routes
                # Block B through the deep branch (productive_streak += 1).
                # Block C flip happens AFTER Block B already ran in the routine
                # branch, so productive_streak stays at 0 even though
                # productive_goals advances here. Test case C2 enshrines this
                # asymmetry. Do NOT "fix" it by incrementing productive_streak
                # here — it would double-count when Block A also flipped, and
                # the C1/C2 test expectations would have to flip in tandem.

            # ---- Persist ----
            signals["routine_streak_global"] = routine_streak_global
            signals["routine_count_total"] = routine_count_total
            signals["productive_streak"] = productive_streak
            signals["consecutive_blocked_sleeps"] = consecutive_blocked_sleeps
            loop_state["signals"] = signals
            loop_state["routine_streaks"] = routine_streaks
            loop_state["goals_completed_this_session"] = goals_completed_this_session
            loop_state["productive_goals_this_session"] = productive_goals_this_session
            slots["loop_state"] = loop_state
            wm["slots"] = slots

            # : advance slot_meta.loop_state.updated_at + increment
            # update_count so wm-prune's stale-detection sees this write.
            # update_count is ALSO the  CAS token the helper compares.
            _update_modified(wm, "loop_state")

            result["final_outcome"] = oc
            result["summary"] = (
                f"per_goal_streak={per_goal_streak} "
                f"routine_streak_global={routine_streak_global} "
                f"routine_count_total={routine_count_total} "
                f"productive_streak={productive_streak} "
                f"goals_completed={goals_completed_this_session} "
                f"productive_goals={productive_goals_this_session} "
                f"flip_threshold={flip_threshold}"
            )
            return True

        def _write(wm):
            tmp = wm_path.with_suffix(wm_path.suffix + ".tmp")
            durable_write_text(tmp, yaml.safe_dump(wm, sort_keys=False))  # os.fsync before rename via _fileops.durable_write_text ()
            tmp.replace(wm_path)

        try:
            cas = loop_state_cas_retry(_read, _mutate, _write)
        except Exception as e:
            # WM read/write failure (or unexpected mutate error) -> fail-open with
            # the caller's claimed outcome (module docstring: "ANY error -> exit 0
            # with the caller's claimed outcome"). Config errors are NOT caught
            # here — they were read above the loop and propagate intentionally.
            print(
                f"[recurring-loop-state-mutate] WARN: WM read/write failed ({e})",
                file=sys.stderr,
            )
            cas = {"noop": True}

        if not cas.get("noop"):
            final_outcome = result.get("final_outcome", final_outcome)
            note = ""
            if cas.get("exhausted"):
                note = " cas=exhausted-committed-last"
            elif cas.get("conflicted"):
                note = f" cas=re-applied(attempts={cas.get('attempts')})"
            # Stderr summary so the LLM can see what bash did this iteration.
            print(
                f"[recurring-loop-state-mutate] {goal_id}: "
                f"outcome_in={args.outcome} outcome_out={final_outcome} "
                f"{result.get('summary', '')}{note}",
                file=sys.stderr,
            )
    finally:
        release_lock(lock_path)

    print(final_outcome)
    sys.exit(0)


if __name__ == "__main__":
    main()
