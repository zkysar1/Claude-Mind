"""Shared reasoning-bank helpers.

Small module used by both `reasoning-bank.py` (query modes) and `retrieve.py`
(partitioning universal vs. domain entries in the unified retrieval output).

Universal entry rule (for cross-domain surfacing):
  applies_to in {any, framework}  OR  category starts with "framework-"

The category-prefix auto-escalation means existing framework-maintenance,
framework-patterns, framework-engineering, framework-architecture entries
qualify as universal without needing an explicit applies_to field — no backfill
required. New entries can set applies_to: any to escalate entries with
non-framework categories, or applies_to: specific to opt a framework-category
entry OUT of cross-domain surfacing.
"""

FRAMEWORK_CATEGORY_PREFIX = "framework-"
UNIVERSAL_APPLIES_TO_VALUES = {"any", "framework"}


def is_universal_rb(rec):
    """True if the reasoning-bank entry is a universal meta-lesson.

    Auto-escalates framework-* category entries even without an explicit
    applies_to value. A record with applies_to="specific" is never universal,
    even if its category starts with "framework-" (explicit opt-out).
    """
    if not isinstance(rec, dict):
        return False
    applies = rec.get("applies_to")
    if applies == "specific":
        return False
    if applies in UNIVERSAL_APPLIES_TO_VALUES:
        return True
    cat = rec.get("category", "") or ""
    return cat.startswith(FRAMEWORK_CATEGORY_PREFIX)


def sort_universal_rbs(entries):
    """Sort a list of universal entries by utilization_score desc, then created desc.

    Mutates and returns the list. Tie-break by recency ensures fresh lessons
    surface over old ones at equal utility.
    """
    entries.sort(
        key=lambda r: (
            (r.get("utilization") or {}).get("utilization_score", 0) or 0,
            r.get("created", "") or "",
        ),
        reverse=True,
    )
    return entries
