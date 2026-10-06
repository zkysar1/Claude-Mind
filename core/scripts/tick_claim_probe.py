#!/usr/bin/env python3
"""tick_claim_probe.py -- who may merge on this box right now? (, )

Asked by `iteration-push.sh --ff-only`, the root-cron sync tick. The tick used to take
a clean fast-forward only and LOG every other shape. Measured 2026-09-30 (g-375-94):
a worker Body that was ahead of origin, or dirty in its own store file, sat 2.29 h
behind origin between units while every tick only logged. The decided rule: when the
box's Body holds no claim, the tick runs the loop's own --no-push integrate, which is
the same integrate the loop runs at its next boundary.

A box that no worker Body has run on had no answer, so the tick only logged there too.
Measured 2026-10-01 on cc-14 (g-375-108), five agents configured and interactive seats
only: 250 log-only ticks after its last integrate on 2026-09-24, and 232 commits (9.5 h)
behind, because own-cloud keeps a few tracked agents/* store files modified there and no
loop ever merged them. The decided rule: when no loop runs on the box, the tick
fast-forwards around those files itself (iteration-push.sh _ip_tick_churn_ff).

This script answers that one question and nothing else. It prints ONE line:

    <verdict> <agent> <evidence>

verdict is `none` (positively no claim held by this box's worker Body), `noloop` (no
worker Body has run here and no loop runs here now), `held`, or `unknown`. agent is `-`
when it could not be resolved, and always `-` for `noloop`. `none` licenses the handoff
to the loop's integrate, and `noloop` the fast-forward around agents/* files. The tick
treats `held`, `unknown`, a crash or any other output as "log only", which is exactly
its behaviour before either change. Always exits 0.

WHAT "THIS BOX'S BODY" MEANS. A root cron has no session: no MIND_AGENT, no MIND_SID.
  agent     MIND_AGENT when set, else the ONE agent with a local-paths.conf on this box.
            Zero or several -> unknown: a multi-resident box is out of scope.
  sessions  the per-session dirs under agents/<agent>/sessions/. They are gitignored and
            never synced, so they name exactly the sessions that ran on this box. Every
            one must be a worker Body session: its body-manifest.yaml records role
            worker, since /start writes that file for a reducer and an observer seat too
            (g-375-113). Any other session -> unknown: a reducer claims in the
            agent-level `in_flight` row, which carries no sid, so a box that may host
            the reducer cannot be answered.
  claim     agent_status.<agent>.in_flight_bodies.<sid> for each of those sessions. A row
            holds a claim while the goal it names is not closed. So does an open goal in
            this box's copy of the world queue whose claimed_by_sid is one of those
            sessions, row or no row (g-375-137).

A BOX NO WORKER BODY HAS RUN ON (g-375-108). The rule above needs one agent and Body
sessions only. When no session on the box, of any agent configured here, records the
worker role, the question is whether a loop runs here at all, and the box-local answer
is the file the loop's own stop hook reads: agents/<agent>/session/agent-state.
  worker    a session's body-manifest.yaml records its role. /start writes the same file
            for a reducer and for an observer seat (body-manifest.py VALID_ROLES), so
            only `role: worker` makes a box a worker Body box, and a manifest with no
            role reads as the writer's default, `worker`. A manifest that cannot be read
            may be a worker's, so it answers unknown, and so does a role outside
            VALID_ROLES, which may be a newer kind of session that runs a loop. The
            role is read first because a worker Body's loop leaves agent-state IDLE: on
            2026-10-02 all ten zc Bodies had one worker-role session and read IDLE
            (g-375-108).
  RUNNING   for any agent configured here -> unknown: that loop's integrate owns the
            merge. The loop running here writes it, so a copy that came from another
            box, or one a crash left behind, can only err toward unknown. A missing file
            is not RUNNING, since /start writes it before a loop runs.

WHY THIS BOX'S OWN ROW FILE, NOT THE DAEMON. A Body writes its claim, and its own release,
into this box's row file first; the store of record catches up from here. So for this
box's own sids a stale file errs toward `held` (a release written elsewhere that has not
arrived yet). A row that was never written is the exception: see the next paragraph.
Reading it directly also keeps a root cron from spawning a daemon, and the row read itself
needs no store credentials.

THE CLAIM OF RECORD TOO, NOT ONLY THE ROW (g-375-137). aspirations-claim.sh writes the row
AFTER the claim commits, so a wrapper that dies in between leaves a claim with no row, and
the row file alone then reads `none`. Measured 2026-10-05 on the worker Bodies: 2 of
38 claims in 24 h committed with no row, and zc-05's tick then answered `none` and
integrated 19 times under its held claim. So the probe also reads this box's copy of the
world queue, and an open goal whose claimed_by_sid is this box's Body session answers
`held`. A worker claims through this box's own daemon, which writes that copy (zc-05's copy
carried its row-less claim when read that day), and a release or close written on another
box that has not arrived yet errs toward `held`. Claims are erased at release and at close,
so only the few lines that carry the key are parsed. A copy that cannot be read in full
answers `unknown`: a scan that cannot finish must never read as "no claim". The copy is
read from disk, like the row file, with no store credentials.

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
non-Body session ran on a worker Body box, a session's manifest cannot be read or records
a role this probe does not know, an agent is RUNNING, the row file is missing or
unreadable, a goal id does not resolve, this box's copy of the world queue cannot be read
in full, or anything raises.

SIDE EFFECTS. Takes no lock and writes nothing of its own. Resolving a row's goal is not
free: goal-resolve.py first refreshes the world archive from the store (one HEAD, plus a
GET when the local copy is stale) and may rewrite that local copy. From root cron the
store credentials come from .env.local, which get_backend() loads itself. A box with no
row reads nothing from the store, and neither does a box no worker Body has run on.

    python3 core/scripts/tick_claim_probe.py --repo /opt/ayoai-mind
"""

