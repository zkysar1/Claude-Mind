"""GET /v1/spark-questions/read — parity with `spark-questions.py read`.

Query params (mutually exclusive — exactly one):
    active=1
    candidates=1
    all=1
    id=<sq-id>
    summary=1

Lives under META_DIR (not WORLD_DIR — spark-questions is meta-store, not
domain knowledge).

Equivalence target: stdout of `python3 core/scripts/spark-questions.py read --<flag>`.
"""
from __future__ import annotations

from ..jsonl_cache import cache
from ..endpoints._jsonl_common import (
    find_by_id, flag, json_response_pretty, missing_flag_error, plain_lines,
)


def _path(ctx):
    return ctx.paths.meta / "spark-questions.jsonl"


def read(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response

    q = ctx.query
    jc = cache()
    path = _path(ctx)

    if flag(q, "active"):
        items = jc.get(path)
        return json_response_pretty([
            r for r in items
            if r.get("type") == "question" and r.get("status") == "active"
        ])

    if flag(q, "candidates"):
        items = jc.get(path)
        return json_response_pretty([r for r in items if r.get("type") == "candidate"])

    if flag(q, "all"):
        return json_response_pretty(jc.get(path))

    rec_id = q.get("id")
    if rec_id:
        items = jc.get(path)
        result = find_by_id(items, rec_id)
        if result is None:
            return Response.error(404, "not_found", f"Record {rec_id} not found")
        return json_response_pretty(result[1])

    if flag(q, "summary"):
        items = jc.get(path)
        lines = []
        for rec in items:
            text = rec.get("text", "")
            truncated = text[:60] + "..." if len(text) > 60 else text
            rec_id = rec.get("id", "?")
            if rec.get("type") == "question":
                yr = rec.get("yield_rate", 0.0) or 0.0
                ta = rec.get("times_asked", 0) or 0
                lines.append(f"{rec_id}: {truncated} [yield={yr:.2f}, asked={ta}]")
            else:
                lines.append(f"{rec_id}: {truncated} [CANDIDATE]")
        return plain_lines(lines)

    return missing_flag_error(["active", "candidates", "all", "id", "summary"])


def register(routes) -> None:
    routes[("GET", "/v1/spark-questions/read")] = read
