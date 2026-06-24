"""test_knowledge_graph_build.py --  (BRD Gap 1b+1c, child 1/4 of ).

Pins the Mind knowledge-graph extractor (core/scripts/knowledge-graph-build.py):
deterministic triple extraction from the Gap-5 bi-temporal stores
(reasoning-bank.jsonl, guardrails.jsonl, knowledge-tree node front matter/bodies),
the record-identifier reference regex, the self-edge guard, additive-union
semantics, and idempotency (a re-run yields zero new triples + byte-stable output).

MIND-SCOPED: this graph indexes the framework's OWN symbolic record identifiers
(rb-/guard-/g-/node-keys), NOT the NPC all-MiniLM-L6-v2 embedding pipeline.

Daemon-safe (no daemon spawn, no daemon_integration marker): the extractor is a
pure function set; the test monkeypatches the module-level WORLD_DIR/META_DIR
(both read at call time, not captured at import) to a tmp dir and seeds fixtures.
locked_write_jsonl's history/changelog side effects are skipped because
resolve_base_dir() returns None for a tmp path outside the real configured roots.

Cross-references:
  - g-306-42 -- this extractor (child 1/4 of g-306-15 HippoRAG-style KG + PPR)
  - test_bitemporal_writer_path.py -- sibling Gap-5 test; importlib load pattern
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

# Hyphen-named script -> importlib load (the test_bitemporal_writer_path pattern).
_KGB_PATH = CORE_SCRIPTS / "knowledge-graph-build.py"
_spec = importlib.util.spec_from_file_location("knowledge_graph_build", _KGB_PATH)
kgb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kgb)


# --- fixture ----------------------------------------------------------------

@pytest.fixture
def kg(tmp_path, monkeypatch):
    """Seed a tmp WORLD_DIR + META_DIR with deterministic fixtures and point the
    extractor's module-level path globals at them."""
    world = tmp_path / "world"
    meta = tmp_path / "meta"
    tree = world / "knowledge" / "tree" / "execution"
    tree.mkdir(parents=True)
    meta.mkdir(parents=True)

    # reasoning-bank.jsonl -- rb-100 is rich (also self-references, to exercise the
    # s==o guard); rb-101 uses `created` as the valid_from fallback.
    (world / "reasoning-bank.jsonl").write_text(
        json.dumps({"id": "rb-100", "category": "framework-architecture",
                    "tags": ["path-resolution", "cruft"],
                    "content": "this rb-100 mirrors rb-099 and guard-165; see g-115-700",
                    "source_goal": "g-115-700",
                    "valid_from": "2026-06-01T00:00:00"}) + "\n" +
        json.dumps({"id": "rb-101", "category": "verification",
                    "tags": ["audit"], "content": "no references here",
                    "created": "2026-05-01T00:00:00"}) + "\n",
        encoding="utf-8")

    # guardrails.jsonl -- one record referencing another guard.
    (world / "guardrails.jsonl").write_text(
        json.dumps({"id": "guard-200", "category": "safety", "tags": ["secrets"],
                    "rule": "never print the PAT; relates to guard-199",
                    "valid_from": "2026-06-02T00:00:00"}) + "\n",
        encoding="utf-8")

    # tree node -- front matter key/tags/valid_from + reference-rich body.
    (tree / "test-node.md").write_text(
        "---\n"
        "key: execution/test-node\n"
        "tags:\n  - pattern\n"
        "valid_from: 2026-06-03T00:00:00\n"
        "---\n"
        "This node derives from rb-100 and tracks g-306-15.\n",
        encoding="utf-8")

    monkeypatch.setattr(kgb, "WORLD_DIR", str(world))
    monkeypatch.setattr(kgb, "META_DIR", str(meta))
    return {"world": world, "meta": meta, "out": meta / "knowledge-graph.jsonl"}


def _has(triples, s, p, o):
    return any(t["s"] == s and t["p"] == p and t["o"] == o for t in triples)


# --- extraction: reasoning bank ---------------------------------------------

def test_reasoning_bank_entity_extraction(kg):
    t = kgb.build_triples()
    assert _has(t, "rb-100", "has_category", "cat:framework-architecture")
    assert _has(t, "rb-100", "has_tag", "tag:path-resolution")
    assert _has(t, "rb-100", "has_tag", "tag:cruft")
    assert _has(t, "rb-100", "derived_from", "g-115-700")
    assert _has(t, "rb-100", "references", "rb-099")
    assert _has(t, "rb-100", "references", "guard-165")
    assert _has(t, "rb-100", "references", "g-115-700")