import argparse
import importlib.util
import json
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

# The file a session dir carries once /start has run in it. A reducer and an observer
# seat write it too, so the role inside it, not its presence, marks a worker Body
# ().
BODY_MARKER = "body-manifest.yaml"

# The role /start records for a worker Body (body-manifest.py VALID_ROLES).
WORKER_ROLE = "worker"

# Every role /start can record, as body-manifest.py VALID_ROLES lists them (a test keeps
# the two equal). A role outside it is a doubt: it may name a newer kind of session that
# runs a loop.
KNOWN_ROLES = ("reducer", "worker", "observer")

# The world queue file, under the world dir: where a claim of record lives ().
QUEUE_FILE = "aspirations.jsonl"


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
    """-> {sid: manifest_role} for the session dirs under sessions_root (empty if absent)."""
    root = Path(sessions_root)
    if not root.is_dir():
        return {}
    return {p.name: manifest_role(p)
            for p in root.iterdir() if p.is_dir() and SID_RE.match(p.name)}


def manifest_role(session_dir):
    """-> the role session_dir's manifest records: '' when it has no manifest, and None
    when the manifest cannot be read as a mapping. A manifest with no role field reads
    as `worker`: the writer's default, and how decide() read every manifest before
    g-375-113."""
    path = Path(session_dir) / BODY_MARKER
    if not path.is_file():
        return ""
    import yaml
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    if not isinstance(doc, dict):
        return None
    return str(doc.get("role") or WORKER_ROLE)


def agent_state(agent_dir):
    """-> agent_dir's agent-state with all whitespace removed, as session-state-get.sh
    prints it, or None when the file is absent. Any other read error raises, and main()
    then answers unknown."""
    try:
        text = (Path(agent_dir) / "session" / "agent-state").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return "".join(text.split())


