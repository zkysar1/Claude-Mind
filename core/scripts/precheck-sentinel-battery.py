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
            payload, dispatch}], findings: [...], blind: [...], error?}

`findings` and `blind` are the COMPOSED-CALLER contract, and they are not
decoration: `iteration-open.py::_findings_from` lifts `payload["findings"]`
and `_blind_from` lifts `payload["blind"]` — those key NAMES are the whole
interface. Emitting only `set` made this battery invisible inside
`iteration-open.sh --apply`, which then printed "no findings; all dispatched
lanes clean" while four always-run gates sat undispatched (measured 2026-08-18,
zeta/cc-02: force_tree_maintain, force_experience_archival,
fresh_eyes_dispatch_pending, force_metric_encoding_pending — all set, none
surfaced). guard-318: confirm the producer's shape, never the caller's
intuition. `set` is retained unchanged for direct readers; `findings` mirrors
it in the sibling `precheck-always-run-battery` shape {name, phase, detail:[]}.

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


def _fail_open(report: dict, reason: str, as_json: bool) -> int:
    """Record a fail-open exit as BLIND, then emit. Three call sites.

    Fail-open must not mean fail-SILENT to a composed caller. Without the
    `blind` entry an errored battery reaches iteration-open as zero findings
    AND zero blind lanes, i.e. indistinguishable from a genuinely clean run —
    the exact "a lane that FAILED must never read as clean" defect guard-4093
    names, and it would defeat that script's own "NO FINDINGS REACHED — N
    lane(s) blind" branch. Reasoned from the contract rather than observed in
    the wild (unlike the `findings` gap above, which was measured).
    """
    report["error"] = reason
    report["blind"].append({"name": "sentinel-battery", "phase": "0-pre..0-pre6",
                            "reason": reason})
    _emit(report, as_json)
    return 0


def run(wm_path_override: str | None, as_json: bool) -> int:
    report: dict = {"checked_at": _now_iso(), "registered": 0, "set": [],
                    "findings": [], "blind": []}
    try:
        import yaml  # noqa: F401
        from _sentinel_registry import battery_slots, is_set
    except Exception as exc:  # fail-open (import env broken)
        return _fail_open(report, f"import_failed: {exc}", as_json)

    slots = battery_slots()
    report["registered"] = len(slots)

    try:
        if wm_path_override:
            wm_path = Path(wm_path_override)
        else:
            from wm import wm_path as _resolve_wm_path  # same resolver as the canary
            wm_path = _resolve_wm_path()
        if not wm_path.exists():
            return _fail_open(report, "no_working_memory_file", as_json)
        data = yaml.safe_load(wm_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # torn read / parse error — fall back to per-phase reads
        return _fail_open(report, f"wm_read_failed: {exc}", as_json)

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
        # A SET sentinel IS a finding. Built in the SAME loop, from the same
        # spec, so the two lists cannot drift apart -- a second pass would be
        # the duplicate-interpretation defect iteration-open's own
        # _findings_from docstring warns about. `name` also satisfies that
        # function's `f.get("name") or f.get("sentinel")` fallback chain.
        report["findings"].append(
            {
                "name": spec["slot"],
                "phase": spec["phase"],
                "detail": [f"SET -> dispatch {spec['skill_section']}"],
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
