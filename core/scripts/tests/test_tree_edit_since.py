"""test_tree_edit_since.py — regression tests for tree-edit-since.py authorship
attribution (g-115-3245).

The detector answers "did I encode to the knowledge tree this iteration?" for
iteration-close.sh's TREE_UPDATED auto-promotion. Before g-115-3245 it answered
the weaker "did the tree change?", which in a multi-agent own-cloud fleet is
satisfied by a PARTNER's encoding syncing down mid-iteration — silently
disabling the encoding-drift counter while reporting green (g-115-3115).

Two contracts are pinned here:
  1. `_tree.yaml` alone never satisfies the detector. It is a shared index with
     no front matter, so it cannot carry authorship even in principle.
  2. A candidate .md must carry `session:` matching $MIND_SID, or carry no
     attribution at all (fail-open for the ~4% of legacy nodes without a
     session stamp).

COLLECTION-SAFETY: every case drives the script as a SUBPROCESS against a tmp
MIND_WORLD. No module-level env pins, no sys.path inserts, no imports of the
script under test — so pytest's shared-process collection cannot be poisoned
(the g-115-2487 / guard-1165 class). STORAGE_BACKEND=local is pinned per
subprocess per guard-955: the child does os.environ.copy() and would otherwise
inherit own-cloud, whose _s3_key ignores the MIND_WORLD tmp override.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "tree-edit-since.py"
SELF_SID = "1111aaaa-0000-4000-8000-000000000001"
PARTNER_SID = "2222bbbb-0000-4000-8000-000000000002"

CUTOFF = datetime(2026, 7, 26, 10, 0, 0)
CUTOFF_ISO = CUTOFF.isoformat()
AFTER = CUTOFF.timestamp() + 60.0
BEFORE = CUTOFF.timestamp() - 60.0


def _node(text: str) -> str:
    return text.lstrip("\n")


SELF_NODE = _node(
    """
---
topic: "a node this session encoded"
last_updated: '2026-07-26'
last_update_trigger:
  type: tree_growth
  session: %s
---

# Body
"""
    % SELF_SID
)

PARTNER_NODE = _node(
    """
---
topic: "a node a partner encoded, synced down mid-iteration"
last_updated: '2026-07-26'
last_update_trigger:
  type: tree_growth
  session: %s
---

# Body
"""
    % PARTNER_SID
)

UNSTAMPED_NODE = _node(
    """
---
topic: "a legacy node carrying no session attribution"
last_updated: '2026-07-26'
---

# Body
"""
)


def _make_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "knowledge" / "tree"
    (tree / "system").mkdir(parents=True)
    return tree


def _write(path: Path, text: str, mtime: float) -> None:
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))


def _run(tmp_path: Path, iso: str = CUTOFF_ISO, sid: str | None = SELF_SID):
    env = os.environ.copy()
    env["MIND_WORLD"] = str(tmp_path)
    env["STORAGE_BACKEND"] = "local"
    if sid is None:
        env.pop("MIND_SID", None)
    else:
        env["MIND_SID"] = sid
    return subprocess.run(
        [sys.executable, str(SCRIPT), iso],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_tree_yaml_alone_does_not_satisfy(tmp_path):
    """EDIT 1 — the shared index is not evidence of THIS agent's encoding.

    This is the exact g-115-3115 shape: the index moves (sync, or any agent's
    write) while no attributable node did.
    """
    tree = _make_tree(tmp_path)
    _write(tree / "_tree.yaml", "root: {}\n", AFTER)
    res = _run(tmp_path)
    assert res.returncode == 1, (
        "_tree.yaml modified after the cutoff must NOT report an encoding; "
        f"got rc={res.returncode} stdout={res.stdout!r}"
    )


def test_self_authored_node_detected(tmp_path):
    tree = _make_tree(tmp_path)
    _write(tree / "system" / "mine.md", SELF_NODE, AFTER)
    res = _run(tmp_path)
    assert res.returncode == 0, f"stdout={res.stdout!r} stderr={res.stderr!r}"
    assert "mine.md" in res.stdout


def test_partner_authored_node_not_detected(tmp_path):
    """EDIT 2 — a partner encoding that synced down must not satisfy the gate."""
    tree = _make_tree(tmp_path)
    _write(tree / "system" / "theirs.md", PARTNER_NODE, AFTER)
    res = _run(tmp_path)
    assert res.returncode == 1, (
        "a node stamped with another session must not count as this agent's "
        f"encoding; got rc={res.returncode} stdout={res.stdout!r}"
    )
    assert "another session" in res.stderr


def test_partner_node_does_not_mask_self_node(tmp_path):
    """Both present — the self-authored node still wins.

    Guards the scan order: the old first-hit short-circuit would exit on
    whichever file rglob yielded first.
    """
    tree = _make_tree(tmp_path)
    _write(tree / "system" / "aaa-theirs.md", PARTNER_NODE, AFTER)
    _write(tree / "system" / "zzz-mine.md", SELF_NODE, AFTER)
    res = _run(tmp_path)
    assert res.returncode == 0, f"stdout={res.stdout!r} stderr={res.stderr!r}"
    assert "zzz-mine.md" in res.stdout


def test_unstamped_node_fails_open(tmp_path):
    """The ~4% of nodes with no session stamp keep the  behavior."""
    tree = _make_tree(tmp_path)
    _write(tree / "system" / "legacy.md", UNSTAMPED_NODE, AFTER)
    res = _run(tmp_path)
    assert res.returncode == 0, f"stdout={res.stdout!r} stderr={res.stderr!r}"


def test_unset_sid_falls_back_to_any_edit(tmp_path):
    """No binding => authorship is unknowable; do not report "never encoded"."""
    tree = _make_tree(tmp_path)
    _write(tree / "system" / "theirs.md", PARTNER_NODE, AFTER)
    res = _run(tmp_path, sid=None)
    assert res.returncode == 0, f"stdout={res.stdout!r} stderr={res.stderr!r}"


def test_node_older_than_cutoff_not_detected(tmp_path):
    tree = _make_tree(tmp_path)
    _write(tree / "system" / "mine.md", SELF_NODE, BEFORE)
    res = _run(tmp_path)
    assert res.returncode == 1


@pytest.mark.parametrize("bad", ["not-a-timestamp", ""])
def test_bad_timestamp_fails_open(tmp_path, bad):
    tree = _make_tree(tmp_path)
    _write(tree / "system" / "mine.md", SELF_NODE, AFTER)
    res = _run(tmp_path, iso=bad)
    assert res.returncode == 1, f"stdout={res.stdout!r} stderr={res.stderr!r}"


def test_missing_tree_dir_fails_open(tmp_path):
    res = _run(tmp_path)
    assert res.returncode == 1
    assert "no tree dir" in res.stderr
