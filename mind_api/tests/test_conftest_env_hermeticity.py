""" -- pin BODY_WM_PATH scrubbing in this directory's conftest.

WHY THIS FILE EXISTS. `bash-agent-inject.py` exports BODY_WM_PATH on every
WORKER box so `wm.wm_path()` routes working-memory I/O to the forked per-Body
file. `wm.py:51` honors that var FIRST and unconditionally -- ahead of
MIND_AGENT_DIR, with no existence check. The `running_daemon` fixture runs the
daemon IN-PROCESS, and `aspirations_write.py::_emit_e9_skip_observation` spawns
wm.py with `os.environ.copy()`, so an unscrubbed BODY_WM_PATH sends every E9
skip/expire observation a test emits into the LIVE Body's working memory.

Measured on cc-08 2026-08-21, both directions, before this pin existed:
  - unscrubbed: test_e9_fires_on_skipped_status FAILS `assert 262 > 262` AND the
    live WM grows (+490 bytes, fixture marker count 2 -> 3)
  - scrubbed:   the same three tests pass and the live WM is byte-identical
    across a full 1,386-test run of this tree
20 fixture-authored observations from five different test files had accumulated
in live fleet state, bound for the reducer's encoding pipeline. The failing
tests were the VISIBLE half; the pollution was the harmful half, and it was
silent -- `_emit_e9_skip_observation` swallows every exception behind
`capture_output=True`. That is guard-2484's exact shape: a test writing into
live fleet state while reporting success.

WHY TWO TESTS. The effect assertion below is VACUOUSLY TRUE on a reducer box
(BODY_WM_PATH is never set there), which is precisely how this class of defect
stayed invisible -- it only manifests on a worker. So the structural assertion
carries the pin on every box, and the effect assertion carries it where the
regression actually bites. Neither alone is sufficient.
"""

import os
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent


def _this_directorys_conftest():
    """The LOADED conftest module for THIS directory, resolved by file path.

    A bare `import conftest` is wrong here and fails in a way that looks like
    the assertion it guards. pytest registers each conftest under the bare name
    `conftest`, and this repo declares three testpaths -- so whichever tree is
    collected first owns `sys.modules["conftest"]`. Running this file inside the
    full suite therefore hands you `core/scripts/tests/conftest.py`, which has
    no _BOOTSTRAP_ENV at all: the test then reports `AttributeError` and reads
    exactly like "the fix was removed". Measured 2026-08-21 -- the bare import
    passed standalone and failed under the suite.

    Looking the module up by __file__ also asserts against the module actually
    governing this directory's tests, rather than re-executing the source and
    testing a fresh copy nobody uses.
    """
    target = _HERE / "conftest.py"
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if f and pathlib.Path(f).resolve() == target:
            return mod
    raise AssertionError(
        f"no loaded module resolves to {target} -- this directory's conftest "
        "was not imported, so the assertions below would be vacuous")


def test_body_wm_path_is_absent_during_tests():
    """Effect: no test in this directory runs with WM I/O redirected.

    Non-vacuous on a WORKER box (where the hook exports the var into pytest's
    own environment); trivially true on a reducer box. Kept because the worker
    box is where the damage happens.
    """
    assert os.environ.get("BODY_WM_PATH") in (None, ""), (
        "BODY_WM_PATH is set during a test in mind_api/tests. wm.wm_path() "
        "honors it FIRST, so any subprocess this suite spawns writes working "
        "memory into the LIVE per-Body file instead of the test's tmp tree. "
        f"value={os.environ.get('BODY_WM_PATH')!r}")


def test_body_wm_path_is_registered_for_per_test_repop():
    """Structural: the module-level pop is not enough on its own.

    The pop at import time fires ONCE, at collection. A test that legitimately
    sets BODY_WM_PATH (guard-862 names it the sanctioned redirect seam) would
    otherwise leak that redirect into every test collected after it. Membership
    in _BOOTSTRAP_ENV is what makes the autouse _restore_env_per_test fixture
    re-pop it before each test.
    """
    conftest = _this_directorys_conftest()
    bootstrap = conftest._BOOTSTRAP_ENV

    # Positive control: assert against a populated mapping that still carries
    # its original members, so this cannot pass against an empty or renamed
    # dict -- the way a structural pin goes vacuous in silence.
    assert len(bootstrap) >= 5, f"_BOOTSTRAP_ENV looks truncated: {bootstrap!r}"
    assert "MIND_WORLD" in bootstrap, (
        "_BOOTSTRAP_ENV no longer carries MIND_WORLD -- this test's control is "
        "stale and its assertion below no longer means what it claims")

    assert "BODY_WM_PATH" in bootstrap, (
        "BODY_WM_PATH dropped out of _BOOTSTRAP_ENV; the autouse fixture will "
        "no longer re-pop it between tests")
    assert bootstrap["BODY_WM_PATH"] is conftest._UNSET, (
        "BODY_WM_PATH must be captured as _UNSET (i.e. popped at module level "
        "BEFORE _BOOTSTRAP_ENV is built) so the fixture POPS it rather than "
        f"restoring a live value; got {bootstrap['BODY_WM_PATH']!r}")
