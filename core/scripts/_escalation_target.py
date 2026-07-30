#!/usr/bin/env python3
"""_escalation_target.py — resolve the aspiration that framework canaries file into.

WHY THIS EXISTS (g-001-250)
---------------------------
`cadence-stale-canary.py` and `stale-sentinel-canary.py` both hardcoded
`ASP_ID = "asp-115"`. asp-115 is the UPSTREAM deployment's recurring-infrastructure
aspiration; it exists in neither of THIS deployment's queues. So every escalation
filed here failed:

    {"error": "add_goal_rc_nonzero", "rc": 1,
     "stderr": "{\\"error\\": \\"aspiration_not_found\\",
                 \\"detail\\": \\"Aspiration asp-115 not found in world\\"}"}

It fired at EVERY iteration close, and because the canary's own report recorded
the failure as data rather than raising, nothing escalated the escalation
failure. Two cadences sat at stuck_count 3 indefinitely and four cadence rituals
(fresh-eyes review / program / tree, evolution) stayed overdue.

WHY NOT JUST HARDCODE asp-001
-----------------------------
These are FRAMEWORK files that travel the promotion chain
(dev -> staging -> prod). A deployment-specific constant would either
break upstream, where asp-115 is correct, or be clobbered by the next framework
sync — a failure mode this repo demonstrably has (g-029-94: a sync reverted two
target-ahead files). So the resolution must be BEHAVIOUR that is correct in every
deployment, not a constant that is correct in one.

RESOLUTION ORDER
----------------
1. `stale_cadence.escalation_aspiration` in core/config/aspirations.yaml, when
   non-empty. Explicit deployment override, highest precedence — it wins even if
   the id does not exist, because silently substituting something else would
   discard an explicit operator statement. It is NOT existence-checked, so a
   typo'd value can reintroduce the original bug; that case is flagged in the
   returned label rather than hidden.
2. The first id in `candidates` that ACTUALLY EXISTS in the world or agent queue.
   Existence is the whole point: the bug was filing into an absent aspiration.
3. `candidates[0]` unchanged. Deliberately NOT a silent no-op — if nothing
   resolves, the add SHOULD fail loudly the way it does today rather than
   swallow the escalation. A canary that quietly drops its own alarm is worse
   than one that errors visibly.

FAIL-OPEN on every read: an unparseable config or unreadable queue degrades to
the next step, never raises. This runs on the iteration-close path.
"""
from __future__ import annotations

import json
from pathlib import Path

# The UPSTREAM deployment's recurring-infra aspiration first (the historical
# g-115-* queue), then this deployment's "Maintain Agent Health" agent queue.
# aspirations-spark and encode-session already document  as the local
# framework-hygiene home (), so this ordering matches existing practice
# rather than inventing a new convention.
DEFAULT_CANDIDATES = ("asp-115", "asp-001")


def _config_override(core_root) -> str:
    try:
        import yaml
        p = Path(core_root) / "config" / "aspirations.yaml"
        if not p.is_file():
            return ""
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        section = cfg.get("stale_cadence") or {}
        return str(section.get("escalation_aspiration") or "").strip()
    except Exception:
        return ""


def _existing_ids(*store_paths) -> set:
    """Aspiration ids present across the given JSONL stores. Never raises."""
    found = set()
    for sp in store_paths:
        if not sp:
            continue
        try:
            p = Path(sp)
            if not p.is_file():
                continue
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict) and rec.get("id"):
                    found.add(str(rec["id"]))
        except Exception:
            continue
    return found


def resolve(core_root, world_dir=None, agent_dir=None,
            candidates=DEFAULT_CANDIDATES) -> tuple:
    """(aspiration_id, source_label). See module docstring for the order."""
    stores = []
    if world_dir:
        stores.append(Path(world_dir) / "aspirations.jsonl")
    if agent_dir:
        stores.append(Path(agent_dir) / "aspirations.jsonl")
    present = _existing_ids(*stores)

    override = _config_override(core_root)
    if override:
        # The override WINS even when absent — silently substituting something
        # else would discard an explicit operator statement. But it is NOT
        # existence-checked, which means a typo'd or stale config value
        # reintroduces the exact aspiration_not_found bug this module exists to
        # fix, just through a different door. So the absence is reported in the
        # label rather than swallowed: callers surface `resolved_via`, so a
        # non-existent override announces itself instead of failing mutely at
        # add-goal time. Found by the  fresh-eyes pass on this file.
        if override in present:
            return override, "config:stale_cadence.escalation_aspiration"
        return override, ("config:stale_cadence.escalation_aspiration"
                          " (WARNING: not present in world or agent queue)")
    for cid in candidates:
        if cid in present:
            return cid, "resolved:exists-in-queue"

    # Nothing resolved — return the upstream default so the failure stays LOUD.
    return candidates[0], "fallback:none-exist"


def source_flag(asp_id, world_dir=None, agent_dir=None) -> str:
    """'world' or 'agent' — which --source aspirations-add-goal.sh needs.

    Filing into an agent-queue aspiration with --source world reproduces the
    original bug in a new costume: the id resolves, the store does not hold it,
    and the add fails aspiration_not_found again.
    """
    try:
        if world_dir and asp_id in _existing_ids(Path(world_dir) / "aspirations.jsonl"):
            return "world"
        if agent_dir and asp_id in _existing_ids(Path(agent_dir) / "aspirations.jsonl"):
            return "agent"
    except Exception:
        pass
    return "world"


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _paths import CORE_ROOT, WORLD_DIR, AGENT_DIR  # type: ignore
    aid, how = resolve(CORE_ROOT, WORLD_DIR, AGENT_DIR)
    print(json.dumps({
        "aspiration": aid,
        "resolved_via": how,
        "source_flag": source_flag(aid, WORLD_DIR, AGENT_DIR),
    }, indent=1))
