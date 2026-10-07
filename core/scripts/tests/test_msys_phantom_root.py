""" outcome 2: an MSYS-form root must not resolve to a phantom C:/c/ tree.

Two leaks of one shape, both measured on a Windows box (DESKTOP-O91DLK2):

1. A daemon recycled from a shell that exported MSYS_NO_PATHCONV=1 inherits
   MIND_WORLD=/c/<real path>. Windows Python reads that as rooted on the
   current drive, so `absolutize` anchored it to C:/c/<real path> and the daemon
   served an empty phantom world for 7 minutes, with no error.
2. `_paths.sh` exports PYTHONPYCACHEPREFIX from the UNCONVERTED PROJECT_ROOT,
   and `_platform.sh` converted every other root but not that one, so every
   native python wrote bytecode under C:/c/ (or made empty cwd-relative c/ dirs).

The Windows branch only executes on Windows, and dev runs POSIX, so these tests
drive it through the injection seams `normalize_msys_path` already has
(guard-3300) and a fake `cygpath`, never through the host.
"""
import os
import subprocess
from pathlib import Path

from _bash_helpers import BASH
from _path_helpers import absolutize

SCRIPTS = Path(__file__).resolve().parents[1]
PROJECT = Path("/proj")


def _fwd(p: Path) -> str:
    return str(p).replace("\\", "/")


def test_absolutize_converts_msys_root_on_windows_when_target_exists():
    # A neutral fixture path: the seed plant rewrites the dev box's workspace and project
    # names inside string literals, and rewrites the drive and MSYS spellings of one path
    # DIFFERENTLY, so `real` would stop being what the MSYS form converts to.
    real = "C:/Work/GitHub/Acme-Project/.mind-data/world"
    res = absolutize("/c/Work/GitHub/Acme-Project/.mind-data/world", PROJECT,
                     is_windows=True, exists=lambda p: p == real)
    # On a Windows host this is WindowsPath('C:/...'); on POSIX absolutize's
    # stage 1 roots the drive form at '/'. Either way the drive form won and
    # nothing was anchored to the project or the cwd.
    assert _fwd(res).endswith(real), res
    assert "/proj" not in _fwd(res), res


def test_absolutize_keeps_msys_value_when_converted_target_is_missing():
    # A legitimate C:/c/... (or a typo) must never be rewritten into a path
    # that does not exist; outcome 1's existence check owns that case.
    res = absolutize("/c/nope/world", PROJECT, is_windows=True, exists=lambda p: False)
    assert "C:/nope" not in _fwd(res), res


def test_absolutize_never_rewrites_on_a_posix_host():
    # /c/foo is an ordinary absolute path off Windows.
    seen = []
    res = absolutize("/c/foo", PROJECT, is_windows=False,
                     exists=lambda p: seen.append(p) or True)
    assert _fwd(res).endswith("/c/foo") and "C:" not in _fwd(res), res
    assert seen == [], "exists() must not be consulted off Windows"


def test_absolutize_default_seams_leave_this_host_alone():
    if os.name == "nt":
        return  # the seams above cover Windows; this pins the POSIX default
    assert absolutize("/c/foo", PROJECT) == Path("/c/foo")


def _fake_cygpath(bin_dir: Path) -> None:
    bin_dir.mkdir()
    cyg = bin_dir / "cygpath"
    cyg.write_text(
        "#!/bin/sh\n"
        "# test double: -m|-u|-w <path>; /x/rest -> X:/rest, else unchanged\n"
        'p="$2"\n'
        'case "$p" in\n'
        '  /[a-zA-Z]/*) d=$(printf %s "$p" | cut -c2 | tr a-z A-Z); '
        'printf "%s:/%s\\n" "$d" "$(printf %s "$p" | cut -c4-)" ;;\n'
        '  *) printf "%s\\n" "$p" ;;\n'
        "esac\n"
    )
    cyg.chmod(0o755)


def _source_platform(tmp_path: Path, extra_env: dict) -> list:
    bin_dir = tmp_path / "bin"
    _fake_cygpath(bin_dir)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "MSYSTEM": "MINGW64",
        "REPO_ROOT": "/c/proj", "PROJECT_ROOT": "/c/proj", "CORE_ROOT": "/c/proj/core",
        "CONFIG_DIR": "/c/proj/core/config", "META_DIR": "/c/meta", "WORLD_DIR": "/c/world",
        "AGENT_DIR": "",
    }
    env.update(extra_env)
    script = (f'. "{_fwd(SCRIPTS / "_platform.sh")}"; '
              'printf "%s\\n" "${PYTHONPYCACHEPREFIX-<unset>}" "$PROJECT_ROOT" "${MSYS_NO_PATHCONV-}"')
    r = subprocess.run([BASH, "-c", script], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.splitlines()


def test_platform_sh_exports_pycache_prefix_in_native_form(tmp_path):
    out = _source_platform(tmp_path, {"PYTHONPYCACHEPREFIX": "/c/proj/core/.pycache"})
    # Control: the fake cygpath really converted the roots it already handled.
    assert out[1] == "C:/proj" and out[2] == "1", out
    assert out[0] == "C:/proj/core/.pycache", out


def test_platform_sh_does_not_invent_a_pycache_prefix(tmp_path):
    out = _source_platform(tmp_path, {})
    assert out[0] == "<unset>", out


def test_platform_sh_rebuilds_the_prefix_paths_sh_exports():
    # _platform.sh re-derives the prefix from the converted PROJECT_ROOT rather
    # than paying another cygpath spawn, so the two literals must not drift.
    want = 'PYTHONPYCACHEPREFIX="$PROJECT_ROOT/core/.pycache"'
    assert want in (SCRIPTS / "_paths.sh").read_text(encoding="utf-8")
    assert want in (SCRIPTS / "_platform.sh").read_text(encoding="utf-8")
