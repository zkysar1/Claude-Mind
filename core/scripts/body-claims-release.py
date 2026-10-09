#!/usr/bin/env python3
"""Release everything a stopping worker Body holds, then read back zero.

Called from the worker branch of /stop BEFORE the learning relay and the close,
so a stopped Body's goals go back to the fleet at once instead of staying
locked until the 4h claim lease expires or a reducer sweep notices them.

WHAT IT RELEASES, AND THROUGH WHICH EXISTING WRITER (no new write path)
----------------------------------------------------------------------
  goal claims   every goal whose raw ``claimed_by_sid`` is this SID, from the
                world AND agent queues (aspirations-query.sh is union-only).
                Each goes through aspirations-release.sh, which also clears
                that goal's in_flight / in_flight_bodies row and its
                iteration-checkpoint anchor, all goal-conditional.
  team-state    worker_close_in_flight_clear.py, the SAME helper the
                stop-hook runs at a genuine close: this SID's in_flight_bodies
                row unconditionally (it is keyed by this SID, so it is ours),
                the agent-keyed in_flight row only when this SID claimed its
                goal. Running it here covers the row whose goal is already
                terminal: there is no claim left to release, so the release
                loop never reaches it, and the row keeps reading busy.

WHAT IT LEAVES, ON PURPOSE
--------------------------
  unit leases   (unit-claim.sh) mark an ARTIFACT in flight, e.g. the open PR
                for one unit of a multi-unit goal, and that artifact outlives
                the session. Releasing the lease at stop reopens the
                duplicate-unit race it exists to close, and the lease expires
                on its own after multi_agent.claim_timeout_hours.

NO --reason ON THE RELEASE. release_negatives records the boxes that tried a
goal and could NOT run it there; a stop is not that signal, and writing one
would tell every later reader this box failed.

VERDICTS (one JSON object on stdout; the exit code carries the same answer)
  nothing-held  0  nothing to release, and the read-back is clean
  released      0  released N, and the read-back is clean
  dry-run       0  --dry-run: lists what would be released, changes nothing
  residue       1  the read-back still shows a claim or a row (listed)
  error         2  the claim query could not run. UNREADABLE IS NOT EMPTY: a
                   query that failed never reports nothing-held, because that
                   is the one answer that would end the stop's release check.

Usage:
    py -3 core/scripts/body-claims-release.py --agent <name> --sid <sid> [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from _runtime_bash import bash_cmd  # noqa: E402


class QueryError(RuntimeError):
    """The claim query did not return a readable answer."""


def _env(agent: str, sid: str) -> Dict[str, str]:
    env = dict(os.environ)
    env["MIND_AGENT"] = agent
    env["MIND_SID"] = sid
    return env


def query_claims(agent: str, sid: str) -> List[Dict[str, Any]]:
    """Goals whose raw record carries claimed_by_sid == sid, any status."""
    proc = subprocess.run(
        bash_cmd(SCRIPTS / "aspirations-query.sh",
                 "--goal-field", "claimed_by_sid", sid, "--full"),
        capture_output=True, text=True, env=_env(agent, sid))
    if proc.returncode != 0:
        raise QueryError(f"aspirations-query.sh rc={proc.returncode}: "
                         f"{proc.stderr.strip()[-300:]}")
    try:
        rows = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise QueryError(f"aspirations-query.sh output is not JSON "
                         f"({len(proc.stdout)} bytes): {e}")
    if not isinstance(rows, list):
        raise QueryError(f"aspirations-query.sh returned {type(rows).__name__}, "
                         f"expected a list: {proc.stdout[:200]}")
    held = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        gid = r.get("goal_id") or r.get("id")
        if gid:
            held.append({"goal_id": gid, "source": r.get("source") or "world",
                         "status": r.get("status"),
                         "title": (r.get("title") or "")[:80]})
    return held


def release_goal(agent: str, sid: str, goal_id: str, source: str) -> Dict[str, Any]:
    proc = subprocess.run(
        bash_cmd(SCRIPTS / "aspirations-release.sh", goal_id, "--source", source),
        capture_output=True, text=True, env=_env(agent, sid))
    out = {"goal_id": goal_id, "source": source, "rc": proc.returncode}
    if proc.returncode != 0:
        out["stderr"] = proc.stderr.strip()[-300:]
    return out


def clear_rows(agent: str, sid: str) -> Dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "worker_close_in_flight_clear.py"),
         "--agent", agent, "--sid", sid],
        capture_output=True, text=True, env=_env(agent, sid))
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"verdict": "unparseable", "rc": proc.returncode,
                "stderr": proc.stderr.strip()[-300:]}


def read_body_row(agent: str, sid: str) -> Any:
    """This SID's in_flight_bodies row: None when absent, the row when present.

    Raises QueryError when the read itself fails, so a broken read can never
    pass for an absent row.
    """
    proc = subprocess.run(
        bash_cmd(SCRIPTS / "team-state-read.sh", "--field",
                 f"agent_status.{agent}.in_flight_bodies.{sid}", "--json"),
        capture_output=True, text=True, env=_env(agent, sid))
    if proc.returncode != 0:
        raise QueryError(f"team-state-read.sh rc={proc.returncode}: "
                         f"{proc.stderr.strip()[-300:]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise QueryError(f"team-state-read.sh output is not JSON: {e}")


def release_all(agent: str, sid: str, *,
                query: Callable[[str, str], List[Dict[str, Any]]] = query_claims,
                release: Callable[..., Dict[str, Any]] = release_goal,
                clear: Callable[[str, str], Dict[str, Any]] = clear_rows,
                read_row: Callable[[str, str], Any] = read_body_row,
                dry_run: bool = False) -> Dict[str, Any]:
    """Pure decision logic; the four I/O legs are injectable for tests."""
    try:
        held = query(agent, sid)
    except QueryError as e:
        return {"verdict": "error", "stage": "query", "detail": str(e)}
    if dry_run:
        return {"verdict": "dry-run", "would_release": held}

    released = [release(agent, sid, h["goal_id"], h["source"]) for h in held]
    rows = clear(agent, sid)

    try:
        after = query(agent, sid)
        row = read_row(agent, sid)
    except QueryError as e:
        return {"verdict": "error", "stage": "read-back", "detail": str(e),
                "released": released, "rows": rows}

    out = {"released": released, "rows": rows}
    if after or row is not None:
        out.update(verdict="residue",
                   still_claimed=[a["goal_id"] for a in after],
                   body_row=row)
        return out
    out["verdict"] = "released" if held else "nothing-held"
    return out


EXIT = {"nothing-held": 0, "released": 0, "dry-run": 0, "residue": 1, "error": 2}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--agent", required=True)
    ap.add_argument("--sid", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if not args.sid.strip():
        print(json.dumps({"verdict": "error", "stage": "args",
                          "detail": "--sid is empty"}))
        return 2
    result = release_all(args.agent, args.sid.strip(), dry_run=args.dry_run)
    print(json.dumps(result))
    return EXIT[result["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
