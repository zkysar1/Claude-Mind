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
    assert cmd[0] == "bash", f"cmd[0] should be 'bash', got {cmd[0]!r}"
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
    """inbox-alert-age-check.py wm-append.sh invocation also uses
    .as_posix(). Audited as part of g-115-786 rb-428 sibling sweep.

    Static source check — confirms the fix landed at the call site. A
    full functional test would require seeding wm.py state and is out
    of scope for this regression suite (the dependent-unblock test
    above proves the pattern works; this asserts the sibling adopted it).
    """
    src = (SCRIPTS_DIR / "inbox-alert-age-check.py").read_text(
        encoding="utf-8")
    # Strong signal: the .as_posix() form is present at the wm-append.sh
    # call site.
    assert '"wm-append.sh").as_posix()' in src, (
        "inbox-alert-age-check.py wm-append.sh invocation missing "
        ".as_posix() — Windows path-separator stripping would silently "
        "no-op proactive_escalation_log writes")
    # Negative assertion: no remaining bare str(SCRIPT_DIR / *.sh) at
    # this call site. (Other str(...) usage may be legitimate where the
    # path is not handed to bash; we narrow to the bash-invocation line.)
    assert 'str(SCRIPT_DIR / "wm-append.sh")' not in src, (
        "inbox-alert-age-check.py still uses str(SCRIPT_DIR / "
        '"wm-append.sh") — vulnerable to bash backslash stripping')


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
