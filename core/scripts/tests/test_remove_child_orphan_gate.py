"""test_remove_child_orphan_gate.py — regression test for the
subtree-orphaning gate added 2026-05-07 (fix #8 from the tree audit).

Pre-fix, `tree.py --remove-child <parent> <child>` silently deleted the
child even when it had grandchildren. The grandchildren's `parent` field
still pointed at the deleted key, making them unreachable from root,
invisible to retrieval, and flagged as orphans by validate_tree. The
audit (2026-05-07) found this was the cause of two stale subtrees in the
live alpha tree. [UNVERIFIED -- inherited from this file's pre-g-115-11522
docstring; no record in this world corroborates the specific 'two stale
subtrees' count, so the figure is carried un-sourced]

Post-fix, the operation refuses with exit code 2 and a message naming
the descendants. Callers must remove descendants depth-first.

This test exercises both:
  1. cmd_remove_child (single-op CLI path)
  2. cmd_batch's remove-child branch (atomic batch path)

Plus the g-115-11522 hermeticity contract for the post-remove sweep that
cmd_remove_child fires automatically (_post_remove_sweep_dangling):
  3. the sweep child must receive an EXPLICIT env naming tree.py's own
     WORLD_DIR (never the caller's ambient world),
  4. with the ambient world re-pointed at a second fixture world B that
     carries a dangling ref, the sweep must leave B byte-untouched and
     write no B repair-ledger row (the pre-fix shape nulled B's ref), and
  5. the live world's stores must be byte-unchanged across the whole run
     with no new live repair-ledger row (the pre-fix child, spawned with
     no env, resolved the live world from the ambient).
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
from io import StringIO
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

#  capture-restore pattern: stash env before module-level mutation.
# : the MIND_WORLD override is NOT restored after the import —
# it is HELD for the whole run (this file is pytest-invisible; it runs
# standalone, one process, and exits after main()). The old restore-after-
# import was the leak: it re-pointed the ambient world at the LIVE world
# before the scenarios ran, so the sweep child that tree.py spawned without
# an explicit env resolved the live world and ran `--apply` against it.
# Holding the override means no code path in this process — including a
# pre-fix sweep child — can reach the live world from the ambient.
# (Under a pytest session, conftest's autouse _restore_env_per_test undoes
# the held override before every other test, so there is no cross-test leak.)
_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")

# : capture the CALLER's ambient world BEFORE the override below,
# through _paths' OWN resolution (env > .mind-data > conf), so the live world
# is right on every box shape: the invisible-half runner exports the live
# MIND_WORLD; a bare run resolves .mind-data/world (or a legacy conf world).
# Scenario 6 asserts the live world's stores are byte-unchanged across the
# whole run.
import _paths as _paths_boot  # noqa: E402
_LIVE_WORLD = _paths_boot.WORLD_DIR

_TMP = tempfile.mkdtemp(prefix="rmchild-orphan-test-")
os.environ["MIND_WORLD"] = _TMP
os.environ.pop("MIND_AGENT", None)
# _paths is now cached with the LIVE world (imported above). The original
# test imported tree.py FIRST, so _paths' fresh import ran AFTER this
# override and resolved WORLD_DIR from MIND_WORLD=_TMP. Reproduce that
# exactly: re-point the cached constant so every downstream
# `from _paths import WORLD_DIR` (tree.py and its imports) sees the fixture
# world. META_DIR needs no patch: it resolves independently of MIND_WORLD
# (conf / .mind-data) and is identical before and after the override.
_paths_boot.WORLD_DIR = Path(_TMP)

_TREE_PATH = CORE_SCRIPTS / "tree.py"
_spec = importlib.util.spec_from_file_location("tree_mod", _TREE_PATH)
_tree_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tree_mod)

# NOTE: MIND_WORLD is deliberately NOT restored here. Scenario 5 temporarily
# re-points it at world B and restores _TMP afterwards. MIND_AGENT is
# restored so downstream helpers (wm, agent resolution) keep working inside
# this process.
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT


def _seed_tree() -> Path:
    tree_dir = Path(_TMP) / "knowledge" / "tree"
    tree_dir.mkdir(parents=True, exist_ok=True)
    tree = {
        "last_updated": "2026-05-07",
        "tree_growth_log": [],
        "nodes": {
            "root": {"file": None, "depth": 0, "parent": None,
                     "children": ["mid"], "child_count": 1, "summary": "root"},
            "mid": {
                "file": "world/knowledge/tree/mid.md",
                "depth": 1, "parent": "root",
                "children": ["leaf-a", "leaf-b"], "child_count": 2,
                "summary": "mid",
            },
            "leaf-a": {
                "file": "world/knowledge/tree/mid/leaf-a.md",
                "depth": 2, "parent": "mid", "children": [], "child_count": 0,
                "summary": "leaf a",
            },
            "leaf-b": {
                "file": "world/knowledge/tree/mid/leaf-b.md",
                "depth": 2, "parent": "mid", "children": [], "child_count": 0,
                "summary": "leaf b",
            },
        },
    }
    path = tree_dir / "_tree.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(tree, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    return path


class _Args:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _read_tree(tree_path: Path) -> dict:
    with open(tree_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _scenario_refuse_with_descendants(tree_path: Path) -> bool:
    """Removing 'mid' with leaf-a + leaf-b as descendants must be refused."""
    saved_stderr = sys.stderr
    sys.stderr = capture = StringIO()
    rc_seen = None
    try:
        _tree_mod.cmd_remove_child(_Args(remove_child=["root", "mid"]))
    except SystemExit as e:
        rc_seen = e.code
    finally:
        sys.stderr = saved_stderr

    err = capture.getvalue()
    if rc_seen != 2:
        print(f"FAIL[scenario 1]: expected SystemExit(2), got rc={rc_seen}",
              file=sys.stderr)
        return False
    if "leaf-a" not in err or "leaf-b" not in err:
        print(f"FAIL[scenario 1]: error message should name descendants; "
              f"got: {err}", file=sys.stderr)
        return False

    # Verify nothing changed on disk
    tree = _read_tree(tree_path)
    if "mid" not in tree["nodes"]:
        print("FAIL[scenario 1]: 'mid' was deleted despite the refusal",
              file=sys.stderr)
        return False
    if "mid" not in tree["nodes"]["root"]["children"]:
        print("FAIL[scenario 1]: 'mid' was unlinked from root despite refusal",
              file=sys.stderr)
        return False
    return True


def _scenario_allow_leaf_removal(tree_path: Path) -> bool:
    """Removing a leaf (no descendants) must succeed."""
    saved_stdout = sys.stdout
    sys.stdout = capture = StringIO()
    rc_seen = None
    try:
        _tree_mod.cmd_remove_child(_Args(remove_child=["mid", "leaf-a"]))
    except SystemExit as e:
        rc_seen = e.code
    finally:
        sys.stdout = saved_stdout

    if rc_seen is not None and rc_seen != 0:
        print(f"FAIL[scenario 2]: leaf removal exited rc={rc_seen}, "
              f"stdout={capture.getvalue()}", file=sys.stderr)
        return False
    out = capture.getvalue()
    try:
        result = json.loads(out)
    except json.JSONDecodeError:
        print(f"FAIL[scenario 2]: leaf removal stdout not JSON: {out}",
              file=sys.stderr)
        return False
    if result.get("removed") != "leaf-a":
        print(f"FAIL[scenario 2]: removed field should be 'leaf-a', got {result}",
              file=sys.stderr)
        return False

    tree = _read_tree(tree_path)
    if "leaf-a" in tree["nodes"]:
        print("FAIL[scenario 2]: leaf-a still in nodes after successful remove",
              file=sys.stderr)
        return False
    if "leaf-a" in tree["nodes"]["mid"]["children"]:
        print("FAIL[scenario 2]: leaf-a still in mid's children after successful remove",
              file=sys.stderr)
        return False
    return True


def _scenario_batch_refuses(tree_path: Path) -> bool:
    """cmd_batch's remove-child must apply the same gate.
    State on entry: root → mid → [leaf-b] (leaf-a removed in scenario 2).
    Submit a batch trying to remove 'mid' (still has leaf-b as descendant).

    In-process call (g-115-2353, 2026-07-16): the prior form piped the batch
    through `tree-update.sh --batch` in a subprocess with MIND_WORLD=_TMP,
    but that wrapper is daemon-routed — the daemon resolves the PRODUCTION
    world from the bound agent's local-paths.conf per-request context and
    ignores the caller's env override, so the batch ran against the live
    tree (no root→mid there) and returned rc=0 while the gate itself was
    intact. Calling cmd_batch in-process (like scenarios 1-2) tests the
    gate, not the transport. cmd_batch reads its payload from stdin."""
    batch_payload = {
        "operations": [
            {"op": "remove-child", "key": "root", "child_key": "mid"},
        ],
    }
    saved_stdin = sys.stdin
    saved_stderr = sys.stderr
    sys.stdin = StringIO(json.dumps(batch_payload))
    sys.stderr = capture = StringIO()
    rc_seen = None
    try:
        _tree_mod.cmd_batch(_Args(batch=True))
    except SystemExit as e:
        rc_seen = e.code
    finally:
        sys.stdin = saved_stdin
        sys.stderr = saved_stderr

    err = capture.getvalue()
    if rc_seen != 2:
        print(f"FAIL[scenario 3]: batch should exit 2, got rc={rc_seen}",
              file=sys.stderr)
        print(f"stderr={err}", file=sys.stderr)
        return False
    if "leaf-b" not in err:
        print(f"FAIL[scenario 3]: batch stderr should name descendants; "
              f"got: {err}", file=sys.stderr)
        return False

    # Verify nothing changed on disk (atomic refuse)
    tree = _read_tree(tree_path)
    if "mid" not in tree["nodes"]:
        print("FAIL[scenario 3]: batch refuse should be atomic; "
              "'mid' was deleted", file=sys.stderr)
        return False
    return True

# --- : the post-remove sweep must be hermetic -------------------
#
# cmd_remove_child fires _post_remove_sweep_dangling, which spawns
# learning-routing-repair.py --apply. The child re-resolves _paths.WORLD_DIR
# from MIND_WORLD at ITS OWN import, so an env-less spawn resolves whatever
# world the CALLER's ambient happens to name. Measured 2026-10-01: with the
# ambient re-pointed at a fixture world B, the pre-fix child nulled B's
# dangling ref and journaled it under B/.history; with the ambient at the
# live world (this file's own old module-level restore shape), the same
# child ran --apply against the live stores. Scenarios 4-6 pin the contract
# that makes the sweep hermetic: the child must receive tree.py's own world
# explicitly, and the live world must stay byte-untouched across the run.


def _hashes(*paths) -> dict:
    import hashlib
    out = {}
    for p in paths:
        p = Path(p)
        out[str(p)] = (hashlib.md5(p.read_bytes()).hexdigest()
                       if p.exists() else "<absent>")
    return out


def _world_stores(world: Path) -> list:
    return [world / n for n in
            ("reasoning-bank.jsonl", "guardrails.jsonl", "pipeline.jsonl")]


def _ledger_rows(world: Path) -> list:
    rows = []
    hist = world / ".history"
    for p in sorted(hist.glob("learning-routing-repair-*.jsonl")) if hist.exists() else []:
        rows += [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return rows


def _seed_two_worlds(base: Path):
    """World A = a clean tree-write world (its single rb ref RESOLVES, so a
    sweep against A writes nothing). World B = the AMBIENT world, carrying a
    dangling reasoning-bank ref that `--apply` would null if it ever ran
    against B."""
    a, b = base / "worldA", base / "worldB"
    (a / "knowledge" / "tree").mkdir(parents=True)
    with open(a / "knowledge" / "tree" / "_tree.yaml", "w", encoding="utf-8") as f:
        yaml.dump({"last_updated": "2026-05-07", "tree_growth_log": [],
                   "nodes": {
                       "root": {"file": None, "depth": 0, "parent": None,
                                "children": ["leaf"], "child_count": 1,
                                "summary": "root"},
                       "leaf": {"file": "world/knowledge/tree/leaf.md",
                                "depth": 1, "parent": "root",
                                "children": [], "child_count": 0,
                                "summary": "leaf"}}}, f)
    (a / "pipeline.jsonl").write_text(
        json.dumps({"id": "2026-01-01_real-hyp", "status": "active"}) + "\n",
        encoding="utf-8")
    (a / "guardrails.jsonl").write_text("", encoding="utf-8")
    (a / "reasoning-bank.jsonl").write_text(
        json.dumps({"id": "rb-a-1", "status": "active",
                    "source_hypothesis": "2026-01-01_real-hyp"}) + "\n",
        encoding="utf-8")
    b.mkdir(parents=True)
    (b / "pipeline.jsonl").write_text(
        json.dumps({"id": "2026-01-01_real-hyp", "status": "active"}) + "\n",
        encoding="utf-8")
    (b / "guardrails.jsonl").write_text("", encoding="utf-8")
    (b / "reasoning-bank.jsonl").write_text(
        json.dumps({"id": "rb-b-1", "status": "active",
                    "source_hypothesis": "2026-02-02_missing-hyp"}) + "\n",
        encoding="utf-8")
    return a, b


def _scenario_sweep_env_is_explicit() -> bool:
    """Scenario 4: the sweep child must be spawned with an EXPLICIT env whose
    MIND_WORLD names tree.py's own WORLD_DIR — not the ambient.

    Captures the call by interposing on subprocess.run and letting the real
    child run (A is clean, so the real run writes nothing). Pre-fix, no env
    was passed at all (the child inherited the ambient) — that shape is the
    defect, and this asserts against it directly."""
    import subprocess
    captured = {}
    real_run = subprocess.run

    def _capture(*a, **kw):
        captured["env"] = kw.get("env")
        return real_run(*a, **kw)

    subprocess.run = _capture
    try:
        _tree_mod._post_remove_sweep_dangling(["leaf-b"])
    finally:
        subprocess.run = real_run

    env = captured.get("env")
    if not isinstance(env, dict):
        print("FAIL[scenario 4]: sweep child spawned with no explicit env "
              f"(inherits the caller's ambient world); got {env!r}",
              file=sys.stderr)
        return False
    if env.get("MIND_WORLD") != str(_tree_mod.WORLD_DIR):
        print(f"FAIL[scenario 4]: sweep env MIND_WORLD={env.get('MIND_WORLD')!r} "
              f"!= tree.py's own WORLD_DIR={_tree_mod.WORLD_DIR!r}",
              file=sys.stderr)
        return False
    return True


def _scenario_two_worlds_sweep_leaves_ambient_untouched() -> bool:
    """Scenario 5: ambient world B carries a dangling ref; the tree write
    happens in a second world A. The sweep must not write into B: all three
    of B's stores stay byte-identical and B gains no repair-ledger row.

    This is the RED the goal names: pre-fix, the env-less child resolved B
    from the ambient, nulled B's rb-b-1.source_hypothesis, and journaled it
    under B/.history (measured 2026-10-01, probe-g11511522-prefix.log)."""
    import subprocess
    base = Path(tempfile.mkdtemp(prefix="rmchild-two-worlds-"))
    try:
        a, b = _seed_two_worlds(base)
        b_hashes_before = _hashes(*_world_stores(b))
        b_ledger_before = _ledger_rows(b)

        # The tree write targets world A: point _tree_path at A's tree.
        saved_tree_path = _tree_mod.TREE_PATH
        try:
            _tree_mod.TREE_PATH = str(a / "knowledge" / "tree" / "_tree.yaml")
            # The child inherits os.environ; re-point the ambient at B for
            # the duration of the call, exactly the shape the old leak had.
            saved_ambient = os.environ.get("MIND_WORLD")
            os.environ["MIND_WORLD"] = str(b)
            saved_out, saved_err = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = StringIO(), StringIO()
            rc_seen = None
            try:
                _tree_mod.cmd_remove_child(_Args(remove_child=["root", "leaf"]))
            except SystemExit as e:
                rc_seen = e.code
            finally:
                out_txt, err_txt = sys.stdout.getvalue(), sys.stderr.getvalue()
                sys.stdout, sys.stderr = saved_out, saved_err
                if saved_ambient is not None:
                    os.environ["MIND_WORLD"] = saved_ambient
                elif "MIND_WORLD" in os.environ:
                    os.environ["MIND_WORLD"] = _TMP
        finally:
            _tree_mod.TREE_PATH = saved_tree_path

        if rc_seen not in (0, None):
            print(f"FAIL[scenario 5]: leaf removal in world A exited rc={rc_seen}, "
                  f"stdout={out_txt!r} stderr={err_txt!r}", file=sys.stderr)
            return False
        # The removal must have happened in A (proves the tree write itself
        # was not misrouted to B).
        with open(a / "knowledge" / "tree" / "_tree.yaml", encoding="utf-8") as f:
            if "leaf" in yaml.safe_load(f)["nodes"]:
                print("FAIL[scenario 5]: leaf was not removed from world A's tree "
                      "(tree write misrouted?)", file=sys.stderr)
                return False
        b_hashes_after = _hashes(*_world_stores(b))
        b_ledger_after = _ledger_rows(b)
        if b_hashes_before != b_hashes_after:
            print("FAIL[scenario 5]: sweep WROTE into ambient world B:\n"
                  f"  before={b_hashes_before}\n  after={b_hashes_after}",
                  file=sys.stderr)
            return False
        if b_ledger_after != b_ledger_before:
            print(f"FAIL[scenario 5]: sweep wrote a B repair-ledger row: "
                  f"{(b_ledger_after - b_ledger_before)[:1]!r}", file=sys.stderr)
            return False
        return True
    finally:
        shutil.rmtree(base, ignore_errors=True)


def main() -> int:
    tree_path = _seed_tree()
    if Path(_tree_mod.TREE_PATH) != tree_path:
        print(f"FAIL: tree-mod TREE_PATH = {_tree_mod.TREE_PATH}, "
              f"expected {tree_path}", file=sys.stderr)
        return 1

    # Scenario 6 baseline: the live world (_LIVE_WORLD, captured at module
    # top BEFORE this file's override) must be byte-unchanged across the
    # whole run. The pre-fix child — spawned with no env from a process
    # whose ambient had been restored to the live world (this file's old
    # module-level shape) — resolved the live world and ran --apply against
    # it. Baselining at main() ENTRY (not at first scenario) means a write
    # anywhere in the run — including scenario 5's ambient re-point — is
    # caught even if the env-capture scenario (4) were to pass by accident.
    live_ok = _LIVE_WORLD is not None and _LIVE_WORLD.is_dir()
    if live_ok:
        live_baseline_hashes = _hashes(*_world_stores(_LIVE_WORLD))
        live_baseline_rows = _ledger_rows(_LIVE_WORLD)

    if not _scenario_refuse_with_descendants(tree_path):
        return 1
    if not _scenario_allow_leaf_removal(tree_path):
        return 1
    if not _scenario_batch_refuses(tree_path):
        return 1
    if not _scenario_sweep_env_is_explicit():
        return 1
    if not _scenario_two_worlds_sweep_leaves_ambient_untouched():
        return 1

    # Scenario 6: the live world, compared against the entry baseline.
    if live_ok:
        after_hashes = _hashes(*_world_stores(_LIVE_WORLD))
        after_rows = _ledger_rows(_LIVE_WORLD)
        if after_hashes != live_baseline_hashes:
            print("FAIL[scenario 6]: the run WROTE the live world's stores:\n"
                  f"  before={live_baseline_hashes}\n  after={after_hashes}",
                  file=sys.stderr)
            return 1
        new_rows = [r for r in after_rows if r not in live_baseline_rows]
        if new_rows:
            print(f"FAIL[scenario 6]: the run added live repair-ledger rows: "
                  f"{new_rows[:1]!r}", file=sys.stderr)
            return 1

    print("PASS: cmd_remove_child refuses subtree orphaning; leaf removal "
          "still works; cmd_batch's remove-child applies the same gate atomically; "
          "post-remove sweep gets an explicit env (scenario 4), leaves an "
          "ambient world byte-untouched (scenario 5), and the live world stays "
          "byte-untouched across the run (scenario 6).")
    return 0


if __name__ == "__main__":
    rc = main()
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(rc)
