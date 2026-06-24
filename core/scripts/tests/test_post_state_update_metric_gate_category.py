"""test_post_state_update_metric_gate_category.py -  option (d).

Regression test for the meta-work category suppression in
core/scripts/post-state-update-metric-gate.sh.

Background: the metric-gate fires when a deep-outcome closure carries 2+
distinct numeric findings in outcome_note prose. g-115-724 self-test
demonstrated a false-positive: the gate fired with 3 distinct findings
(250loc, 44loc, 47loc) on a bravo framework-hygiene deep close - none of
which were production metrics worth encoding. Per g-115-726 description,
option (d) was chosen as the cheapest + most targeted fix: suppress when
goal.category starts with framework- or contains hygiene.

Cases covered:
  1. category=framework-architecture + production-shape outcome_note
     -> not-fired, reason cites meta-work suppression (g-115-726 option d).
  2. category=framework-hygiene + same outcome_note -> same suppression.
  3. category=npc-cognition + same outcome_note -> FIRES (control: the
     gate still works for production-domain categories).
  4. category=ayoai-platform-services + same outcome_note -> FIRES
     (control: ayoai-* is production domain).
  5. category=framework-meta + same outcome_note -> suppressed
     (framework-* prefix).
  6. category=some-other-hygiene + same outcome_note -> suppressed
     (*hygiene* suffix match).

Pattern: direct bash subprocess invocation; the gate's heredocs do their
own python work so no DaemonFixture needed. Mirrors the in-script "test
case" numbering comments (post-state-update-metric-gate.sh lines 81/89/170).

Run: py -3 -m pytest core/scripts/tests/test_post_state_update_metric_gate_category.py -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
GATE = CORE_SCRIPTS / "post-state-update-metric-gate.sh"

# Resolve a Windows-safe bash (Git Bash on Windows, system bash elsewhere).
# Bare "bash" on Windows can resolve to WSL bash which cannot exec
# `C:\...` paths and hangs on `python3` invocation — see _bash_helpers
# docstring for the full story (, rb-919).
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH  # noqa: E402

# An outcome_note with 3 distinct numeric findings that match the gate's
# extraction patterns (Pattern A: 2x; Pattern B: 250 vs 44; Pattern C: 0 ->
# 69). With any production-domain category, this MUST fire (control). With
# framework-* or *hygiene*, the new suppression MUST short-circuit it.
PRODUCTION_SHAPE_NOTE = (
    "Verified: jose intent throughput improved 2x. "
    "RichmondKey latency 250 vs 44 ms baseline. "
    "BT failure rate 0 -> 69 per hour."
)


# ---- Hermetic isolation of the gate's ambient-state reads (1) ----
# The gate reads TWO pieces of LIVE agent state that make its fired/not-fired
# decision non-deterministic between a full-suite run and an alone run:
#   - Test-case-5 (gate:192): $AGENT_DIR/session/iteration-checkpoint.json +
#     tree-edit-since.py against the live knowledge tree. If a checkpoint exists
#     with a selected_at OLDER than the most recent tree edit (routine during a
#     full-suite run where another agent is concurrently editing the tree), the
#     gate suppresses -> fired=false.
#   - Test-case-6 (gate:207): WM force_metric_encoding_pending. If a prior
#     signal's candidate fingerprint matches PRODUCTION_SHAPE_NOTE's
#     {2x, 250 vs 44, 0 -> 69}, the dedup suppresses -> fired=false.
# The bug 1 diagnosed ("passes alone, fails in full suite") is this
# non-hermeticity: _run_gate inherited the live bootstrap agent's session state
# via the default subprocess env. The conftest _restore_env_per_test fixture
# restores MIND_AGENT/MIND_WORLD but NOT the filesystem state (checkpoint, WM)
# the gate reads. Route the gate at an isolated agent with an EMPTY session/
# (no checkpoint -> Test 5 skips at the file-existence guard; no
# working-memory.yaml -> Test 6 dedup skips) so the firing decision rests solely
# on outcome_class + category + distinct_count -- which is what this file tests.
# Mirrors the temp-agent pattern in test_stale_sentinel_canary.py.
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
_ISO_AGENT_NAME = "_test_metric_gate_agent"
_GATE_ENV = None  # populated per-test by the autouse fixture below


@pytest.fixture(autouse=True)
def _isolated_gate_agent(tmp_path_factory):
    """Point the gate subprocess at an isolated agent dir with a clean session.

    g-115-1633: the isolated agent dir is created under a TMP root (NOT live
    PROJECT_ROOT/agents/) and resolution is routed at it via MIND_AGENT_DIR,
    now honored by BOTH _paths.py and _paths.sh (the gate runs via bash). The
    prior version created PROJECT_ROOT/agents/_test_metric_gate_agent, which the
    running fleet ADOPTED mid-test (agents_root().glob(*/local-paths.conf)) and
    then leaked on Windows when rmtree(ignore_errors=True) hit the fleet's open
    handle. tmp_path_factory dirs are auto-cleaned by pytest, so no rmtree of a
    live agents/ dir is needed and the leak class is eliminated at the source.

    The agent dir has an empty session/ and a local-paths.conf whose
    WORLD_PATH/META_PATH point at a throwaway tmp dir. _run_gate runs the gate
    with MIND_AGENT + MIND_AGENT_DIR set to this isolated agent so the gate's
    Test-case-5 (checkpoint) and Test-case-6 (WM dedup) reads find nothing and
    the gate's decision is deterministic.
    """
    global _GATE_ENV
    world = tmp_path_factory.mktemp("metric-gate-world")
    iso_agent_dir = tmp_path_factory.mktemp("metric-gate-agent")
    (iso_agent_dir / "session").mkdir(parents=True, exist_ok=True)
    (iso_agent_dir / "local-paths.conf").write_text(
        f"WORLD_PATH={world.as_posix()}\n"
        f"META_PATH={(world / 'meta').as_posix()}\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["MIND_AGENT"] = _ISO_AGENT_NAME
    env["MIND_AGENT_DIR"] = str(iso_agent_dir)  # route resolution at the tmp dir (3)
    env.pop("MIND_WORLD", None)      # force conf-based world resolution
    _GATE_ENV = env
    try:
        yield
    finally:
        _GATE_ENV = None


def _run_gate(outcome_class: str, goal_id: str, category: str,
              slug: str = "test-slug", outcome_note: str = ""):
    """Invoke the gate via bash and return (rc, parsed_json_or_none, stderr).

    Path conversion: bash on Windows (Git for Windows / MSYS) does NOT
    accept native `C:\\path\\to\\script.sh` argv -- backslashes are stripped
    in arg parsing. Convert to forward-slash form via Path.as_posix() so
    bash can resolve the path. This is the same pattern iteration-close.sh
    uses when invoking child bash scripts.
    """
    proc = subprocess.run(
        [BASH, str(GATE), outcome_class, goal_id, category, slug],
        input=outcome_note, capture_output=True, text=True, timeout=60,
        env=_GATE_ENV,
    )
    parsed = None
    if proc.stdout.strip():
        try:
            parsed = json.loads(proc.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            parsed = None
    return proc.returncode, parsed, proc.stderr


# ---- Suppression cases ----------------------------------------------------


def test_framework_architecture_category_suppressed():
    """framework-architecture: production-shape findings -> not-fired."""
    rc, j, err = _run_gate(
        outcome_class="deep", goal_id="g-test-001",
        category="framework-architecture",
        outcome_note=PRODUCTION_SHAPE_NOTE,
    )
    assert rc == 0, f"gate must always exit 0; got rc={rc}; stderr={err!r}"
    assert j is not None, f"gate must emit JSON; got stdout={None}"
    assert j["fired"] is False, (
        f"framework-architecture must suppress; got {j}")
    assert "meta-work" in j["reason"], (
        f"reason must cite meta-work suppression; got {j['reason']!r}")
    assert "g-115-726" in j["reason"], (
        f"reason must cite originating goal g-115-726 for audit trail; "
        f"got {j['reason']!r}")


def test_framework_hygiene_category_suppressed():
    """framework-hygiene: production-shape findings -> not-fired (matches both
    framework-* AND *hygiene* patterns)."""
    rc, j, err = _run_gate(
        outcome_class="deep", goal_id="g-test-002",
        category="framework-hygiene",
        outcome_note=PRODUCTION_SHAPE_NOTE,
    )
    assert rc == 0, err
    assert j is not None
    assert j["fired"] is False
    assert "meta-work" in j["reason"]
    assert "framework-hygiene" in j["reason"], (
        f"reason should name the category; got {j['reason']!r}")


def test_framework_meta_category_suppressed():
    """framework-meta: framework-* prefix triggers suppression."""
    rc, j, err = _run_gate(
        outcome_class="deep", goal_id="g-test-003",
        category="framework-meta",
        outcome_note=PRODUCTION_SHAPE_NOTE,
    )
    assert rc == 0, err
    assert j["fired"] is False
    assert "meta-work" in j["reason"]


def test_arbitrary_hygiene_suffix_suppressed():
    """Any *hygiene* category triggers suppression (defensive — not just
    framework-hygiene). E.g., a hypothetical 'docs-hygiene' should also
    suppress LOC-counts on meta-work."""
    rc, j, err = _run_gate(
        outcome_class="deep", goal_id="g-test-004",
        category="docs-hygiene",
        outcome_note=PRODUCTION_SHAPE_NOTE,
    )
    assert rc == 0, err
    assert j["fired"] is False
    assert "meta-work" in j["reason"]


# ---- Control: production categories still fire ---------------------------


def test_npc_cognition_category_still_fires():
    """npc-cognition (production domain): gate fires on production findings.
    Control test that the suppression is targeted, not a blanket disable."""
    rc, j, err = _run_gate(
        outcome_class="deep", goal_id="g-test-005",
        category="npc-cognition",
        outcome_note=PRODUCTION_SHAPE_NOTE,
    )
    assert rc == 0, err
    assert j is not None
    assert j["fired"] is True, (
        f"npc-cognition with production findings must fire; got {j}")
    assert j["distinct_count"] >= 2, (
        f"must extract 2+ findings; got {j.get('distinct_count')}")


def test_ayoai_category_still_fires():
    """ayoai-platform-services (production domain): gate fires.
    Second control case for the production-domain firing path."""
    rc, j, err = _run_gate(
        outcome_class="deep", goal_id="g-test-006",
        category="ayoai-platform-services",
        outcome_note=PRODUCTION_SHAPE_NOTE,
    )
    assert rc == 0, err
    assert j is not None
    assert j["fired"] is True
    assert j["distinct_count"] >= 2


# ---- Pre-existing skip paths remain intact -------------------------------


def test_non_deep_outcome_still_short_circuits_before_category_check():
    """outcome_class != deep -> not-fired regardless of category.
    Pins the ordering: outcome_class check fires before category check."""
    rc, j, err = _run_gate(
        outcome_class="routine", goal_id="g-test-007",
        category="npc-cognition",
        outcome_note=PRODUCTION_SHAPE_NOTE,
    )
    assert rc == 0, err
    assert j["fired"] is False
    assert "outcome_class=routine" in j["reason"], (
        f"non-deep skip must trigger first; got {j['reason']!r}")
