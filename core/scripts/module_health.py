#!/usr/bin/env python3
"""module_health.py -- M-6 per-module success-rate tracking.

Reads and writes world/module-health.yaml.  Called by utilization-feedback.py
after each goal's feedback pass to update per-category and per-store health
metrics.

Implements the ModuleHealth schema from perception-module.md Section 8.

A "module" maps to a retrieval category (tree node top-level key, e.g.
"data-engineering") or a supplementary store type ("reasoning_bank",
"guardrails", "pattern_signatures").

Storage is world-scoped (not session-scoped) so health metrics survive
across sessions and accumulate into meaningful aggregates.

Functions:
    load_module_health(world_dir)  -- load world/module-health.yaml
    save_module_health(world_dir, data)  -- atomic write
    record_invocation(health, module_id, outcome)  -- update counters
    check_reconsolidation(health, module_id)  -- reconsolidation trigger
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    raise SystemExit("PyYAML required: pip install pyyaml")


# Reconsolidation thresholds -- differentiated by module type.
# Tree categories trigger sooner (fewer invocations needed) at a higher
# success-rate floor.  Supplementary stores tolerate lower success rates
# because supplementary matching is fuzzier by nature.
DEFAULT_TREE_THRESHOLD = 0.4
DEFAULT_TREE_MIN_INVOCATIONS = 50
DEFAULT_SUPP_THRESHOLD = 0.3
DEFAULT_SUPP_MIN_INVOCATIONS = 100

# Module IDs that represent supplementary stores (not tree categories).
SUPPLEMENTARY_MODULES = {"reasoning_bank", "guardrails", "pattern_signatures"}

HEALTH_FILENAME = "module-health.yaml"


def _empty_module() -> dict[str, Any]:
    """Return the default counters for a new module entry."""
    return {
        "total_invocations": 0,
        "successful": 0,
        "failed": 0,
        "null_returns": 0,
        "success_rate": 0.0,
        "avg_latency_ms": 0.0,  # placeholder -- latency tracking deferred to M-9
    }


def load_module_health(world_dir: Path) -> dict[str, Any]:
    """Load world/module-health.yaml, return dict.  Creates empty if missing."""
    p = Path(world_dir) / HEALTH_FILENAME
    if not p.exists():
        return {"modules": {}}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return {"modules": {}}
    if not isinstance(data, dict):
        return {"modules": {}}
    if "modules" not in data or not isinstance(data["modules"], dict):
        data["modules"] = {}
    return data


def save_module_health(world_dir: Path, data: dict[str, Any]) -> None:
    """Atomic write world/module-health.yaml (tmp+rename pattern)."""
    p = Path(world_dir) / HEALTH_FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(p) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False,
                  allow_unicode=True, width=200)
    os.replace(str(tmp), str(p))


def record_invocation(health: dict[str, Any], module_id: str,
                      outcome: str) -> None:
    """Record one invocation for *module_id*.

    Parameters
    ----------
    health : dict
        The top-level health dict (contains ``modules`` key).
    module_id : str
        Tree category slug (e.g. ``"data-engineering"``) or supplementary
        store type (``"reasoning_bank"``).
    outcome : str
        One of ``"helpful"`` | ``"noise"`` | ``"null"`` | ``"unknown"``.

    Updates ``total_invocations``, ``successful`` / ``failed`` /
    ``null_returns``, and recomputes ``success_rate``.  Unknown outcomes
    increment ``total_invocations`` only (no signal -- same philosophy as
    utilization-feedback.py ``--all-unknown``).
    """
    modules = health.setdefault("modules", {})
    if module_id not in modules:
        modules[module_id] = _empty_module()
    m = modules[module_id]

    m["total_invocations"] = m.get("total_invocations", 0) + 1

    if outcome == "helpful":
        m["successful"] = m.get("successful", 0) + 1
    elif outcome == "noise":
        m["failed"] = m.get("failed", 0) + 1
    elif outcome == "null":
        m["null_returns"] = m.get("null_returns", 0) + 1
    # "unknown" increments total only -- no signal.

    # Recompute success_rate (guard against zero division).
    total = m["total_invocations"]
    m["success_rate"] = round(m["successful"] / total, 4) if total > 0 else 0.0


def check_reconsolidation(health: dict[str, Any], module_id: str,
                          *, tree_threshold: float | None = None,
                          tree_min: int | None = None,
                          supp_threshold: float | None = None,
                          supp_min: int | None = None) -> bool:
    """Return ``True`` if *module_id* has crossed its reconsolidation trigger.

    Differentiated thresholds: tree categories use ``(tree_threshold,
    tree_min)``; supplementary stores use ``(supp_threshold, supp_min)``.
    Callers may override via keyword args or rely on module-level defaults.
    """
    modules = health.get("modules", {})
    m = modules.get(module_id)
    if not m:
        return False

    is_supp = module_id in SUPPLEMENTARY_MODULES
    if is_supp:
        threshold = supp_threshold if supp_threshold is not None else DEFAULT_SUPP_THRESHOLD
        min_inv = supp_min if supp_min is not None else DEFAULT_SUPP_MIN_INVOCATIONS
    else:
        threshold = tree_threshold if tree_threshold is not None else DEFAULT_TREE_THRESHOLD
        min_inv = tree_min if tree_min is not None else DEFAULT_TREE_MIN_INVOCATIONS

    total = m.get("total_invocations", 0)
    if total < min_inv:
        return False

    success_rate = m.get("success_rate", 0.0)
    return success_rate < threshold
