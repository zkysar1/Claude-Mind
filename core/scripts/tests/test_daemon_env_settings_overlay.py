"""test_daemon_env_settings_overlay.py —  regression tests.

The committed .claude/settings.json "env" block reaches a Claude Code session's
own tool calls as it stood when the SESSION LAUNCHED. A detached daemon has no
session: it takes whatever shell spawned it. The pull that brings a new flag
also moves the daemon's code, and the staleness respawn then runs in that same
pre-pull shell, so a flag committed after the session launched never reached
the process that acts on it. Measured 2026-09-28 on the segmented board writer
(BOARD_SEGMENTED_CHANNELS): 21h after the flip reached main, four boxes'
daemons still appended every post to the legacy channel file.

_daemon_env_scrub.sh's daemon_overlay_settings_env closes it at the two spawn
sites (_runtime.sh rt_spawn, mind-api-start.sh): each committed settings key
the spawning shell lacks is exported before the daemon starts. Pinned here:

  function level (a fixture settings file)
    - an absent key is FILLED, and the spawn-log line names it
    - an inherited value WINS, including an empty one (a test's or an
      operator's pin survives)
    - a scrubbed key is NEVER filled (the scrub stays the daemon's own)
    - a pytest parent fills NOTHING (test daemons stay hermetic)
    - fail-open: missing / malformed / wrong-shaped settings fill nothing

  call-site level (the repo's REAL settings file, the REAL spawn code)
    - rt_spawn hands the daemon every non-scrubbed settings key it lacked
    - mind-api-start.sh does the same

Strategy mirrors test_runtime_spawn_env_scrub.py / test_daemon_start_env_scrub.py:
run the real bash, stub only the leaves, capture the child's environment. The
fake interpreter DELEGATES `-c` to the real one, because the overlay itself
runs `<launcher> -c` to parse the settings file; a fake that only dumped its
environment would make every call-site case pass vacuously or fail for the
wrong reason.

DAEMON_ENV_HELPER_UNDER_TEST overrides the helper the function-level cases
source. It exists for the mutation check (a copy whose overlay is a no-op must
turn the fill cases red), never for production.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent   # the repo root (core/scripts/tests -> ..)
RUNTIME_SH = CORE_SCRIPTS / "_runtime.sh"
START_SH = CORE_SCRIPTS / "mind-api-start.sh"
SETTINGS = PROJECT_ROOT / ".claude" / "settings.json"

sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH as GIT_BASH  # noqa: E402

# The scrubbed set, restated for the assertions only. The helper's
# daemon_env_key_is_scrubbed is the definition; test_scrubbed_set_matches_helper
# fails if this copy drifts from it.
SCRUBBED_EXACT = {
    "GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_PREFIX",
    "GIT_NAMESPACE", "MIND_ALLOW_TMP_OWNCLOUD_PUT", "STORAGE_BACKEND",
    "STORAGE_S3_BUCKET", "STORAGE_S3_ENDPOINT_URL", "STORAGE_DDB_SESSIONS_TABLE",
    "STORAGE_DDB_LOCK_TABLE", "ENVIRONMENT_ID", "MACHINE_ID", "MACHINE_MULTI",
    "OWNCLOUD_SYNC_INTERVAL", "OWNCLOUD_CACHE_TTL", "MIND_API_TOKEN",
    "MIND_API_BIND", "MIND_API_PORT",
}


def _to_bash_path(p) -> str:
    """C:\\a\\b -> /c/a/b for Git-Bash (msys) consumption."""
    s = str(p).replace("\\", "/")
    if len(s) > 1 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


def _helper_path() -> Path:
    return Path(os.environ.get("DAEMON_ENV_HELPER_UNDER_TEST")
                or CORE_SCRIPTS / "_daemon_env_scrub.sh")


def _is_scrubbed(key: str) -> bool:
    return key.startswith(("PYTEST_", "MOTO_")) or key in SCRUBBED_EXACT


def _base_env(drop=()) -> dict:
    """This process's env minus test markers and the named keys, so each case
    states exactly what the spawning shell carries."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PYTEST_", "MOTO_"))}
    for k in drop:
        env.pop(k, None)
    return env


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def _fake_interpreter(bindir: Path, capture: Path, names=("fake-python3",)) -> Path:
    """A stand-in for the daemon launcher: `-c` runs the REAL interpreter (the
    overlay's settings parse), anything else dumps the environment it was handed
    (the daemon spawn) and exits."""
    bindir.mkdir(parents=True, exist_ok=True)
    real = _to_bash_path(sys.executable)
    body = (
        "#!/bin/bash\n"
        # A `py` launcher is invoked as `py -3 ...`.
        'if [ "${1:-}" = "-3" ]; then shift; fi\n'
        f'if [ "${{1:-}}" = "-c" ]; then exec "{real}" "$@"; fi\n'
        f'env > "{_to_bash_path(capture)}"\n'
    )
    first = None
    for name in names:
        fake = bindir / name
        _write(fake, body)
        fake.chmod(0o755)
        first = first or fake
    return first


