#!/usr/bin/env python3
"""Regression pins for the  hardening of the python3 shim in _paths.sh.

THE INCIDENT (rb-9904 / guard-5772, 2026-09-01): the framework's
`.python-shim/python3` exec'd a PATH-relative launcher NAME (`py`), and on a
Linux box whose `/usr/local/bin/py` is itself `exec python3 "$@"` the two
formed an exec-loop — one pid, tiny RSS, cpu_time ≈ elapsed — that burned a
full core on the fleet host for 3 days, invisible to every count/RSS monitor.
The unbounded `python3 -c pass` probe that decides whether to GENERATE the shim
could also enter an existing loop and hang, and a "python3 unusable" verdict
then wrote the very shim that loops.

Three clauses, each pinned by a test that FAILS when its clause is removed:

  1. never generate (nor prepend) the shim on a non-Windows shell
  2. the generation probe is bounded by `timeout`
  3. the generated shim execs an ABSOLUTE path resolved at generation time

$OSTYPE is the platform gate, and bash honours an INHERITED value (set-if-not),
so every test picks the platform it simulates by exporting OSTYPE — `linux-gnu`
on a Windows box, `msys` on a Linux one — and runs on either.

Every fake launcher lives in a per-test bin dir placed FIRST on PATH, so it
shadows the real python3/py/python of whichever box runs the suite; nothing here
touches the repo's own .python-shim.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from _bash_helpers import BASH  # noqa: E402

PATHS_SH = _HERE.parent / "_paths.sh"


def _script(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body + "\n", encoding="utf-8", newline="\n")
    path.chmod(0o755)


class _Repo:
    """A throwaway repo: core/scripts/_paths.sh + an empty agent conf + a fake bin."""

    def __init__(self):
        self._td = tempfile.TemporaryDirectory(prefix="paths-shim-")
        self.root = Path(self._td.name)
        core_scripts = self.root / "core" / "scripts"
        core_scripts.mkdir(parents=True)
        (core_scripts / "_paths.sh").write_bytes(PATHS_SH.read_bytes())
        agent = self.root / "agents" / "alpha"
        agent.mkdir(parents=True)
        (agent / "local-paths.conf").write_text("WORLD_PATH=\nMETA_PATH=\n")
        self.shim_dir = core_scripts / ".python-shim"
        self.bin = self.root / "fakebin"
        self.bin.mkdir()

    def cleanup(self):
        self._td.cleanup()

    def fake(self, name: str, body: str) -> None:
        _script(self.bin / name, body)

    def run(self, ostype: str, script: str, timeout: int = 45):
        env = os.environ.copy()
        env.pop("MIND_SKIP_PY_SHIM", None)
        env["OSTYPE"] = ostype
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        env["MIND_AGENT"] = "alpha"
        t0 = time.monotonic()
        p = subprocess.run([BASH, "-c", script], cwd=str(self.root), env=env,
                           capture_output=True, text=True, timeout=timeout)
        return p, time.monotonic() - t0


SOURCE = "source core/scripts/_paths.sh; echo SOURCED; echo PY3=$(command -v python3)"


class TestLinuxNeverGeneratesTheShim(unittest.TestCase):
    """Clause 1. The scenario is the fleet host's: a `py` that is a bare
    `exec python3` wrapper, and a python3 the probe would call unusable."""

    def setUp(self):
        self.repo = _Repo()
        self.addCleanup(self.repo.cleanup)
        self.repo.fake("py", 'exec python3 "$@"')          # the Ubuntu wrapper shape
        self.repo.fake("python3", "exit 49")                # the Store-stub verdict

    def test_linux_shell_generates_nothing_and_leaves_path_alone(self):
        p, _ = self.repo.run("linux-gnu", SOURCE)
        self.assertIn("SOURCED", p.stdout, p.stderr)
        self.assertFalse(self.repo.shim_dir.exists(),
                         "a Linux shell must never write .python-shim")
        self.assertNotIn(".python-shim", p.stdout)

    def test_positive_control_same_scenario_on_a_windows_shell_generates(self):
        # Proves the scenario above reaches the generation branch when the gate
        # opens — so the ONLY thing standing between it and a shim is the gate.
        p, _ = self.repo.run("msys", SOURCE)
        self.assertIn("SOURCED", p.stdout, p.stderr)
        self.assertTrue((self.repo.shim_dir / "python3").exists())
        self.assertIn(".python-shim", p.stdout)

    def test_existing_shim_dir_is_not_prepended_on_a_linux_shell(self):
        self.repo.shim_dir.mkdir()
        _script(self.repo.shim_dir / "python3", 'exec py "$@"')  # the loop-forming shape
        p, _ = self.repo.run("linux-gnu", SOURCE)
        self.assertIn("SOURCED", p.stdout, p.stderr)
        self.assertNotIn(".python-shim", p.stdout,
                         "a stale shim dir on Linux must stay off PATH")
        # positive control: the same dir IS prepended on a Windows shell
        p, _ = self.repo.run("msys", SOURCE)
        self.assertIn(".python-shim", p.stdout)


class TestLoopPairOnPathDoesNotHangSourcing(unittest.TestCase):
    """Clause 1, the self-install half. With `py` and `python3` forming an
    exec-loop on PATH, the OLD block ran `python3 -c pass` unbounded and hung
    forever; the gate means a Linux shell never runs the probe at all."""

    def test_source_returns_fast_with_an_exec_loop_on_path(self):
        repo = _Repo()
        self.addCleanup(repo.cleanup)
        repo.fake("py", 'exec python3 "$@"')
        repo.fake("python3", 'exec py "$@"')
        p, elapsed = repo.run("linux-gnu", "source core/scripts/_paths.sh; echo SOURCED",
                              timeout=40)
        self.assertIn("SOURCED", p.stdout, p.stderr)
        self.assertLess(elapsed, 5.0,
                        "sourcing _paths.sh must not enter the loop (nor its 15s timeout)")
        self.assertFalse(repo.shim_dir.exists())


class TestWindowsGenerationShape(unittest.TestCase):
    """Clauses 2 and 3, on a simulated Windows shell."""

    def setUp(self):
        self.repo = _Repo()
        self.addCleanup(self.repo.cleanup)
        # A working launcher: the real interpreter running this test.
        self.repo.fake("py", f'exec "{sys.executable}" "$@"')

    def test_generated_shim_execs_an_absolute_path_and_works(self):
        self.repo.fake("python3", "exit 49")
        p, _ = self.repo.run("msys", (
            'PY_ABS=$(command -v py); source core/scripts/_paths.sh; '
            'echo "TARGET=$PY_ABS"; python3 -c "print(6*7)"'))
        self.assertIn("TARGET=", p.stdout, p.stderr)
        target = p.stdout.split("TARGET=", 1)[1].splitlines()[0].strip()
        self.assertTrue(target.endswith("/py"), target)
        shim = (self.repo.shim_dir / "python3").read_text(encoding="utf-8")
        self.assertIn(f'_py="{target}"', shim, shim)
        self.assertIn('exec "$_py" "$@"', shim)
        self.assertNotIn("for candidate", shim, "no per-call PATH re-resolution")
        self.assertNotIn("exec py", shim)
        self.assertNotIn("exec python3", shim)
        self.assertIn("42", p.stdout, "the generated shim must run the real interpreter")
        self.assertTrue((self.repo.shim_dir / "python").exists())

    def test_generated_shim_fails_loud_when_its_target_disappears(self):
        self.repo.fake("python3", "exit 49")
        p, _ = self.repo.run("msys", SOURCE)
        self.assertTrue((self.repo.shim_dir / "python3").exists(), p.stderr)
        (self.repo.bin / "py").unlink()
        q = subprocess.run([BASH, "core/scripts/.python-shim/python3", "-c", "pass"],
                           cwd=str(self.repo.root), capture_output=True, text=True,
                           timeout=30)
        self.assertEqual(q.returncode, 127, q.stderr)
        self.assertIn("delete", q.stderr)

    def test_hanging_probe_is_bounded_by_timeout(self):
        # A python3 that never returns — the Store stub's worst behaviour. The
        # OLD block waited on it forever; the bounded probe gives up at 15s and
        # treats the hang as "unusable", which is what the shim is for.
        self.repo.fake("python3", "exec sleep 60")
        p, elapsed = self.repo.run("msys", SOURCE, timeout=50)
        self.assertIn("SOURCED", p.stdout, p.stderr)
        self.assertLess(elapsed, 40.0, "the probe must be bounded, not waited out")
        self.assertTrue((self.repo.shim_dir / "python3").exists(),
                        "a timed-out probe still resolves to the working launcher")


if __name__ == "__main__":
    unittest.main()
