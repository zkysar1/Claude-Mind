"""Which goal is the CALLER executing? — ambient provenance resolver ().

Shared by CLI tree.py and daemon tree_write.py, same shape as _l1_pick.py: pure
functions, paths as args, no _paths import (daemon-import-safe).

WHY THE AGENT-KEYED ROW CANNOT ANSWER IT. team-state agent_status.<agent>.in_flight
is keyed by AGENT NAME and is written only by the reducer: team-state-in-flight.sh
stamps it only when this box's running-session-id exists AND equals the claiming
MIND_SID. A worker Body's claim writes agent_status.<agent>.in_flight_bodies.<sid>
instead. So the agent-keyed row names the REDUCER's goal (or nothing), and a
provenance stamp read from it on a worker's behalf is misattributed: well-formed,
passes every schema check, and wrong (guard-6256, guard-2474).

THE RULE, in order, for caller session `sid`:
  1. `sid` has an in_flight_bodies row with a goal_id -> that goal. The row exists only
     for a non-reducer Body, so its presence IS the proof of who is asking.
  2. `sid` is the reducer (equals running-session-id) -> the agent-keyed in_flight goal.
     This is the read-side twin of the write-side predicate in team-state-in-flight.sh:
     the one session allowed to write that row is the one allowed to read it back as
     its own (guard-2611: identical predicate on both sides, whitespace stripped alike).
  3. Anything else -> None. No session identity, an observer, a Body whose row was
     released or reaped: the writer cannot be told from a sibling, and an omitted stamp
     is recoverable where a wrong one is not (the field is additive; absent is valid).
"""
from pathlib import Path


def read_reducer_sid(state_dir) -> str:
    """The reducer's session id on THIS box: <agent state dir>/running-session-id.

    '' when the file is absent or unreadable — which is every worker box, and is
    never an error. Whitespace is removed entirely (`tr -d '[:space:]'`), exactly as
    team-state-in-flight.sh reads the same file."""
    try:
        if state_dir is None:
            return ""
        text = (Path(state_dir) / "running-session-id").read_text(encoding="utf-8")
        return "".join(text.split())
    except Exception:
        return ""


def resolve_executing_goal_id(world_dir, agent, sid, reducer_sid, core_path=None):
    """The goal id session `sid` is executing, or None when that cannot be told.

    `reducer_sid` comes from read_reducer_sid(); it is a parameter so this stays pure
    and the caller names which agent state dir it means. Fail-open: any error -> None."""
    try:
        sid = (sid or "").strip()
        if not agent or not sid:
            return None
        from _team_state import read_agent_row
        row = read_agent_row(world_dir, agent, core_path=core_path) or {}
        body = (row.get("in_flight_bodies") or {}).get(sid)
        if isinstance(body, dict):
            gid = body.get("goal_id")
            if isinstance(gid, str) and gid:
                return gid
        if reducer_sid and sid == reducer_sid:
            gid = (row.get("in_flight") or {}).get("goal_id")
            if isinstance(gid, str) and gid:
                return gid
        return None
    except Exception:
        return None
