"""Tests for hypothesis_lineage.py (Phase 3 — Arbor edges on the flat pipeline)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hypothesis_lineage as hl  # noqa: E402


def _h(hid, parent=None, relation=None, status="active"):
    r = {"id": hid, "status": status}
    if parent is not None:
        r["parent_hypothesis"] = parent
    if relation is not None:
        r["relation"] = relation
    return r


# --------------------------------------------------------------------------- #
# index + validation
# --------------------------------------------------------------------------- #


def test_index_rejects_missing_and_duplicate_ids():
    with pytest.raises(ValueError, match="missing 'id'"):
        hl.index([{"status": "active"}])
    with pytest.raises(ValueError, match="duplicate"):
        hl.index([_h("a"), _h("a")])


def test_flat_records_are_valid_roots():
    # backward-compatible: existing flat pipeline records (no lineage fields) pass.
    hl.validate_lineage([_h("a"), _h("b"), _h("c")])  # no raise


def test_validate_accepts_well_formed_tree():
    recs = [_h("root"), _h("k1", "root", "refines"), _h("k2", "root", "contradicts"),
            _h("k1a", "k1", "refines")]
    hl.validate_lineage(recs)  # no raise


def test_validate_rejects_dangling_parent():
    with pytest.raises(ValueError, match="not found"):
        hl.validate_lineage([_h("k1", "ghost", "refines")])


def test_validate_rejects_bad_relation():
    with pytest.raises(ValueError, match="relation"):
        hl.validate_lineage([_h("root"), _h("k1", "root", "vibes")])


def test_validate_rejects_relation_without_parent():
    with pytest.raises(ValueError, match="without a parent"):
        hl.validate_lineage([_h("k1", parent=None, relation="refines")])


def test_validate_rejects_self_cycle_and_loop():
    with pytest.raises(ValueError, match="cycle"):
        hl.validate_lineage([_h("a", "a", "refines")])
    # a -> b -> a
    with pytest.raises(ValueError, match="cycle"):
        hl.validate_lineage([_h("a", "b", "refines"), _h("b", "a", "refines")])


# --------------------------------------------------------------------------- #
# traversal
# --------------------------------------------------------------------------- #


def test_children_and_relation_filter():
    recs = [_h("root"), _h("k1", "root", "refines"), _h("k2", "root", "contradicts")]
    assert {c["id"] for c in hl.children(recs, "root")} == {"k1", "k2"}
    assert {c["id"] for c in hl.children(recs, "root", relation="refines")} == {"k1"}


def test_subtree_ids_follows_all_then_filtered():
    recs = [_h("root"), _h("k1", "root", "refines"), _h("k1a", "k1", "refines"),
            _h("k2", "root", "contradicts"), _h("k2a", "k2", "refines")]
    assert hl.subtree_ids(recs, "root") == {"root", "k1", "k1a", "k2", "k2a"}
    # follow only refines from root: k2 is a contradicts child, so it + its subtree are excluded
    assert hl.subtree_ids(recs, "root", follow_relations={"refines"}) == {"root", "k1", "k1a"}


def test_subtree_unknown_root_raises():
    with pytest.raises(ValueError, match="unknown root"):
        hl.subtree_ids([_h("a")], "ghost")


# --------------------------------------------------------------------------- #
# prune-on-refutation — the load-bearing behavior
# --------------------------------------------------------------------------- #


def test_prune_follows_refines_only():
    recs = [
        _h("root"),
        _h("k1", "root", "refines"),       # depends on root -> undermined
        _h("k1a", "k1", "refines"),        # transitively depends -> undermined
        _h("alt", "root", "contradicts"),  # competing alternative -> NOT pruned
        _h("alt_ref", "alt", "refines"),   # refines the alternative -> NOT pruned (under alt)
        _h("repl", "root", "supersedes"),  # already replaced root -> NOT pruned
    ]
    undermined = hl.prune_on_refutation(recs, "root")
    assert undermined == ["k1", "k1a"]
    # crucially: the contradicting alternative and its subtree survive a parent refutation
    assert "alt" not in undermined and "alt_ref" not in undermined and "repl" not in undermined


def test_prune_excludes_the_refuted_node_itself():
    recs = [_h("root"), _h("k1", "root", "refines")]
    assert "root" not in hl.prune_on_refutation(recs, "root")


def test_prune_refuses_malformed_graph():
    with pytest.raises(ValueError):
        hl.prune_on_refutation([_h("a", "ghost", "refines")], "a")


# --------------------------------------------------------------------------- #
# child outcome summary
# --------------------------------------------------------------------------- #


def test_child_outcome_summary():
    recs = [_h("root"),
            _h("k1", "root", "refines", status="confirmed"),
            _h("k2", "root", "refines", status="refuted"),
            _h("alt", "root", "contradicts", status="confirmed")]
    s = hl.child_outcome_summary(recs, "root")
    assert s["refines"] == {"confirmed": 1, "refuted": 1}
    assert s["contradicts"] == {"confirmed": 1}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_validate_and_prune(tmp_path, capsys):
    recs = [_h("root"), _h("k1", "root", "refines"), _h("alt", "root", "contradicts")]
    p = tmp_path / "pipeline.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in recs), encoding="utf-8")

    assert hl.main(["validate", "--pipeline", str(p)]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True

    assert hl.main(["prune", "--pipeline", str(p), "--refuted", "root"]) == 0
    assert json.loads(capsys.readouterr().out)["undermined"] == ["k1"]
