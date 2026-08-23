#!/usr/bin/env python3
"""Cadence gate for /aspirations-evolve — the evolution-cadence precheck safety-net.

Exits 0 when evolution SHOULD fire this iteration:
  last_evolution_at_time is unset OR older than
  maintenance_cadence.evolution.hours_cadence, AND the per-session evolution
  cap (global.max_evolutions_per_session) is not yet reached.
Exits 1 on noop OR ANY error (fail-open — the cadence gate must NEVER block the
loop). guard-424: errors print to stderr; they are never silently swallowed.

WHY THIS EXISTS (g-115-2240): the evolution cadence tick lives in Phase 8.8 of
the aspirations loop, which recurring-close.sh bypasses — it wraps only the 4
iteration-close phases (verify/state-update/learning-gate/productivity) then
emits the terminal imperative, so the next iteration re-enters at Phase -1.5 and
Phase 8.7/8.8/9/11 never run. On recurring-heavy sessions (the common
bravo/fleet pattern) Phase 8.8 never fires and evolution STARVES (observed
2026-07-15: last fired ~99h prior vs the 12h cadence). This is the precheck-side
safety net — it fires regardless of close path, mirroring the sibling cadence
sweeps that already run in precheck (fresh-eyes 0.5e, felt-sense 0.5f,
health-regression 0.5h, curriculum 0.5i). Idempotent with Phase 8.8 via the
shared last_evolution_at_time stamp: whichever fires first stamps it (via
aspirations-evolve's mandatory final write), the other sees a fresh stamp and
no-ops.

Invoked from aspirations-precheck Phase 0.5j (deferrable sweep — the budget meter
drops it in the tight zone, honoring maintenance_cadence.evolution.tight_zone_skip
= true, so this script does NOT check the zone itself).

State in:  <agent>/session/working-memory.yaml -> last_evolution_at_time slot
                                               -> loop_state.evolutions
Params in: core/config/aspirations.yaml -> maintenance_cadence.evolution.hours_cadence
           core/config/evolution-triggers.yaml -> global.max_evolutions_per_session
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import _paths  # noqa: E402
import _rt  # noqa: E402  canonical Python -> daemon client (post-cutover; see _rt.py)

CONFIG_PATH = _paths.CONFIG_DIR / "aspirations.yaml"
EVO_TRIGGERS_PATH = _paths.CONFIG_DIR / "evolution-triggers.yaml"

DEFAULT_HOURS_CADENCE = 12
DEFAULT_MAX_EVOLUTIONS = 2


def _warn(msg: str) -> None:
    # guard-424: cadence/precheck scripts fail LOUD (stderr), never silent.
    print(f"evolution-cadence-check: {msg}", file=sys.stderr)


def _load_yaml(path: Path):
    try:
        import yaml  # type: ignore
    except ImportError:
        _warn(f"PyYAML unavailable — cannot read {path.name}; using defaults")
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        _warn(f"yaml parse failed for {path}: {e}")
        return None


def _wm_slot(slot_name: str):
    """Read a WM slot via the daemon. as_json=True preserves the stored JSON
    text (bare ISO string for last_evolution_at_time; dict for loop_state).
    Fail-open to None on any daemon/parse error."""
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


def _parse_iso_epoch(iso):
    """Parse an ISO 'YYYY-MM-DDThh:mm:ss' local-time string to an epoch float.
    None for missing/empty/sentinel/unparseable (mirrors fresh-eyes-cadence
    -check.py._parse_iso_epoch)."""
    if not iso or not isinstance(iso, str):
        return None
    iso = iso.strip()
    if not iso or iso.startswith("0000-"):
        return None
    try:
        return datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").timestamp()
    except (ValueError, TypeError):
        return None


def decide(last_iso, last_epoch, hours_cadence, evolutions, max_evo, now_epoch):
    """Pure decision (unit-tested). Returns (exit_code, stdout_msg, stderr_warn).

    exit_code 0 = fire, 1 = noop. The per-session cap is checked BEFORE the
    cadence (a capped session never fires regardless of staleness). last_epoch
    is the parsed epoch of last_iso, or None when last_iso is unset/unparseable;
    the two args are distinguished so an UNSET stamp fires (evolution never ran)
    while an unparseable (non-empty) stamp fails open to NOOP + a loud warn."""
    if evolutions >= max_evo:
        return (1, f"noop: session cap reached (evolutions={evolutions} >= max={max_evo})", None)
    if not last_iso:
        return (0, "fire: last_evolution_at_time is unset — evolution never stamped", None)
    if last_epoch is None:
        # Unparseable stamp: staleness is indeterminate. Conservative fail-open =
        # NOOP — spuriously firing evolution every iteration on a corrupt stamp
        # is worse than skipping; Phase 8.8 or a corrected stamp will fire it
        # correctly. guard-424: WARN loudly.
        return (1, "", f"unparseable last_evolution_at_time={last_iso!r} — fail-open noop")
    age_hours = (now_epoch - last_epoch) / 3600.0
    if age_hours >= hours_cadence:
        return (
            0,
            f"fire: evolution stale {age_hours:.1f}h >= cadence {hours_cadence:.0f}h "
            f"(last={last_iso}, evolutions={evolutions}/{max_evo})",
            None,
        )
    return (
        1,
        f"noop: evolution fresh {age_hours:.1f}h < cadence {hours_cadence:.0f}h "
        f"(last={last_iso})",
        None,
    )


def main() -> int:
    # --- config (SSOT; fail-open to documented defaults on any read error) ---
    asp = _load_yaml(CONFIG_PATH)
    hours_cadence = DEFAULT_HOURS_CADENCE
    if isinstance(asp, dict):
        mc = (asp.get("maintenance_cadence") or {}).get("evolution") or {}
        hours_cadence = mc.get("hours_cadence", DEFAULT_HOURS_CADENCE)
    try:
        hours_cadence = float(hours_cadence)
    except (TypeError, ValueError):
        hours_cadence = float(DEFAULT_HOURS_CADENCE)

    evo = _load_yaml(EVO_TRIGGERS_PATH)
    max_evo = DEFAULT_MAX_EVOLUTIONS
    if isinstance(evo, dict):
        max_evo = (evo.get("global") or {}).get(
            "max_evolutions_per_session", DEFAULT_MAX_EVOLUTIONS
        )
    try:
        max_evo = int(max_evo)
    except (TypeError, ValueError):
        max_evo = DEFAULT_MAX_EVOLUTIONS

    # --- gather WM state (I/O), then delegate to the pure decide() ---
    loop_state = _wm_slot("loop_state") or {}
    evolutions = 0
    if isinstance(loop_state, dict):
        try:
            evolutions = int(loop_state.get("evolutions", 0) or 0)
        except (TypeError, ValueError):
            evolutions = 0

    last_iso = _wm_slot("last_evolution_at_time")
    if isinstance(last_iso, dict):  # tolerate a {"timestamp": ...} shape
        last_iso = last_iso.get("timestamp")
    last_epoch = _parse_iso_epoch(last_iso) if last_iso else None

    code, msg, warn = decide(
        last_iso,
        last_epoch,
        hours_cadence,
        evolutions,
        max_evo,
        datetime.now().timestamp(),
    )
    if warn:
        _warn(warn)
    if msg:
        print(msg)
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # ultimate fail-open (guard-424: loud, never block)
        _warn(f"unexpected error: {e} — fail-open noop")
        sys.exit(1)
