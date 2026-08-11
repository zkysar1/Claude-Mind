"""Dogfood suite for claim_artifact_sweep (forge-skill Step 3.6, ).

FIXTURE SEAM — what these tests DO and DO NOT cover (guard-1462).

Two seams are used deliberately, because a single seam would leave the layer
that actually carried a real bug untested:

  seam A — classify() directly.  Covers the three-way verdict logic only.
           EXCLUDES: file enumeration, glob patterns, JSONL parsing, the
           nested-goal flatten, window building, adjacency merge, coverage
           accounting. A classify-only suite would have passed green through
           the measured 2026-08-01 defect where the goals surface scanned 28
           aspiration wrappers instead of 4,687 nested goals — that bug lives
           entirely upstream of classify().

  seam B — sweep() against a tmp WORLD_DIR/PROJECT_ROOT.  Covers enumeration,
           parsing, the nested flatten, coverage counting, and the UNREADABLE
           verdict. EXCLUDES: the argparse/CLI layer, the text renderer, and
           the real-corpus scale. Those are covered by the LIVE run recorded in
           the goal's verify summary, not here.

Anti-vacuity: the PASS / FAIL / EDGE fixtures must produce THREE DISTINCT
verdicts. A suite where PASS and FAIL agree proves nothing (guard-1220). Each
assertion below is per-fixture rather than an aggregate count, so a defect that
corrupts one axis cannot hide behind a summary line that happens to stay green
(guard-1793).
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import claim_artifact_sweep as mod  # noqa: E402


TOKENS = ["WidgetWatch", "delimiter", "processed"]


# ---------------------------------------------------------------- seam A ----

def test_pass_fixture_asserts():
    """PASS: tokens co-occur, no retraction marker -> ASSERTS."""
    excerpt = "WidgetWatch counts the whole prefix without a delimiter, so processed items inflate it."
    verdict, why = mod.classify(excerpt, TOKENS, TOKENS, 2)
    assert verdict == "ASSERTS", why


def test_fail_fixture_already_corrected():
    """FAIL: same tokens, retraction marker present -> ALREADY_CORRECTED.

    Differs from the PASS fixture in exactly one respect (the marker), so the
    verdict split is attributable to the marker and nothing else.
    """
    excerpt = ("CORRECTED: WidgetWatch counts the whole prefix without a delimiter, "
               "so processed items inflate it. This mechanism was FALSIFIED.")
    verdict, why = mod.classify(excerpt, TOKENS, TOKENS, 2)
    assert verdict == "ALREADY_CORRECTED", why


def test_edge_fixture_unrelated():
    """EDGE: only one token present -> UNRELATED (same words, other subject)."""
    excerpt = "The processed order queue drains nightly."
    verdict, why = mod.classify(excerpt, ["processed"], TOKENS, 2)
    assert verdict == "UNRELATED", why


def test_three_fixtures_are_mutually_distinct():
    """Anti-vacuity: the three fixtures must not collapse to one verdict."""
    a = mod.classify("WidgetWatch delimiter processed", TOKENS, TOKENS, 2)[0]
    b = mod.classify("retracted: WidgetWatch delimiter processed", TOKENS, TOKENS, 2)[0]
    c = mod.classify("processed only", ["processed"], TOKENS, 2)[0]
    assert len({a, b, c}) == 3, f"verdicts collapsed: {a},{b},{c}"


def test_min_tokens_threshold_is_load_bearing():
    """Raising min_tokens must be able to flip ASSERTS -> UNRELATED."""
    excerpt = "WidgetWatch uses a delimiter."
    assert mod.classify(excerpt, ["WidgetWatch", "delimiter"], TOKENS, 2)[0] == "ASSERTS"
    assert mod.classify(excerpt, ["WidgetWatch", "delimiter"], TOKENS, 3)[0] == "UNRELATED"


def test_append_only_stores_get_mark_never_remove_shape():
    """guard-1072: union-by-id stores must never be told to remove."""
    for surface in ("reasoning_bank", "guardrails", "goals", "goals_agent_queue"):
        assert "NEVER pop/remove" in mod.correction_shape(surface)
    assert mod.correction_shape("knowledge_tree") == "edit-in-place"


# ---------------------------------------------------------------- seam B ----

@pytest.fixture
def tmp_world(tmp_path, monkeypatch):
    """Minimal world + project tree, wired into the module's resolved paths."""
    world = tmp_path / "world"
    proj = tmp_path / "proj"
    (world / "knowledge" / "tree" / "sys").mkdir(parents=True)
    (world / "conventions").mkdir(parents=True)
    for sub in ("core/config/conventions", "core/scripts", ".claude/rules", ".claude/skills"):
        (proj / sub).mkdir(parents=True)

    (world / "knowledge" / "tree" / "sys" / "node.md").write_text(
        "# Node\nWidgetWatch counts the whole prefix with no delimiter, so processed keys count.\n",
        encoding="utf-8")

    rb = [
        {"id": "rb-001", "title": "asserting entry",
         "content": "WidgetWatch has no delimiter and therefore counts processed keys."},
        {"id": "rb-002", "title": "corrected entry",
         "content": "FALSIFIED: WidgetWatch does use a delimiter; processed keys are excluded."},
        {"id": "rb-003", "title": "unrelated entry",
         "content": "The processed order queue drains nightly."},
    ]
    (world / "reasoning-bank.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rb), encoding="utf-8")
    (world / "guardrails.jsonl").write_text(
        json.dumps({"id": "guard-001", "rule": "unrelated rule"}), encoding="utf-8")

    # ONE aspiration wrapper holding TWO goals. The asserting text lives only in
    # the NESTED goal — a non-flattening reader scans 1 record and finds nothing.
    (world / "aspirations.jsonl").write_text(json.dumps({
        "id": "asp-001", "title": "wrapper", "description": "nothing here",
        "goals": [
            {"id": "g-1", "title": "nested asserting goal",
             "description": "WidgetWatch counts processed keys because it lacks a delimiter."},
            {"id": "g-2", "title": "quiet goal", "description": "unrelated work"},
        ],
    }), encoding="utf-8")

    monkeypatch.setattr(mod, "WORLD_DIR", world)
    monkeypatch.setattr(mod, "PROJECT_ROOT", proj)
    monkeypatch.delenv("MIND_AGENT", raising=False)
    return world, proj


def test_sweep_finds_nested_goal_descriptions(tmp_world):
    """Regression: goals must be FLATTENED out of their aspiration wrapper.

    Measured 2026-08-01 — the pre-fix reader scanned 28 wrappers and missed
    4,687 nested goals, i.e. 99.4% of the goal-description surface, while
    reporting a healthy-looking non-zero coverage count.
    """
    res = mod.sweep(TOKENS, min_tokens=2)
    assert res["coverage"]["goals"]["scanned"] == 2, "nested goals not flattened"
    ids = [h["record_id"] for h in res["asserts"] if h["surface"] == "goals"]
    assert "g-1" in ids


def test_sweep_separates_the_three_verdicts_on_a_real_tree(tmp_world):
    res = mod.sweep(TOKENS, min_tokens=2)
    by_id = {h["record_id"]: h["verdict"] for h in res["all_hits"]
             if h["surface"] == "reasoning_bank"}
    assert by_id.get("rb-001") == "ASSERTS"
    assert by_id.get("rb-002") == "ALREADY_CORRECTED"
    assert by_id.get("rb-003") in (None, "UNRELATED")
    assert res["verdict"] == "CORRECTIONS_REQUIRED"


def test_sweep_reports_clean_when_nothing_asserts(tmp_world):
    """Two-way proof: a token set that matches nothing must NOT say CORRECTIONS_REQUIRED."""
    res = mod.sweep(["ZZZnotpresent", "QQQabsent"], min_tokens=2)
    assert res["counts"]["ASSERTS"] == 0
    assert res["verdict"] == "CLEAN"


def test_unreadable_surface_is_not_reported_as_clean(tmp_world):
    """rb-245: a zero from a failed path is vacuous, never a pass."""
    world, _ = tmp_world
    (world / "reasoning-bank.jsonl").unlink()
    res = mod.sweep(TOKENS, min_tokens=2)
    assert "reasoning_bank" in res["unreadable_surfaces"]
    assert res["verdict"] == "UNREADABLE_SURFACES"
    assert res["coverage"]["reasoning_bank"]["error"]


def test_empty_root_is_not_conflated_with_unreadable(tmp_world):
    """The two kinds of zero must stay distinguishable.

    A MISSING root is a vacuous zero and poisons the verdict (rb-245). A root
    that EXISTS and holds nothing scanned zero and reported something true —
    treating it as vacuous would mark every sparse deployment broken and train
    readers to ignore the warning that matters.
    """
    res = mod.sweep(TOKENS, min_tokens=2)
    # .claude/rules exists in the fixture but is empty.
    assert "framework_rules" in res["empty_surfaces"]
    assert "framework_rules" not in res["unreadable_surfaces"]
    assert res["verdict"] != "UNREADABLE_SURFACES"


def test_truncation_at_the_cap_is_reported_not_silent(tmp_world):
    """A capped scan must announce itself (guard-1760).

    Measured 2026-08-01: a product-repo scan hit the 4000-file cap silently,
    reporting `4000 files` — indistinguishable from complete coverage on a
    tool whose entire job is completeness.
    """
    world, _ = tmp_world
    tree = world / "knowledge" / "tree" / "sys"
    for i in range(6):
        (tree / f"n{i}.md").write_text("WidgetWatch delimiter processed\n", encoding="utf-8")
    capped = mod.sweep(TOKENS, min_tokens=2, per_surface_limit=3)
    assert capped["coverage"]["knowledge_tree"]["truncated"] is True
    assert "knowledge_tree" in capped["truncated_surfaces"]

    full = mod.sweep(TOKENS, min_tokens=2, per_surface_limit=999)
    assert full["coverage"]["knowledge_tree"]["truncated"] is False
    assert full["truncated_surfaces"] == []


def test_vendor_dirs_are_excluded(tmp_world):
    """node_modules must not consume the cap — it carries no authored claims."""
    world, _ = tmp_world
    nm = world / "knowledge" / "tree" / "sys" / "node_modules"
    nm.mkdir(parents=True)
    (nm / "junk.md").write_text("WidgetWatch delimiter processed\n", encoding="utf-8")
    res = mod.sweep(TOKENS, min_tokens=2)
    assert not any("node_modules" in h["path"] for h in res["all_hits"])


def test_coverage_is_reported_for_every_surface(tmp_world):
    """The positive control itself must exist for each surface."""
    res = mod.sweep(TOKENS, min_tokens=2)
    for name, cov in res["coverage"].items():
        assert "scanned" in cov and "unit" in cov, name
