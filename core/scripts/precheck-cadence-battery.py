"""precheck-cadence-battery — one call running every deferrable skill-invocation
cadence gate check and reporting which FIRE (g-115-2984, fix for g-115-2982).

Kills the cadence-starvation-by-abbreviation class: the six skill-invocation
cadence rituals (fresh-eyes-review/program/tree, felt-sense, curriculum,
evolution) were SIX separate LLM-orchestrated precheck phases (0.5e / 0.5e.5 /
0.5e.7 / 0.5f / 0.5i / 0.5j), each abbreviate-able under context pressure. When
the LLM skipped a phase, its cadence-check gate was never run, so the ritual's
skill was never invoked and the ritual starved silently. felt-sense (0.5f)
starved 3 days / 581 goals exactly this way — foxtrot looped continuously, the
budget meter was NOT dropping it (g-115-2982 refuted that mechanism), and a
non-fire at diff 581>>cadence 75 proved the gate was never run. This battery is
the g-115-2303 sentinel-battery / g-115-2550 orchestrator-entry-battery pattern
applied to cadences: post-autocompact, "run the cadence battery" is the ONE line
that must survive summarization; the output re-derives the full cadence set
(which FIRE + the skill to invoke) from _cadence_registry, so a phase can never
silently fall out of the protocol.

READ-ONLY: the six cadence-check scripts "only read state" (goal-count vs
last-fire), so the battery is side-effect-free — it runs the checks and prints;
the SKILL.md dispatch loop keeps ownership of the actual SKILL INVOCATION on
FIRE. The battery deliberately does NOT read the budget meter: the checks are
cheap and read-only, so running them costs almost nothing even under pressure;
the meter's tight-zone `deferrable` drop gates the EXPENSIVE skill invocation and
is applied at DISPATCH time in the SKILL.md (each FIRE line carries its
meter_name for that gate).

Scope (principled — see _cadence_registry docstring): the six skill-invocation
cadences. l1-skew (0.5g, self-acting board post) and health-regression (0.5h,
DORMANT + multi-step verify/revert) keep their own phases — both sit outside the
skill-invocation-skip starvation class.

Output (guard-424 fail-loud-with-stderr; guard-614 structured on EVERY exit path):
  default — one human line per FIRING cadence + a summary line:
      ▸ CADENCE FIRE: <name> (phase <phase>) meter=<meter_name> → <dispatch>
      [cadence-battery] N fire / M checked
  --json  — {checked_at, registered, fired:[{name,phase,meter_name,dispatch}],
            error?}

Fail-open: any error prints the structured report with an `error` field (also to
stderr, guard-424) and exits 0 — the LLM falls back to the per-phase cadence
checks. The battery must never block the loop.

Invocation (aspirations-precheck Phase 0.5-cadence-battery): the thin wrapper
`bash core/scripts/precheck-cadence-battery.sh` or direct
`py -3 core/scripts/precheck-cadence-battery.py`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

_CHECK_TIMEOUT_S = 30


def _now_iso() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _run_bash(argv, timeout):
    """Run a core/scripts bash script. Return (returncode:int|None, err:str|None).

    returncode None + err set == the check could not run (timeout / missing
    script) — the caller treats it as a noop and surfaces the error so the LLM
    can fall back to the per-phase check. Exit 0 == FIRE, any non-zero == noop.
    """
    script = argv[0]
    from _runtime_bash import BASH  # rb-1472: not bare "bash"
    full = [BASH, str(SCRIPT_DIR / script)] + list(argv[1:])
    try:
        r = subprocess.run(
            full, cwd=str(PROJECT_ROOT),
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode, None
    except Exception as exc:  # timeout, missing script, spawn failure
        return None, f"{script}: {exc}"


def _emit(report: dict, as_json: bool) -> None:
    if report.get("error"):
        # guard-424: cadence/precheck scripts fail LOUD with stderr, never silent.
        print(f"[cadence-battery] {report['error']}", file=sys.stderr)
    if as_json:
        print(json.dumps(report, ensure_ascii=False))
        return
    for e in report.get("fired", []):
        print(
            f"▸ CADENCE FIRE: {e['name']} (phase {e['phase']}) "
            f"meter={e['meter_name']} → {e['dispatch']}"
        )
    n_fire = len(report.get("fired", []))
    n_reg = report.get("registered", 0)
    err = f" error={report['error']}" if report.get("error") else ""
    if n_fire == 0 and not report.get("error"):
        print(f"[cadence-battery] all {n_reg} cadence gates noop — nothing to fire")
    else:
        print(f"[cadence-battery] {n_fire} fire / {n_reg} checked{err}")


def run(as_json: bool, check_runner=None) -> int:
    """Run every registered cadence gate check, report the FIRE set.

    check_runner: injectable (argv, timeout) -> (returncode|None, err|None) for
    tests. Defaults to the real bash subprocess runner.
    """
    runner = check_runner or _run_bash
    report: dict = {"checked_at": _now_iso(), "registered": 0, "fired": []}
    try:
        from _cadence_registry import cadences
    except Exception as exc:  # fail-open (import env broken)
        report["error"] = f"import_failed: {exc}"
        _emit(report, as_json)
        return 0

    cads = cadences()
    report["registered"] = len(cads)

    check_errors: list[str] = []
    for c in cads:
        rc, err = runner(c["check_cmd"], _CHECK_TIMEOUT_S)
        if err is not None:
            check_errors.append(err)
            continue  # broken/timed-out check -> noop (fail-open); surfaced below
        if rc == 0:  # FIRE — cadence crossed
            report["fired"].append(
                {
                    "name": c["name"],
                    "phase": c["phase"],
                    "meter_name": c["meter_name"],
                    "dispatch": c["fire_dispatch"],
                }
            )
        # any non-zero rc -> noop (cadence not crossed / config disabled)

    if check_errors:
        report["error"] = "check_errors: " + "; ".join(check_errors)

    _emit(report, as_json)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="emit one JSON object")
    args = parser.parse_args()
    return run(args.json)


if __name__ == "__main__":
    sys.exit(main())
