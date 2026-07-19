"""Tests for knowledge-export — the store-reader that bridges the pure
:mod:`knowledge_projection` core to the real Mind stores.

Hermetic: every test builds a tiny temp world (tree yaml + 3 JSONL) and a fake
project root, so nothing depends on the box's actual stores. The security
properties (framework suppression, agent-name / path / secret redaction) are
re-asserted end-to-end through the reader, because that is where store-shape
mistakes would re-leak what the pure core already knows to strip.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
EXPORT_PY = SCRIPT_DIR.parent / "knowledge-export.py"


def _load_mod():
    spec = importlib.util.spec_from_file_location("knowledge_export_under_test", EXPORT_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load_mod()


# ── fixture builder ──────────────────────────────────────────────────────────

def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _build_world(root: Path) -> Path:
    """Create a minimal but realistic world/ tree + three JSONL stores under root."""
    world = root / "world"
    tree_dir = world / "knowledge" / "tree"
    tree_dir.mkdir(parents=True)
    (root / "agents" / "alpha").mkdir(parents=True)  # → agent name "alpha"

    tree = {
        "nodes": {
            "coral-reefs": {
                "summary": "Reefs studied by alpha at /home/ec2/world data.",
                "parent": "marine-biology",
                "children": ["bleaching"],
                "file": "world/knowledge/tree/marine-biology/coral-reefs.md",
            },
            "9-hook-internals": {  # leading digit exercises _humanize_key
                "summary": "framework plumbing per guard-321",
                "parent": "system",
                "children": [],
                "file": "world/knowledge/tree/system/hook-internals.md",
            },
        }
    }
    (tree_dir / "_tree.yaml").write_text(yaml.safe_dump(tree), encoding="utf-8")

    _write_jsonl(
        world / "reasoning-bank.jsonl",
        [
            {"applies_to": "domain", "category": "marine-biology/method",
             "title": "Cross-check sources",
             "failure_lesson": "Trusted one source once; it was wrong."},
            {"applies_to": "framework", "category": "framework-architecture",
             "title": "Never inline $VAR",
             "failure_lesson": "guard-165 plumbing detail in _paths.py"},
        ],
    )
    _write_jsonl(
        world / "guardrails.jsonl",
        [
            {"category": "marine-biology/method", "rule": "Verify every claim against two sources."},
            {"category": "framework-architecture", "rule": "Never critical() in a handler."},
            {"rule": "Untagged guardrail — must fail closed."},  # no category
        ],
    )
    _write_jsonl(
        world / "pipeline.jsonl",
        [
            {"category": "marine-biology/reefs", "claim": "Warmer water bleaches reefs faster.",
             "horizon": "short", "stage": "resolved", "outcome": "Confirmed by alpha."},
            {"category": "system/loop", "claim": "The veto budget bounds continuations.",
             "horizon": "session", "stage": "active", "outcome": ""},
        ],
    )
    return world


# ── store-reader unit helpers ────────────────────────────────────────────────

def test_read_jsonl_skips_blank_and_bad_lines(tmp_path: Path) -> None:
    p = tmp_path / "s.jsonl"
    p.write_text('{"a": 1}\n\n  \nnot json\n{"b": 2}\n[1,2]\n', encoding="utf-8")
    assert M._read_jsonl(p) == [{"a": 1}, {"b": 2}]  # bad line + non-dict [1,2] dropped


def test_read_jsonl_missing_file_is_empty(tmp_path: Path) -> None:
    assert M._read_jsonl(tmp_path / "nope.jsonl") == []


def test_humanize_key_strips_leading_number_and_dashes() -> None:
    assert M._humanize_key("9-action-matrix-design") == "Action matrix design"
    assert M._humanize_key("coral-reefs") == "Coral reefs"
    assert M._humanize_key("deep_sea") == "Deep sea"
    assert M._humanize_key("") == ""


def test_read_tree_nodes_shapes_records(tmp_path: Path) -> None:
    world = _build_world(tmp_path)
    nodes = {n["key"]: n for n in M.read_tree_nodes(world)}
    assert set(nodes) == {"coral-reefs", "9-hook-internals"}
    reef = nodes["coral-reefs"]
    assert reef["title"] == "Coral reefs"
    assert reef["parent"] == "marine-biology"
    assert reef["children"] == ["bleaching"]
    # category carries the file path so the projection's system/-suppression works.
    assert reef["category"] == "world/knowledge/tree/marine-biology/coral-reefs.md"


def test_read_tree_nodes_missing_yaml_is_empty(tmp_path: Path) -> None:
    assert M.read_tree_nodes(tmp_path / "world") == []


def test_agent_names_lists_agent_dirs(tmp_path: Path) -> None:
    _build_world(tmp_path)
    assert M._agent_names(tmp_path) == ["alpha"]


def test_secret_values_selects_by_suffix_and_length() -> None:
    env = {
        "MY_API_KEY": "longsecretvalue123",   # selected
        "SESSION_TOKEN": "anothersecret456",  # selected
        "SHORT_KEY": "abc",                   # too short (<8) — excluded
        "PLAIN_NAME": "notasecretatall",      # wrong suffix — excluded
    }
    got = set(M._secret_values(env))
    assert got == {"longsecretvalue123", "anothersecret456"}


# ── build_bundle integration (security-critical) ─────────────────────────────

def test_build_bundle_filters_and_redacts(tmp_path: Path) -> None:
    world = _build_world(tmp_path)
    bundle = M.build_bundle(world, tmp_path, env={})

    # Exactly the domain entries survive; framework rows are gone.
    assert bundle.counts() == {"tree": 1, "hypotheses": 1, "guardrails": 1, "lessons": 1}

    node = bundle.tree[0]
    assert node["key"] == "coral-reefs"
    # Redaction ran through the reader: agent name + absolute path stripped.
    assert "alpha" not in node["summary"].lower()
    assert "/home/ec2" not in node["summary"]
    assert "the agent" in node["summary"]

    assert bundle.hypotheses[0]["horizon"] == "short"
    assert "alpha" not in bundle.hypotheses[0]["outcome"].lower()
    assert bundle.guardrails[0]["rule"].startswith("Verify every claim")
    assert "Cross-check" in bundle.lessons[0]["title"]


def test_build_bundle_never_leaks_framework_subtree(tmp_path: Path) -> None:
    world = _build_world(tmp_path)
    bundle = M.build_bundle(world, tmp_path, env={})
    # The system tree node, framework guardrail, untagged guardrail, and system
    # hypothesis are all absent.
    assert all("system" not in str(t.get("parent")) for t in bundle.tree)
    rules = [g["rule"] for g in bundle.guardrails]
    assert not any("fail closed" in r for r in rules)
    assert not any("critical()" in r for r in rules)
    assert all(h["status"] != "active" for h in bundle.hypotheses)


def test_build_bundle_redacts_env_secret_values(tmp_path: Path) -> None:
    world = _build_world(tmp_path)
    secret = "topsecrettoken0xdeadbeef"
    # Inject the secret into an exposed lesson, then confirm the reader strips it
    # when that same value is present as an env secret.
    _write_jsonl(
        world / "reasoning-bank.jsonl",
        [{"applies_to": "domain", "category": "marine-biology/note", "title": "Note",
          "failure_lesson": f"the key was {secret} — never show it"}],
    )
    bundle = M.build_bundle(world, tmp_path, env={"SOME_API_TOKEN": secret})
    lesson = bundle.lessons[0]["lesson"]
    assert secret not in lesson
    assert "[redacted]" in lesson


def test_build_bundle_empty_world_is_empty_bundle(tmp_path: Path) -> None:
    (tmp_path / "world").mkdir()
    bundle = M.build_bundle(tmp_path / "world", tmp_path, env={})
    assert bundle.counts() == {"tree": 0, "hypotheses": 0, "guardrails": 0, "lessons": 0}


# ── OKF markdown bundle (PEARL §10.5) ────────────────────────────────────────

def _fm(text: str) -> dict:
    """Parse the ---fenced YAML frontmatter of an OKF concept file."""
    assert text.startswith("---\n")
    _, fm, _body = text.split("---\n", 2)
    return yaml.safe_load(fm)


def test_okf_bundle_layout_and_required_type(tmp_path: Path) -> None:
    world = _build_world(tmp_path)
    bundle = M.build_bundle(world, tmp_path, env={})
    out = tmp_path / "okf"
    counts = M.write_okf_bundle(bundle, out)

    assert (out / "index.md").is_file()
    # One wiki article per exposed node; each carries the required `type` discriminator.
    node_files = list((out / "nodes").glob("*.md"))
    assert len(node_files) == counts["tree"] == 1
    fm = _fm(node_files[0].read_text(encoding="utf-8"))
    assert fm["type"] == "node"  # the one REQUIRED OKF frontmatter key
    assert fm["key"] == "coral-reefs"
    # Hypotheses, guardrails, lessons each become their own typed concept files.
    assert _fm(next((out / "hypotheses").glob("*.md")).read_text(encoding="utf-8"))["type"] == "hypothesis"
    assert _fm(next((out / "guardrails").glob("*.md")).read_text(encoding="utf-8"))["type"] == "guardrail"
    assert _fm(next((out / "lessons").glob("*.md")).read_text(encoding="utf-8"))["type"] == "lesson"


def test_okf_bundle_carries_redaction_into_markdown(tmp_path: Path) -> None:
    world = _build_world(tmp_path)
    bundle = M.build_bundle(world, tmp_path, env={})
    out = tmp_path / "okf"
    M.write_okf_bundle(bundle, out)
    body = next((out / "nodes").glob("*.md")).read_text(encoding="utf-8")
    # The reef node's summary named alpha + a path — the markdown must not leak them.
    assert "alpha" not in body.lower()
    assert "/home/ec2" not in body
    assert "the agent" in body


def test_okf_index_links_resolve_to_written_files(tmp_path: Path) -> None:
    world = _build_world(tmp_path)
    bundle = M.build_bundle(world, tmp_path, env={})
    out = tmp_path / "okf"
    M.write_okf_bundle(bundle, out)
    index = (out / "index.md").read_text(encoding="utf-8")
    # Every nodes/<stem>.md link in the index points at a file that exists (invariant 6
    # allows dangling, but our own article links must resolve).
    import re as _re
    for rel in _re.findall(r"\]\((nodes/[^)]+\.md)\)", index):
        assert (out / rel).is_file(), f"index links missing file {rel}"


def test_okf_empty_bundle_writes_index_and_empty_dirs(tmp_path: Path) -> None:
    (tmp_path / "world").mkdir()
    bundle = M.build_bundle(tmp_path / "world", tmp_path, env={})
    out = tmp_path / "okf"
    counts = M.write_okf_bundle(bundle, out)
    assert counts == {"tree": 0, "hypotheses": 0, "guardrails": 0, "lessons": 0}
    assert (out / "index.md").is_file()
    assert list((out / "nodes").glob("*.md")) == []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
