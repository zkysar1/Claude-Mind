#!/usr/bin/env python3
# domain-leak-exempt: framework multi-agent-oversight infra — generic aggregation, no domain strings.
"""Collective belief — collective-level interpretability (Phase 5).

Part of the evaluative substrate; eval_harness.py is the keystone + in-code index of all seven.

WHY THIS EXISTS
---------------
DeepMind's AGI->ASI paper (2606.12683) singles out the MULTI-AGENT COLLECTIVE as the
under-studied, safety-critical route to advanced capability — exactly this framework's shape. The
research pass found we already implement every per-agent safeguard the paper recommends
(interpretability via git+reasoning-bank, external audit via fresh-eyes, staged-capability
gating via the curriculum) — but ALL of it is per-AGENT. The one real gap is COLLECTIVE-level
oversight: there is no "what does the team, as a whole, believe and intend?" view, and no probe
for emergent cross-agent issues (shared belief drift, two agents pulling the same lever, an agent
siloed from the rest).

This module is the collective-interpretability aggregator: given each agent's view (its primary
drive + current focus tags + active aspirations), it reports what the team is collectively
focused on, where agents OVERLAP (a coordination-contention surface — the claim protocol prevents
double-claims on a single goal, but not two agents independently working the same area), and who
is SILOED (a divergence / coverage signal). It is descriptive, not prescriptive — it gives a
human (or a /fresh-eyes-collective skill) one screen to reason about the team as a unit.

Pure, hermetic, domain-free. Operates on pre-extracted agent views (deriving focus tags from an
agent's self.md prose is an LLM/dev-team step — this owns the aggregation, not the extraction).
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Dict, List, Optional, Sequence


@dataclass(frozen=True)
class AgentView:
    """One agent's contribution to the collective picture."""
    agent: str
    primary_drive: str = ""
    focus_tags: frozenset = field(default_factory=frozenset)
    aspirations: tuple = ()

    @staticmethod
    def from_dict(d: dict) -> "AgentView":
        if not d.get("agent"):
            raise ValueError(f"agent view missing 'agent': {d!r}")
        tags = d.get("focus_tags", []) or []
        # normalize tags: lowercase + strip, drop empties (so 'NPC ' and 'npc' unify)
        norm = frozenset(t.strip().lower() for t in tags if str(t).strip())
        return AgentView(agent=str(d["agent"]),
                         primary_drive=str(d.get("primary_drive", "")),
                         focus_tags=norm,
                         aspirations=tuple(d.get("aspirations", []) or ()))


def collective_view(views: Sequence[AgentView]) -> dict:
    """Aggregate per-agent views into a collective picture.

    Returns:
      n_agents
      roster              : {agent: primary_drive}
      focus_distribution  : {tag: [agents]} sorted, every focus tag and who holds it
      shared_focus        : [tag] held by >= 2 agents (collective concentration)
      overlaps            : [{agents:[a,b], shared:[tags]}] pairwise focus overlap
                            (a coordination-contention surface to watch)
      siloed              : [agent] whose focus tags intersect NO other agent
                            (divergence / single-point-of-coverage signal)
    """
    agents = [v.agent for v in views]
    dupes = sorted({a for a in agents if agents.count(a) > 1})
    if dupes:
        raise ValueError(f"duplicate agents in views: {dupes}")

    roster = {v.agent: v.primary_drive for v in views}
    dist: Dict[str, List[str]] = {}
    for v in views:
        for t in v.focus_tags:
            dist.setdefault(t, []).append(v.agent)
    focus_distribution = {t: sorted(a) for t, a in sorted(dist.items())}
    shared_focus = sorted(t for t, a in focus_distribution.items() if len(a) >= 2)

    overlaps = []
    for v1, v2 in combinations(views, 2):
        shared = sorted(v1.focus_tags & v2.focus_tags)
        if shared:
            overlaps.append({"agents": sorted([v1.agent, v2.agent]), "shared": shared})

    siloed = []
    for v in views:
        other_tag_sets = [o.focus_tags for o in views if o.agent != v.agent]
        others = frozenset().union(*other_tag_sets) if other_tag_sets else frozenset()
        # Siloed = this agent has focus tags, OTHER tagged agents exist to compare
        # against, and none of them share any of its tags. Requiring `others` to be
        # non-empty avoids false-flagging the only agent, or an agent whose sole peer
        # has no tags (there is no basis to call it diverged from the team).
        if v.focus_tags and others and not (v.focus_tags & others):
            siloed.append(v.agent)

    return {
        "n_agents": len(views),
        "roster": roster,
        "focus_distribution": focus_distribution,
        "shared_focus": shared_focus,
        "overlaps": overlaps,
        "siloed": sorted(siloed),
    }


def load_views(path) -> List[AgentView]:
    """Load agent views from a JSON list or JSONL of view dicts."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix == ".json":
        rows = json.loads(text)
    else:
        rows = [json.loads(ln) for ln in text.splitlines() if ln.strip()
                and not ln.startswith("#")]
    return [AgentView.from_dict(r) for r in rows]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Collective belief — aggregate per-agent views into a team picture.")
    ap.add_argument("--views", required=True,
                    help="JSON list / JSONL of {agent, primary_drive, focus_tags, aspirations}")
    args = ap.parse_args(argv)
    print(json.dumps(collective_view(load_views(args.views)), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
