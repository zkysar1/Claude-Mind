"""Regression test for the saturated_frontier check ( / guard-2437).

A knowledge-tree node can declare its frontier SATURATED via `saturated_topics`
on its `_tree.yaml` entry. Before this check that declaration lived only in the
node BODY, reachable only by an agent already reading the node — i.e. one who no
longer needed it. Five independent agents re-measured
multi-env-cognitive-load-baseline.md through that gap.

BOTH DIRECTIONS ARE PINNED (guard-1836, and the goal's own verification): a
check that only ever passes is indistinguishable from one that never runs
(guard-1760, guard-1802). So the fire cases use the REAL titles of the three
direct-measurement filings from the N=5 population, and the silent cases include
the node's own recommended alternative action.

Hermetic: builds its own tmp `_tree.yaml`; never reads the live world.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent
for _p in (str(_SCRIPTS), str(_SCRIPTS / "gates")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import goal_duplication as gd  # noqa: E402


@pytest.fixture()
def saturated_world(tmp_path: Path) -> Path:
    """A tmp world whose tree declares one saturated node."""
    tree_dir = tmp_path / "knowledge" / "tree"
    tree_dir.mkdir(parents=True)
    (tree_dir / "_tree.yaml").write_text(
        yaml.safe_dump({
            "nodes": {
                "multi-env-cognitive-load-baseline": {
                    "file": "world/knowledge/tree/.../multi-env-cognitive-load-baseline.md",
                    "saturated_topics": [
                        "cognitive load",
                        "cost add next environment",
                        "primitives amortization",
                    ],
                },
                # A normal node with no marker — must never contribute a match.
                "some-other-node": {"file": "world/knowledge/tree/other.md"},
            }
        }),
        encoding="utf-8",
    )
    return tmp_path


def _fired(goal, world):
    return not gd._check_saturated_frontier(goal, world)["passed"]


# --- Direction 1: MUST FIRE (real titles from the N=5 population) ------------

@pytest.mark.parametrize("title,description", [
    # 
    ("Adapter cognitive-load audit: quantify the marginal cost (LOC/slots/"
     "helpers) of adding the next environment", "quantify marginal cost"),
    # 
    ("Measure the cost-to-add-the-next-environment across all four adapters",
     "Self names low cognitive load to add the next environment as PRIMARY"),
    # 
    ("Primitive amortization audit v2: does the shared primitives/ core reach "
     "all 4 envs", "audit whether primitives amortization holds across envs"),
])
def test_fires_on_real_saturated_measurement_filings(saturated_world, title,
                                                     description):
    assert _fired({"title": title, "description": description}, saturated_world)


def test_fire_reason_names_the_node_and_the_override(saturated_world):
    res = gd._check_saturated_frontier(
        {"title": "Measure cognitive load per adapter", "description": ""},
        saturated_world)
    assert res["passed"] is False
    assert "multi-env-cognitive-load-baseline" in res["reason"]
    assert "override-duplication" in res["reason"]
    assert res["matches"][0]["node"] == "multi-env-cognitive-load-baseline"


# --- Direction 2: MUST STAY SILENT ------------------------------------------

@pytest.mark.parametrize("title,description", [
    # An unrelated measurement goal — the explicit negative in the goal spec.
    ("Measure full-suite runtime and baseline the chunk ladder on cc-05",
     "benchmark pytest wall-clock across chunk counts"),
    # Topic tokens present, but no measurement verb: real work on the pattern.
    ("Add the football adapter to the universal environment abstraction",
     "implement the three mandatory slots for a new environment"),
    ("Reduce cognitive load in the adapter contract",
     "refactor base.py so the next environment needs fewer slots"),
    # The node's OWN recommended lever must not be blocked by the node.
    ("Run live ARC play on cc-03 to move the PRIMARY score above zero",
     "the real lever is the live ARC score, gated on LLM-CEGIS synth"),
])
def test_silent_on_unrelated_or_non_measurement_goals(saturated_world, title,
                                                      description):
    assert not _fired({"title": title, "description": description},
                      saturated_world)


def test_partial_topic_overlap_does_not_fire(saturated_world):
    """ALL tokens of a topic must appear — 'environment' alone is not enough.

    This is the precision guard. The module-shared tokenizer requires 5+ char
    tokens and would reduce 'cost add next environment' to {environment},
    firing on every environment goal; _saturation_topic_tokens keeps the short
    tokens precisely to prevent that.
    """
    assert not _fired(
        {"title": "Measure the environment startup latency",
         "description": "benchmark how long an environment takes to boot"},
        saturated_world)


# --- Fail-open paths ---------------------------------------------------------

def test_no_world_dir_skips(saturated_world):
    res = gd._check_saturated_frontier({"title": "Measure cognitive load"}, None)
    assert res["passed"] is True
    assert "skipped" in res["reason"]


def test_missing_tree_file_fails_open(tmp_path):
    res = gd._check_saturated_frontier(
        {"title": "Measure cognitive load per adapter"}, tmp_path)
    assert res["passed"] is True


@pytest.mark.parametrize("shape,payload", [
    # The shape the original single test pinned — invalid YAML -> YAMLError.
    ("invalid-yaml", b"{[not valid yaml"),
    # Truncated UTF-8 mid-character. Realistic here: this deployment has
    # MEASURED torn mid-write syncs corrupting files in the synced tree. A
    # UnicodeDecodeError is a ValueError, so the original (OSError, YAMLError)
    # tuple did not catch it.
    ("truncated-utf8", b"nodes:\n  n:\n    file: caf\xc3"),
    # VALID YAML that is not a mapping. `safe_load(...) or {}` substitutes only
    # on None, so these reached `.get` and raised AttributeError.
    ("valid-yaml-list", b"- a\n- b\n"),
    ("valid-yaml-scalar", b"just a string\n"),
])
def test_malformed_tree_fails_open(tmp_path, shape, payload):
    """Fail-open must hold for EVERY malformed shape, not just invalid YAML.

    This check runs as step 7 of evaluate() on every goal filing, so anything
    that escapes here blocks the filing endpoint fleet-wide. Three of these
    four shapes escaped when this test covered only the first — the test NAME
    claimed the class while the BODY covered one member, which is precisely
    what made the gap invisible. Parametrized so the name and the coverage
    cannot drift apart again.
    """
    tree_dir = tmp_path / "knowledge" / "tree"
    tree_dir.mkdir(parents=True)
    (tree_dir / "_tree.yaml").write_bytes(payload)
    res = gd._check_saturated_frontier(
        {"title": "Measure cognitive load per adapter"}, tmp_path)
    assert res["passed"] is True, f"{shape} escaped fail-open"


def test_node_without_marker_never_matches(tmp_path):
    tree_dir = tmp_path / "knowledge" / "tree"
    tree_dir.mkdir(parents=True)
    (tree_dir / "_tree.yaml").write_text(
        yaml.safe_dump({"nodes": {"plain": {"file": "world/x.md"}}}),
        encoding="utf-8")
    assert not _fired(
        {"title": "Measure cognitive load per adapter", "description": ""},
        tmp_path)


# --- Wiring: the check is actually in evaluate()'s check list ----------------

def test_check_is_wired_into_evaluate(saturated_world):
    """A check that exists but is never called is indistinguishable from one
    that always passes (guard-1760). Pin the wiring, not just the function."""
    res = gd.evaluate(
        {"title": "Measure cognitive load per adapter to quantify cost",
         "description": "re-measure the adapter cognitive load baseline"},
        agent_name="bravo", world_dir=saturated_world)
    names = [c.get("name") for c in res["checks"]]
    assert "saturated_frontier" in names
    sat = [c for c in res["checks"] if c["name"] == "saturated_frontier"][0]
    assert sat["passed"] is False
    assert res["would_block"] is True


def test_override_clears_the_block(saturated_world):
    res = gd.evaluate(
        {"title": "Measure cognitive load per adapter to quantify cost",
         "description": "re-measure the adapter cognitive load baseline"},
        agent_name="bravo", world_dir=saturated_world,
        override_duplication="measuring a NEW fifth adapter, not the encoded four")
    assert res["would_block"] is False
