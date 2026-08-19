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


def sort_universal_rbs(entries, counters=None):
    """Sort a list of universal entries by utilization_score desc, then created desc.

    Mutates and returns the list. Tie-break by recency ensures fresh lessons
    surface over old ones at equal utility.

    `counters` (g-358-05) is the reasoning-bank utilization sidecar map; None
    keeps the embedded-field reading, so every existing caller is unchanged.

    THE IMPORT IS LAZY AND THE MODULE STAYS IMPORT-FREE AT TOP LEVEL — this is
    the constraint `mind_api/src/world/reasoning_bank.py::_store_paths` measured
    and documented, not a style choice. That module imports THIS one at daemon
    module level (its own header notes `_rb_helpers` has no imports at all), and
    `_utilization_store` resolves WORLD_DIR from `local-paths.conf` at import.
    A top-level import here would therefore freeze a global at daemon LOAD in a
    process whose path contract is per-request `ctx.paths`. Deferring keeps that
    side effect out of module load, and `sys.modules` makes the repeat cost nil.
    Note the frozen global is never consulted either way: `utilization_of` takes
    the counters map explicitly and needs no world_dir — the same "pass it
    explicitly" discipline `_store_paths` applies to `world`.

    On ImportError the embedded field is used, which is exactly today's
    behaviour — the conservative direction, never a silent empty.
    """
    try:
        from _utilization_store import utilization_of
    except ImportError:
        def utilization_of(rec, _c=None):
            return rec.get("utilization") or {}

    entries.sort(
        key=lambda r: (
            (utilization_of(r, counters) or {}).get("utilization_score", 0) or 0,
            r.get("created", "") or "",
        ),
        reverse=True,
    )
    return entries