def _overlay(settings_text, env: dict, launcher=None) -> tuple:
    """Source the helper under a fixture PROJECT_ROOT, run the overlay in a
    subshell, and return (rc, stdout-line, {key: value} of the post-overlay env).
    launcher=None means the real interpreter; "" passes an empty launcher."""
    tmp = Path(tempfile.mkdtemp(prefix="daemon-env-overlay-"))
    if settings_text is not None:
        (tmp / ".claude").mkdir()
        _write(tmp / ".claude" / "settings.json", settings_text)
    if launcher is None:
        launcher = _to_bash_path(sys.executable)
    dump = tmp / "after.json"
    line_file = tmp / "line.txt"
    script = tmp / "harness.sh"
    # The overlay is called with a REDIRECT, never inside $(...): command
    # substitution is a subshell, so its exports would vanish before the dump.
    _write(script, (
        "set -uo pipefail\n"
        f'export PROJECT_ROOT="{_to_bash_path(tmp)}"\n'
        f'source "{_to_bash_path(_helper_path())}"\n'
        "(\n"
        f'  daemon_overlay_settings_env "{launcher}" > "{_to_bash_path(line_file)}"; rc=$?\n'
        '  printf "%s\\n" "$rc"\n'
        f'  cat "{_to_bash_path(line_file)}"\n'
        f'  "{_to_bash_path(sys.executable)}" -c "import json,os,sys;'
        f'json.dump(dict(os.environ),open(sys.argv[1],\'w\'))" "{_to_bash_path(dump)}"\n'
        ")\n"
    ))
    r = subprocess.run([GIT_BASH, _to_bash_path(script)], capture_output=True,
                       text=True, timeout=60, cwd=str(PROJECT_ROOT), env=env)
    assert r.returncode == 0, f"harness failed:\n{r.stdout}\n{r.stderr}"
    rc, _, line = r.stdout.partition("\n")
    after = json.loads(dump.read_text(encoding="utf-8"))
    return int(rc), line.strip(), after


FIXTURE = json.dumps({"env": {
    "FEATURE_ALPHA": "on",
    "BOARD_SEGMENTED_CHANNELS": "coordination",
}})


def test_fills_absent_keys_and_names_them():
    env = _base_env(drop=("FEATURE_ALPHA", "BOARD_SEGMENTED_CHANNELS"))
    rc, line, after = _overlay(FIXTURE, env)
    assert rc == 0
    assert after.get("FEATURE_ALPHA") == "on"
    assert after.get("BOARD_SEGMENTED_CHANNELS") == "coordination"
    assert "FEATURE_ALPHA" in line and "BOARD_SEGMENTED_CHANNELS" in line, (
        f"the spawn-log line must name what it filled: {line!r}")


