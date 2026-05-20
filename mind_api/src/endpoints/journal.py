"""GET /v1/journal/read — parity with `journal.py read`.

Query params (mutually exclusive — exactly one):
    session=<int>        find by session number
    date=<YYYY-MM-DD>    find records by date
    summary=1
    meta=1               computed metadata
    latest=1             most recent session record
    recent=<N>           (default 5 when value empty)

Lives under AGENT_DIR (<agent>/journal.jsonl).
"""
from __future__ import annotations

from ..jsonl_cache import cache
from ._jsonl_common import flag, json_response_pretty, missing_flag_error, plain_lines


def _path(ctx):
    return ctx.paths.agent / "journal.jsonl"


def _parse_n(s: str, default: int = 5) -> int:
    if s == "" or s is None:
        return default
    try:
        n = int(s)
    except ValueError:
        return default
    return n if n > 0 else default


def read(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response

    q = ctx.query
    jc = cache()
    path = _path(ctx)

    session = q.get("session")
    if session is not None and session != "":
        try:
            sess_num = int(session)
        except ValueError:
            return Response.error(400, "invalid_param", "session must be integer")
        items = jc.get(path)
        match = next((r for r in items if r.get("session") == sess_num), None)
        if match is None:
            return Response.error(404, "not_found", f"Session {sess_num} not found")
        return json_response_pretty(match)

    date = q.get("date")
    if date:
        items = jc.get(path)
        return json_response_pretty([r for r in items if r.get("date") == date])

    if flag(q, "summary"):
        items = jc.get(path)
        lines = []
        for rec in items:
            sess = rec.get("session", "?")
            d = rec.get("date", "?")
            goals = len(rec.get("goals_completed", []) or [])
            events = len(rec.get("key_events", []) or [])
            tags = rec.get("tags", []) or []
            tags_str = ", ".join(tags) if tags else ""
            lines.append(f"Session {sess} ({d}): {goals} goals, {events} events [{tags_str}]")
        return plain_lines(lines)

    if flag(q, "meta"):
        items = jc.get(path)
        if not items:
            meta = {"total_sessions": 0, "last_updated": None, "date_range": []}
        else:
            dates = sorted([rec.get("date", "") for rec in items if rec.get("date")])
            meta = {
                "total_sessions": len(items),
                "last_updated": dates[-1] if dates else None,
                "date_range": [dates[0], dates[-1]] if dates else [],
            }
        return json_response_pretty(meta)

    if flag(q, "latest"):
        items = jc.get(path)
        if not items:
            return Response.error(404, "not_found", "No journal records found")
        latest = max(items, key=lambda r: r.get("session", 0))
        return json_response_pretty(latest)

    recent = q.get("recent")
    if recent is not None:
        n = _parse_n(recent)
        items = list(jc.get(path))
        items.sort(key=lambda r: r.get("session", 0), reverse=True)
        return json_response_pretty(items[:n])

    return missing_flag_error(["session", "date", "summary", "meta", "latest", "recent"])


def register(routes) -> None:
    routes[("GET", "/v1/journal/read")] = read
