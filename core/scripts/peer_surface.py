#!/usr/bin/env python3
"""peer_surface.py -- summarize the cross-deployment channel for /prime.

Reads board JSONL on stdin (concatenated output of one or more
`board-read.sh --json` calls) and emits a compact peer summary.

Config arrives via the environment (guard-165: never interpolate shell values
into Python source):
  PEER_SELF_ENV   -- this deployment's ENVIRONMENT_ID (e.g. "ayoai-mind")
  PEER_REGISTRY   -- JSON object {env_id: backend} from core/config/environments/
  PEER_ROSTER     -- comma-separated live local agent names (team-state roster)
  PEER_WINDOW     -- human label for the scan window (e.g. "7d"), display only
  PEER_JSON       -- "1" to emit JSON instead of display lines

See core/config/conventions/cross-deployment-channel.md.

THREE measured traps this file exists to encode (g-115-3927, 2026-07-30):

1. `board-read.sh --json` emits JSONL (one object per line), NOT a JSON array.
   `json.load()` raises "Extra data" on line 2. A naive parse wrapped in
   try/except returns ZERO rows -- a silent false zero that reads as "no peers
   exist", which is the exact belief this surface exists to correct (rb-245).

2. Do NOT filter peer traffic by tag. `cross-deployment` appears on 0 of 139
   inbound posts; a tag query returns empty and reads as "the channel does not
   exist" -- a confident wrong answer about real traffic.

3. "author not in local roster" OVER-counts. Measured over 7d on coordination,
   two non-roster authors (`investigate`, `meta-tiebreaker`) were LOCAL posts
   whose author field captured a fragment of a goal title
   ("Claiming g-115-3007: Investigate: ..."). They are not peers. Rather than
   denylisting them, this module attributes a bare author to a deployment only
   when that same agent name is independently observed in `<agent>@<env-id>`
   form. `omni` attributes (it also posts as `omni@zds-mind`); `investigate`
   does not, so it is reported separately as unattributed rather than silently
   counted or silently dropped.
"""
import json
import os
import sys


def parse_jsonl(stream):
    """Parse concatenated JSONL. Trap 1: this is NOT a JSON array."""
    rows = []
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except (ValueError, TypeError):
            continue
    return rows


def split_author(author):
    """`agent@env-id` -> (agent, env-id). Bare -> (agent, None).

    Split on the FIRST '@' only: every registry env-id contains a hyphen, which
    is why '@' is the separator rather than '-' (an `alpha-ayoai-mind` form
    cannot be split back into agent+env unambiguously).
    """
    text = str(author or "")
    if "@" in text:
        agent, _, env = text.partition("@")
        return agent, env or None
    return text, None


def parse_routing_tag(tag):
    """One board ROUTING TAG -> (agent, env-id|None).

    Strips an optional `requires_action_by:` prefix, then defers to
    split_author so the tag rule and the author rule can never disagree about
    where the boundary is.
    """
    text = str(tag or "")
    if text.startswith("requires_action_by:"):
        text = text.split(":", 1)[1]
    return split_author(text)


def routing_tag_targets_agent(tag, agent_name, self_env):
    """Does one board tag route work to `agent_name` on THIS deployment?

    Accepts BOTH addressing forms, which is the whole point (g-115-4188):
      bare       -- `zeta`, `requires_action_by:zeta`
      qualified  -- `zeta@ayoai-mind`, `requires_action_by:zeta@ayoai-mind`

    The qualified form is what cross-deployment-channel.md RECOMMENDS for a
    collision-set agent (a name declared by more than one deployment's roster),
    and insight-trigger-sweep.py's refusal path actively tells posters to write
    it. The consumers this replaced tested `f"requires_action_by:{agent}" in
    tags` (or `parsed != agent`) -- exact string equality, which the qualified
    form fails. So bare was LOUDLY REFUSED upstream while qualified was
    SILENTLY DROPPED downstream: a pincer, in which qualifying posts to satisfy
    the sweep converts a visible failure into an invisible one.

    NOT A PATTERN MATCH (guard-2860). The comparison is component-wise equality
    on a parsed (agent, env) pair -- never a prefix, glob, or startswith. A
    `split("@")[0] == agent_name` shortcut would admit `zeta@zds-mind`, i.e. a
    PEER DEPLOYMENT's same-named agent, as if it were the local one. The
    cardinality of what this newly admits is therefore a property of the CODE
    (exactly the self-env-qualified form) and not of whatever is on disk.

    UNRESOLVABLE self_env FAILS OPEN, AND DELIBERATELY DIVERGES FROM THE SWEEP
    (guard-1562 -- name what each direction newly admits or refuses).
    insight-trigger-sweep.py REFUSES an explicit @env target when
    ENVIRONMENT_ID is unresolvable, which is right for the sweep: its action is
    FILING A GOAL, so a wrong route is a durable cross-deployment mutation, and
    its refusal is recoverable because it names the post. The consumers here
    print an advisory / decide whether to surface work to their own agent. A
    false positive is one dismissible line; a false negative is a silent
    lane-skip of a directive aimed at this agent -- the guard-1310 incident
    class. Different action, different fail-safe direction. Do NOT "align" the
    two by copying the sweep's posture.
    """
    agent, env = parse_routing_tag(tag)
    if agent != agent_name:
        return False
    if env is None:
        return True          # bare -> unchanged behavior (353 of 360 live tags)
    if not self_env:
        # FALSY, not `is None` (fresh-eyes on this file's own first cut). Both
        # live callers pass _paths.ENVIRONMENT_ID, whose trailing `or None`
        # normalizes falsy to None -- so `is None` was correct for them and the
        # gap was LATENT, never live. But the contract above is "unresolvable
        # fails OPEN", and a caller writing the natural
        # os.environ.get("ENVIRONMENT_ID", "") would hand us "" and get
        # fail-CLOSED: every qualified tag silently dropped, the exact defect
        # this function exists to fix, in the opposite direction and invisible.
        return True
    return env == self_env   # @self-env is me; @peer-env is not


