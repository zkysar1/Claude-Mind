"""test_module_health.py -- M-6 per-module success-rate tracking.

Covers:
  - record_invocation increments correct counters for helpful/noise/null/unknown
  - success_rate computed correctly (successful / total)
  - success_rate is 0.0 when total_invocations is 0 (no div-by-zero)
  - check_reconsolidation returns False below min_invocations
  - check_reconsolidation returns True when success_rate < threshold above min_invocations
  - check_reconsolidation returns False when success_rate >= threshold
  - Differentiated thresholds: tree (0.4, 50) vs supplementary (0.3, 100)
  - load/save round-trip (tempdir, no real world/ writes)
  - New module auto-created on first invocation
  - Custom threshold overrides via keyword args
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# Bootstrap a clean env so _paths imports succeed without real agent binding.
_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")

_TMPDIR = tempfile.mkdtemp(prefix="module-health-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)

from module_health import (  # noqa: E402
    _empty_module,
    check_reconsolidation,
    load_module_health,
    record_invocation,
    save_module_health,
    DEFAULT_SUPP_MIN_INVOCATIONS,
    DEFAULT_SUPP_THRESHOLD,
    DEFAULT_TREE_MIN_INVOCATIONS,
    DEFAULT_TREE_THRESHOLD,
    SUPPLEMENTARY_MODULES,
)

# Restore env so downstream tests inherit clean conftest defaults.
if _ORIG_MIND_WORLD is not None:
    os.environ["MIND_WORLD"] = _ORIG_MIND_WORLD
elif "MIND_WORLD" in os.environ:
    del os.environ["MIND_WORLD"]
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT


def _fresh_health():
    """Return a health dict with no modules."""
    return {"modules": {}}


# ---------------------------------------------------------------------------
# record_invocation: counter correctness
# ---------------------------------------------------------------------------

def test_record_helpful_increments():
    health = _fresh_health()
    record_invocation(health, "data-engineering", "helpful")
    m = health["modules"]["data-engineering"]
    assert m["total_invocations"] == 1
    assert m["successful"] == 1
    assert m["failed"] == 0
    assert m["null_returns"] == 0


def test_record_noise_increments():
    health = _fresh_health()
    record_invocation(health, "data-engineering", "noise")
    m = health["modules"]["data-engineering"]
    assert m["total_invocations"] == 1
    assert m["failed"] == 1
    assert m["successful"] == 0
    assert m["null_returns"] == 0


def test_record_null_increments():
    health = _fresh_health()
    record_invocation(health, "data-engineering", "null")
    m = health["modules"]["data-engineering"]
    assert m["total_invocations"] == 1
    assert m["null_returns"] == 1
    assert m["successful"] == 0
    assert m["failed"] == 0


def test_record_unknown_increments_total_only():
    health = _fresh_health()
    record_invocation(health, "data-engineering", "unknown")
    m = health["modules"]["data-engineering"]
    assert m["total_invocations"] == 1
    assert m["successful"] == 0
    assert m["failed"] == 0
    assert m["null_returns"] == 0


# ---------------------------------------------------------------------------
# success_rate computation
# ---------------------------------------------------------------------------

def test_success_rate_computed():
    """3 helpful + 2 noise = success_rate 0.6."""
    health = _fresh_health()
    for _ in range(3):
        record_invocation(health, "cat-a", "helpful")
    for _ in range(2):
        record_invocation(health, "cat-a", "noise")
    m = health["modules"]["cat-a"]
    assert m["total_invocations"] == 5
    assert m["successful"] == 3
    assert m["failed"] == 2
    assert m["success_rate"] == 0.6


def test_success_rate_zero_total():
    """No invocations -> success_rate 0.0, no ZeroDivisionError."""
    health = _fresh_health()
    health["modules"]["cat-b"] = _empty_module()
    m = health["modules"]["cat-b"]
    assert m["total_invocations"] == 0
    assert m["success_rate"] == 0.0


def test_success_rate_all_noise():
    """All noise -> success_rate 0.0."""
    health = _fresh_health()
    for _ in range(5):
        record_invocation(health, "cat-c", "noise")
    m = health["modules"]["cat-c"]
    assert m["success_rate"] == 0.0


def test_success_rate_all_helpful():
    """All helpful -> success_rate 1.0."""
    health = _fresh_health()
    for _ in range(4):
        record_invocation(health, "cat-d", "helpful")
    m = health["modules"]["cat-d"]
    assert m["success_rate"] == 1.0


def test_success_rate_with_unknown_and_null():
    """Unknown and null invocations affect total but not successful count.
    2 helpful + 1 unknown + 1 null = total 4, successful 2, rate 0.5."""
    health = _fresh_health()
    record_invocation(health, "cat-e", "helpful")
    record_invocation(health, "cat-e", "helpful")
    record_invocation(health, "cat-e", "unknown")
    record_invocation(health, "cat-e", "null")
    m = health["modules"]["cat-e"]
    assert m["total_invocations"] == 4
    assert m["successful"] == 2
    assert m["success_rate"] == 0.5


# ---------------------------------------------------------------------------
# check_reconsolidation: threshold logic
# ---------------------------------------------------------------------------

def test_reconsolidation_below_min():
    """10 invocations with bad rate, but under 50 min -> False."""
    health = _fresh_health()
    for _ in range(10):
        record_invocation(health, "cat-f", "noise")
    assert not check_reconsolidation(health, "cat-f")


def test_reconsolidation_triggered_tree():
    """60 invocations, success_rate=0.3 -> True for tree (threshold 0.4)."""
    health = _fresh_health()
    for _ in range(18):
        record_invocation(health, "cat-g", "helpful")
    for _ in range(42):
        record_invocation(health, "cat-g", "noise")
    m = health["modules"]["cat-g"]
    assert m["total_invocations"] == 60
    assert m["success_rate"] == 0.3
    assert check_reconsolidation(health, "cat-g")


def test_reconsolidation_not_triggered_healthy():
    """60 invocations, success_rate=0.8 -> False."""
    health = _fresh_health()
    for _ in range(48):
        record_invocation(health, "cat-h", "helpful")
    for _ in range(12):
        record_invocation(health, "cat-h", "noise")
    m = health["modules"]["cat-h"]
    assert m["success_rate"] == 0.8
    assert not check_reconsolidation(health, "cat-h")


def test_supp_differentiated_threshold():
    """60 invocations, success_rate=0.35.
    -> False for supp (threshold 0.3, min 100: under min).
    -> True for tree (threshold 0.4, min 50: above min, below threshold)."""
    health = _fresh_health()
    # Tree module -- 60 invocations, 21 helpful = 0.35
    for _ in range(21):
        record_invocation(health, "tree-cat", "helpful")
    for _ in range(39):
        record_invocation(health, "tree-cat", "noise")
    assert health["modules"]["tree-cat"]["success_rate"] == 0.35
    assert check_reconsolidation(health, "tree-cat")  # tree: 60 >= 50, 0.35 < 0.4

    # Supplementary module -- same data shape but ID is in SUPPLEMENTARY_MODULES
    for _ in range(21):
        record_invocation(health, "reasoning_bank", "helpful")
    for _ in range(39):
        record_invocation(health, "reasoning_bank", "noise")
    assert health["modules"]["reasoning_bank"]["success_rate"] == 0.35
    # supp: 60 < 100 min_invocations -> False (not enough data)
    assert not check_reconsolidation(health, "reasoning_bank")


def test_supp_reconsolidation_above_min():
    """Supplementary module with 120 invocations, success_rate=0.25 -> True."""
    health = _fresh_health()
    for _ in range(30):
        record_invocation(health, "guardrails", "helpful")
    for _ in range(90):
        record_invocation(health, "guardrails", "noise")
    m = health["modules"]["guardrails"]
    assert m["total_invocations"] == 120
    assert m["success_rate"] == 0.25
    assert check_reconsolidation(health, "guardrails")


def test_supp_healthy_above_min():
    """Supplementary module with 120 invocations, success_rate=0.5 -> False."""
    health = _fresh_health()
    for _ in range(60):
        record_invocation(health, "pattern_signatures", "helpful")
    for _ in range(60):
        record_invocation(health, "pattern_signatures", "noise")
    m = health["modules"]["pattern_signatures"]
    assert m["success_rate"] == 0.5
    assert not check_reconsolidation(health, "pattern_signatures")


def test_reconsolidation_nonexistent_module():
    """Module not in health -> False (no data, no trigger)."""
    health = _fresh_health()
    assert not check_reconsolidation(health, "does-not-exist")


def test_reconsolidation_custom_thresholds():
    """Override thresholds via keyword args."""
    health = _fresh_health()
    for _ in range(10):
        record_invocation(health, "small-cat", "noise")
    # Default: 10 < 50 min -> False
    assert not check_reconsolidation(health, "small-cat")
    # Custom min=5: 10 >= 5, rate 0.0 < 0.4 -> True
    assert check_reconsolidation(health, "small-cat", tree_min=5)


def test_reconsolidation_custom_supp_thresholds():
    """Override supp thresholds via keyword args."""
    health = _fresh_health()
    for _ in range(60):
        record_invocation(health, "reasoning_bank", "noise")
    # Default supp: 60 < 100 min -> False
    assert not check_reconsolidation(health, "reasoning_bank")
    # Custom supp_min=50: 60 >= 50, rate 0.0 < 0.3 -> True
    assert check_reconsolidation(health, "reasoning_bank", supp_min=50)


# ---------------------------------------------------------------------------
# load/save round-trip
# ---------------------------------------------------------------------------

def test_round_trip_yaml():
    """Write to tmpdir, read back, verify data matches."""
    tmpdir = Path(tempfile.mkdtemp(prefix="mh-roundtrip-"))
    health = _fresh_health()
    record_invocation(health, "cat-rt", "helpful")
    record_invocation(health, "cat-rt", "helpful")
    record_invocation(health, "cat-rt", "noise")
    save_module_health(tmpdir, health)

    loaded = load_module_health(tmpdir)
    m = loaded["modules"]["cat-rt"]
    assert m["total_invocations"] == 3
    assert m["successful"] == 2
    assert m["failed"] == 1
    assert m["success_rate"] == round(2 / 3, 4)


def test_load_missing_file():
    """Load from directory with no health file -> empty modules."""
    tmpdir = Path(tempfile.mkdtemp(prefix="mh-missing-"))
    loaded = load_module_health(tmpdir)
    assert loaded == {"modules": {}}


def test_load_corrupt_file():
    """Load from directory with corrupt YAML -> empty modules."""
    tmpdir = Path(tempfile.mkdtemp(prefix="mh-corrupt-"))
    p = tmpdir / "module-health.yaml"
    p.write_text("not: [valid: yaml: {{", encoding="utf-8")
    loaded = load_module_health(tmpdir)
    assert loaded == {"modules": {}}


# ---------------------------------------------------------------------------
# New module auto-creation
# ---------------------------------------------------------------------------

def test_new_module_auto_created():
    """record_invocation for a module not yet in health -> creates entry."""
    health = _fresh_health()
    assert "brand-new" not in health["modules"]
    record_invocation(health, "brand-new", "helpful")
    assert "brand-new" in health["modules"]
    m = health["modules"]["brand-new"]
    assert m["total_invocations"] == 1
    assert m["successful"] == 1
    assert m["success_rate"] == 1.0


def test_multiple_modules_independent():
    """Invocations on different modules don't cross-contaminate."""
    health = _fresh_health()
    record_invocation(health, "mod-a", "helpful")
    record_invocation(health, "mod-a", "helpful")
    record_invocation(health, "mod-b", "noise")
    assert health["modules"]["mod-a"]["successful"] == 2
    assert health["modules"]["mod-a"]["failed"] == 0
    assert health["modules"]["mod-b"]["successful"] == 0
    assert health["modules"]["mod-b"]["failed"] == 1


# ---------------------------------------------------------------------------
# Constants sanity checks
# ---------------------------------------------------------------------------

def test_supplementary_modules_set():
    """Verify SUPPLEMENTARY_MODULES contains the expected stores."""
    assert "reasoning_bank" in SUPPLEMENTARY_MODULES
    assert "guardrails" in SUPPLEMENTARY_MODULES
    assert "pattern_signatures" in SUPPLEMENTARY_MODULES
    # Tree categories should NOT be in the set
    assert "data-engineering" not in SUPPLEMENTARY_MODULES


def test_default_thresholds():
    """Verify the default constants match the canonical plan."""
    assert DEFAULT_TREE_THRESHOLD == 0.4
    assert DEFAULT_TREE_MIN_INVOCATIONS == 50
    assert DEFAULT_SUPP_THRESHOLD == 0.3
    assert DEFAULT_SUPP_MIN_INVOCATIONS == 100
