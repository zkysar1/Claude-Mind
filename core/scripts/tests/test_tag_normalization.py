"""test_tag_normalization.py — _normalize_tags canonicalization (P3 #12).

Covers:
  - Lowercase: 'IAUS' → 'iaus'
  - Underscore → dash: 'work_class' → 'work-class'
  - Whitespace strip: ' tag ' → 'tag'
  - Dedup: ['iaus', 'IAUS'] → ['iaus']
  - Order preservation: first occurrence wins
  - Idempotent: already-canonical tags pass through unchanged
  - Non-list tags: missing/None/non-list — leave alone (no crash)
  - Non-string entries: pass through (let strict validation surface them)
  - Empty string after strip: dropped
  - Wired into validate_rb_record + validate_guard_record (write-time hook)
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# Bootstrap clean WORLD env BEFORE importing reasoning-bank module
#  capture-restore pattern: stash env before module-level mutation
# so subsequent tests in the same pytest session don't inherit a popped
# MIND_AGENT. See test_applies_to_required.py for full rationale.
_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")

_TMPDIR = tempfile.mkdtemp(prefix="rb-tag-norm-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)

import importlib.util
RB_PATH = CORE_SCRIPTS / "reasoning-bank.py"
spec = importlib.util.spec_from_file_location("reasoning_bank", RB_PATH)
rb_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rb_mod)

# Restore env so downstream tests inherit clean conftest defaults.
if _ORIG_MIND_WORLD is not None:
    os.environ["MIND_WORLD"] = _ORIG_MIND_WORLD
elif "MIND_WORLD" in os.environ:
    del os.environ["MIND_WORLD"]
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT

_normalize_tags = rb_mod._normalize_tags


def test_lowercase_normalization():
    rec = {"tags": ["IAUS", "SSOT"]}
    _normalize_tags(rec)
    assert rec["tags"] == ["iaus", "ssot"]


def test_underscore_to_dash():
    rec = {"tags": ["work_class", "memory_pipeline"]}
    _normalize_tags(rec)
    assert rec["tags"] == ["work-class", "memory-pipeline"]


def test_whitespace_strip():
    rec = {"tags": [" tag ", "  another  "]}
    _normalize_tags(rec)
    assert rec["tags"] == ["tag", "another"]


def test_dedup_collapses_case_variants():
    """The original P3 #12 motivating case — IAUS and iaus collapse."""
    rec = {"tags": ["iaus", "IAUS", "fallback", "ssot"]}
    _normalize_tags(rec)
    assert rec["tags"] == ["iaus", "fallback", "ssot"]


def test_dedup_collapses_separator_variants():
    rec = {"tags": ["work-class", "work_class"]}
    _normalize_tags(rec)
    assert rec["tags"] == ["work-class"]


def test_order_preservation_first_wins():
    """Dedup keeps the FIRST occurrence's index, not the lexically-first variant."""
    rec = {"tags": ["zebra", "apple", "ZEBRA"]}
    _normalize_tags(rec)
    assert rec["tags"] == ["zebra", "apple"]


def test_idempotent_on_canonical_tags():
    """Already-canonical tags pass through with no churn."""
    canonical = ["framework", "rb-428-family", "single-source", "g-115-244"]
    rec = {"tags": list(canonical)}
    _normalize_tags(rec)
    assert rec["tags"] == canonical
    # Re-run — must produce the same output
    _normalize_tags(rec)
    assert rec["tags"] == canonical


def test_missing_tags_field_safe():
    """No tags key — function returns silently, doesn't add one."""
    rec = {"id": "rb-001", "type": "lesson"}
    _normalize_tags(rec)
    assert "tags" not in rec


def test_none_tags_field_safe():
    """tags=None — leave alone (validation will catch the type issue elsewhere)."""
    rec = {"tags": None}
    _normalize_tags(rec)
    assert rec["tags"] is None


def test_non_list_tags_safe():
    """tags as string — leave alone (validation catches type issue)."""
    rec = {"tags": "not-a-list"}
    _normalize_tags(rec)
    assert rec["tags"] == "not-a-list"


def test_non_string_entries_passthrough():
    """Non-string tag entries pass through — strict validation handles them."""
    rec = {"tags": ["good", 42, None, "also-good"]}
    _normalize_tags(rec)
    # 42 and None passed through; strings normalized; dedup applies to strings
    assert rec["tags"] == ["good", 42, None, "also-good"]


def test_empty_string_after_strip_dropped():
    """Whitespace-only tag becomes empty after strip — dropped, not preserved."""
    rec = {"tags": ["valid", "   ", "also-valid"]}
    _normalize_tags(rec)
    assert rec["tags"] == ["valid", "also-valid"]


def test_multiple_separators_collapsed():
    """Consecutive separators collapse to single dash."""
    rec = {"tags": ["a___b", "x---y", "p _ q"]}
    _normalize_tags(rec)
    assert rec["tags"] == ["a-b", "x-y", "p-q"]


# ---------------------------------------------------------------------------
# Wired into validate_rb_record + validate_guard_record
# ---------------------------------------------------------------------------

def _seed_rb(rec_overrides):
    """Build a minimal rb record with the given overrides for validation tests."""
    base = {
        "id": "rb-999",
        "type": "success",
        "status": "active",
        "category": "test-category",
        "title": "test",
        "content": "test content",
        "created": "2026-05-10T00:00:00",
        "tags": [],
        "applies_to": "specific",
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
    base.update(rec_overrides)
    return base


def test_validate_rb_record_normalizes_tags():
    """Submit a record with case/separator variants — validate_rb_record
    mutates tags into canonical form before returning."""
    rec = _seed_rb({"tags": ["IAUS", "work_class", "  Padded  ", "iaus"]})
    rb_mod.validate_rb_record(rec)
    assert rec["tags"] == ["iaus", "work-class", "padded"]


def test_validate_guard_record_normalizes_tags():
    """Same canonicalization on the guardrail validator."""
    guard = {
        "id": "guard-999",
        "rule": "test",
        "status": "active",
        "category": "test-category",
        "trigger_condition": "test condition",
        "source": "test-source",
        "severity": "MEDIUM",
        "trigger_pattern": "test",
        "action_hint": "test",
        "created": "2026-05-10T00:00:00",
        "tags": ["SSOT", "fresh_eyes", "ssot"],
        "context_triggers": [],
        "phases": [],
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
    rb_mod.validate_guard_record(guard)
    assert guard["tags"] == ["ssot", "fresh-eyes"]


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
