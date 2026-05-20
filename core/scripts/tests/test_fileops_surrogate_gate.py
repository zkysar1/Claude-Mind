""": write-time UTF-8 surrogate gate (round-trip test).

Validates that core/scripts/_fileops.py rejects payloads containing unpaired
UTF-16 surrogates (U+D800-U+DFFF) BEFORE acquiring a file lock or writing.
This is the safety net for upstream stdin readers that fail to reconfigure
encoding to utf-8 — see g-276-02 audit report.

Failure modes the gate must catch:
  - Single string containing U+DC9D (the byte sequence 0xed 0xb2 0x9d that
    crashed alpha/aspirations.jsonl on 2026-05-07)
  - Surrogate nested in a dict value (the realistic shape — every JSONL
    record is a dict of strings)
  - Surrogate nested in a list element
  - Surrogate as a dict KEY (Python allows it; gate must catch it)

Failure modes the gate must NOT trip on (false positives):
  - ASCII text
  - Valid UTF-8 with em-dash / smart quotes / NFC-composed characters
  - 4-byte UTF-8 emoji
  - Empty string
  - Numbers, booleans, None

Run: py -3 core/scripts/tests/test_fileops_surrogate_gate.py
"""
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

# Make _fileops importable
SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import _fileops


SURROGATE = "\udc9d"  # U+DC9D — the canonical 0xed 0xb2 0x9d crash signature
EM_DASH = "—"
SMART_QUOTE = "“"
EMOJI = "\U0001f600"


def assert_raises(fn, *args, **kwargs):
    """Run fn(*args), expect ValueError mentioning the gate."""
    try:
        fn(*args, **kwargs)
    except ValueError as e:
        msg = str(e)
        assert "FILEOPS_SURROGATE_GATE" in msg, f"Wrong error: {msg!r}"
        return msg
    raise AssertionError(f"Expected ValueError, got nothing for fn={fn.__name__}")


def assert_no_raise(fn, *args, **kwargs):
    """Run fn(*args), expect no exception."""
    try:
        fn(*args, **kwargs)
    except Exception as e:
        raise AssertionError(f"Expected no exception, got {type(e).__name__}: {e}")


def test_helper_catches_string_surrogate():
    msg = assert_raises(_fileops._validate_no_surrogates, SURROGATE, "/test/path")
    assert "/test/path" in msg, f"Missing path in error: {msg!r}"


def test_helper_catches_dict_value_surrogate():
    payload = {"id": "g-test", "title": f"prefix{SURROGATE}suffix"}
    assert_raises(_fileops._validate_no_surrogates, payload, "/test/path")


def test_helper_catches_dict_key_surrogate():
    payload = {f"key{SURROGATE}": "value"}
    assert_raises(_fileops._validate_no_surrogates, payload, "/test/path")


def test_helper_catches_list_element_surrogate():
    payload = ["clean", f"dirty{SURROGATE}", "clean"]
    assert_raises(_fileops._validate_no_surrogates, payload, "/test/path")


def test_helper_catches_nested_dict_in_list():
    payload = {"items": [{"name": "ok"}, {"name": f"bad{SURROGATE}"}]}
    assert_raises(_fileops._validate_no_surrogates, payload, "/test/path")


def test_helper_passes_clean_strings():
    # ASCII
    assert_no_raise(_fileops._validate_no_surrogates, "hello", "/test/path")
    # Em-dash
    assert_no_raise(_fileops._validate_no_surrogates, f"a {EM_DASH} b", "/test/path")
    # Smart quotes
    assert_no_raise(_fileops._validate_no_surrogates, f"{SMART_QUOTE}quoted{SMART_QUOTE}", "/test/path")
    # Emoji
    assert_no_raise(_fileops._validate_no_surrogates, f"happy {EMOJI}", "/test/path")
    # Empty
    assert_no_raise(_fileops._validate_no_surrogates, "", "/test/path")
    # Mixed clean dict
    assert_no_raise(
        _fileops._validate_no_surrogates,
        {"id": "g-001", "title": f"{EM_DASH}-prefixed", "tags": ["a", "b"]},
        "/test/path",
    )


def test_helper_passes_non_strings():
    assert_no_raise(_fileops._validate_no_surrogates, 42, "/test/path")
    assert_no_raise(_fileops._validate_no_surrogates, 3.14, "/test/path")
    assert_no_raise(_fileops._validate_no_surrogates, True, "/test/path")
    assert_no_raise(_fileops._validate_no_surrogates, None, "/test/path")


def test_env_kill_switch_disables_gate():
    os.environ["FILEOPS_SURROGATE_GATE"] = "off"
    try:
        # Surrogate that would normally raise — should pass through
        assert_no_raise(_fileops._validate_no_surrogates, SURROGATE, "/test/path")
    finally:
        os.environ.pop("FILEOPS_SURROGATE_GATE", None)


