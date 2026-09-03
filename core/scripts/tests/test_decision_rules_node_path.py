"""decision-rules-append / -increment must resolve `--node-path` for an external world.

Measured 2026-09-03 on a downstream deployment whose world lives outside the
repo: the reducer's state-update called decision-rules-append.sh with the bare
tree-relative form `<category>/<node>.md`, the script joined it to PROJECT_ROOT,
and ~10 minutes of the phase went to discovering that only an absolute path
worked. The virtual `world/knowledge/tree/...` form failed the same way. Both
scripts now resolve through tree.resolve_node_path — normalize_virtual_path (the
canonicalization point) then resolve_file_path — while absolute and existing
repo-relative paths keep their old meaning.

The subprocess carries MIND_WORLD so WORLD_DIR is the tmp world; conftest pops
that variable from the pytest process only, not from an env we pass explicitly.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
APPEND = SCRIPTS / "decision-rules-append.py"
INCREMENT = SCRIPTS / "decision-rules-increment.py"

NODE = """---
topic: "Test Node"
last_updated: '2026-08-01'
---

# Test Node

Body text.

## Decision Rules

- IF a probe returns zero THEN positive-control it before believing it — applied: 1 (2026-08-01)
"""

RULE = '{"if":"a second probe returns zero","then":"positive-control that one too"}'


def _world(td: Path) -> Path:
    node = td / "world" / "knowledge" / "tree" / "system" / "testnode" / "node.md"
    node.parent.mkdir(parents=True, exist_ok=True)
    node.write_text(NODE, encoding="utf-8")
    (td / "meta").mkdir(exist_ok=True)
    return node


def _run(script: Path, td: Path, node_arg: str, stdin: str, extra=()):
    env = dict(os.environ)
    env.update({"MIND_WORLD": str(td / "world"), "MIND_META": str(td / "meta"),
                "MIND_AGENT": "alpha", "MIND_SID": "caeb1579-54b2-4fdc-b99f-fd23b4ebbba2",
                "STORAGE_BACKEND": "local"})  # guard-955: never own-cloud in a test
    goal = ["--goal", "g-000-01"] if script is APPEND else []   # increment takes no --goal
    return subprocess.run(
        [sys.executable, str(script), *goal, "--node-path", node_arg, *extra],
        input=stdin, capture_output=True, text=True, timeout=60, env=env,
    )


@pytest.mark.parametrize("form", ["absolute", "virtual", "bare"])
def test_append_resolves_every_node_path_form(form):
    with tempfile.TemporaryDirectory() as td_s:
        td = Path(td_s)
        node = _world(td)
        arg = {"absolute": str(node),
               "virtual": "world/knowledge/tree/system/testnode/node.md",
               "bare": "system/testnode/node.md"}[form]
        r = _run(APPEND, td, arg, RULE)
        assert r.returncode == 0, (form, r.stderr)
        assert "appended=1" in r.stdout, (form, r.stdout)
        assert "a second probe returns zero" in node.read_text(encoding="utf-8")


def test_increment_resolves_the_bare_form_too():
    with tempfile.TemporaryDirectory() as td_s:
        td = Path(td_s)
        node = _world(td)
        r = _run(INCREMENT, td, "system/testnode/node.md",
                 '{"rules": ["IF a probe returns zero THEN positive-control it before believing it"]}')
        assert r.returncode == 0, r.stderr
        assert "applied: 2" in node.read_text(encoding="utf-8"), node.read_text(encoding="utf-8")


def test_missing_node_names_both_the_resolved_and_the_given_path():
    with tempfile.TemporaryDirectory() as td_s:
        td = Path(td_s)
        _world(td)
        r = _run(APPEND, td, "system/nope/missing.md", RULE)
        assert r.returncode == 1
        assert "does not exist" in r.stderr
        assert "system/nope/missing.md" in r.stderr           # the given form
        assert str(td / "world" / "knowledge" / "tree") in r.stderr  # where it looked
