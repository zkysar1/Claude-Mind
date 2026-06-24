"""Regression tests for  — Windows path-separator stripping in
the rb-428 subprocess-shells-to-.sh family.

Canonical incident: dependent-unblock.py invoked aspirations-update-goal.sh
via subprocess.run(["bash", str(SCRIPT_DIR / "aspirations-update-goal.sh"),
...]). On Windows, str(WindowsPath) yields backslash separators
(C:\\<WORKSPACE>\\...) — bash interprets each `\\X` as an escape sequence
and consumes the backslash, yielding C:<WORKSPACE>... (nonexistent path).
Every dependent-unblock call silently no-opped; downstream Predecessor
Output injection was lost.

Fix: use Path.as_posix() instead of str(Path) when handing a path to
subprocess that will be interpreted by a shell-like consumer (bash).
.as_posix() produces forward-slash form (C:/<WORKSPACE>/...) which bash
accepts verbatim.

These tests pin the contract at the dependent-unblock subprocess boundary
AND at the two audited rb-428 siblings (inbox-alert-age-check,
blocker-recheck). New invocations added to this family MUST adopt the
same .as_posix() pattern, OR widen the assertion below to cover the new
call site.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE.parent  # core/scripts

# Skip the suite if MIND_AGENT is not set — the modules under test import
# _paths which requires the env binding. Mirrors the policy in other
# core/scripts/tests files.
import os

if not os.environ.get("MIND_AGENT"):
    os.environ["MIND_AGENT"] = "alpha"  # safe default for test runs


def _load_module_from_path(module_name: str, file_path: Path):
    """Load a module from a path containing hyphens (e.g. dependent-unblock.py).

    Hyphens prevent `import dependent-unblock` so we use spec-based loading.
    Ensures core/scripts is on sys.path so the loaded module can resolve
    its own sibling imports (_paths, aspirations, etc.).
    """
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(
            f"could not load {module_name} from {file_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def dependent_unblock_module():
    """Load dependent-unblock.py for direct _update() probing."""
    return _load_module_from_path(
        "dependent_unblock_under_test",
        SCRIPTS_DIR / "dependent-unblock.py")


def test_update_uses_posix_path_no_backslash(dependent_unblock_module):
    """_update() builds subprocess cmd with the wrapper path in
    forward-slash form so bash does not strip backslashes on Windows.

    This is the canonical regression for g-115-786. Before the fix:
      cmd[1] == r"C:\\<WORKSPACE>\\...\\aspirations-update-goal.sh"
    After the fix:
      cmd[1] == "C:/<WORKSPACE>/.../aspirations-update-goal.sh"
    """
    captured_cmd = []

    def _fake_run(cmd, *args, **kwargs):
        captured_cmd.append(list(cmd))
        result = types.SimpleNamespace()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    with mock.patch.object(
            dependent_unblock_module.subprocess, "run", _fake_run):
        ok, err = dependent_unblock_module._update(
            "world", "g-test-001", "blocked_by", "[]", dry_run=False)

    assert ok, f"_update reported failure: {err!r}"
    assert len(captured_cmd) == 1, (
        f"expected exactly one subprocess.run call, "
        f"got {len(captured_cmd)}")
    cmd = captured_cmd[0]
    # rb-1472 (commit 5089de1a): the bash resolver now returns the full Git
    # login-launcher path (e.g. C:\<WORKSPACE>\...\bash.exe), not the bare
    # string "bash". Assert the interpreter's basename instead of strict
    # equality. .replace("\\", "/") keeps basename robust on POSIX Python,
    # where os.path.basename does not split backslash separators.
    bash_base = os.path.basename(cmd[0].replace("\\", "/"))
    assert bash_base in ("bash", "bash.exe"), (
        f"cmd[0] should resolve to a bash interpreter, got {cmd[0]!r}")
    wrapper_path = cmd[1]
    # Core assertion: no backslash separators in the wrapper path
    # argument. On Windows, backslashes would be stripped by bash escape
    # interpretation. On POSIX, both forms work but forward-slash is the
    # natural shape.
    assert "\\" not in wrapper_path, (
        f"wrapper path argument contains backslash separator "
        f"(would be stripped by bash on Windows): {wrapper_path!r}")
    # And the path must end with the expected wrapper basename — this
    # catches any future refactor that swaps to a different helper.
    assert wrapper_path.endswith("/aspirations-update-goal.sh"), (
        f"wrapper path should end with /aspirations-update-goal.sh, "
        f"got {wrapper_path!r}")


def test_update_dry_run_skips_subprocess(dependent_unblock_module):
    """Dry-run mode short-circuits — never invokes subprocess.

    Belt-and-suspenders: confirms the .as_posix() fix did not accidentally
    move the subprocess.run call outside the dry_run guard.
    """
    sentinel_called = []

    def _fake_run(cmd, *args, **kwargs):
        sentinel_called.append(True)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    with mock.patch.object(
            dependent_unblock_module.subprocess, "run", _fake_run):
        ok, err = dependent_unblock_module._update(
            "world", "g-test-002", "description", "x", dry_run=True)

    assert ok is True
    assert err == ""
    assert sentinel_called == [], (
        "dry-run must not invoke subprocess.run")


def test_inbox_alert_uses_posix_path():
    """inbox-alert-age-check.py invokes its .sh subprocesses (board-read.sh +
    board-post.sh) through bash_cmd(), which prepends the resolved BASH and
    emits the script path via Path.as_posix() (guard-580 + guard-581). Audited
    as part of g-115-786 rb-428 sibling sweep; migrated to the shared bash_cmd()
    helper in g-115-900 -- the inline .as_posix() guarantee moved into
    core/scripts/_runtime_bash.py:bash_cmd, pinned by
    test_runtime_bash_resolution.py::test_bash_cmd_posix_normalizes_path.

    g-115-1533 replaced the per-agent wm-append.sh cooldown with the shared,
    durable coordination-board scan (board-read.sh) + breadcrumb (board-post.sh),
    so the Windows-safe bash_cmd() guarantee now applies to THOSE call sites.

    Static source check -- confirms the safe pattern landed at the call sites.
    A full functional test would require a live board and is out of scope for
    this regression suite (the dependent-unblock test above proves the
    path-shape contract; this asserts the sibling adopted the helper).
    """
    src = (SCRIPTS_DIR / "inbox-alert-age-check.py").read_text(
        encoding="utf-8")
    # Strong signal: the board .sh invocations go through bash_cmd(), which is
    # Windows-safe by construction (resolved BASH + .as_posix()).
    assert 'bash_cmd(SCRIPT_DIR / "board-read.sh"' in src, (
        "inbox-alert-age-check.py board-read.sh invocation no longer uses "
        "bash_cmd() -- Windows path-separator stripping (or the System32 "
        "WSL stub) would silently no-op the shared cooldown board scan")
    assert 'bash_cmd(SCRIPT_DIR / "board-post.sh"' in src, (
        "inbox-alert-age-check.py board-post.sh invocation no longer uses "
        "bash_cmd() -- the cooldown breadcrumb post would silently no-op on "
        "Windows")
    # Negative assertions: the legacy vulnerable forms must NOT reappear at
    # these call sites -- bare str(...) would let bash strip backslashes.
    assert 'str(SCRIPT_DIR / "board-read.sh")' not in src, (
        "inbox-alert-age-check.py still uses str(SCRIPT_DIR / "
        '"board-read.sh") -- vulnerable to bash backslash stripping')
    assert 'str(SCRIPT_DIR / "board-post.sh")' not in src, (
        "inbox-alert-age-check.py still uses str(SCRIPT_DIR / "
        '"board-post.sh") -- vulnerable to bash backslash stripping')


def test_blocker_recheck_uses_posix_path():
    """blocker-recheck.py session-signal-set.sh invocation uses
    .as_posix() AND wraps the call through bash. Audited as part of
    g-115-786 rb-428 sibling sweep.

    Pre-fix it used [str(SCRIPT_DIR / "session-signal-set.sh"), arg]
    (no bash prefix) — Windows can't execute .sh directly via shebang,
    so the call always failed silently. Post-fix it uses
    ["bash", (...).as_posix(), arg].
    """
    src = (SCRIPTS_DIR / "blocker-recheck.py").read_text(encoding="utf-8")
    assert '"session-signal-set.sh").as_posix()' in src, (
        "blocker-recheck.py session-signal-set.sh invocation missing "
        ".as_posix() — Windows path-separator stripping risk")
    assert (
        'str(SCRIPT_DIR / "session-signal-set.sh"), "blocker-cleared"'
        not in src), (
        "blocker-recheck.py still uses bare str(SCRIPT_DIR / .sh) "
        "without bash prefix — Windows can't execute .sh directly")
