#!/usr/bin/env python3
"""Resolve a goal's PEER ORIGIN — which agent, in which sibling deployment,
injected or filed it — into the canonical ``<agent>@<env-id>`` address the
cross-deployment channel mandates (core/config/conventions/
cross-deployment-channel.md, "Addressing an agent").

WHY THIS EXISTS (g-361-03, goal-completion audit 2026-08-16). Cross-world
goals arrive two ways — an ``asp-xw-<ts>`` sandbox aspiration, or a
``g-NNN-NN`` goal injected into a standard aspiration (``--target-aspiration``)
— and when THIS world completes one, nothing told the origin world. The peer
does read this world's coordination board and cites our ids back (two measured
round-trips), so an explicitly ADDRESSED ``Completed:`` post is the delivery
path that already works. To address it, do_verify needs the origin as ONE
shape. Measured on the 313-goal in-progress snapshot the origin appears as
``omni@zds-mind`` (canonical), ``zds-mind/omni`` (slash), ``zds-mind``
(env-only) and, for peer-FILED goals, no cross_world_origin at all — only
``filed_by_agent: omni``. This module folds all four into one answer.

RESOLUTION ORDER (first hit wins), goal record then its aspiration:
  1. ``cross_world_origin`` / ``injected_by`` on the GOAL
  2. ``cross_world_origin`` / ``injected_by`` on the ASPIRATION (asp-xw shape)
  3. ``filed_by_agent`` on the goal when it names an agent that is NOT on the
     local roster and appears in exactly ONE peer registry ``known_agents``
     (an ambiguous or roster-local name resolves to nothing — never guess a
     peer; guard-2015 / cross-deployment-channel.md clause 3)

SHAPES: ``a@e`` -> (a, e); ``e/a`` -> (a, e) [legacy slash]; ``e`` alone
-> (None, e) [env-only: address stays None — an env is not an agent, so no
``requires_action_by`` can be formed; callers get the env for a bare tag].
An env is a PEER only when it is a registered environment id other than this
world's ENVIRONMENT_ID; a value naming THIS env is not a cross-world origin.

CLI:  xw_origin.py --goal <goal-id> [--file <aspirations.jsonl>] [--source world|agent]
      prints one JSON object ({"agent","env","address","shape","field"}) or
      nothing (rc 0 either way — a caller in a close path must never fail on
      provenance lookup; rc 2 only on usage error).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

_ENV_DIR = _HERE.parent / "config" / "environments"


def registry(env_dir: Path = _ENV_DIR) -> Dict[str, List[str]]:
    """{env-id: [known agents]} from core/config/environments/*.yaml.
    Cheap line scan (the files are tiny and flat) so this stays importable
    from a Bash close path without a yaml dependency."""
    out: Dict[str, List[str]] = {}
    if not env_dir.is_dir():
        return out
    for p in sorted(env_dir.glob("*.yaml")):
        env_id = p.stem
        agents: List[str] = []
        in_known = False
        try:
            for raw in p.read_text(encoding="utf-8").splitlines():
                line = raw.rstrip()
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                if s.startswith("environment_id:"):
                    env_id = s.split(":", 1)[1].strip().strip("'\"") or env_id
                    continue
                if s.startswith("known_agents:"):
                    in_known = True
                    rest = s.split(":", 1)[1].strip()
                    if rest.startswith("["):
                        agents.extend(a.strip().strip("'\"") for a in
                                      rest.strip("[]").split(",") if a.strip())
                        in_known = False
                    continue
                if in_known:
                    if s.startswith("- "):
                        agents.append(s[2:].strip().strip("'\""))
                        continue
                    in_known = False
        except OSError:
            continue
        out[env_id] = agents
    return out


def self_env() -> str:
    return (os.environ.get("ENVIRONMENT_ID") or "").strip()


def parse_origin(value: Any, envs: Dict[str, List[str]], me: str
                 ) -> Optional[Tuple[Optional[str], str, str]]:
    """One provenance VALUE -> (agent|None, env, shape) when it names a PEER
    deployment; None when it is empty, malformed, or names this world."""
    text = str(value or "").strip()
    if not text:
        return None
    agent: Optional[str]
    env: str
    if "@" in text:
        agent, _, env = text.partition("@")
        shape = "canonical"
    elif "/" in text:
        env, _, agent = text.partition("/")
        shape = "slash"
    else:
        agent, env, shape = None, text, "env-only"
    agent = (agent or "").strip() or None
    env = env.strip()
    if not env or env not in envs or env == me:
        return None
    return agent, env, shape


def resolve(goal: Dict[str, Any], asp: Optional[Dict[str, Any]] = None, *,
            envs: Optional[Dict[str, List[str]]] = None,
            me: Optional[str] = None,
            local_roster: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """The peer origin of `goal` (inside `asp`), or None when it has none."""
    envs = registry() if envs is None else envs
    me = self_env() if me is None else me
    env_only: Optional[Dict[str, Any]] = None
    for holder, label in ((goal, "goal"), (asp or {}, "aspiration")):
        for field in ("cross_world_origin", "injected_by"):
            hit = parse_origin(holder.get(field), envs, me)
            if not hit:
                continue
            agent, env, shape = hit
            found = {"agent": agent, "env": env,
                     "address": f"{agent}@{env}" if agent else None,
                     "shape": shape, "field": f"{label}.{field}"}
            if agent:
                return found
            # env-only names the deployment but no agent to address; keep
            # looking — the sibling field usually carries the agent (measured:
            # every env-only cross_world_origin sat beside a canonical
            # injected_by) — and fall back to it only if nothing does.
            env_only = env_only or found
    filed = str(goal.get("filed_by_agent") or "").strip()
    if filed and "@" not in filed and filed not in (local_roster or []):
        homes = [e for e, agents in envs.items() if e != me and filed in agents]
        if len(homes) == 1:
            return {"agent": filed, "env": homes[0],
                    "address": f"{filed}@{homes[0]}",
                    "shape": "filed_by", "field": "goal.filed_by_agent"}
    return env_only


def find_goal(path: Path, goal_id: str
              ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    if not path.exists():
        return None, None
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                asp = json.loads(line)
            except json.JSONDecodeError:
                continue
            for g in asp.get("goals") or []:
                if isinstance(g, dict) and g.get("id") == goal_id:
                    return g, asp
    return None, None


def _local_roster() -> List[str]:
    """Agent names present on this box (agents/<name>/) — a bare
    filed_by_agent matching one of these is LOCAL, not a peer."""
    try:
        from _paths import agents_root  # type: ignore
        root = Path(agents_root())
    except Exception:
        return []
    try:
        return sorted(p.name for p in root.iterdir() if p.is_dir()
                      and not p.name.startswith("."))
    except OSError:
        return []


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--goal", required=True)
    ap.add_argument("--file", help="aspirations.jsonl to scan (default: the "
                    "world or agent store per --source)")
    ap.add_argument("--source", default="world", choices=("world", "agent"))
    args = ap.parse_args(argv)
    path: Optional[Path] = Path(args.file) if args.file else None
    if path is None:
        try:
            from _paths import WORLD_DIR, AGENT_DIR  # type: ignore
            base = WORLD_DIR if args.source == "world" else AGENT_DIR
            path = Path(base) / "aspirations.jsonl"
        except Exception:
            return 0
    goal, asp = find_goal(path, args.goal)
    if goal is None:
        return 0
    hit = resolve(goal, asp, local_roster=_local_roster())
    if hit:
        print(json.dumps(hit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
