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
import subprocess
import sys
from pathlib import Path

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
        input=outcome_note, capture_output=True, text=True, timeout=15,
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
