#!/usr/bin/env python3
"""Cadence gate for the curriculum re-evaluation ritual ().

Exits 0 when `curriculum-evaluate` should re-run this iteration (>=
`interval_hours` since the last evaluation, or never evaluated). Exits 1 on
noop or error (fail-open — a cadence gate must NEVER block the loop).

Invoked from aspirations-precheck Phase 0.5i. On a fire (exit 0) the precheck
phase runs `curriculum-evaluate.sh`, stamps the WM slot with the current
timestamp, and routes any qualifying promotion through `/curriculum-gates`
(which owns the guard-33 email-confirmed promotion). This script ONLY reads
state — it never evaluates, never stamps, never promotes. Keeping it pure makes
it trivially unit-testable and mirrors the read-only contract of its sibling
`fresh-eyes-cadence-check.py`.

Why time-based (not goal-count like fresh-eyes): the staleness this fixes
(g-115-1801 — delta's Foundation graduation-gate evaluation sat a month old,
goals=3/competence=0.0 while the live values were goals=27/competence=0.589) is
a WALL-CLOCK drift. The only reliable existing `/curriculum-gates` triggers are
session-end consolidation and cadence-gated evolution; an agent looping
continuously under autocompact reaches neither for weeks, so its stored gate
snapshot ages indefinitely regardless of goal throughput. A time cadence is the
axis that actually elapsed.

Config block (core/config/aspirations.yaml):
    curriculum_cadence:
      enabled: true
      interval_hours: 24
      wm_slot: last_curriculum_eval

Flags:
    --verbose   Print the elapsed/interval breakdown alongside the fire/noop code.
"""
from __future__ import annotations

import argparse
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
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)  # noqa: E402

CONFIG_PATH = _paths.CONFIG_DIR / "aspirations.yaml"
CONFIG_BLOCK = "curriculum_cadence"
DEFAULT_INTERVAL_HOURS = 24
DEFAULT_WM_SLOT = "last_curriculum_eval"


def _load_yaml(path: Path):
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001
        print(
            f"curriculum-cadence-check: WARN: yaml parse failed for {path}: {e}",
            file=sys.stderr,
        )
        return None


def wm_slot_value(slot_name: str):
    """Read `slot_name` via the daemon. as_json=True preserves the same JSON
    text the deleted wm.py CLI printed (do NOT drop as_json — wm.py's default
    was YAML, and json.loads on YAML silently degrades to None). Fail-soft:
    any daemon error / empty / 'null' returns None (treated as 'never
    evaluated' → fire)."""
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


def _parse_iso_epoch(iso) -> float | None:
    """Parse an ISO 'YYYY-MM-DDThh:mm:ss' string to a local-time epoch float.
    Returns None for a missing/empty/unparseable value so callers treat 'no
    real prior stamp' as 'never evaluated → fire'."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _slot_timestamp(slot_val) -> str | None:
    """Extract the ISO timestamp from the WM slot. The precheck stamps the slot
    with either a bare ISO string or a {"timestamp": "..."} dict — accept both
    (defensive against shape drift, mirror of the sibling gates' type-guards)."""
    if isinstance(slot_val, dict):
        return slot_val.get("timestamp") or slot_val.get("last_evaluated")
    if isinstance(slot_val, str):
        return slot_val
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    cfg = _load_yaml(CONFIG_PATH)
    if cfg is None:
        print("curriculum-cadence-check: config read failed — noop", file=sys.stderr)
        return 1  # fail-open — cadence gate MUST NOT block the loop

    block = cfg.get(CONFIG_BLOCK)
    if not isinstance(block, dict):
        # No config block → feature not configured. Noop (backward-compatible:
        # a world without this block behaves exactly as before this ritual).
        if args.verbose:
            print(
                f"curriculum-cadence-check: no '{CONFIG_BLOCK}' block in "
                f"aspirations.yaml — noop"
            )
        return 1

    if block.get("enabled") is False:
        if args.verbose:
            print("curriculum-cadence-check: disabled via config — noop")
        return 1

    try:
        interval_hours = float(block.get("interval_hours", DEFAULT_INTERVAL_HOURS))
    except (TypeError, ValueError):
        interval_hours = float(DEFAULT_INTERVAL_HOURS)
    slot_name = str(block.get("wm_slot", DEFAULT_WM_SLOT))

    slot_val = wm_slot_value(slot_name)
    last_epoch = _parse_iso_epoch(_slot_timestamp(slot_val))

    if last_epoch is None:
        # Never evaluated (or unparseable/missing stamp) → fire so the first
        # cadence pass establishes a fresh snapshot.
        if args.verbose:
            print(
                f"curriculum-cadence-check: fire (slot={slot_name} unset/unparseable — "
                f"first evaluation, interval={interval_hours}h)"
            )
        else:
            print(f"curriculum-cadence-check: fire (slot {slot_name} unset — first eval)")
        return 0

    try:
        now_epoch = datetime.now().timestamp()
    except (OSError, OverflowError):
        # Clock read failed — noop (fail-open). Next iteration retries.
        print("curriculum-cadence-check: clock read failed — noop", file=sys.stderr)
        return 1

    elapsed_hours = (now_epoch - last_epoch) / 3600.0
    fire = elapsed_hours >= interval_hours
    if args.verbose:
        print(
            f"curriculum-cadence-check: {'fire' if fire else 'noop'} "
            f"(slot={slot_name} elapsed={elapsed_hours:.2f}h interval={interval_hours}h)"
        )
    else:
        print(
            f"curriculum-cadence-check: {'fire' if fire else 'noop'} "
            f"(elapsed={elapsed_hours:.1f}h >= interval={interval_hours}h)"
            if fire
            else f"curriculum-cadence-check: noop (elapsed={elapsed_hours:.1f}h < interval={interval_hours}h)"
        )
    return 0 if fire else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        # Fail-open outer guard — ANY unexpected error must exit 1 (noop), not
        # propagate. Breaking this gate would silently wedge the precheck phase.
        print(f"curriculum-cadence-check: unexpected error: {exc} — noop", file=sys.stderr)
        sys.exit(1)
