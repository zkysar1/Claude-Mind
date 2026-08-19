"""_peer_registry — SSOT for the cross-deployment peer name set.

Extracted from insight-trigger-sweep.py under g-115-5890, whose filing text is
explicit about why: "do not re-derive the name set, because a second copy would
drift and getting it wrong pushes local work at a peer." A second consumer
(peer-thread-relay-sweep.py) needed the same names, and the loader lived in a
HYPHENATED module that cannot be imported — which is the mechanical reason a
copy would otherwise have been forced rather than chosen. Extracting removes the
temptation instead of documenting it.

Scope is deliberately ONE thing: which agent names belong to a PEER deployment.
Two neighbouring facts are owned elsewhere and are NOT re-implemented here —
re-deriving either is the same defect this module exists to prevent:

  self env id  -> _paths.ENVIRONMENT_ID  (goal-selector.py:3282 records a prior
                  local copy of this that referenced an unimported PROJECT_ROOT
                  and raised NameError inside an `except OSError` guard)
  local roster -> _agents.get_active_agents(), or insight-trigger-sweep's
                  _local_roster() shard-basename form

Fail-open throughout: an unreadable registry yields NO peer names, which makes
every bare name resolve LOCAL. That is the safe direction — the dangerous error
is claiming a local agent's name for a peer, never the reverse.

DELIBERATE NON-SHARING — read before "finishing" the extraction. Only the
LOADER is shared with insight-trigger-sweep.py. Its resolve_addressing keeps its
own peer-env policy, and the two are OPPOSITE on purpose when self_env is
unresolvable:

    insight-trigger-sweep : `{e for e in registry if e != self_env}` — with
                            self_env None, EVERY env reads as a peer, so the
                            collision set grows and more targets are REFUSED.
                            Its action is refusing a board post: refusing is
                            recoverable and names the post, so failing large is
                            its safe direction.
    peer_envs() here      : returns EMPTY, so nothing is a peer and bare names
                            stay local. Its consumer decides whether to surface
                            work as belonging to ANOTHER DEPLOYMENT; a false
                            peer pushes local work at a peer, so failing small
                            is ITS safe direction.

Unifying these would silently flip one consumer's fail-safe direction under an
unresolvable ENVIRONMENT_ID — the same trap peer_surface.routing_tag_targets_agent
already documents ("Different action, different fail-safe direction. Do NOT
'align' the two by copying the sweep's posture."). Share I/O; never share policy
between consumers whose wrong answers cost different things.
"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_REGISTRY_DIR = PROJECT_ROOT / "core" / "config" / "environments"


def load_env_registry(registry_dir=None):
    """{env_id: entry-dict} from core/config/environments/*.yaml.

    Each entry may carry an OPTIONAL `known_agents` list — agent names known to
    operate in that deployment (the durable half of the collision set). Absence
    of the field contributes no names. Fail-open: any read/parse error yields
    fewer entries, never an abort — a missing registry degrades to "no peers
    known", which preserves the bare-name-means-local installed base.

    `registry_dir` is a TEST seam only; production callers pass nothing.
    """
    registry = {}
    base = Path(registry_dir) if registry_dir else ENV_REGISTRY_DIR
    try:
        import yaml
        if base.is_dir():
            for p in sorted(base.glob("*.yaml")):
                try:
                    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                except Exception:
                    continue
                env_id = str(data.get("environment_id") or "").strip()
                if env_id:
                    registry[env_id] = data
    except Exception:
        pass
    return registry


def peer_envs(registry, self_env):
    """Registry env-ids that are NOT this deployment.

    An unresolvable `self_env` (None/"") means we cannot tell which entry is
    ours, so EVERY registered env would read as a peer — including this one.
    That direction is unsafe: it would let a local agent's name be claimed for a
    peer, which is exactly the "pushes local work at a peer" failure the module
    docstring names. So an unresolvable self_env yields NO peers.
    """
    if not self_env:
        return set()
    return {e for e in registry if e != self_env}


def peer_agent_names(registry, self_env):
    """Agent names declared by peer deployments' `known_agents`.

    This is the REGISTRY-derived half of the collision set only. Callers that
    also learn names from observed `<agent>@<env-id>` traffic (see
    insight-trigger-sweep.resolve_addressing) union that in themselves — it is
    trigger-local evidence, not registry fact, and does not belong here.
    """
    names = set()
    for env in peer_envs(registry, self_env):
        for name in (registry.get(env, {}).get("known_agents") or []):
            name = str(name).strip()
            if name:
                names.add(name)
    return names


def peer_env_for_agent(registry, self_env, agent_name):
    """Which peer env declares `agent_name`, or None.

    Returns the FIRST match in sorted env order when more than one peer declares
    the same name. Deterministic rather than arbitrary, and the ambiguity is
    surfaced by classify_agent_name below rather than silently resolved here.
    """
    if not agent_name:
        return None
    for env in sorted(peer_envs(registry, self_env)):
        for name in (registry.get(env, {}).get("known_agents") or []):
            if str(name).strip() == agent_name:
                return env
    return None


def classify_agent_name(agent_name, registry, self_env, roster):
    """One agent name -> ("peer"|"ambiguous"|"local"|"unknown", peer_env|None).

    The four verdicts, and why "ambiguous" is not collapsed into either
    neighbour (cross-deployment-channel.md "Addressing an agent"):

      peer      — declared by a peer's known_agents and NOT in the local roster.
                  Unambiguously theirs.
      ambiguous — declared by a peer AND present in the local roster. The name
                  alone cannot say which deployment is meant. Callers MUST NOT
                  auto-route these; surface them for a human/qualified form
                  (`<agent>@<env-id>`) instead. Live example: `zeta` is both a
                  local agent here and in zds-mind's known_agents.
      local     — in the local roster only.
      unknown   — neither. Not evidence of a peer; a bracket token in a subject
                  line is free text and most tokens are not agent names at all.

    Collapsing "ambiguous" into "peer" would relay a LOCAL agent's work to a
    peer deployment; collapsing it into "local" would silently strand a genuine
    peer directive. Both are the failure this file exists to prevent, in
    opposite directions, which is why it stays its own verdict.
    """
    name = str(agent_name or "").strip()
    if not name:
        return ("unknown", None)
    roster = set(roster or ())
    env = peer_env_for_agent(registry, self_env, name)
    if env and name in roster:
        return ("ambiguous", env)
    if env:
        return ("peer", env)
    if name in roster:
        return ("local", None)
    return ("unknown", None)