def test_inherited_value_wins_even_when_empty():
    env = _base_env()
    env["FEATURE_ALPHA"] = "mine"
    env["BOARD_SEGMENTED_CHANNELS"] = ""
    rc, line, after = _overlay(FIXTURE, env)
    assert rc == 0
    assert after["FEATURE_ALPHA"] == "mine", "an inherited value was overridden"
    assert after["BOARD_SEGMENTED_CHANNELS"] == "", (
        "an inherited EMPTY value was overridden: a test or operator pin of "
        "'no segmented channels' must survive")
    assert line == "", f"nothing was absent, so nothing may be named: {line!r}"


def test_never_fills_a_scrubbed_key():
    declared = ["STORAGE_BACKEND", "MACHINE_ID", "MIND_API_TOKEN", "GIT_DIR",
                "PYTEST_ADDOPTS", "MOTO_ANY"]
    settings = json.dumps({"env": {**{k: "from-settings" for k in declared},
                                   "FEATURE_ALPHA": "on"}})
    env = _base_env(drop=declared + ["FEATURE_ALPHA"])
    rc, _, after = _overlay(settings, env)
    assert rc == 0
    assert after.get("FEATURE_ALPHA") == "on", (
        "positive control: the benign key beside them must still be filled")
    leaked = [k for k in declared if after.get(k) == "from-settings"]
    assert not leaked, f"the overlay filled scrubbed keys: {leaked}"


def test_pytest_parent_fills_nothing():
    env = _base_env(drop=("FEATURE_ALPHA", "BOARD_SEGMENTED_CHANNELS"))
    env["PYTEST_CURRENT_TEST"] = "core/scripts/tests/test_x.py::test_y"
    rc, line, after = _overlay(FIXTURE, env)
    assert rc == 0
    assert "FEATURE_ALPHA" not in after and "BOARD_SEGMENTED_CHANNELS" not in after
    assert line == ""


@pytest.mark.parametrize("settings_text", [
    None,                                         # no settings file at all
    "{not json",                                  # malformed
    json.dumps(["env"]),                          # top level not an object
    json.dumps({"env": ["FEATURE_ALPHA=on"]}),     # env not an object
    json.dumps({"env": {"FEATURE_ALPHA": 1}}),     # non-string value
    json.dumps({"hooks": {}}),                    # no env block
], ids=["missing", "malformed", "top-list", "env-list", "non-string", "no-env"])
def test_fail_open_fills_nothing(settings_text):
    env = _base_env(drop=("FEATURE_ALPHA",))
    rc, line, after = _overlay(settings_text, env)
    assert rc == 0
    assert "FEATURE_ALPHA" not in after
    assert line == ""


def test_invalid_key_names_skipped_valid_neighbour_filled():
    settings = json.dumps({"env": {"BAD-KEY": "x", "1LEADING_DIGIT": "x",
                                   "HAS SPACE": "x", "FEATURE_ALPHA": "on"}})
    env = _base_env(drop=("FEATURE_ALPHA",))
    rc, line, after = _overlay(settings, env)
    assert rc == 0
    assert after.get("FEATURE_ALPHA") == "on"
    assert not {"BAD-KEY", "1LEADING_DIGIT", "HAS SPACE"} & set(after)
    assert line.endswith("FEATURE_ALPHA"), line


def test_empty_launcher_fills_nothing():
    env = _base_env(drop=("FEATURE_ALPHA",))
    rc, line, after = _overlay(FIXTURE, env, launcher="")
    assert rc == 0 and "FEATURE_ALPHA" not in after and line == ""


