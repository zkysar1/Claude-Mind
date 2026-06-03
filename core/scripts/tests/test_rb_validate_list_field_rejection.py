"""test_rb_validate_list_field_rejection.py — B10 (Lodestar soak).

POST /v1/store/append?store=reasoning-bank 500'd 9x in the soak with
"TypeError: unhashable type: 'list'" -> the rb lesson was LOST. Root cause:
a list-valued scalar field (type / status / applies_to) hits a
`field not in <set>` membership test in validate_rb_record; `list not in set`
raises TypeError (unhashable), which escaped store.append's `except ValueError`
handler and surfaced as a 500.

This pins the DAEMON validator (mind_api/src/store_registry.py) — the live
write path — so a malformed list-valued field now raises a clean ValueError
(caught -> 400 validation_failed) instead of a 500 that drops the write. The
CLI twin (core/scripts/reasoning-bank.py) is pinned by test_applies_to_required.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mind_api.src.store_registry import validate_rb_record  # noqa: E402


def _minimal_rb(**overrides):
    rec = {
        "id": "rb-999",
        "type": "success",
        "status": "active",
        "category": "test",
        "title": "test",
        "content": "test content",
        "created": "2026-06-01T00:00:00",
        "applies_to": "framework",
        "tags": [],
    }
    rec.update(overrides)
    return rec


def test_valid_record_passes():
    """Baseline: a well-formed record does not raise."""
    validate_rb_record(None, _minimal_rb(), skip_id=False)


@pytest.mark.parametrize("field,bad", [
    ("type", ["success"]),
    ("type", ["success", "failure"]),
    ("status", ["active"]),
    ("applies_to", ["framework"]),
    ("applies_to", ["framework", "domain"]),
])
def test_list_valued_scalar_raises_valueerror_not_typeerror(field, bad):
    """A list where a scalar is expected must raise ValueError, never TypeError.

    Before B10 this raised `TypeError: unhashable type: 'list'` which escaped
    the caller's `except ValueError` and 500'd. The isinstance guard now
    short-circuits to a clean ValueError (-> 400 validation_failed).
    """
    rec = _minimal_rb(**{field: bad})
    with pytest.raises(ValueError) as ei:
        validate_rb_record(None, rec, skip_id=False)
    # The message must name the field, and must NOT be the cryptic unhashable text.
    assert field in str(ei.value).lower() or field.replace("_", " ") in str(ei.value).lower(), \
        f"error should name {field}: {ei.value}"
    assert "unhashable" not in str(ei.value).lower()


def test_dict_valued_scalar_also_safe():
    """A dict (also unhashable) where a scalar is expected: clean ValueError."""
    rec = _minimal_rb(applies_to={"x": 1})
    with pytest.raises(ValueError):
        validate_rb_record(None, rec, skip_id=False)


def _run_all():
    """Standalone runner (no pytest) — manual param expansion."""
    failures = []
    cases = [
        ("valid", lambda: validate_rb_record(None, _minimal_rb(), skip_id=False), None),
    ]
    for field, bad in [("type", ["success"]), ("status", ["active"]),
                       ("applies_to", ["framework"])]:
        cases.append((f"{field}={bad}",
                      lambda f=field, b=bad: validate_rb_record(
                          None, _minimal_rb(**{f: b}), skip_id=False),
                      ValueError))
    for name, fn, expect in cases:
        try:
            fn()
            if expect is None:
                print(f"PASS {name}")
            else:
                failures.append(name)
                print(f"FAIL {name}: expected {expect.__name__}, none raised")
        except Exception as e:
            if expect and isinstance(e, expect) and "unhashable" not in str(e).lower():
                print(f"PASS {name} ({type(e).__name__})")
            else:
                failures.append(name)
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{len(cases) - len(failures)}/{len(cases)} passed")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(_run_all())