def test_locked_append_jsonl_rejects_before_write():
    """Critical: gate must fire BEFORE the file is touched. If a corrupted
    payload reaches disk and the lock is held, recovery is harder."""
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "out.jsonl"
        bad_item = {"id": "g-bad", "text": f"corrupt{SURROGATE}data"}
        try:
            _fileops.locked_append_jsonl(target, bad_item)
        except ValueError as e:
            assert "FILEOPS_SURROGATE_GATE" in str(e)
        else:
            raise AssertionError("locked_append_jsonl did not raise on surrogate payload")
        # File must NOT exist (gate fired before any open() call)
        assert not target.exists(), f"File created despite gate: {target}"
        # Lock file must NOT exist (gate fired before acquire_lock)
        lock_path = target.with_suffix(".lock")
        assert not lock_path.exists(), f"Lock file leaked: {lock_path}"


def test_locked_append_jsonl_accepts_clean_unicode():
    """Round-trip test: clean unicode (em-dash, smart quotes, emoji) must
    write and read back identically."""
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "out.jsonl"
        good_item = {
            "id": "g-clean",
            "title": f"contains {EM_DASH} and {SMART_QUOTE}quotes{SMART_QUOTE} and {EMOJI}",
            "tags": ["a", "b"],
            "count": 42,
        }
        _fileops.locked_append_jsonl(target, good_item)
        assert target.exists(), "File not written"
        # Round-trip: read back, parse, compare
        line = target.read_text(encoding="utf-8").strip()
        round_tripped = json.loads(line)
        assert round_tripped == good_item, f"Round-trip mismatch:\n  in:  {good_item!r}\n  out: {round_tripped!r}"


def test_locked_write_jsonl_rejects_before_write():
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "out.jsonl"
        items = [
            {"id": "g-1", "text": "clean"},
            {"id": "g-2", "text": f"bad{SURROGATE}"},
        ]
        try:
            _fileops.locked_write_jsonl(target, items)
        except ValueError as e:
            assert "FILEOPS_SURROGATE_GATE" in str(e)
        else:
            raise AssertionError("locked_write_jsonl did not raise on surrogate payload")
        assert not target.exists(), f"File created despite gate: {target}"


def test_locked_write_json_rejects_before_write():
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "out.json"
        bad_data = {"key": f"val{SURROGATE}"}
        try:
            _fileops.locked_write_json(target, bad_data)
        except ValueError as e:
            assert "FILEOPS_SURROGATE_GATE" in str(e)
        else:
            raise AssertionError("locked_write_json did not raise on surrogate payload")
        assert not target.exists(), f"File created despite gate: {target}"


def test_locked_write_yaml_rejects_before_write():
    """: surrogate-laced YAML payload must fail at the gate, not on disk."""
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "out.yaml"
        bad_data = {"key": f"val{SURROGATE}"}
        try:
            _fileops.locked_write_yaml(target, bad_data)
        except ValueError as e:
            assert "FILEOPS_SURROGATE_GATE" in str(e)
        else:
            raise AssertionError("locked_write_yaml did not raise on surrogate payload")
        assert not target.exists(), f"File created despite gate: {target}"
        # Lock file must NOT exist (gate fired before acquire_lock — same
        # invariant test_locked_append_jsonl_rejects_before_write asserts).
        lock_path = target.with_suffix(".lock")
        assert not lock_path.exists(), f"Lock file leaked: {lock_path}"


def test_locked_write_yaml_rejects_dict_key_surrogate():
    """: surrogates in YAML KEYS (not just values) must also be caught.
    YAML allows arbitrary string keys; the walker must visit dict.keys() too."""
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "out.yaml"
        bad_data = {f"key{SURROGATE}": "value"}
        try:
            _fileops.locked_write_yaml(target, bad_data)
        except ValueError as e:
            assert "FILEOPS_SURROGATE_GATE" in str(e)
        else:
            raise AssertionError("locked_write_yaml did not raise on surrogate KEY")
        assert not target.exists(), f"File created despite gate: {target}"


def test_locked_write_yaml_accepts_clean_unicode():
    """: clean unicode (em-dash + smart quotes + emoji + nested dict)
    must round-trip identically through locked_write_yaml + yaml.safe_load."""
    import yaml
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "out.yaml"
        good_data = {
            "id": "g-clean",
            "title": f"contains {EM_DASH} and {SMART_QUOTE}quotes{SMART_QUOTE} and {EMOJI}",
            "nested": {"sub_key": f"more {EM_DASH} text"},
            "list_field": ["a", f"b{EM_DASH}c", "d"],
            "count": 42,
        }
        _fileops.locked_write_yaml(target, good_data)
        assert target.exists(), "File not written"
        # Round-trip: read back, parse, compare
        with open(target, "r", encoding="utf-8") as f:
            round_tripped = yaml.safe_load(f)
        assert round_tripped == good_data, (
            f"Round-trip mismatch:\n  in:  {good_data!r}\n  out: {round_tripped!r}"
        )


