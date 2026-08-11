"""GET /v1/aspirations/query — pre-migration `aspirations.py query` parity.

Query parameters (at least one required — same rule as the CLI):
    goal_status=<status>[,<status>...]   comma-separated, validated against
                                          VALID_GOAL_STATUSES
    goal_field_name=<field>              paired with goal_field_value
    goal_field_value=<value>             AND semantics with name (both or neither)
    title_contains=<substring>           case-insensitive
    full=true|1|yes                      return the full canonical goal record
                                          (raw goal dict + {asp_id, source}) instead
                                          of the 6-field projection (g-115-1304;
                                          category added g-115-1614)

The identifier key is available as BOTH `id` and `goal_id` in full mode
(g-115-3473), so a caller keyed on either name is correct. The default
projection still emits `goal_id` only. Before g-115-3473 the two projections
had mutually exclusive id keys, which silently returned None for every record
when a goal_id-keyed reader passed --full.

Response: application/json — `json.dumps(results, indent=2, ensure_ascii=True)`,
byte-for-byte matching `cmd_query`'s stdout. Always reads BOTH world and agent
queues: `--source` is ACCEPTED BUT DOES NOT FILTER, so `--source agent` returns
world goals too. That is by design, not a bug — every row carries its own
`source` key, so a caller filters client-side on that. Measured cc-07: the two
invocations are byte-identical, 3982 rows, {'world': 3933, 'agent': 49}.
(The previous citation here pointed at `aspirations.py:879`, which is now
unrelated `streak-breaks.jsonl` code — the line number drifted, so a reader
following it could not verify the claim. Behaviour re-verified by measurement
rather than re-cited to another line that will drift again.)

If NEITHER store path exists the endpoint returns 404 `no_aspiration_store`
naming the paths probed — it does NOT return an empty array (g-115-4842).

What this endpoint does NOT do (Phase B scope-cut, same as aspirations/read):
  - Call `_log_goal_read` for each returned goal. The CLI logged reads to power
    the stale-read gate, which was retired g-115-2107 — so this logging is not
    coming back. Historical context: mind_api/docs/development-history/DECISIONS.md.

INVARIANT: VALID_GOAL_STATUSES mirrors aspirations.py:37. Mirroring keeps the
endpoint independent of aspirations.py's import side effects (utf-8 reconfigure,
_gate_log import chain) — same rationale as _COMPACT_GOAL_KEEP in
endpoints/aspirations.py. When VALID_GOAL_STATUSES changes upstream, mirror here.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from ..jsonl_cache import cache


VALID_GOAL_STATUSES = {
    "pending", "in-progress", "completed", "blocked",
    "skipped", "expired", "decomposed", "superseded",
}


def _parse_goal_status(raw: str):
    """Mirror of aspirations.py:_parse_goal_status_list. Returns (set, error_str).
    error_str is non-empty on invalid input — caller maps to a 400."""
    wanted = {s.strip() for s in raw.split(",") if s.strip()}
    invalid = wanted - VALID_GOAL_STATUSES
    if invalid:
        return None, (
            f"invalid status(es): {','.join(sorted(invalid))}. "
            f"Valid: {','.join(sorted(VALID_GOAL_STATUSES))}"
        )
    return wanted, ""


def _goal_matches(goal: Dict[str, Any], status_filter, field_filter, title_substr) -> bool:
    """AND semantics across filters. Mirror of aspirations.py:_goal_matches."""
    if status_filter is not None:
        if goal.get("status") not in status_filter:
            return False
    if field_filter is not None:
        field, value = field_filter
        actual = goal.get(field)
        if isinstance(actual, list):
            if value not in actual:
                return False
        else:
            if str(actual) != value:
                return False
    if title_substr is not None:
        title = goal.get("title", "")
        if title_substr.lower() not in title.lower():
            return False
    return True


def query(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response

    q = ctx.query
    raw_status = q.get("goal_status")
    raw_field_name = q.get("goal_field_name")
    raw_field_value = q.get("goal_field_value")
    raw_title = q.get("title_contains")
    raw_full = q.get("full")
    # full=true|1|yes → return raw goal records + {asp_id, source} metadata instead
    # of the 5-field projection. Goal-by-id full-record read in one call ().
    full_mode = raw_full is not None and str(raw_full).strip().lower() in ("true", "1", "yes")

    # --goal-field is a 2-tuple at the CLI; rebuild from paired query params.
    # Reject "name but no value" / "value but no name" with the same shape of
    # error argparse would produce — keeps wrapper authors honest.
    if (raw_field_name is None) != (raw_field_value is None):
        return Response.error(
            400, "invalid_goal_field",
            "goal_field_name and goal_field_value must be provided together",
        )

    if not (raw_status or raw_field_name or raw_title):
        # Mirror cmd_query line 875 stderr message verbatim, status 400.
        return Response.error(
            400, "missing_filter",
            "Specify at least one filter (goal_status, goal_field, title_contains)",
        )

    status_filter = None
    if raw_status is not None:
        status_filter, err = _parse_goal_status(raw_status)
        if err:
            return Response.error(400, "invalid_goal_status", err)

    field_filter: Tuple[str, str] | None = None
    if raw_field_name is not None:
        field_filter = (raw_field_name, raw_field_value)

    jc = cache()
    sources: List[Tuple[str, List[Dict[str, Any]]]] = []
    probed: List[str] = []
    world_path = ctx.paths.world / "aspirations.jsonl"
    probed.append(str(world_path))
    if world_path.exists():
        sources.append(("world", jc.get(world_path)))
    if ctx.paths.agent is not None:
        agent_path = ctx.paths.agent / "aspirations.jsonl"
        probed.append(str(agent_path))
        if agent_path.exists():
            sources.append(("agent", jc.get(agent_path)))

    # ZERO READABLE STORES IS NOT AN EMPTY RESULT — it is an unanswerable query
    # (). Without this, both `.exists()` checks failing left `sources`
    # empty, the loop below ran zero times, and the endpoint returned `[]` with
    # HTTP 200 and no stderr — byte-identical to a genuinely-clean queue. That is
    # the reported signature exactly: on one box EVERY query returned [] rc=0
    # while `aspirations-read.sh` (a different resolver) reported 5149 world
    # goals in the same minute. A zero whose two explanations imply OPPOSITE
    # actions is not a measurement (rb-245), and here the two are "nothing
    # matched your filter" and "I could not find the store at all".
    #
    # Deliberately an ERROR, not an empty list with a warning: this endpoint's
    # whole output is the JSON array, so a caller parsing it has nowhere to see
    # a warning, and every existing consumer already treats [] as authoritative.
    # A fresh world with no aspirations.jsonl yet also has no goals to query, so
    # naming the paths probed is strictly more useful there too.
    if not sources:
        return Response.error(
            404, "no_aspiration_store",
            "no aspiration store found — probed: " + ", ".join(probed),
        )

    results: List[Dict[str, Any]] = []
    for source_name, aspirations in sources:
        for asp in aspirations:
            asp_id = asp.get("id", "")
            for goal in asp.get("goals", []):
                if _goal_matches(goal, status_filter, field_filter, raw_title):
                    if full_mode:
                        # Full-record read (): raw goal dict + {asp_id,
                        # source} metadata. Returns the canonical record
                        # (description, verification, priority, intended_agent) so a
                        # goal-by-id lookup needs one call, not an aspiration-scoped
                        # read + .goals[] traverse. asp_id/source are appended last so
                        # they override any same-named goal keys (metadata authoritative).
                        #
                        # goal_id is emitted ALONGSIDE the raw record's own "id" so
                        # either key works and no caller can be wrong ().
                        # The two projections previously had MUTUALLY EXCLUSIVE id
                        # keys: default carried goal_id and no id, full carried id and
                        # no goal_id. That is a silent-None trap precisely because
                        # --full is the mode the Multi-Agent Safety Rule mandates (it
                        # is the only mode carrying claimed_by), so the flag that makes
                        # the guard possible was the flag that nullified a goal_id-keyed
                        # implementation of it. Emitted last, like asp_id/source, so a
                        # stray goal key cannot shadow it.
                        results.append({**goal, "goal_id": goal.get("id", ""),
                                        "asp_id": asp_id, "source": source_name})
                    else:
                        results.append({
                            "goal_id": goal.get("id", ""),
                            "asp_id": asp_id,
                            "source": source_name,
                            "title": goal.get("title", ""),
                            "status": goal.get("status", ""),
                            "category": goal.get("category", ""),
                        })

    # cmd_query uses ensure_ascii=True — match byte-for-byte.
    return Response.text(
        json.dumps(results, indent=2, ensure_ascii=True),
        content_type="application/json",
    )


def register(routes) -> None:
    routes[("GET", "/v1/aspirations/query")] = query
