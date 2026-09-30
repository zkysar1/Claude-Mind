#!/usr/bin/env python3
"""tick_claim_probe.py -- does this box's worker Body hold a claim right now? ()

Asked by `iteration-push.sh --ff-only`, the root-cron sync tick. The tick used to take
a clean fast-forward only and LOG every other shape. Measured 2026-09-30 (g-375-94):
a worker Body that was ahead of origin, or dirty in its own store file, sat 2.29 h
behind origin between units while every tick only logged. The decided rule: when the
box's Body holds no claim, the tick runs the loop's own --no-push integrate, which is
the same integrate the loop runs at its next boundary.

This script answers that one question and nothing else. It prints ONE line:

    <verdict> <agent> <evidence>

verdict is `none` (positively no claim held on this box), `held`, or `unknown`, and
agent is `-` when it could not be resolved. Only `none` licenses the handoff. The tick
treats `held`, `unknown`, a crash or any other output as "log only", which is exactly
its behaviour before this change. Always exits 0.

WHAT "THIS BOX'S BODY" MEANS. A root cron has no session: no MIND_AGENT, no MIND_SID.
  agent     MIND_AGENT when set, else the ONE agent with a local-paths.conf on this box.
            Zero or several -> unknown: a multi-resident box is out of scope.
  sessions  the per-session dirs under agents/<agent>/sessions/. They are gitignored and
            never synced, so they name exactly the sessions that ran on this box. Every
            one must be a worker Body session (it carries body-manifest.yaml). Any other
            session -> unknown: a reducer claims in the agent-level `in_flight` row, which
            carries no sid, so a box that may host the reducer cannot be answered.
  claim     agent_status.<agent>.in_flight_bodies.<sid> for each of those sessions. A row
            holds a claim while the goal it names is not closed.

WHY THIS BOX'S OWN ROW FILE, NOT THE DAEMON. A Body writes its claim, and its own release,
into this box's row file first; the store of record catches up from here. So for this
box's own sids a stale file can only err toward `held` (a release written elsewhere that
has not arrived yet), never toward `none`. Reading it directly also keeps a root cron from
spawning a daemon, and the row read itself needs no store credentials.

WHY THE GOAL'S STATUS AND NOT JUST THE ROW. A row can outlive its claim. When another
session writes the close (zc-02's g-115-11303 was completed at 03:20:01 under a different
session), the Body's own close path never clears its row, and body_row_reaper clears only
rows left by an unclean Body death. So a row naming a CLOSED goal holds no claim. The
status comes from goal-resolve.py, which also resolves archived and evicted goals.
That status is read from this box's copy of the world queue, which guard-980 warns is a
mirror. Here a stale copy errs toward `held` (a close that has not arrived), and can only
err toward `none` if a closed goal is reopened and claimed again before the copy catches
up: an integrate during that unit, the case measured low-harm (g-375-93, g-375-94).

FAIL-SAFE. Every doubt answers `unknown`: the synced repo is not this script's tree, a
non-Body session ran here, the row file is missing or unreadable, a goal id does not
resolve, or anything raises.

SIDE EFFECTS. Takes no lock and writes nothing of its own. Resolving a row's goal is not
free: goal-resolve.py first refreshes the world archive from the store (one HEAD, plus a
GET when the local copy is stale) and may rewrite that local copy. From root cron the
store credentials come from .env.local, which get_backend() loads itself. A box with no
row reads nothing from the store.

    python3 core/scripts/tick_claim_probe.py --repo /opt/ayoai-mind
"""

import argparse
import importlib.util
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# Goal statuses after which no claim is held (core/config/conventions/aspirations.md).
# `blocked` is deliberately OPEN: a row naming it is a claim the Body has not released.
CLOSED = frozenset({"completed", "skipped", "expired", "decomposed", "superseded"})

# A session dir's name IS its sid: 32 hex for a zakcode Body, a dashed UUID for others.
SID_RE = re.compile(r"^[0-9a-f][0-9a-f-]{7,63}$")

# The file only a worker Body's session dir carries.
BODY_MARKER = "body-manifest.yaml"


