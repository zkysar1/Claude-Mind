"""Equivalence + behavior tests for uncommitted_work gate (PR 7a/1).

The contract: subprocess CLI invocation and direct module call must
produce identical JSON payloads for the same logical inputs.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "core" / "scripts"
CLI = SCRIPTS_DIR / "uncommitted-work-gate.py"


def _run_cli(repo: Path, goal_id: str, override: str | None,
             agent: str = "") -> dict:
    """Invoke the CLI wrapper as subprocess, return parsed JSON payload.

    NOTE: world_dir is not parameterizable for the CLI test path — the CLI
    resolves it via `os.environ["MIND_AGENT"]` + PROJECT_ROOT/<agent>/local-paths.conf,
    which we can't safely inject without writing into the real PROJECT_ROOT.
    CLI equivalence tests therefore always run with world_dir=None (no audit
    log). Audit-log side-effect parity is covered by direct-module tests
    that pass world_dir explicitly (see test_override_writes_audit_ledger).
    """
    args = [sys.executable, str(CLI),
            "--goal-id", goal_id,
            "--repo-path", str(repo),
            "--output", "json"]
    if override is not None:
        args.extend(["--override", override])
    env = os.environ.copy()
    env["MIND_AGENT"] = agent
    # BODY_ROLE is injected into every Bash call by the PreToolUse hook, so an
    # inherited env carries whatever role THIS box happens to be — which made
    # the CLI-vs-module parity assertions below depend on where they were run
    # (a worker box and a reducer box would disagree). Pin it empty so the CLI
    # sees the same "no role supplied" input the direct-module calls pass.
    #  exposed this; the fragility pre-dates that change.
    env.pop("BODY_ROLE", None)
    proc = subprocess.run(args, env=env, capture_output=True, text=True,
                          check=False)
    if proc.returncode not in (0, 1):
        pytest.fail(f"CLI failed rc={proc.returncode}: stderr={proc.stderr}")
    return json.loads(proc.stdout)


def _call_module(repo: Path, goal_id: str, override: str | None,
                 agent: str = "", world_dir: Path | None = None) -> dict:
    from gates.uncommitted_work import evaluate
    return evaluate(
        goal_id=goal_id,
        override=override,
        repo_path=repo,
        world_dir=world_dir,
        agent_name=agent,
    )


# ---------------------------------------------------------------------------
# Clean repo — both paths report no dirty files.
# ---------------------------------------------------------------------------

def test_clean_repo_equivalence(empty_clean_repo: Path):
    """Clean repo → would_block=False, dirty list empty, in both paths."""
    cli = _run_cli(empty_clean_repo, "g-c-1", None)
    mod = _call_module(empty_clean_repo, "g-c-1", None)

    # repo_path field is a string in both — but the CLI sees the
    # str(Path) of the argv arg, and the module sees str(Path(repo)).
    # They must match.
    assert cli == mod, f"clean repo mismatch:\nCLI: {cli}\nMOD: {mod}"
    assert cli["would_block"] is False
    assert cli["dirty_framework_files"] == []
    assert cli["override_applied"] is None


def test_clean_repo_exit_code(empty_clean_repo: Path):
    proc = subprocess.run(
        [sys.executable, str(CLI), "--goal-id", "g-c-2",
         "--repo-path", str(empty_clean_repo), "--output", "json"],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0


# ---------------------------------------------------------------------------
# Dirty repo — both paths see the same dirty files, ignore ephemeral churn.
# ---------------------------------------------------------------------------

def test_dirty_repo_equivalence(dirty_framework_repo: Path):
    """Dirty repo → both paths agree on dirty list AND filter ephemeral files."""
    cli = _run_cli(dirty_framework_repo, "g-d-1", None)
    mod = _call_module(dirty_framework_repo, "g-d-1", None)

    assert cli == mod, f"dirty repo mismatch:\nCLI: {cli}\nMOD: {mod}"
    assert cli["would_block"] is True
    assert cli["dirty_framework_files"] == ["CLAUDE.md", "core/scripts/foo.py"]
    # journal.jsonl must NOT appear — that's agent state churn.
    assert not any("journal.jsonl" in f for f in cli["dirty_framework_files"])


def test_dirty_repo_exit_code(dirty_framework_repo: Path):
    proc = subprocess.run(
        [sys.executable, str(CLI), "--goal-id", "g-d-2",
         "--repo-path", str(dirty_framework_repo), "--output", "json"],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 1


# ---------------------------------------------------------------------------
# Override behavior — empty/whitespace coerced; non-empty bypasses.
# ---------------------------------------------------------------------------

def test_override_empty_string_treated_as_no_override(dirty_framework_repo: Path):
    """Empty override → would_block stays True (same as no override)."""
    cli = _run_cli(dirty_framework_repo, "g-d-3", "")
    mod = _call_module(dirty_framework_repo, "g-d-3", "")
    assert cli == mod
    assert cli["would_block"] is True
    assert cli["override_applied"] is None


def test_override_whitespace_treated_as_no_override(dirty_framework_repo: Path):
    cli = _run_cli(dirty_framework_repo, "g-d-4", "   ")
    mod = _call_module(dirty_framework_repo, "g-d-4", "   ")
    assert cli == mod
    assert cli["would_block"] is True
    assert cli["override_applied"] is None


def test_override_real_justification_bypasses(dirty_framework_repo: Path):
    """Non-empty override → would_block False, override_applied is preserved."""
    cli = _run_cli(dirty_framework_repo, "g-d-5",
                   "ratcheting prior PR work")
    mod = _call_module(dirty_framework_repo, "g-d-5",
                       "ratcheting prior PR work")
    assert cli == mod
    assert cli["would_block"] is False
    assert cli["override_applied"] == "ratcheting prior PR work"
    # But the dirty list is still populated — caller may want to log/display it.
    assert cli["dirty_framework_files"]


# ---------------------------------------------------------------------------
# Override audit-log side effect — direct module call writes to ledger.
# (CLI side-effect parity is harder to test cleanly because the CLI reads
# local-paths.conf; we test the module's side effect directly.)
# ---------------------------------------------------------------------------

def test_override_writes_audit_ledger(dirty_framework_repo: Path,
                                       world_with_ledger):
    """Module's evaluate() must append to world/uncommitted-work-overrides.jsonl
    when override fires AND dirty files exist."""
    world, agent = world_with_ledger
    from gates.uncommitted_work import evaluate
    payload = evaluate(
        goal_id="g-audit-1",
        override="documented exception",
        repo_path=dirty_framework_repo,
        world_dir=world,
        agent_name=agent,
    )
    assert payload["would_block"] is False
    assert payload["override_applied"] == "documented exception"

    ledger = world / "uncommitted-work-overrides.jsonl"
    assert ledger.exists(), "ledger should have been written"
    entries = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines()
               if l.strip()]
    assert len(entries) == 1
    rec = entries[0]
    assert rec["goal_id"] == "g-audit-1"
    assert rec["agent"] == agent
    assert rec["justification"] == "documented exception"
    assert rec["dirty_files"] == ["CLAUDE.md", "core/scripts/foo.py"]
    assert "ts" in rec


def test_override_no_dirty_files_no_ledger_write(empty_clean_repo: Path,
                                                  world_with_ledger):
    """Override + clean repo → no ledger entry (nothing to audit)."""
    world, agent = world_with_ledger
    from gates.uncommitted_work import evaluate
    payload = evaluate(
        goal_id="g-audit-2",
        override="paranoid pass",
        repo_path=empty_clean_repo,
        world_dir=world,
        agent_name=agent,
    )
    assert payload["would_block"] is False
    assert payload["dirty_framework_files"] == []
    ledger = world / "uncommitted-work-overrides.jsonl"
    assert not ledger.exists() or ledger.read_text(encoding="utf-8").strip() == ""


def test_override_no_world_dir_fails_open(dirty_framework_repo: Path, capsys):
    """world_dir=None + override → no ledger write, stderr warn, no crash."""
    from gates.uncommitted_work import evaluate
    payload = evaluate(
        goal_id="g-audit-3",
        override="no world",
        repo_path=dirty_framework_repo,
        world_dir=None,
        agent_name="alpha",
    )
    assert payload["would_block"] is False
    captured = capsys.readouterr()
    assert "no WORLD_DIR" in captured.err


# ---------------------------------------------------------------------------
# Pattern filtering — internal regex behavior.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,expected", [
    ("core/scripts/foo.py", True),
    ("core/scripts/foo.sh", True),
    ("core/scripts/foo.txt", False),  # wrong extension
    ("core/config/aspirations.yaml", True),
    ("core/config/notes.md", True),
    ("core/logs/foo.log", False),  # core/logs/ not in patterns
    (".claude/skills/forge-skill/SKILL.md", True),
    (".claude/skills/forge-skill/scripts/helper.sh", True),
    (".claude/rules/return-protocol.md", True),
    ("CLAUDE.md", True),
    ("alpha/aspirations.jsonl", False),   # agent state, ignored
    ("alpha/session/agent-state", False),  # agent state, ignored
    ("world/knowledge/tree/_tree.yaml", False),  # not framework code
])
def test_is_framework_code(path: str, expected: bool):
    from gates.uncommitted_work import _is_framework_code
    assert _is_framework_code(path) == expected


def test_is_framework_code_handles_backslashes():
    """Windows git output uses backslashes — must normalize."""
    from gates.uncommitted_work import _is_framework_code
    assert _is_framework_code("core\\scripts\\foo.py") is True
    assert _is_framework_code(".claude\\rules\\return-protocol.md") is True


# ---------------------------------------------------------------------------
# Porcelain parser — handles rename + malformed lines.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line,expected", [
    ("?? new-file.py", "new-file.py"),
    (" M modified.py", "modified.py"),
    ("R  old.py -> new.py", "new.py"),
    ('?? "with space.py"', "with space.py"),  # git quotes paths with spaces
    ("", None),
    ("xx", None),  # too short
])
def test_parse_porcelain_line(line: str, expected: str | None):
    from gates.uncommitted_work import _parse_porcelain_line
    assert _parse_porcelain_line(line) == expected