def open_store_claims(queue_path, sids):
    """-> [(sid, goal_id, status)] for every goal in the world queue file at queue_path
    that one of `sids` holds open (its claimed_by_sid, status not CLOSED), or None when
    the file cannot be read in full (g-375-137)."""
    held = []
    try:
        with open(queue_path, "r", encoding="utf-8") as fh:
            for line in fh:
                # Release and close erase the key, so only an aspiration with a live
                # claim carries it: 12 of zc-05's lines on 2026-10-05.
                if "claimed_by_sid" not in line:
                    continue
                asp = json.loads(line)
                if not isinstance(asp, dict):
                    return None
                for g in asp.get("goals") or []:
                    if not isinstance(g, dict):
                        continue
                    sid = g.get("claimed_by_sid")
                    if isinstance(sid, str) and sid in sids and g.get("status") not in CLOSED:
                        held.append((sid, str(g.get("id") or "?"), str(g.get("status"))))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return held


def decide(sessions, row, status_of, claims=()):
    """-> (verdict, evidence). Pure, so the rule is testable without a box.

    sessions   {sid: True when its manifest records the worker role} for this box
    row        the agent's team-state row as a dict, or None when it could not be read
    status_of  goal_id -> status string, or None when the id does not resolve
    claims     open_store_claims() for this box's copy of the world queue: the open goals
               its sessions hold, or None when that copy could not be read
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
    # A claim of record holds the box whether or not its row was written ().
    if claims is None:
        return "unknown", "this box's copy of the world queue cannot be read"
    for sid, gid, status in sorted(claims):
        if sid in sessions:
            return "held", f"{sid[:8]} holds {gid} ({status}) in this box's world queue"
    if notes:
        return "none", "; ".join(notes)
    return "none", f"no in-flight row for this box's {len(sessions)} Body session(s)"


def no_loop(roles, states):
    """-> (verdict, evidence) for a box no worker Body has run on, or None when one has
    and decide() answers instead (g-375-108). Pure, so the rule is testable without a box.

    roles   {sid: manifest_role} for every session dir on this box, of every agent
    states  {agent: agent_state} for every agent with a local-paths.conf on this box
    """
    if WORKER_ROLE in roles.values():
        return None
    unread = sorted(sid for sid, role in roles.items() if role is None)
    if unread:
        return "unknown", f"session {unread[0][:8]} has a {BODY_MARKER} that cannot be read"
    odd = sorted(sid for sid, role in roles.items() if role and role not in KNOWN_ROLES)
    if odd:
        return "unknown", (f"session {odd[0][:8]} records role {roles[odd[0]]!r}, "
                           f"which this probe does not know")
    running = sorted(agent for agent, state in states.items() if state == "RUNNING")
    if running:
        return "unknown", f"{running[0]} is RUNNING here, so its loop owns the merge"
    return "noloop", (f"no loop runs here: {len(states)} agent(s) configured, none RUNNING; "
                      f"{len(roles)} session(s), none a worker Body")


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
    # A box no worker Body has run on is answered from this box's own files ().
    roles = {}
    for conf_agent in confs:
        roles.update(local_sessions(_paths.agent_sessions_root(conf_agent)))
    answer = no_loop(roles, {a: agent_state(_paths.agent_dir(a)) for a in confs})
    if answer:
        return answer[0], "-", answer[1]
    agent, why = resident_agent(confs, os.environ.get("MIND_AGENT", "").strip())
    if not agent:
        return "unknown", "-", why
    world = _paths.WORLD_DIR
    if not world:
        return "unknown", agent, "WORLD_PATH does not resolve"
    # A session is a worker Body by the role its manifest records ().
    sessions = {sid: role == WORKER_ROLE
                for sid, role in local_sessions(_paths.agent_sessions_root(agent)).items()}
    import yaml
    import _team_state
    try:
        with open(_team_state.row_path(world, agent), "r", encoding="utf-8") as fh:
            row = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        row = None
    claims = open_store_claims(Path(world) / QUEUE_FILE, set(sessions))
    verdict, evidence = decide(sessions, row, _goal_status_resolver(world), claims)
    return verdict, agent, evidence


def main(argv=None):
    ap = argparse.ArgumentParser(description="Who may merge on this box: does its worker Body "
                                             "hold a claim, or does no loop run here at all? "
                                             "Prints '<none|noloop|held|unknown> <agent> <evidence>'.")
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
