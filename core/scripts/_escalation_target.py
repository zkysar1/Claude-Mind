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
2. The catch-all named by THIS world's `conventions/deployment-routing.md` (the
   domain overlay `/encode-session` already routes through — g-001-195), when
   that id is live in a queue. The convention is deployment DATA, so it is the
   right home for the mapping; a stale entry (retired/absent id) falls through
   rather than reproducing the bug through a new door.
3. The first id in `candidates` that is LIVE in the world or agent queue.
   Existence is the whole point: the bug was filing into an absent aspiration —
   and a retired/archived record is absent for this purpose (the upstream world
   carries a RETIRED asp-001 that must never be targeted).
4. A title heuristic over the live queues (world first, then agent): the
   aspiration whose title reads as the framework's operating-rhythm /
   maintenance / hygiene home. Measured 2026-08-30 on a third deployment
   (coach@zc-03, world queue asp-002 "Operating Rhythm", asp-003 "Memory
   Hygiene", asp-006/007 domain work): neither candidate existed, no convention
   file, so every watchdog escalation — 28 GitDriftProbe ticks in 5 h — failed
   `aspiration_not_found` and no goal ever landed. Loud-and-useless every tick
   is not the failure mode step 5 was written to preserve.
5. `candidates[0]` unchanged. Deliberately NOT a silent no-op — if nothing
   resolves, the add SHOULD fail loudly the way it does today rather than
   swallow the escalation. A canary that quietly drops its own alarm is worse
   than one that errors visibly.

FAIL-OPEN on every read: an unparseable config or unreadable queue degrades to
the next step, never raises. This runs on the iteration-close path.
"""
from __future__ import annotations

import json
import re
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


#: A record in one of these states is not a filing target: the goal would land
#: in a queue nothing selects from (the upstream world's  is `retired` +
#: `archived: true`, and deployment-routing.md says never to target it).
_DEAD_STATUSES = frozenset({"retired", "archived", "completed"})

#: Titles that read as the framework's operating / maintenance home. Ordered by
#: specificity; the first pattern with a live match wins, then ascending id.
_HOME_TITLE_PATTERNS = (
    re.compile(r"(?i)operating rhythm"),
    re.compile(r"(?i)agent health|framework[- ]hygiene|framework[- ]maintenance"),
    re.compile(r"(?i)recurring infrastructure|infrastructure hygiene|housekeeping"),
    re.compile(r"(?i)\bmaintenance\b|\bhygiene\b"),
)


def _live_records(*store_paths) -> dict:
    """{aspiration id: title} for LIVE records across the given JSONL stores.

    Retired / archived / completed records are skipped — for a filing target
    they are as absent as a missing id. Never raises.
    """
    found = {}
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
                if not (isinstance(rec, dict) and rec.get("id")):
                    continue
                if str(rec.get("status") or "").lower() in _DEAD_STATUSES:
                    continue
                if rec.get("archived") is True:
                    continue
                found.setdefault(str(rec["id"]), str(rec.get("title") or ""))
        except Exception:
            continue
    return found


def _existing_ids(*store_paths) -> set:
    """Live aspiration ids present across the given JSONL stores. Never raises."""
    return set(_live_records(*store_paths))


_ROUTING_ROW_RE = re.compile(r"(?i)catch-all")
_ASP_ID_RE = re.compile(r"\basp-\d{3,4}\b")


def _routing_convention(world_dir) -> str:
    """The catch-all id named by `<world>/conventions/deployment-routing.md`, or ''.

    Reads the table row that mentions "catch-all" and carries an aspiration id —
    the shape the convention documents for every deployment ("| Framework-hygiene
    / maintenance catch-all | **asp-115** | `world` |"). The FIRST such row is
    this world's own mapping; the sibling-deployments table below it is only
    consulted if no own row exists, which the convention's own text forbids
    relying on, so a file with only sibling rows resolves to nothing here.
    Never raises.
    """
    try:
        if not world_dir:
            return ""
        p = Path(world_dir) / "conventions" / "deployment-routing.md"
        if not p.is_file():
            return ""
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.lstrip().startswith("|"):
                continue
            if "deployment" in line.lower() and "catch-all" in line.lower() and "|---" not in line:
                # The sibling table's HEADER row ("| Deployment | Catch-all | ...")
                # is not a mapping; everything after it is another world's data.
                break
            if _ROUTING_ROW_RE.search(line):
                m = _ASP_ID_RE.search(line)
                if m:
                    return m.group(0)
    except Exception:
        return ""
    return ""


def _title_heuristic(world_records: dict, agent_records: dict) -> tuple:
    """(id, title) of the live aspiration that reads as the operating/maintenance
    home — world queue first, then agent — or ('', '')."""
    for records in (world_records, agent_records):
        for rx in _HOME_TITLE_PATTERNS:
            hits = sorted((aid, title) for aid, title in records.items()
                          if title and rx.search(title))
            if hits:
                return hits[0]
    return "", ""


def resolve(core_root, world_dir=None, agent_dir=None,
            candidates=DEFAULT_CANDIDATES) -> tuple:
    """(aspiration_id, source_label). See module docstring for the order."""
    world_records = _live_records(Path(world_dir) / "aspirations.jsonl") if world_dir else {}
    agent_records = _live_records(Path(agent_dir) / "aspirations.jsonl") if agent_dir else {}
    present = set(world_records) | set(agent_records)

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
    routed = _routing_convention(world_dir)
    if routed and routed in present:
        return routed, "resolved:deployment-routing.md"
    for cid in candidates:
        if cid in present:
            return cid, "resolved:exists-in-queue"
    home_id, home_title = _title_heuristic(world_records, agent_records)
    if home_id:
        return home_id, f"resolved:title-heuristic ({home_title[:40]})"

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
