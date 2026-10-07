"""efs-ssh.sh: the opt-in EFS_TRANSPORT=ssh branch (, outcome 3).

WHAT IS BEING PINNED. `world/scripts/efs-ssh.sh` reaches the operator over SSM by
default. With EFS_TRANSPORT=ssh it `exec`s ssh to the operator's home-host guest
instead: key-only, host key pinned, the remote command bounded by timeout(1)
inside the SSM budget, stdin streamed, no fallback to SSM. The default path must
not change, and the secret-file rail and the path warning (both run BEFORE the
branch, on the plaintext command) must still apply in ssh mode (guard-4376).

HOW. `ssh` is a fake first on PATH that records its argv (NUL-separated, so a
command containing a newline survives) and its stdin; the SSM runner is the same
stub style test_efs_ssh_path_warning.py uses, extended to record whether it was
asked to do anything beyond the secret-file check. Both are the real script's
real seams: nothing here reproduces the script's logic in Python.

MUTATION RESULTS (guard-4166). Measured 2026-10-03 on cc-04: one exact-match edit per
mutant, applied to a COPY of efs-ssh.sh with this module's _EFS_SSH repointed at it;
every prediction was written before the run and 11 of 11 matched. Controls: an
unmodified copy is all green, a script that only exits 99 is all red.
  * `case` block deleted             -> 11 red, every ssh-mode test except the rail test
                                        (the rail runs before the branch either way);
                                        the two SSM-default tests stay green.
  * StrictHostKeyChecking dropped    -> test_ssh_argv_is_key_only_and_pinned only.
  * port hardcoded to 22             -> test_ssh_argv_is_key_only_and_pinned only (the
                                        2222 case is what pins it).
  * branch moved above the rail      -> test_secret_rail_still_runs_before_ssh AND
                                        test_path_warning_still_fires_and_the_command_is_unchanged
                                        (both run before the branch).
  * ssh failure falls through to SSM -> test_remote_exit_code_is_the_scripts_exit_code AND
                                        test_ssh_failure_is_not_retried_over_ssm.
  * remote timeout(1) dropped        -> test_remote_command_is_bounded_and_survives_quoting AND
                                        test_remote_bound_follows_the_ssm_budget.
  * KNOWN_HOSTS not required         -> test_each_missing_setting_exits_77_and_is_named only.
  * default flipped to ssh           -> test_default_is_ssm_and_never_spawns_ssh only.
  * stdin redirected from /dev/null  -> test_stdin_streams_to_ssh only.
SKIPS where the external world tree is absent (world/ is a user-configured
external path; see core/config/conventions/external-paths.md).
"""
import os
import re
import shlex
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH  # noqa: E402

PROJECT_ROOT = SCRIPT_DIR.parents[2]
MARKER = "__EFS9f3a__"

_SSM_STUB = f"""#!/usr/bin/env bash
if [ "${{1:-}}" = "--check-secret-read" ]; then exit "${{STUB_RAIL_RC:-0}}"; fi
echo ran > "$STUB_RAN"
printf '%s\\n' '{MARKER}RC:0' '{MARKER}LEN:3' '{MARKER}ERR' '{MARKER}OUT' 'ok'
"""

_FAKE_SSH = """#!/usr/bin/env bash
printf '%s\\0' "$@" > "$FAKE_SSH_ARGS"
cat > "$FAKE_SSH_STDIN"
echo fake-ssh-out
exit "${FAKE_SSH_RC:-0}"
"""


def _msys(p) -> str:
    """C:/Users/x -> /c/Users/x. Identity on POSIX, where as_posix() is already
    the right form and there is no drive letter to collide with the separator."""
    s = Path(p).as_posix()
    m = re.match(r"^([A-Za-z]):/(.*)$", s)
    return f"/{m.group(1).lower()}/{m.group(2)}" if m else s


def _world_path():
    r = subprocess.run(
        [BASH, "-c", 'source core/scripts/_paths.sh && printf %s "$WORLD_PATH"'],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=120,
    )
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


_WORLD = _world_path()
_EFS_SSH = (_WORLD / "scripts" / "efs-ssh.sh") if _WORLD else None

