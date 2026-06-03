"""test_attribution_filter_path_normalize.py — 0 regression.

Verifies the path-format normalization in _cross_agent_attribution_filter.py
(and, by SSOT import, iteration-commit.sh — rb-1405): the Source 2
uncommitted-edits.jsonl lookup must compare equal whether a path was recorded
absolute (legacy uncommitted-edits-record.sh output — C:/<WORKSPACE>/.../core/foo.py)
or relative (current writer output — core/foo.py). Before the fix, an absolute
log entry silently missed a relative git-diff candidate for the same file, so
partner WIP was not dropped and got mis-attributed (silent set-membership miss).

Two layers:
  - _normalize_rel_path unit tests: relative passthrough, Windows-drive
    absolute, MSYS /c/ absolute, backslash, non-root absolute, empty/None.
  - filter_paths integration: an ABSOLUTE partner-log entry drops a RELATIVE
    candidate for the same file (Source 2), and the original (unnormalized)
    path is preserved in the decision record.

Reuses the staging-helper pattern from test_attribution_filter_no_self_inflight.py
(which documents g-115-1180 as the anticipated Source 2 path-normalization fix).

Run: py -3 -m pytest core/scripts/tests/test_attribution_filter_path_normalize.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from _cross_agent_attribution_filter import (  # noqa: E402
    _normalize_rel_path,
    filter_paths,
)

# ── _normalize_rel_path unit tests ───────────────────────────────────────────

ROOT = "<PROJECT_ROOT>"


def test_normalize_relative_passthrough():
    assert _normalize_rel_path("core/scripts/foo.py", ROOT) == "core/scripts/foo.py"


def test_normalize_windows_drive_absolute():
    assert (
        _normalize_rel_path("<PROJECT_ROOT>/core/scripts/foo.py", ROOT)
        == "core/scripts/foo.py"
    )


def test_normalize_backslash_absolute():
    assert (
        _normalize_rel_path(r"<PROJECT_ROOT>\core\scripts\foo.py", ROOT)
        == "core/scripts/foo.py"
    )


def test_normalize_msys_form_against_drive_root():
    # Log written by Git-Bash as /c/...; project_root resolved by Python as C:/...
    assert (
        _normalize_rel_path("/c/<WORKSPACE>/GitHub/Ayoai-Mind/core/scripts/foo.py", ROOT)
        == "core/scripts/foo.py"
    )


def test_normalize_drive_form_against_msys_root():
    # Symmetric: project_root in MSYS form, entry in Windows-drive form.
    assert (
        _normalize_rel_path(
            "<PROJECT_ROOT>/core/foo.py",
            "/c/<WORKSPACE>/GitHub/Ayoai-Mind",
        )
        == "core/foo.py"
    )


def test_normalize_non_root_absolute_unchanged():
    # An absolute path NOT under project_root stays absolute (forward-slashed) —
    # it simply will not match relative candidates (fail-open, same as before).
    assert _normalize_rel_path("D:/Other/repo/x.py", ROOT) == "D:/Other/repo/x.py"


def test_normalize_empty_and_none_passthrough():
    assert _normalize_rel_path("", ROOT) == ""
    assert _normalize_rel_path(None, ROOT) is None


def test_normalize_empty_root_forward_slashes_only():
    assert _normalize_rel_path(r"core\scripts\foo.py", "") == "core/scripts/foo.py"


# ── filter_paths integration (Source 2 absolute-vs-relative) ─────────────────


def _stage_project(tmp_path, self_agent, partners):
    project_root = tmp_path / "repo"
    world = tmp_path / "world"
    world.mkdir(parents=True, exist_ok=True)
    for agent in [self_agent, *partners]:
        adir = project_root / "agents" / agent
        adir.mkdir(parents=True, exist_ok=True)
        (adir / "local-paths.conf").write_text(
            "WORLD_PATH=" + world.as_posix() + "\n", encoding="utf-8"
        )
        (adir / "session").mkdir(parents=True, exist_ok=True)
    return project_root, world


def _write_team_state(world, agent_status):
    import yaml

    (world / "team-state.yaml").write_text(
        yaml.safe_dump({"agent_status": agent_status}), encoding="utf-8"
    )


def _write_uncommitted_log(agent_dir, recorded_paths):
    (agent_dir / "session" / "uncommitted-edits.jsonl").write_text(
        "\n".join(json.dumps({"file": p}) for p in recorded_paths),
        encoding="utf-8",
    )


def test_source2_absolute_log_entry_drops_relative_candidate(tmp_path):
    """0 core fix: partner recorded an ABSOLUTE path in its
    uncommitted-edits.jsonl (legacy writer). A RELATIVE candidate for the same
    file must be dropped as partner-uncommitted-log. Before the fix the exact
    string match missed and the file was wrongly KEPT (mis-attribution)."""
    project_root, world = _stage_project(tmp_path, "zeta", ["bravo"])
    _write_team_state(world, {"zeta": {"in_flight": None}})

    rel = "core/scripts/shared-tool.py"
    absolute = (project_root / rel).as_posix()  # legacy absolute form
    _write_uncommitted_log(project_root / "agents" / "bravo", [absolute])

    kept, dropped = filter_paths([rel], "zeta", str(project_root), str(world))

    assert kept == [], f"absolute-log entry must drop relative candidate; got kept={kept}"
    assert len(dropped) == 1
    assert dropped[0]["reason"] == "partner-uncommitted-log"
    assert dropped[0]["owner"] == "bravo"
    assert dropped[0]["path"] == rel, (
        "original candidate path must be preserved in the decision record"
    )


def test_source2_relative_log_entry_still_drops_relative_candidate(tmp_path):
    """No-regression: a RELATIVE log entry (current writer output) still drops a
    relative candidate. Normalization is idempotent on already-relative paths."""
    project_root, world = _stage_project(tmp_path, "zeta", ["bravo"])
    _write_team_state(world, {"zeta": {"in_flight": None}})

    rel = "core/config/shared.yaml"
    _write_uncommitted_log(project_root / "agents" / "bravo", [rel])

    kept, dropped = filter_paths([rel], "zeta", str(project_root), str(world))

    assert kept == []
    assert len(dropped) == 1
    assert dropped[0]["reason"] == "partner-uncommitted-log"
    assert dropped[0]["owner"] == "bravo"


def test_source2_unrelated_absolute_does_not_drop(tmp_path):
    """Guard against over-normalization: an absolute log entry for a DIFFERENT
    file must not drop an unrelated candidate. With self.in_flight=null and no
    mtime signal, the unrelated candidate is KEPT (Source 3 disabled — the
    documented fail-open in test_attribution_filter_no_self_inflight.py Case 4)."""
    project_root, world = _stage_project(tmp_path, "zeta", ["bravo"])
    _write_team_state(world, {"zeta": {"in_flight": None}})

    _write_uncommitted_log(
        project_root / "agents" / "bravo",
        [(project_root / "core/scripts/other.py").as_posix()],
    )

    candidate = "core/scripts/mine.py"
    kept, dropped = filter_paths([candidate], "zeta", str(project_root), str(world))

    assert candidate in kept, (
        f"unrelated candidate must not be dropped by Source 2; "
        f"got kept={kept}, dropped={dropped}"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
