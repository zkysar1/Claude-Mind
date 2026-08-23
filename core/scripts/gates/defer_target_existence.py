"""Defer-target existence — does the goal id a defer NAMES actually exist?
(g-115-7282)

A `defer_reason` that names a DEPENDENCY goal id which resolves in no queue
and no archive freezes its goal against a target nothing can ever clear. The
premise-axis recheck (`defer-recheck.py`) re-probes the dependency forever and
correctly keeps the goal frozen, because the dependency really is unresolved —
so no amount of re-probing frees it (`reclaim-routed-work.md`, the RULE-vs-
PREMISE axes). The only cheap moment to catch a bad id is the WRITE.

ADVISORY BY CONSTRUCTION. `evaluate()` returns a message; it never refuses.
The population it guards is one where the CITATIONS were almost always right
and the STORE lost the records — measured 2026-08-22 across the live corpus:
of 51 cited ids resolving nowhere, 41 still have a world-surface footprint and
20 appear in committed framework files; only 5 have no footprint at all. A
refusal would therefore block correct writes to punish a defect on the other
side of the system. Warning is the honest strength.

THREE DESIGN CONSTRAINTS, each measured rather than reasoned:

1. **Role-aware ids only.** `_dep_ids()` imports `_extract_dep_ids` from
   `defer-recheck.py` instead of re-typing its regexes, so this warns on
   exactly the population the framework's own clearing path acts on. A wide
   "any goal id in the text" predicate flags 34 goals where the role-aware one
   flags 3; the other 31 merely MENTION a sibling id as context ("may be
   legitimately subset-scoped to the g-004-07 composite"). A warning that fires
   on 31 correct defers gets trained away as noise — which would leave the
   check technically present and practically dead (guard-4883).

2. **Never gate the caller on `is_narrative_defer`.** That predicate returns
   False for every `STRUCTURED_DEFER_PREFIXES` value, and structured defers are
   where dependency ids actually live: of the 79 non-terminal defers citing a
   goal id, **79 were structured and 0 were narrative**. Reusing it would fire
   on zero of the real population while looking correct in review — the
   guard-1802 class (a predicate narrower than the population it audits reports
   clean forever). Callers trigger on the FIELD.

3. **Resolve against every queue, not the one being written.** A world-queue
   defer legitimately cites an agent-queue goal. `sources_for()` is the single
   builder of that path list so the CLI and daemon call sites cannot drift into
   asking different questions. Measured cost 0.07s over 12 files / 5,378 ids.

Blind spot, stated rather than hidden: the agents root is enumerated by glob,
so only agent dirs present on THIS box are visible; a peer box's private agent
queue is not. World is the shared store and carries the overwhelming majority
of cited ids, so this under-warns rather than over-warns — the safe direction
for an advisory.

Daemon safety: no env reads, no globals mutated after first use, and every
path is passed in by the caller (endpoints must resolve through `ctx.paths`,
never module-level constants — `.claude/rules/path-resolution.md`).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

__all__ = ["sources_for", "known_goal_ids", "evaluate", "MESSAGE_PREFIX"]

MESSAGE_PREFIX = "[defer-target-advisory]"

_EXTRACT = None


def _extractor():
    """Memoized `_extract_dep_ids` from defer-recheck.py.

    The hyphenated filename is not importable by name, so the importlib dance
    lives here ONCE rather than at each call site. Returns None if the module
    cannot be loaded — callers degrade to silence, never to a wide fallback
    regex (a fallback would silently re-open constraint 1 above).
    """
    global _EXTRACT
    if _EXTRACT is None:
        try:
            path = Path(__file__).resolve().parent.parent / "defer-recheck.py"
            spec = importlib.util.spec_from_file_location(
                "_defer_recheck_dep_ids", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _EXTRACT = mod._extract_dep_ids
        except Exception:
            _EXTRACT = False
    return _EXTRACT or None


def sources_for(world_dir, agents_root) -> List[Path]:
    """Every aspirations store a cited goal id could legitimately live in.

    Single builder for both call sites. `agents_root` is the parent directory
    holding all agent dirs — `agent_dir(name).parent` on the daemon side,
    `agents_root()` on the CLI side. Pass None to skip the agent sweep.
    """
    out: List[Path] = []
    if world_dir is not None:
        w = Path(world_dir)
        out.append(w / "aspirations.jsonl")
        out.append(w / "aspirations-archive.jsonl")
    if agents_root is not None:
        r = Path(agents_root)
        try:
            out += sorted(r.glob("*/aspirations.jsonl"))
            out += sorted(r.glob("*/aspirations-archive.jsonl"))
        except Exception:
            pass
    return out


def known_goal_ids(sources: Iterable[Path]) -> Set[str]:
    """Every declared goal id across `sources`. Unreadable files are skipped."""
    known: Set[str] = set()
    for p in sources or []:
        try:
            if not Path(p).exists():
                continue
            with open(p, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        asp = json.loads(line)
                    except Exception:
                        continue
                    for g in (asp.get("goals") or []):
                        gid = g.get("id")
                        if gid:
                            known.add(gid)
        except Exception:
            continue
    return known


def evaluate(goal_id: str, text: Any, sources: Iterable[Path]) -> Dict[str, Any]:
    """Classify a defer_reason write against the known-goal-id universe.

    Returns a dict that is ALWAYS shaped the same:
        {"warn": bool, "cited": [...], "missing": [...],
         "known_count": int, "message": str | None}

    `warn` is False — with `message` None — whenever the answer is not
    trustworthy: no extractor, no cited dependency, or an empty id universe.
    That last case is the positive control in code: a resolver that silently
    built nothing would report EVERY dependency as a phantom, which is the
    exact shape of the founding claim this gate had to re-measure.
    """
    empty: Dict[str, Any] = {"warn": False, "cited": [], "missing": [],
                             "known_count": 0, "message": None}
    extract = _extractor()
    if extract is None:
        return empty
    try:
        cited = [g for g in extract(str(text or "")) if g != goal_id]
    except Exception:
        return empty
    if not cited:
        return empty
    known = known_goal_ids(sources)
    if not known:
        return dict(empty, cited=cited)
    missing = [g for g in cited if g not in known]
    if not missing:
        return {"warn": False, "cited": cited, "missing": [],
                "known_count": len(known), "message": None}
    return {
        "warn": True,
        "cited": cited,
        "missing": missing,
        "known_count": len(known),
        "message": (
            f"{MESSAGE_PREFIX} {goal_id}: defer_reason names dependency goal "
            f"id(s) that resolve in no queue and no archive: "
            f"{', '.join(missing)}. The defer WAS written — this never "
            f"refuses. Confirm the id, or expect this goal to stay frozen "
            f"against a target nothing can clear."
        ),
    }
