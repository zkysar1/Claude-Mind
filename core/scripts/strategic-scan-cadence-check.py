#!/usr/bin/env python3
"""Cadence gate for /aspirations-strategic-scan — the strategic-scan precheck
safety-net (g-115-4691).

Exits 0 when the strategic scan SHOULD fire this iteration:
  last_strategic_scan is unset OR older than strategic_scan.hours_cadence.
Exits 1 on noop OR ANY error (fail-open — the cadence gate must NEVER block the
loop). guard-424: errors print to stderr; they are never silently swallowed.

WHY THIS EXISTS (g-115-4691). Orchestrator Phase 1.5 is an LLM-enumerated
conditional ("IF scan_due (goal_cadence, recurring_settling, OR time_cadence)
THEN Skill(aspirations-strategic-scan)", core/config/aspirations-loop-digest.md
L110-115). Nothing in bash read `last_strategic_scan` for a cadence decision, so
the ritual starved exactly like the six cadences g-115-2984 rescued into the
Phase 0.5e battery — measured 19.5h against a 4h cadence (alpha, cc-04,
2026-08-02). This script is the bash half; _cadence_registry registers it so the
battery runs it in the ONE call that survives a compaction summary.

WHY A PHASE-1.5-LOCAL GATE WOULD NOT HAVE WORKED, measured rather than argued.
The alternative considered was "a bash gate at Phase 1.5 the orchestrator cannot
abbreviate past". A bash call placed INSIDE an LLM-skippable block inherits the
skippability, and the digest already contains the proof: L111/L113 wrap the phase
in `execution-diary.sh phase-start/phase-end phase-1-strategic-scan`, a bash call
whose whole purpose is to witness the phase. On cc-02 (zeta, 2026-08-02) the
stamp was 3.9h fresh — the scan demonstrably ran — and `phase-1-strategic-scan`
appeared 0 times in 178 diary lines. The witness is as skippable as the witnessed.
Only a gate reached from a call the LLM already makes unconditionally can help.

SCOPE — this gate covers the TIME trigger only, and that is deliberate:
  * time_cadence      GATED HERE (strategic_scan.hours_cadence).
  * goal_cadence      NOT gated. `last_strategic_scan` carries no goal-count
                      baseline: its single writer (guard-155,
                      aspirations-strategic-scan Phase S5 via verified-wm-set.sh)
                      emits a bare ISO string, not the
                      {timestamp, goals_count_at_last_fire} dict the fresh-eyes /
                      felt-sense / l1-skew / scar-tissue gates read. Adding it
                      means changing the single-writer surface.
  * recurring_settling NOT gated (strategic_scan.recurring_ratio_trigger) —
                      needs a recurring-ratio over recent completions, which is
                      not a cheap read-only WM lookup.
Both ungated triggers can only make the scan fire SOONER. They cannot starve it,
because any fire stamps the slot and resets every trigger — so the time bound
enforced here is the binding constraint on starvation. Phase 1.5 keeps them.
Stated explicitly per guard-1760: a gate must not let its coverage be mistaken
for the whole predicate.

Idempotent with orchestrator Phase 1.5 via the shared `last_strategic_scan`
stamp — whichever fires first stamps it and the other sees a fresh stamp and
no-ops. Same pairing evolution already uses (Phase 8.8 <-> precheck Phase 0.5j
via last_evolution_at_time).

READ-ONLY on the slot (guard-155): this script never writes `last_strategic_scan`.

State in:  <agent>/session/working-memory.yaml -> last_strategic_scan slot
Params in: core/config/aspirations.yaml -> strategic_scan.hours_cadence
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

DEFAULT_HOURS_CADENCE = 4


def _warn(msg: str) -> None:
    # guard-424: cadence/precheck scripts fail LOUD (stderr), never silent.
    print(f"strategic-scan-cadence-check: {msg}", file=sys.stderr)


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
    text (a bare ISO string for last_strategic_scan). Fail-open to None on any
    daemon/parse error."""
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
    None for missing/empty/sentinel/unparseable (mirrors
    evolution-cadence-check.py._parse_iso_epoch)."""
    if not iso or not isinstance(iso, str):
        return None
    iso = iso.strip()
    if not iso or iso.startswith("0000-"):
        return None
    try:
        return datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").timestamp()
    except (ValueError, TypeError):
        return None


def decide(last_iso, last_epoch, hours_cadence, now_epoch):
    """Pure decision (unit-tested). Returns (exit_code, stdout_msg, stderr_warn).

    exit_code 0 = fire, 1 = noop. last_epoch is the parsed epoch of last_iso, or
    None when last_iso is unset/unparseable; the two args are distinguished so an
    UNSET stamp fires (the scan never ran) while an unparseable (non-empty) stamp
    fails open to NOOP + a loud warn — spuriously re-firing the scan every
    iteration on a corrupt stamp is worse than skipping, and Phase 1.5 or a
    corrected stamp will fire it correctly."""
    if not last_iso:
        return (0, "fire: last_strategic_scan is unset — strategic scan never stamped", None)
    if last_epoch is None:
        return (1, "", f"unparseable last_strategic_scan={last_iso!r} — fail-open noop")
    age_hours = (now_epoch - last_epoch) / 3600.0
    if age_hours >= hours_cadence:
        return (
            0,
            f"fire: strategic scan stale {age_hours:.1f}h >= cadence "
            f"{hours_cadence:.0f}h (last={last_iso})",
            None,
        )
    return (
        1,
        f"noop: strategic scan fresh {age_hours:.1f}h < cadence "
        f"{hours_cadence:.0f}h (last={last_iso})",
        None,
    )


def main() -> int:
    # --- config (SSOT; fail-open to the documented default on any read error) ---
    asp = _load_yaml(CONFIG_PATH)
    hours_cadence = DEFAULT_HOURS_CADENCE
    if isinstance(asp, dict):
        hours_cadence = (asp.get("strategic_scan") or {}).get(
            "hours_cadence", DEFAULT_HOURS_CADENCE
        )
    try:
        hours_cadence = float(hours_cadence)
    except (TypeError, ValueError):
        hours_cadence = float(DEFAULT_HOURS_CADENCE)

    # --- gather WM state (I/O), then delegate to the pure decide() ---
    last_iso = _wm_slot("last_strategic_scan")
    if isinstance(last_iso, dict):  # tolerate a {"timestamp": ...} shape
        last_iso = last_iso.get("timestamp")
    last_epoch = _parse_iso_epoch(last_iso) if last_iso else None

    code, msg, warn = decide(
        last_iso, last_epoch, hours_cadence, datetime.now().timestamp()
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
