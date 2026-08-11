#!/usr/bin/env python3
# domain-leak-exempt: framework recovery infra; phase names are execution-diary literals, not domain terms
"""Wedged-loop detector for recovery-gate Path D (g-328-23).

Reads the bound agent's ``execution-diary.jsonl`` and reports whether the loop
is WEDGED: an ``phase_start`` left unclosed (a phase entered but never exited)
whose age exceeds the wedge threshold.

This is the "heartbeat fresh but no phase progress" signature from the
2026-07-04 own-cloud fleet-wedge incident (g-328-19 failures #4/#5): a loop
wedged at phase-0-precheck behind a ``_fileops.acquire_lock`` exception keeps
re-ticking the DDB heartbeat (FRESH) while diary writes stall behind the wedged
lock, freezing the diary at an old unclosed ``phase_start``. Paths A/C of
recovery-gate BOTH require the heartbeat to be STALE, so a fresh heartbeat
masked the wedge and it required a manual restart.

recovery-gate.sh Path D combines THIS verdict with ``heartbeat == fresh`` (the
discriminator vs the dead-runner Paths A/C) plus the shared execute-in-flight
suppressor, so a genuinely-long ``phase-4-execute`` is never mis-recovered.

Reuses phase-cost-report.py's marker load (``_load_markers``) -- the single
source of truth for execution-diary parsing -- via importlib. Only the LAST
marker is inspected (see ``check_wedge``), so no pairing logic is needed.

Exit codes (recovery-gate gates on these):
  0  = wedged   -> recover
  1  = clean    -> no recovery
  2  = error    -> fail-OPEN to no-recovery (a recovery gate must NEVER flip a
                   healthy agent to IDLE because the wedge check errored).
JSON verdict on stdout in all cases.
"""
import importlib.util
import json
import os
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _paths import AGENT_DIR  # noqa: E402
from _dt import parse_naive_iso  # noqa: E402 -- guard-1398 SSOT, never raises

# Reuse the canonical marker-load (_load_markers) from phase-cost-report.py. The
# filename has a hyphen (not import-safe as a bare module name), so load it by
# path. phase-cost-report.py guards main() under __main__, so exec_module only
# binds its imports + function defs (no side effects).
_PCR_PATH = SCRIPT_DIR / "phase-cost-report.py"
_pcr_spec = importlib.util.spec_from_file_location("phase_cost_report", str(_PCR_PATH))
_pcr = importlib.util.module_from_spec(_pcr_spec)
_pcr_spec.loader.exec_module(_pcr)

# 65, NOT 45 (g-328-25). The configured aspirations.yaml value MUST stay >
# runner_heartbeat.stale_minutes (60): a healthy non-phase-4 phase's local
# runner-heartbeat ages WITH its phase_start (both stamped near the Phase -0.5
# -> Phase 0 boundary, no mid-phase re-tick during active work), so making
# wedge_stale > stale_minutes lets recovery-gate.sh's heartbeat-FRESH gate
# suppress a healthy long phase (its heartbeat is already stale by the time
# phase_start crosses the wedge threshold) while a genuine wedge -- heartbeat
# re-ticked FRESH while the diary freezes -- still fires. At 45 (< 60) a long
# precheck/state-update false-recovered a healthy agent. Guarded by
# test_config_invariant_wedge_exceeds_heartbeat_stale.
DEFAULT_WEDGE_MINUTES = 65.0


