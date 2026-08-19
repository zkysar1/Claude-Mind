"""test_paths_fallthrough_parity.py —  regression test.

`_paths.sh` and `_paths.py` both fall through to a first-available
local-paths.conf when the bound agent's conf cannot be used. Before this fix
each one warned on exactly the case the other stayed SILENT about, so the same
event was loud or silent depending only on which language the caller happened
to be written in:

    case                          _paths.sh   _paths.py
    MIND_AGENT unset             WARN (2+)   silent
    MIND_AGENT set, conf absent   silent     WARN

Measured 2026-08-17 (cc-07, uname -r 6.8.0-137-generic): identical input
`MIND_AGENT=nonexistent-zzz`, python emitted a WARN naming the agent, bash
emitted nothing at all.

The two cases are NOT symmetric and the tests below pin that asymmetry:

  * UNSET is genuine ambiguity — warn only when 2+ confs make the pick
    non-obvious. With exactly one conf there is nothing to be ambiguous about
    and a warning would be pure noise on every single-agent box.
  * SET-but-absent is ALWAYS an error, at ANY conf count. A specific agent was
    named and is not provisioned here; nothing is ambiguous. AGENT_DIR still
    resolves to the named (nonexistent) dir while WORLD/META come from another
    agent's conf, so the process reads healthy on world paths while misrouting
    agent-private writes.

The python half is hermetic (monkeypatched `agents_root`/`agent_dir` over a
tmp tree) so it proves the 2+-conf branch on a box that has exactly one conf —
which is the standard fleet topology and therefore the box this would
otherwise never be tested on. The bash half runs the real `_paths.sh` in a
subprocess; its assertion is count-independent by construction, so it holds on
single- and multi-conf boxes alike.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CORE_SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
PATHS_SH = CORE_SCRIPTS / "_paths.sh"

# A bare "bash" argv[0] resolves to System32 WSL bash on win32 and can hang past
# the timeout (guard-580) — the same CreateProcess/System32 root cause behind the
# timeout-class suite failures. _bash_helpers.BASH mirrors _paths.sh's own
# MIND_SHELL resolution. conftest.py puts this dir on sys.path for collected
# tests; the insert keeps a direct `py -3 <file>` run working too.
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH  # noqa: E402


def _live_conf_count() -> int:
    """How many agents on THIS box carry a local-paths.conf."""
    return len(list((PROJECT_ROOT / "agents").glob("*/local-paths.conf")))


def _source_paths_sh(env_overrides: dict) -> str:
    """Source the real _paths.sh in a subprocess; return its stderr."""
    env = dict(os.environ)
    for k, v in env_overrides.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    # A stale override would silently redirect agent-dir resolution.
    env.pop("MIND_AGENT_DIR", None)
    # .as_posix(), never str(Path): bash silently strips the backslashes of a
    # str(WindowsPath), so the source would fail with a path that LOOKS right
    # in the error message (guard-581).
    proc = subprocess.run(
        [BASH, "-c", f'source "{PATHS_SH.as_posix()}"; echo "WORLD_PATH=$WORLD_PATH"'],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.stderr


class TestBashFallthroughWarns(unittest.TestCase):
    """_paths.sh — the half that was silent."""

    def test_named_but_missing_agent_warns(self):
        """MIND_AGENT names an unprovisioned agent -> loud, at ANY conf count.

        This is the regression the goal was filed on. Pre-fix stderr was EMPTY.
        """
        if _live_conf_count() < 1:
            self.skipTest("no local-paths.conf on this box; nothing to fall through to")
        err = _source_paths_sh({"MIND_AGENT": "nonexistent-zzz-g1156417"})
        self.assertIn("[_paths] WARN", err, "bash must warn on named-but-missing")
        self.assertIn(
            "nonexistent-zzz-g1156417", err,
            "the WARN must NAME the agent — an unnamed warning cannot be traced "
            "back to the polluter that set it",
        )

    def test_correctly_bound_agent_is_silent(self):
        """A healthy binding must stay quiet — no new noise on the hot path.

        _paths.sh is sourced by effectively every bash call in the framework, so
        a warning that fires on the happy path would be emitted thousands of
        times a day and would train readers to ignore the prefix entirely.
        """
        confs = sorted((PROJECT_ROOT / "agents").glob("*/local-paths.conf"))
        if not confs:
            self.skipTest("no local-paths.conf on this box")
        err = _source_paths_sh({"MIND_AGENT": confs[0].parent.name})
        self.assertNotIn(
            "[_paths] WARN", err,
            f"a correctly-bound agent must not warn; got: {err!r}",
        )


class TestPythonUnsetFallthroughWarns(unittest.TestCase):
    """_paths.py — the half that was silent, proven hermetically."""

    def setUp(self):
        if str(CORE_SCRIPTS) not in sys.path:
            sys.path.insert(0, str(CORE_SCRIPTS))
        import _paths
        self._paths = _paths
        self._orig_agents_root = _paths.agents_root
        self._orig_agent_dir = _paths.agent_dir
        self._env_snapshot = {
            k: os.environ.get(k)
            for k in ("MIND_AGENT", "MIND_WORLD", "MIND_META", "MIND_AGENT_DIR")
        }

    def tearDown(self):
        self._paths.agents_root = self._orig_agents_root
        self._paths.agent_dir = self._orig_agent_dir
        for k, v in self._env_snapshot.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _run_with_confs(self, names, agent=None):
        """Point _read_local_paths at a tmp agents-root holding len(names) confs."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for n in names:
                d = root / n
                d.mkdir(parents=True)
                (d / "local-paths.conf").write_text(
                    f"WORLD_PATH={root / n / 'world'}\nMETA_PATH={root / n / 'meta'}\n",
                    encoding="utf-8",
                )
            self._paths.agents_root = lambda: root
            self._paths.agent_dir = lambda name: root / name
            if agent is None:
                os.environ.pop("MIND_AGENT", None)
            else:
                os.environ["MIND_AGENT"] = agent
            buf = io.StringIO()
            with redirect_stderr(buf):
                parsed = self._paths._read_local_paths()
            return parsed, buf.getvalue()

    def test_unset_with_two_confs_warns(self):
        """2+ confs + unset agent -> ambiguous pick, must be logged.

        Hermetic: proves the branch on a single-conf box, where it can never
        fire against the live tree.
        """
        parsed, err = self._run_with_confs(["aaa", "bbb"])
        self.assertTrue(parsed.get("WORLD_PATH"), "must still resolve a conf")
        self.assertIn("[_paths] WARN", err, "python must warn on unset + 2 confs")
        self.assertIn("aaa", err, "the WARN must name which agent won the pick")

    def test_unset_with_one_conf_is_silent(self):
        """Exactly one conf -> unambiguous. Silence is correct, not a miss.

        This is the single-agent-box guarantee: the standard fleet topology is
        one conf per box (measured cc-07: 6 agent dirs, 1 conf), so a warning
        here would fire on every hook fail-open on every box in the fleet.
        """
        parsed, err = self._run_with_confs(["solo"])
        self.assertTrue(parsed.get("WORLD_PATH"), "must still resolve the conf")
        self.assertNotIn(
            "[_paths] WARN", err,
            f"single-conf fall-through is unambiguous; got: {err!r}",
        )

    def test_named_but_missing_warns_at_one_conf(self):
        """SET-but-absent is an error at ANY count — including one conf.

        Pins the asymmetry: unlike the unset case above, this must NOT be
        gated on conf count. Same tmp tree, same single conf, opposite verdict.
        """
        parsed, err = self._run_with_confs(["solo"], agent="ghost-zzz")
        self.assertTrue(parsed.get("WORLD_PATH"), "must still fall through")
        self.assertIn("[_paths] WARN", err, "named-but-missing must warn at any count")
        self.assertIn("ghost-zzz", err, "the WARN must name the missing agent")


