"""precheck-sentinel-battery — one call enumerating all precheck force-gate
sentinels (g-115-2303).

Kills the compaction-truncation omission class: the precheck battery was
LLM-ENUMERATED (six wm-read calls across aspirations-precheck Phases
0-pre..0-pre6). Post-autocompact reconstructions dropped phases — zeta's
carried 3 of 6; a set force_metric_encoding_pending sat unread 3 iterations
until the stale-sentinel-canary auto-filed an Investigate (g-115-2302). A
compaction summary need only preserve "run the battery script" (1 line); the
script owns the slot list via _sentinel_registry (shared with the canary —
single source of truth), so a phase can never silently fall out of the
protocol.

READ-ONLY: the battery enumerates and prints; the SKILL.md phase bodies keep
ownership of action + dispatch-stamp + clear for each sentinel. The canary
stays unchanged as the slow-path backstop.

Output (guard-614: structured output on EVERY exit path, including fail-open):
  default — one human line per SET sentinel plus a summary line:
      ▸ SENTINEL: <slot> (phase <phase>) payload=<json> → dispatch: <section>
      [sentinel-battery] N set / M registered
  --json  — single JSON object {checked_at, registered, set: [{slot, phase,
            payload, dispatch}], error?}

"Set" follows _sentinel_registry.is_set — identical to the canary AND to the
consumer phases' own gates: a fired_key dict with fired!=true is NOT set (the
consumer's `IF signal.fired == true` branch would skip it), so the battery
omits it rather than printing a line nobody should act on.

Fail-open: any error prints the summary/JSON with an `error` field and exits
0 — the LLM falls back to per-phase wm-read calls. The battery must never
block the loop.

Invocation (aspirations-precheck Phase 0-pre, top): direct
`py -3 core/scripts/precheck-sentinel-battery.py` or the thin wrapper
`bash core/scripts/precheck-sentinel-battery.sh`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))


def _now_iso() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _emit(report: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False))
        return
    for entry in report.get("set", []):
        payload = json.dumps(entry["payload"], ensure_ascii=False)
        if len(payload) > 400:
            payload = payload[:400] + "…"
        print(
            f"▸ SENTINEL: {entry['slot']} (phase {entry['phase']}) "
            f"payload={payload} → dispatch: {entry['dispatch']}"
        )
    n_set = len(report.get("set", []))
    n_reg = report.get("registered", 0)
    err = f" error={report['error']}" if report.get("error") else ""
    if n_set == 0 and not err:
        print(f"[sentinel-battery] all {n_reg} registered sentinels null — no gates to dispatch")
    else:
        print(f"[sentinel-battery] {n_set} set / {n_reg} registered{err}")


def run(wm_path_override: str | None, as_json: bool) -> int:
    report: dict = {"checked_at": _now_iso(), "registered": 0, "set": []}
    try:
        import yaml  # noqa: F401
        from _sentinel_registry import battery_slots, is_set
    except Exception as exc:  # fail-open (import env broken)
        report["error"] = f"import_failed: {exc}"
        _emit(report, as_json)
        return 0

    slots = battery_slots()
    report["registered"] = len(slots)

    try:
        if wm_path_override:
            wm_path = Path(wm_path_override)
        else:
            from wm import wm_path as _resolve_wm_path  # same resolver as the canary
            wm_path = _resolve_wm_path()
        if not wm_path.exists():
            report["error"] = "no_working_memory_file"
            _emit(report, as_json)
            return 0
        data = yaml.safe_load(wm_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # torn read / parse error — fall back to per-phase reads
        report["error"] = f"wm_read_failed: {exc}"
        _emit(report, as_json)
        return 0

    wm_slots = data.get("slots", data) if isinstance(data, dict) else {}
    for spec in slots:
        value = wm_slots.get(spec["slot"])
        if not is_set(value):
            continue
        report["set"].append(
            {
                "slot": spec["slot"],
                "phase": spec["phase"],
                "payload": value,
                "dispatch": spec["skill_section"],
            }
        )

    _emit(report, as_json)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="emit one JSON object")
    parser.add_argument(
        "--wm-path",
        default=None,
        help="override working-memory.yaml path (tests only)",
    )
    args = parser.parse_args()
    return run(args.wm_path, args.json)


if __name__ == "__main__":
    sys.exit(main())
