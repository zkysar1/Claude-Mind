"""Scaffolded-exploration gate logic — daemon-safe extraction (PR 7c/2).

Apply: goals in product categories MUST cite an Investigate precursor via
the `discovered_by` field — otherwise the agent skips straight to building
without first investigating. Operationalizes the 96% coverage gap surfaced
in `alpha/reports/scaffolded-exploration-pattern-2026-05-09.md`
(g-115-562): only 4 of 165 completed Investigates had a discovered_by
linkage to a follow-up Apply.

Public API:
    evaluate(goal, *, override_no_investigate=None,
             product_category_prefixes=DEFAULT_PRODUCT_PREFIXES) -> dict

Return shape:
    {
      "would_block": bool,
      "reason": str,
      "matched_category_prefix": str | None,
      "override_applied": str | None,
    }

Telemetry (`_gate_log`) is emitted INSIDE evaluate() so daemon callers get
parity with the CLI invocation. The legacy inline check in aspirations.py
emitted no telemetry — this is a NEW behavior in 7c/2 and matches the
sibling gates' shape (origin-signal, goal-duplication, etc.).

NOTE on `product_category_prefixes`: domain-specific prefixes live in
`world/config/scaffolded-exploration.yaml` (key `product_category_prefixes`)
and are loaded via `_world_config.load_world_config(...)`. The legacy
hardcoded tuple ("npc-", "ayoai-", "processor-", "intelligence-") was moved
to the the framework world overlay on 2026-05-18 (Phase 2.5 of packaging plan). The
core default is now an empty tuple — a fresh deployment without an overlay
file gets a gate that never fires on category, which is the safe-default
posture. Callers may still pass `product_category_prefixes=` explicitly to
override the world overlay for testing.

Daemon safety:
  - Reads no env directly.
  - File I/O: world/config overlay read at first call per process (cached
    in _world_config._CACHE); errors swallowed → empty defaults.
  - No override audit ledger (the legacy inline check has none either;
    overrides are only echoed to stderr by the caller).
"""
from __future__ import annotations

from typing import Optional, Tuple

from _gate_log import log as _gate_log  # type: ignore
from _world_config import load_world_config as _load_world_config  # type: ignore


def _default_product_prefixes() -> Tuple[str, ...]:
    """Lazy accessor for product-category prefixes from world overlay.
    Safe-empty default — fresh deployment without overlay returns ()."""
    cfg = _load_world_config(
        "scaffolded-exploration",
        default={"product_category_prefixes": []},
    )
    prefixes = cfg.get("product_category_prefixes") or []
    return tuple(p for p in prefixes if isinstance(p, str))


# Back-compat: resolved-once snapshot for callers that import the constant.
# Refresh via process restart (daemon recycles on post-commit) or by calling
# _world_config.clear_cache("scaffolded-exploration") + re-import.
DEFAULT_PRODUCT_PREFIXES: Tuple[str, ...] = _default_product_prefixes()

# Title prefix that flags "this is a build/apply goal" — the same prefix
# the loop's CREATE_BLOCKER protocol uses for Apply: goals. Hardcoded
# because changing it would be a framework-wide convention change.
_APPLY_PREFIX = "Apply:"


def evaluate(
    goal: dict,
    *,
    override_no_investigate: Optional[str] = None,
    product_category_prefixes: Tuple[str, ...] = DEFAULT_PRODUCT_PREFIXES,
) -> dict:
    """Run the gate. Returns a dict the caller renders to JSON/stderr.

    Args:
        goal: The goal dict being filed. Reads `title`, `category`,
            `discovered_by`.
        override_no_investigate: Justification string. When non-empty AND
            the gate would have blocked, sets override_applied and returns
            would_block=False.
        product_category_prefixes: Tuple of category prefixes that trigger
            the gate. Default preserves the legacy hardcoded list.

    Side effects: single _gate_log() record per call.
    """
    title = (goal or {}).get("title", "") or ""
    category = (goal or {}).get("category", "") or ""
    discovered_by = (goal or {}).get("discovered_by")

    # Skip 1: not an Apply: goal → gate doesn't apply.
    if not title.startswith(_APPLY_PREFIX):
        out = {
            "would_block": False,
            "reason": "not an Apply: goal — gate does not apply",
            "matched_category_prefix": None,
            "override_applied": None,
        }
        _emit_telemetry("pass", out, category)
        return out

    # Skip 2: not a product category → gate doesn't apply.
    matched_prefix = next(
        (p for p in product_category_prefixes if category.startswith(p)),
        None,
    )
    if matched_prefix is None:
        out = {
            "would_block": False,
            "reason": (
                f"Apply: goal but category {category!r} is not in product "
                f"prefixes {list(product_category_prefixes)} — gate does not apply"
            ),
            "matched_category_prefix": None,
            "override_applied": None,
        }
        _emit_telemetry("pass", out, category)
        return out

    # Skip 3: has discovered_by → Investigate precursor cited → pass.
    if discovered_by:
        out = {
            "would_block": False,
            "reason": f"discovered_by cited ({discovered_by!r}) — Investigate precursor present",
            "matched_category_prefix": matched_prefix,
            "override_applied": None,
        }
        _emit_telemetry("pass", out, category)
        return out

    # Block (or override): Apply: goal in product category, no discovered_by.
    if override_no_investigate:
        out = {
            "would_block": False,
            "reason": (
                f"override applied — Apply: goal in product category "
                f"{category!r} without discovered_by"
            ),
            "matched_category_prefix": matched_prefix,
            "override_applied": override_no_investigate,
        }
        _emit_telemetry("override", out, category)
        return out

    out = {
        "would_block": True,
        "reason": (
            f"Apply goal in product category {category!r} must cite an "
            f"Investigate precursor via discovered_by, OR pass "
            f"--override-no-investigate \"<justification>\". "
            f"See world/conventions/scaffolded-exploration.md for the "
            f"trigger conditions and Investigate-precursor shape."
        ),
        "matched_category_prefix": matched_prefix,
        "override_applied": None,
    }
    _emit_telemetry("block", out, category)
    return out


def _emit_telemetry(decision: str, out: dict, category: str) -> None:
    """Best-effort — never raises."""
    try:
        _gate_log(
            "scaffolded-exploration-gate",
            decision,
            payload={
                "category": category,
                "matched_category_prefix": out.get("matched_category_prefix"),
                "override_applied": out.get("override_applied"),
            },
        )
    except Exception:
        pass
