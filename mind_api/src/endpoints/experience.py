"""GET /v1/experience/read — parity with `experience.py read`.

Query params (mutually exclusive — exactly one):
    id=<exp-id>             searches live then archive
    category=<cat>          live only
    goal=<goal-id>
    hypothesis=<hyp-id>
    summary=1
    type=<type>
    most_retrieved=<N>      (default 10 when value empty)
    least_retrieved=<N>     (default 10 when value empty)
    recent=<N>              (default 10 when value empty)
    archive=1
    meta=1
    validate=1              cross-file integrity check (JSONL vs .md files)

Live + archive + meta paths are AGENT-local (<agent>/experience*.jsonl).
"""
from __future__ import annotations

import json

from ..jsonl_cache import cache
from ._jsonl_common import (
    find_by_id, flag, json_response_pretty, missing_flag_error, plain_lines,
)


def _live(ctx):
    return ctx.paths.agent / "experience.jsonl"


def _archive(ctx):
    return ctx.paths.agent / "experience-archive.jsonl"


def _meta(ctx):
    return ctx.paths.agent / "experience-meta.json"


def _parse_n(s: str, default: int = 10) -> int:
    if s == "" or s is None:
        return default
    try:
        n = int(s)
    except ValueError:
        return default
    return n if n > 0 else default


def _validate(ctx):
    """Cross-file integrity check: JSONL records vs .md files.

    Mirrors experience.py cmd_validate (lines 796-837). Returns a JSON result
    with valid/invalid status, orphaned files, and missing files.
    """
    from ..server import Response
    from pathlib import Path

    jc = cache()
    items = list(jc.get(_live(ctx))) + list(jc.get(_archive(ctx)))

    # Resolve PROJECT_ROOT from ctx — two levels up from the agent dir.
    project_root = ctx.paths.agent.parent if ctx.paths.agent else None

    # Collect all content_paths from JSONL
    jsonl_paths = {}
    for rec in items:
        cp = rec.get("content_path", "")
        if cp:
            p = Path(cp)
            abs_cp = p if p.is_absolute() else (project_root / cp if project_root else p)
            jsonl_paths[str(abs_cp)] = rec.get("id", "?")

    # Collect all .md files in experience dir
    experience_dir = ctx.paths.agent / "experience" if ctx.paths.agent else None
    md_files = {}
    if experience_dir and experience_dir.exists():
        for f in experience_dir.iterdir():
            if f.suffix == ".md":
                md_files[str(f)] = f.name

    # JSONL records without .md files
    missing_md = []
    for abs_path, rec_id in jsonl_paths.items():
        if not Path(abs_path).exists():
            missing_md.append({"id": rec_id, "expected_path": abs_path})

    # .md files without JSONL records
    orphan_md = []
    jsonl_abs_set = set(jsonl_paths.keys())
    for abs_path, name in md_files.items():
        if abs_path not in jsonl_abs_set:
            orphan_md.append({"file": name, "path": abs_path})

    result = {
        "valid": len(missing_md) == 0 and len(orphan_md) == 0,
        "jsonl_without_md": missing_md,
        "md_without_jsonl": orphan_md,
        "total_jsonl": len(items),
        "total_md": len(md_files),
    }
    return json_response_pretty(result)


def read(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response

    q = ctx.query
    jc = cache()

    # T1.8: validate flag — cross-file integrity check (experience.py lines 796-837).
    if flag(q, "validate"):
        return _validate(ctx)

    rec_id = q.get("id")
    if rec_id:
        items = jc.get(_live(ctx))
        result = find_by_id(items, rec_id)
        if result is None:
            items = jc.get(_archive(ctx))
            result = find_by_id(items, rec_id)
        if result is None:
            return Response.error(404, "not_found", f"Record {rec_id} not found")
        return json_response_pretty(result[1])

    category = q.get("category")
    if category:
        items = jc.get(_live(ctx))
        return json_response_pretty([r for r in items if r.get("category") == category])

    goal = q.get("goal")
    if goal:
        items = jc.get(_live(ctx))
        return json_response_pretty([r for r in items if r.get("goal_id") == goal])

    hypothesis = q.get("hypothesis")
    if hypothesis:
        items = jc.get(_live(ctx))
        return json_response_pretty(
            [r for r in items if r.get("hypothesis_id") == hypothesis]
        )

    if flag(q, "summary"):
        items = jc.get(_live(ctx))
        lines = []
        for rec in items:
            typ = rec.get("type", "?")
            cat = rec.get("category", "?")
            summary = rec.get("summary", "(no summary)")
            lines.append(f"{rec.get('id', '?')}: [{typ}] {cat} — {summary}")
        return plain_lines(lines)

    typ = q.get("type")
    if typ:
        items = jc.get(_live(ctx))
        return json_response_pretty([r for r in items if r.get("type") == typ])

    most = q.get("most_retrieved")
    if most is not None:
        n = _parse_n(most)
        items = list(jc.get(_live(ctx)))
        items.sort(key=lambda r: (r.get("retrieval_stats") or {}).get("retrieval_count", 0),
                   reverse=True)
        return json_response_pretty(items[:n])

    least = q.get("least_retrieved")
    if least is not None:
        n = _parse_n(least)
        items = list(jc.get(_live(ctx)))
        items.sort(key=lambda r: (r.get("retrieval_stats") or {}).get("retrieval_count", 0))
        return json_response_pretty(items[:n])

    recent = q.get("recent")
    if recent is not None:
        n = _parse_n(recent)
        items = list(jc.get(_live(ctx)))
        items.sort(key=lambda r: r.get("created", ""), reverse=True)
        return json_response_pretty(items[:n])

    if flag(q, "archive"):
        return json_response_pretty(jc.get(_archive(ctx)))

    if flag(q, "meta"):
        meta_p = _meta(ctx)
        from storage_backend import get_backend
        get_backend().ensure_local(meta_p)  # own-cloud read-path fix: materialize S3-only file before local read
        if not meta_p.exists():
            return Response.text("{}", content_type="application/json")
        try:
            data = json.loads(meta_p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return Response.error(500, "meta_read_failed", str(e))
        return json_response_pretty(data)

    return missing_flag_error([
        "id", "category", "goal", "hypothesis", "summary", "type",
        "most_retrieved", "least_retrieved", "recent", "archive", "meta",
        "validate",
    ])


def register(routes) -> None:
    routes[("GET", "/v1/experience/read")] = read
