#!/usr/bin/env python3
"""release-train-check.py — the release train's time-push trigger, read-only ().

Same implementation as agent-watchdog's ReleaseTrainProbe (_release_train.py),
so what a reader re-measures is what fired.

  bash core/scripts/release-train-check.sh            # one-line verdict
  bash core/scripts/release-train-check.sh --json     # measurement + verdict + open goals
  bash core/scripts/release-train-check.sh --nudge    # iteration-close.sh's in-turn line

--nudge prints ONE LLM-ACTION line only when the train is due AND an open
release-train goal exists for the newest tag, and nothing otherwise. It is the
in-turn half of the probe's goal: the goal is the lease that carries the
disposal, the line makes the reducer SEE it every iteration whether or not the
selector ever ranks the goal (guard-3746). Closing the goal deliberately
therefore silences the line until the probe re-files. When the train is not
due it returns before the goal store is read. It exits 0; iteration-close.sh
also runs it with `|| true`.

THE BASIS IS FETCHED, NOT ASSUMED. The default and --json modes fetch
origin/main AND the v* tags before measuring; the manual equivalent is
`git fetch origin main --tags`. A bare `git fetch origin main` brings no tags,
so on a box that did not cut the newest tag it leaves the previous tag reading
as newest (g-115-11144). --nudge runs every iteration, so it fetches only when
the local reading is due. The fetch writes refs only (origin/main and the v*
tags), never the working tree or the index. `tag_basis` in --json says which
basis the verdict rests on; RELEASE_TRAIN_NO_FETCH=1 disables the fetch.

Exit (default and --json): 0 not due, or not this deployment's train;
2 due; 1 unmeasured (a git read or the basis fetch failed).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _release_train as rt  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="print measurement + verdict as JSON")
    ap.add_argument("--nudge", action="store_true",
                    help="print the in-turn LLM-ACTION line only when due with an open goal")
    ap.add_argument("--repo", help="repository to measure (default: PROJECT_ROOT)")
    ap.add_argument("--world-dir", help="world root (default: WORLD_DIR) — diagnostic/test hook")
    args = ap.parse_args(argv)

    try:
        from _paths import AGENT_DIR, PROJECT_ROOT, WORLD_DIR
    except Exception:  # noqa: BLE001 — explicit flags still work without _paths
        AGENT_DIR = PROJECT_ROOT = WORLD_DIR = None
    repo = Path(args.repo) if args.repo else Path(PROJECT_ROOT or ".")
    world = args.world_dir if args.world_dir else WORLD_DIR
    agent_dir = None if args.world_dir else AGENT_DIR

    role = rt.self_role(world)
    if role != "frontier":
        if not args.nudge:
            result = {"role": role, "frontier": False,
                      "verdict": {"due": False,
                                  "reason": f"not this deployment's train (self_role={role!r})"}}
            print(json.dumps(result, indent=2) if args.json
                  else f"release-train: {result['verdict']['reason']}")
        return 0

    cfg = rt.config()
    m = rt.measure_with_basis(repo, rt.framework_paths(), cfg["stale_hours"],
                              always=not args.nudge)
    verdict = rt.decide(m, cfg["stale_hours"])
    signal = rt.signal_for(m["newest_tag"]) if m.get("newest_tag") else None
    if args.nudge and not verdict["due"]:
        return 0  # nothing to say, so the goal store is never read ()
    goals = rt.open_release_goals(world, agent_dir)
    current = [g.get("id") for g in goals if g.get("origin_signal") == signal]

    if args.nudge:
        if current:
            gid = current[0]
            print(f"[release-train] LLM-ACTION: release train stalled - {verdict['reason']}; "
                  f"open goal {gid} holds the disposal: cut and promote per "
                  f"core/config/conventions/promotion-runbook.md (run "
                  f"core/scripts/deployment-shaped-run.py on the candidate before the cut), "
                  f"or close {gid} skipped with why not.")
        return 0

    if args.json:
        print(json.dumps({"role": role, "frontier": True, "config": cfg, "measurement": m,
                          "verdict": verdict, "signal": signal,
                          "open_goals": [{"id": g.get("id"), "origin_signal": g.get("origin_signal"),
                                          "status": g.get("status"),
                                          "claimed_by": g.get("claimed_by")} for g in goals]},
                         indent=2))
    else:
        state = "DUE" if verdict["due"] else ("UNMEASURED" if m.get("error") else "ok")
        tail = f" Open goal: {', '.join(current)}." if current else ""
        basis = m.get("tag_basis")
        if basis and basis != "fetched" and not m.get("error"):
            tail += f" [basis: {basis}]"
        print(f"release-train: {state} - {verdict['reason']}.{tail}")
    if m.get("error"):
        return 1
    return 2 if verdict["due"] else 0


if __name__ == "__main__":
    sys.exit(main())
