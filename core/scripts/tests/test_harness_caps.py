"""_harness_caps -- harness detection and the no-notify yield helpers ().

The all-blocked B7.2 yield, idle-tick.sh and both cycle-cache directive
printers now branch on ONE question -- can the hosting harness notify the loop
when a background job exits? -- answered here from env markers alone. These
tests pin the detection precedence (mirrors _runtime.sh::rt_judge_provenance),
the fail-safe default for an unknown harness, the override, the wake sizing
against ScheduleWakeup's [60, 3600] clamp, and the shared directive text.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

from _bash_helpers import BASH  # guard-580: never a bare "bash" argv[0]

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent.parent

_HARNESS_MARKERS = ("CLAUDECODE", "ZAKCODE_MODEL", "ZAKCODE_SESSION", "MIND_HARNESS_BG_NOTIFY")


def _mod():
    spec = importlib.util.spec_from_file_location("_harness_caps_t", SCRIPTS / "_harness_caps.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in _HARNESS_MARKERS}
    env.update(extra)
    return env


def test_detection_precedence_matches_runtime_sh():
    hc = _mod()
    assert hc.detect_harness({}) == "unknown"
    assert hc.detect_harness({"CLAUDECODE": "1"}) == "claude-code"
    assert hc.detect_harness({"ZAKCODE_MODEL": "x"}) == "zakcode"
    assert hc.detect_harness({"ZAKCODE_SESSION": "s"}) == "zakcode"
    # _runtime.sh checks CLAUDECODE first; both set -> claude-code.
    assert hc.detect_harness({"CLAUDECODE": "1", "ZAKCODE_MODEL": "x"}) == "claude-code"


def test_capability_table_and_fail_safe_unknown():
    hc = _mod()
    assert hc.background_job_notify({"CLAUDECODE": "1"}) is True
    assert hc.background_job_notify({"ZAKCODE_MODEL": "x"}) is False
    # Unknown harness -> False: a spare wake-up is harmless, a missing one is a dead loop.
    assert hc.background_job_notify({}) is False
    assert hc.capabilities({})["harness"] == "unknown"


def test_override_env_wins_and_garbage_is_ignored():
    hc = _mod()
    assert hc.background_job_notify({"ZAKCODE_MODEL": "x", "MIND_HARNESS_BG_NOTIFY": "1"}) is True
    assert hc.background_job_notify({"CLAUDECODE": "1", "MIND_HARNESS_BG_NOTIFY": "false"}) is False
    assert hc.background_job_notify({"CLAUDECODE": "1", "MIND_HARNESS_BG_NOTIFY": "maybe"}) is True


def test_wake_delay_is_sleep_plus_margin_clamped():
    hc = _mod()
    assert hc.wake_delay_seconds(1800) == 1860
    assert hc.wake_delay_seconds(30) == 90
    assert hc.wake_delay_seconds(7200) == 3600
    assert hc.wake_delay_seconds(3600) == 3600
    assert hc.wake_delay_seconds("garbage") == 60


def test_no_notify_hint_is_empty_on_notifying_harness_and_sized_otherwise():
    hc = _mod()
    assert hc.no_notify_hint(1800, {"CLAUDECODE": "1"}) == ""
    hint = hc.no_notify_hint(1800, {"ZAKCODE_MODEL": "x"})
    assert "delaySeconds=1860" in hint
    assert "<<autonomous-loop-dynamic>>" in hint
    assert "END THE TURN" in hint and "no Skill(aspirations)" in hint
    assert hint.endswith("\n")
    assert "delaySeconds=3600" in hc.no_notify_hint(7200, {})


def test_cli_get_json_and_hint():
    py = SCRIPTS / "_harness_caps.py"
    r = subprocess.run([sys.executable, str(py), "--get", "background_job_notify"],
                       env=_clean_env(ZAKCODE_MODEL="x"), capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "false", r
    r = subprocess.run([sys.executable, str(py), "--get", "background_job_notify"],
                       env=_clean_env(CLAUDECODE="1"), capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "true", r
    r = subprocess.run([sys.executable, str(py), "--json"],
                       env=_clean_env(), capture_output=True, text=True)
    assert '"harness": "unknown"' in r.stdout and '"background_job_notify": false' in r.stdout
    r = subprocess.run([sys.executable, str(py), "--hint", "1800"],
                       env=_clean_env(CLAUDECODE="1"), capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == ""
    r = subprocess.run([sys.executable, str(py), "--hint", "1800"],
                       env=_clean_env(), capture_output=True, text=True)
    assert "delaySeconds=1860" in r.stdout
    r = subprocess.run([sys.executable, str(py), "--get", "nope"],
                       env=_clean_env(), capture_output=True, text=True)
    assert r.returncode == 2 and "unknown capability" in r.stderr


def test_wrapper_script_is_the_python_invocation_safe_path():
    text = (SCRIPTS / "harness-capabilities.sh").read_text(encoding="utf-8")
    assert "_paths.sh" in text and "_harness_caps.py" in text
    r = subprocess.run([BASH, (SCRIPTS / "harness-capabilities.sh").as_posix(), "--get", "background_job_notify"],
                       env=_clean_env(ZAKCODE_SESSION="s", MIND_AGENT=os.environ.get("MIND_AGENT", "alpha")),
                       capture_output=True, text=True, cwd=str(REPO))
    assert r.returncode == 0 and r.stdout.strip() == "false", r
