"""Work-class resolver. Loads core/config/work-class-mapping.yaml AND
world/config/work-class-mapping.yaml (overlay) once per process and returns
the work_class for a given category string.

Consumed by aspirations.py (new-goal writer), backfill-work-class.py
(one-shot), and iteration-close.sh (session_completions append via a
`--resolve <category>` subprocess call).

Fail-open: missing mapping files, unparseable YAML, or unmapped categories
all resolve to the configured `default` (default: "unclassified").
Criterion 7e in goal-selector.py and self-drift-gate.py both exclude
"unclassified" from the denominator, so an unmapped category never
poisons the class-balance computation — it just contributes nothing.

Overlay merge: world overrides core per-key. A fresh deployment without a
world overlay gets full framework-universal coverage from core alone.
Packaging plan Phase 2.6 (2026-05-18) split the mapping so domain-specific
categories (npc-*, ayoai-*, roblox-*, etc.) live in world/.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "work-class-mapping.yaml"

# Use _world_config helper for the overlay — same pattern as Phase 2.5 tables.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _world_config import load_world_config as _load_world_config  # noqa: E402


@lru_cache(maxsize=1)
def _load() -> tuple[dict, str]:
    """Return (merged_mapping, default). Fail-open on any error.

    Merge order: core first (framework-universal); world overlay second
    (per-key override). Caller never sees the split — `mapping[k]` returns
    whichever store last claimed the key.
    """
    default = "unclassified"
    if yaml is None:
        return {}, default

    core_mapping: dict = {}
    if _CONFIG_PATH.is_file():
        try:
            data = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
            m = data.get("mapping") or {}
            d = data.get("default") or default
            if isinstance(m, dict):
                core_mapping = m
            default = d
        except Exception:
            pass

    overlay = _load_world_config("work-class-mapping", default={"mapping": {}})
    world_mapping = overlay.get("mapping") or {}
    if not isinstance(world_mapping, dict):
        world_mapping = {}

    merged = dict(core_mapping)
    merged.update(world_mapping)
    return merged, default


def resolve(category: Optional[str]) -> str:
    """Resolve a category string to its work_class.

    Returns the configured default (typically "unclassified") for missing,
    empty, or unmapped categories.
    """
    if not category:
        mapping, default = _load()
        return default
    mapping, default = _load()
    return mapping.get(category, default)


def main():
    """CLI: print the work_class for an input category on stdout.

    Usage: python _work_class.py <category>
    Exit 0 always (fail-open). Missing category prints the default.
    """
    if len(sys.argv) < 2:
        print(resolve(None))
        return
    print(resolve(sys.argv[1]))


if __name__ == "__main__":
    main()
