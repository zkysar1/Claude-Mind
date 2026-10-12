"""test_tree_last_updated_drift_check.py — .

Regression tests for core/scripts/tree-last-updated-drift-check.py, which
audits (and with --apply backfills) the _tree.yaml per-node `last_updated`
index against each node .md front-matter `last_updated` (the single source of
truth, g-001-67).

  - --audit classifies synced / index_ahead / index_stale
  - --apply backfills index last_updated = node .md fm for index-STALE nodes
    (the forward, durable direction) and REPORTS index-AHEAD nodes without
    writing them (g-115-5509: last_updated is a NEWER-wins merge field, so a
    backward write cannot survive a peer merge; on a stale mirror it can
    persist and corrupt a correct index)
  - --exit-on-ahead returns 2 ONLY when index_ahead > 0 (the dangerous,
    over-reports-freshness direction); index_stale alone exits 0
  - missing/unparseable front matter is skipped, never crashes

Subprocess-based so it exercises the real argparse + _paths MIND_WORLD
resolution the recurring Layer-D goal will use, not in-process calls.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
DRIFT_PY = CORE_SCRIPTS / "tree-last-updated-drift-check.py"
PYTHON = sys.executable


def _setup_world(tmp, node_specs):
    """node_specs: {key: (index_last_updated, fm_last_updated)}.

    Writes _tree.yaml (index) + one node .md per key. fm=None writes a .md with
    NO front matter (exercises the skip path).
    """
    import yaml
    world = tmp / "world"
    tree_dir = world / "knowledge" / "tree"
    tree_dir.mkdir(parents=True, exist_ok=True)
    nodes = {}
    for key, (idx, fm) in node_specs.items():
        rel = f"knowledge/tree/{key}.md"
        nodes[key] = {"file": f"world/{rel}", "depth": 1, "last_updated": idx}
        md = world / rel
        md.parent.mkdir(parents=True, exist_ok=True)
        if fm is None:
            md.write_text("no front matter here\n", encoding="utf-8")
        else:
            md.write_text(
                f"---\nlast_updated: '{fm}'\nsummary: x\n---\n# {key}\n",
                encoding="utf-8")
    (tree_dir / "_tree.yaml").write_text(yaml.dump({"nodes": nodes}),
                                         encoding="utf-8")
    return world


def _run(world, args):
    env = dict(os.environ)
    env["MIND_WORLD"] = str(world)
    env.pop("MIND_AGENT", None)
    return subprocess.run([PYTHON, str(DRIFT_PY)] + args,
                          capture_output=True, text=True, env=env, timeout=30)


def test_audit_classifies_all_three_directions():
    tmp = Path(tempfile.mkdtemp(prefix="drift-audit-"))
    _setup_world(tmp, {
        "synced-node": ("2026-03-01", "2026-03-01"),
        "ahead-node": ("2026-06-01", "2026-01-01"),   # index ahead of fm
        "stale-node": ("2026-01-01", "2026-05-01"),   # index behind fm
    })
    r = _run(tmp / "world", [])  # default = audit
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["synced"] == 1
    assert d["index_ahead"] == 1
    assert d["index_stale"] == 1
    assert d["desynced"] == 2


def test_apply_backfills_index_stale_not_ahead():
    """ contract: --apply writes ONLY the forward (index_stale)
    direction; the index_ahead node is reported, never written. (Replaces the
    g-115-1683 test_apply_backfills_index_to_fm, whose two-direction write was
    the defect: the backward write silently no-ops against the NEWER-wins
    merge on a healthy peer, and PERSISTS on a stale mirror.)"""
    import yaml
    tmp = Path(tempfile.mkdtemp(prefix="drift-apply-"))
    world = _setup_world(tmp, {
        "ahead-node": ("2026-06-01", "2026-01-01"),
        "stale-node": ("2026-01-01", "2026-05-01"),
        "synced-node": ("2026-03-01", "2026-03-01"),
    })
    r = _run(world, ["--apply"])
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["backfilled"] == 1          # the stale node only
    assert d["index_ahead_not_written"] == [
        {"key": "ahead-node", "idx": "2026-06-01", "fm": "2026-01-01"}]

    # On-disk: the stale index moved forward to fm; the ahead index is
    # UNTOUCHED (a backward write must not land); synced untouched.
    tree = yaml.safe_load(
        (world / "knowledge/tree/_tree.yaml").read_text(encoding="utf-8"))
    assert str(tree["nodes"]["ahead-node"]["last_updated"])[:10] == "2026-06-01"
    assert str(tree["nodes"]["stale-node"]["last_updated"])[:10] == "2026-05-01"
    assert str(tree["nodes"]["synced-node"]["last_updated"])[:10] == "2026-03-01"

    # The loud line names the row, the merge class, and the evidence trail.
    assert "index_ahead NOT WRITTEN: ahead-node" in r.stderr
    assert "_TREE_NEWER_FIELDS" in r.stderr

    # Re-audit -> only the reported ahead row remains (by design: it is
    # reconciled separately, not by this script's write path).
    r2 = _run(world, [])
    d2 = json.loads(r2.stdout)
    assert d2["desynced"] == 1
    assert d2["index_ahead"] == 1
    assert d2["index_stale"] == 0
    assert d2["synced"] == 2


def test_exit_on_ahead_fires_only_on_index_ahead():
    # index_stale alone is NOT the dangerous direction -> exit 0.
    tmp = Path(tempfile.mkdtemp(prefix="drift-exit-"))
    world = _setup_world(tmp, {"stale-node": ("2026-01-01", "2026-05-01")})
    r = _run(world, ["--exit-on-ahead"])
    assert r.returncode == 0, \
        f"stale-only should exit 0, got {r.returncode}: {r.stderr}"

    # An index_ahead node -> exit 2 (drift the recurring goal must surface).
    tmp2 = Path(tempfile.mkdtemp(prefix="drift-exit2-"))
    world2 = _setup_world(tmp2, {"ahead-node": ("2026-06-01", "2026-01-01")})
    r2 = _run(world2, ["--exit-on-ahead"])
    assert r2.returncode == 2, \
        f"index_ahead should exit 2, got {r2.returncode}: {r2.stderr}"


def test_missing_front_matter_is_skipped_not_crashed():
    #  (original drift-check suite): a node .md with NO front matter
    # is skipped by audit and --apply, never crashed.
    tmp = Path(tempfile.mkdtemp(prefix="drift-nofm-"))
    world = _setup_world(tmp, {
        "good-node": ("2026-01-01", "2026-05-01"),
        "nofm-node": ("2026-02-01", None),   # .md has no front matter
    })
    r = _run(world, [])
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["errors"].get("no_front_matter") == 1
    assert d["index_stale"] == 1   # only good-node classified

    # --apply must skip the no-fm node, not crash or backfill it.
    ra = _run(world, ["--apply"])
    assert ra.returncode == 0, ra.stderr
    da = json.loads(ra.stdout)
    assert da["backfilled"] == 1
    assert da["skipped_errors"].get("no_front_matter") == 1


def test_full_emits_complete_lists_beyond_sample_cap():
    # : --full must emit EVERY index_ahead/index_stale entry, not the
    # capped [:8] sample. Set up 10 index_ahead nodes so the sample (8) and the
    # full list (10) are distinguishable — the whole point of the flag: a
    # capped sample cannot prove a property (e.g. "no entry has idx > cutoff")
    # across the full set, which left a hypothesis structurally unresolvable.
    tmp = Path(tempfile.mkdtemp(prefix="drift-full-"))
    specs = {f"ahead-{i:02d}": (f"2026-06-{i + 1:02d}", "2026-01-01")
             for i in range(10)}   # 10 index_ahead nodes (idx newer than fm)
    world = _setup_world(tmp, specs)

    # Default (no --full): full keys ABSENT, sample capped at 8, count == 10.
    r = _run(world, [])
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["index_ahead"] == 10
    assert len(d["sample_index_ahead"]) == 8            # capped sample
    assert "index_ahead_full" not in d                  # backward-compat
    assert "index_stale_full" not in d

    # --full: complete list present, len == count (10) — NOT capped at 8.
    rf = _run(world, ["--full"])
    assert rf.returncode == 0, rf.stderr
    df = json.loads(rf.stdout)
    assert "index_ahead_full" in df
    assert len(df["index_ahead_full"]) == df["index_ahead"] == 10
    assert len(df["index_ahead_full"]) > len(df["sample_index_ahead"])
    assert all(set(e.keys()) == {"key", "idx", "fm"}
               for e in df["index_ahead_full"])
    assert "index_stale_full" in df                     # present even when empty

    # --full does NOT alter the exit contract (exit 0 without --exit-on-ahead).
    assert rf.returncode == 0


def test_apply_last_updated_is_a_newer_wins_merge_field():
    """ regression pin (sq-019): the whole reason --apply refuses to
    write the index_ahead direction is that `last_updated` merges NEWER-wins
    (coordination_merge._TREE_NEWER_FIELDS), so a backward write either
    reverts on the next peer merge (rb-7277, guard-3209) or persists on a
    stale mirror and corrupts a correct index (bravo cc-05, 2026-09-30).

    This test FAILS loudly if someone "fixes" the drift check by re-enabling
    the backward write (i.e. by reclassifying the field out of NEWER): the
    premise of the report-don't-write remedy is asserted here, not just
    documented. It also pins that the class lookup actually classifies
    `last_updated` as NEWER -- a future author who moves the tuple without
    updating the lookup still gets a red build with the WHY in the message.
    """
    sys.path.insert(0, str(CORE_SCRIPTS))
    try:
        import coordination_merge as cm
    finally:
        sys.path.remove(str(CORE_SCRIPTS))
    assert "last_updated" in cm._TREE_NEWER_FIELDS, (
        "g-115-5509 premise broken: last_updated is no longer a NEWER-wins "
        "merge field. The tree-last-updated-drift-check --apply remedy "
        "(report index_ahead, never write it backward) exists ONLY because "
        "the backward write cannot survive the newer-wins merge. If this "
        "field changed merge class on purpose, re-open g-115-5509 before "
        "restoring the two-direction backfill.")
    assert cm._classify_tree_field("last_updated") == "NEWER", (
        "g-115-5509 premise broken: _classify_tree_field no longer maps "
        "last_updated to NEWER -- the class-membership premise of the "
        "report-don't-write remedy has changed; re-open g-115-5509.")
