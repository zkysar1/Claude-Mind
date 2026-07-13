"""Pytest configuration for core/scripts/tests/.

Many production modules (wm.py, journal.py, curriculum.py, etc.) compute
module-level paths from AGENT_DIR at import time. AGENT_DIR resolves from
MIND_AGENT in _paths.py. Without an agent bound, these imports fail with
TypeError at `CONST = AGENT_DIR / "..."`.

Production code stays strict: it MUST fail loud at import when AGENT_DIR is
None, because any non-test caller running without an agent has misconfigured
its environment, and a clear TypeError at module load beats an obscure
AttributeError deep in some function call later.

The wrinkle: ~12 test files in this directory call
`os.environ.pop("MIND_AGENT", None)` at MODULE level to bootstrap a clean
WORLD env. Pytest collects test modules alphabetically; one early-alphabetical
test (test_audit_baselines_race.py) imports `_paths` which caches AGENT_DIR
at first-import time. Any pop that fires BEFORE that first import causes the
cache to lock in AGENT_DIR=None for the entire pytest session.

Fix: in this conftest (which pytest loads BEFORE any test module), (1) set
MIND_AGENT to the first available agent, (2) import `_paths` once to lock
in AGENT_DIR. Subsequent test-module pops affect os.environ but cannot
unset the now-cached `_paths.AGENT_DIR`. Tests that override the WM path
must set the BODY_WM_PATH env var (e.g.
`monkeypatch.setenv("BODY_WM_PATH", str(tmp))`) — NOT patch `wm.WM_PATH`:
after g-306-61 WM_PATH is a dynamic `__getattr__` property and read_wm /
write_wm / cmd_init / cmd_reset resolve through `wm_path()` (BODY_WM_PATH
env → else AGENT_DIR/session/working-memory.yaml), so patching the module
attribute is a no-op for I/O and silently targets the live bound-agent WM
(running such a test under MIND_AGENT clobbers live working memory). (g-115-1626)

Selection: honor an externally-set MIND_AGENT if present, else pick the
first directory under PROJECT_ROOT containing a local-paths.conf.

Second wrinkle (added 2026-05-19): pytest collection imports ALL test
modules before any test RUNS. So a module-level pop in test_tree_idf.py
contaminates the env that test_auto_contract.py sees at run time, even
though test_auto_contract.py sorts FIRST alphabetically. ~18 of the 20
baseline failures traced to this: lost MIND_AGENT (Cluster H —
fresh importlib-loaded wm.py re-reads env and fails assert_agent_dir),
and lost MIND_WORLD redirected to a temp dir (Cluster B-G — daemon
fixtures resolve against the wrong world). The `_restore_env_per_test`
autouse fixture below snapshots both vars at conftest-load time (before
any polluter module has imported) and restores them before each test.
"""
import os
import sys
from pathlib import Path

import pytest


_AGENTS_PARENT_DIR = "agents"  # Phase 2.5.C: sync with _paths.py AGENTS_PARENT_DIR


def _set_default_agent():
    if os.environ.get("MIND_AGENT"):
        return
    project_root = Path(__file__).resolve().parents[3]
    agents_root = project_root / _AGENTS_PARENT_DIR if _AGENTS_PARENT_DIR else project_root
    for entry in sorted(agents_root.iterdir()):
        if entry.is_dir() and (entry / "local-paths.conf").is_file():
            os.environ["MIND_AGENT"] = entry.name
            return


_set_default_agent()

# Hermetic storage backend (lodestar-s7 test isolation): tests must NEVER touch
# real S3. After the own-cloud cutover, .env.local carries
# STORAGE_BACKEND=own-cloud, so ANY daemon spawned from this repo —
# including test-spawned subprocess daemons that run mind_api.src.__main__ and
# its _load_env_local — would otherwise inherit own-cloud and either hit real S3
# or 500 in the hermetic test env. Pin local for the whole pytest session
# (subprocesses inherit os.environ). Own-cloud behavior is covered by the
# moto-mocked test_owncloud_backend.py, which constructs the backend directly
# rather than via this env selector, so the pin does not reduce its coverage.
os.environ["STORAGE_BACKEND"] = "local"