def classify(rows, self_env, registry, roster):
    """Partition board rows into confirmed-peer / attributed / unattributed."""
    peer_envs = {e for e in registry if e != self_env}
    roster = set(roster)

    # Pass 1: agents independently observed in explicit <agent>@<env> form.
    # This is the evidence that lets a bare author be attributed in pass 2.
    marked = {}  # agent -> env
    for row in rows:
        agent, env = split_author(row.get("author"))
        if env and env in peer_envs:
            marked[agent] = env

    confirmed = 0            # explicit @env-id form
    attributed = 0           # bare, but the agent posts under @env-id elsewhere
    unattributed = {}        # bare, non-roster, no @-form evidence -> NOT counted
    unregistered_envs = {}   # marked with an env-id ABSENT from the registry
    by_env = {}
    by_channel = {}

    for row in rows:
        agent, env = split_author(row.get("author"))
        if env and env in peer_envs:
            confirmed += 1
        elif env is None and agent not in roster and agent in marked:
            env = marked[agent]
            attributed += 1
        else:
            if env is None and agent and agent not in roster:
                unattributed[agent] = unattributed.get(agent, 0) + 1
            elif env and env != self_env:
                # An env-id we do not have registered. Do NOT count it as peer
                # traffic -- we cannot vouch for a deployment we have no record
                # of -- but NEVER drop it silently either. This is the highest-
                # signal event this surface can observe (a deployment nobody
                # registered is posting here), and the first draft discarded it
                # with zero trace: not counted, not reported, indistinguishable
                # from no traffic at all. That is the same silent-zero class the
                # module docstring exists to prevent, so it must not reappear as
                # the handling of an UNKNOWN peer. (guard-1616: account for what
                # an aggregate excluded.)
                unregistered_envs[env] = unregistered_envs.get(env, 0) + 1
            continue
        by_env[env] = by_env.get(env, 0) + 1
        channel = str(row.get("channel") or "?")
        by_channel[channel] = by_channel.get(channel, 0) + 1

    return {
        "inbound_total": confirmed + attributed,
        "confirmed": confirmed,
        "attributed": attributed,
        "by_env": by_env,
        "by_channel": by_channel,
        "unattributed": unattributed,
        "unregistered_envs": unregistered_envs,
    }


def main():
    self_env = os.environ.get("PEER_SELF_ENV", "").strip()
    try:
        registry = json.loads(os.environ.get("PEER_REGISTRY") or "{}")
    except (ValueError, TypeError):
        registry = {}
    roster = [a for a in (os.environ.get("PEER_ROSTER") or "").split(",") if a.strip()]
    roster = [a.strip() for a in roster]
    window = os.environ.get("PEER_WINDOW", "").strip() or "window"

    result = classify(parse_jsonl(sys.stdin), self_env, registry, roster)
    peers = sorted(e for e in registry if e != self_env)
    result["peers"] = peers
    result["self_env"] = self_env
    result["self_backend"] = registry.get(self_env)
    result["registry"] = registry

    if os.environ.get("PEER_JSON") == "1":
        print(json.dumps(result, sort_keys=True))
        return 0

    if not peers:
        # No registry read (or a single-entry registry): say so plainly rather
        # than printing "0 peers", which reads as a measurement of isolation.
        print("Peers: registry unreadable or self-only — "
              "see core/config/conventions/cross-deployment-channel.md")
        return 0

    peer_desc = ", ".join("%s:%s" % (e, registry.get(e) or "?") for e in peers)
    self_desc = "%s:%s" % (self_env or "?", registry.get(self_env) or "?")
    print("Peers: %d registered (%s) | self=%s" % (len(peers), peer_desc, self_desc))

    if result["inbound_total"]:
        ranked = sorted(result["by_env"].items(), key=lambda kv: -kv[1])
        if len(ranked) == 1:
            envs = ranked[0][0]
        else:
            envs = ", ".join("%s %d" % (e, n) for e, n in ranked)
        chans = ", ".join(
            "%s %d" % (c, n)
            for c, n in sorted(result["by_channel"].items(), key=lambda kv: -kv[1])
        )
        print("Inbound (%s): %d posts from %s [%s]"
              % (window, result["inbound_total"], envs, chans))
    else:
        # Absence of recent traffic is NOT absence of a channel. Say which.
        print("Inbound (%s): none — channel is live but quiet this window" % window)

    if result["unregistered_envs"]:
        # Loud on purpose, and placed ABOVE the excluded-author footnote: an
        # unknown deployment posting here is more interesting than any count on
        # the line above it.
        detail = ", ".join(
            "%s x%d" % (e, n)
            for e, n in sorted(result["unregistered_envs"].items(), key=lambda kv: -kv[1])
        )
        print("  /!\\ %d post(s) from UNREGISTERED deployment(s): %s"
              % (sum(result["unregistered_envs"].values()), detail))
        print("      Not counted above (cannot vouch for an unregistered peer). "
              "If real, add to core/config/environments/.")

    if result["unattributed"]:
        top = sorted(result["unattributed"].items(), key=lambda kv: -kv[1])[:3]
        print("  (excluded %d non-roster author(s) with no deployment marker: %s)"
              % (len(result["unattributed"]),
                 ", ".join("%s x%d" % (a, n) for a, n in top)))

    print("Cross via core/scripts/peer-board-post.sh "
          "(convention: core/config/conventions/cross-deployment-channel.md)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