def test_scrubbed_set_matches_helper():
    """The assertion copy above must agree with the helper's predicate, key by
    key, in both directions — otherwise a drift silently weakens the tests."""
    probe = sorted(SCRUBBED_EXACT) + ["PYTEST_X", "MOTO_Y", "FEATURE_ALPHA",
                                      "BOARD_SEGMENTED_CHANNELS", "WORLD_PATH",
                                      "RUNTIME_DIR", "TZ"]
    script = (
        f'source "{_to_bash_path(_helper_path())}"\n'
        'for k in "$@"; do if daemon_env_key_is_scrubbed "$k"; then echo "$k"; fi; done\n'
    )
    r = subprocess.run([GIT_BASH, "-c", script, "probe", *probe],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    helper_says = set(r.stdout.split())
    assert helper_says == {k for k in probe if _is_scrubbed(k)}


# --- call-site level: the REAL spawn code against the REAL settings file ------

def _settings_keys_expected() -> dict:
    env = json.loads(SETTINGS.read_text(encoding="utf-8")).get("env") or {}
    expected = {k: v for k, v in env.items()
                if isinstance(v, str) and not _is_scrubbed(k)}
    assert expected, "anti-vacuity: the repo settings env block is empty"
    return expected


def _env_dict(captured: str) -> dict:
    out = {}
    for line in captured.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def test_rt_spawn_hands_the_daemon_absent_settings_keys():
    expected = _settings_keys_expected()
    tmp = Path(tempfile.mkdtemp(prefix="rt-spawn-overlay-"))
    capture = tmp / "captured.env"
    fake = _fake_interpreter(tmp / "bin", capture)
    script = tmp / "harness.sh"
    _write(script, (
        "set -uo pipefail\n"
        f'export PROJECT_ROOT="{_to_bash_path(PROJECT_ROOT)}"\n'
        f'export RT_DIR="{_to_bash_path(tmp)}/rt"\n'
        f'source "{_to_bash_path(RUNTIME_SH)}"\n'
        "rt_daemon_kill() { :; }\n"
        f'rt_python_launcher() {{ echo "{_to_bash_path(fake)}"; }}\n'
        "rt_spawn\n"
        "for _ in $(seq 1 50); do\n"
        f'  [ -s "{_to_bash_path(capture)}" ] && break\n'
        "  sleep 0.1\n"
        "done\n"
        f'cat "{_to_bash_path(capture)}"\n'
    ))
    r = subprocess.run([GIT_BASH, _to_bash_path(script)], capture_output=True,
                       text=True, timeout=60, cwd=str(PROJECT_ROOT),
                       env=_base_env(drop=expected))
    assert r.returncode == 0 and r.stdout.strip(), (
        f"fake launcher never ran:\n{r.stdout}\n{r.stderr}")
    child = _env_dict(r.stdout)
    wrong = {k: child.get(k) for k, v in expected.items() if child.get(k) != v}
    assert not wrong, f"rt_spawn did not hand the daemon these settings keys: {wrong}"


def test_launcher_hands_the_daemon_absent_settings_keys():
    expected = _settings_keys_expected()
    tmp = Path(tempfile.mkdtemp(prefix="daemon-start-overlay-"))
    rt_dir = tmp / "rt"
    rt_dir.mkdir(parents=True)
    capture = tmp / "captured.env"
    _fake_interpreter(tmp / "bin", capture, names=("python3", "py"))
    env = _base_env(drop=list(expected) + ["STORAGE_BACKEND",
                                           "MIND_ALLOW_SHARED_DAEMON_FROM_TEST"])
    env["PATH"] = f"{tmp / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    env["RUNTIME_DIR"] = str(rt_dir)
    subprocess.run([GIT_BASH, _to_bash_path(START_SH)], capture_output=True,
                   text=True, timeout=90, cwd=str(PROJECT_ROOT), env=env)
    assert capture.exists() and capture.read_text(encoding="utf-8").strip(), (
        "fake interpreter never ran — the launcher did not reach its spawn site, "
        "so this case proves nothing (anti-vacuity floor, rb-245)")
    child = _env_dict(capture.read_text(encoding="utf-8"))
    wrong = {k: child.get(k) for k, v in expected.items() if child.get(k) != v}
    assert not wrong, (
        f"mind-api-start.sh did not hand the daemon these settings keys: {wrong}")