def wedge_threshold_minutes():
    """Env override wins; else aspirations.yaml runner_heartbeat.wedge_stale_minutes;
    else DEFAULT_WEDGE_MINUTES. Any error falls back to the default (never raises)."""
    env = os.environ.get("WEDGE_STALE_MINUTES")
    if env:
        try:
            return float(env)
        except (ValueError, TypeError):
            pass
    try:
        import yaml  # local import keeps module load light + optional
        cfg = SCRIPT_DIR.parent / "config" / "aspirations.yaml"
        with open(cfg, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        val = (data.get("runner_heartbeat") or {}).get("wedge_stale_minutes")
        if val is not None:
            return float(val)
    except Exception:
        pass
    return DEFAULT_WEDGE_MINUTES


def last_diary_activity(diary_path, now):
    """Newest CREDIBLE timestamp across ALL entry types in the diary, or None.

    Deliberately NOT ``_load_markers``: that filters to phase_start/phase_end,
    which is the whole reason ordinary progress writes were invisible to this
    detector (g-115-5227). This is the ACTIVITY signal -- any write of any kind
    -- as distinct from the phase-marker signal.

    Entries timestamped AFTER ``now`` are ignored: a future-dated row is not
    evidence that anything is alive, and admitting one permanently disables this
    detector. Found by fresh-eyes review of this same change -- the veto tested
    only the upper bound, so a row dated a day ahead produced
    ``since_min = -1440`` which trivially satisfies ``<= threshold``. The diary
    is ``sync_tier: continuity`` (remote-authoritative, cross-box), so a peer box
    with a skewed clock can write one into a diary whose clock it does not own.

    Parsed via ``_dt.parse_naive_iso`` (guard-1398 SSOT), NOT ``_pcr._parse_ts``,
    and non-dict rows are skipped. Both are the SAME defect as the future-dated
    row above seen one level down -- the value was guarded, the TYPE was not --
    and both were found by a second fresh-eyes pass over this same fix. Measured
    on this box: a tz-AWARE ordinary row made ``ts > now`` raise
    ``TypeError: can't compare offset-naive and offset-aware``, which the caller
    catches as ``liveness_veto: unreadable`` -> clean, so one such row permanently
    suppressed Path D; before this function existed an aware ordinary row was
    never read and the wedge fired normally, so the veto INTRODUCED that
    regression. A bare JSON scalar/array row raised ``AttributeError`` the same
    way. Live diary carries 0 of either across 205 rows (positive-controlled), so
    both were latent, not active.

    The non-dict guard does NOT close that hazard for the script as a whole:
    ``check_wedge`` calls ``_load_markers`` FIRST, and it does a bare
    ``e.get("entry_type")``, so a non-dict row still raises before this function
    is reached (guard-3001 -- a guard cannot protect what runs before it). That
    one is pre-existing, lives in a SHARED loader with other consumers, and is
    filed separately rather than fixed here.

    ``now`` is REQUIRED rather than defaulting: with exactly one production
    caller there is no ergonomic case for a default, and a default here would be
    a fail-open one that silently restores the defect for any future caller that
    omits it (guard-1718).

    Fail-closed-as-suppressed per guard-487: a suppression input that cannot be
    read must not silently disable the suppression. Callers treat None as
    "unknown" and the caller here treats unknown as liveness-present, which is
    also this script's documented fail-open-to-no-recovery contract, so the two
    rules agree rather than conflict.
    """
    diary_path = Path(diary_path)
    if not diary_path.exists():
        return None
    newest = None
    with open(diary_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(e, dict):
                continue          # a bare JSON scalar/array is not a diary entry
            ts = parse_naive_iso(e.get("timestamp"))
            if ts is None or ts > now:
                continue          # unparseable, or future-dated -> not credible
            if newest is None or ts > newest:
                newest = ts
    return newest


def check_wedge(diary_path, now, threshold_minutes):
    """Pure detector. Returns a verdict dict. Reads only ``diary_path``.

    A loop is WEDGED when the MOST RECENT phase marker is a ``phase_start`` --
    a phase entered with nothing logged after it -- that has been open longer
    than ``threshold_minutes`` AND no diary write of ANY kind has landed inside
    that window. That is the "stuck inside a phase" signature of
    the 2026-07-04 own-cloud fleet-wedge: the loop wrote ``phase_start
    phase-0-precheck``, then the ``_fileops.acquire_lock`` exception froze every
    subsequent diary write, leaving that phase_start as the frozen last entry.

    LIVENESS VETO (g-115-5227, measured 2026-08-07 on zeta/cc-02): a recent
    ordinary diary write vetoes the wedge verdict. An unpaired phase-open marker
    stays admissible evidence -- the last-marker design below deliberately needs
    no pairing, and nothing in the loop obliges a close marker -- but it is only
    evidence of a FROZEN diary, so an ordinary write inside the window falsifies
    it directly. This costs nothing the detector currently catches: the
    frozen-diary incident has no such writes BY CONSTRUCTION, and the
    churning-diary wedge (writes succeed, loop re-fails post-start) is already
    scoped out below as a future sibling refinement. It removes a measured false
    positive that flipped a healthy loop to IDLE 70 minutes into a deep goal.
    Same fix shape guard-2507 prescribes: test ACTIVITY, not the entry artifact.

    Using the LAST marker (NOT the oldest unclosed one) is load-bearing: the
    execution-diary accumulates historically-unclosed ``phase_start`` records
    across autocompact / early-return boundaries (20+ is normal on a
    long-running agent), so the OLDEST unclosed start is almost always ancient
    and would false-positive every call (verified against a live diary during
    g-328-23). Only the CURRENT (last) unclosed phase reflects a live wedge. A
    last marker that is a ``phase_end`` means the loop closed its most recent
    phase and is progressing -> clean.

    Scope note: this detects the FROZEN-diary wedge (writes blocked, last entry
    is a stale phase_start), which is the documented incident. A wedge where the
    diary keeps CHURNING fresh phase_starts with no phase_ends (writes succeed
    but the loop re-fails post-start) is a distinct shape left to a future
    sibling refinement.
    """
    diary_path = Path(diary_path)
    markers = _pcr._load_markers(diary_path)
    if not markers:
        return {"verdict": "clean", "reason": "no phase markers"}
    last = markers[-1]
    if last.get("entry_type") != "phase_start":
        return {
            "verdict": "clean",
            "reason": "last diary marker is a phase_end -- loop progressed past its most recent phase",
            "last_phase": last.get("phase", ""),
        }
    age_min = (now - last["_ts"]).total_seconds() / 60.0
    wedged = age_min > threshold_minutes
    if wedged:
        # Liveness veto — see the docstring. Any diary write inside the window
        # falsifies "the diary is frozen". Unreadable/absent activity is treated
        # as liveness present (suppress), matching both guard-487's
        # fail-closed-for-suppressions direction and this script's
        # never-recover-on-uncertainty contract.
        try:
            newest = last_diary_activity(diary_path, now)
        except Exception as e:  # noqa: BLE001 -- suppress on unreadable input
            return {
                "verdict": "clean",
                "stuck_phase": last.get("phase", ""),
                "stuck_goal_id": last.get("goal_id"),
                "age_minutes": round(age_min, 2),
                "threshold_minutes": threshold_minutes,
                "liveness_veto": "unreadable",
                "reason": "wedge age exceeded but the activity read failed (%s) -- suppressing, a recovery gate must not flip a healthy agent to IDLE on an unreadable input" % e,
            }
        if newest is not None:
            since_min = (now - newest).total_seconds() / 60.0
            if since_min <= threshold_minutes:
                return {
                    "verdict": "clean",
                    "stuck_phase": last.get("phase", ""),
                    "stuck_goal_id": last.get("goal_id"),
                    "age_minutes": round(age_min, 2),
                    "threshold_minutes": threshold_minutes,
                    "liveness_veto": "recent_diary_write",
                    "minutes_since_last_write": round(since_min, 2),
                    "reason": (
                        "phase '%s' open %.1fmin (> %smin) BUT a diary write landed %.1fmin ago "
                        "-- the diary is not frozen, so the unclosed marker is not wedge evidence (g-115-5227)"
                        % (last.get("phase", ""), age_min, threshold_minutes, since_min)
                    ),
                }
    return {
        "verdict": "wedged" if wedged else "clean",
        "stuck_phase": last.get("phase", ""),
        "stuck_goal_id": last.get("goal_id"),
        "age_minutes": round(age_min, 2),
        "threshold_minutes": threshold_minutes,
        "reason": (
            "most recent phase '%s' open %.1fmin with no phase_end (> %smin threshold)"
            % (last.get("phase", ""), age_min, threshold_minutes)
        ) if wedged else (
            "most recent phase '%s' open %.1fmin (within %smin threshold)"
            % (last.get("phase", ""), age_min, threshold_minutes)
        ),
    }


def main():
    try:
        override = os.environ.get("WEDGE_DIARY_PATH")
        if override:
            diary_path = Path(override)
        elif AGENT_DIR:
            diary_path = Path(AGENT_DIR) / "session" / "execution-diary.jsonl"
        else:
            print(json.dumps({"verdict": "clean", "reason": "no AGENT_DIR bound"}))
            return 1
        result = check_wedge(diary_path, datetime.now(), wedge_threshold_minutes())
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["verdict"] == "wedged" else 1
    except Exception as e:  # noqa: BLE001 -- fail-open to no-recovery
        print(json.dumps({"verdict": "clean", "reason": "error: %s" % e}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
