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
  - Emits NO stdout unless the advisory fires; when it does, stdout is the
    measured structured payload (guard-1680) and never raw check-file output
  - Graceful on missing/empty/malformed stdin
  - IN-SCOPE + unread   -> advisory fires (stderr + structured payload)
  - IN-SCOPE + read      -> silent (manifest has the path)
  - core/scripts + unread -> advisory fires (advisory scope, g-115-2210)
  - core/scripts + read   -> silent (symmetry)
  - OUT-OF-SCOPE (.claude/rules) -> silent (scope-aware; the regression this guards)
  - PRODUCTION SHAPE (MIND_AGENT unset, agent from session binding) -> fires;
    already-read -> silent; no resolvable agent -> fail-open silent
  - Constitutional anchor -> silent, and never receives an `allow` payload

g-115-3731 (2026-07-28) — two independent defects made this gate fully inert
from the day it landed (2026-05-30, ddff97349) until that date, and this suite
passed throughout. (1) Agent resolution: MIND_AGENT is injected only into
PreToolUse[Bash], so the wrapper's `[ -z "$AGENT_DIR" ] && exit 0` bail fired on
every real Edit; every test here set MIND_AGENT, exercising a branch production
never takes. (2) Output channel: stderr from a non-blocking PreToolUse hook
reaches the user's terminal, never the model — so even had it run, it said
nothing to the reader it exists to warn. The `test_no_stdout_even_when_advisory
_fires` assertion actively pinned defect 2 and was replaced, not deleted; see
that replacement's docstring.

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
def _throwaway_agent(manifest_paths=None, session_id=SID, inject_agent_env=True,
                     bind_session=False):
    """Create agents/<throwaway>/session under the real PROJECT_ROOT.

    If manifest_paths is given, write a context-reads.txt manifest with a
    matching session header and those (normalized) paths. Yields the env dict
    the gate should run with. Always cleans up the agent dir.

    inject_agent_env / bind_session select WHICH OF THE TWO SHAPES is exercised
    — and getting that choice wrong is how this gate stayed inert for 59 days
    (g-115-3731):

      inject_agent_env=True  (default) — HAND-TEST shape. MIND_AGENT is set in
        the child env. Convenient, and what every test in this file did before
        2026-07-28. It is NOT the shape PreToolUse[Edit] delivers.
      inject_agent_env=False + bind_session=True — PRODUCTION shape.
        MIND_AGENT is scrubbed, exactly as Claude Code invokes an Edit hook
        (the var is injected only into PreToolUse[Bash]), and a real
        sessions/<SID>/binding.yaml is written so the gate's binding fallback
        has something to resolve.

    Keep at least one PRODUCTION-shape test per behavior. A suite that only
    ever runs the hand-test shape verifies a code path production never takes,
    and reports green while the hook is fully dead — the guard-920 class
    (replicate the literal production shape, not the contract-ideal one),
    here on the ENV axis rather than the argv axis.
    """
    agent_dir = PROJECT_ROOT / "agents" / THROWAWAY_AGENT
    session_dir = agent_dir / "session"
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        # Minimal conf so _paths.sh resolves AGENT_DIR; empty world/meta is
        # fine because in-scope tests use .claude/skills (no WORLD_DIR needed).
        # newline="" disables CRLF translation on Windows — without it,
        # write_text emits "\r\n" and the shell consumers below read a
        # trailing \r into every value (guard-1688: the fixture feeds corrupt
        # bytes while the assertion is correct). Same hardening as
        # test_orphan_root_sweep_mode_d_integration.py's local-paths.conf.
        (agent_dir / "local-paths.conf").write_text(
            "WORLD_PATH=\nMETA_PATH=\n", encoding="utf-8", newline=""
        )
        if manifest_paths is not None:
            lines = [f"#session:{session_id}"] + [_norm(p) for p in manifest_paths]
            (session_dir / "context-reads.txt").write_text(
                "\n".join(lines) + "\n", encoding="utf-8", newline=""
            )
        if bind_session:
            bdir = agent_dir / "sessions" / session_id
            bdir.mkdir(parents=True, exist_ok=True)
            (bdir / "binding.yaml").write_text(
                f"session_id: {session_id}\n"
                f"agent: {THROWAWAY_AGENT}\n"
                "mode: autonomous\n"
                "started_at: '2026-01-01T00:00:00'\n"
                "started_by: test\n",
                encoding="utf-8",
                newline="",
            )
        env = dict(os.environ)
        if inject_agent_env:
            env["MIND_AGENT"] = THROWAWAY_AGENT
        else:
            env.pop("MIND_AGENT", None)
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


