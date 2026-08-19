""": conftest must CLEAR MIND_WORLD/MIND_META, above the _paths pre-import.

WHY EVERY TEST HERE SPAWNS A CHILD (guard-2337): conftest captures the bootstrap
env at IMPORT time, and by the time any test in this session runs, that capture
has already happened. Setting os.environ inside a test body therefore proves
nothing about the behaviour under test -- the only way to observe an import-time
decision is to plant the value in a child process's environment and import
conftest fresh there.

Two distinct properties are pinned, and they fail independently:

  * the ENV is cleared            -- kills "no pop at all"
  * _paths.WORLD_DIR is unpolluted -- kills "pop placed below the _paths
                                      pre-import", which leaves the shell value
                                      baked into the module cache while the env
                                      itself looks clean

Mutation-checked: deleting the pop fails both; moving it below `import _paths`
fails only the second. A single combined assertion would have missed the latter,
which is the defect the placement comment in conftest exists to prevent.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent

FOREIGN_WORLD = "/tmp/g1154545-foreign-world"
FOREIGN_META = "/tmp/g1154545-foreign-meta"

# Imports conftest exactly as pytest would, then reports what survived.
_PROBE = """
import json, os, sys
sys.path.insert(0, {tests_dir!r})
import conftest
import _paths
print(json.dumps({{
    "world_in_env": "MIND_WORLD" in os.environ,
    "meta_in_env": "MIND_META" in os.environ,
    "world_value": os.environ.get("MIND_WORLD"),
    "meta_value": os.environ.get("MIND_META"),
    "bootstrap_world_unset": conftest._BOOTSTRAP_MIND_WORLD is conftest._UNSET,
    "bootstrap_meta_unset": conftest._BOOTSTRAP_MIND_META is conftest._UNSET,
    "paths_world_dir": str(_paths.WORLD_DIR),
    "paths_meta_dir": str(_paths.META_DIR),
}}))
"""


def _probe(extra_env, drop=()):
    """Import conftest in a child whose env carries `extra_env` minus `drop`."""
    env = dict(os.environ)
    env.update(extra_env)
    for key in drop:
        env.pop(key, None)
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE.format(tests_dir=str(TESTS_DIR))],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(TESTS_DIR.parents[1]),
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"probe child failed rc={proc.returncode}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def polluted():
    """A child that was launched with BOTH vars pointing somewhere foreign."""
    return _probe({"MIND_WORLD": FOREIGN_WORLD, "MIND_META": FOREIGN_META})


def test_the_probe_can_actually_see_a_planted_value():
    """Positive control: without conftest, the planted value IS visible.

    Without this, every assertion below would pass just as well against a child
    whose env was never populated -- a green suite proving nothing (guard-2421).
    """
    proc = subprocess.run(
        [sys.executable, "-c", "import os; print(os.environ.get('MIND_WORLD'))"],
        capture_output=True,
        text=True,
        env={**os.environ, "MIND_WORLD": FOREIGN_WORLD},
        timeout=60,
    )
    assert proc.stdout.strip() == FOREIGN_WORLD, (
        "the planting mechanism itself does not work, so nothing below is a "
        "measurement of conftest"
    )


def test_world_is_cleared_from_a_polluted_child_env(polluted):
    assert polluted["world_in_env"] is False, (
        f"MIND_WORLD survived conftest import as {polluted['world_value']!r}"
    )


def test_meta_is_cleared_from_a_polluted_child_env(polluted):
    assert polluted["meta_in_env"] is False, (
        f"MIND_META survived conftest import as {polluted['meta_value']!r}"
    )


def test_clear_runs_above_the_snapshot(polluted):
    """The snapshot must capture _UNSET, not the foreign value.

    This is the half that makes the autouse restore correct: if the snapshot
    captured the shell's value, the fixture would faithfully RE-ASSERT it before
    every single test -- the original defect.
    """
    assert polluted["bootstrap_world_unset"] is True
    assert polluted["bootstrap_meta_unset"] is True


def test_clear_runs_above_the_paths_pre_import(polluted):
    """The stricter placement claim: _paths must not have cached the foreign root.

    conftest pre-imports _paths (to lock AGENT_DIR), and _paths computes
    WORLD_DIR/META_DIR as module-level constants. A pop placed merely next to the
    snapshot -- which sits BELOW that pre-import -- would clear the env while
    leaving the foreign value baked into the module cache for the whole session.
    """
    assert polluted["paths_world_dir"] != FOREIGN_WORLD, (
        "_paths.WORLD_DIR cached the foreign world: the clear is below the "
        "_paths pre-import"
    )
    assert polluted["paths_meta_dir"] != FOREIGN_META, (
        "_paths.META_DIR cached the foreign meta: the clear is below the "
        "_paths pre-import"
    )


def test_resolution_is_identical_with_and_without_a_polluted_launcher(polluted):
    """The determinism property, stated as an equality rather than asserted.

    This is the actual deliverable: where the suite resolves its world must not
    depend on how the launching shell was built. Compared against a child
    launched with both vars explicitly absent.
    """
    clean = _probe({}, drop=("MIND_WORLD", "MIND_META"))
    assert polluted["paths_world_dir"] == clean["paths_world_dir"]
    assert polluted["paths_meta_dir"] == clean["paths_meta_dir"]
