"""_frontier.py — claimable-frontier census over the aspiration queues.

The CLAIMABLE FRONTIER is the set of pending goals a Body could claim right
now: status `pending`, not recurring, not deferred, participants not user-only,
and every `blocked_by` dependency SATISFIED — where "satisfied" is
`_dependency_graph.resolve_dependency`, the one resolver the selector also
uses (a superseded-then-completed dependency satisfies; a skipped one does
not; an id absent from every queue is `unknown`, which is ignorance, not
evidence, and is reported separately rather than counted either way).

Every pending goal that fails ONLY the dependency test is GATED, and the live
goals it waits on — walked transitively to the ones that themselves wait on
nothing — are its ROOTS. A root is the goal whose completion would widen the
frontier; the number of goals it gates is its fan-out.

Why a fleet needs this number. A fleet of N Bodies is at most as parallel as
its frontier is wide, and nothing in the loop measured the width. Measured
2026-08-29 on a live 8-Body deployment: 15 pending goals, frontier 0, every
one gated (directly or through a chain) on ONE in-progress goal — five of the
eight Bodies closed for lack of work while one Body wrote the gate's module.
Each worker's genuine-close message said "all goals dependency-blocked" and
the fleet read the quiet as health. The dependencies were over-specified: the
consumers needed the module's INTERFACE (names on disk), not its completion.

Two consumers, one implementation — a second predicate would be free to drift:
  - `agent-watchdog.py` DependencyFunnelProbe (reducer tick) — files, and
    later retires, the Investigate goal prescribing the interface-contract
    relaxation.
  - `frontier-check.py` — the read-only CLI a reader re-measures with.

Cross-agent globs here (`*/aspirations.jsonl`, `*/sessions/*/body-manifest.yaml`)
take the agents root as an ARGUMENT so tests are hermetic by construction; the
callers pass `_paths.agents_root()` (agent-dir-resolution.md consumer table).
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from _dependency_graph import norm_blocked_by, resolve_dependency  # noqa: E402
from _fileops import _parse_jsonl_skip_corrupt  # noqa: E402

FUNNEL_SIGNAL_PREFIX = "investigate:dependency-funnel-"

# body-manifest.yaml `body_state` values that mean "this Body is done".
# Mirrors stop-hook.sh's closed set (closed-pending-merge|merged|closed-stale).
CLOSED_BODY_STATES = frozenset({"closed-pending-merge", "merged", "closed-stale"})
ACTIVE_BODY_STATES = frozenset({"active", "parked"})

_BODY_STATE_RE = re.compile(r"^body_state:\s*['\"]?([\w-]+)['\"]?\s*$", re.MULTILINE)
_MAX_WALK = 64  # a dependency chain deeper than this is a data defect, not a graph


def _user_only(goal: dict) -> bool:
    parts = goal.get("participants")
    if isinstance(parts, str):
        parts = [parts]
    return isinstance(parts, list) and bool(parts) and all(p == "user" for p in parts)


def load_goal_index(world_dir, agents_root_path) -> tuple[dict, list, dict]:
    """Every goal in every queue, keyed by id.

    Returns (goal_index, aspirations, stats). Each goal carries `_asp_id`,
    `_asp_status` and `_source` ("world" or the agent dir name — the value
    `aspirations-update-goal.sh --source` takes). `blocked_by` resolves
    GLOBALLY across queues (coordination.md "Dependency Chains"), so the index
    spans all of them. `stats["parse_skipped"]` counts unparseable lines: a
    clean census beside a nonzero skip count is a census over a population it
    never fully saw (guard-3714), and the CLI prints it for that reason.
    """
    goal_index: dict = {}
    aspirations: list = []
    stats = {"parse_skipped": 0, "stores": []}
    # (source, path, archived). The ARCHIVE is folded in on purpose
    # (guard-1890): a dependency on a completed-then-archived goal must resolve
    # as satisfied, not as an unknown id — reporting it as broken is the false
    # positive that froze  for 37 days. Live stores are read AFTER the
    # archives so a live record wins on an id collision.
    stores = []
    if world_dir:
        w = Path(world_dir)
        stores.append(("world", w / "aspirations-archive.jsonl", True))
        stores.append(("world", w / "aspirations.jsonl", False))
    if agents_root_path:
        root = Path(agents_root_path)
        if root.is_dir():
            for d in sorted(root.iterdir()):
                if not d.is_dir():
                    continue
                stores.append((d.name, d / "aspirations-archive.jsonl", True))
                stores.append((d.name, d / "aspirations.jsonl", False))
    for source, path, archived in stores:
        if not path.exists():
            continue
        items, errors, _total = _parse_jsonl_skip_corrupt(path)
        stats["parse_skipped"] += int(errors or 0)
        stats["stores"].append(str(path))
        for asp in items:
            if not isinstance(asp, dict):
                continue
            if not archived:
                aspirations.append(dict(asp, _source=source))
            for g in asp.get("goals") or []:
                if not isinstance(g, dict) or not g.get("id"):
                    continue
                goal_index[g["id"]] = dict(
                    g, _asp_id=asp.get("id"), _asp_status=asp.get("status"),
                    _source=source, _archived=archived)
    return goal_index, aspirations, stats


def live_blockers(goal: dict, goal_index: dict) -> tuple[list, list]:
    """(open_blockers, unknown_blockers) for one goal, via the SSOT resolver."""
    open_ids, unknown = [], []
    for b in norm_blocked_by(goal.get("blocked_by")):
        verdict, _resolved, _chain = resolve_dependency(b, goal_index)
        if verdict == "satisfied":
            continue
        if verdict == "unknown":
            unknown.append(b)
            continue
        open_ids.append(b)  # "open" or "cycle" — both keep the consumer waiting
    return open_ids, unknown


def roots_of(goal_id: str, goal_index: dict) -> set:
    """The live goals `goal_id` ultimately waits on: walk open blockers until
    reaching goals that have no open blockers of their own. Cycle-safe and
    depth-bounded; a blocker absent from the index is not a root (it is
    unknown) and is skipped here — `live_blockers` reports it separately."""
    roots: set = set()
    seen: set = set()
    stack = [goal_id]
    steps = 0
    while stack and steps < _MAX_WALK:
        steps += 1
        gid = stack.pop()
        if gid in seen:
            continue
        seen.add(gid)
        goal = goal_index.get(gid)
        if goal is None:
            continue
        open_ids, _unknown = live_blockers(goal, goal_index)
        if not open_ids:
            if gid != goal_id:
                roots.add(gid)
            continue
        stack.extend(open_ids)
    return roots


def count_bodies(agents_root_path, now: float | None = None,
                 lookback_hours: float = 6.0) -> dict:
    """Body census from `agents/*/sessions/*/body-manifest.yaml`.

    `active` = manifests in an active/parked state; `closed_recent` = manifests
    in a closed state whose file changed within `lookback_hours` — Bodies that
    stopped recently, which on a frontier of 0 means "stopped for lack of
    work". Read with a line regex rather than a YAML parser: the file is
    written by the framework in a fixed shape and a parser dependency would
    make a census fail where a grep would not.
    """
    out = {"active": 0, "closed_recent": 0, "scanned": 0}
    if not agents_root_path:
        return out
    root = Path(agents_root_path)
    if not root.is_dir():
        return out
    now = time.time() if now is None else now
    cutoff = now - lookback_hours * 3600.0
    for manifest in root.glob("*/sessions/*/body-manifest.yaml"):
        try:
            text = manifest.read_text(encoding="utf-8", errors="replace")
            mtime = manifest.stat().st_mtime
        except OSError:
            continue
        m = _BODY_STATE_RE.search(text)
        if not m:
            continue
        out["scanned"] += 1
        state = m.group(1)
        if state in ACTIVE_BODY_STATES:
            out["active"] += 1
        elif state in CLOSED_BODY_STATES and mtime >= cutoff:
            out["closed_recent"] += 1
    return out


def frontier_census(world_dir, agents_root_path, *, now: float | None = None,
                    lookback_hours: float = 6.0, max_ids: int = 40) -> dict:
    """The whole census as one dict. Pure read; never raises on store shape.

    Keys a consumer acts on:
      claimable          ids a Body could claim now (capped at `max_ids`; the
                         count is `claimable_count`)
      gated              ids pending on an open dependency (capped; count in
                         `gated_count`)
      roots              [{id, title, status, asp_id, source, claimed_by,
                         gates}] sorted by fan-out, descending
      unknown_blockers   [(goal_id, blocker_id)] — dependencies on ids absent
                         from every queue (guard-1890: ignorance, not evidence)
      in_progress, deferred, blocked, user_only   context counts
      bodies             count_bodies()
      parse_skipped      unparseable lines across the stores read
    """
    goal_index, aspirations, stats = load_goal_index(world_dir, agents_root_path)
    active_asp_ids = {a.get("id") for a in aspirations if a.get("status") == "active"}

    claimable, gated, unknown_pairs = [], [], []
    counts = {"in_progress": 0, "deferred": 0, "blocked": 0, "user_only": 0,
              "recurring": 0}
    root_gates: dict = {}
    for gid, goal in goal_index.items():
        if goal.get("_asp_id") not in active_asp_ids:
            continue
        status = goal.get("status")
        if status == "in-progress":
            counts["in_progress"] += 1
            continue
        if status == "blocked":
            counts["blocked"] += 1
            continue
        if status != "pending":
            continue
        if goal.get("recurring"):
            counts["recurring"] += 1
            continue
        if goal.get("defer_reason"):
            counts["deferred"] += 1
            continue
        if _user_only(goal):
            counts["user_only"] += 1
            continue
        open_ids, unknown = live_blockers(goal, goal_index)
        for u in unknown:
            unknown_pairs.append((gid, u))
        if open_ids or unknown:
            # An unknown blocker gates too: the selector resolves blocked_by by
            # membership in the done set, and an id no queue holds is never in
            # it. It just has no ROOT — a relaxation cannot open it, and the
            # probe reports the unknown pairs instead of filing.
            gated.append(gid)
            for r in roots_of(gid, goal_index):
                root_gates[r] = root_gates.get(r, 0) + 1
        else:
            claimable.append(gid)

    roots = []
    for rid, n in sorted(root_gates.items(), key=lambda kv: (-kv[1], kv[0])):
        rg = goal_index.get(rid) or {}
        roots.append({
            "id": rid,
            "title": (rg.get("title") or "")[:120],
            "status": rg.get("status"),
            "asp_id": rg.get("_asp_id"),
            "source": rg.get("_source", "world"),
            "claimed_by": rg.get("claimed_by"),
            "gates": n,
        })

    return {
        "claimable_count": len(claimable),
        "claimable": sorted(claimable)[:max_ids],
        "gated_count": len(gated),
        "gated": sorted(gated)[:max_ids],
        "roots": roots,
        "unknown_blockers": unknown_pairs[:max_ids],
        "pending_total": len(claimable) + len(gated),
        **counts,
        "active_aspirations": len(active_asp_ids),
        "bodies": count_bodies(agents_root_path, now=now, lookback_hours=lookback_hours),
        "parse_skipped": stats["parse_skipped"],
        "stores_scanned": stats["stores"],
    }


def open_funnel_goals(goal_index: dict) -> list:
    """Every open, unclaimed goal the funnel probe filed (by its signal prefix),
    each with the root id its signal names — the retire path's population."""
    out = []
    for gid, goal in goal_index.items():
        sig = goal.get("origin_signal") or ""
        if not isinstance(sig, str) or not sig.startswith(FUNNEL_SIGNAL_PREFIX):
            continue
        if goal.get("status") != "pending" or goal.get("claimed_by"):
            continue
        out.append({"id": gid, "root": sig[len(FUNNEL_SIGNAL_PREFIX):],
                    "source": goal.get("_source", "world")})
    return out