class TestCrossLanguageParity(unittest.TestCase):
    """The invariant the two suites above exist to protect."""

    def test_both_helpers_warn_on_named_but_missing(self):
        """One event, two languages, same verdict.

        The defect was not that either helper was wrong in isolation — each was
        defensible on its own. It was that they DISAGREED, so whether an
        operator saw the event depended on an irrelevant implementation detail
        of the caller.
        """
        if _live_conf_count() < 1:
            self.skipTest("no local-paths.conf on this box")
        agent = "parity-ghost-zzz"
        bash_err = _source_paths_sh({"MIND_AGENT": agent})

        if str(CORE_SCRIPTS) not in sys.path:
            sys.path.insert(0, str(CORE_SCRIPTS))
        proc = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, {str(CORE_SCRIPTS)!r}); import _paths"],
            cwd=str(PROJECT_ROOT),
            env={**os.environ, "MIND_AGENT": agent},
            capture_output=True, text=True, timeout=60,
        )
        py_err = proc.stderr

        self.assertIn("[_paths] WARN", bash_err, "bash side must warn")
        self.assertIn("[_paths] WARN", py_err, "python side must warn")
        for stream, label in ((bash_err, "bash"), (py_err, "python")):
            self.assertIn(agent, stream, f"{label} WARN must name the agent")


if __name__ == "__main__":
    unittest.main()
