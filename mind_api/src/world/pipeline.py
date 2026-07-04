"""GET /v1/pipeline/read — pre-migration `pipeline.py read --<flag>` parity.

Query parameters (mutually exclusive — exactly one):
    stage=<discovered|active|measurement-pending|resolved|archived>
    id=<rec-id>
    summary=1
    counts=1
    accuracy=1
    unreflected=1
    replay_candidates=1
    archive=1
    meta=1

Equivalence target: stdout of `python3 core/scripts/pipeline.py read --<flag>`.

What this endpoint does NOT do:
  - Honor source=agent. Pipeline is world-only by design (only one
    pipeline per domain; agent-local pipelines were never built).
"""
from __future__ import annotations

import json
from datetime import date

from ..jsonl_cache import cache
from ..endpoints._jsonl_common import (
    find_by_id, flag, json_response_pretty, missing_flag_error, plain_lines,
)


VALID_STAGES = {"discovered", "active", "measurement-pending", "resolved", "archived"}


def _live_path(ctx):
    return ctx.paths.world / "pipeline.jsonl"


def _archive_path(ctx):
    return ctx.paths.world / "pipeline-archive.jsonl"


def _meta_path(ctx):
    return ctx.paths.world / "pipeline-meta.json"


def read(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response

    q = ctx.query
    jc = cache()

    stage = q.get("stage")
    if stage:
        if stage not in VALID_STAGES:
            return Response.error(400, "invalid_stage", f"Invalid stage: {stage}")
        path = _archive_path(ctx) if stage == "archived" else _live_path(ctx)
        items = jc.get(path)
        if stage != "archived":
            items = [r for r in items if r.get("stage") == stage]
        return json_response_pretty(items)

    rec_id = q.get("id")
    if rec_id:
        items = jc.get(_live_path(ctx))
        result = find_by_id(items, rec_id)
        if result is None:
            items = jc.get(_archive_path(ctx))
            result = find_by_id(items, rec_id)
        if result is None:
            return Response.error(404, "not_found", f"Record {rec_id} not found")
        return json_response_pretty(result[1])

    if flag(q, "summary"):
        items = jc.get(_live_path(ctx))
        lines = []
        for rec in items:
            stg = (rec.get("stage") or "?").upper()
            outcome = rec.get("outcome", "")
            outcome_str = f" → {outcome}" if outcome else ""
            title = rec.get("title", "(untitled)")
            lines.append(f"{rec.get('id', '?')}: {title} [{stg}]{outcome_str}")
        return plain_lines(lines)

    if flag(q, "counts"):
        meta_p = _meta_path(ctx)
        from storage_backend import get_backend
        get_backend().ensure_local(meta_p)  # own-cloud read-path fix: materialize S3-only file before local read
        if meta_p.exists():
            try:
                meta = json.loads(meta_p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                return Response.error(500, "meta_read_failed", str(e))
            return json_response_pretty(meta.get("stage_counts", {}))
        # Compute from data
        items = jc.get(_live_path(ctx))
        archive = jc.get(_archive_path(ctx))
        counts = {"discovered": 0, "active": 0, "measurement-pending": 0,
                  "resolved": 0, "archived": len(archive)}
        for r in items:
            stg = r.get("stage", "discovered")
            if stg in counts:
                counts[stg] += 1
        return json_response_pretty(counts)

    if flag(q, "accuracy"):
        meta_p = _meta_path(ctx)
        from storage_backend import get_backend
        get_backend().ensure_local(meta_p)  # own-cloud read-path fix: materialize S3-only file before local read
        if meta_p.exists():
            try:
                meta = json.loads(meta_p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                return Response.error(500, "meta_read_failed", str(e))
            return json_response_pretty(meta.get("accuracy", {}))
        return Response.text("{}", content_type="application/json")

    if flag(q, "unreflected"):
        items = jc.get(_live_path(ctx))
        unreflected = [r for r in items
                       if r.get("stage") == "resolved" and not r.get("reflected", False)]
        return json_response_pretty(unreflected)

    if flag(q, "replay_candidates"):
        items = jc.get(_live_path(ctx))
        archive = jc.get(_archive_path(ctx))
        all_resolved = [r for r in list(items) + list(archive)
                        if r.get("stage") in ("resolved", "archived")]
        candidates = []
        today = date.today()
        for r in all_resolved:
            if not r.get("reflected", False):
                continue
            replay = r.get("replay_metadata") or {}
            # 1: a chronic-CORRECTED hypothesis encoded as a calibration
            # guardrail by Replay Step 3.6 has zero further replay value. Archived
            # records are merged into the candidate pool above, so without this
            # source-level exclusion an encoded item re-surfaces every cycle
            # (self-limited only by the rc>=5 cap, ~3-5 wasted cycles each). This
            # bash-gates the skip that Replay Step 1's spaced-repetition filter
            # also applies LLM-side — script-enforced > LLM-gated.
            if replay.get("encoded_via_chronic") is True:
                continue
            next_review = replay.get("next_review_date")
            if next_review:
                try:
                    review_date = date.fromisoformat(next_review)
                    if review_date > today:
                        continue
                except ValueError:
                    pass
            candidates.append(r)
        return json_response_pretty(candidates)

    if flag(q, "archive"):
        return json_response_pretty(jc.get(_archive_path(ctx)))

    if flag(q, "meta"):
        meta_p = _meta_path(ctx)
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
        "stage", "id", "summary", "counts", "accuracy",
        "unreflected", "replay_candidates", "archive", "meta",
    ])


def register(routes) -> None:
    routes[("GET", "/v1/pipeline/read")] = read
