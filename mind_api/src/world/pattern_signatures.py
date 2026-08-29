"""GET /v1/pattern-signatures/read — parity with `pattern-signatures.py read`.

Query params (mutually exclusive — exactly one):
    all=1
    active=1
    id=<sig-id>
    summary=1
    count=1

Equivalence target: stdout of `python3 core/scripts/pattern-signatures.py read --<flag>`.
"""
from __future__ import annotations

from ..jsonl_cache import cache
from ..endpoints._jsonl_common import (
    find_by_id, flag, json_response_pretty, missing_flag_error, plain_lines,
)


def _path(ctx):
    return ctx.paths.world / "pattern-signatures.jsonl"


def read(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response

    q = ctx.query
    jc = cache()
    path = _path(ctx)

    if flag(q, "all"):
        return json_response_pretty(jc.get(path))

    if flag(q, "active"):
        items = jc.get(path)
        return json_response_pretty([r for r in items if r.get("status") == "active"])

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
            sig_id = rec.get("id", "?")
            name = rec.get("name", "(unnamed)")
            vs = rec.get("validation_status", "?")
            stats = rec.get("outcome_stats", {}) or {}
            accuracy = stats.get("accuracy", 0.0)
            confirmed = stats.get("confirmed", 0)
            total = stats.get("total", 0)
            lines.append(f"{sig_id}: {name} [{vs}] accuracy={accuracy} ({confirmed}/{total})")
        return plain_lines(lines)

    # COUNT — record count only (). Counting `summary=1` LINES is
    # not a record count; the two error directions are documented ONCE, in
    # mind_api/src/world/reasoning_bank.py. Do not restate them here — two
    # copies of a measured claim is the drift surface (guard-5032).
    if flag(q, "count"):
        return json_response_pretty({"count": len(jc.get(path))})

    return missing_flag_error(["all", "active", "id", "summary", "count"])


def register(routes) -> None:
    routes[("GET", "/v1/pattern-signatures/read")] = read
