"""test_kg_retrieve_key_namespace.py -- integration guard for the knowledge-graph
build vs retrieve.py PPR key-namespace contract (g-306-46).

g-306-45 found the g-306-44 PPR blend was 100% inert: retrieve.py seeded PPR with
node:<basename> while knowledge-graph-build.py keys nodes by node:<relpath> (0/50
match on real data). Every g-306-44 unit test (test_ppr_blend.py) and g-306-45
regression guard mocks _compute_ppr_scores, so NO automated test exercises
retrieve's key derivation against the REAL build key derivation -- only the
manual ppr-blend-ab-harness.py did. This file closes that gap: it asserts
retrieve._graph_node_key_candidates derives the SAME "node:<...>" form that
knowledge-graph-build._node_key (+ "node:" prefix) emits for the common
(no-front-matter-key) tree node, and FAILS if either derivation drifts -- the
exact silent-integration-break class the mocked unit tests miss.

Pure stdlib + importlib (mirrors test_ppr_blend.py's scratch-world bootstrap).
Both modules import against a scratch MIND_WORLD; the functions under test
(_node_key, _graph_node_key_candidates, _resolve_ppr_key) are pure path
arithmetic -- no disk, no live world, no PPR module, no graph file. The fixture
path need not exist: Path.relative_to / with_suffix / as_posix are pure.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# Bind both modules to a scratch world before import (both resolve _paths.WORLD_DIR
# at module load). Capture/restore so sibling tests don't inherit the scratch env.
_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")
_TMPDIR = tempfile.mkdtemp(prefix="kg-retrieve-keyns-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)


def _load(mod_name: str, filename: str):
    """Import a core/scripts file (incl. hyphenated names) under a synthetic
    module name via importlib -- the same pattern test_ppr_blend.py uses for
    retrieve.py, extended to the hyphenated knowledge-graph-build.py."""
    spec = importlib.util.spec_from_file_location(mod_name, CORE_SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_retrieve = _load("retrieve_keyns_mod", "retrieve.py")
_kgb = _load("kg_build_keyns_mod", "knowledge-graph-build.py")

if _ORIG_MIND_WORLD is None:
    os.environ.pop("MIND_WORLD", None)
else:
    os.environ["MIND_WORLD"] = _ORIG_MIND_WORLD
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT


# Shared real-shaped fixture: a tree node at a nested relpath with NO front-matter
# key (the common case -- and the exact shape that drifted in , where the
# graph stored node:execution/.../framework-patterns but the blend seeded the bare
# node:framework-patterns). Path need not exist on disk (pure path arithmetic).
_TREE_ROOT = Path(_TMPDIR) / "knowledge" / "tree"
_REL = ("execution", "system-constraints-loop", "framework-patterns")
_MD_PATH = _TREE_ROOT.joinpath(*_REL).with_suffix(".md")   # .../framework-patterns.md
_BASENAME = _REL[-1]                                        # "framework-patterns"
_EXPECTED_REL_KEY = "node:" + "/".join(_REL)                # node:execution/.../framework-patterns


def _build_key(fm: dict | None = None) -> str:
    """The "node:<...>" subject knowledge-graph-build.py emits for the fixture node."""
    return "node:" + _kgb._node_key(_MD_PATH, fm or {}, _TREE_ROOT)


def _retrieve_candidates(fm_key: str | None = None) -> list:
    """The "node:<...>" candidates retrieve.py would match against the PPR ranking.
    retrieve.load_tree_nodes keys the node by basename-stem and carries the source
    path in node['file'] -- the two inputs _graph_node_key_candidates consumes."""
    key = fm_key or _BASENAME
    node = {"file": str(_MD_PATH)}
    return _retrieve._graph_node_key_candidates(key, node)


# ── The core contract: build key derivation == retrieve path-derived candidate ──

def test_common_case_build_key_equals_retrieve_path_candidate():
    # No front-matter key -> build keys by tree-root-relative POSIX path (no .md).
    # retrieve must derive the SAME "node:<relpath>" as its FIRST (preferred)
    # candidate. If either side drifts (build stops stripping .md, retrieve changes
    # its "/knowledge/tree/" marker, path normalization diverges across OSes), these
    # differ and this test FAILS -- regression-guarding the inert-blend class.
    bkey = _build_key()
    cands = _retrieve_candidates()
    assert bkey == _EXPECTED_REL_KEY            # build derivation is what we expect
    assert cands[0] == _EXPECTED_REL_KEY        # retrieve's PREFERRED candidate matches
    assert bkey in cands                        # _resolve_ppr_key would find it


def test_resolve_ppr_key_picks_the_build_key_when_present():
    # End-to-end through the resolver: given a PPR ranking that contains the build's
    # actual graph key, _resolve_ppr_key must return THAT key (not the inert
    # basename). This is the function _score_weight_limit calls per candidate -- if
    # it returned the basename, _ppr_weight would no-op to 1.0 (the original bug).
    ppr_scores = {_EXPECTED_REL_KEY: 0.9}        # graph stores the relpath form
    resolved = _retrieve._resolve_ppr_key(_BASENAME, {"file": str(_MD_PATH)}, ppr_scores)
    assert resolved == _EXPECTED_REL_KEY
    assert resolved == _build_key()              # resolver lands on the build's key


def test_naive_basename_seed_misses_the_build_key():
    # The  bug shape: the naive "node:"+basename seed (what 
    # shipped) is NOT the build's key for a nested node. This pins WHY the
    # path-derived candidate is required -- the distinct-namespace premise the
    # whole fix rests on. If a refactor collapsed the candidates to basename-only
    # again, test_common_case above already fails; this documents the inert seed.
    bkey = _build_key()
    naive = "node:" + _BASENAME
    assert naive != bkey
    assert naive == "node:framework-patterns"    # the exact inert seed  found


def test_build_and_retrieve_anchor_on_same_tree_subdir():
    # Both derivations anchor on the "knowledge/tree/" boundary: build's tree_root
    # is <WORLD>/<*_TREE_SUBDIR>, retrieve extracts the relpath after the literal
    # "/knowledge/tree/" marker. If _TREE_SUBDIR is relocated without updating
    # retrieve's marker, every relpath candidate would mismatch -> blend inert
    # again. This guards that drift vector directly.
    assert _kgb._TREE_SUBDIR == ("knowledge", "tree")
    marker = "/" + "/".join(_kgb._TREE_SUBDIR) + "/"
    assert marker == "/knowledge/tree/"


def test_front_matter_key_case_is_documented_known_divergence():
    # The minority case: when a node carries an explicit front-matter `key`, build
    # emits node:<fm-key> (NOT the path). retrieve's candidates stay
    # [node:<relpath>, node:<basename>] -- it does not reproduce an arbitrary fm
    # key, so this case legitimately does NOT match (acknowledged in
    # _graph_node_key_candidates' docstring). Pinning this BEHAVIOR makes any future
    # change to either side surface here rather than silently shifting which nodes
    # the blend can reach. (The common path-keyed case is the 0/50 incident and is
    # guarded by test_common_case above; fm-keyed nodes are the known minority.)
    bkey = _build_key(fm={"key": "custom-fm-key"})
    assert bkey == "node:custom-fm-key"          # build honors front-matter key
    cands = _retrieve_candidates()
    assert bkey not in cands                      # retrieve does NOT derive the fm key
