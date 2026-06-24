"""Value-framing resolver (FW-5 / R2, ). Loads
core/config/value-framing-mapping.yaml once per process and returns a positive,
one-line framing string for a given (outcome_class, work_class) pair.

WHY (`.claude/rules/learning-philosophy.md` Recognition half / FW-5): the
`outcome_class=routine` label reads as "did not count" and demoralizes, even
though a routine sweep that returns clean is often the reason a regression did
not ship. The reframe already exists in PROSE; this helper is the DERIVED,
presentation-only structural label, surfaced where routine is RECORDED
(journal-append.sh `Value:` line; agent-completion-report Contribution).

DERIVED-AT-PRESENTATION, NEVER STORED. This sibling of `_work_class.py` adds NO
enum and touches NO stored field — it only re-presents the existing
(outcome_class, work_class) pair as an affirming sentence. outcome_class stays
the routine|deep ENUM that recurring-close.sh / cargo-cult-detector.py /
goal-selector.py / recurring-loop-state-mutate.py switch on (guard-541). Zero
blast radius by construction.

Fail-open: a missing mapping file, unparseable YAML, missing PyYAML, an unknown
outcome_class, or an unknown/empty work_class all resolve to a sensible framing
(the outcome_class's `unclassified` entry, or the top-level `default`). The
caller (a presentation surface) must never break on a framing lookup.

Unlike `_work_class.py` there is NO world overlay: framing strings are
framework-universal presentation prose, not domain-specific category routing.
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

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "value-framing-mapping.yaml"

_DEFAULT_FRAMING = "Work recorded: every closed goal moves the loop forward."


@lru_cache(maxsize=1)
def _load() -> tuple[dict, str]:
    """Return (mapping, default). Fail-open on any error.

    mapping shape: {outcome_class: {work_class: framing, ...}, ...}
    """
    if yaml is None:
        return {}, _DEFAULT_FRAMING
    if not _CONFIG_PATH.is_file():
        return {}, _DEFAULT_FRAMING
    try:
        data = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}, _DEFAULT_FRAMING
    mapping = data.get("mapping") or {}
    if not isinstance(mapping, dict):
        mapping = {}
    default = data.get("default") or _DEFAULT_FRAMING
    if not isinstance(default, str):
        default = _DEFAULT_FRAMING
    return mapping, default


def resolve(outcome_class: Optional[str], work_class: Optional[str]) -> str:
    """Resolve (outcome_class, work_class) to a one-line value framing.

    - Unknown/missing outcome_class -> top-level default.
    - Known outcome_class but unknown/empty work_class -> that outcome_class's
      `unclassified` framing (or the top-level default if absent).
    """
    mapping, default = _load()
    if not outcome_class:
        return default
    oc_map = mapping.get(outcome_class)
    if not isinstance(oc_map, dict):
        return default
    if work_class and work_class in oc_map:
        return oc_map[work_class]
    # Empty / unmapped work_class -> the outcome_class's catch-all framing.
    return oc_map.get("unclassified", default)


def main():
    """CLI: print the value framing for (outcome_class, work_class) on stdout.

    Usage: python _value_framing.py <outcome_class> [work_class]
    Exit 0 always (fail-open). Missing args print the default framing.
    """
    oc = sys.argv[1] if len(sys.argv) >= 2 else None
    wc = sys.argv[2] if len(sys.argv) >= 3 else None
    print(resolve(oc, wc))


if __name__ == "__main__":
    main()
