"""Cognitive-primitive title prefix registry ().

Single source of truth for the cognitive-primitive prefix set described in
CLAUDE.md "Cognitive Primitives" section.  Different consumers need different
views of the same canonical data — putting them in one module prevents the
cross-file invariant rb-657 documented (precheck-eval.py:534 vs.
backfill-origin-signal.py:57 each carrying their own subset tuple).

Schema (per row):
    prefix          : "Apply:", "Unblock:", … — case-sensitive title token
    kind            : "apply", "unblock", … — origin_signal kind label
    semantic_class  : "cognitive_primitive" | "tactical_correction" | "meta_diagnostic"

Three views are exposed:

* ``PRIMITIVE_PREFIXES``  — full case-sensitive tuple of all 6 prefixes;
  used by precheck-eval.py for "all primitive goals?" detection.
* ``SIGNAL_KIND_PRIMITIVES`` — tuple of (lower_prefix, kind) for the 4
  primitives that map to an origin_signal kind; used by
  backfill-origin-signal.py.  The "Apply:" and "Batch:" prefixes are not
  origin-signal kinds — they have their own gate-allowed origin_signal
  prefixes ("decomposition:", "investigate:" etc.) so they are excluded
  from this view.
* ``starts_with_any_primitive(title)`` / ``signal_kind_for_title(title)``
  — convenience helpers so consumers don't re-implement the same
  startswith logic.

Adding a new primitive:
    1. Append a row to ``_REGISTRY``.
    2. Decide whether the new prefix is an origin_signal kind — if so it
       lands in ``SIGNAL_KIND_PRIMITIVES`` automatically (any row with
       ``semantic_class == "cognitive_primitive"`` is included).
    3. Update CLAUDE.md "Cognitive Primitives" section so the registry and
       the doc stay in sync.

This is a leaf module: imports only ``typing``.  Importable from any
core/scripts/*.py that needs primitive-prefix logic.
"""
from __future__ import annotations

from typing import Optional, Tuple


# Each row: (prefix, kind, semantic_class).
#
# semantic_class drives which view a row appears in. Verified against
# origin-signal-gate.py allowed prefixes (2026-05-06):
#   - cognitive_primitive  → in both PRIMITIVE_PREFIXES and SIGNAL_KIND_PRIMITIVES.
#                           Gate accepts the matching kind: prefix as a valid
#                           origin_signal (unblock:, idea:, investigate:, maintain:).
#   - tactical_correction  → in PRIMITIVE_PREFIXES only.  Apply: titles land as
#                           "decomposition:..." origin_signals (set at filing
#                           time by aspirations-spark Phase 6.5 handoff path);
#                           the gate REJECTS "apply:" as a top-level kind.
#   - meta_diagnostic      → in PRIMITIVE_PREFIXES only.  Batch: titles come
#                           from cargo-cult-detector consolidation and carry
#                           "drift_detected:..." origin_signals; the gate
#                           REJECTS "batch:" as a top-level kind.
_REGISTRY: Tuple[Tuple[str, str, str], ...] = (
    ("Apply:",       "apply",       "tactical_correction"),
    ("Unblock:",     "unblock",     "cognitive_primitive"),
    ("Maintain:",    "maintain",    "cognitive_primitive"),
    ("Idea:",        "idea",        "cognitive_primitive"),
    ("Investigate:", "investigate", "cognitive_primitive"),
    ("Batch:",       "batch",       "meta_diagnostic"),
)


PRIMITIVE_PREFIXES: Tuple[str, ...] = tuple(p for p, _, _ in _REGISTRY)


# Lower-cased prefix → kind mapping for origin_signal inference.  Only
# rows whose semantic_class is "cognitive_primitive" qualify — the other
# classes have their own origin_signal categories.  Order matches the
# registry (so adding a new cognitive_primitive row preserves the lookup
# order in backfill-origin-signal.py).
SIGNAL_KIND_PRIMITIVES: Tuple[Tuple[str, str], ...] = tuple(
    (prefix.lower(), kind)
    for prefix, kind, sclass in _REGISTRY
    if sclass == "cognitive_primitive"
)


def starts_with_any_primitive(title: Optional[str]) -> bool:
    """Return True if ``title`` (case-sensitive) starts with any primitive prefix.

    Mirrors the precheck-eval.py invariant: a goal title is considered a
    cognitive primitive iff it starts with one of the 6 prefixes exactly as
    spelled in CLAUDE.md.
    """
    if not title:
        return False
    return title.strip().startswith(PRIMITIVE_PREFIXES)


def signal_kind_for_title(title: Optional[str]) -> Optional[str]:
    """Return the origin_signal kind label for a primitive title, else None.

    Case-insensitive — matches backfill-origin-signal.py semantics where the
    title is lowered before prefix comparison so legacy goals with mixed-case
    prefixes still backfill correctly.
    """
    if not title:
        return None
    low = title.strip().lower()
    for prefix, kind in SIGNAL_KIND_PRIMITIVES:
        if low.startswith(prefix):
            return kind
    return None
