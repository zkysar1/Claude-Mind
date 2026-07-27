"""test_defer_recheck_patterns.py — regression test for .

Asserts that defer-recheck.py's three new precondition_unmet pattern
handlers (added in g-115-340 / iter-110) recognize their respective
narrative shapes and produce the expected action:

  precon_elapsed:  "precondition_unmet: NNh_elapsed_since_g-XXX-YY_<event>"
                   → "clear" when cited dep completed AND elapsed >= NN
                   → "skipped" with diagnostic when cond unmet/dep missing
  precon_timegate: "precondition_unmet: time-gate not_before=DATE"
                   → "skipped" with migration recommendation (per g-001-193,
                     never auto-clears — narrative time-gates should migrate
                     to the structured deferred_until field)
  precon_studio:   "precondition_unmet: domain_session_required"
                   → "skipped" with batched-notification routing reason

Cases covered (verification.outcomes (a) for g-115-340: 3 new pattern
handlers in defer-recheck.py with unit tests):
  1. precon_elapsed clears when dep completed past target window
  2. precon_elapsed skips when dep completed but inside target window
  3. precon_elapsed skips when cited dep is pending
  4. precon_elapsed skips when cited dep is not in queues
  5. precon_timegate recognized — never auto-clears, recommends migration
  6. precon_studio recognized — never auto-clears, queues for batched
     smoke-test notification
  7. _try_new_patterns dispatcher returns None for unrecognized text

Pattern: same importlib + sys.path shape as test_defer_gate_unblock_filing.py.
defer-recheck.py uses a hyphenated filename so we load it via
spec_from_file_location with a hyphen-free attribute name.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))


def _import_defer_recheck():
    """Load defer-recheck.py via importlib (hyphen-free attribute name)."""
    spec = importlib.util.spec_from_file_location(
        "defer_recheck_mod", CORE_SCRIPTS / "defer-recheck.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load spec for defer-recheck.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _completed_iso(hours_ago: float) -> str:
    """Return an ISO-8601 timestamp NN hours before now."""
    t = dt.datetime.now() - dt.timedelta(hours=hours_ago)
    return t.isoformat(timespec="seconds")


def _completed_date(days_ago: int) -> str:
    """Return a YYYY-MM-DD date N days before today (matches completed_date schema)."""
    return (dt.datetime.now() - dt.timedelta(days=days_ago)).date().isoformat()


def _make_by_id(*goals: dict) -> dict:
    return {g["id"]: g for g in goals}


def test_precon_elapsed_clears_when_dep_completed_past_target():
    mod = _import_defer_recheck()
    reason = "precondition_unmet: 24h_elapsed_since_g-115-191_deploy"
    by_id = _make_by_id({
        "id": "g-115-191",
        "status": "completed",
        "completed_at": _completed_iso(48),  # 48h > 24h target
    })
    r = mod._try_precon_elapsed(reason, by_id)
    assert r is not None, "expected pattern to match"
    assert r["action"] == "clear", f"expected clear, got {r}"
    assert r["dep_ids"] == ["g-115-191"]
    assert "elapsed=" in r["reason"]


def test_precon_elapsed_skips_when_dep_completed_inside_target():
    mod = _import_defer_recheck()
    reason = "precondition_unmet: 24h_elapsed_since_g-115-191_deploy"
    by_id = _make_by_id({
        "id": "g-115-191",
        "status": "completed",
        "completed_at": _completed_iso(8),  # 8h < 24h target
    })
    r = mod._try_precon_elapsed(reason, by_id)
    assert r is not None
    assert r["action"] == "skipped", f"expected skipped, got {r}"
    assert "< target=24" in r["reason"]


def test_precon_elapsed_skips_when_dep_pending():
    mod = _import_defer_recheck()
    reason = "precondition_unmet: 24h_elapsed_since_g-115-191_deploy"
    by_id = _make_by_id({
        "id": "g-115-191",
        "status": "pending",
    })
    r = mod._try_precon_elapsed(reason, by_id)
    assert r is not None
    assert r["action"] == "skipped"
    assert "status=pending" in r["reason"]


def test_precon_elapsed_skips_when_dep_missing():
    mod = _import_defer_recheck()
    reason = "precondition_unmet: 24h_elapsed_since_g-999-99_deploy"
    by_id = {}  # empty queues
    r = mod._try_precon_elapsed(reason, by_id)
    assert r is not None
    assert r["action"] == "skipped"
    assert "not found" in r["reason"]


def test_precon_timegate_recognized_no_auto_clear():
    """Per : narrative time-gates must NOT auto-clear; recommend migration."""
    mod = _import_defer_recheck()
    reason = "precondition_unmet: time-gate not_before=2026-05-06"
    r = mod._try_precon_timegate(reason)
    assert r is not None, "expected pattern to match"
    assert r["action"] == "skipped", f"narrative time-gate must NOT auto-clear, got {r}"
    assert "deferred_until" in r["reason"], "reason must surface the migration recommendation"
    assert "g-001-193" in r["reason"], "reason must cite g-001-193 finding"


def test_precon_studio_recognized_no_auto_clear():
    mod = _import_defer_recheck()
    reason = ("precondition_unmet: domain_session_required — protocol "
              "requires user to open Roblox Studio")
    r = mod._try_precon_studio(reason)
    assert r is not None, "expected pattern to match"
    assert r["action"] == "skipped", "Studio gate must NOT auto-clear (user-action required)"
    assert "user-action" in r["reason"]
    assert "batched smoke test" in r["reason"]


def test_try_new_patterns_returns_none_for_unrecognized():
    mod = _import_defer_recheck()
    # Free-form narrative with none of the three patterns
    r = mod._try_new_patterns("just some prose about waiting", {})
    assert r is None, f"expected None for unrecognized text, got {r}"


def test_try_new_patterns_dispatcher_attaches_pattern_field():
    """Dispatcher must annotate the matched handler name onto the result."""
    mod = _import_defer_recheck()
    by_id = _make_by_id({
        "id": "g-115-191",
        "status": "completed",
        "completed_at": _completed_iso(48),
    })
    r = mod._try_new_patterns(
        "precondition_unmet: 24h_elapsed_since_g-115-191_deploy", by_id)
    assert r is not None
    assert r.get("pattern") == "precon_elapsed"

    r = mod._try_new_patterns(
        "precondition_unmet: time-gate not_before=2026-05-06", {})
    assert r is not None
    assert r.get("pattern") == "precon_timegate"

    r = mod._try_new_patterns(
        "precondition_unmet: domain_session_required", {})
    assert r is not None
    assert r.get("pattern") == "precon_studio"


def test_time_gated_dep_recognized_when_all_deps_completed():
    """: deferred_until goal whose defer names ALL-completed deps
    → recognize-only classification (never a clear)."""
    mod = _import_defer_recheck()
    by_id = _make_by_id({
        "id": "g-115-2051",
        "status": "completed",
        "completed_at": _completed_iso(4),
    })
    g = {"id": "g-115-2052",
         "status": "pending",
         "deferred_until": "2026-07-16T00:00:00",
         "defer_reason": "blocked_on_dependency: g-115-2051 (fleet census)"}
    r = mod._classify_time_gated_dep(g, by_id)
    assert r is not None, "expected classification for completed dep + time gate"
    assert r["action"] == "skipped", f"must be recognize-only, got {r['action']}"
    assert r["pattern"] == "deps_complete_time_gated"
    assert r["dep_ids"] == ["g-115-2051"]
    assert "Never auto-cleared" in r["reason"]


def test_time_gated_dep_none_when_dep_pending():
    """Dep still pending → no classification (silent skip, prior behavior)."""
    mod = _import_defer_recheck()
    by_id = _make_by_id({"id": "g-115-2051", "status": "pending"})
    g = {"id": "g-115-2052",
         "status": "pending",
         "deferred_until": "2026-07-16T00:00:00",
         "defer_reason": "blocked_on_dependency: g-115-2051 (fleet census)"}
    assert mod._classify_time_gated_dep(g, by_id) is None


def test_time_gated_dep_none_when_no_dep_pattern():
    """Pure time-gate narrative (no dep ids) → None ( skip stands)."""
    mod = _import_defer_recheck()
    g = {"id": "g-115-2094",
         "status": "pending",
         "deferred_until": "2026-07-16T00:00:00",
         "defer_reason": "observation window: stale writers need days to reveal"}
    assert mod._classify_time_gated_dep(g, {}) is None


# Standalone runner — match the bare-pytest-style of sibling regression tests
# (no pytest dependency assumed in CI).

def _run_all():
    cases = [
        ("precon_elapsed clears past target",
            test_precon_elapsed_clears_when_dep_completed_past_target),
        ("precon_elapsed skips inside target",
            test_precon_elapsed_skips_when_dep_completed_inside_target),
        ("precon_elapsed skips when dep pending",
            test_precon_elapsed_skips_when_dep_pending),
        ("precon_elapsed skips when dep missing",
            test_precon_elapsed_skips_when_dep_missing),
        ("precon_timegate recognized no auto-clear",
            test_precon_timegate_recognized_no_auto_clear),
        ("precon_studio recognized no auto-clear",
            test_precon_studio_recognized_no_auto_clear),
        ("dispatcher returns None for unrecognized",
            test_try_new_patterns_returns_none_for_unrecognized),
        ("dispatcher attaches pattern field",
            test_try_new_patterns_dispatcher_attaches_pattern_field),
        ("time-gated dep recognized when all deps completed",
            test_time_gated_dep_recognized_when_all_deps_completed),
        ("time-gated dep none when dep pending",
            test_time_gated_dep_none_when_dep_pending),
        ("time-gated dep none when no dep pattern",
            test_time_gated_dep_none_when_no_dep_pattern),
    ]
    failed = 0
    names = []
    for name, fn in cases:
        try:
            fn()
            names.append(name)
        except AssertionError as e:
            failed += 1
            print(f"FAIL: {name}: {e}")
    if failed == 0:
        print(f"TEST PASS: {len(cases)} cases — " + "; ".join(n.split()[0] for n in names))
    else:
        print(f"TEST FAIL: {failed}/{len(cases)} cases failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_run_all())
