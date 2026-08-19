"""Shared helpers for JSONL-reader endpoints.

Every Phase B PR 4 endpoint (pipeline, reasoning-bank, guardrails, pattern-
signatures, spark-questions, experience, journal) reads JSONL via jsonl_cache
and emits stdout-equivalent JSON. The shape is repetitive enough to deserve
shared helpers; differences (field names, sort keys, summary line formats)
stay in each endpoint module.

ENCODING NOTE: every json.dumps here uses `ensure_ascii=False`. The Python
CLIs do the same. Matching this byte-for-byte is the whole point of the
"output equivalence" contract — switching to ensure_ascii=True silently
escapes non-ASCII characters in summaries and breaks every parity test.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple


def find_by_id(items: List[Dict[str, Any]], rec_id: str) -> Optional[Tuple[int, Dict[str, Any]]]:
    """Return (index, record) for the first item whose id == rec_id, or None."""
    for i, rec in enumerate(items):
        if rec.get("id") == rec_id:
            return (i, rec)
    return None


# --- collision-reid displacement awareness () ---------------------
# coordination_merge.py::_merge_id_keyed_jsonl re-ids a record when two boxes
# independently mint the same guard-N/rb-N for DIFFERENT records: the earlier
# `created` keeps the id, the loser is moved to the next free id and stamped
# `displaced_from: <the id it lost>`.
#
# The merge preserves both RECORDS. Nothing preserves REFERENCES TO them. A
# citation written before the merge keeps naming the old id, which after the
# merge belongs to an unrelated record -- so the lookup does not 404, it
# returns a well-formed WRONG answer with no signal anything moved. Measured
# on this world 2026-08-18: 68 real displacement events, 12 ids whose old
# number now resolves to unrelated content, ~435 live citations of those 12.
# Already burned a reader once -- the directive-lane-series-bravo tree node
# carries a hand-written CORRECTION for guard-3785 that misattributes the
# reassignment to its own authoring error and abandons the knowledge, when
# the successor (guard-3786) was one displaced_from lookup away.
#
# The old->new mapping is NOT lost: it is durably on the successor record.
# These helpers surface it at read time. They deliberately do NOT redirect --
# the record AT the requested id is still the correct answer for that id
# (communication-clarity.md rule 5: fail visibly, never silently substitute a
# different source). They ANNOTATE, and on a genuine miss they name the
# successor in the 404 rather than swallowing it.
def find_displacers(items: List[Dict[str, Any]],
                    rec_id: str) -> List[Dict[str, Any]]:
    """Records stamped `displaced_from == rec_id` — i.e. every record that
    once held this id and was re-id'd off it by the collision-reid path."""
    return [r for r in items
            if isinstance(r, dict) and r.get("displaced_from") == rec_id]


def displacement_notice(rec_id: str,
                        displacers: List[Dict[str, Any]]) -> str:
    """Human-readable ambiguity warning for a record served under an id that
    another record was displaced from. Names successors so a stale citation
    can be re-resolved without a content search (guard-1154's manual
    re-resolve step, automated)."""
    ids = ", ".join(str(r.get("id")) for r in displacers)
    return (f"AMBIGUOUS ID: {rec_id} was also held by {len(displacers)} other "
            f"record(s) before a concurrent-allocation collision re-id'd "
            f"them to [{ids}]. A citation of {rec_id} written before that "
            f"merge refers to one of those, NOT to this record. Verify the "
            f"cited meaning against this record's content before acting.")


def not_found_detail(rec_id: str, displacers: List[Dict[str, Any]]) -> str:
    """404 detail that names the successor when the id was vacated by a
    re-id, instead of a bare not-found that hides a recoverable answer."""
    if not displacers:
        return f"Record {rec_id} not found"
    ids = ", ".join(str(r.get("id")) for r in displacers)
    return (f"Record {rec_id} not found — it was displaced by a "
            f"concurrent-allocation collision re-id. Successor record(s): "
            f"[{ids}].")


def json_response_pretty(obj: Any):
    """Emit a Response.text wrapping json.dumps(..., indent=2, ensure_ascii=False)."""
    from ..server import Response
    return Response.text(
        json.dumps(obj, indent=2, ensure_ascii=False),
        content_type="application/json",
    )


def json_response_compact(obj: Any):
    """Emit a Response.text wrapping json.dumps(..., no indent, ensure_ascii=False)."""
    from ..server import Response
    return Response.text(
        json.dumps(obj, ensure_ascii=False),
        content_type="application/json",
    )


def plain_lines(lines: List[str]):
    """Emit a Response.text plain/text body from a list of lines (newline-joined)."""
    from ..server import Response
    return Response.text("\n".join(lines), content_type="text/plain")


def missing_flag_error(flags: List[str]):
    """Mirror the CLI's `Specify one of: --a, --b, --c` stderr line as a 400."""
    from ..server import Response
    label = ", ".join(f"--{f.replace('_', '-')}" for f in flags)
    return Response.error(400, "missing_flag", f"Specify one of: {label}")


def flag(q: Dict[str, str], name: str) -> bool:
    """True iff query string `name` is present with a truthy value (1/true/yes)."""
    v = q.get(name)
    if v is None:
        return False
    return v.lower() not in ("", "0", "false", "no")
