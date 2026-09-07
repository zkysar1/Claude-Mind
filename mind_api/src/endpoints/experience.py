"""GET /v1/experience/read — parity with `experience.py read`.

Query params. FILTERS COMPOSE with SELECTORS (g-115-5730); they are not
mutually exclusive, whatever this docstring said before — it said "exactly
one" while a live caller was already combining three, and the code silently
honoured only the first:
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

Composition contract:
    FILTERS   category / goal / hypothesis / type   — narrow, and AND together
    SELECTORS most_retrieved | least_retrieved | recent — order + limit; these
              stay exclusive among themselves (precedence in that order)
    FORMAT    summary=1 — renders the filtered+limited set as plain text lines,
              NOT JSON. That is by design and predates this change: `summary=1`
              alone returns the same plain lines. A caller needing JSON, or any
              field the line does not carry (e.g. tree_nodes_related), must not
              pass it.
    TERMINAL  id / archive / meta / validate — distinct modes, not composable.

Live + archive + meta paths are AGENT-local (<agent>/experience*.jsonl).
"""
from __future__ import annotations

import json

from ..jsonl_cache import cache
from ._jsonl_common import (
    find_by_id, flag, json_response_pretty, missing_flag_error,
    parse_int_param, plain_lines,
)
# Reuse the WRITER's derivation helper rather than adding a third copy of the
# regex (). experience_write.py's own comment demands its copy stay
# literally identical to core/scripts/experience.py's; a third copy here would
# make that promise harder to keep, and this module needs the exact same
# id->goal-id mapping the writer uses. Import direction is safe: experience_write
# does not import this module, so there is no cycle.
from .experience_write import _derive_goal_id_from_id


def _live(ctx):
    return ctx.paths.agent / "experience.jsonl"


def _archive(ctx):
    return ctx.paths.agent / "experience-archive.jsonl"


def _meta(ctx):
    return ctx.paths.agent / "experience-meta.json"


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

    # ── COMPOSED READ PIPELINE () ─────────────────────────────
    # These params were once a first-match-wins if/return chain, so a
    # COMBINATION silently took the earliest branch and dropped the rest:
    # `--type goal_execution --recent 30` returned the whole type-filtered
    # store (849 records here, byte-identical to `--type` alone) because the
    # `type` branch returned before `recent` was ever read. No error, rc=0.
    #
    # The module docstring called these "mutually exclusive", but NOTHING
    # ENFORCED that and a real caller was already combining them:
    # aspirations-consolidate Step 2.9 has called
    # `--type goal_execution --recent 30 --summary` on every consolidation, on
    # every box, since long before this was noticed. So the exclusivity was a
    # claim in prose, not a property of the code — and the gap ran silent for
    # the one reason that matters: each flag is CORRECT ALONE, so any test
    # covering them individually passes while the combination is broken.
    #
    # Three separable concerns, applied in order. Filters NARROW, selectors
    # ORDER-AND-LIMIT, format RENDERS. Ordering is load-bearing: filter before
    # limit, or `--type X --recent N` returns the N most recent records OF ANY
    # TYPE and then filters them, which yields fewer than N (often zero) while
    # still looking plausible.
    items = list(jc.get(_live(ctx)))

    category = q.get("category")
    if category:
        items = [r for r in items if r.get("category") == category]

    goal = q.get("goal")
    if goal:
    # Match the goal_id FIELD or the goal-id embedded in the record ID
    # (). The field alone is not sufficient, and the reason is
    # durability rather than writer bugs:  already fixed the
    # writer and REPAIRED 862 records in this file at 04:13 on 2026-08-21 —
    # by 06:00 the very next writes had reverted 26 of them to null. This
    # store is append-heavy and fleet-synced, so a peer holding a copy that
    # predates a repair silently un-does it on merge (guard-3209: a locked
    # write plus a clean re-read proves the write LANDED, not that it
    # SURVIVES). A field-only predicate therefore re-breaks on its own,
    # and re-running the backfill is a treadmill.
    #
    # The record ID cannot be eroded that way — it is the record's identity,
    # every writer forms it as exp-{goal_id}[-{slug}], and no merge rewrites
    # it. So deriving from the id makes this read correct for the 28
    # currently-invisible records AND immune to the next erosion, with no
    # migration.
    #
    # This is what makes guard-2939's anti-overwrite pre-check able to fire
    # at all: that check asks "does this goal already have an experience?"
    # and a false [] tells a caller the bare exp-<goal-id> id and .md path
    # are free when they are taken, so the Write silently overwrites.
    #
    # NOT widened to source_id, deliberately: source_id is the goal id only
    # for goal_execution records, and carries hypothesis/other ids on the
    # rest, so matching it would return foreign records under a goal query.
    # The id derivation is exact — the regex anchors a full canonical goal
    # id — so it cannot false-positive.
        items = [
            r for r in items
            if r.get("goal_id") == goal
            or _derive_goal_id_from_id(r.get("id")) == goal
        ]

    hypothesis = q.get("hypothesis")
    if hypothesis:
        items = [r for r in items if r.get("hypothesis_id") == hypothesis]

    typ = q.get("type")
    if typ:
        items = [r for r in items if r.get("type") == typ]

    # Selectors stay mutually exclusive AMONG THEMSELVES (most > least >
    # recent), preserving the pre-existing precedence: two orderings cannot
    # both apply, and silently picking one was the old behaviour too.
    most = q.get("most_retrieved")
    least = q.get("least_retrieved")
    recent = q.get("recent")
    if most is not None:
        n, err = parse_int_param(most, "most_retrieved", 10)
        if err is not None:
            return err
        items.sort(key=lambda r: (r.get("retrieval_stats") or {}).get("retrieval_count", 0),
                   reverse=True)
        items = items[:n]
    elif least is not None:
        n, err = parse_int_param(least, "least_retrieved", 10)
        if err is not None:
            return err
        items.sort(key=lambda r: (r.get("retrieval_stats") or {}).get("retrieval_count", 0))
        items = items[:n]
    elif recent is not None:
        n, err = parse_int_param(recent, "recent", 10)
        if err is not None:
            return err
        items.sort(key=lambda r: r.get("created", ""), reverse=True)
        items = items[:n]

    want_summary = flag(q, "summary")
    # `summary` alone is a legitimate whole-store query and must keep working,
    # so it LICENSES the read as well as formatting it — otherwise a bare
    # --summary would fall through to the missing-filter error below.
    selected = bool(category or goal or hypothesis or typ) or (
        most is not None or least is not None or recent is not None
    )
    if selected or want_summary:
        if want_summary:
            # PLAIN TEXT BY DESIGN, and that is not a defect of the
            # combination: `--summary` ALONE returns these same lines. A caller
            # that needs to json.load the result must not pass --summary, and a
            # caller that needs a field this line does not carry (notably
            # tree_nodes_related) cannot use --summary at all. It now at least
            # summarises the FILTERED, LIMITED set rather than the whole store.
            return plain_lines([
                f"{rec.get('id', '?')}: [{rec.get('type', '?')}] "
                f"{rec.get('category', '?')} — {rec.get('summary', '(no summary)')}"
                for rec in items
            ])
        return json_response_pretty(items)

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
