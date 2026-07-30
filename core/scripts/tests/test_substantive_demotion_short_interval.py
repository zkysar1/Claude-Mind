"""test_substantive_demotion_short_interval.py -- .

Proves the interval-scoped exemption in goal-selector.apply_substantive_demotion,
and pins the config-allowlist trap that silently disabled it on first implementation.

WHY THIS EXISTS
---------------
FW-1's substantive_demotion caps a recurring goal's score to `margin` below the
best substantive candidate unless it is overdue past `overdue_exempt_ratio`. That
exemption is a PURE RATIO test with no absolute-time bound, so the shipped 5.0
means elapsed == 6x the interval: a 6h monitor stays demoted until 36h stale,
which is precisely a monitor whose entire value is timeliness being starved.

Measured 2026-07-29 over 38 live recurring candidates, and the reason the fix is
interval-scoped rather than the obvious flat absolute-hours OR: a flat ">=12h
overdue" bound exempts 24/38 (baseline 6/38) and ">=48h" still exempts 19/38 --
a 3-4x relaxation that re-opens the exact recurring domination FW-1 was built to
prevent. The interval-scoped form exempts 8/38 and cannot touch the 55 of 61
corpus recurring goals whose interval exceeds the threshold.

THE ALLOWLIST TRAP (test_new_keys_survive_config_load)
------------------------------------------------------
load_recurring_config() iterates `defaults.items()` as an ALLOWLIST. A key present
in aspirations.yaml but absent from that dict is discarded with no parse error and
no warning -- the YAML is valid, the load succeeds, and the setting evaporates.
On first implementation both new keys were added to aspirations.yaml but NOT to
`defaults`, so the feature was a silent no-op: the selector emitted the new field
correctly (proving the code had reloaded) while the exemption never fired. A
config key that does nothing is byte-identical to a config key that works but has
not yet triggered, which is why this is pinned by a test rather than left to
review.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "alpha")

gs = importlib.import_module("goal-selector")

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT


BASE_CONFIG = {
    "substantive_demotion_enabled": True,
    "substantive_demotion_margin": 0.5,
    "substantive_demotion_floor": 5.0,
    "substantive_demotion_overdue_exempt_ratio": 5.0,
    "substantive_demotion_short_interval_hours": 6.0,
    "substantive_demotion_short_interval_exempt_ratio": 1.0,
}


def _substantive(score=11.3):
    return {
        "goal_id": "g-sub-01",
        "recurring": False,
        "score": score,
        "raw": {"agent_executable": 2},
    }


def _recurring(goal_id, score, interval_hours, overdue_ratio):
    return {
        "goal_id": goal_id,
        "recurring": True,
        "score": score,
        "raw": {"agent_executable": 2},
        "recurring_interval_hours": interval_hours,
        "recurring_overdue_ratio": overdue_ratio,
    }


def _demoted(row):
    return bool((row.get("breakdown") or {}).get("substantive_demotion"))


def _run(rows, **overrides):
    config = dict(BASE_CONFIG)
    config.update(overrides)
    return gs.apply_substantive_demotion(rows, config)


# ── the exemption itself ──────────────────────────────────────────────────────

def test_short_interval_monitor_overdue_is_exempt():
    """A 6h monitor at 2x its interval stale is NOT demoted (the  case)."""
    mon = _recurring("g-mon-6h", 11.78, interval_hours=6.0, overdue_ratio=3.891)
    _run([_substantive(), mon])
    assert not _demoted(mon)
    assert mon["score"] == 11.78, "exempt goal's score must be left untouched"


def test_long_interval_goal_same_ratio_is_still_demoted():
    """Identical ratio, 40h interval -> still demoted. The fix must NOT relax the
    long-interval lane; that relaxation is what the flat absolute-hours variants
    got wrong (24/38 exempt vs baseline 6/38)."""
    long_goal = _recurring("g-long-40h", 11.78, interval_hours=40.0, overdue_ratio=3.891)
    _run([_substantive(), long_goal])
    assert _demoted(long_goal)
    assert long_goal["score"] == 10.8  # cap = 11.3 - 0.5


def test_short_interval_below_ratio_is_still_demoted():
    """Short interval alone does not exempt -- the goal must also be overdue."""
    fresh = _recurring("g-mon-fresh", 11.78, interval_hours=6.0, overdue_ratio=0.4)
    _run([_substantive(), fresh])
    assert _demoted(fresh)


def test_boundary_interval_is_inclusive():
    """interval == threshold is inside the monitor class (`<=`, not `<`)."""
    edge = _recurring("g-mon-edge", 11.78, interval_hours=6.0, overdue_ratio=1.0)
    _run([_substantive(), edge])
    assert not _demoted(edge)


def test_zero_interval_never_exempts():
    """A missing/zero interval must not satisfy the guard -- `0 < iv` is the
    reason the disabled case below is a no-op rather than a blanket exemption."""
    zero = _recurring("g-mon-zero", 11.78, interval_hours=0.0, overdue_ratio=9.0)
    _run([_substantive(), zero], substantive_demotion_overdue_exempt_ratio=99.0)
    assert _demoted(zero)


def test_disabled_by_zero_threshold_restores_prior_behavior():
    """short_interval_hours=0 -> `0 < iv <= 0` unsatisfiable -> pre-."""
    mon = _recurring("g-mon-6h", 11.78, interval_hours=6.0, overdue_ratio=3.891)
    _run([_substantive(), mon], substantive_demotion_short_interval_hours=0.0)
    assert _demoted(mon)


def test_original_ratio_exemption_still_applies():
    """The pre-existing exemption is untouched: a long-interval goal past
    overdue_exempt_ratio is still exempt regardless of the new clause."""
    stale = _recurring("g-long-stale", 11.78, interval_hours=40.0, overdue_ratio=7.0)
    _run([_substantive(), stale])
    assert not _demoted(stale)


# ── the allowlist trap ────────────────────────────────────────────────────────

def test_new_keys_survive_config_load():
    """load_recurring_config() must actually RETURN the new keys.

    This is the regression that bit on first implementation: both keys were in
    aspirations.yaml and neither reached RECURRING_CONFIG, because the loader
    treats its `defaults` dict as an allowlist. Nothing errored -- the feature
    was simply inert.
    """
    cfg = gs.load_recurring_config()
    for key in ("substantive_demotion_short_interval_hours",
                "substantive_demotion_short_interval_exempt_ratio"):
        assert key in cfg, f"{key} was dropped by the load_recurring_config allowlist"
        assert isinstance(cfg[key], float)


def test_no_substantive_demotion_key_is_silently_dropped():
    """Generalized allowlist-drift guard.

    Every `substantive_demotion_*` key in the shipped aspirations.yaml `recurring`
    section must survive load_recurring_config(). This catches the NEXT key added
    to the YAML without a matching `defaults` entry, not just the two added here.
    """
    asp_yaml = PROJECT_ROOT / "core" / "config" / "aspirations.yaml"
    recurring = (yaml.safe_load(asp_yaml.read_text(encoding="utf-8")) or {}).get("recurring", {})
    yaml_keys = {k for k in recurring if k.startswith("substantive_demotion_")}
    assert yaml_keys, "fixture guard: no substantive_demotion_* keys found in aspirations.yaml"

    cfg = gs.load_recurring_config()
    dropped = sorted(k for k in yaml_keys if k not in cfg)
    assert not dropped, (
        "keys present in aspirations.yaml but silently dropped by the "
        f"load_recurring_config() allowlist: {dropped}"
    )
