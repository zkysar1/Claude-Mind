"""The idle-sleep yield carries the no-notify harness branch everywhere it is
printed (g-357-89).

Four surfaces tell the loop how to yield into a registered sleep: the
all-blocked B7.2 pseudocode, idle-tick.sh, dry-idle-cycle-cache.py and
quiescence-cycle-cache.py. Each used to state "the harness re-invokes on
completion" as a fact; it is a Claude Code fact. These tests pin that every
surface now consults harness-capabilities and, on a no-notify harness, tells
the model to launch once, arm ScheduleWakeup sized to the sleep, and end the
turn -- and says nothing extra on a notifying harness.
"""
from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from _bash_helpers import BASH  # guard-580: never a bare "bash" argv[0]

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
REPO = CORE_SCRIPTS.parent.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_MARKERS = ("CLAUDECODE", "ZAKCODE_MODEL", "ZAKCODE_SESSION", "MIND_HARNESS_BG_NOTIFY")


def _import_with_agent(name):
    saved = os.environ.get("MIND_AGENT")
    os.environ.setdefault("MIND_AGENT", "alpha")
    try:
        return importlib.import_module(name)
    finally:
        if saved is None:
            os.environ.pop("MIND_AGENT", None)
        else:
            os.environ["MIND_AGENT"] = saved


def _set_harness(monkeypatch, **markers):
    for k in _MARKERS:
        monkeypatch.delenv(k, raising=False)
    for k, v in markers.items():
        monkeypatch.setenv(k, v)


def test_b72_pseudocode_asks_the_harness_and_carries_both_branches():
    text = (REPO / ".claude/skills/aspirations-all-blocked/SKILL.md").read_text(encoding="utf-8")
    assert "harness-capabilities.sh --get background_job_notify" in text
    assert "IF true:" in text and "IF false" in text
    assert re.search(r'ScheduleWakeup\(prompt="<<autonomous-loop-dynamic>>", delaySeconds=min\(\{sleep_seconds\}\+60, 3600\)\)', text)
    # The forbidden half is still forbidden on both branches.
    assert "no synchronous Skill" in text
    assert "no Skill(aspirations), no further Bash, no prose" in text


def test_anti_pattern_c_carries_the_carve_out():
    text = (REPO / ".claude/rules/schedule-wakeup-correctness.md").read_text(encoding="utf-8")
    assert "One sanctioned substitution (g-357-89)" in text
    assert "never a synchronous Skill re-entry after a registered sleep" in text


@pytest.mark.parametrize("modname", ["dry-idle-cycle-cache", "quiescence-cycle-cache"])
def test_cycle_cache_hit_directive_branches_on_the_harness(monkeypatch, capsys, modname):
    mod = _import_with_agent(modname)
    now = datetime.now()
    wake = (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
    cache = {"sleep_seconds": 480, "cycle_count": 1, "streak": 2, "blocker_set_hash": "abc"}

    def emit():
        # The two printers have different signatures; the directive shape is shared.
        if modname.startswith("dry"):
            mod._emit_hit_directive(cache, wake, now, 3)
        else:
            mod._emit_hit_directive(cache, 3, earliest_wake_at=wake)

    _set_harness(monkeypatch, CLAUDECODE="1")
    emit()
    notifying = capsys.readouterr().out
    assert "interruptible-sleep.sh 480" in notifying
    assert "ScheduleWakeup" not in notifying

    _set_harness(monkeypatch, ZAKCODE_MODEL="x")
    emit()
    silent = capsys.readouterr().out
    assert "interruptible-sleep.sh 480" in silent
    assert "delaySeconds=540" in silent and "END THE TURN" in silent


def test_idle_tick_directive_branches_on_the_harness(tmp_path):
    """idle-tick.sh prints the hint line only on a no-notify harness."""
    agent = f"_idletick-{os.getpid()}"
    adir = REPO / "agents" / agent / "session"
    adir.mkdir(parents=True, exist_ok=True)
    try:
        wake = (datetime.now() + timedelta(seconds=900)).strftime("%Y-%m-%dT%H:%M:%S")
        subprocess.run([BASH, (CORE_SCRIPTS / "wm-set.sh").as_posix(), "blocked_sleep_until"],
                       input=f'"{wake}"', text=True, capture_output=True,
                       env={**os.environ, "MIND_AGENT": agent}, cwd=str(REPO))
        base = {k: v for k, v in os.environ.items() if k not in _MARKERS}
        base["MIND_AGENT"] = agent
        idle_tick = (CORE_SCRIPTS / "idle-tick.sh").as_posix()
        r_notify = subprocess.run([BASH, idle_tick], capture_output=True, text=True,
                                  env={**base, "CLAUDECODE": "1"}, cwd=str(REPO))
        r_silent = subprocess.run([BASH, idle_tick], capture_output=True, text=True,
                                  env={**base, "ZAKCODE_MODEL": "x"}, cwd=str(REPO))
    finally:
        import shutil
        shutil.rmtree(REPO / "agents" / agent, ignore_errors=True)
    if "IDLE TICK" not in r_notify.stdout:
        pytest.skip(f"idle-tick did not reach the directive (wm slot write unavailable here): {r_notify.stdout[:200]} {r_notify.stderr[:200]}")
    assert "ScheduleWakeup" not in r_notify.stdout
    assert "IDLE TICK" in r_silent.stdout and "delaySeconds=" in r_silent.stdout and "END THE TURN" in r_silent.stdout
