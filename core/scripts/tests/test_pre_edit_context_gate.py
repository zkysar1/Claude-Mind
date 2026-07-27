"""G14: pre-edit-context-gate.sh advisory hook tests.

Verifies the advisory PreToolUse[Edit|MultiEdit] hook that warns when the
agent edits an IN-SCOPE file it has NOT Read in the current session.

The gate delegates its read/scope/session decision to
`context-reads.py check-file`, which is the single source of truth for which
path classes are advisory-tracked (is_in_scope_advisory: core/config,
.claude/skills, world/knowledge/tree, world/conventions,
aspirations-compact.json, AND core/scripts framework code — g-115-2210). The
advisory therefore fires ONLY for in-scope files — editing a still-out-of-scope
file (.claude/rules, self.md, product code) stays silent so the banner never
cries wolf on reads the manifest can't track.

Tests:
  - Always exits 0 (never blocks, never denies)
  - Never emits stdout (would be read as a deny payload by Claude Code)
  - Graceful on missing/empty/malformed stdin
  - IN-SCOPE + unread   -> stderr advisory fires
  - IN-SCOPE + read      -> silent (manifest has the path)
  - core/scripts + unread -> advisory fires (advisory scope, g-115-2210)
  - core/scripts + read   -> silent (symmetry)
  - OUT-OF-SCOPE (.claude/rules) -> silent (scope-aware; the regression this guards)

Run: py -3 -m pytest core/scripts/tests/test_pre_edit_context_gate.py -v
  or: py -3 core/scripts/tests/test_pre_edit_context_gate.py
"""
import json
import os
import shutil
import subprocess
import sys
import traceback
from contextlib import contextmanager
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TESTS_DIR.parent            # core/scripts
PROJECT_ROOT = SCRIPT_DIR.parent.parent   # repo root (core -> Ayoai-Mind)
GATE_SCRIPT = SCRIPT_DIR / "pre-edit-context-gate.sh"

# Resolve Git Bash explicitly — bare "bash" on Windows PATH resolves to WSL
# bash (/mnt/c/...), which breaks Windows-path resolution and can't exec the
# Windows python the gate calls. conftest.py adds TESTS_DIR to sys.path for
# pytest; the insert below makes the standalone __main__ runner work too.
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
from _bash_helpers import BASH  # noqa: E402

# Real in-scope / out-of-scope files in this repo (used by scope tests).
IN_SCOPE_FILE = PROJECT_ROOT / ".claude" / "skills" / "respond" / "SKILL.md"
# core/scripts is advisory-tracked since  (framework-code edit surface).
CORE_SCRIPTS_FILE = SCRIPT_DIR / "iteration-close.sh"
# Genuinely still-out-of-scope: .claude/rules stays silent-by-design (Rule 4).
OUT_OF_SCOPE_FILE = PROJECT_ROOT / ".claude" / "rules" / "read-before-edit.md"

# A throwaway agent name that no real session uses. Created under the real
# PROJECT_ROOT so _paths.sh fully resolves (real core/ tree present), torn
# down in finally. Distinctive prefix so cleanup never touches a real agent.
THROWAWAY_AGENT = "_gate_test_throwaway_agent_"
SID = "gate-test-sid-001"


def _norm(p):
    """Match context-reads.py normalize_path: resolve + forward slashes."""
    return str(Path(p).resolve()).replace("\\", "/")


def _make_hook_json(file_path, session_id=SID):
    """Build the stdin JSON that Claude Code sends to PreToolUse hooks."""
    return json.dumps({
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path},
        "session_id": session_id,
    })


@contextmanager
def _throwaway_agent(manifest_paths=None, session_id=SID):
    """Create agents/<throwaway>/session under the real PROJECT_ROOT.

    If manifest_paths is given, write a context-reads.txt manifest with a
    matching session header and those (normalized) paths. Yields the env dict
    the gate should run with. Always cleans up the agent dir.
    """
    agent_dir = PROJECT_ROOT / "agents" / THROWAWAY_AGENT
    session_dir = agent_dir / "session"
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        # Minimal conf so _paths.sh resolves AGENT_DIR; empty world/meta is
        # fine because in-scope tests use .claude/skills (no WORLD_DIR needed).
        (agent_dir / "local-paths.conf").write_text("WORLD_PATH=\nMETA_PATH=\n")
        if manifest_paths is not None:
            lines = [f"#session:{session_id}"] + [_norm(p) for p in manifest_paths]
            (session_dir / "context-reads.txt").write_text("\n".join(lines) + "\n")
        env = dict(os.environ)
        env["MIND_AGENT"] = THROWAWAY_AGENT
        yield env
    finally:
        # Only ever remove the distinctive throwaway dir.
        if agent_dir.name == THROWAWAY_AGENT and agent_dir.is_dir():
            shutil.rmtree(agent_dir, ignore_errors=True)


def _run_gate(stdin_text, env=None, timeout=30):
    """Run the gate with given stdin/env. Returns (rc, stdout, stderr).

    Relative script path avoids the Windows C:/ vs MSYS /c/ mismatch."""
    result = subprocess.run(
        [BASH, "core/scripts/pre-edit-context-gate.sh"],
        input=stdin_text,
        capture_output=True,
        text=True,
        env=env or os.environ,
        timeout=timeout,
        cwd=str(PROJECT_ROOT),
    )
    return result.returncode, result.stdout, result.stderr


# ── Robustness: always exit 0, never stdout ────────────────────────────────

def test_always_exits_zero_on_empty_stdin():
    rc, stdout, stderr = _run_gate("")
    assert rc == 0, f"Expected exit 0, got {rc}. stderr={stderr}"
    assert stdout.strip() == "", f"No stdout allowed, got: {stdout!r}"


