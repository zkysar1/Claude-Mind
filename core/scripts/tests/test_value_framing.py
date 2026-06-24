"""Tests for _value_framing.py (FW-5 / R2, ).

Pins BOTH sides of the helper+YAML pair (rb-1915): the resolver logic AND the
shape/coverage of value-framing-mapping.yaml, since the journal `Value:` line
depends on every (outcome_class, work_class) pair resolving to a non-empty,
ASCII-safe sentence.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent  # core/scripts/tests
CORE_SCRIPTS = SCRIPT_DIR.parent  # core/scripts
sys.path.insert(0, str(CORE_SCRIPTS))

import _value_framing  # noqa: E402

MAPPING_PATH = CORE_SCRIPTS.parent / "config" / "value-framing-mapping.yaml"

OUTCOME_CLASSES = ("routine", "deep")
WORK_CLASSES = ("product", "framework", "hygiene", "research")


# ---------------------------------------------------------------------------
# Resolver logic
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("oc", OUTCOME_CLASSES)
@pytest.mark.parametrize("wc", WORK_CLASSES + ("unclassified",))
def test_known_pairs_return_nonempty(oc, wc):
    out = _value_framing.resolve(oc, wc)
    assert isinstance(out, str) and out.strip(), f"empty framing for ({oc},{wc})"


def test_specific_framings_match_yaml():
    """The resolver returns exactly what the YAML declares (no drift)."""
    data = yaml.safe_load(MAPPING_PATH.read_text(encoding="utf-8"))
    mapping = data["mapping"]
    for oc in OUTCOME_CLASSES:
        for wc, expected in mapping[oc].items():
            assert _value_framing.resolve(oc, wc) == expected


def test_empty_work_class_falls_to_unclassified():
    """work_class empty/None -> the outcome_class's `unclassified` framing."""
    data = yaml.safe_load(MAPPING_PATH.read_text(encoding="utf-8"))
    for oc in OUTCOME_CLASSES:
        expected = data["mapping"][oc]["unclassified"]
        assert _value_framing.resolve(oc, "") == expected
        assert _value_framing.resolve(oc, None) == expected


def test_unknown_work_class_falls_to_unclassified():
    data = yaml.safe_load(MAPPING_PATH.read_text(encoding="utf-8"))
    for oc in OUTCOME_CLASSES:
        expected = data["mapping"][oc]["unclassified"]
        assert _value_framing.resolve(oc, "no-such-class") == expected


def test_unknown_outcome_class_returns_default():
    default = yaml.safe_load(MAPPING_PATH.read_text(encoding="utf-8"))["default"]
    assert _value_framing.resolve("weird", "framework") == default
    assert _value_framing.resolve("weird", "") == default


def test_empty_outcome_class_returns_default():
    default = yaml.safe_load(MAPPING_PATH.read_text(encoding="utf-8"))["default"]
    assert _value_framing.resolve("", "framework") == default
    assert _value_framing.resolve(None, None) == default


def test_resolve_never_returns_empty():
    """Every reachable input yields a non-empty string (the Value: line guard)."""
    for oc in OUTCOME_CLASSES + ("", None, "weird"):
        for wc in WORK_CLASSES + ("", None, "unclassified", "no-such"):
            out = _value_framing.resolve(oc, wc)
            assert isinstance(out, str) and out.strip()


# ---------------------------------------------------------------------------
# YAML shape / coverage (pin the data, rb-1915)
# ---------------------------------------------------------------------------

def test_mapping_yaml_well_formed():
    data = yaml.safe_load(MAPPING_PATH.read_text(encoding="utf-8"))
    assert isinstance(data.get("default"), str) and data["default"].strip()
    mapping = data["mapping"]
    for oc in OUTCOME_CLASSES:
        assert oc in mapping, f"missing outcome_class {oc}"
        oc_map = mapping[oc]
        # every tracked work_class + the unclassified fallback must be present
        for wc in WORK_CLASSES + ("unclassified",):
            assert wc in oc_map, f"missing ({oc},{wc}) framing"
            assert isinstance(oc_map[wc], str) and oc_map[wc].strip()


def test_framings_are_ascii_safe():
    """guard-549: framings flow through bash echo + a bash-captured CLI on
    Windows MSYS, where a multi-byte em-dash can mangle. Keep them ASCII."""
    data = yaml.safe_load(MAPPING_PATH.read_text(encoding="utf-8"))
    strings = [data["default"]]
    for oc_map in data["mapping"].values():
        strings.extend(oc_map.values())
    for s in strings:
        assert s.isascii(), f"non-ASCII framing breaks the bash pipe: {s!r}"


# ---------------------------------------------------------------------------
# CLI parity
# ---------------------------------------------------------------------------

def test_cli_matches_resolve():
    helper = CORE_SCRIPTS / "_value_framing.py"
    cases = [("routine", "hygiene"), ("deep", "framework"), ("routine", ""), ("weird", "x")]
    for oc, wc in cases:
        argv = [sys.executable, str(helper), oc] + ([wc] if wc else [])
        out = subprocess.run(argv, capture_output=True, text=True, check=True).stdout.strip()
        assert out == _value_framing.resolve(oc, wc or None)


def test_cli_no_args_prints_default():
    helper = CORE_SCRIPTS / "_value_framing.py"
    default = yaml.safe_load(MAPPING_PATH.read_text(encoding="utf-8"))["default"]
    out = subprocess.run([sys.executable, str(helper)], capture_output=True, text=True, check=True).stdout.strip()
    assert out == default
