"""One harness contract for the loop's yield block (2026-09-18; Zak-Code ADR-0191).

The block that tells the loop how to yield on a registered sleep used to be keyed on the
hosting harness (`_harness_caps.py`, g-357-89 / g-373-10): a vessel that could not notify
on a background job's exit got a trailing-& launch plus a sized ScheduleWakeup as the
re-entry, and a chunked foreground sleep under its tool cap. The vessel now reports the
exit exactly as Claude Code does, so the table and both branches are gone and the block
has ONE owner and ONE text. These tests pin BEHAVIOUR (guard-6333): the same bytes under
every harness marker, the override env of the deleted table changing nothing, the shell
printer's door, and the retired branches' fingerprints absent — and they clear every
marker themselves rather than running under a fixture that sets one.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "core" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from _runtime_bash import BASH  # noqa: E402  (guard-580: never a bare "bash" argv)

MARKERS = ("CLAUDECODE", "ZAKCODE_SESSION", "ZAKCODE_MODEL", "MIND_HARNESS_BG_NOTIFY")
ENVS = {
    "claude-code": {"CLAUDECODE": "1"},
    "zakcode": {"ZAKCODE_SESSION": "sid-1", "ZAKCODE_MODEL": "gemini-3.5-flash"},
    "unknown": {},
}
EXPECTED = (
    "Emit exactly ONE tool call:\n"
    '  Bash("MIND_AGENT=alpha QUIESCENCE_SLEEP=1 bash core/scripts/interruptible-sleep.sh 1800", '
    "run_in_background=true)\n"
    "When the harness notifies you of its exit, call Skill('aspirations') with args='loop'.\n"
)
RETIRED_FINGERPRINTS = ("CANNOT NOTIFY", "CAPS A FOREGROUND SLEEP", "ScheduleWakeup", " &")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _clear_markers(monkeypatch) -> None:
    for name in MARKERS:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("harness", sorted(ENVS))
def test_the_yield_block_is_one_text_on_every_harness(monkeypatch, harness):
    _clear_markers(monkeypatch)
    for key, value in ENVS[harness].items():
        monkeypatch.setenv(key, value)
    text = _load("_sleep_directive").sleep_directive(1800, "alpha", "QUIESCENCE_SLEEP=1")
    assert text == EXPECTED
    for phrase in RETIRED_FINGERPRINTS:
        assert phrase not in text, phrase


def test_the_override_env_of_the_deleted_table_changes_nothing(monkeypatch):
    _clear_markers(monkeypatch)
    monkeypatch.setenv("ZAKCODE_SESSION", "sid-1")
    monkeypatch.setenv("MIND_HARNESS_BG_NOTIFY", "0")
    mod = _load("_sleep_directive")
    assert mod.sleep_directive(1800, "alpha", "QUIESCENCE_SLEEP=1") == EXPECTED
    assert mod.sleep_directive("garbage", "alpha", "DRY_SLEEP=1").count("interruptible-sleep.sh 0") == 1


def test_the_shell_printer_gets_the_same_block():
    env = {k: v for k, v in os.environ.items() if k not in MARKERS}
    env["ZAKCODE_SESSION"] = "sid-1"  # the vessel marker, where the old branch fired
    env["STORAGE_BACKEND"] = "local"
    script = (SCRIPTS / "sleep-directive.sh").as_posix()
    out = subprocess.run(
        [BASH, script, "1800", "alpha", "QUIESCENCE_SLEEP=1"],
        capture_output=True, text=True, env=env, cwd=ROOT, check=False,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout == EXPECTED
    bad = subprocess.run(
        [BASH, script, "1800"], capture_output=True, text=True, env=env, cwd=ROOT, check=False
    )
    assert bad.returncode == 2 and "usage" in bad.stderr


def test_the_dry_spin_guard_printer_emits_the_one_block(monkeypatch, capsys):
    _clear_markers(monkeypatch)
    monkeypatch.setenv("ZAKCODE_SESSION", "sid-1")
    monkeypatch.setenv("MIND_AGENT", "alpha")
    guard = _load("dry-spin-guard")
    guard.emit_directive(1800, {"at": "2026-09-18T00:00:00", "sid": "sid-1"}, 3)
    out = capsys.readouterr().out
    assert EXPECTED.replace("QUIESCENCE_SLEEP=1", "DRY_SLEEP=1") in out
    for phrase in RETIRED_FINGERPRINTS:
        assert phrase not in out, phrase


def test_the_capability_table_is_gone():
    assert not (SCRIPTS / "_harness_caps.py").exists()
    assert not (SCRIPTS / "harness-capabilities.sh").exists()
    for name in ("dry-idle-cycle-cache", "dry-spin-guard", "quiescence-cycle-cache"):
        assert "_harness_caps" not in (SCRIPTS / f"{name}.py").read_text(encoding="utf-8"), name