def test_always_exits_zero_on_malformed_json():
    rc, stdout, stderr = _run_gate("not valid json {{{")
    assert rc == 0, f"Expected exit 0, got {rc}. stderr={stderr}"
    assert stdout.strip() == "", f"No stdout allowed, got: {stdout!r}"


def test_always_exits_zero_on_missing_file_path():
    rc, stdout, stderr = _run_gate(json.dumps({"tool_name": "Edit", "tool_input": {}}))
    assert rc == 0, f"Expected exit 0, got {rc}. stderr={stderr}"
    assert stdout.strip() == "", f"No stdout allowed, got: {stdout!r}"


def test_no_stdout_even_when_advisory_fires():
    """The advisory goes to stderr; check-file's stdout must be captured,
    never leaked as the hook's stdout (Claude Code reads stdout as deny)."""
    with _throwaway_agent(manifest_paths=[]) as env:  # empty manifest -> in-scope warns
        rc, stdout, stderr = _run_gate(_make_hook_json(str(IN_SCOPE_FILE)), env=env)
    assert rc == 0
    assert stdout.strip() == "", f"Hook must not produce stdout, got: {stdout!r}"


# ── Scope-aware behavior (the core of the fix) ─────────────────────────────

def test_advisory_when_in_scope_file_unread():
    """In-scope file (a SKILL.md) absent from manifest -> stderr advisory."""
    with _throwaway_agent(manifest_paths=[]) as env:
        rc, stdout, stderr = _run_gate(_make_hook_json(str(IN_SCOPE_FILE)), env=env)
    assert rc == 0
    assert "ADVISORY" in stderr, (
        f"Expected advisory for unread in-scope file. stderr={stderr!r}")
    assert "has not been Read" in stderr


def test_silent_when_in_scope_file_already_read():
    """In-scope file present in manifest (same session) -> no advisory."""
    with _throwaway_agent(manifest_paths=[IN_SCOPE_FILE]) as env:
        rc, stdout, stderr = _run_gate(_make_hook_json(str(IN_SCOPE_FILE)), env=env)
    assert rc == 0
    assert "ADVISORY" not in stderr, (
        f"In-scope file already read must be silent. stderr={stderr!r}")


def test_silent_when_out_of_scope_file():
    """Out-of-scope file (.claude/rules/*.md) -> silent even when unread.

    This is the regression guard: the manifest never records reads of
    out-of-scope files, so warning there would be a guaranteed false
    positive. The gate must stay silent. (.claude/rules is the still-silent
    surface after g-115-2210 moved core/scripts into advisory scope.)"""
    with _throwaway_agent(manifest_paths=[]) as env:
        rc, stdout, stderr = _run_gate(_make_hook_json(str(OUT_OF_SCOPE_FILE)), env=env)
    assert rc == 0
    assert "ADVISORY" not in stderr, (
        f"Out-of-scope file must be silent (scope-aware). stderr={stderr!r}")


def test_advisory_when_core_scripts_unread():
    """core/scripts framework code IS advisory-tracked since  — an
    unread script edit MUST fire the advisory (the whole point of the goal)."""
    with _throwaway_agent(manifest_paths=[]) as env:
        rc, stdout, stderr = _run_gate(_make_hook_json(str(CORE_SCRIPTS_FILE)), env=env)
    assert rc == 0
    assert "ADVISORY" in stderr, (
        f"core/scripts edit must fire advisory (g-115-2210). stderr={stderr!r}")
    assert "has not been Read" in stderr


def test_silent_when_core_scripts_already_read():
    """core/scripts file present in manifest -> no advisory (symmetry check)."""
    with _throwaway_agent(manifest_paths=[CORE_SCRIPTS_FILE]) as env:
        rc, stdout, stderr = _run_gate(_make_hook_json(str(CORE_SCRIPTS_FILE)), env=env)
    assert rc == 0
    assert "ADVISORY" not in stderr, (
        f"core/scripts already read must be silent. stderr={stderr!r}")


def test_advisory_when_session_mismatch():
    """Manifest from a DIFFERENT session -> in-scope file treated as unread."""
    with _throwaway_agent(manifest_paths=[IN_SCOPE_FILE], session_id="stale-other-sid") as env:
        rc, stdout, stderr = _run_gate(
            _make_hook_json(str(IN_SCOPE_FILE), session_id=SID), env=env)
    assert rc == 0
    assert "ADVISORY" in stderr, (
        f"Stale-session manifest must trigger advisory. stderr={stderr!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Test runner (standalone, outside pytest)

if __name__ == "__main__":
    tests = [
        ("always_exits_zero_on_empty_stdin", test_always_exits_zero_on_empty_stdin),
        ("always_exits_zero_on_malformed_json", test_always_exits_zero_on_malformed_json),
        ("always_exits_zero_on_missing_file_path", test_always_exits_zero_on_missing_file_path),
        ("no_stdout_even_when_advisory_fires", test_no_stdout_even_when_advisory_fires),
        ("advisory_when_in_scope_file_unread", test_advisory_when_in_scope_file_unread),
        ("silent_when_in_scope_file_already_read", test_silent_when_in_scope_file_already_read),
        ("silent_when_out_of_scope_file", test_silent_when_out_of_scope_file),
        ("advisory_when_core_scripts_unread", test_advisory_when_core_scripts_unread),
        ("silent_when_core_scripts_already_read", test_silent_when_core_scripts_already_read),
        ("advisory_when_session_mismatch", test_advisory_when_session_mismatch),
    ]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as e:
            failures += 1
            print(f"  FAIL  {name}: {e}")
            traceback.print_exc()
    if failures:
        print(f"\n{failures}/{len(tests)} tests failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} tests passed")