def test_advisory_reaches_the_model_via_structured_stdout():
    """When the advisory fires, stdout carries the measured delivery payload.

    THIS TEST REPLACES `test_no_stdout_even_when_advisory_fires`, which
    asserted `stdout == ""` on the advisory path. That assertion was not merely
    obsolete — it PINNED THE DEFECT. guard-1680 / g-115-3511 measured that a
    non-blocking PreToolUse hook cannot reach the model via stderr at all
    (stderr + exit 0 goes to the user's terminal; only a DENY feeds stderr back
    to Claude). So the old test enforced silence toward the one reader the gate
    exists to warn, and would have failed the fix.

    What remains true, and is still enforced by the three robustness tests
    above: check-file's raw stdout must NEVER leak through as the hook's own
    stdout. The gate captures it and emits a deliberate JSON payload instead.
    `permissionDecision: "allow"` is explicitly NOT a deny — the edit proceeds.

    Field set is copied verbatim from trailing-echo-exit-gate.py's measured
    payload; do not narrow it here from first principles (`allow` + reason
    ALONE was probed and did not deliver). Narrowing is tracked in g-115-3598.
    """
    with _throwaway_agent(manifest_paths=[]) as env:  # empty manifest -> in-scope warns
        rc, stdout, stderr = _run_gate(_make_hook_json(str(IN_SCOPE_FILE)), env=env)
    assert rc == 0
    payload = json.loads(stdout)  # must be exactly one JSON object, not raw path text
    hso = payload["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "allow", "the gate must NEVER deny"
    for field in ("permissionDecisionReason", "additionalContext"):
        assert "ADVISORY" in hso[field], f"{field} must carry the advisory text"
    assert "ADVISORY" in payload["systemMessage"]
    assert str(IN_SCOPE_FILE.name) in hso["additionalContext"], (
        "the payload must name the file, not just say something is unread")


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


# ── PRODUCTION SHAPE: MIND_AGENT UNSET (, the 59-day defect) ─────
#
# Every test above this block sets MIND_AGENT in the child env. That is the
# hand-test shape. PreToolUse[Edit|MultiEdit] never provides it — the var is
# injected only into PreToolUse[Bash] by bash-agent-inject.py — so from
# 2026-05-30 (ddff97349) to 2026-07-28 the wrapper bailed at
# `[ -z "$AGENT_DIR" ] && exit 0` on EVERY real invocation, before the check it
# exists to perform. Ten tests passed the whole time. These tests run the shape
# production actually delivers, so that regression cannot recur silently.

def test_production_shape_advisory_fires_without_agent_env():
    """No MIND_AGENT; agent resolved from the session binding -> advisory."""
    with _throwaway_agent(manifest_paths=[], inject_agent_env=False,
                          bind_session=True) as env:
        rc, stdout, stderr = _run_gate(_make_hook_json(str(IN_SCOPE_FILE)), env=env)
    assert rc == 0
    assert "MIND_AGENT" not in env, "test must not leak the hand-test shape"
    assert "ADVISORY" in stderr, (
        "PRODUCTION shape (MIND_AGENT unset) must fire the advisory — this is "
        f"the exact assertion that would have caught the 59-day inertia. stderr={stderr!r}")
    assert json.loads(stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_production_shape_silent_when_already_read():
    """No MIND_AGENT + file present in this session's manifest -> silent.

    The negative half of the production-shape pair. Without it, a wrapper that
    warned unconditionally (ignoring the manifest) would still pass the
    positive test above.
    """
    with _throwaway_agent(manifest_paths=[IN_SCOPE_FILE], inject_agent_env=False,
                          bind_session=True) as env:
        rc, stdout, stderr = _run_gate(_make_hook_json(str(IN_SCOPE_FILE)), env=env)
    assert rc == 0
    assert "ADVISORY" not in stderr, f"already-read must be silent. stderr={stderr!r}"
    assert stdout.strip() == "", f"no payload when nothing to warn about: {stdout!r}"


def test_fail_open_when_no_agent_resolvable():
    """Neither env nor binding resolves an agent -> rc 0, both channels silent.

    Fail-open is the gate's core posture: an advisory hook must never wedge an
    edit. bind_session=False leaves the SID unbound, so the binding fallback
    finds nothing.
    """
    with _throwaway_agent(manifest_paths=[], inject_agent_env=False,
                          bind_session=False) as env:
        rc, stdout, stderr = _run_gate(_make_hook_json(str(IN_SCOPE_FILE)), env=env)
    assert rc == 0, f"must fail open, got rc={rc}. stderr={stderr!r}"
    assert stdout.strip() == "", f"no payload without an agent: {stdout!r}"
    assert "ADVISORY" not in stderr


# ── Constitutional anchor must never receive an `allow` ────────────────────

def test_constitutional_anchor_never_gets_an_allow_payload():
    """The anchor files stay silent — no advisory, and crucially no `allow`.

    `permissionDecision: "allow"` is what makes this hook reachable by the
    model, but it also short-circuits the permission system. The anchor
    (.claude/settings.local.json + settings-structural-validator.{py,sh}, see
    CLAUDE.md) is hard-denied at every tier, and settings-structural-validator
    lives under core/scripts/ — inside advisory scope — so without an explicit
    exclusion it would reach the emitter. Advisory value there is nil anyway:
    the agent must not edit these files at all.

    This guards a property the FIX introduced. It did not exist before 2026-07-28
    because the gate emitted no payload at all.
    """
    anchors = [
        PROJECT_ROOT / ".claude" / "settings.local.json",
        SCRIPT_DIR / "settings-structural-validator.py",
        SCRIPT_DIR / "settings-structural-validator.sh",
    ]
    with _throwaway_agent(manifest_paths=[]) as env:
        for anchor in anchors:
            rc, stdout, stderr = _run_gate(_make_hook_json(str(anchor)), env=env)
            assert rc == 0, f"{anchor.name}: rc={rc}"
            assert stdout.strip() == "", (
                f"{anchor.name}: the anchor must NEVER receive an allow payload, "
                f"got: {stdout!r}")
            assert "ADVISORY" not in stderr, f"{anchor.name}: unexpected advisory"


# ─────────────────────────────────────────────────────────────────────────────
# Test runner (standalone, outside pytest)

if __name__ == "__main__":
    tests = [
        ("always_exits_zero_on_empty_stdin", test_always_exits_zero_on_empty_stdin),
        ("always_exits_zero_on_malformed_json", test_always_exits_zero_on_malformed_json),
        ("always_exits_zero_on_missing_file_path", test_always_exits_zero_on_missing_file_path),
        ("advisory_reaches_the_model_via_structured_stdout", test_advisory_reaches_the_model_via_structured_stdout),
        ("advisory_when_in_scope_file_unread", test_advisory_when_in_scope_file_unread),
        ("silent_when_in_scope_file_already_read", test_silent_when_in_scope_file_already_read),
        ("silent_when_out_of_scope_file", test_silent_when_out_of_scope_file),
        ("advisory_when_core_scripts_unread", test_advisory_when_core_scripts_unread),
        ("silent_when_core_scripts_already_read", test_silent_when_core_scripts_already_read),
        ("advisory_when_session_mismatch", test_advisory_when_session_mismatch),
        ("production_shape_advisory_fires_without_agent_env", test_production_shape_advisory_fires_without_agent_env),
        ("production_shape_silent_when_already_read", test_production_shape_silent_when_already_read),
        ("fail_open_when_no_agent_resolvable", test_fail_open_when_no_agent_resolvable),
        ("constitutional_anchor_never_gets_an_allow_payload", test_constitutional_anchor_never_gets_an_allow_payload),
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
