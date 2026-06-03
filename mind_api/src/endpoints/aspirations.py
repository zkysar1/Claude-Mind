"""GET /v1/aspirations/read — pre-migration cmd_read parity.

Bash wrapper translates flags to query parameters:
    --active            -> ?active=1
    --active-compact    -> ?active_compact=1
    --summary           -> ?summary=1
    --archive           -> ?archive=1
    --stepping-stones   -> ?stepping_stones=1
    --meta              -> ?meta=1
    --blocked           -> ?blocked=1
    --id ASP            -> ?id=ASP
    --limit N           -> &limit=N
    --source agent      -> &source=agent          (default: world)

Response Content-Type:
  text/plain  for --summary (multi-line, one aspiration per line)
  application/json  for every other flag (matches cmd_read's stdout shape)

Output equivalence with the existing CLI is the acceptance test — every byte
that `python3 core/scripts/aspirations.py read --<flag>` would print to
stdout must come back in the response body.

What this endpoint does NOT do (Phase A scope-cut, see mind_api/docs/development-history/DECISIONS.md):
  - Call `_log_goal_read` on --id reads (low-value observability side effect;
    re-enable in Phase B once we extract a daemon-safe stale-read writer).
  - Honour `WORLD_ONLY_COMMANDS` — `read` is never in that set, so safe.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from ..jsonl_cache import cache


# --- Output shaping --------------------------------------------------------

# Mirrors aspirations.py COMPACT_GOAL_KEEP at the time of writing. Duplicated
# rather than imported to keep the endpoint independent of aspirations.py's
# import side effects (utf-8 stdio reconfigure, _gate_log import chain).
# When COMPACT_GOAL_KEEP gains/loses fields upstream, mirror here. This is
# annoying but Phase B will refactor the upstream into a daemon-safe helper.
_COMPACT_GOAL_KEEP = {
    "id", "title", "status", "priority", "category", "skill",
    "recurring", "interval_hours", "lastAchievedAt", "achievedCount",
    "currentStreak", "longestStreak",
    "participants", "blocked_by", "blocked_since", "deferred_until", "defer_reason",
    "defer_reason_set_at", "blocker_ref",
    "args", "parent_goal", "discovered_by", "started",
    "depends_on", "abstained_by",
}


def _compact_aspiration(asp: Dict[str, Any], source: str) -> Dict[str, Any]:
    result = {k: v for k, v in asp.items() if k != "goals"}
    result.pop("description", None)
    result["source"] = source
    result["goals"] = [
        {k: v for k, v in g.items() if k in _COMPACT_GOAL_KEEP}
        for g in asp.get("goals", [])
    ]
    return result


def _find_by_id(items: List[Dict[str, Any]], asp_id: str):
    for i, asp in enumerate(items):
        if asp.get("id") == asp_id:
            return (i, asp)
    return None


# --- Path resolution -------------------------------------------------------

def _resolve_paths(ctx, source: str):
    """Return (live, archive, meta) paths for the given source.

    `source` is "world" (default) or "agent". When "agent", paths point to
    AGENT_DIR; otherwise WORLD_DIR. Matches aspirations.py main()'s
    `args.source == "agent"` branch.

    INVARIANT: the live path must equal `aspirations_write.py:_resolve_paths`'s
    live_path for the same (source, agent). jsonl_cache keys on the Path
    object — divergent construction here vs there silently desyncs the
    get (read) from the invalidate (write).
    """
    if source == "agent":
        base = ctx.paths.agent
    else:
        base = ctx.paths.world
    return (
        base / "aspirations.jsonl",
        base / "aspirations-archive.jsonl",
        base / "aspirations-meta.json",
    )


# --- Handlers --------------------------------------------------------------

def read(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response

    q = ctx.query
    source = (q.get("source") or "world").lower()
    if source not in ("world", "agent"):
        return Response.error(400, "invalid_source", f"source must be world or agent, got {source!r}")

    # source=agent requires an EXPLICIT X-Mind-Agent header. The resolver
    # falls back to "first available agent" when the header is missing,
    # which is a sensible default for daemon-internal calls — but for
    # source=agent we want an explicit choice from the caller.
    if source == "agent":
        explicit_agent = (ctx.headers.get("x-ayoai-agent") or "").strip()
        if not explicit_agent:
            return Response.error(400, "agent_unset",
                                  "X-Mind-Agent header required for source=agent")

    live_path, archive_path, meta_path = _resolve_paths(ctx, source)
    jc = cache()

    # The flags are mutually exclusive at the CLI; the daemon honours the
    # first matched one in cmd_read's argparse order to keep parity.
    if _flag(q, "active_compact"):
        items = jc.get(live_path)
        items = [a for a in items if a.get("status") == "active"]
        compact = [_compact_aspiration(a, source=source) for a in items]
        return Response.text(
            json.dumps(compact, indent=None, separators=(",", ":"), ensure_ascii=True),
            content_type="application/json",
        )

    if _flag(q, "active"):
        items = jc.get(live_path)
        return Response.text(
            json.dumps(items, indent=2, ensure_ascii=False),
            content_type="application/json",
        )

    asp_id = q.get("id")
    if asp_id:
        items = jc.get(live_path)
        result = _find_by_id(items, asp_id)
        if result is None:
            items = jc.get(archive_path)
            result = _find_by_id(items, asp_id)
        if result is None:
            return Response.error(404, "not_found", f"Aspiration {asp_id} not found")
        return Response.text(
            json.dumps(result[1], indent=2, ensure_ascii=False),
            content_type="application/json",
        )

    if _flag(q, "summary"):
        items = jc.get(live_path)
        lines = []
        for asp in items:
            progress = asp.get("progress", {}) or {}
            completed = progress.get("completed_goals", 0)
            total = progress.get("total_goals", 0)
            recurring = progress.get("recurring_goals", 0)
            status = (asp.get("status", "unknown") or "unknown").upper()
            rec_suffix = f" + {recurring} recurring" if recurring else ""
            lines.append(
                f"{asp['id']}: {asp['title']} [{status}] ({completed}/{total} goals{rec_suffix})"
            )
        return Response.text("\n".join(lines), content_type="text/plain")

    if _flag(q, "archive"):
        items = jc.get(archive_path)
        return Response.text(
            json.dumps(items, indent=2, ensure_ascii=False),
            content_type="application/json",
        )

    if _flag(q, "stepping_stones"):
        limit = int(q.get("limit", "5"))
        # list() wrap is load-bearing: jc.get returns the SHARED cache copy
        # (see jsonl_cache.JsonlCache.get docstring). Sorting the original
        # would corrupt the cache for every other reader. Do NOT remove.
        archived = list(jc.get(archive_path))
        archived.sort(key=lambda a: a.get("completed_at") or "", reverse=True)
        stones = []
        for asp in archived[:limit]:
            # B9-deep: an archived aspiration may have been goal-evicted while
            # live, so its `goals` list under-counts. Fold archived_census back
            # in for an accurate stepping-stone tally (read inline — see
            # core/scripts/_goal_census.py; display-only, no import coupling).
            _bs = ((asp.get("archived_census") or {}).get("by_status") or {})
            _evicted_total = sum(v for v in _bs.values() if isinstance(v, int))
            _evicted_completed = _bs.get("completed", 0) if isinstance(_bs.get("completed"), int) else 0
            stones.append({
                "id": asp["id"],
                "title": asp["title"],
                "motivation": asp.get("motivation", ""),
                "scope": asp.get("scope", "unknown"),
                "tags": asp.get("tags", []),
                "goals_completed": len([g for g in asp.get("goals", [])
                                        if g.get("status") == "completed"])
                                   + _evicted_completed,
                "goals_total": len(asp.get("goals", [])) + _evicted_total,
                "goal_summaries": [
                    {"title": g["title"], "status": g.get("status", "unknown"),
                     "category": g.get("category")}
                    for g in asp.get("goals", [])
                ],
            })
        return Response.text(
            json.dumps(stones, indent=2, ensure_ascii=False),
            content_type="application/json",
        )

    if _flag(q, "meta"):
        if not meta_path.exists():
            return Response.text("{}", content_type="application/json")
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return Response.error(500, "meta_read_failed", str(e))
        return Response.text(
            json.dumps(data, indent=2, ensure_ascii=False),
            content_type="application/json",
        )

    if _flag(q, "blocked"):
        items = jc.get(live_path)
        blocked_goals = []
        non_terminal = {"pending", "in-progress", "blocked"}
        for asp in items:
            for g in asp.get("goals", []):
                status = g.get("status", "pending")
                if status not in non_terminal:
                    continue
                if status == "blocked" or g.get("defer_reason"):
                    blocked_goals.append({
                        "aspiration_id": asp["id"],
                        "aspiration_title": asp.get("title", ""),
                        "goal": g,
                    })
        return Response.text(
            json.dumps(blocked_goals, indent=2, ensure_ascii=False),
            content_type="application/json",
        )

    return Response.error(
        400, "missing_flag",
        "Specify one of: active, active_compact, id, summary, archive, "
        "stepping_stones, meta, blocked",
    )


def _flag(q: Dict[str, str], name: str) -> bool:
    """A query-string flag is "set" iff present with a truthy value.

    Match what the bash wrapper produces: it passes `?summary=1` (or =true).
    Empty or 0 is treated as unset to be forgiving of caller idiom.
    """
    v = q.get(name)
    if v is None:
        return False
    return v.lower() not in ("", "0", "false", "no")


def register(routes) -> None:
    routes[("GET", "/v1/aspirations/read")] = read
