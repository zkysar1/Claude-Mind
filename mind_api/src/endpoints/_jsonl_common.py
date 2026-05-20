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
