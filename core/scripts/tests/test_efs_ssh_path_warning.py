"""efs-ssh.sh warns on an un-translated EFS path ().

WHAT IS BEING PINNED. `world/scripts/efs-ssh.sh` emits ONE stderr warning when
its command names a path that starts with /mnt/AyoAi, /mnt/efs or /efs — the
namespaces a Lambda log reports — and stays silent for the operator-side
/home/ec2-user/AyoAi-Efs prefix. It NEVER rewrites the command; the warning is
advisory and the command runs unchanged.

WHY THE NEGATIVE TEST IS THE LOAD-BEARING ONE. The CORRECT path
/home/ec2-user/AyoAi-Efs/mnt/AyoAi/X CONTAINS the substring /mnt/AyoAi, so the
obvious implementation (`grep -q /mnt/AyoAi`) fires on every correct call. A
warning that cries wolf on the normal path is worse than no warning, because
readers learn to ignore it — so `test_correct_prefix_is_silent` is what keeps
the guard useful, not merely present.

MUTATION EXPECTATIONS (guard-4166 — an absence assertion and a dead component
look identical, so state per-test which pins move BEFORE running the mutant):
  * Delete the whole `if` block  -> FIVE tests go RED (the three *_warns, plus
                                    test_warning_names_the_translation_and_the_locator
                                    and test_exactly_one_warning_even_with_two_bad_paths,
                                    both of which also need the detector to fire);
                                    test_correct_prefix_is_silent stays GREEN.
  * Replace the anchored regex with a bare substring `grep -q /mnt/AyoAi`
                                 -> test_correct_prefix_is_silent goes RED;
                                    the WARN tests stay GREEN.
The two mutants move DISJOINT sets. That asymmetry is the evidence the pins
test the behaviour rather than each other; both were executed for this change.

WHY grep IS INVOKED THROUGH A SUBPROCESS AND NOT REPRODUCED IN PYTHON: an
interactive shell may define `grep` as a FUNCTION wrapping a different engine,
and it answers differently from the /usr/bin/grep every script actually gets
(measured while writing this test: the anchored pattern reported nomatch under
the shell function and MATCH under /usr/bin/grep, on the same input — the
probe-with-canonical-code-path.md rule-4 class). Testing the real script
through bash is the only shape that measures what production runs.

SKIPS where the external world tree is absent: world/ is a user-configured
external path (see core/config/conventions/external-paths.md), so a clone
without one has no script to exercise.
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH  # noqa: E402

# parents[2], not .parent.parent: SCRIPT_DIR is core/scripts/tests, so two
# hops land on core/ and `source core/scripts/_paths.sh` then fails from that
# cwd — which returned None and SKIPPED every test in this file rather than
# failing. A skip reads as 'not applicable here', so the broken predicate was
# invisible until the skip reason was actually printed (-rs).
PROJECT_ROOT = SCRIPT_DIR.parents[2]
MARKER = "__EFS9f3a__"
WARN_PREFIX = "[efs-ssh] WARNING:"

# Emits exactly the trailer shape efs-ssh.sh's awk parser expects, so the
# script's own parsing path is exercised rather than bypassed. LEN must equal
# the byte count of the OUT section ("ok\n" = 3) or the script reports the
# truncation failure (exit 75) instead of the command's own rc.
_STUB = f"""#!/usr/bin/env bash
if [ "${{1:-}}" = "--check-secret-read" ]; then exit 0; fi
printf '%s\\n' '{MARKER}RC:0' '{MARKER}LEN:3' '{MARKER}ERR' '{MARKER}OUT' 'ok'
"""


def _world_path():
    r = subprocess.run(
        [BASH, "-c", 'source core/scripts/_paths.sh && printf %s "$WORLD_PATH"'],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=120,
    )
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


_WORLD = _world_path()
_EFS_SSH = (_WORLD / "scripts" / "efs-ssh.sh") if _WORLD else None


@unittest.skipUnless(
    _EFS_SSH is not None and _EFS_SSH.is_file(),
    "world/scripts/efs-ssh.sh not present (external world path unconfigured)",
)
class EfsSshPathWarning(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = Path(
            subprocess.run([BASH, "-c", "mktemp -d"], capture_output=True,
                           text=True, timeout=60).stdout.strip()
        )
        cls._stub = cls._tmp / "ssm-stub.sh"
        cls._stub.write_text(_STUB, encoding="utf-8")
        cls._stub.chmod(0o755)

    @classmethod
    def tearDownClass(cls):
        subprocess.run([BASH, "-c", f"rm -rf {cls._tmp}"], timeout=60)

    def _run(self, command):
        env = dict(os.environ, EFS_SSM_RUN=str(self._stub))
        return subprocess.run(
            [BASH, str(_EFS_SSH), command],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            timeout=180, stdin=subprocess.DEVNULL, env=env,
        )

    # --- WARN: the namespaces a  log reports ------------------------
    def test_mnt_ayoai_warns(self):
        r = self._run("ls /mnt/AyoAi/Accounts")
        self.assertIn(WARN_PREFIX, r.stderr)

    def test_mnt_efs_warns(self):
        self.assertIn(WARN_PREFIX, self._run("cat /mnt/efs/MindSidecars/x").stderr)

    def test_bare_efs_warns(self):
        self.assertIn(WARN_PREFIX, self._run("ls /efs/foo").stderr)

    def test_warning_names_the_translation_and_the_locator(self):
        """The warning must be actionable in the moment, not just a flag."""
        err = self._run("ls /mnt/AyoAi/Accounts").stderr
        self.assertIn("/home/ec2-user/AyoAi-Efs/mnt/AyoAi/X", err)
        self.assertIn("aws-resource-locators.md", err)
        self.assertIn("wc -l", err)  # the positive control that separates
        #                              wrong-mount from genuinely-absent

    # --- SILENT: the correct operator-side prefix (the false-positive trap)
    def test_correct_prefix_is_silent(self):
        for cmd in (
            "ls /home/ec2-user/AyoAi-Efs/mnt/AyoAi/Accounts",
            "ls /home/ec2-user/AyoAi-Efs",
            "ls /home/ec2-user/AyoAi-Efs/mnt/AyoAi/Accounts/1/env/logs",
            "hostname",
            "ls /efsomething",
        ):
            with self.subTest(cmd=cmd):
                self.assertNotIn(WARN_PREFIX, self._run(cmd).stderr)

    # --- exactly one, and non-interfering ---------------------------------
    def test_exactly_one_warning_even_with_two_bad_paths(self):
        err = self._run("cp /mnt/AyoAi/a /mnt/efs/b").stderr
        self.assertEqual(err.count(WARN_PREFIX), 1)

    def test_command_still_runs_unchanged(self):
        """Warn-only: the stubbed remote result must survive intact."""
        warned = self._run("ls /mnt/AyoAi/Accounts")
        clean = self._run("ls /home/ec2-user/AyoAi-Efs/mnt/AyoAi/Accounts")
        self.assertEqual(warned.returncode, 0)
        self.assertEqual(warned.stdout, clean.stdout)


if __name__ == "__main__":
    unittest.main()
