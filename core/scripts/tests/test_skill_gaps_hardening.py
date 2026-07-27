"""test_skill_gaps_hardening.py —  regression pins.

Covers the meta-yaml.py list-value/dotpath hardening + the skill-gaps schema
validator that together make the gaps-as-string + orphan-key corruption class
unable to recur:

  - parse_value: a JSON list/dict value round-trips instead of being stored as
    a literal string (gaps-as-string fix).
  - navigate: bracket-index notation (gaps[N]) routes to a list index instead
    of creating a literal sibling key (orphan-key fix).
  - cmd_set: index == len(list) appends (the real list-overlay behavior the
    orphan-key bug faked).
  - skill-gaps-validate: detects each corruption class as a detective net.

Pure stdlib + importlib (loads the hyphen-named scripts as modules). Hermetic:
parse_value / navigate / cmd_set run in-process with the I/O boundary
monkeypatched, so nothing touches the live meta/ or world/ directories
(guard-652 — no live MIND_WORLD/MIND_META coupling; no subprocess).
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent          # core/scripts
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))  # so meta-yaml's `from _paths import` resolves


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(modname, SCRIPTS_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Loaded once (each exec runs meta-yaml's reconfigure_stdio — keep it to one call).
_MY = _load("meta_yaml_mod", "meta-yaml.py")
_VAL = _load("skill_gaps_validate_mod", "skill-gaps-validate.py")


# ---------------------------------------------------------------------------
# parse_value — JSON list/dict round-trip (gaps-as-string fix)
# ---------------------------------------------------------------------------

def test_parse_value_json_list_and_dict():
    assert _MY.parse_value("[1, 2, 3]") == [1, 2, 3]
    assert _MY.parse_value('["a", "b"]') == ["a", "b"]
    assert _MY.parse_value('{"id": "gap-007"}') == {"id": "gap-007"}
    assert _MY.parse_value('[{"id": "x"}]') == [{"id": "x"}]


def test_parse_value_scalars_unchanged():
    assert _MY.parse_value("null") is None
    assert _MY.parse_value("true") is True
    assert _MY.parse_value("false") is False
    assert _MY.parse_value("42") == 42
    assert _MY.parse_value("3.14") == 3.14
    assert _MY.parse_value("hello") == "hello"


def test_parse_value_invalid_json_falls_through_to_string():
    # Looks like JSON but isn't valid -> stays a string (no crash).
    assert _MY.parse_value("[draft] needs review") == "[draft] needs review"
    assert _MY.parse_value("{unquoted: bad}") == "{unquoted: bad}"
    assert _MY.parse_value("[") == "["


# ---------------------------------------------------------------------------
# navigate — bracket-index normalization (orphan-key fix)
# ---------------------------------------------------------------------------

def test_navigate_bracket_index_routes_to_list():
    data = {"gaps": [{"id": "gap-001"}, {"id": "gap-002"}]}
    parent, key = _MY.navigate(data, "gaps[1].id")
    assert parent is data["gaps"][1]
    assert key == "id"
    parent, key = _MY.navigate(data, "gaps[1]")
    assert parent is data["gaps"]
    assert key == 1


def test_navigate_dotted_numeric_still_works():
    data = {"gaps": [{"id": "gap-001"}]}
    parent, key = _MY.navigate(data, "gaps.0.id")
    assert parent is data["gaps"][0]
    assert key == "id"


def test_navigate_append_position_returns_len():
    data = {"gaps": [{"id": "gap-001"}, {"id": "gap-002"}]}
    parent, key = _MY.navigate(data, "gaps[2]")   # index == len -> append slot
    assert parent is data["gaps"]
    assert key == 2


# ---------------------------------------------------------------------------
# cmd_set — list-index append + list-value round-trip (real code, stubbed I/O)
# ---------------------------------------------------------------------------

def _run_cmd_set(initial: dict, dotpath: str, value: str, string: bool = False) -> dict:
    """Drive the real cmd_set with the I/O boundary stubbed; return written dict."""
    captured: dict = {}
    orig = (_MY.read_yaml, _MY.write_yaml, _MY.resolve_path, _MY.append_log)
    _MY.read_yaml = lambda path: initial
    _MY.write_yaml = lambda path, data: captured.update(data=data)
    _MY.resolve_path = lambda rel: Path(rel)
    _MY.append_log = lambda *a, **k: "mc-test"
    try:
        args = types.SimpleNamespace(
            file="skill-gaps.yaml", dotpath=dotpath, value=value,
            string=string, reason="g-115-1263 test",
        )
        _MY.cmd_set(args)
    finally:
        _MY.read_yaml, _MY.write_yaml, _MY.resolve_path, _MY.append_log = orig
    return captured["data"]


def test_cmd_set_appends_via_bracket_index():
    out = _run_cmd_set(
        {"gaps": [{"id": "gap-001"}, {"id": "gap-002"}]},
        "gaps[2]", '{"id": "gap-003", "status": "registered"}',
    )
    gaps = out["gaps"]
    assert isinstance(gaps, list) and len(gaps) == 3
    assert gaps[2] == {"id": "gap-003", "status": "registered"}
    assert "gaps[2]" not in out  # the orphan key MUST NOT exist


def test_cmd_set_list_value_round_trips_as_list():
    out = _run_cmd_set({}, "years", "[2025, 2026]")
    assert out["years"] == [2025, 2026]
    assert not isinstance(out["years"], str)


def test_cmd_set_string_flag_forces_literal():
    out = _run_cmd_set({}, "note", "[2025, 2026]", string=True)
    assert out["note"] == "[2025, 2026]"  # --string keeps it verbatim


# ---------------------------------------------------------------------------
# skill-gaps-validate — detective layer catches each corruption class
# ---------------------------------------------------------------------------

def test_validator_passes_valid():
    data = {"last_updated": "2026-05-27",
            "gaps": [{"id": "gap-001"}, {"id": "gap-002"}]}
    assert _VAL.validate(data) == []


def test_validator_catches_gaps_as_string():
    issues = _VAL.validate({"gaps": "[{'id': 'gap-001'}]"})
    assert any("STRING" in i for i in issues)


def test_validator_catches_orphan_key():
    issues = _VAL.validate({"gaps": [{"id": "gap-001"}], "gaps[1]": {"id": "gap-002"}})
    assert any("orphan key" in i for i in issues)


def test_validator_catches_duplicate_id():
    issues = _VAL.validate({"gaps": [{"id": "gap-001"}, {"id": "gap-001"}]})
    assert any("duplicate gap id" in i for i in issues)


def test_validator_catches_missing_id_and_nonmap_entry():
    issues = _VAL.validate({"gaps": [{"no_id": 1}, "not-a-mapping"]})
    assert any("missing a non-empty string 'id'" in i for i in issues)
    assert any("expected a mapping" in i for i in issues)


def test_validator_catches_missing_gaps_key():
    issues = _VAL.validate({"last_updated": "2026-05-27"})
    assert any("missing top-level 'gaps'" in i for i in issues)


# ---------------------------------------------------------------------------
# Aggregate runner (mirrors the repo's def-main test convention)
# ---------------------------------------------------------------------------

def main() -> int:
    tests = [obj for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL: {t.__name__}: {e}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR: {t.__name__}: {type(e).__name__}: {e}", file=sys.stderr)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
