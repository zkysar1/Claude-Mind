"""Extract structured `deferred_until` date from a defer_reason narrative.

Daemon-safe extraction (PR 7e/3) — pure `extract(text, now=None)` function
plus constants. Used by:
  - aspirations.py:_extract_defer_date (CLI path, now imports directly)
  - mind_api/src/endpoints/aspirations_write.py (daemon update_goal)
  - defer-date-extractor.py CLI shim

Reads a defer_reason narrative ("Not before 2026-07-14", "after July 14,
2026", "in 7 days") and returns the implied future ISO 8601 timestamp
suitable for `deferred_until`. Earliest future match wins (most
conservative defer). No match → matched=False.

Origin: LifingPolls plan item 5 (2026-05-08). Auto-pairs narrative defers
with structured time gates so goal-selector and aspirations-precheck can
act on them mechanically.

Daemon safety: pure function. No I/O, no env reads. `now` is injectable
for deterministic tests.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional


# ---- Constants -----------------------------------------------------------

# Month name → number lookup. Includes 3-letter abbreviations. Case-
# insensitive matching at use site.
MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

# Relative-time multipliers (in days). "month" intentionally ~30 —
# narrative dates are not surveying-grade, and 30 is the standard
# accounting month for "in N months" estimates. "year" is 365.
RELATIVE_UNITS = {
    "day": 1, "days": 1,
    "week": 7, "weeks": 7,
    "month": 30, "months": 30,
    "year": 365, "years": 365,
}


# ---- Patterns ------------------------------------------------------------

# Compiled regex patterns, ordered by specificity. First match wins.
ISO_DATE = re.compile(
    r"\b(?P<year>20\d{2})-(?P<month>\d{1,2})-(?P<day>\d{1,2})\b"
)

# "July 14, 2026" / "Jul 14 2026" / "14 July 2026"
MONTH_DAY_YEAR = re.compile(
    r"\b(?P<month>january|jan|february|feb|march|mar|april|apr|may|june|jun|"
    r"july|jul|august|aug|september|sep|sept|october|oct|november|nov|"
    r"december|dec)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<year>20\d{2})\b",
    re.IGNORECASE,
)
DAY_MONTH_YEAR = re.compile(
    r"\b(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?P<month>january|jan|february|feb|"
    r"march|mar|april|apr|may|june|jun|july|jul|august|aug|september|sep|sept|"
    r"october|oct|november|nov|december|dec),?\s+(?P<year>20\d{2})\b",
    re.IGNORECASE,
)

# "in 7 days", "in 2 weeks", "in 3 months"
RELATIVE_IN_N = re.compile(
    r"\bin\s+(?P<n>\d+)\s+(?P<unit>days?|weeks?|months?|years?)\b",
    re.IGNORECASE,
)

# "next week", "next month", "next year"
RELATIVE_NEXT = re.compile(
    r"\bnext\s+(?P<unit>week|month|year)\b",
    re.IGNORECASE,
)

# "tomorrow"
RELATIVE_TOMORROW = re.compile(r"\btomorrow\b", re.IGNORECASE)


# ---- Due-date disambiguation (3) --------------------------------
#
# A date governed by DUE-BY language ("by 2026-11-02", "submit by X",
# "X deadline") is a DUE date — work must happen BEFORE it — NOT a
# start-after date. Storing such a date as `deferred_until` inverts the
# semantics: it freezes the goal until its own deadline. Near-miss:
#  (ARC final-submission) sat frozen on deferred_until=2026-11-02
# extracted from "submit ... by the 2026-11-02 deadline".
#
# Only ABSOLUTE dates are guarded — relative phrasings ("in 7 days",
# "next week", "tomorrow") are inherently start-after and cannot be
# governed by a due-by preposition. Start-after markers ("until", "after",
# "not before", "from") are deliberately NOT in the due-set, so the
# module's intended inputs ("Not before 2026-07-14", "after July 14, 2026",
# "Defer until 14 March 2027") keep matching.

# A due-preposition immediately preceding the date (window ENDS with it).
_DUE_BEFORE = re.compile(
    r"\b(?:by|due(?:\s+by)?|submit(?:ted)?\s+by|complete[d]?\s+by|"
    r"finish(?:ed)?\s+by|ship\s+by|deliver(?:ed)?\s+by|"
    r"no\s+later\s+than|nlt)\s+(?:the\s+)?$",
    re.IGNORECASE,
)
# "deadline" immediately following the date (window STARTS with it).
_DUE_AFTER = re.compile(r"^\W*deadline\b", re.IGNORECASE)


def _is_due_context(text: str, start: int, end: int) -> bool:
    """True if the date at text[start:end] is governed by due-date language
    ("by X", "X deadline") rather than start-after language ("until X",
    "after X", "not before X"). Such a date is a due date and MUST NOT
    become a deferred_until. See g-115-1783."""
    before = text[max(0, start - 30):start]
    after = text[end:end + 15]
    return bool(_DUE_BEFORE.search(before) or _DUE_AFTER.match(after))


# ---- Extraction ----------------------------------------------------------

def extract(text: str, now: Optional[datetime] = None) -> dict:
    """Extract earliest matching future date from text.

    Returns:
        {"matched": bool,
         "deferred_until": str | None,    # ISO 8601 if matched
         "pattern": str | None,           # which pattern fired
         "match_text": str | None}        # the substring that matched
    """
    if now is None:
        now = datetime.now()
    text = text or ""

    # Try patterns in order of specificity. Absolute dates first (highest
    # signal), then relative.
    matches: list[tuple[datetime, str, str]] = []

    for m in ISO_DATE.finditer(text):
        # 3: skip due-by dates ("by X", "X deadline") — they are
        # due dates, not start-after dates, and must not become deferred_until.
        if _is_due_context(text, m.start(), m.end()):
            continue
        try:
            dt = datetime(
                int(m.group("year")), int(m.group("month")),
                int(m.group("day")))
            if dt > now:
                matches.append((dt, "iso_date", m.group(0)))
        except ValueError:
            continue

    for m in MONTH_DAY_YEAR.finditer(text):
        if _is_due_context(text, m.start(), m.end()):
            continue
        mo = MONTHS.get(m.group("month").lower())
        if mo is None:
            continue
        try:
            dt = datetime(int(m.group("year")), mo, int(m.group("day")))
            if dt > now:
                matches.append((dt, "month_day_year", m.group(0)))
        except ValueError:
            continue

    for m in DAY_MONTH_YEAR.finditer(text):
        if _is_due_context(text, m.start(), m.end()):
            continue
        mo = MONTHS.get(m.group("month").lower())
        if mo is None:
            continue
        try:
            dt = datetime(int(m.group("year")), mo, int(m.group("day")))
            if dt > now:
                matches.append((dt, "day_month_year", m.group(0)))
        except ValueError:
            continue

    for m in RELATIVE_IN_N.finditer(text):
        n = int(m.group("n"))
        # Normalize plural so the lookup key always matches dict membership.
        # The RELATIVE_IN_N regex restricts unit to days?|weeks?|months?|
        # years?, and RELATIVE_UNITS holds both forms — so the normalized
        # key is ALWAYS in the dict. Do not add a fallback; an unreachable
        # fallback would hide typos in this regex during future edits.
        unit = m.group("unit").lower().rstrip("s") + ("s" if n != 1 else "")
        dt = now + timedelta(days=n * RELATIVE_UNITS[unit])
        matches.append((dt, "relative_in_n", m.group(0)))

    for m in RELATIVE_NEXT.finditer(text):
        # RELATIVE_NEXT regex is week|month|year — singulars only — and all
        # three are RELATIVE_UNITS keys. Direct lookup is total; do not add
        # a fallback (see RELATIVE_IN_N above for the reasoning).
        unit = m.group("unit").lower()
        dt = now + timedelta(days=RELATIVE_UNITS[unit])
        matches.append((dt, "relative_next", m.group(0)))

    for m in RELATIVE_TOMORROW.finditer(text):
        dt = now + timedelta(days=1)
        matches.append((dt, "relative_tomorrow", m.group(0)))

    if not matches:
        return {"matched": False, "deferred_until": None,
                "pattern": None, "match_text": None}

    # Earliest future date wins (most conservative defer).
    matches.sort(key=lambda x: x[0])
    dt, pattern, match_text = matches[0]
    return {
        "matched": True,
        "deferred_until": dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "pattern": pattern,
        "match_text": match_text,
    }
