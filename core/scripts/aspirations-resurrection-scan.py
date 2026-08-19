#!/usr/bin/env python3
"""Read-only detector for RESURRECTED aspirations in the live store — the
/verify-learning half of the fix whose remedy is the daemon archive_sweep
reconcile (goal-completion audit, 2026-08-16). Same predicate, one source:
core/scripts/_aspirations_resurrection.py.

Reads the live store and the archive THROUGH THE DAEMON (aspirations-read.sh
--active / --archive), never the local mirror directly, so the verdict is
about the authoritative state. Prints a one-line PASS/FAIL/SKIP and, with
--json, the machine-readable finding list.

  PASS  no live aspiration carries a goal the archive already dispositioned
  FAIL  N resurrected aspirations — a stale-fence union re-added a retired
        record; run `aspirations-archive.sh` (the reconcile) and check which
        box holds a stale live file
  SKIP  a store could not be read (daemon down / no bound agent) — an
        unreadable population is NOT a clean one

Exit codes: 0 PASS, 1 FAIL, 2 SKIP (so a wrapper can act on it) — pass
--exit-zero to always exit 0 when only the text verdict is wanted.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, List

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import _aspirations_resurrection as _resurrection  # noqa: E402
from _runtime_bash import bash_cmd  # noqa: E402  # guard-580/581: resolved BASH + posix path


def _read(flag: str, source: str, timeout: float) -> List[Any]:
    cmd = bash_cmd(_HERE / "aspirations-read.sh", flag, "--source", source)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0 or not out.stdout.strip():
        return []
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = data.get("aspirations") or []
    return data if isinstance(data, list) else []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", default="world", choices=("world", "agent"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--exit-zero", action="store_true")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args(argv)

    live = _read("--active", args.source, args.timeout)
    archive = _read("--archive", args.source, args.timeout)
    rc = 0
    if not live and not archive:
        verdict = ("SKIP: aspirations-read.sh returned nothing for live and archive "
                   "(daemon down or no bound agent) — an unreadable population is "
                   "NOT a clean one; re-run when the daemon is up")
        found: List[dict] = []
        rc = 2
    else:
        found = _resurrection.find_resurrected(live, archive)
        if found:
            ids = ", ".join(
                f"{f['asp_id']}[{','.join(f['goal_ids'])}]"
                + ("" if f["would_rearchive"] else "(kept live: post-archive work)")
                for f in found)
            verdict = (f"FAIL: {len(found)} RESURRECTED aspiration(s) live — the archive "
                       f"already holds them terminal but a stale-fence union re-added a "
                       f"pristine copy: {ids}. Run `bash core/scripts/aspirations-archive.sh` "
                       f"(archive_sweep reconciles from the archive record) and find the box "
                       f"holding the stale live file (rb-8064)")
            rc = 1
        else:
            verdict = (f"PASS: no resurrected aspiration in the live store "
                       f"({len(live)} live vs {len(archive)} archived ids)")
    if args.json:
        print(json.dumps({"verdict": verdict.split(":", 1)[0], "live": len(live),
                          "archive": len(archive), "resurrected": found}))
    else:
        print(verdict)
    return 0 if args.exit_zero else rc


if __name__ == "__main__":
    sys.exit(main())
