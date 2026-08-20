"""orchestrator-entry-battery — one call enumerating the aspirations/SKILL.md
orchestrator ENTRY-phase checks (g-115-2550; consumer action of CONFIRMED
hypothesis 2026-07-16_sentinel-battery-survives-compaction).

Kills the entry-phase omission class the precheck battery (g-115-2303) killed
for precheck sentinels: the orchestrator entry sequence (Phases -0.5a..-0.5e)
was LLM-ENUMERATED — N discrete file/WM checks re-derived from SKILL.md memory
every re-entry. Fresh incident 2026-07-18T00:07 (g-115-2314 resolution
evidence): a post-compaction re-entry ran the precheck battery correctly yet
MISSED Phase -0.5c — compact-checkpoint.yaml sat unconsumed 25min (stale-skip
made it harmless; a fresher checkpoint would have mattered). A compaction
summary need only preserve "run the entry battery" (1 line); the script owns
the check list, so an entry phase can never silently fall out of the protocol.

READ-ONLY: the battery enumerates and prints; the SKILL.md phase bodies keep
ownership of action + clear for each check. Time-dependent entry calls
(stranded-claim-sweep, quiescence/dry-idle cache checks, idle-tick) are NOT
invoked here — idle-tick recomputes on every call and wm-read mutates
accessed_at, so each must be called exactly once by its own phase. The battery
lists them in protocol order in the footer so the sequence itself survives
compaction.

ENTRY_CHECKS is the registry (single consumer today; extract to a shared
module mirroring _sentinel_registry if a canary consumer appears).

Output (guard-614: structured output on EVERY exit path):
  default — one line per ACTIONABLE check plus a summary + protocol footer:
      ▸ ENTRY: <name> (phase <phase>) payload=<json> → dispatch: <section>
      [entry-battery] N actionable / M checks
  --json  — single JSON object {checked_at, checks, actionable: [...],
            findings: [...], blind: [...], error?}

`findings` and `blind` are the COMPOSED-CALLER contract: `iteration-open.py`
lifts `payload["findings"]` via `_findings_from` and `payload["blind"]` via
`_blind_from`, by those key names. Emitting only `actionable` made this battery
invisible inside `iteration-open.sh --apply` — it ran the stage, reported rc=0,
and surfaced nothing. `actionable` is retained unchanged for direct readers
(the orchestrator's own Phase -0.5a0 calls this script directly and reads the
human lines); `findings` MIRRORS it for the composed path. Found 2026-08-18
alongside the identical defect in precheck-sentinel-battery (g-115-6618): two
of the three stages iteration-open composes were blind, and only
precheck-always-run-battery — which already emitted `findings` — worked, which
is why it was the sole lane ever reporting. guard-318.

Fail-open: any error prints the summary/JSON with an `error` field and exits
0 — the LLM falls back to per-phase checks. The battery must never block the
loop.

Invocation (aspirations/SKILL.md Phase -0.5, after heartbeat-tick): direct
`py -3 core/scripts/orchestrator-entry-battery.py` or the thin wrapper
`bash core/scripts/orchestrator-entry-battery.sh`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# Registry: file-presence checks + WM-slot checks, in orchestrator entry order.
# kind: "file" (path relative to agent_state_dir) or "wm_slot" (non-null = actionable).
ENTRY_CHECKS = [
    {
        "name": "pending_agents",
        "kind": "file",
        "rel": "pending-agents.yaml",
        "phase": "-0.5a",
        "skill_section": (
            "aspirations/SKILL.md Phase -0.5a (Background Agent Result Collection; "
            "pending-agents.sh list --json, collect + deregister)"
        ),
    },
    {
        "name": "compact_checkpoint",
        "kind": "file",
        "rel": "compact-checkpoint.yaml",
        "phase": "-0.5c",
        "skill_section": (
            "aspirations/SKILL.md Phase -0.5c (Compact Checkpoint Processing; "
            "compact-restore-slots.sh — idempotent, stale-skip deletes)"
        ),
    },
    {
        "name": "pending_phase_6_spark",
        "kind": "wm_slot",
        "slot": "pending_phase_6_spark",
        "phase": "-0.5c.2",
        "skill_section": (
            "aspirations/SKILL.md Phase -0.5c.2 (Pending Phase-6 Spark Sentinel; "
            "TTL check -> spark-fire-dedup check -> fire-or-clear)"
        ),
    },
    {
        "name": "blocked_sleep_until",
        "kind": "wm_slot",
        "slot": "blocked_sleep_until",
        "phase": "-0.5e",
        "skill_section": (
            "aspirations/SKILL.md Phase -0.5e Branch B (blocked-sleep residual; "
            "load-blocked-sleep-recovery.sh digest — do NOT re-read the slot)"
        ),
    },
]

# Always-run entry calls the battery deliberately does NOT invoke (each is
# time-dependent or state-mutating and must run exactly once, by its phase).
#
# stranded-claim-sweep is named as the .sh WRAPPER, not the bare .py, and that
# is load-bearing (guard-3864 / rb-7918 / ). Only the wrapper sources
# _paths.sh, which reads the per-agent local-paths.conf for MIND_WORLD --
# STORAGE_BACKEND is set globally in settings.json, so a bare `py -3` has the
# backend but no mappable world root, silently falls back to the LOCAL MIRROR,
# and decides whether a live peer's claim gets released from stale data. This
# footer is the line the protocol relies on surviving summarization after an
# autocompact, so naming the forbidden form here propagates it fleet-wide, every
# iteration. Measured 2026-08-19 (zeta, cc-02, both forms in one turn):
# shard_provenance "local-mirror" (bare .py) vs "authoritative" (wrapper).
# The other three .py entries below are CORRECT as-is -- their SKILL.md phases
# invoke them as `py -3 core/scripts/<name>.py`; only this one has a wrapper.
PROTOCOL_FOOTER = (
    "[entry-battery] always-run entry calls (protocol order, invoke each ONCE): "
    "stranded-claim-sweep.sh --apply (-0.5c.1) -> quiescence-cycle-cache.py check "
    "(-0.5e.0) -> dry-idle-cycle-cache.py check (-0.5e.0b) -> idle-tick.sh (-0.5e) "
    "-> quiescence-gate.py verify-wake (-0.5e') -> identity restore (-0.5d)"
)


def _now_iso() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _payload_str(payload) -> str:
    """Serialize a dispatch payload for the human-mode line. Dict payloads emit
    SHORT scalar fields first and long text last, THEN truncate — so the cap
    can only ever eat prose, never machine-consumed keys (g-115-2553: the
    writer's insertion order put a long `summary` before set_at/expires_at, the
    400-char cap cut them from a live pending_phase_6_spark line, and Phase
    -0.5a0's contract — the payload on the line IS the read — forced a
    contract-violating wm re-read just to recover set_at for the dedup call)."""
    if isinstance(payload, dict):
        short = {k: v for k, v in payload.items() if len(str(v)) <= 64}
        payload = {**short, **{k: v for k, v in payload.items() if k not in short}}
    s = json.dumps(payload, ensure_ascii=False, default=str)
    if len(s) > 400:
        s = s[:400] + "…"
    return s


def _emit(report: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False))
        return
    for entry in report.get("actionable", []):
        payload = _payload_str(entry.get("payload"))
        print(
            f"▸ ENTRY: {entry['name']} (phase {entry['phase']}) "
            f"payload={payload} → dispatch: {entry['dispatch']}"
        )
    n_act = len(report.get("actionable", []))
    n_chk = report.get("checks", 0)
    err = f" error={report['error']}" if report.get("error") else ""
    if n_act == 0 and not err:
        print(f"[entry-battery] all {n_chk} entry checks clean — no dispatches")
    else:
        print(f"[entry-battery] {n_act} actionable / {n_chk} checks{err}")
    print(PROTOCOL_FOOTER)


def _fail_open(report: dict, reason: str, as_json: bool) -> int:
    """Record a fail-open exit as BLIND, then emit. Mirrors the identical helper
    in precheck-sentinel-battery.py — fail-open must not mean fail-SILENT to a
    composed caller, or an errored battery is indistinguishable from a clean one
    (guard-4093) and iteration-open's own "N lane(s) blind" branch is unreachable.
    """
    report["error"] = reason
    report["blind"].append({"name": "entry-checks", "phase": "-0.5a..-0.5e",
                            "reason": reason})
    _emit(report, as_json)
    return 0


def run(agent_override: str | None, wm_path_override: str | None, as_json: bool) -> int:
    report: dict = {"checked_at": _now_iso(), "checks": len(ENTRY_CHECKS),
                    "actionable": [], "findings": [], "blind": []}

    # Resolve agent state dir (agent-wide session/ dir holds both files).
    try:
        from _paths import agent_state_dir  # type: ignore

        agent = agent_override or os.environ.get("MIND_AGENT", "")
        state_dir = Path(agent_state_dir(agent)) if agent else None
    except Exception as exc:
        return _fail_open(report, f"paths_import_failed: {exc}", as_json)
    if state_dir is None:
        return _fail_open(report, "no_agent_binding (MIND_AGENT unset)", as_json)

    # WM slots (same resolver + torn-read posture as precheck-sentinel-battery).
    wm_slots: dict = {}
    try:
        import yaml

        if wm_path_override:
            wm_path = Path(wm_path_override)
        else:
            from wm import wm_path as _resolve_wm_path

            wm_path = _resolve_wm_path()
        if wm_path.exists():
            data = yaml.safe_load(wm_path.read_text(encoding="utf-8")) or {}
            wm_slots = data.get("slots", data) if isinstance(data, dict) else {}
        else:
            report["error"] = "no_working_memory_file"
            report["blind"].append({"name": "entry-checks/wm_slot", "phase": "-0.5a..-0.5e",
                                    "reason": "no_working_memory_file"})
    except Exception as exc:
        report["error"] = f"wm_read_failed: {exc}"
        report["blind"].append({"name": "entry-checks/wm_slot", "phase": "-0.5a..-0.5e",
                                "reason": f"wm_read_failed: {exc}"})
    # These two do NOT return: the file-kind checks below are still evaluable, so
    # the battery degrades PARTIALLY rather than going dark. The blind entry is
    # what tells a composed caller that the wm_slot half was not covered -- without
    # it, a partial run and a full clean run are the same object.

    for spec in ENTRY_CHECKS:
        try:
            if spec["kind"] == "file":
                p = state_dir / spec["rel"]
                if p.exists():
                    payload = {"path": str(p), "mtime": _dt.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%dT%H:%M:%S")}
                else:
                    continue
            else:  # wm_slot
                value = wm_slots.get(spec["slot"])
                if value is None or value == "null":
                    continue
                payload = value
        except Exception:
            continue  # per-check fail-open
        report["actionable"].append(
            {
                "name": spec["name"],
                "phase": spec["phase"],
                "payload": payload,
                "dispatch": spec["skill_section"],
            }
        )
        # An ACTIONABLE entry check IS a finding. Built in the SAME loop from the
        # same spec so the two lists cannot drift; `detail` is a list, which
        # iteration-open's _findings_from joins.
        report["findings"].append(
            {
                "name": spec["name"],
                "phase": spec["phase"],
                "detail": [f"ACTIONABLE -> dispatch {spec['skill_section']}"],
            }
        )

    _emit(report, as_json)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="emit one JSON object")
    parser.add_argument("--agent", default=None, help="override agent name (tests only)")
    parser.add_argument("--wm-path", default=None, help="override working-memory.yaml path (tests only)")
    args = parser.parse_args()
    return run(args.agent, args.wm_path, args.json)


if __name__ == "__main__":
    sys.exit(main())