def resident_agent(conf_agents, env_agent):
    """-> (agent or None, why). MIND_AGENT when set, else the box's only agent config."""
    if env_agent:
        if env_agent in conf_agents:
            return env_agent, "MIND_AGENT"
        return None, f"MIND_AGENT={env_agent} has no local-paths.conf here"
    if len(conf_agents) == 1:
        return conf_agents[0], "its only local-paths.conf"
    return None, f"{len(conf_agents)} agents have a local-paths.conf here"


def local_sessions(sessions_root):
    """-> {sid: is_worker_body} for the session dirs under sessions_root (empty if absent)."""
    root = Path(sessions_root)
    if not root.is_dir():
        return {}
    return {p.name: (p / BODY_MARKER).is_file()
            for p in root.iterdir() if p.is_dir() and SID_RE.match(p.name)}


def decide(sessions, row, status_of):
    """-> (verdict, evidence). Pure, so the rule is testable without a box.

    sessions   {sid: is_worker_body} for this box (local_sessions)
    row        the agent's team-state row as a dict, or None when it could not be read
    status_of  goal_id -> status string, or None when the id does not resolve
    """
    others = sorted(sid for sid, body in sessions.items() if not body)
    if others:
        return "unknown", f"a non-Body session ran here ({others[0][:8]})"
    if not isinstance(row, dict):
        return "unknown", "this box's team-state row is missing or unreadable"
    bodies = row.get("in_flight_bodies") or {}
    if not isinstance(bodies, dict):
        return "unknown", "in_flight_bodies is not a mapping"
    notes = []
    for sid in sorted(sessions):
        entry = bodies.get(sid)
        if entry is None:
            continue
        gid = entry.get("goal_id") if isinstance(entry, dict) else None
        if not gid:
            return "unknown", f"row {sid[:8]} names no goal id"
        status = status_of(gid)
        if status is None:
            return "unknown", f"row {sid[:8]} names {gid}, which does not resolve"
        if status not in CLOSED:
            return "held", f"row {sid[:8]} names {gid} ({status})"
        notes.append(f"row {sid[:8]} names {gid} ({status})")
    if notes:
        return "none", "; ".join(notes)
    return "none", f"no in-flight row for this box's {len(sessions)} Body session(s)"


def _goal_status_resolver(world):
    """goal_id -> status via goal-resolve.py (live, archived or evicted), else None."""
    spec = importlib.util.spec_from_file_location("goal_resolve", HERE / "goal-resolve.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def status_of(gid):
        r = mod.resolve(gid, world=str(world))
        return None if r.get("disposition") == "unknown" else r.get("status")
    return status_of


def probe(repo):
    """-> (verdict, agent or '-', evidence) for the tree at repo."""
    import _paths
    root = Path(_paths.PROJECT_ROOT).resolve()
    if Path(repo).resolve() != root:
        return "unknown", "-", f"--repo {repo} is not this script's tree ({root})"
    confs = [c.parent.name for c in _paths.enumerate_agent_confs()]
    agent, why = resident_agent(confs, os.environ.get("MIND_AGENT", "").strip())
    if not agent:
        return "unknown", "-", why
    world = _paths.WORLD_DIR
    if not world:
        return "unknown", agent, "WORLD_PATH does not resolve"
    sessions = local_sessions(_paths.agent_sessions_root(agent))
    import yaml
    import _team_state
    try:
        with open(_team_state.row_path(world, agent), "r", encoding="utf-8") as fh:
            row = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        row = None
    verdict, evidence = decide(sessions, row, _goal_status_resolver(world))
    return verdict, agent, evidence


def main(argv=None):
    ap = argparse.ArgumentParser(description="Does this box's worker Body hold a claim? "
                                             "Prints '<none|held|unknown> <agent> <evidence>'.")
    ap.add_argument("--repo", required=True, help="the tree the sync tick is about to integrate")
    args = ap.parse_args(argv)
    try:
        verdict, agent, evidence = probe(args.repo)
    except Exception as e:  # noqa: BLE001 -- any doubt is `unknown`, which keeps the tick log-only
        verdict, agent, evidence = "unknown", "-", f"probe failed: {type(e).__name__}: {e}"
    print(f"{verdict} {agent} {evidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
