"""test_applies_to_required.py — applies_to is now in RB_REQUIRED_FIELDS (P3 #14).

Background (2026-05-10):
  Audit found 299/620 active reasoning-bank entries (48%) with applies_to=None.
  The legacy convention treated absent → "specific", but 294 of those were
  actually framework/domain/any — silently misclassified, failing to surface
  in cross-domain retrieval. The fix is twofold: backfill (handled by
  audit-applies-to.py --apply-all) and prevention — make the field required
  so every new entry must explicitly choose.

This test enforces the prevention layer.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# Bootstrap clean WORLD env BEFORE importing reasoning-bank module.
# : capture original env values FIRST so we can restore after
# rb_mod imports. Without restoration, this module's env mutation pollutes
# subsequent tests in the same pytest session — confirmed Cat D of
#  (5/5 window_streak failures with goal_not_found). pytest
# imports ALL test modules during collection BEFORE running any tests, so
# in-process atexit / setup_module / teardown_module cannot guard against
# this leak — the mutation has to be undone at import time itself.
_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")

_TMPDIR = tempfile.mkdtemp(prefix="rb-applies-required-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)

RB_PATH = CORE_SCRIPTS / "reasoning-bank.py"
spec = importlib.util.spec_from_file_location("reasoning_bank", RB_PATH)
rb_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rb_mod)

# : restore original env values immediately after rb_mod is loaded.
# The functions exercised by these tests (validate_rb_record,
# RB_REQUIRED_FIELDS, RB_ADD_SCHEMA_TEXT) are pure logic and do not re-read
# MIND_WORLD / MIND_AGENT at call time, so restoring env now does NOT
# break this module's own tests. It DOES prevent the leak that breaks
# downstream tests (test_window_streak, test_stall_goal_filer_override,
# test_streak_break_reflector, test_stale_sentinel_canary, test_auto_contract,
# test_inactivity_detector, test_inferred_unknown_autoflag,
# test_cross_aspiration_support — Cat D of ).
if _ORIG_MIND_WORLD is not None:
    os.environ["MIND_WORLD"] = _ORIG_MIND_WORLD
elif "MIND_WORLD" in os.environ:
    del os.environ["MIND_WORLD"]
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT


def _seed_minimal_rb(applies_to=...):
    """Build a minimal-but-valid rb record. Pass applies_to=... to omit it."""
    rec = {
        "id": "rb-999",
        "type": "success",
        "status": "active",
        "category": "test",
        "title": "test",
        "content": "test content",
        "created": "2026-05-10T00:00:00",
        "tags": [],
        "utilization": {
            "retrieval_count": 0,
            "times_helpful": 0,
            "times_noise": 0,
            "times_active": 0,
            "times_skipped": 0,
            "times_cited": 0,
            "times_inferred_helpful": 0,
            "times_inferred_unknown": 0,
            "last_retrieved": None,
            "utilization_score": 0.0,
        },
    }
    if applies_to is not ...:
        rec["applies_to"] = applies_to
    return rec


def test_applies_to_in_required_fields():
    """The constant itself names the field as required."""
    assert "applies_to" in rb_mod.RB_REQUIRED_FIELDS, \
        f"applies_to must be in RB_REQUIRED_FIELDS, got: {rb_mod.RB_REQUIRED_FIELDS}"


def test_missing_applies_to_fails_validation():
    """A record with no applies_to key fails validate_rb_record."""
    rec = _seed_minimal_rb()  # applies_to omitted
    try:
        rb_mod.validate_rb_record(rec)
        assert False, "expected ValueError on missing applies_to"
    except ValueError as e:
        assert "applies_to" in str(e), f"error must mention applies_to, got: {e}"


def test_applies_to_none_fails_validation():
    """Explicit None still fails — the change moves None from valid to invalid."""
    rec = _seed_minimal_rb(applies_to=None)
    try:
        rb_mod.validate_rb_record(rec)
        assert False, "expected ValueError on applies_to=None"
    except ValueError as e:
        # Either "Missing required fields" (None == missing for set check)
        # or "Invalid applies_to" — either is fine, both fail loud
        assert "applies_to" in str(e), f"error must mention applies_to, got: {e}"


def test_each_valid_value_passes():
    """All four canonical values pass: any, framework, domain, specific."""
    for v in ("any", "framework", "domain", "specific"):
        rec = _seed_minimal_rb(applies_to=v)
        rb_mod.validate_rb_record(rec)  # must not raise


def test_invalid_value_fails():
    """Non-canonical values (typos, legacy 'lesson' etc.) fail loud."""
    for bad in ("Framework", "ANY", "lesson", "all", ""):
        rec = _seed_minimal_rb(applies_to=bad)
        try:
            rb_mod.validate_rb_record(rec)
            assert False, f"expected ValueError on applies_to={bad!r}"
        except ValueError as e:
            assert "applies_to" in str(e), \
                f"error must mention applies_to for {bad!r}, got: {e}"


# test_schema_text_marks_applies_to_required RETIRED 2026-05-17 ().
# The RB_ADD_SCHEMA_TEXT module-level constant was intentionally removed
# during H2 Wave 2 daemon-cutover — reasoning-bank.py:209 comment confirms
# the schema text now lives in the daemon endpoint's CLI usage block, not as
# a Python-importable constant. The prevention contract (applies_to required
# in RB_REQUIRED_FIELDS, validation refuses missing/invalid values) is still
# pinned by test_applies_to_in_required_fields + test_missing_applies_to_fails_validation
# + test_applies_to_none_fails_validation + test_each_valid_value_passes +
# test_invalid_value_fails above. The schema-text test was a docs-presence
# check, not a validation contract — removing it does not weaken the
# prevention layer this file enforces.


def test_list_valued_fields_raise_valueerror_not_typeerror():
    """B10 (Lodestar): a list where a scalar is expected (type/status/applies_to)
    must raise ValueError, never TypeError (unhashable type: 'list'). The TypeError
    used to escape store.append's `except ValueError` and 500 -> dropped rb lesson.
    Pins the CLI twin in sync with mind_api/src/store_registry.py (B10 daemon fix)."""
    for field, bad in (("type", ["success"]), ("status", ["active"]),
                       ("applies_to", ["framework"])):
        rec = _seed_minimal_rb(applies_to="framework")
        rec[field] = bad
        try:
            rb_mod.validate_rb_record(rec)
            assert False, f"expected ValueError on {field}={bad!r}"
        except TypeError as e:  # noqa: F841
            assert False, f"{field}={bad!r} raised TypeError (B10 regression): {e}"
        except ValueError as e:
            assert "unhashable" not in str(e).lower(), \
                f"{field}: must be a clean ValueError, not unhashable TypeError: {e}"


# --- entry_type () — CLI twin, kept in sync with the daemon validator
# (mind_api/src/store_registry.py, tested by test_rb_validate_list_field_rejection.py).
# The two validators MUST accept/reject the same entry_type inputs; these mirror
# the daemon cases so a one-sided change to either fails here. ---

def test_entry_type_absent_and_null_pass_cli():
    """entry_type is OPTIONAL: absent and explicit-null both validate."""
    rec = _seed_minimal_rb(applies_to="framework")
    rb_mod.validate_rb_record(rec)  # absent — must not raise
    rec["entry_type"] = None
    rb_mod.validate_rb_record(rec)  # explicit null — must not raise


def test_entry_type_procedure_passes_cli():
    """The one valid non-null value validates on the CLI side too."""
    rec = _seed_minimal_rb(applies_to="framework")
    rec["entry_type"] = "procedure"
    rb_mod.validate_rb_record(rec)  # must not raise


def test_entry_type_unknown_rejected_cli():
    """An unknown entry_type fails loud and names the field."""
    rec = _seed_minimal_rb(applies_to="framework")
    rec["entry_type"] = "procedrue"  # typo
    try:
        rb_mod.validate_rb_record(rec)
        assert False, "expected ValueError on unknown entry_type"
    except ValueError as e:
        assert "entry_type" in str(e).lower(), f"error must name entry_type, got: {e}"


def test_entry_type_list_valued_rejected_cli():
    """B10: a list-valued entry_type raises a clean ValueError, never TypeError."""
    rec = _seed_minimal_rb(applies_to="framework")
    rec["entry_type"] = ["procedure"]
    try:
        rb_mod.validate_rb_record(rec)
        assert False, "expected ValueError on list-valued entry_type"
    except TypeError as e:  # noqa: F841
        assert False, f"list entry_type raised TypeError (B10 regression): {e}"
    except ValueError as e:
        assert "unhashable" not in str(e).lower(), \
            f"must be a clean ValueError, not unhashable TypeError: {e}"


def _run_all():
    tests = [v for k, v in globals().items()
             if k.startswith("test_") and callable(v)]
    failures = []
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures.append((t.__name__, str(e) or "<no message>"))
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:
            failures.append((t.__name__, f"{type(e).__name__}: {e}"))
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(_run_all())