# 5: dormant-pin the own-cloud tempdir tripwire OFF for the whole pytest
# session. Under pytest ALL backends are hermetic -- get_backend() returns
# LocalBackend (STORAGE_BACKEND=local above), and the only tests reaching
# OwnCloudBackend._put construct it directly against a moto-mocked S3 (no real
# cloud), so their tmp-path PUTs are safe. The _assert_not_tempdir_put tripwire
# (refuses a PUT under a tempfile/pytest tmp dir) must therefore NOT fire here;
# it exists to catch NON-pytest runners -- main()-style `python3 test_x.py` and
# the bash aggregator (run-asp-257-suite.sh) -- where THIS conftest never loads
# and get_backend() may return a REAL own-cloud backend that would collide on the
# production S3 key (rb-2983/guard-955). The presence of this env var IS the
# "am I inside a hermetic pytest session?" signal the tripwire keys off.
os.environ["MIND_ALLOW_TMP_OWNCLOUD_PUT"] = "1"

# Pre-import _paths to lock AGENT_DIR into the module cache before any test
# module pops MIND_AGENT. Without this, a test that pops the env BEFORE
# _paths is first imported caches AGENT_DIR=None for the whole session.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _paths  # noqa: E402, F401  — side-effect import to lock cache

# Also expose this directory (core/scripts/tests/) on sys.path so test
# modules can `from _bash_helpers import BASH` (, 2026-05-16).
# Six tests previously each duplicated a local `_resolve_bash` to dodge
# WSL bash on Windows; the helper is now a single module here. Pytest
# discovers conftest.py and runs this insert BEFORE any test module
# imports — ad-hoc `py -3 test_foo.py` invocations still need the test
# file to add SCRIPT_DIR to sys.path themselves (the refactored tests
# do this for symmetry with their existing CORE_SCRIPTS insert).
sys.path.insert(0, str(Path(__file__).resolve().parent))


# Snapshot the bootstrap env BEFORE any test module imports — used by the
# autouse fixture below to undo collection-time pollution. Sentinel _UNSET
# distinguishes "var was absent at bootstrap" from "var was empty string".
_UNSET = object()
_BOOTSTRAP_MIND_AGENT = os.environ.get("MIND_AGENT", _UNSET)
_BOOTSTRAP_MIND_WORLD = os.environ.get("MIND_WORLD", _UNSET)
_BOOTSTRAP_MIND_BACKEND = os.environ.get("STORAGE_BACKEND", _UNSET)
_BOOTSTRAP_ALLOW_TMP_PUT = os.environ.get("MIND_ALLOW_TMP_OWNCLOUD_PUT", _UNSET)


@pytest.fixture(autouse=True)
def _restore_env_per_test():
    """Restore MIND_AGENT and MIND_WORLD before each test.

    Polluter test modules pop or overwrite these at module load. Pytest
    collects all modules before running any test, so the pollution lands
    before the FIRST test runs — even tests that sort earlier
    alphabetically than the polluters. Restoring per-test undoes the
    collection-time damage without forcing 11+ polluter files to grow
    proper setUp/tearDown discipline.

    Tests that legitimately need a different env (e.g.
    test_paths_read_local_paths_fail_loud.py) override inside the test
    body and restore in their own tearDown — this fixture is compatible
    because it runs BEFORE the test starts.
    """
    if _BOOTSTRAP_MIND_AGENT is _UNSET:
        os.environ.pop("MIND_AGENT", None)
    else:
        os.environ["MIND_AGENT"] = _BOOTSTRAP_MIND_AGENT
    if _BOOTSTRAP_MIND_WORLD is _UNSET:
        os.environ.pop("MIND_WORLD", None)
    else:
        os.environ["MIND_WORLD"] = _BOOTSTRAP_MIND_WORLD
    # Keep the storage backend pinned local across tests that may mutate it
    # (lodestar-s7 test isolation — see the module-level set above).
    if _BOOTSTRAP_MIND_BACKEND is _UNSET:
        os.environ.pop("STORAGE_BACKEND", None)
    else:
        os.environ["STORAGE_BACKEND"] = _BOOTSTRAP_MIND_BACKEND
    # 5: keep the own-cloud tempdir-tripwire dormant-pin stable across
    # tests that mutate it (the tripwire's own regression test toggles it).
    if _BOOTSTRAP_ALLOW_TMP_PUT is _UNSET:
        os.environ.pop("MIND_ALLOW_TMP_OWNCLOUD_PUT", None)
    else:
        os.environ["MIND_ALLOW_TMP_OWNCLOUD_PUT"] = _BOOTSTRAP_ALLOW_TMP_PUT
    yield
