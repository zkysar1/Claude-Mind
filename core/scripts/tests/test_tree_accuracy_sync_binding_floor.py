"""tree-accuracy-sync.py category→node binding match-quality floor ().

resolve_node_for_category took find_nodes(...)[0] with NO quality bar, so a SHORT
node key (`sol`, `pip`, `deb`) matched as a SUBSTRING inside a longer category
word (`sol` in con-SOL-idation) became a silent binding that wrote hypothesis
accuracy onto an unrelated node. These tests pin THREE fixes and their limits:

  #3a  short-key substring collisions are REFUSED (cached or freshly derived),
       leaving the category unresolved so no accuracy is written; a legitimate
       whole-short-token binding is spared.
  #3b  a resolved binding with no whole-token support is REPORTED (advisory),
       never refused — the census showed some are legitimate summary matches.
  #2   load_bindings distinguishes absent/ok/unreadable, and plan_updates never
       wholesale-replaces a present-but-unreadable cache (bravo fresh-eyes,
       rb-6775 / guard-3205).

Each behaviour carries a positive control or an explicit mutation proof so a
test that passes for the wrong reason fails loudly (guard-2903 / guard-4166).
"""
import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
MODULE_PATH = SCRIPT_DIR / "tree-accuracy-sync.py"


def _load():
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location("_tas_floor_under_test", str(MODULE_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _node(key, summary=""):
    return {"key": key, "file": "", "depth": 1, "summary": summary, "node_type": "leaf"}


def _returns(result):
    return lambda cat, nodes, ei, top=1, leaf_only=True: result


# ── #3a helper: _is_short_key_substring_collision ────────────────────────────

def test_short_key_collision_flags_the_substring_class():
    m = _load()
    assert m._is_short_key_substring_collision("memory-consolidation", "sol")
    assert m._is_short_key_substring_collision("hypothesis-pipeline", "pip")
    assert m._is_short_key_substring_collision("debugging-lane", "deb")


def test_short_key_collision_spares_a_whole_short_token():
    # The category actually CONTAINS the whole short token — a real binding.
    m = _load()
    assert not m._is_short_key_substring_collision("sol-metrics", "sol")


def test_short_key_collision_spares_long_keys():
    # A key carrying any >=4-char token is never this class, even at zero overlap
    # (system-behavior→npc shares whole `behavior`; the semantic case is NOT ours).
    m = _load()
    assert not m._is_short_key_substring_collision(
        "system-behavior", "npc-behavior-and-player-interaction")
    assert not m._is_short_key_substring_collision(
        "working-memory", "own-cloud-s3-cost-profile")


# ── #3b helper: _has_whole_token_support ─────────────────────────────────────

def test_whole_token_support_key_summary_and_no_long_token():
    m = _load()
    assert m._has_whole_token_support("hypothesis-pipeline", "hypothesis-calibration", "")
    assert m._has_whole_token_support("infrastructure-cost", "pearl-pricing-strategy",
                                      "notes on cost and pricing")
    assert not m._has_whole_token_support("working-memory", "own-cloud-s3-cost-profile",
                                          "object store lifecycle")
    # arc-agi-3 tokenizes to nothing >=4 chars: never flagged (avoids false reject)
    assert m._has_whole_token_support("arc-agi-3", "grid-perception-decomposition", "grids")


# ── #3a resolve_node_for_category: refuse, spare, self-heal ──────────────────

def test_resolve_refuses_freshly_derived_short_key_collision(tmp_path, monkeypatch):
    m = _load()
    monkeypatch.setattr(m, "BINDINGS_PATH", tmp_path / "b.json")
    monkeypatch.setattr(m, "find_nodes", _returns([_node("sol")]))
    bindings = {}
    assert m.resolve_node_for_category("memory-consolidation", {"sol": {}}, {}, bindings) is None
    assert "memory-consolidation" not in bindings  # never persisted


def test_resolve_binds_a_legitimate_short_key(tmp_path, monkeypatch):
    # POSITIVE CONTROL: the guard must not refuse a real whole-short-token match.
    m = _load()
    monkeypatch.setattr(m, "BINDINGS_PATH", tmp_path / "b.json")
    monkeypatch.setattr(m, "find_nodes", _returns([_node("sol")]))
    bindings = {}
    assert m.resolve_node_for_category("sol-metrics", {"sol": {}}, {}, bindings) is not None
    assert bindings["sol-metrics"] == "sol"


def test_resolve_self_heals_a_cached_short_key_collision(tmp_path, monkeypatch):
    # The TRAP (nulling re-derives) is defused: re-derivation is refused too.
    m = _load()
    monkeypatch.setattr(m, "BINDINGS_PATH", tmp_path / "b.json")
    monkeypatch.setattr(m, "find_nodes", _returns([]))  # isolate the cached drop
    bindings = {"memory-consolidation": "sol"}
    assert m.resolve_node_for_category("memory-consolidation", {"sol": {}}, {}, bindings) is None
    assert "memory-consolidation" not in bindings  # bad pin removed


def test_short_key_guard_is_not_green_by_default(tmp_path, monkeypatch):
    # MUTATION PROOF (guard-2903): with the floor disabled the collision BINDS,
    # proving the guard — not some other path — is what refuses it above.
    m = _load()
    monkeypatch.setattr(m, "BINDINGS_PATH", tmp_path / "b.json")
    monkeypatch.setattr(m, "MIN_KEY_TOKEN_LEN", 0)
    monkeypatch.setattr(m, "find_nodes", _returns([_node("sol")]))
    bindings = {}
    assert m.resolve_node_for_category("memory-consolidation", {"sol": {}}, {}, bindings) is not None
    assert bindings["memory-consolidation"] == "sol"


# ── #2 load_bindings provenance ──────────────────────────────────────────────

def test_load_bindings_absent(tmp_path, monkeypatch):
    m = _load()
    monkeypatch.setattr(m, "BINDINGS_PATH", tmp_path / "nope.json")
    assert m.load_bindings() == ({}, "absent")


def test_load_bindings_ok(tmp_path, monkeypatch):
    m = _load()
    p = tmp_path / "b.json"
    p.write_text('{"c":"n"}', encoding="utf-8")
    monkeypatch.setattr(m, "BINDINGS_PATH", p)
    assert m.load_bindings() == ({"c": "n"}, "ok")


def test_load_bindings_unreadable(tmp_path, monkeypatch):
    m = _load()
    p = tmp_path / "b.json"
    p.write_text("{ not json at all", encoding="utf-8")
    monkeypatch.setattr(m, "BINDINGS_PATH", p)
    assert m.load_bindings() == ({}, "unreadable")


def test_plan_updates_does_not_overwrite_unreadable_cache(tmp_path, monkeypatch):
    # THE HARM: a present-but-corrupt cache read as {} must not be wholesale-saved.
    m = _load()
    p = tmp_path / "b.json"
    p.write_text("CORRUPT NOT JSON", encoding="utf-8")
    monkeypatch.setattr(m, "BINDINGS_PATH", p)
    monkeypatch.setattr(m, "find_nodes", _returns([_node("node-a")]))
    tree = {"nodes": {"node-a": {}}, "entity_index": {}}
    _, summary = m.plan_updates({"cat-new": {"confirmed": 1, "total": 1}}, tree,
                                persist_bindings=True)
    assert p.read_text(encoding="utf-8") == "CORRUPT NOT JSON"  # untouched
    assert summary.get("bindings_persist_skipped")


def test_plan_updates_still_persists_a_readable_cache(tmp_path, monkeypatch):
    # POSITIVE CONTROL: the skip is scoped to 'unreadable', not a blanket disable.
    m = _load()
    p = tmp_path / "b.json"
    monkeypatch.setattr(m, "BINDINGS_PATH", p)
    monkeypatch.setattr(m, "find_nodes", _returns([_node("node-a")]))
    tree = {"nodes": {"node-a": {}}, "entity_index": {}}
    m.plan_updates({"cat-new": {"confirmed": 1, "total": 1}}, tree, persist_bindings=True)
    assert json.loads(p.read_text(encoding="utf-8")) == {"cat-new": "node-a"}


# ── #3b low-quality reporting is advisory, not a refusal ──────────────────────

def test_low_quality_binding_is_reported_not_refused(tmp_path, monkeypatch):
    m = _load()
    monkeypatch.setattr(m, "BINDINGS_PATH", tmp_path / "b.json")
    monkeypatch.setattr(m, "find_nodes",
                        _returns([_node("own-cloud-s3-cost-profile", "object store lifecycle")]))
    tree = {"nodes": {"own-cloud-s3-cost-profile": {"summary": "object store lifecycle"}},
            "entity_index": {}}
    plan, summary = m.plan_updates({"working-memory": {"confirmed": 1, "total": 1}}, tree,
                                   persist_bindings=False)
    assert {"category": "working-memory", "node_key": "own-cloud-s3-cost-profile"} \
        in summary["low_quality_bindings"]
    assert "working-memory" not in summary["unresolved_categories"]  # bound, not refused
    assert plan and plan[0][0] == "own-cloud-s3-cost-profile"


def test_supported_binding_is_not_flagged_low_quality(tmp_path, monkeypatch):
    # POSITIVE CONTROL paired with the flagging test above.
    m = _load()
    monkeypatch.setattr(m, "BINDINGS_PATH", tmp_path / "b.json")
    monkeypatch.setattr(m, "find_nodes", _returns([_node("hypothesis-calibration", "")]))
    tree = {"nodes": {"hypothesis-calibration": {"summary": ""}}, "entity_index": {}}
    _, summary = m.plan_updates({"hypothesis-pipeline": {"confirmed": 1, "total": 1}}, tree,
                                persist_bindings=False)
    assert summary["low_quality_bindings"] == []  # shares whole token 'hypothesis'
