"""test_chronic_friction.py — chronic_friction aggregation per aspiration.

Covers LifingPolls plan item 8 (2026-05-08).

Lanes:
  1. No friction sources → empty themes written, no error
  2. Multiple defer_reasons sharing tokens → top theme captured
  3. RB entries tagged with asp_id contribute to themes
  4. Idempotent — re-running overwrites with fresh aggregation
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
AGGREGATOR = CORE_SCRIPTS / "chronic-friction-aggregator.py"


def _make_world_and_agent(tmp: Path, *, defer_reasons: list[str] | None = None,
                           rb_entries: list[dict] | None = None) -> tuple[Path, Path]:
    world = tmp / "world"
    world.mkdir()
    goals = []
    for i, dr in enumerate(defer_reasons or []):
        goals.append({
            "id": f"g-100-{i+1:02d}",
            "title": f"Test goal {i+1}",
            "description": "Test",
            "status": "blocked",
            "priority": "MEDIUM",
            "blocked_by": [],
            "defer_reason": dr,
            "deferred_until": None,
            "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
            "origin_signal": "user_directive",
            "achievedCount": 0,
            "participants": ["agent"],
        })
    if not goals:
        # Need at least one goal so the asp validates
        goals.append({
            "id": "g-100-01",
            "title": "Filler",
            "description": "Filler",
            "status": "pending",
            "priority": "MEDIUM",
            "blocked_by": [],
            "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
            "origin_signal": "user_directive",
            "achievedCount": 0,
            "participants": ["agent"],
        })
    asp = {
        "id": "asp-100",
        "title": "Test",
        "motivation": "Test",
        "scope": "project",
        "priority": "MEDIUM",
        "status": "active",
        "created": "2026-05-08T12:00:00",
        "goals": goals,
    }
    (world / "aspirations.jsonl").write_text(
        json.dumps(asp, ensure_ascii=False) + "\n", encoding="utf-8")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")

    rb_path = world / "reasoning-bank.jsonl"
    if rb_entries:
        with open(rb_path, "w", encoding="utf-8") as f:
            for r in rb_entries:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    else:
        rb_path.write_text("", encoding="utf-8")

    agent_dir = tmp / "alpha"
    agent_dir.mkdir()
    (agent_dir / "session").mkdir()
    (agent_dir / "aspirations.jsonl").write_text("", encoding="utf-8")
    (agent_dir / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world, agent_dir


def _run(world: Path, agent_dir: Path, asp_id: str | None = None,
         dry_run: bool = False):
    env = os.environ.copy()
    env["MIND_WORLD"] = str(world)
    env["MIND_AGENT"] = "alpha"
    env["MIND_AGENT_DIR"] = str(agent_dir)
    args = [sys.executable, str(AGGREGATOR)]
    if asp_id:
        args += ["--asp-id", asp_id]
    if dry_run:
        args.append("--dry-run")
    proc = subprocess.run(args, capture_output=True, text=True,
                          timeout=15, env=env)
    return proc.returncode, proc.stdout, proc.stderr


def _read_aspiration(world: Path, asp_id: str) -> dict:
    text = (world / "aspirations.jsonl").read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        asp = json.loads(line)
        if asp.get("id") == asp_id:
            return asp
    raise KeyError(asp_id)


# ---- Tests ----------------------------------------------------------------


def test_no_friction_writes_empty():
    """No defer_reasons + no rb entries → chronic_friction is empty list."""
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent_dir = _make_world_and_agent(Path(tmpd))
        rc, out, err = _run(world, agent_dir, asp_id="asp-100")
        assert rc == 0, err
        asp = _read_aspiration(world, "asp-100")
        assert asp.get("chronic_friction") == [], asp.get("chronic_friction")


def test_recurring_defer_reasons_surface_theme():
    """Multiple defer_reasons sharing tokens → top theme captured."""
    with tempfile.TemporaryDirectory() as tmpd:
        # Three goals all blocked on the same studio bridge issue.
        # Trigram "studio bridge unavailable" should surface as top theme.
        world, agent_dir = _make_world_and_agent(
            Path(tmpd),
            defer_reasons=[
                "studio bridge unavailable for testing",
                "blocked because studio bridge unavailable in environment",
                "studio bridge unavailable causing test failures",
                "Bridge plugin keeps disconnecting from studio",
            ]
        )
        rc, out, err = _run(world, agent_dir, asp_id="asp-100")
        assert rc == 0, err
        asp = _read_aspiration(world, "asp-100")
        themes = asp.get("chronic_friction") or []
        assert len(themes) >= 1, themes
        # Top theme should reference studio + bridge in some order
        top = themes[0]["theme"]
        assert "studio" in top and "bridge" in top, top
        assert themes[0]["count"] >= 2


def test_rb_entries_with_asp_tag_contribute():
    """RB entries tagged with asp_id appear in the friction signal."""
    with tempfile.TemporaryDirectory() as tmpd:
        rb_entries = [
            {
                "id": "rb-test-001",
                "title": "memory pressure observed during execution",
                "content": "memory pressure observed during execution",
                "type": "failure",
                "category": "test",
                "tags": ["asp-100"],
                "status": "active",
                "created": "2026-04-01",
                "source_goal": "g-100-01",
            },
            {
                "id": "rb-test-002",
                "title": "memory pressure spike repeated",
                "content": "memory pressure spike repeated under load",
                "type": "failure",
                "category": "test",
                "tags": ["asp-100"],
                "status": "active",
                "created": "2026-04-02",
                "source_goal": "g-100-01",
            },
        ]
        world, agent_dir = _make_world_and_agent(
            Path(tmpd), rb_entries=rb_entries)
        rc, out, err = _run(world, agent_dir, asp_id="asp-100")
        assert rc == 0, err
        asp = _read_aspiration(world, "asp-100")
        themes = asp.get("chronic_friction") or []
        # Should detect "memory pressure" as a recurring theme
        all_themes_text = " ".join(t["theme"] for t in themes)
        assert "memory" in all_themes_text or "pressure" in all_themes_text, themes


def test_idempotent_overwrite():
    """Re-running overwrites with fresh aggregation, not append."""
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent_dir = _make_world_and_agent(
            Path(tmpd),
            defer_reasons=["partner agent silent on review",
                           "partner agent silent on review again",
                           "another silent partner agent block"]
        )
        # First run
        rc, _, err = _run(world, agent_dir, asp_id="asp-100")
        assert rc == 0, err
        asp = _read_aspiration(world, "asp-100")
        first_themes = asp.get("chronic_friction") or []
        assert len(first_themes) >= 1

        # Second run — same data → same result, not doubled
        rc, _, err = _run(world, agent_dir, asp_id="asp-100")
        assert rc == 0, err
        asp = _read_aspiration(world, "asp-100")
        second_themes = asp.get("chronic_friction") or []
        # Themes match in count (the source data is identical)
        assert len(second_themes) == len(first_themes)
        # Top theme phrase is stable
        assert second_themes[0]["theme"] == first_themes[0]["theme"]


def test_dry_run_no_writes():
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent_dir = _make_world_and_agent(
            Path(tmpd),
            defer_reasons=["partner agent silent",
                           "partner agent silent again"]
        )
        rc, out, err = _run(world, agent_dir, asp_id="asp-100", dry_run=True)
        assert rc == 0, err
        assert "dry-run" in out, out
        asp = _read_aspiration(world, "asp-100")
        # chronic_friction should NOT have been set on the aspiration
        assert "chronic_friction" not in asp or asp["chronic_friction"] == []


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
