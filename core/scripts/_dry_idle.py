#!/usr/bin/env python3
"""_dry_idle.py -- pure-compute core for the dry-idle backoff (-b, Layer 2).

The autonomous loop's DRY state -- zero executable goals AND quiescence
denied/NA -- previously spun via synchronous Skill re-entry (no interruptible
sleep), burning tokens on repeated full-reload cycles under a stable empty
queue. This module provides the PURE decision + curve functions that Layer 3
(g-115-2084-c) wires into an interruptible-sleep terminal, and Layer 4
(g-115-2084-d) mirrors into a fast-path short-circuit cache. It carries NO CLI
and does NO I/O beyond the single fail-open config read -- the loop-survival
risk lives in Layer 3's terminal rewrite, isolated behind this tested logic.

Dry vs quiescence (MUTUALLY EXCLUSIVE, criterion 5):
  - QUIESCENCE: the blocked queue is structurally user-gated (every blocked
    goal carries a valid blocker_ref); the quiescence gate APPROVES a long
    sleep. A legitimate, gated blocked state -- NOT dry.
  - DRY: zero executable goals AND quiescence could NOT approve (denied, or
    not-applicable because no blocker_refs / no active snapshot). This is the
    spin state this backoff addresses.
A state is at most ONE of {executing, quiescent, dry}. When quiescence is
approved, is_dry_state() returns False regardless of executable_count -- the
source of the mutual-exclusion guarantee.

Curve: sleep(streak) = min(base_seconds * multiplier**(streak-1), max_seconds)
  streak 1->120 2->240 3->480 4->960 5->1920 6->3840 7+->7200 (capped)

All functions are PURE. load_config() is the only reader; it fails open to
DEFAULTS when the config is missing/unparseable so a fresh world or a config
typo can never crash the loop.
"""
from pathlib import Path

# Fallback config -- MUST mirror core/config/aspirations.yaml dry_idle_backoff
# (landed by Layer 1, -a). load_config() merges the live block over
# these; DEFAULTS is the fail-open floor when the config is unreadable.
DEFAULTS = {
    "enabled": True,
    "base_seconds": 120,
    "multiplier": 2.0,
    "max_seconds": 7200,
    "budget_pct": 0.90,
    "reset_on_executable": True,
    "stop_after_cap_cycles": None,
    # : max age of a signals.last_all_blocked marker for the DRY-SPIN
    # guard to treat the previous cycle as "just routed all_blocked and wrote no
    # sleep" and short-circuit re-entry. Deliberately equal to the base_seconds
    # DEFAULT (120), not to the live base_seconds: a deployment that raises
    # base_seconds to 7200 for flat 2-hour idle blocks () must not widen
    # this window to 2h, because the window bounds how long a STALE marker can
    # keep firing the guard. Past the gap a genuine long sleep elapsed -> normal
    # entry. See dry-spin-guard.py and guard-4870 (a re-entry watch is valid only
    # against a signal re-measured between the two reads).
    "min_reentry_gap_s": 120,
}

# Quiescence decisions that leave the loop with NO gated sleep -> dry-eligible.
# "approved" is deliberately excluded: an approved quiescence sleep is the
# mutually-exclusive sibling state, never dry.
_DRY_QUIESCENCE = ("denied", "na")


def load_config(config_path=None):
    """Read dry_idle_backoff from core/config/aspirations.yaml, merged over
    DEFAULTS. Fail-open: any missing key / parse error yields DEFAULTS.

    The LIVE read (config_path None) goes through _config_overlay.merged_config,
    so meta/config-overrides.yaml keys `aspirations.dry_idle_backoff.<k>` take
    effect — a single-agent deployment runs flat 2-hour idle blocks
    (base_seconds 7200) while a busy fleet keeps the 2-minute re-check (user
    directive 2026-09-03, g-357-90). An explicit config_path (tests) reads raw.
    """
    cfg = dict(DEFAULTS)
    try:
        import yaml
        block = None
        if config_path is None:
            config_path = (Path(__file__).resolve().parent.parent
                           / "config" / "aspirations.yaml")
            try:
                from _config_overlay import merged_config
                block = (merged_config("aspirations.yaml") or {}).get("dry_idle_backoff")
            except Exception:
                block = None  # overlay unavailable -> raw framework read below
        if not isinstance(block, dict):
            raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
            block = raw.get("dry_idle_backoff") or {}
        for k in DEFAULTS:
            if k in block:
                cfg[k] = block[k]
    except Exception:
        pass  # fail-open to DEFAULTS -- never crash the loop on a config read
    return cfg


