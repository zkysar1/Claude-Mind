"""Tests for trivial-goal-classify.py (; design ).

The classifier predicts whether a goal is TRIVIAL (skip the Phase 3.9-4.5
ceremony) via a conservative 3-boolean AND-gate that fails to "full" on any
uncertainty. These tests pin the pure helpers + classify_goal (the daemon read
in main() is the only impure part and is not exercised here).

Pattern: same importlib + sys.path shape as test_defer_drift_check.py (the
script name has hyphens, so it cannot be a plain `import`).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "trivial-goal-classify.py"

# A populated, ENABLED config so classify_goal logic is exercised independent of
# the live aspirations.yaml master flag (which defaults OFF).
CFG = {
    "enabled": True,
    "pure_action_skills": ["notify", "health", "presence", "inbox"],
    "action_only_categories": ["monitoring", "notification"],
    "never_trivial_categories": ["framework-architecture", "framework-engineering"],
}


def _import():
    spec = importlib.util.spec_from_file_location("trivial_goal_classify", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["trivial_goal_classify"] = mod
    spec.loader.exec_module(mod)
    return mod


def _goal(**kw):
    """A pure-action, trivial-shaped goal; override fields via kwargs."""
    g = {
        "id": "g-001-99",
        "status": "in-progress",
        "skill": "/notify-user",
        "category": "notification",
        "verification": {"checks": []},
    }
    g.update(kw)
    return g


# ── empty_verification_checks (Boolean 1) ───────────────────────────────────

def test_empty_checks_true_when_verification_null():
    mod = _import()
    assert mod.empty_verification_checks(_goal(verification=None)) is True


def test_empty_checks_true_when_verification_not_dict():
    mod = _import()
    assert mod.empty_verification_checks(_goal(verification="oops")) is True


def test_empty_checks_true_when_checks_absent_or_empty():
    mod = _import()
    assert mod.empty_verification_checks(_goal(verification={})) is True
    assert mod.empty_verification_checks(_goal(verification={"outcomes": ["x"]})) is True
    assert mod.empty_verification_checks(_goal(verification={"checks": []})) is True


def test_empty_checks_false_when_checks_present():
    mod = _import()
    g = _goal(verification={"checks": [{"type": "file_check", "target": "x"}]})
    assert mod.empty_verification_checks(g) is False


# ── no_diff_expected (Boolean 2) ─────────────────────────────────────────────

def test_no_diff_explicit_false_wins():
    mod = _import()
    # produces_diff:false → True even with a code category + non-action skill
    g = _goal(produces_diff=False, category="framework-architecture", skill=None)
    assert mod.no_diff_expected(g, CFG) is True


def test_no_diff_explicit_true_overrides_pure_action_skill():
    mod = _import()
    # produces_diff:true → False even though skill is pure-action
    assert mod.no_diff_expected(_goal(produces_diff=True), CFG) is False


def test_no_diff_pure_action_skill_substring():
    mod = _import()
    assert mod.no_diff_expected(_goal(skill="/notify-user", category="x"), CFG) is True
    assert mod.no_diff_expected(_goal(skill="/inbox-check", category="x"), CFG) is True


def test_no_diff_action_only_category():
    mod = _import()
    assert mod.no_diff_expected(_goal(skill=None, category="monitoring"), CFG) is True


def test_no_diff_false_for_code_category_with_null_skill():
    mod = _import()
    # The brief's hard rule: never infer no-diff from a code/framework/analysis
    # category. "framework-architecture" is absent from action_only_categories,
    # skill is null → not pure-action → False.
    assert mod.no_diff_expected(_goal(skill=None, category="framework-architecture"), CFG) is False
    assert mod.no_diff_expected(_goal(skill=None, category="analysis"), CFG) is False


# ── no_retrieval_call (Boolean 3) ────────────────────────────────────────────

def test_no_retrieval_explicit_none():
    mod = _import()
    assert mod.no_retrieval_call(_goal(retrieval="none", skill=None), CFG) is True


def test_no_retrieval_explicit_nonnone_means_needs_retrieval():
    mod = _import()
    # An explicit retrieval mode other than "none" means retrieval IS needed,
    # even if the skill would otherwise look pure-action.
    assert mod.no_retrieval_call(_goal(retrieval="shallow", skill="/notify-user"), CFG) is False


def test_no_retrieval_pure_action_skill_fallback():
    mod = _import()
    assert mod.no_retrieval_call(_goal(skill="/health-check"), CFG) is True


def test_no_retrieval_false_for_judgment_goal():
    mod = _import()
    assert mod.no_retrieval_call(_goal(skill=None, category="analysis"), CFG) is False


# ── _skill_is_pure_action ────────────────────────────────────────────────────

def test_skill_pure_action_null_is_false():
    mod = _import()
    assert mod._skill_is_pure_action(None, CFG) is False
    assert mod._skill_is_pure_action("", CFG) is False


def test_skill_pure_action_case_insensitive():
    mod = _import()
    assert mod._skill_is_pure_action("/NOTIFY-User", CFG) is True


def test_skill_pure_action_non_match():
    mod = _import()
    assert mod._skill_is_pure_action("/research-topic", CFG) is False


# ── classify_goal (the AND-gate) ─────────────────────────────────────────────

def test_classify_trivial_when_all_three_hold():
    mod = _import()
    g = _goal(skill="/notify-user", category="notification",
              verification={"checks": []}, retrieval="none")
    verdict, reasons = mod.classify_goal(g, CFG)
    assert verdict == "trivial"
    assert reasons["empty_verification_checks"] is True
    assert reasons["no_diff_expected"] is True
    assert reasons["no_retrieval_call"] is True


def test_classify_full_when_any_boolean_fails():
    mod = _import()
    # checks present → b1 False → full (other two hold)
    g = _goal(verification={"checks": [{"type": "file_check"}]}, retrieval="none")
    verdict, _ = mod.classify_goal(g, CFG)
    assert verdict == "full"


def test_classify_never_trivial_category_overrides_all():
    mod = _import()
    # Even with all three booleans satisfiable, a never_trivial category → full.
    g = _goal(category="framework-architecture", skill="/notify-user",
              verification={"checks": []}, retrieval="none", produces_diff=False)
    verdict, reasons = mod.classify_goal(g, CFG)
    assert verdict == "full"
    assert reasons.get("never_trivial_category") == "framework-architecture"


def test_classify_g_305_15_self_shape_is_full():
    """The implementing goal itself: framework-architecture, empty checks,
    skill null. Must be 'full' (deep code goal) — caught by the denylist AND by
    no_diff_expected (code category, null skill)."""
    mod = _import()
    g = {
        "id": "g-305-15",
        "category": "framework-architecture",
        "skill": None,
        "verification": {"outcomes": ["..."], "checks": []},
    }
    verdict, _ = mod.classify_goal(g, CFG)
    assert verdict == "full"


def test_classify_recurring_health_check_goal_is_trivial():
    """The canonical trivial target: a recurring health-check with empty checks,
    pure-action skill, no retrieval need."""
    mod = _import()
    g = {
        "id": "g-115-99",
        "recurring": True,
        "skill": "/health-probe",
        "category": "monitoring",
        "verification": {"checks": []},
    }
    verdict, _ = mod.classify_goal(g, CFG)
    assert verdict == "trivial"


# ── default-OFF guarantee + config loader ────────────────────────────────────

def test_default_config_disabled_by_default():
    """: the master flag ships OFF. This is the byte-identical-behavior
    guarantee — with enabled:false main() returns 'full' for every goal."""
    mod = _import()
    assert mod.DEFAULT_CONFIG["enabled"] is False


def test_load_config_returns_expected_shape():
    """load_config must always return a dict with the four keys and fail-soft to
    defaults on any error (never raise into the hot path)."""
    mod = _import()
    cfg = mod.load_config()
    assert isinstance(cfg, dict)
    assert isinstance(cfg["enabled"], bool)
    for k in ("pure_action_skills", "action_only_categories",
              "never_trivial_categories"):
        assert isinstance(cfg[k], list)


def test_live_config_master_flag_is_off():
    """The shipped aspirations.yaml lightweight_mode.enabled must be false
    (regression guard against an accidental enable commit)."""
    mod = _import()
    assert mod.load_config()["enabled"] is False
