#!/usr/bin/env python3
"""Pins for rt_python_launcher and rt_python_launcher_into
(core/scripts/_python_launcher.sh), g-115-11431 and g-115-11513.

The launcher selection is the single source of truth for "which python do I
call": `py -3` on a Windows shell that has the py launcher, `python3` everywhere
else. Since g-115-11431 every core script that runs inline python resolves
through it, instead of calling a bare `py -3` that dies rc=127 on a Linux box
without the /usr/local/bin/py shim (WSL, fresh containers), mostly behind
`|| true`. rt_python_launcher_into holds the selection and sets a named
variable. rt_python_launcher prints the same value for `$(...)` callers.

Clauses, each pinned by a test that fails when the clause is removed:

  1. Windows shell (OSTYPE msys*/cygwin*) with py on PATH  -> "py -3"
  2. Windows shell without py                               -> rc=1, no output
     (the daemon must never start under python3 on Windows, g-115-733)
  3. any other shell -> "python3", EVEN WHEN py is on PATH: fleet Linux boxes
     carry /usr/local/bin/py, and a POSIX box must still get python3
  4. the inline idiom `PYLAUNCH="$(rt_python_launcher)" || PYLAUNCH=python3`
     yields python3 on a Windows shell without py (guard-1098 fallback)
  5. no fork inside the selection: it reads $OSTYPE instead of `uname -s`.
     The uname form cost ~217 ms per call on Git Bash (measured 2026-09-29),
     and the resolver now runs at the top of ~60 scripts, several on hook paths.
  6. rt_python_launcher_into sets the variable IN THE CALLER'S SHELL, with the
     same value and rc as the print form in every case above ("" and rc=1 on a
     Windows shell without py), whatever name the caller picks. It has no
     subshell, which the print form's caller always pays: ~26 ms per call on
     Git Bash (g-115-11513).

$OSTYPE is the platform gate, and bash honours an INHERITED value (set-if-not;
the positive control below proves it on the box that runs the suite), so every
test picks its platform by exporting OSTYPE. PATH is set INSIDE the script,
never through the env dict: Git for Windows' bash rebuilds PATH at startup
(guard-4445), so only an in-script assignment reliably hides the real py.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from _bash_helpers import BASH  # noqa: E402

LAUNCHER_SH = _HERE.parent / "_python_launcher.sh"


def _bash_path(p: Path) -> str:
    """C:\\x\\y -> /c/x/y for Git Bash; POSIX paths unchanged."""
    s = str(p).replace("\\", "/")
    if len(s) > 1 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


class RtPythonLauncherTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="pylaunch-")
        root = Path(self._td.name)
        self.with_py = root / "with-py"
        self.no_py = root / "no-py"
        self.with_py.mkdir()
        self.no_py.mkdir()
        fake = self.with_py / "py"
        fake.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8", newline="\n")
        fake.chmod(0o755)

    def tearDown(self):
        self._td.cleanup()

    def _run(self, ostype: str, bindir: Path, body: str):
        script = (
            f'PATH="{_bash_path(bindir)}"; hash -r\n'
            f'source "{_bash_path(LAUNCHER_SH)}"\n'
            f"{body}\n"
        )
        env = os.environ.copy()
        env["OSTYPE"] = ostype
        return subprocess.run([BASH, "-c", script], env=env,
                              capture_output=True, text=True, timeout=60)

    def _resolve(self, ostype: str, bindir: Path):
        p = self._run(ostype, bindir, 'out="$(rt_python_launcher)"; rc=$?; printf "%s|%s" "$out" "$rc"')
        self.assertEqual(p.returncode, 0, p.stderr)
        out, rc = p.stdout.rsplit("|", 1)
        return out, int(rc)

    def _resolve_into(self, ostype: str, bindir: Path, name: str = "PYLAUNCH"):
        # The variable starts stale, so a path that leaves it untouched cannot
        # pass for one that sets it to "".
        p = self._run(ostype, bindir,
                      f'{name}=stale; rt_python_launcher_into {name}; rc=$?; '
                      f'printf "%s|%s" "${name}" "$rc"')
        self.assertEqual(p.returncode, 0, p.stderr)
        out, rc = p.stdout.rsplit("|", 1)
        return out, int(rc)

    def test_inherited_ostype_is_honoured(self):
        # Positive control: without it, every platform case below could be
        # silently testing the host platform only.
        for ostype in ("linux-gnu", "msys", "darwin23"):
            p = subprocess.run([BASH, "-c", 'printf "%s" "$OSTYPE"'],
                               env={**os.environ, "OSTYPE": ostype},
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(p.stdout, ostype)

    def test_windows_shell_with_py_gets_py_dash_3(self):
        for ostype in ("msys", "cygwin"):
            self.assertEqual(self._resolve(ostype, self.with_py), ("py -3", 0), ostype)

    def test_windows_shell_without_py_fails_loud(self):
        for ostype in ("msys", "cygwin"):
            self.assertEqual(self._resolve(ostype, self.no_py), ("", 1), ostype)

    def test_posix_shell_gets_python3_even_with_py_on_path(self):
        for ostype in ("linux-gnu", "darwin23", ""):
            for bindir in (self.with_py, self.no_py):
                self.assertEqual(self._resolve(ostype, bindir), ("python3", 0),
                                 f"OSTYPE={ostype!r} bindir={bindir.name}")

    def test_inline_idiom_falls_back_to_python3(self):
        body = 'PYLAUNCH="$(rt_python_launcher)" || PYLAUNCH=python3; printf "%s" "$PYLAUNCH"'
        self.assertEqual(self._run("msys", self.no_py, body).stdout, "python3")
        self.assertEqual(self._run("msys", self.with_py, body).stdout, "py -3")
        self.assertEqual(self._run("linux-gnu", self.with_py, body).stdout, "python3")

    def test_function_does_not_fork(self):
        # The selection lives in rt_python_launcher_into; the print form only
        # delegates. Neither body may fork.
        p = self._run("msys", self.with_py,
                      "declare -f rt_python_launcher_into rt_python_launcher")
        self.assertEqual(p.returncode, 0, p.stderr)
        body = p.stdout
        self.assertIn("OSTYPE", body)
        self.assertIn("printf -v", body)
        self.assertNotIn("uname", body)
        self.assertNotIn("$(", body)
        self.assertNotIn("`", body)

    def test_into_gives_the_print_forms_value_and_rc_everywhere(self):
        for ostype in ("msys", "cygwin", "linux-gnu", "darwin23", ""):
            for bindir in (self.with_py, self.no_py):
                self.assertEqual(self._resolve_into(ostype, bindir),
                                 self._resolve(ostype, bindir),
                                 f"OSTYPE={ostype!r} bindir={bindir.name}")

    def test_into_values(self):
        # The parity test above would still pass if both forms broke together.
        self.assertEqual(self._resolve_into("msys", self.with_py), ("py -3", 0))
        self.assertEqual(self._resolve_into("msys", self.no_py), ("", 1))
        self.assertEqual(self._resolve_into("linux-gnu", self.with_py), ("python3", 0))

    def test_into_sets_whatever_name_the_caller_picks(self):
        # A local in rt_python_launcher_into would shadow a caller variable of
        # the same name, so printf -v would set the local and the caller's
        # variable would stay stale. The print form's own local name is the
        # likeliest collision.
        for name in ("PYLAUNCH", "_rt_python_launcher", "launcher"):
            self.assertEqual(self._resolve_into("msys", self.with_py, name),
                             ("py -3", 0), name)

    def test_into_inline_idiom_falls_back_to_python3(self):
        body = 'rt_python_launcher_into PYLAUNCH || PYLAUNCH=python3; printf "%s" "$PYLAUNCH"'
        self.assertEqual(self._run("msys", self.no_py, body).stdout, "python3")
        self.assertEqual(self._run("msys", self.with_py, body).stdout, "py -3")
        self.assertEqual(self._run("linux-gnu", self.with_py, body).stdout, "python3")


if __name__ == "__main__":
    unittest.main()
