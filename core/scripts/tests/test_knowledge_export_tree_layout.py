"""Regression tests for the two tree layouts knowledge-export must read ().

The defect: ``read_tree_nodes`` resolved the index at ``<world>/knowledge/tree`` ONLY and
returned ``[]`` silently when it was absent. The mind-sidecar provisioner has written
``<world>/tree`` since 5996fa7 (2026-07-17) and never the conformant path, so 17 of 18
live envs read as empty — and because ``knowledge_projection`` DERIVES its category
allowlist from the exposed tree and fails closed, that empty tree also suppressed every
hypothesis and guardrail sitting at the correct path. One missing input, four zero stores.

``test_empty_conformant_dir_does_not_shadow_populated_sidecar_tree`` is the one that pins
the trap: "just mkdir the conformant path in the provisioner" would have created an empty
directory that shadowed a populated ``<world>/tree`` and re-zeroed every env it touched.
Selection is by presence of the INDEX, never of the directory.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS))
_spec = importlib.util.spec_from_file_location("knowledge_export", _SCRIPTS / "knowledge-export.py")
ke = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(ke)

TREE_YAML = """nodes:
  widget-service-latency:
    file: intelligence/widget-service/latency.md
    summary: One measured note about widget-service latency.
    parent: ''
    children: []
    last_updated: '2026-08-26'
"""
HYPOTHESIS = {
    "id": "2026-08-26_demo",
    "title": "Demo hypothesis",
    "category": "intelligence",
    "stage": "active",
    "position": "YES",
    "confidence": 0.6,
    "formed_date": "2026-08-26",
}


def _plant_tree(world: Path, *parts: str) -> Path:
    tree_dir = world.joinpath(*parts)
    tree_dir.mkdir(parents=True, exist_ok=True)
    (tree_dir / "_tree.yaml").write_text(TREE_YAML, encoding="utf-8")
    node = tree_dir / "intelligence" / "widget-service"
    node.mkdir(parents=True, exist_ok=True)
    (node / "latency.md").write_text("---\ntitle: latency\n---\n\nA note.\n", encoding="utf-8")
    return tree_dir


def _plant_stores(world: Path) -> None:
    (world / "pipeline.jsonl").write_text(json.dumps(HYPOTHESIS) + "\n", encoding="utf-8")


@pytest.mark.parametrize("layout", [("knowledge", "tree"), ("tree",)])
def test_both_layouts_export_the_whole_bundle(tmp_path: Path, layout: tuple[str, ...]) -> None:
    """The framework layout AND the sidecar layout both reach a non-zero bundle.

    The hypothesis assertion is the load-bearing half: it is at the CORRECT path in both
    arms, so a zero there is the derived-allowlist cascade, not a missing store.
    """
    world = tmp_path / "world"
    world.mkdir()
    _plant_tree(world, *layout)
    _plant_stores(world)

    bundle = ke.build_bundle(world, _SCRIPTS.parent.parent, env={})
    counts = bundle.counts()
    assert counts["tree"] == 1, counts
    assert counts["hypotheses"] == 1, counts


def test_empty_conformant_dir_does_not_shadow_populated_sidecar_tree(tmp_path: Path) -> None:
    """An empty ``knowledge/tree/`` must not win over a populated ``tree/``."""
    world = tmp_path / "world"
    world.mkdir()
    _plant_tree(world, "tree")
    (world / "knowledge" / "tree").mkdir(parents=True)  # the decoy
    _plant_stores(world)

    assert ke._resolve_tree_dir(world) == world / "tree"
    assert ke.build_bundle(world, _SCRIPTS.parent.parent, env={}).counts()["tree"] == 1


def test_no_index_anywhere_resolves_to_none(tmp_path: Path) -> None:
    world = tmp_path / "world"
    world.mkdir()
    assert ke._resolve_tree_dir(world) is None
    assert ke.read_tree_nodes(world) == []


def test_store_evidence_separates_a_new_world_from_a_broken_export(tmp_path: Path) -> None:
    """The positive control behind the all-zero refusal (guard-2298).

    A world with no stores reports NO evidence, so an all-zero bundle there is honest and
    must still publish. A world holding readable bytes reports them, so an all-zero bundle
    there is a broken export and the caller refuses.
    """
    empty = tmp_path / "empty"
    empty.mkdir()
    assert ke.world_store_evidence(empty) == {}

    holding = tmp_path / "holding"
    holding.mkdir()
    _plant_stores(holding)
    evidence = ke.world_store_evidence(holding)
    assert evidence.get("pipeline.jsonl", 0) > 0, evidence


def test_sidecar_layout_is_announced_on_stderr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A silent compatibility fallback is indistinguishable from the bug it papers over."""
    world = tmp_path / "world"
    world.mkdir()
    _plant_tree(world, "tree")
    ke._resolve_tree_dir(world)
    assert "non-conformant" in capsys.readouterr().err


def test_conformant_layout_is_silent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    world = tmp_path / "world"
    world.mkdir()
    _plant_tree(world, "knowledge", "tree")
    ke._resolve_tree_dir(world)
    assert capsys.readouterr().err == ""


def _run_main(monkeypatch: pytest.MonkeyPatch, world: Path, out: Path) -> int:
    monkeypatch.setenv("WORLD_PATH", str(world))
    monkeypatch.delenv("MIND_WORLD", raising=False)
    monkeypatch.delenv("META_PATH", raising=False)
    return ke.main(["-o", str(out)])


def test_main_refuses_an_all_zero_bundle_over_a_world_holding_stores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rc=2 AND the previous bundle survives — the property the caller depends on.

    ``knowledge-export.sh`` writes to a tmp file and only ``mv``s it into place when the
    exporter exits 0, so a non-zero rc is what leaves the last good bundle standing. If
    this refusal is ever removed, rc becomes 0, the mv fires, and a hollow bundle replaces
    a good one — which is exactly the failure 17 envs suffered. Asserting the SURVIVING
    CONTENT (not just the rc) is what makes that removal go red (guard-2257).
    """
    world = tmp_path / "world"
    world.mkdir()
    _plant_stores(world)  # readable stores, and deliberately NO tree index

    out = tmp_path / "bundle.json"
    out.write_text('{"previous": "good"}', encoding="utf-8")

    rc = _run_main(monkeypatch, world, out)

    assert rc == 2, rc
    assert json.loads(out.read_text(encoding="utf-8")) == {"previous": "good"}


def test_main_still_publishes_an_honestly_empty_world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A brand-new world has no stores, so its all-zero bundle is honest and must ship.

    The refusal keys on EVIDENCE, not on the zeros — without this arm a stricter guard
    ("refuse every all-zero bundle") would pass the test above while breaking every
    freshly-provisioned env.
    """
    world = tmp_path / "world"
    world.mkdir()

    out = tmp_path / "bundle.json"
    rc = _run_main(monkeypatch, world, out)

    assert rc == 0, rc
    assert out.is_file()
    assert json.loads(out.read_text(encoding="utf-8")).get("counts") is not None
