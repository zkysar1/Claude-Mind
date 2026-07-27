"""test_completion_artifact_jsonl.py — regression for  / rb-1838.

The completion-artifact gate's ARTIFACT_PATH_RE extension alternation was
leftmost-match-ordered (`json` before `jsonl`) with no trailing boundary, and
Python `re` alternation is leftmost-match (NOT longest-match). So a goal
description naming a `.jsonl` artifact matched `.json` and truncated the path:
a real `meta/gate-firings.jsonl` resolved to a non-existent
`meta/gate-firings.json` and produced a false-positive missing-artifact block
(the gate's own `near_misses` then mapped `.json` back to the existing
`.jsonl`, surfacing the contradiction — canonical incident g-115-1465).

Fix: longest-first alternation (`jsonl` before `json`) plus a trailing
`(?![A-Za-z0-9])` boundary so the full extension is matched order-independently.

These tests pin BOTH directions: the `.jsonl` FP must be gone AND a genuinely
missing `.jsonl` artifact must still block (the fix corrects extension matching,
it does not disable the gate). Real `.json` extraction must be unaffected.
"""
from __future__ import annotations

from gates.completion_artifact import (  # type: ignore
    ARTIFACT_PATH_RE,
    evaluate,
)


def test_jsonl_extension_not_truncated_to_json():
    # Core regression: a .jsonl path extracts as .jsonl, never the truncated .json.
    found = set(ARTIFACT_PATH_RE.findall("produces meta/gate-firings.jsonl"))
    assert "meta/gate-firings.jsonl" in found
    assert "meta/gate-firings.json" not in found


def test_real_json_still_matches():
    # The fix must NOT break genuine .json extraction.
    found = set(ARTIFACT_PATH_RE.findall("writes meta/spark-questions.json"))
    assert "meta/spark-questions.json" in found


def test_mixed_json_and_jsonl_in_one_text():
    # Both kinds coexisting must each match at their true suffix.
    text = ("Outcome: meta/gate-firings.jsonl appended and "
            "meta/spark-questions.json refreshed plus core/scripts/foo.py")
    found = set(ARTIFACT_PATH_RE.findall(text))
    assert "meta/gate-firings.jsonl" in found
    assert "meta/gate-firings.json" not in found
    assert "meta/spark-questions.json" in found
    assert "core/scripts/foo.py" in found


def test_existing_jsonl_artifact_does_not_block(tmp_path):
    # End-to-end: an Apply goal naming an existing .jsonl artifact must not be
    # blocked (the FP that previously forced --override-missing-artifact).
    meta_dir = tmp_path / "meta"
    meta_dir.mkdir()
    (meta_dir / "gate-firings.jsonl").write_text("{}\n", encoding="utf-8")
    result = evaluate(
        goal_id="g-test-jsonl-ok",
        goal_title="Apply: wire gate-firings logging",
        goal_description="Outcome: meta/gate-firings.jsonl is appended each fire.",
        override=None,
        project_root=tmp_path,
        world_dir=None,
        meta_dir=meta_dir,
        agent_name="test",
    )
    assert result["would_block"] is False, result
    assert result["missing_artifacts"] == [], result
    assert result["checked_paths"] == 1, result


def test_missing_jsonl_artifact_still_blocks(tmp_path):
    # The gate must still catch a genuinely-missing .jsonl artifact — the fix
    # is about correct extension matching, not about disabling the gate.
    meta_dir = tmp_path / "meta"
    meta_dir.mkdir()  # directory exists, file does not
    result = evaluate(
        goal_id="g-test-jsonl-missing",
        goal_title="Apply: wire gate-firings logging",
        goal_description="Outcome: meta/gate-firings.jsonl is appended each fire.",
        override=None,
        project_root=tmp_path,
        world_dir=None,
        meta_dir=meta_dir,
        agent_name="test",
    )
    assert result["would_block"] is True, result
    assert "meta/gate-firings.jsonl" in result["missing_artifacts"], result
