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
    narrative=1   — composes with `id=` (one record) or `stage=` (filtered);
                   alone it covers the live+archive union. See NARRATIVE_CHAIN.

`stage=archived` does NOT mean the same thing to `narrative=1` as it does to the
bare `stage` branch, and the difference is load-bearing. The bare branch reads the
ARCHIVE FILE ONLY and skips the stage filter entirely; `narrative` unions live+archive
with live-wins dedup (replay_candidates' ordering) and then filters on the record's own
`stage`. For an id present in BOTH files with DIFFERENT stages, the live copy wins and
is then excluded. Measured 2026-08-04 (echo, cc-03): 829 vs 827 — the two ids are
`2026-05-12_g-115-644-prune-steady-state` (live stage discovered) and
`2026-07-08_cc05-gated-goals-churn-until-preconditioned` (live stage measurement-pending).
`narrative` reports the FRESHER stage; the bare branch reports a stale archived label.
Pinned by test_archived_stage_asymmetry_is_pinned — do not "fix" it by mimicking the
archive-only read, which would report a stage the live record contradicts.

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

# The outcome narrative is NOT always in `outcome_detail`. Ordered fallback chain,
# canonical source for every reader (gap-062). Measured 2026-08-04 over the 351-record
# replay-candidate population: outcome_detail wins on 260 (74.1%); 79 (22.5%) win on a
# fallback key; 12 (3.4%) have no narrative under ANY of the ten. A reader keyed on
# outcome_detail alone renders the last two groups identically, so "recorded under a
# different key" and "never recorded" become indistinguishable — which is the whole
# reason this lives in one place instead of being re-derived per caller.
# `result` is deliberately absent: it is the bare verdict ("CONFIRMED"), never prose.
NARRATIVE_CHAIN = (
    "outcome_detail", "resolution_note", "resolution", "resolution_summary",
    "resolution_evidence", "outcome_note", "reflection_note", "actual_outcome",
    "evidence_for", "rationale",
)


def _narrative_text(value):
    """Coerce a narrative field of ANY shape to a single string; "" means absent.

    Not every narrative key holds a str. Measured 2026-08-04 across the same 351
    records: 6 winning values were LISTS (evidence_for) and 1 was a DICT (resolution).
    A normalizer that assumes str raises AttributeError on exactly those 7, so the
    coercion is load-bearing rather than defensive. Never truncates — a hand-rolled
    variant that truncated before scanning produced a false 0-of-10 indicator scan
    (zeta, 2026-07-31), which is the measurement error this helper exists to retire.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "; ".join(p for p in (_narrative_text(v) for v in value) if p)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True) if value else ""
    return str(value).strip()


def narrative_of(rec):
    """(key, text) for the first non-empty link in NARRATIVE_CHAIN; (None, "") if bare."""
    for key in NARRATIVE_CHAIN:
        text = _narrative_text(rec.get(key))
        if text:
            return key, text
    return None, ""


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

    # Checked BEFORE stage/id: those two branches return early, so `narrative=1&id=X`
    # would otherwise be swallowed and answered with the raw record. Nothing existing
    # sets `narrative`, so precedence here cannot change any current caller's result.
    if flag(q, "narrative"):
        n_stage = q.get("stage")
        if n_stage and n_stage not in VALID_STAGES:
            return Response.error(400, "invalid_stage", f"Invalid stage: {n_stage}")
        by_id: dict = {}
        # live iterates second → the live copy wins the dedup, matching where
        # update_field actually writes (same ordering rationale as replay_candidates).
        for r in list(jc.get(_archive_path(ctx))) + list(jc.get(_live_path(ctx))):
            rid = r.get("id")
            if rid is not None:
                by_id[rid] = r
        n_id = q.get("id")
        if n_id:
            rec = by_id.get(n_id)
            if rec is None:
                return Response.error(404, "not_found", f"Record {n_id} not found")
            selected = [rec]
        else:
            selected = [r for r in by_id.values()
                        if not n_stage or r.get("stage") == n_stage]
        out = []
        for r in selected:
            key, text = narrative_of(r)
            out.append({
                "id": r.get("id"),
                "stage": r.get("stage"),
                "outcome": r.get("outcome", ""),
                # null (not "") when the record is genuinely bare, so a caller can
                # tell an unrecorded lesson from an unread one without re-deriving it.
                "narrative_key": key,
                "narrative": text,
                "chars": len(text),
            })
        return json_response_pretty(out)

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
            # Live stage=archived tombstones () are counted by their
            # archive-file copy already — skip them here or the count doubles.
            if stg in counts and stg != "archived":
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
        # Dedup by id (): a tombstoned id is present in BOTH files by
        # design — without collapsing, every archived hypothesis would surface
        # twice as a replay candidate.
        #
        # WHICH copy wins: the LIVE copy ( — read/write copy-preference
        # inversion fix). A record moved to archived is kept in live as a FULL
        # stage=archived tombstone AND appended to archive exactly ONCE
        # (pipeline_write.move dedup-guards the archive append). update_field
        # probes LIVE-first, so every post-archival replay stamp (replay_count,
        # next_review_date) lands on the LIVE copy — the archive copy is frozen
        # at first-archival. The prior archive-wins order therefore read STALE
        # metadata: a replay that pushed next_review into the future (written to
        # live) was invisible here, so the exclusion never took and the record
        # leaked back as a candidate every cycle (150/165 = 91% of world records
        # at diagnosis, /rb-4354). Iterating archive FIRST then live
        # makes the fresher live copy win the last-write dedup, aligning this
        # read with where update_field actually writes. Archive-only records
        # (live tombstone already pruned) still resolve to the archive copy —
        # their only copy, and also update_field's target — so no case regresses.
        _by_id: dict = {}
        for r in list(archive) + list(items):
            rid = r.get("id")
            if rid is not None:
                _by_id[rid] = r  # live (items) iterates second → live copy wins ()
        all_resolved = [r for r in _by_id.values()
                        if r.get("stage") in ("resolved", "archived")]
        candidates = []
        today = date.today()
        for r in all_resolved:
            if not r.get("reflected", False):
                continue
            replay = r.get("replay_metadata") or {}
            # : a chronic-CORRECTED hypothesis encoded as a calibration
            # guardrail by Replay Step 3.6 has zero further replay value. Archived
            # records are merged into the candidate pool above, so without this
            # source-level exclusion an encoded item re-surfaces every cycle
            # (self-limited only by the rc>=5 cap, ~3-5 wasted cycles each). This
            # bash-gates the skip that Replay Step 1's spaced-repetition filter
            # also applies LLM-side — script-enforced > LLM-gated.
            if replay.get("encoded_via_chronic") is True:
                continue
            # : rc>=5 records have exhausted the spaced-repetition
            # ladder (Replay Step 1's cap). For already-archived records the
            # LLM-side remedy (pipeline-move to archived) is a no-op, so
            # without this source-level exclusion they resurface every cycle.
            # replay_count is a string on some records — coerce; unparseable
            # values fall through to include (fail-open).
            try:
                if int(replay.get("replay_count") or 0) >= 5:
                    continue
            except (TypeError, ValueError):
                pass
            next_review = replay.get("next_review_date")
            if next_review:
                try:
                    # [:10] tolerates datetime-form strings ("YYYY-MM-DDTHH:MM:SS")
                    # — bare fromisoformat(date) rejects them and the swallowed
                    # ValueError would silently defeat the 7-day exclusion.
                    review_date = date.fromisoformat(str(next_review)[:10])
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
        "unreflected", "replay_candidates", "archive", "meta", "narrative",
    ])


def register(routes) -> None:
    routes[("GET", "/v1/pipeline/read")] = read