def test_locked_modify_yaml_rejects_surrogate_in_modifier_return():
    """: when modifier_fn returns a dict with a surrogate-laced
    string, the gate must raise BEFORE yaml.dump runs. Validation is
    post-modifier (inside the lock) because new_data does not exist
    until modifier_fn runs — distinct from locked_write_yaml's
    pre-lock validation, but same fail-loud guarantee."""
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "store.yaml"

        def bad_modifier(data):
            data["corrupted"] = f"value{SURROGATE}"
            return data

        try:
            _fileops.locked_modify_yaml(target, bad_modifier, initial={})
        except ValueError as e:
            assert "FILEOPS_SURROGATE_GATE" in str(e)
        else:
            raise AssertionError(
                "locked_modify_yaml did not raise on surrogate-laced "
                "modifier_fn return"
            )
        # File MUST NOT exist — gate fired before yaml.dump and tmp-rename.
        assert not target.exists(), f"File created despite gate: {target}"
        # Lock file MUST NOT leak — finally: release_lock cleans up even on raise.
        lock_path = target.with_suffix(".lock")
        assert not lock_path.exists(), f"Lock file leaked: {lock_path}"


def test_locked_modify_yaml_accepts_clean_unicode():
    """: modifier_fn returning clean unicode (em-dash + smart
    quotes + emoji + nested) must round-trip through yaml.dump + safe_load."""
    import yaml
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "store.yaml"

        def good_modifier(data):
            data["id"] = "g-clean"
            data["title"] = f"contains {EM_DASH} and {SMART_QUOTE}quotes{SMART_QUOTE} and {EMOJI}"
            data["nested"] = {"sub_key": f"more {EM_DASH} text"}
            data["list_field"] = ["a", f"b{EM_DASH}c", "d"]
            data["count"] = 42
            return data

        result = _fileops.locked_modify_yaml(target, good_modifier, initial={})
        assert target.exists(), "File not written"
        with open(target, "r", encoding="utf-8") as f:
            round_tripped = yaml.safe_load(f)
        assert round_tripped == result, (
            f"Round-trip mismatch:\n  modifier_returned: {result!r}\n  on_disk: {round_tripped!r}"
        )
        # Sanity: clean unicode preserved exactly through yaml.dump.
        assert EM_DASH in round_tripped["title"]
        assert EMOJI in round_tripped["title"]


def test_canonical_2026_05_07_byte_signature_caught():
    """The exact 0xed 0xb2 0x9d byte sequence that crashed alpha/aspirations.jsonl
    today decodes to U+DC9D — confirm the gate would have caught it."""
    # This is the exact byte sequence that crashed precheck Phase 0 today.
    # If decoded as utf-8 with errors='surrogateescape' it produces a single
    # low-surrogate U+DC9D. Construct it directly:
    crash_signature = "\udc9d"
    payload = {"description": f" - {crash_signature}corrupted"}
    msg = assert_raises(_fileops._validate_no_surrogates, payload, "alpha/aspirations.jsonl")
    assert "alpha/aspirations.jsonl" in msg


# ─────────────────────────────────────────────────────────────────────────────
# Test runner

if __name__ == "__main__":
    tests = [
        ("helper_catches_string_surrogate", test_helper_catches_string_surrogate),
        ("helper_catches_dict_value_surrogate", test_helper_catches_dict_value_surrogate),
        ("helper_catches_dict_key_surrogate", test_helper_catches_dict_key_surrogate),
        ("helper_catches_list_element_surrogate", test_helper_catches_list_element_surrogate),
        ("helper_catches_nested_dict_in_list", test_helper_catches_nested_dict_in_list),
        ("helper_passes_clean_strings", test_helper_passes_clean_strings),
        ("helper_passes_non_strings", test_helper_passes_non_strings),
        ("env_kill_switch_disables_gate", test_env_kill_switch_disables_gate),
        ("locked_append_jsonl_rejects_before_write", test_locked_append_jsonl_rejects_before_write),
        ("locked_append_jsonl_accepts_clean_unicode", test_locked_append_jsonl_accepts_clean_unicode),
        ("locked_write_jsonl_rejects_before_write", test_locked_write_jsonl_rejects_before_write),
        ("locked_write_json_rejects_before_write", test_locked_write_json_rejects_before_write),
        ("locked_write_yaml_rejects_before_write", test_locked_write_yaml_rejects_before_write),
        ("locked_write_yaml_rejects_dict_key_surrogate", test_locked_write_yaml_rejects_dict_key_surrogate),
        ("locked_write_yaml_accepts_clean_unicode", test_locked_write_yaml_accepts_clean_unicode),
        ("locked_modify_yaml_rejects_surrogate_in_modifier_return", test_locked_modify_yaml_rejects_surrogate_in_modifier_return),
        ("locked_modify_yaml_accepts_clean_unicode", test_locked_modify_yaml_accepts_clean_unicode),
        ("canonical_2026_05_07_byte_signature_caught", test_canonical_2026_05_07_byte_signature_caught),
    ]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as e:
            failures += 1
            print(f"  FAIL  {name}: {e}")
            traceback.print_exc()
    if failures:
        print(f"\n{failures}/{len(tests)} tests failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} tests passed")