_SSH_ENV = {
    "EFS_TRANSPORT": "ssh",
    "EFS_SSH_HOST": "guest.invalid",
    "EFS_SSH_KEY_PATH": "/k/id",
    "EFS_SSH_KNOWN_HOSTS": "/k/known",
}


@unittest.skipUnless(
    _EFS_SSH is not None and _EFS_SSH.is_file(),
    "world/scripts/efs-ssh.sh not present (external world path unconfigured)",
)
class EfsSshSshTransport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # guard-956: never build an rm on an unguarded variable (see
        # test_efs_ssh_path_warning.py for the measured hazard).
        # cygpath -m: the MSYS path `mktemp -d` prints (/tmp/tmp.X) is not one native Python can
        # open; the mixed form (C:/...) is, and both sides accept it (guard-581: -m, never -w).
        _tmp = subprocess.run(
            [BASH, "-c", 'd=$(mktemp -d) && { cygpath -m "$d" 2>/dev/null || echo "$d"; }'],
            capture_output=True, text=True, timeout=60).stdout.strip()
        if not _tmp or _tmp in (".", "/"):
            raise unittest.SkipTest(f"mktemp -d gave no usable path: {_tmp!r}")
        cls._tmp = Path(_tmp)
        (cls._tmp / "bin").mkdir()
        cls._stub = cls._tmp / "ssm-stub.sh"
        cls._stub.write_text(_SSM_STUB, encoding="utf-8")
        cls._stub.chmod(0o755)
        fake = cls._tmp / "bin" / "ssh"
        fake.write_text(_FAKE_SSH, encoding="utf-8")
        fake.chmod(0o755)

    @classmethod
    def tearDownClass(cls):
        tmp = str(getattr(cls, "_tmp", "") or "")
        if tmp and tmp not in (".", "/"):
            subprocess.run([BASH, "-c", f'rm -rf "{tmp}"'], timeout=60)

    def setUp(self):
        self._args = self._tmp / "ssh-args"
        self._stdin = self._tmp / "ssh-stdin"
        self._ran = self._tmp / "ssm-ran"
        for p in (self._args, self._stdin, self._ran):
            if p.exists():
                p.unlink()

    def _run(self, command, extra_env=None, stdin_text=None, drop=()):
        env = dict(os.environ, EFS_SSM_RUN=str(self._stub),
                   FAKE_SSH_BIN=_msys(self._tmp / "bin"),
                   FAKE_SSH_ARGS=str(self._args), FAKE_SSH_STDIN=str(self._stdin),
                   STUB_RAN=str(self._ran))
        for k in ("EFS_TRANSPORT", "EFS_SSH_HOST", "EFS_SSH_KEY_PATH", "EFS_SSH_KNOWN_HOSTS",
                  "EFS_SSH_PORT", "SSM_POLL_TIMEOUT"):
            env.pop(k, None)
        env.update(extra_env or {})
        for k in drop:
            env.pop(k, None)
        # The fake ssh goes on PATH from INSIDE bash: Git-for-Windows bash rebuilds PATH at
        # startup, so a PATH set through env cannot shadow its own /usr/bin/ssh (guard-4445).
        return subprocess.run(
            [BASH, "-c", 'export PATH="$FAKE_SSH_BIN:$PATH"; exec bash "$0" "$@"', str(_EFS_SSH),
             command], cwd=str(PROJECT_ROOT), capture_output=True,
            text=True, timeout=180, env=env,
            input=stdin_text if stdin_text is not None else None,
            stdin=None if stdin_text is not None else subprocess.DEVNULL,
        )

    def _ssh_argv(self):
        self.assertTrue(self._args.exists(), "ssh was not invoked")
        return self._args.read_bytes().decode().split("\0")[:-1]

    # --- the default path is untouched ------------------------------------
    def test_default_is_ssm_and_never_spawns_ssh(self):
        r = self._run("hostname")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ok", r.stdout)
        self.assertTrue(self._ran.exists(), "SSM runner was not used")
        self.assertFalse(self._args.exists(), "default path spawned ssh")

    def test_explicit_ssm_is_the_default(self):
        r = self._run("hostname", {"EFS_TRANSPORT": "ssm"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self._ran.exists())
        self.assertFalse(self._args.exists())

    # --- ssh mode ---------------------------------------------------------
    def test_ssh_mode_runs_ssh_and_never_the_ssm_runner(self):
        r = self._run("hostname", _SSH_ENV)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("fake-ssh-out", r.stdout)
        self._ssh_argv()
        self.assertFalse(self._ran.exists(), "ssh mode also ran the SSM runner")

    def test_ssh_argv_is_key_only_and_pinned(self):
        self._run("hostname", dict(_SSH_ENV, EFS_SSH_PORT="2222"))
        argv = self._ssh_argv()
        self.assertEqual(argv[argv.index("-i") + 1], "/k/id")
        self.assertEqual(argv[argv.index("-p") + 1], "2222")
        for opt in ("BatchMode=yes", "IdentitiesOnly=yes", "StrictHostKeyChecking=yes",
                    "UserKnownHostsFile=/k/known", "ConnectTimeout=15"):
            self.assertIn(opt, argv)
        self.assertEqual(argv[-2], "ec2-user@guest.invalid")

    def test_port_defaults_to_22(self):
        self._run("hostname", _SSH_ENV)
        argv = self._ssh_argv()
        self.assertEqual(argv[argv.index("-p") + 1], "22")

    def test_remote_command_is_bounded_and_survives_quoting(self):
        command = "echo 'a b'; printf '%s\\n' \"$HOME\" | head -1"
        self._run(command, _SSH_ENV)
        remote = self._ssh_argv()[-1]
        self.assertEqual(shlex.split(remote), ["timeout", "115", "bash", "-c", command])

    def test_remote_bound_follows_the_ssm_budget(self):
        self._run("hostname", dict(_SSH_ENV, SSM_POLL_TIMEOUT="30"))
        self.assertEqual(shlex.split(self._ssh_argv()[-1])[:2], ["timeout", "25"])

    def test_stdin_streams_to_ssh(self):
        r = self._run("cat", _SSH_ENV, stdin_text="abc")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._stdin.read_text(), "abc")

    def test_remote_exit_code_is_the_scripts_exit_code(self):
        r = self._run("exit 7", dict(_SSH_ENV, FAKE_SSH_RC="7"))
        self.assertEqual(r.returncode, 7)

    def test_ssh_failure_is_not_retried_over_ssm(self):
        r = self._run("hostname", dict(_SSH_ENV, FAKE_SSH_RC="255"))
        self.assertEqual(r.returncode, 255)
        self.assertFalse(self._ran.exists(), "ssh failure fell back to SSM")

    # --- configuration errors are loud and spawn nothing ------------------
    def test_each_missing_setting_exits_77_and_is_named(self):
        for var in ("EFS_SSH_HOST", "EFS_SSH_KEY_PATH", "EFS_SSH_KNOWN_HOSTS"):
            with self.subTest(missing=var):
                r = self._run("hostname", _SSH_ENV, drop=(var,))
                self.assertEqual(r.returncode, 77)
                self.assertIn(var, r.stderr)
                self.assertFalse(self._args.exists())
                self.assertFalse(self._ran.exists())

    def test_unknown_transport_exits_78(self):
        r = self._run("hostname", {"EFS_TRANSPORT": "carrier-pigeon"})
        self.assertEqual(r.returncode, 78)
        self.assertIn("carrier-pigeon", r.stderr)
        self.assertFalse(self._args.exists())
        self.assertFalse(self._ran.exists())

    # --- what runs BEFORE the branch still applies in ssh mode ------------
    def test_secret_rail_still_runs_before_ssh(self):
        r = self._run("cat /etc/sysconfig/anything", dict(_SSH_ENV, STUB_RAIL_RC="1"))
        self.assertEqual(r.returncode, 1)
        self.assertFalse(self._args.exists(), "the rail was bypassed in ssh mode")

    def test_path_warning_still_fires_and_the_command_is_unchanged(self):
        r = self._run("ls /mnt/AyoAi/Accounts", _SSH_ENV)
        self.assertIn("[efs-ssh] WARNING:", r.stderr)
        self.assertIn("/mnt/AyoAi/Accounts", self._ssh_argv()[-1])


if __name__ == "__main__":
    unittest.main()
