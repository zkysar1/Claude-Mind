"""Pins the confidence sort in build-agent-context.py against non-numeric values.

THE DEFECT. build_context() sorted the matched tree nodes with
`kn[1].get("confidence", 0)`. The default fires only when the key is ABSENT, and
_tree.yaml holds nodes whose confidence is an explicit null and one whose
confidence is the string 'high'. One such node among the matches made the
None/str-vs-float comparison raise TypeError, so the mandated spawn-context
builder (agent-spawning.md, guard-323) crashed for every category that matched
it. MEASURED 2026-09-27 (alpha, hostname cc-04, uname -r 6.8.0-142-generic):
1667 nodes, 548 float, 1111 absent, 7 null, 1 str; `--category framework`
matched 2 of the null nodes and exited 1 with that TypeError. Class: guard-1512.

This drives the REAL build_context over a fixture tree rather than testing the
key in isolation, because a test that stubs the walk leaves the walk untested
(guard-1512's corollary). guard-955: pinned local so no case can reach a
production store.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest
import yaml

os.environ["STORAGE_BACKEND"] = "local"

_SCRIPTS = Path(__file__).resolve().parents[1]
_TARGET = _SCRIPTS / "build-agent-context.py"

# Every node matches the query category through its key.
_NODES = {
    "widget-framework-numeric": {"summary": "numeric", "confidence": 0.9},
    "widget-framework-null": {"summary": "explicit null", "confidence": None},
    "widget-framework-string": {"summary": "string", "confidence": "high"},
    "widget-framework-absent": {"summary": "no confidence key"},
    "widget-framework-low": {"summary": "numeric low", "confidence": 0.2},
}


def _load():
    """Import the hyphenated script as a module (no package path exists)."""
    sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location("_bac_conf_under_test", _TARGET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def bac(tmp_path, monkeypatch):
    mod = _load()
    tree = tmp_path / "_tree.yaml"
    tree.write_text(yaml.safe_dump({"nodes": _NODES, "entity_index": {}}), encoding="utf-8")
    monkeypatch.setattr(mod, "TREE_PATH", tree)
    monkeypatch.setattr(mod, "RB_PATH", tmp_path / "absent-rb.jsonl")
    monkeypatch.setattr(mod, "GUARD_PATH", tmp_path / "absent-guards.jsonl")
    monkeypatch.setattr(mod, "WORLD_DIR", tmp_path)
    monkeypatch.setattr(mod, "AGENT_DIR", tmp_path)
    return mod


def _knowledge_keys(result):
    """Node keys in the order the KNOWLEDGE section rendered them."""
    lines = result.split("\n")
    start = next(i for i, l in enumerate(lines) if "KNOWLEDGE (" in l)
    keys = []
    for line in lines[start + 1:]:
        if not line.startswith("- ["):
            break
        keys.append(line[3:line.index("]")])
    return keys


def test_non_numeric_confidence_does_not_crash(bac):
    result = bac.build_context(["widget-framework"], "researcher", None, 100_000)
    assert set(_knowledge_keys(result)) == set(_NODES), (
        "every matched node must still render; a malformed confidence is sorted, "
        "never dropped"
    )


def test_numeric_confidence_orders_first(bac):
    keys = _knowledge_keys(
        bac.build_context(["widget-framework"], "researcher", None, 100_000))
    assert keys[:2] == ["widget-framework-numeric", "widget-framework-low"], keys
    assert set(keys[2:]) == {
        "widget-framework-null", "widget-framework-string", "widget-framework-absent",
    }, "non-numeric and absent confidences sort as 0, below every real value"


def test_pre_fix_key_raises_on_this_fixture():
    """POSITIVE CONTROL: the fixture must reproduce the crash under the old key.

    Without it, a fixture that never mixed None/str with floats would pass the
    tests above while telling us nothing about the defect.
    """
    matched = list(_NODES.items())
    with pytest.raises(TypeError):
        matched.sort(key=lambda kn: kn[1].get("confidence", 0), reverse=True)
