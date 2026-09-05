"""test_yaml_c_loader.py — the C-scanner YAML loader on the retrieval hot path
(2026-09-03).

tree_match._yaml_loader() returns libyaml's CSafeLoader when the PyYAML wheel
ships it, else SafeLoader. retrieve.read_yaml, tree_match.parse_front_matter
and the daemon's yaml_cache._load all parse through it. Measured on the live
tree: the 1.75 MB index 6.87 s -> 0.82 s and 2,983 node front matters
4.14 s -> 0.54 s, both byte-for-byte EQUAL to the pure-Python result.

Invariants pinned here:
  1. The two loaders produce IDENTICAL objects on the shapes the tree uses —
     CRLF line endings (the Windows boxes), unicode, ISO dates, floats, ints,
     booleans, nulls, nested lists/maps, quoted and block scalars, anchors.
  2. Every hot-path reader goes through the helper (equality against
     yaml.safe_load on the same text).
  3. A box without libyaml falls back to SafeLoader and still parses.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tree_match as TM  # noqa: E402

SAMPLE = (
    "topic: \"Métacognition — survey (arXiv:2607.11881)\"\n"
    "last_updated: '2026-09-03'\n"
    "created: 2026-07-10\n"
    "confidence: 0.78\n"
    "retrieval_count: 12\n"
    "utility_ratio: 0.0\n"
    "flag: true\n"
    "nothing: null\n"
    "entities: [metacognition, monitoring-control, meta-d-prime, calibration]\n"
    "last_update_trigger:\n"
    "  type: research-encode\n"
    "  source: \"User-directed read; commit eef2e1f\"\n"
    "nested:\n"
    "  - a: 1\n"
    "    b: [x, y]\n"
    "  - c: 'quoted: colon'\n"
    "block: |\n"
    "  line one\n"
    "  line two\n"
    "folded: >\n"
    "  folded text\n"
    "  continues\n"
    "anchored: &A {k: v}\n"
    "ref: *A\n"
)


def test_c_loader_preferred_when_available():
    if not getattr(yaml, "__with_libyaml__", False):
        pytest.skip("PyYAML built without libyaml on this box")
    assert TM._yaml_loader() is yaml.CSafeLoader


def test_fallback_when_libyaml_absent(monkeypatch):
    monkeypatch.delattr(yaml, "CSafeLoader", raising=False)
    assert TM._yaml_loader() is yaml.SafeLoader
    assert yaml.load(SAMPLE, Loader=TM._yaml_loader()) == yaml.safe_load(SAMPLE)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_loaders_agree_on_tree_shapes(newline):
    text = SAMPLE.replace("\n", newline)
    assert yaml.load(text, Loader=TM._yaml_loader()) == yaml.safe_load(text)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_parse_front_matter_matches_safe_load(tmp_path, newline):
    md = tmp_path / "node.md"
    body = "---" + newline + SAMPLE.replace("\n", newline) + "---" + newline + "# body" + newline
    md.write_bytes(body.encode("utf-8"))
    assert TM.parse_front_matter(md) == yaml.safe_load(SAMPLE)


def test_read_yaml_matches_safe_load(tmp_path, monkeypatch):
    import retrieve as R
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    p = tmp_path / "index.yaml"
    p.write_text("nodes:\n  k:\n" + "".join("    " + ln + "\n" for ln in SAMPLE.splitlines()),
                 encoding="utf-8")
    assert R.read_yaml(p) == yaml.safe_load(p.read_text(encoding="utf-8"))


def test_daemon_yaml_cache_matches_safe_load(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "mind_api" / "src"))
    import yaml_cache
    p = tmp_path / "cached.yaml"
    p.write_text(SAMPLE, encoding="utf-8")
    assert yaml_cache.YamlCache._load(p) == yaml.safe_load(SAMPLE)
