#!/usr/bin/env python3
"""Compute an inbox-derived backlog counter from an aspiration's goals and
write it to a team-state field (g-115-849).

Domain-free: the caller supplies WHICH aspiration, WHICH origin_signal prefix,
and WHICH team-state field. The canonical caller is the domain inbox-sweep
flow (e.g. world/scripts/alert-sweep.sh), which counts pending un-claimed
"Unblock:" goals filed from inbound alerts (origin_signal "alert-email:<key>")
on its tracking aspiration, and surfaces the backlog via
team-state.inbox_alert_backlog for the precheck partner-snapshot phase.

Value written to team-state.<field>:
    null                                                      zero matching goals
    {count, oldest_age_hours, oldest_goal_id, updated_at}     otherwise

Atomicity: the write reuses team-state.py's `update` CLI, whose
locked_modify_yaml serializes against every other team-state writer.

Isolation: both the aspiration read and the team-state write resolve WORLD_DIR
through core/scripts/_paths.py, which honors MIND_WORLD — so a subprocess with
MIND_WORLD=<tmp> isolates this script for hermetic tests (rb-1555).

Fail-open: any error logs to stderr and exits 0. A missed backlog refresh is
non-fatal — the next sweep recomputes from scratch. Never block the caller.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

MATCH_STATUSES = ("pending", "in-progress")


def _eprint(*a):
    print(*a, file=sys.stderr)


def _resolve_aspirations_path(source):
    """world -> WORLD_DIR/aspirations.jsonl ; agent -> AGENT_DIR/aspirations.jsonl.

    Imported lazily so an agent-less context (no MIND_AGENT bound) can still
    run the default world scan. Both resolve through _paths (MIND_WORLD-aware).
    """
    if source == "agent":
        from _paths import AGENT_DIR
        return AGENT_DIR / "aspirations.jsonl"
    from _paths import WORLD_DIR
    return WORLD_DIR / "aspirations.jsonl"


def _parse_dt(s):
    """Parse an ISO 8601 local timestamp to datetime; None on any failure.

    guard-420: never let a malformed/absent record field raise during the
    arithmetic below — return None and let the caller treat it as unknown.
    """
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return None


def compute_backlog(aspirations_path, aspiration_id, origin_prefix,
                    title_prefix, now=None):
    """Return the backlog dict, or None when no goal matches.

    Pure read — no writes, no side effects. `now` is injectable for tests.
    A goal matches when ALL hold:
      - origin_signal starts with origin_prefix
      - status in {pending, in-progress}
      - not claimed (claimed_by absent / falsy)
      - title starts with title_prefix (when title_prefix is non-empty)
    """
    if now is None:
        now = datetime.now()
    if not aspirations_path.exists():
        return None

    matches = []
    for line in aspirations_path.read_text(
            encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            asp = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(asp, dict) or asp.get("id") != aspiration_id:
            continue
        for g in asp.get("goals", []) or []:
            if not isinstance(g, dict):
                continue
            if not (g.get("origin_signal") or "").startswith(origin_prefix):
                continue
            if (g.get("status") or "") not in MATCH_STATUSES:
                continue
            if g.get("claimed_by"):          # actively claimed -> not backlog
                continue
            if title_prefix and not (g.get("title") or "").startswith(title_prefix):
                continue
            matches.append(g)

    if not matches:
        return None

    # Oldest = earliest created_at. Goals with an unparseable created_at sort
    # LAST (so a missing timestamp never wins "oldest" spuriously).
    def _age_key(g):
        dt = _parse_dt(g.get("created_at"))
        return (dt is None, dt or now)

    oldest = min(matches, key=_age_key)
    oldest_dt = _parse_dt(oldest.get("created_at"))
    if oldest_dt is not None:
        oldest_age_hours = round(max(0.0, (now - oldest_dt).total_seconds() / 3600.0), 1)
    else:
        oldest_age_hours = 0.0

    return {
        "count": len(matches),
        "oldest_age_hours": oldest_age_hours,
        "oldest_goal_id": oldest.get("id"),
        "updated_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def write_team_state_field(field, value):
    """Set team-state.<field> via team-state.py's atomic `update` CLI.

    value=None writes JSON null (the documented zero-backlog form). Returns
    True on success, False on a (non-fatal, logged) write failure.
    """
    team_state_py = HERE / "team-state.py"
    proc = subprocess.run(
        [sys.executable, str(team_state_py), "update",
         "--field", field, "--value", json.dumps(value), "--operation", "set"],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        _eprint("inbox-backlog-update: team-state write failed "
                f"(rc={proc.returncode}): "
                f"{(proc.stderr or proc.stdout or '').strip()[:300]}")
        return False
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Compute + write an inbox backlog counter to team-state.")
    ap.add_argument("--aspiration", required=True,
                    help="Aspiration id to scan (e.g. asp-115)")
    ap.add_argument("--origin-prefix", required=True,
                    help='origin_signal prefix to match (e.g. "alert-email:")')
    ap.add_argument("--source", choices=["world", "agent"], default="world",
                    help="Which aspiration queue (default world)")
    ap.add_argument("--field", default="inbox_alert_backlog",
                    help="team-state field to write (default inbox_alert_backlog)")
    ap.add_argument("--title-prefix", default="Unblock:",
                    help='Only count goals whose title starts with this '
                         '(default "Unblock:"; pass "" to count all)')
    ap.add_argument("--print", action="store_true", dest="do_print",
                    help="Also print the computed backlog JSON to stdout")
    args = ap.parse_args(argv)

    try:
        asp_path = _resolve_aspirations_path(args.source)
        backlog = compute_backlog(asp_path, args.aspiration,
                                  args.origin_prefix, args.title_prefix)
        written = write_team_state_field(args.field, backlog)
        if args.do_print:
            print(json.dumps({"field": args.field, "value": backlog,
                              "written": written}))
    except Exception as e:        # fail-open on ANY error
        _eprint(f"inbox-backlog-update: non-fatal error: {e}")
    return 0                       # always exit 0 — never block the caller


if __name__ == "__main__":
    sys.exit(main())
