""": context-reads advisory-scope split invariants.

The read-before-edit ADVISORY + the RECORDER cover core/scripts framework code
(is_in_scope_advisory), but the BLOCKING re-read dedup gate (cmd_gate / exit 2)
keeps the NARROW is_in_scope. This split is load-bearing: unifying the two
predicates would start refusing whole-file re-reads of a just-edited script,
colliding with verify-before-assuming.md's mandated "re-verify after linter/user
notification." These tests pin the split at the engine level so that regression
can't land silently.

Invariants:
  A. Dedup gate does NOT block a core/scripts whole-file re-read, even when the
     path is already in the manifest (advisory scope != dedup scope).
  B. Dedup gate STILL blocks a narrow-scope (.claude/skills) re-read — the
     pre-2210 behavior is untouched (no regression).
  C. The recorder DOES record a core/scripts read (so the advisory has signal).
  D. The recorder still records a narrow-scope read (sanity).

Run: py -3 -m pytest core/scripts/tests/test_context_reads_advisory_scope.py -v
  or: py -3 core/scripts/tests/test_context_reads_advisory_scope.py
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
PROJECT_ROOT = SCRIPT_DIR.parent.parent  # repo root

if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
from _bash_helpers import BASH  # noqa: E402

# A core/scripts file (advisory scope, NOT dedup scope) and a narrow-scope file
# (.claude/skills — both advisory AND dedup scope).
CORE_SCRIPTS_FILE = SCRIPT_DIR / "iteration-close.sh"
NARROW_SCOPE_FILE = PROJECT_ROOT / ".claude" / "skills" / "respond" / "SKILL.md"

THROWAWAY_AGENT = "_advisory_scope_test_throwaway_agent_"
SID = "advisory-scope-sid-001"


def _norm(p):
    return str(Path(p).resolve()).replace("\\", "/")


@contextmanager
def _throwaway_agent(manifest_paths=None, session_id=SID):
    """Create agents/<throwaway>/session under the real PROJECT_ROOT, optionally
    seeding a context-reads.txt manifest. Yields the env dict. Always cleans up."""
    agent_dir = PROJECT_ROOT / "agents" / THROWAWAY_AGENT
    session_dir = agent_dir / "session"
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        # newline="" disables CRLF translation on Windows (guard-1688) — both
        # files are read by the shell scripts under test.
        (agent_dir / "local-paths.conf").write_text(
            "WORLD_PATH=\nMETA_PATH=\n", encoding="utf-8", newline=""
        )
        if manifest_paths is not None:
            lines = [f"#session:{session_id}"] + [_norm(p) for p in manifest_paths]
            (session_dir / "context-reads.txt").write_text(
                "\n".join(lines) + "\n", encoding="utf-8", newline=""
            )
        env = dict(os.environ)
        env["MIND_AGENT"] = THROWAWAY_AGENT
        yield env
    finally:
        if agent_dir.name == THROWAWAY_AGENT and agent_dir.is_dir():
            shutil.rmtree(agent_dir, ignore_errors=True)


def _hook_json(file_path, session_id=SID):
    return json.dumps({
        "tool_name": "Read",
        "tool_input": {"file_path": file_path},
        "session_id": session_id,
    })


def _run(script_rel, stdin_text, env, timeout=30):
    r = subprocess.run(
        [BASH, script_rel],
        input=stdin_text, capture_output=True, text=True,
        env=env, timeout=timeout, cwd=str(PROJECT_ROOT),
    )
    return r.returncode, r.stdout, r.stderr


def _manifest_lines(env):
    mf = PROJECT_ROOT / "agents" / THROWAWAY_AGENT / "session" / "context-reads.txt"
    if not mf.exists():
        return []
    return [l.strip() for l in mf.read_text().splitlines() if l.strip()]


# ── A. Dedup gate does NOT block a recorded core/scripts re-read ─────────────

def test_dedup_gate_does_not_block_core_scripts_reread():
    """core/scripts already in manifest -> gate STILL allows (exit 0). This is
    the whole reason for the is_in_scope vs is_in_scope_advisory split."""
    with _throwaway_agent(manifest_paths=[CORE_SCRIPTS_FILE]) as env:
        rc, _out, err = _run("core/scripts/context-reads-gate.sh",
                             _hook_json(str(CORE_SCRIPTS_FILE)), env)
    assert rc == 0, (
        f"core/scripts re-read must NOT be blocked (advisory scope != dedup "
        f"scope). Got exit {rc}. stderr={err!r}")


# ── B. Dedup gate STILL blocks a narrow-scope re-read (no regression) ────────

def test_dedup_gate_still_blocks_narrow_scope_reread():
    """A narrow-scope file already in manifest -> gate blocks (exit 2). The
    pre-2210 dedup behavior is untouched."""
    with _throwaway_agent(manifest_paths=[NARROW_SCOPE_FILE]) as env:
        rc, _out, _err = _run("core/scripts/context-reads-gate.sh",
                              _hook_json(str(NARROW_SCOPE_FILE)), env)
    assert rc == 2, (
        f"narrow-scope re-read must still be dedup-blocked (exit 2), got {rc}")


def test_dedup_gate_allows_first_narrow_read():
    """Sanity: first read of a narrow-scope file (not yet in manifest) -> allow."""
    with _throwaway_agent(manifest_paths=[]) as env:
        rc, _out, _err = _run("core/scripts/context-reads-gate.sh",
                              _hook_json(str(NARROW_SCOPE_FILE)), env)
    assert rc == 0, f"first read must be allowed (exit 0), got {rc}"


# ── C/D. Recorder records both core/scripts (advisory) and narrow scope ──────

def test_recorder_records_core_scripts():
    """core/scripts read IS recorded (advisory scope) -> gives the advisory its
    signal. Without this the pre-edit advisory could never fire for scripts."""
    with _throwaway_agent(manifest_paths=[]) as env:
        rc, _out, _err = _run("core/scripts/context-reads-record.sh",
                              _hook_json(str(CORE_SCRIPTS_FILE)), env)
        assert rc == 0, f"record hook must exit 0, got {rc}"
        lines = _manifest_lines(env)
    assert _norm(CORE_SCRIPTS_FILE) in lines, (
        f"core/scripts read must be recorded. manifest={lines!r}")


def test_recorder_records_narrow_scope():
    """Sanity: narrow-scope read still recorded (unchanged behavior)."""
    with _throwaway_agent(manifest_paths=[]) as env:
        rc, _out, _err = _run("core/scripts/context-reads-record.sh",
                              _hook_json(str(NARROW_SCOPE_FILE)), env)
        assert rc == 0, f"record hook must exit 0, got {rc}"
        lines = _manifest_lines(env)
    assert _norm(NARROW_SCOPE_FILE) in lines, (
        f"narrow-scope read must be recorded. manifest={lines!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Standalone runner (outside pytest)

if __name__ == "__main__":
    tests = [
        ("dedup_gate_does_not_block_core_scripts_reread",
         test_dedup_gate_does_not_block_core_scripts_reread),
        ("dedup_gate_still_blocks_narrow_scope_reread",
         test_dedup_gate_still_blocks_narrow_scope_reread),
        ("dedup_gate_allows_first_narrow_read",
         test_dedup_gate_allows_first_narrow_read),
        ("recorder_records_core_scripts", test_recorder_records_core_scripts),
        ("recorder_records_narrow_scope", test_recorder_records_narrow_scope),
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