def is_dry_state(executable_count, quiescence_decision):
    """True iff the loop is in the DRY spin state: zero executable goals AND
    quiescence denied/NA. Quiescence-approved is NEVER dry (mutual exclusion,
    criterion 5) -- so a caller cannot double-sleep both backoffs on one cycle.

    quiescence_decision: "approved" | "denied" | "na" (na = gate did not run /
    no active snapshot / no blocker_refs to gate).
    """
    if quiescence_decision not in _DRY_QUIESCENCE:
        return False  # approved (or any non-dry decision) -> quiescence owns it
    try:
        return int(executable_count) == 0
    except (TypeError, ValueError):
        return False  # unparseable count -> conservatively NOT dry


def dry_sleep_seconds(streak, config=None):
    """Exponential backoff curve, capped at max_seconds.
    sleep(streak) = min(base_seconds * multiplier**(streak-1), max_seconds).
    streak < 1 is clamped to 1 (the first dry cycle sleeps base_seconds)."""
    cfg = config or DEFAULTS
    s = max(1, int(streak))
    raw = cfg["base_seconds"] * (cfg["multiplier"] ** (s - 1))
    return int(min(raw, cfg["max_seconds"]))


def at_cap(streak, config=None):
    """True iff dry_sleep_seconds(streak) has reached max_seconds (the curve
    has flattened). cap_cycles counts consecutive at-cap dry cycles, which
    Layer 3 compares against stop_after_cap_cycles."""
    cfg = config or DEFAULTS
    return dry_sleep_seconds(streak, cfg) >= cfg["max_seconds"]


def advance_streak(current_streak, is_dry, config=None):
    """Streak transition. On a dry cycle: increment. On a non-dry cycle:
    reset to 0 iff reset_on_executable (default True); else hold the streak."""
    cfg = config or DEFAULTS
    cur = int(current_streak or 0)
    if is_dry:
        return cur + 1
    return 0 if cfg.get("reset_on_executable", True) else cur


def next_dry_signals(prev, is_dry, now_iso, config=None):
    """Pure transition for the loop_state.signals.dry_idle sub-slot. Given the
    prior dict (or None), the current dry observation, and now, return the
    updated dict. Layer 3 persists it; keeping the transition here (pure +
    tested) makes Layer 3 a thin wiring layer.

    Shape (pinned by test_compact_restore_preserves_dry_idle_signals, g-115-2084-a):
      streak / last_dry_at / sleep_total_s / session_start_at / cap_cycles
    """
    cfg = config or DEFAULTS
    prev = prev or {}
    streak = advance_streak(prev.get("streak", 0), is_dry, cfg)
    out = {
        "streak": streak,
        "last_dry_at": now_iso if is_dry else prev.get("last_dry_at"),
        "sleep_total_s": int(prev.get("sleep_total_s", 0) or 0),
        "session_start_at": prev.get("session_start_at") or now_iso,
        "cap_cycles": int(prev.get("cap_cycles", 0) or 0),
    }
    if is_dry:
        out["sleep_total_s"] += dry_sleep_seconds(streak, cfg)
        # cap_cycles = consecutive dry cycles whose sleep hit max_seconds.
        # Resets to 0 the moment a cycle is below cap (it never is once streak
        # passes the cap point, but the reset keeps the semantics honest if
        # base/multiplier/max are retuned).
        out["cap_cycles"] = (out["cap_cycles"] + 1) if at_cap(streak, cfg) else 0
    else:
        # A non-dry (executable) cycle breaks the consecutive dry run, so the
        # consecutive-at-cap counter resets too — mirrors the streak reset in
        # advance_streak. Without this, cap_cycles would carry a stale count
        # across an executable interlude and mislead Layer 3's
        # stop_after_cap_cycles check on the next dry run.
        out["cap_cycles"] = 0
    return out
