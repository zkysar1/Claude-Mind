#!/usr/bin/env python3
"""_dt.py -- shared tolerant naive-ISO datetime parsing.

Single home for the "parse an ISO-8601 timestamp into a NAIVE datetime"
operation that ~24 core/scripts sites had each open-coded as
``datetime.fromisoformat(str(s).replace("Z", ""))`` (g-115-3008 audit /
g-115-3027 consolidation).

Why the open-coded idiom is a latent bug
----------------------------------------
``str.replace("Z", "")`` strips a trailing ``Z`` but does NOT strip a numeric
``+00:00`` / ``-05:00`` offset. So an offset-bearing input yields an *aware*
datetime, and the very next line -- ``datetime.now() - dt`` (naive ``now``) --
raises ``TypeError: can't subtract offset-naive and offset-aware datetimes``.
It stays LATENT under the naive-UTC convention (g-115-2546: all internal
timestamps are naive wall-clock UTC) and only fires on off-convention or
external data (e.g. a skill-invocation source, an experience timestamp, an
S3 ``LastModified`` echoed back). The robust fix is to STRIP tzinfo after
parsing, never to string-munge the offset away.

``parse_naive_iso`` is the canonical implementation, lifted verbatim in spirit
from the already-proven ``liveness_check._parse_iso`` (which fixed this exact
class for the liveness path). Route new date-parse sites here rather than
re-open-coding the idiom.
"""

from datetime import datetime

__all__ = ["parse_naive_iso"]


def parse_naive_iso(val):
    """Parse an ISO-8601 timestamp into a NAIVE datetime, or return None.

    Tolerant of:
      - None / empty / the literal strings "null"/"none" (-> None)
      - JSON quoting (surrounding single or double quotes)
      - a trailing ``Z`` (UTC designator -> ``+00:00`` before parsing)
      - a numeric tz offset (``+00:00`` etc.) -- stripped AFTER parsing so the
        result is always naive (this is the bit ``.replace("Z", "")`` got wrong)

    Returns a naive ``datetime`` (tzinfo stripped) so age math against a naive
    ``datetime.now()`` is consistent whether or not the source carried an
    offset. Returns None on any unparseable input -- never raises.
    """
    if val is None:
        return None
    s = str(val).strip().strip('"').strip("'")
    if not s or s.lower() in ("null", "none"):
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    # Normalize to naive local time so age math against a naive ``now`` is
    # consistent whether the source carried a tz offset or not.
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt
