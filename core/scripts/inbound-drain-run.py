#!/usr/bin/env python3
"""inbound-drain-run — audited entry point for the `inbound-drain` hook slot.

Pattern B executable slot (core/config/conventions/domain-hooks.md). Core names
the SLOT — `world/scripts/inbound-drain.sh` — and never the domain script behind
it; naming a specific world artifact from core is the anti-pattern that
convention opens with. Sibling of `outcome-observation-run.sh`, and for the same
reason: an audited entry point is what makes a hook's absence VISIBLE. That slot
rotted (g-115-4879) precisely because a caller invoked the collector directly and
skipped the audit layer, leaving a healthy output concealing a dead hook.

WHY A FLATTENER EXISTS HERE AT ALL. The precheck battery reads findings from
TOP-LEVEL keys of a lane's JSON (`_findings_for` does `payload.get(k)`), while
the domain drain reports per-environment counts NESTED under `environments[]`. A
lane wired straight to it would find no top-level key, report clean forever, and
be indistinguishable from a lane that genuinely found nothing — the exact shape
this whole feature exists to avoid. So the counts are summed to the top level
here, in core, where the battery's contract lives.

ABSENCE IS NOT ZERO, and the status word is how you tell which world you are in:
  no-slot        this world has no inbound-drain slot. A supported configuration
                 (fresh worlds have none) and a silent no-op by contract.
  not-a-vessel   the slot ran and declined: no instance token, no root, or no
                 spool for this environment. NOT a drain of zero.
  unparseable    the slot printed something that is not JSON. A MALFUNCTION, and
                 it is reported as a finding rather than swallowed — an exit-0
                 with unreadable output is guard-1091's shape and must never
                 reach the battery as clean.
  ok             the drain ran. `drained` is then a real number.

FAIL-OPEN BY CONTRACT: always exit 0. An always-run precheck lane may never block
the loop (guard-614).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _runtime_bash import BASH  # noqa: E402

SLOT_NAME = "inbound-drain"


def _world_path() -> Path | None:
    """Resolve WORLD_PATH the way every other core consumer does — via _paths."""
    try:
        import _paths  # noqa: WPS433
        w = getattr(_paths, "world_path", None)
        if callable(w):
            return Path(w())
        for attr in ("WORLD_PATH", "WORLD_DIR"):
            v = getattr(_paths, attr, None)
            if v:
                return Path(v)
    except Exception:
        pass
    v = os.environ.get("WORLD_PATH") or os.environ.get("WORLD_DIR")
    return Path(v) if v else None


#: The DEFAULT slot, used when this world does not fill the hook. Pattern B says
#: the world slot is an OVERRIDE, and until 2026-09-07 an unfilled hook meant the
#: feature was simply absent. That is the right default for an optional hook and
#: the wrong one here: `world/` is excluded from the seed and is not in git, so
#: the environments this feature exists to serve are exactly the ones that can
#: never carry a world slot (measured 0 of 45 live workspaces — ).
DEFAULT_SLOT = SCRIPT_DIR / f"{SLOT_NAME}-default.sh"


def _resolve_slot(slot_override: Path | None) -> tuple[Path | None, str]:
    """Return (slot, source). Source is world | core-default | none.

    ORDER IS THE CONTRACT: a world that fills the hook still wins, so adding the
    default cannot change behaviour on any box that already had a slot.
    """
    if slot_override is not None:
        return slot_override, "override"
    wp = _world_path()
    if wp is not None:
        world_slot = wp / "scripts" / f"{SLOT_NAME}.sh"
        if world_slot.is_file():
            return world_slot, "world"
    if DEFAULT_SLOT.is_file():
        return DEFAULT_SLOT, "core-default"
    return None, "none"


def run(apply: bool = False, slot_override: Path | None = None) -> dict:
    slot, slot_source = _resolve_slot(slot_override)

    # Pattern B requirement 3: missing convention = silent no-op, never an error.
    # Reachable now only if the core default is ALSO missing, which means a
    # damaged install rather than an unfilled hook — so the note says so.
    if slot is None:
        return {"status": "no-slot", "slot": SLOT_NAME, "slot_source": "none",
                "drained": 0, "failed": [],
                "note": "no world slot and no core default — the core default "
                        f"({DEFAULT_SLOT.name}) is missing from this install"}
    if not slot.is_file():
        return {"status": "no-slot", "slot": slot.as_posix(), "slot_source": slot_source,
                "drained": 0, "failed": [],
                "note": "the named slot does not exist"}

    argv = [BASH, slot.as_posix(), "--json"]   # guard-580: BASH resolved, never bare "bash"
    if apply:
        argv.append("--apply")
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "unparseable", "slot": slot.as_posix(), "slot_source": slot_source,
                "drained": 0,
                "failed": [{"file": "-", "reason": f"slot did not run: {exc}"}]}

    out = (proc.stdout or "").strip()
    if not out:
        return {"status": "unparseable", "slot": slot.as_posix(), "slot_source": slot_source,
                "drained": 0,
                "failed": [{"file": "-", "reason":
                            "slot printed ZERO bytes at rc=%s — a malfunction, not a "
                            "result; every branch of the slot emits JSON" % proc.returncode}]}
    try:
        payload = json.loads(out)
    except ValueError:
        return {"status": "unparseable", "slot": slot.as_posix(), "slot_source": slot_source,
                "drained": 0,
                "failed": [{"file": "-", "reason":
                            "slot output is not JSON: %s" % out[:200]}]}

    if payload.get("not_a_vessel"):
        return {"status": "not-a-vessel", "slot": slot.as_posix(), "slot_source": slot_source,
                "drained": 0,
                "failed": [], "note": payload.get("reason") or "slot declined"}

    envs = payload.get("environments") or []
    res = {
        "status": "ok",
        "slot": slot.as_posix(),
        "slot_source": slot_source,
        "root": payload.get("root"),
        "apply": bool(apply),
        "environments_seen": len(envs),
        "drained": sum(int(e.get("processed") or 0) for e in envs),
        "claimed": sum(int(e.get("claimed") or 0) for e in envs),
        "rejected": sum(int(e.get("rejected") or 0) for e in envs),
        "quarantined": sum(int(e.get("quarantined") or 0) for e in envs),
        "skipped_tmp": sum(int(e.get("skipped_tmp") or 0) for e in envs),
        "unconfigured": sum(int(e.get("unconfigured") or 0) for e in envs),
        "failed": [],
        "stranded": [],
    }
    # `failed` is the battery's UNIVERSAL finding key, so a per-env failure count
    # has to become a list entry or it is invisible to _findings_for.
    for e in envs:
        n = int(e.get("failed") or 0)
        if n:
            res["failed"].append({"file": e.get("environment") or "?",
                                  "reason": f"{n} record(s) failed and stayed claimed"})
    # UNCONFIGURED must reach the same finding key, or a STUCK member directive is
    # reported as a healthy empty spool (measured 2026-09-07, , cc-07: an
    # empty inbound/ and one undrained directive both printed
    # `status=ok drained=0 failed=0` at rc=0 — byte-identical). The drain counts it
    # and exits 2 precisely because "an undrained spool must never report success",
    # but the slot ends `|| true; exit 0` so that rc never survives, and this dict
    # was the only other channel. Concealing a dead hook behind a healthy output is
    # the exact class this runner's docstring exists to prevent.
    for e in envs:
        n = int(e.get("unconfigured") or 0)
        if n:
            res["failed"].append({"file": e.get("environment") or "?",
                                  "reason": f"{n} directive(s) left queued: no target "
                                            "aspiration configured (INBOUND_DIRECTIVE_ASP_ID)"})
    # THE NAME IS LOAD-BEARING AND IT WAS WRONG (, measured 2026-09-12 echo/cc-03).
    # This finding read `SIDECAR_DIRECTIVE_ASP_ID` until now; that name is set by NOTHING and
    # read by NOTHING -- the engine's only target var is inbound_drain.DIRECTIVE_ASP_ENV =
    # "INBOUND_DIRECTIVE_ASP_ID", and `grep -rn SIDECAR_DIRECTIVE_ASP_ID` over this repo AND
    # Ayoai-Environment-Server@origin/main returned only this string and the test pinning it.
    # So the one channel that survives the slot's `|| true; exit 0` told every reader to go
    # look for a variable that cannot exist. Cost, measured:  was picked up TWELVE
    # times across five Bodies, each recording "SIDECAR_DIRECTIVE_ASP_ID still UNSET" as the
    # one true remaining blocker, while the vessel carried INBOUND_DIRECTIVE_ASP_ID set
    # (value_len=7) and its minted "Assigned by the member" aspiration all along. A finding
    # that names a phantom is worse than silence: it is actionable-looking and unactionable.
    # rb-2515 class ("reported absent may be PRESENT under a different env-var name").
    # STRANDED records (): a record a PRIOR run claimed into processing/
    # and never completed. The drain deliberately leaves it there — re-applying a
    # member's half-applied instruction is an operator judgment, not a sweep's
    # (inbound_drain L47-51) — so this reports and NEVER re-queues.
    #
    # Two keys on purpose. `stranded` carries the structured identity for a
    # dashboard read; `failed` is the battery's UNIVERSAL finding key, and an
    # entry must land there or _findings_for cannot see it at all — the same
    # reason `unconfigured` is routed there above.
    #
    # ONE ENTRY PER RECORD, never a per-environment count: environment-granularity
    # aggregation is the defect being corrected (rb-10397). A count cannot be
    # acted on; "which file, in which environment, how old" can.
    for e in envs:
        for st in (e.get("stranded") or []):
            res["stranded"].append(st)
            res["failed"].append({
                "file": f"{st.get('environment')}/{st.get('file')}",
                "reason": f"member directive claimed but unapplied for "
                          f"{st.get('age_minutes')}m — report only; "
                          f"--requeue-stale is the operator's act",
            })
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run the inbound-drain hook slot.")
    ap.add_argument("--apply", action="store_true", help="perform the moves and writes")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--slot", help="explicit slot path (tests)")
    args = ap.parse_args(argv)

    res = run(apply=args.apply, slot_override=Path(args.slot) if args.slot else None)

    if args.json:
        print(json.dumps(res))
    else:
        print(f"[inbound-drain] status={res['status']} drained={res.get('drained', 0)} "
              f"failed={len(res.get('failed', []))} apply={res.get('apply', False)}")
        if res.get("note"):
            print(f"  note: {res['note']}")
        for f in res.get("failed", []):
            print(f"  FAILED {f['file']}: {f['reason']}")
    return 0   # fail-open by contract


if __name__ == "__main__":
    sys.exit(main())
