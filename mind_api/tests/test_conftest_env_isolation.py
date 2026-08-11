"""Pin the `mind_api/tests` conftest's per-test env restore ().

This directory carried NO autouse fixture at all until g-115-4421 — its
STORAGE_BACKEND pin was applied once at module import, so a test that mutated
the var without restoring it left it mutated for every test collected
afterwards. `core/scripts/tests/conftest.py` has had the equivalent guard since
g-115-1875; this suite was the asymmetric half.

The pin is deliberately ORDER-DEPENDENT: `test_a_*` pollutes with a RAW
`os.environ` write (monkeypatch would unwind itself and prove nothing), and
`test_b_*` asserts the conftest fixture undid it. Delete the autouse fixture and
`test_b_*` goes red — that is the mutation proof, and it is the only shape that
actually exercises the leak-across-tests property. A single-test version would
pass with no fixture present at all.

guard-1165: every mutation below happens INSIDE a test body. Nothing at module
level touches os.environ — pytest imports all collected modules into one shared
process at collection time, so a module-level write here would poison the very
suite this file is meant to protect.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

POLLUTED_BACKEND = "own-cloud"
POLLUTED_WORLD = "/tmp/g-115-4421-polluted-world-should-never-survive"

# Values planted in a SUBPROCESS env before conftest is imported, to exercise
# the import-time ordering (). Distinct from POLLUTED_WORLD above:
# that one is planted by a test BODY, this one before module import.
IMPORT_TIME_WORLD = "/tmp/g-115-4533-import-time-world-should-never-be-captured"
IMPORT_TIME_META = "/tmp/g-115-4533-import-time-meta-should-never-be-captured"

# Loads conftest.py by PATH (this directory is a package, so a bare
# `import conftest` would not resolve) and reports, for each key, whether the
# module's _BOOTSTRAP_ENV snapshot captured the UNSET sentinel or a real value.
# The conftest path arrives as argv[1] -- never interpolated into this source
# (guard-165). Importing conftest standalone is side-effect-safe: its
# module-level code only touches sys.path / PATH / os.environ, and every
# fixture is lazy.
_PROBE_SRC = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("_conftest_probe", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
snap = mod._BOOTSTRAP_ENV
parts = []
for key in ("MIND_WORLD", "MIND_META"):
    got = snap.get(key, "MISSING_KEY")
    parts.append(key + "=" + ("UNSET" if got is mod._UNSET else "CAPTURED:" + str(got)))
print(";".join(parts))
"""


def test_a_polluter_raw_writes_env():
    """Simulate the real polluter shape: a raw os.environ write, no restore."""
    os.environ["STORAGE_BACKEND"] = POLLUTED_BACKEND
    os.environ["MIND_WORLD"] = POLLUTED_WORLD
    assert os.environ["STORAGE_BACKEND"] == POLLUTED_BACKEND
    assert os.environ["MIND_WORLD"] == POLLUTED_WORLD


def test_b_conftest_restored_backend_pin():
    """STORAGE_BACKEND is back to the conftest's session pin.

    Red without the autouse fixture: test_a leaves own-cloud set, and under
    own-cloud `OwnCloudBackend._s3_key` derives the key from
    customer_prefix+env_id+filename, ignoring any tmp world redirect — the
    guard-955 / rb-2983 production-key collision.
    """
    assert os.environ.get("STORAGE_BACKEND") == "local", (
        "conftest autouse _restore_env_per_test did not re-pin "
        "STORAGE_BACKEND after a prior test polluted it"
    )


def test_b_conftest_restored_world_override():
    """MIND_WORLD is back to its bootstrap state (absent on a normal run).

    MIND_WORLD is the FIRST entry in the daemon's world-resolution chain
    (mind_api/src/agent_paths.py: env override -> .mind-data/ -> local-paths.conf),
    so a leaked value silently outranks the per-test tmp `local-paths.conf` that
    every fixture in this directory relies on for isolation.

    Asserts ABSENCE, not `!= POLLUTED_WORLD` (g-115-4533). The original form
    compared against this test file's OWN pollution sentinel, so it passed
    identically whether the bootstrap snapshot held the UNSET sentinel or a
    real production world path -- a production path is `!= POLLUTED_WORLD`
    too. It would have stayed green through the entire 2026-07-31 leak. An
    assertion whose comparison target is a value the test itself defined
    proves only that the code did not produce that synthetic value
    (guard-2336).
    """
    assert os.environ.get("MIND_WORLD") is None, (
        "conftest autouse _restore_env_per_test left MIND_WORLD set to "
        f"{os.environ.get('MIND_WORLD')!r}; it must be ABSENT so the daemon's "
        "resolver falls through to the per-test tmp local-paths.conf"
    )
    assert os.environ.get("MIND_META") is None, (
        "conftest autouse _restore_env_per_test left MIND_META set to "
        f"{os.environ.get('MIND_META')!r}"
    )


def test_c_pop_runs_above_the_bootstrap_snapshot():
    """The MIND_WORLD/MIND_META pop MUST sit ABOVE the _BOOTSTRAP_ENV snapshot.

    THIS is the assertion that actually pins the g-115-4479 fix, and it has to
    run in a SUBPROCESS. The ordering is the entire fix: the snapshot captures
    whatever is live at import time, so moving the pop below it is a silent
    no-op -- the snapshot then holds the ambient production world, and the
    autouse fixture faithfully RE-PINS production before every test.

    An in-process check cannot pin this, and that is measured rather than
    assumed (zeta, cc-02, 2026-08-01). The leak is LAUNCH-SHAPE dependent:

        bare shell                       -> MIND_WORLD unset
        after `source core/scripts/_paths.sh` -> MIND_WORLD=<production world>

    because _paths.sh:262 exports it. So on a box where the launching shell
    never sourced _paths.sh there is nothing to pop, and an in-process
    assertion passes with the pop DELETED -- mutation-blind exactly where the
    defect lives. This test plants the vars in the child's env itself, so it
    discriminates on every box regardless of how the parent shell was launched.

    Red when the pop is moved below the snapshot, or removed (guard-1475:
    proven by re-introducing the defect, not assumed).
    """
    conftest = Path(__file__).resolve().parent / "conftest.py"
    assert conftest.is_file(), f"conftest not found at {conftest}"

    env = os.environ.copy()
    env["MIND_WORLD"] = IMPORT_TIME_WORLD
    env["MIND_META"] = IMPORT_TIME_META

    proc = subprocess.run(
        [sys.executable, "-c", _PROBE_SRC, os.fspath(conftest)],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        cwd=os.fspath(conftest.resolve().parents[2]),
    )
    assert proc.returncode == 0, (
        f"conftest import probe failed rc={proc.returncode}\n"
        f"stdout: {proc.stdout[-2000:]}\nstderr: {proc.stderr[-2000:]}"
    )

    out = proc.stdout.strip()
    assert "MIND_WORLD=UNSET" in out, (
        "conftest._BOOTSTRAP_ENV captured a live MIND_WORLD at import time -- "
        "the pop is no longer running ABOVE the snapshot, so the autouse "
        f"fixture will RE-PIN it before every test. probe said: {out!r}"
    )
    assert "MIND_META=UNSET" in out, (
        "conftest._BOOTSTRAP_ENV captured a live MIND_META at import time -- "
        f"same ordering defect. probe said: {out!r}"
    )
