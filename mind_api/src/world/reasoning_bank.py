"""GET /v1/rb/read and GET /v1/guard/read.

Pre-migration parity with `reasoning-bank.py rb read` and
`reasoning-bank.py guard read`.

RB query params (mutually exclusive — exactly one):
    active=1
    id=<rb-id>
    category=<cat>
    universal=1
    tag=<tag>            (case-insensitive exact match)
    summary=1
    recent=<N>           (default 10 when present without value via recent=)

Guard query params:
    active=1
    id=<guard-id>
    category=<cat>
    summary=1

Equivalence target: stdout of the corresponding CLI invocation.
"""
from __future__ import annotations

import sys
from pathlib import Path

from ..jsonl_cache import cache
from ..endpoints._jsonl_common import (
    find_by_id, flag, json_response_pretty, missing_flag_error, plain_lines,
)


# Reach into core/scripts for is_universal_rb + sort_universal_rbs. These
# helpers are pure functions of the items list — no _paths globals — so
# safe to import in the daemon process. _paths gets sys.path-injected
# via _rb_helpers' own imports; that's fine, daemon doesn't depend on
# the bake-in.
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "core" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _rb_helpers import is_universal_rb, sort_universal_rbs  # noqa: E402


# ---------------------------------------------------------------------------
# RB read
# ---------------------------------------------------------------------------

def _rb_path(ctx):
    return ctx.paths.world / "reasoning-bank.jsonl"


def rb_read(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response

    q = ctx.query
    jc = cache()
    path = _rb_path(ctx)

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

    category = q.get("category")
    if category:
        items = jc.get(path)
        return json_response_pretty([r for r in items if r.get("category") == category])

    if flag(q, "universal"):
        items = jc.get(path)
        filtered = [r for r in items
                    if r.get("status") == "active" and is_universal_rb(r)]
        # sort_universal_rbs mutates the list in place — make a private copy
        # because items came from the shared cache.
        filtered = list(filtered)
        sort_universal_rbs(filtered)
        return json_response_pretty(filtered)

    tag = q.get("tag")
    if tag:
        items = jc.get(path)
        tag_lower = tag.lower()
        out = []
        for r in items:
            if r.get("status") != "active":
                continue
            tags = r.get("tags") or []
            if not isinstance(tags, list):
                continue
            if any(tag_lower == str(t).lower() for t in tags):
                out.append(r)
        return json_response_pretty(out)

    if flag(q, "summary"):
        items = jc.get(path)
        lines = []
        for rec in items:
            typ = rec.get("type", "?")
            cat = rec.get("category", "?")
            title = rec.get("title", "(untitled)")
            lines.append(f"{rec.get('id', '?')}: [{typ}] {cat} — {title}")
        return plain_lines(lines)

    recent = q.get("recent")
    if recent is not None:
        # recent can be empty string (just `?recent=`) — CLI default 10
        try:
            n = int(recent) if recent != "" else 10
        except ValueError:
            return Response.error(400, "invalid_param", "recent must be integer")
        if n <= 0:
            n = 10
        items = jc.get(path)
        active = [r for r in items if r.get("status") == "active"]
        # Make a copy before sort — items is the shared cache list.
        active = sorted(active, key=lambda r: r.get("created", ""), reverse=True)
        return json_response_pretty(active[:n])

    return missing_flag_error([
        "active", "id", "category", "universal", "tag", "summary", "recent",
    ])


# ---------------------------------------------------------------------------
# Guard read
# ---------------------------------------------------------------------------

def _guard_path(ctx):
    return ctx.paths.world / "guardrails.jsonl"


def guard_read(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response

    q = ctx.query
    jc = cache()
    path = _guard_path(ctx)

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

    category = q.get("category")
    if category:
        items = jc.get(path)
        return json_response_pretty([r for r in items if r.get("category") == category])

    if flag(q, "summary"):
        items = jc.get(path)
        lines = []
        for rec in items:
            cat = rec.get("category", "?")
            rule = (rec.get("rule") or "(no rule)")[:80]
            lines.append(f"{rec.get('id', '?')}: [{cat}] {rule}")
        return plain_lines(lines)

    return missing_flag_error(["active", "id", "category", "summary"])


def register(routes) -> None:
    routes[("GET", "/v1/rb/read")] = rb_read
    routes[("GET", "/v1/guard/read")] = guard_read