def test_self_reference_is_skipped(kg):
    # rb-100 mentions "rb-100" in its own content -> would be a self-edge.
    t = kgb.build_triples()
    assert not _has(t, "rb-100", "references", "rb-100")
    assert not any(x["s"] == x["o"] for x in t)


def test_created_fallback_for_valid_from(kg):
    # rb-101 has no valid_from -> the extractor falls back to `created`.
    t = kgb.build_triples()
    rb101 = [x for x in t if x["src"] == "rb-101"]
    assert rb101
    assert all(x["valid_from"] == "2026-05-01T00:00:00" for x in rb101)


# --- extraction: guardrails -------------------------------------------------

def test_guardrail_entity_extraction(kg):
    t = kgb.build_triples()
    assert _has(t, "guard-200", "has_category", "cat:safety")
    assert _has(t, "guard-200", "has_tag", "tag:secrets")
    assert _has(t, "guard-200", "references", "guard-199")


# --- extraction: knowledge-tree nodes ---------------------------------------

def test_tree_node_extraction(kg):
    t = kgb.build_triples()
    # No `category` in front matter -> derived from the first path segment.
    assert _has(t, "node:execution/test-node", "has_category", "cat:execution")
    assert _has(t, "node:execution/test-node", "has_tag", "tag:pattern")
    assert _has(t, "node:execution/test-node", "references", "rb-100")
    assert _has(t, "node:execution/test-node", "references", "g-306-15")


def test_node_without_front_matter(kg):
    # A tree node with no front matter: the whole file is body; key derives from
    # the path relative to the tree root.
    (kg["world"] / "knowledge" / "tree" / "execution" / "plain.md").write_text(
        "Plain node referencing guard-200.\n", encoding="utf-8")
    t = kgb.build_triples()
    assert _has(t, "node:execution/plain", "references", "guard-200")


# --- reference regex --------------------------------------------------------

def test_reference_regex_matches_all_id_forms():
    refs = list(kgb._extract_refs(
        "rb-099 guard-165 g-115-700 g-306-15 sig-12 asp-3 bel-7 "
        "trans-4 sq-2 pt-9 sa-5 -- but not foobar-1 nor http-2"))
    for expected in ("rb-099", "guard-165", "g-115-700", "g-306-15", "sig-12",
                     "asp-3", "bel-7", "trans-4", "sq-2", "pt-9", "sa-5"):
        assert expected in refs, expected
    assert "foobar-1" not in refs
    assert "http-2" not in refs


def test_reference_regex_dedups():
    refs = list(kgb._extract_refs("rb-1 rb-1 rb-1 guard-2"))
    assert refs == ["rb-1", "guard-2"]


# --- write semantics: dry-run, idempotency, additive union ------------------

def test_dry_run_writes_nothing(kg):
    s = kgb.run(apply=False)
    assert s["dry_run"] is True
    assert s["applied"] is False
    assert not kg["out"].exists()
    assert s["extracted_triples"] > 0


def test_idempotency(kg):
    s1 = kgb.run(apply=True)
    assert s1["applied"] is True
    assert s1["new_triples"] == s1["extracted_triples"]
    assert kg["out"].exists()
    first_bytes = kg["out"].read_bytes()

    # Second apply over an unchanged source set: zero new, no rewrite, byte-stable.
    s2 = kgb.run(apply=True)
    assert s2["new_triples"] == 0
    assert s2["applied"] is False
    assert kg["out"].read_bytes() == first_bytes


def test_additive_union_preserves_existing(kg):
    # A pre-existing triple absent from the fresh extraction must survive the union.
    stale = {"s": "rb-999", "p": "references", "o": "rb-998",
             "src": "rb-999", "store": "reasoning-bank",
             "valid_from": None, "valid_to": None}
    kg["out"].write_text(json.dumps(stale) + "\n", encoding="utf-8")

    s = kgb.run(apply=True)
    assert s["existing_triples"] == 1
    assert s["new_triples"] >= 1
    keys = {(r["s"], r["p"], r["o"])
            for r in (json.loads(l) for l in kg["out"].read_text().splitlines() if l.strip())}
    assert ("rb-999", "references", "rb-998") in keys            # stale preserved
    assert ("rb-100", "has_category", "cat:framework-architecture") in keys  # new added


def test_deterministic_order(kg):
    a = kgb.build_triples()
    b = kgb.build_triples()
    assert [kgb._triple_key(x) for x in a] == [kgb._triple_key(x) for x in b]


def test_output_path_under_meta(kg):
    assert kgb._output_path() == Path(kg["meta"]) / "knowledge-graph.jsonl"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
